"""Explicit fault-point selection and transient, geometry-independent results."""
from __future__ import annotations

from dataclasses import dataclass
import cmath
import html
import math

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QGraphicsObject, QLabel, QTextBrowser, QVBoxLayout)

from ..calculation.point_fault import PointFaultTarget
from ..core.fault_types import FaultSpec, FaultType


FAULT_LABELS = {
    FaultType.THREE_PHASE: 'КЗ(3)',
    FaultType.LINE_LINE: 'КЗ(2)',
    FaultType.LINE_GROUND: 'КЗ(1)',
    FaultType.LINE_LINE_GROUND: 'КЗ(2,1)',
}
FAULT_DESCRIPTIONS = {
    FaultType.THREE_PHASE: 'Трёхфазное (ABC)',
    FaultType.LINE_LINE: 'Двухфазное (BC)',
    FaultType.LINE_GROUND: 'Однофазное на землю (AG)',
    FaultType.LINE_LINE_GROUND: 'Двухфазное на землю (BCG)',
}
FAULT_COLORS = ('#1d4ed8', '#7e22ce', '#b45309', '#047857')


def point_fault_icon():
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#b91c1c'), 2))
    painter.drawLine(2, 10, 18, 10)
    painter.drawLine(10, 2, 10, 18)
    painter.setBrush(QColor('#dc2626'))
    painter.drawEllipse(QPointF(10, 10), 4, 4)
    painter.end()
    return QIcon(pixmap)


@dataclass(frozen=True)
class FaultPointChoice:
    label: str
    target: PointFaultTarget


def point_choices(model, context):
    """Resolve canonical physical endpoints; screen coordinates are never input."""
    if (node_id := context.get('node_id')) is not None:
        node = model.electrical_nodes.get(node_id)
        return (FaultPointChoice(node.name, PointFaultTarget(node_id=node.id)),) if node else ()
    if (port_id := context.get('port_id')) is not None:
        ports = (port_id,)
    else:
        equipment = model.equipment.get(context.get('equipment_id'))
        ports = equipment.port_ids if equipment is not None else ()
    choices = []
    for pid in ports:
        if pid not in model.ports or model.node_for_port(pid) is None:
            continue
        definition = model.port_definition(pid)
        node = model.node_for_port(pid)
        prefix = {'from': 'В начале', 'to': 'В конце'}.get(definition.role, 'Сторона') if context.get('physical_route') else 'Вывод'
        label = f'{prefix}: {definition.display_name} — {node.name}'
        choices.append(FaultPointChoice(label, PointFaultTarget(port_id=pid)))
    return tuple(choices)


class PointFaultDialog(QDialog):
    """A local choice only; accepting the dialog does not run a full project."""
    def __init__(self, model, context, mode_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Рассчитать КЗ здесь')
        self.setMinimumWidth(510)
        self.setObjectName('pointFaultDialog')
        self.choices = point_choices(model, context)
        layout = QVBoxLayout(self)
        self.mode_label = QLabel('Текущий режим: ' + mode_name)
        self.mode_label.setWordWrap(True)
        layout.addWidget(self.mode_label)
        layout.addWidget(QLabel('Расчётная точка / физическая сторона:'))
        self.point_selector = QComboBox()
        self.point_selector.setObjectName('faultPointSide')
        if len(self.choices) > 1:
            self.point_selector.addItem('Выберите сторону…', None)
        for choice in self.choices:
            self.point_selector.addItem(choice.label, choice.target)
        layout.addWidget(self.point_selector)
        if context.get('physical_route'):
            note = QLabel('На линии доступны её начало и конец. Внутренняя точка КЛ/ВЛ пока не поддерживается; расстояние по рисунку не используется.')
            note.setWordWrap(True)
            layout.addWidget(note)
        self.checks = {}
        for kind, label in FAULT_LABELS.items():
            check = QCheckBox(label + ' — ' + FAULT_DESCRIPTIONS[kind])
            check.setChecked(True)
            check.toggled.connect(self._validate)
            self.checks[kind] = check
            layout.addWidget(check)
        self.message = QLabel('')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addWidget(QLabel('Первичные токи в точке КЗ, кА. Сопротивление повреждения: 0 Ом.'))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.calculate_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.calculate_button.setText('Рассчитать выбранные КЗ')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.point_selector.currentIndexChanged.connect(self._validate)
        self._validate()

    @property
    def target(self):
        return self.point_selector.currentData()

    @property
    def specs(self):
        return tuple(FaultSpec(kind) for kind, check in self.checks.items() if check.isChecked())

    def _validate(self):
        if not hasattr(self, 'calculate_button'):
            return
        reason = ('Нет подключённого физического вывода. Сначала соедините его с узлом.' if not self.choices
                  else 'Выберите сторону аппарата.' if self.target is None
                  else 'Выберите хотя бы один вид КЗ.' if not self.specs else '')
        self.message.setText(reason)
        self.calculate_button.setEnabled(not reason)


def outcome_row(outcome):
    label = FAULT_LABELS[outcome.spec.kind]
    if not outcome.available:
        return f'{label}: нет результата', outcome.message or outcome.code
    currents = outcome.fault.iabc_ka
    if outcome.spec.kind is FaultType.LINE_LINE_GROUND:
        value = max(abs(currents[1]), abs(currents[2]))
        measured = 'max(|IB|, |IC|)'
    elif outcome.spec.kind is FaultType.LINE_LINE:
        value, measured = abs(currents[1]), '|IB|'
    else:
        value, measured = abs(currents[0]), '|IA|'
    return f'{label}: {measured} = {value:.3f} кА', ''


def point_details_html(result):
    escape = html.escape
    sections = [f'<h3>{escape(result.node_name)}</h3><p>Режим: {escape(result.mode_name)}<br>'
                'Первичные токи в точке КЗ на её ступени напряжения. Модули / углы, кА / °. Z повреждения = 0 Ом.</p>']
    for row in result.outcomes:
        sections.append('<h4>' + escape(outcome_row(row)[0]) + '</h4>')
        if not row.available:
            sections.append('<p>' + escape(row.message or row.code) + '</p>')
            continue
        parts = []
        for label, value in zip(('IA', 'IB', 'IC'), row.fault.iabc_ka):
            parts.append(f'{label}: {abs(value):.6f} кА ∠ {math.degrees(cmath.phase(value)):.2f}°')
        parts.append(f'3I0: {abs(row.fault.residual_current_ka):.6f} кА')
        sections.append('<p>' + '<br>'.join(parts) + '</p>')
        if row.fault.assumptions:
            sections.append('<p>' + '<br>'.join(escape(x) for x in row.fault.assumptions) + '</p>')
    if result.warnings:
        sections.append('<p>' + '<br>'.join(escape(x) for x in result.warnings) + '</p>')
    return ''.join(sections)


class PointFaultDetailsDialog(QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle('КЗ в выбранной точке — подробности')
        self.resize(650, 560)
        layout = QVBoxLayout(self)
        self.text = QTextBrowser()
        self.text.setHtml(point_details_html(result))
        layout.addWidget(self.text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def invalidate(self):
        self.text.setPlainText('Исходные данные, выбранный режим или запрос изменились. Прежние числа скрыты. Выполните расчёт выбранной точки снова.')


class PointFaultWorker(QThread):
    progressed = Signal(int, int, str)
    completed = Signal(object, str)

    def __init__(self, request, prepared_mode=None, parent=None):
        super().__init__(parent)
        self.request = request
        self.prepared_mode = prepared_mode

    def run(self):
        from ..calculation.point_fault import run_point_faults
        from ..calculation.input import CalculationCancelled
        try:
            result = run_point_faults(self.request, prepared_mode=self.prepared_mode,
                cancelled=self.isInterruptionRequested, progress=self.progressed.emit)
            if self.isInterruptionRequested():
                self.completed.emit(None, 'Расчёт КЗ отменён.')
            else:
                self.completed.emit(result, '')
        except CalculationCancelled:
            self.completed.emit(None, 'Расчёт КЗ отменён.')
        except Exception as exc:
            self.completed.emit(None, str(exc))


class PointFaultOverlay(QGraphicsObject):
    detailsRequested = Signal()

    def __init__(self, result):
        super().__init__()
        self.result = result
        self.rows = tuple(outcome_row(row)[0] for row in result.outcomes)
        self.setFlag(self.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setZValue(10000)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip('Нажмите для подробностей.\n' + '\n'.join(
            text + (' — ' + reason if reason else '') for text, reason in map(outcome_row, result.outcomes)))

    def boundingRect(self):
        return QRectF(0, 0, 350, 59 + 22 * len(self.rows))

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#94a3b8'), 1))
        painter.setBrush(QColor('#ffffff'))
        painter.drawRoundedRect(self.boundingRect().adjusted(.5, .5, -.5, -.5), 6, 6)
        painter.setFont(QFont('Segoe UI', 9))
        painter.setPen(QColor('#1e293b'))
        metrics = painter.fontMetrics()
        title = metrics.elidedText('КЗ: ' + self.result.node_name, Qt.TextElideMode.ElideRight, 330)
        painter.drawText(QPointF(10, 18), title)
        for index, row in enumerate(self.result.outcomes):
            painter.setPen(QColor(FAULT_COLORS[list(FAULT_LABELS).index(row.spec.kind)]))
            painter.drawText(QPointF(10, 41 + index * 22), self.rows[index])
        painter.setPen(QColor('#475569'))
        footer = metrics.elidedText(self.result.mode_name + ' · Подробнее…', Qt.TextElideMode.ElideRight, 330)
        painter.drawText(QPointF(10, self.boundingRect().height() - 9), footer)

    def mousePressEvent(self, event):
        event.accept()
        self.detailsRequested.emit()


class PointFaultOverlayController(QObject):
    """Track only drawing anchors during drag, never recalculate or fingerprint."""
    detailsRequested = Signal()

    def __init__(self, scene):
        super().__init__(scene)
        self.scene = scene
        self.item = None
        self.context = {}
        scene.changed.connect(self.refresh_anchor)

    def set_result(self, result, context=None):
        self.context = dict(context or {})
        if self.item is not None and self.item.result is not result:
            self.scene.removeItem(self.item)
            self.item.deleteLater()
            self.item = None
        if result is not None and self.item is None:
            self.item = PointFaultOverlay(result)
            self.item.detailsRequested.connect(self.detailsRequested.emit)
            self.scene.addItem(self.item)
        self.refresh_anchor()

    def _anchor(self):
        target = self.item.result.request.target
        owner = self.scene._items_by_id.get(self.context.get('representation_id'))
        if target.port_id is not None:
            owners = (owner,) if owner is not None else self.scene._items_by_id.values()
            for candidate in owners:
                port = candidate._port_items.get(target.port_id)
                if port is not None and candidate.isVisible() and not getattr(candidate, '_suppress_body', False):
                    return port.scenePos()
            route = self.scene._route_items_by_id.get(self.context.get('route_id'))
            routes = (route,) if route is not None else self.scene._route_items_by_id.values()
            for candidate in routes:
                for start, anchor in ((True, candidate.route.start_anchor), (False, candidate.route.end_anchor)):
                    if target.port_id in (anchor.branch_port_id, anchor.target_port_id):
                        return candidate._path.pointAtPercent(0 if start else 1)
            return None
        if owner is not None and owner.representation.electrical_node_id == target.node_id:
            local = self.context.get('local_point')
            return owner.mapToScene(QPointF(*local)) if local is not None else owner.scenePos()
        route = self.scene._route_items_by_id.get(self.context.get('route_id'))
        if route is not None and route.route.electrical_node_id == target.node_id:
            return route._path.pointAtPercent(self.context.get('route_fraction', .5))
        for candidate in self.scene._items_by_id.values():
            if candidate.representation.electrical_node_id == target.node_id and candidate.isVisible():
                return candidate.scenePos()
        return None

    def refresh_anchor(self, *_):
        if self.item is None:
            return
        anchor = self._anchor()
        self.item.setVisible(anchor is not None)
        if anchor is not None:
            position = anchor + QPointF(18, -18)
            if self.item.pos() != position:
                self.item.setPos(position)
