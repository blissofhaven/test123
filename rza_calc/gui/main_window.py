# -*- coding: utf-8 -*-
"""Главное окно РЗА-Про: редактор схемы и вкладка анализа."""
from __future__ import annotations

from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import (
    QTreeWidgetItemIterator,
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QGraphicsItem, QGraphicsPathItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter,
    QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.model import GeneratorBranch
from ..core.result import FAIL, OK, UNRESOLVED
from ..editor import ProjectEditorController
from ..io.project import save_project
from .editor_panels import EditorWorkspaceWidget
from .analysis_scheme import AnalysisSchemeView
from .project_settings import ProjectSettings
from .theme import COLORS, DiagramColorMode, voltage_stroke
from .view_model import FAULT_TYPE_LABELS, ProjectViewModel, TreeEntry, fault_status_label


def _presentation_render(method):
    """Share reads only across one synchronous window presentation pass."""
    @wraps(method)
    def render(self, *args, **kwargs):
        # __init__ receives its VM before QMainWindow/self.vm exist.
        vm = (args[0] if args and isinstance(args[0], ProjectViewModel)
              else kwargs.get("vm"))
        if vm is None:
            vm = self.vm
        with vm.presentation_snapshot():
            return method(self, *args, **kwargs)
    return render


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            _clear_layout(child)


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    return table


def _set_rows(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            if value in ("Норма", "✓"):
                item.setForeground(QColor(COLORS["green"]))
            elif value in ("Ошибка", "✗"):
                item.setForeground(QColor(COLORS["red"]))
            elif value in ("Не определено", "?"):
                item.setForeground(QColor(COLORS["amber"]))
            table.setItem(row_index, column, item)


def _phase_magnitudes(values) -> str:
    if values is None:
        return "—"
    return " / ".join(f"{abs(value):.4f}".rstrip("0").rstrip(".").replace(".", ",")
                      for value in values)


def _fault_status(row) -> str:
    if row.error:
        label = fault_status_label(row.status_code)
        return row.error if row.error.startswith(label) else f"{label}: {row.error}"
    return "С допущением" if row.assumptions else "Рассчитано"


def _fault_table_widths(table: QTableWidget) -> None:
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)


def _set_fault_rows(table: QTableWidget, rows: list[list[str]]) -> None:
    _set_rows(table, rows)
    for index, values in enumerate(rows):
        # Длинная причина доступна и у режима, который сейчас не выбран
        # в шапке: таблица может сокращать текст по ширине колонки.
        table.item(index, len(values) - 1).setToolTip(values[-1])


class GeneratorCard(QFrame):
    toggled = Signal(str, bool)

    def __init__(self, branch_id: str, parent=None):
        super().__init__(parent)
        self.branch_id = branch_id
        self.setObjectName("card")
        self.setFixedWidth(108)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(2)
        top = QHBoxLayout()
        self.check = QCheckBox()
        self.name_label = QLabel()
        self.name_label.setObjectName("cardTitle")
        top.addWidget(self.check)
        top.addWidget(self.name_label, 1)
        self.power_label = QLabel()
        self.power_label.setObjectName("cardCaption")
        self.state_label = QLabel()
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(top)
        layout.addWidget(self.power_label)
        layout.addWidget(self.state_label)
        self.check.toggled.connect(lambda value: self.toggled.emit(self.branch_id, value))

    def update_data(self, name: str, power: float, enabled: bool) -> None:
        self.name_label.setText(name)
        self.power_label.setText(f"{power:g} МВт")
        self.check.blockSignals(True)
        self.check.setChecked(enabled)
        self.check.blockSignals(False)
        self.state_label.setText("ВКЛ" if enabled else "ОТКЛ")
        self.state_label.setObjectName("statusOk" if enabled else "muted")
        self.state_label.setStyleSheet(
            f"color: {COLORS['green'] if enabled else COLORS['muted']}; font-weight: 700;"
        )


class HeaderWidget(QWidget):
    modeSelected = Signal(str)
    customRequested = Signal()
    generatorChanged = Signal(str, bool)
    recalcRequested = Signal()
    reportRequested = Signal()
    checksRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, vm: ProjectViewModel, parent=None):
        super().__init__(parent)
        self.setObjectName("header")
        self.setMinimumHeight(112)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(12)

        brand_box = QHBoxLayout()
        menu = QPushButton("☰")
        menu.setObjectName("toolButton")
        menu.setToolTip("Меню проекта")
        brand_text = QVBoxLayout()
        title = QLabel("РЗА-Про")
        title.setObjectName("brand")
        subtitle = QLabel("Расчёт уставок и селективности")
        subtitle.setObjectName("brandSub")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_box.addWidget(menu)
        brand_box.addLayout(brand_text)
        outer.addLayout(brand_box)

        mode_frame = QFrame()
        mode_frame.setObjectName("modeCard")
        mode_layout = QVBoxLayout(mode_frame)
        mode_layout.setContentsMargins(10, 7, 10, 7)
        mode_layout.setSpacing(5)
        mode_layout.addWidget(QLabel("Режим сети"))
        buttons = QHBoxLayout()
        buttons.setSpacing(5)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        for mode_id, title_text in (("max", "Максимальный"), ("min", "Минимальный")):
            button = QPushButton(title_text)
            button.setObjectName("modeButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, mid=mode_id: self.modeSelected.emit(mid))
            self.mode_group.addButton(button)
            self.mode_buttons[mode_id] = button
            buttons.addWidget(button)
        custom = QPushButton("Пользовательский")
        custom.setObjectName("modeButton")
        custom.setCheckable(True)
        custom.clicked.connect(self.customRequested.emit)
        self.mode_group.addButton(custom)
        self.mode_buttons["gui_custom"] = custom
        buttons.addWidget(custom)
        mode_layout.addLayout(buttons)
        bottom = QHBoxLayout()
        hint = QLabel("ТКЗ рассчитываются по составу работающих машин")
        hint.setObjectName("helpText")
        self.scenario = QComboBox()
        self.scenario.setToolTip("Другие сохранённые режимы сети")
        self.scenario.setMinimumWidth(180)
        self.scenario.currentIndexChanged.connect(self._scenario_changed)
        bottom.addWidget(hint, 1)
        bottom.addWidget(self.scenario)
        mode_layout.addLayout(bottom)
        outer.addWidget(mode_frame)

        self.generator_layout = QHBoxLayout()
        self.generator_layout.setSpacing(7)
        self.generator_cards: dict[str, GeneratorCard] = {}
        outer.addLayout(self.generator_layout)

        self.summary = QFrame()
        self.summary.setObjectName("summaryCard")
        self.summary.setFixedWidth(135)
        summary_layout = QVBoxLayout(self.summary)
        summary_layout.setContentsMargins(11, 7, 11, 7)
        caption = QLabel("Суммарная мощность")
        caption.setObjectName("cardCaption")
        self.total_label = QLabel("—")
        self.total_label.setObjectName("summaryValue")
        self.cos_label = QLabel("по включённым ГТГ")
        self.cos_label.setObjectName("cardCaption")
        summary_layout.addWidget(caption)
        summary_layout.addWidget(self.total_label)
        summary_layout.addWidget(self.cos_label)
        outer.addWidget(self.summary)

        for glyph, title_text, signal in (
            ("↻", "Пересчитать", self.recalcRequested),
            ("▤", "Отчёт", self.reportRequested),
            ("♢", "Проверки", self.checksRequested),
            ("⚙", "Настройки", self.settingsRequested),
        ):
            button = QPushButton(f"{glyph}\n{title_text}")
            button.setObjectName("actionButton")
            button.clicked.connect(signal.emit)
            outer.addWidget(button)

        self.refresh(vm)

    def _scenario_changed(self, index: int) -> None:
        mode_id = self.scenario.itemData(index)
        if mode_id:
            self.modeSelected.emit(str(mode_id))

    def refresh(self, vm: ProjectViewModel) -> None:
        choices = vm.mode_choices()
        self.scenario.blockSignals(True)
        self.scenario.clear()
        self.scenario.addItem("Другие режимы…", None)
        for mode_id, name in choices:
            if mode_id not in ("max", "min"):
                self.scenario.addItem(name, mode_id)
        if vm.mode_id not in ("max", "min", "gui_custom"):
            index = self.scenario.findData(vm.mode_id)
            self.scenario.setCurrentIndex(max(0, index))
        else:
            self.scenario.setCurrentIndex(0)
        self.scenario.blockSignals(False)

        for mode_id, button in self.mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(vm.mode_id == mode_id)
            button.blockSignals(False)
        if vm.mode_id not in self.mode_buttons:
            self.mode_group.setExclusive(False)
            for button in self.mode_buttons.values():
                button.setChecked(False)
            self.mode_group.setExclusive(True)

        statuses = vm.generators()
        existing = set(self.generator_cards)
        wanted = {item.branch_id for item in statuses}
        if existing != wanted:
            _clear_layout(self.generator_layout)
            self.generator_cards.clear()
            for item in statuses:
                card = GeneratorCard(item.branch_id)
                card.toggled.connect(self.generatorChanged.emit)
                self.generator_layout.addWidget(card)
                self.generator_cards[item.branch_id] = card
        for item in statuses:
            self.generator_cards[item.branch_id].update_data(
                item.name, item.power_mw, item.enabled
            )
        self.total_label.setText(f"{vm.total_generation_mw():g} МВт")


class NetworkTreePanel(QWidget):
    selectionRequested = Signal(str, str)

    ICONS = {
        "project": "▣", "facility": "▦", "level": "⌁", "node": "━",
        "branch": "↕", "load": "▼", "group": "▹", "bay": "□",
        "equipment": "◇", "section": "━",
    }

    def __init__(self, vm: ProjectViewModel, parent=None):
        super().__init__(parent)
        self.setObjectName("leftPanel")
        self.setMinimumWidth(250)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 10, 10)
        layout.setSpacing(8)
        title = QLabel("Структура сети")
        title.setObjectName("sectionTitle")
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕  Поиск по сети…")
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self._selected)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(title)
        layout.addWidget(self.search)
        layout.addWidget(self.tree, 1)
        self.refresh(vm)

    def refresh(self, vm: ProjectViewModel) -> None:
        """Перестроить дерево, сохранив раскрытые ветки, выбор и прокрутку."""
        expanded, selected = set(), None
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if key and item.isExpanded():
                expanded.add(str(key))
            if key and item.isSelected():
                selected = str(key)
            iterator += 1
        scroll = self.tree.verticalScrollBar().value()
        first_build = self.tree.topLevelItemCount() == 0

        self.tree.blockSignals(True)
        self.tree.clear()
        for entry in vm.tree():
            self.tree.addTopLevelItem(self._item(entry))
        if first_build:
            self.tree.expandToDepth(1)
        else:
            iterator = QTreeWidgetItemIterator(self.tree)
            while iterator.value():
                item = iterator.value()
                key = item.data(0, Qt.ItemDataRole.UserRole)
                if key and str(key) in expanded:
                    item.setExpanded(True)
                if key and selected and str(key) == selected:
                    item.setSelected(True)
                    self.tree.setCurrentItem(item)
                iterator += 1
        self.tree.blockSignals(False)
        self.tree.verticalScrollBar().setValue(scroll)
        if self.search.text().strip():
            self._filter(self.search.text())

    def _item(self, entry: TreeEntry) -> QTreeWidgetItem:
        prefix = self.ICONS.get(entry.kind, "•")
        item = QTreeWidgetItem([f"{prefix}  {entry.title}"])
        item.setToolTip(0, entry.subtitle or entry.title)
        item.setData(0, Qt.ItemDataRole.UserRole, entry.key)
        if entry.kind in ("project", "facility"):
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        for child in entry.children:
            item.addChild(self._item(child))
        return item

    def _selected(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        key = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if ":" not in key:
            return
        kind, object_id = key.split(":", 1)
        if kind in ("node", "branch", "load"):
            self.selectionRequested.emit(kind, object_id)

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()

        def visit(item: QTreeWidgetItem) -> bool:
            child_visible = False
            for index in range(item.childCount()):
                child_visible = visit(item.child(index)) or child_visible
            own = needle in item.text(0).casefold()
            visible = not needle or own or child_visible
            item.setHidden(not visible)
            if needle and child_visible:
                item.setExpanded(True)
            return visible

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))


class _HoverPathItem(QGraphicsPathItem):
    """Широкая область выбора ветви с подсветкой при наведении."""

    def __init__(self, path: QPainterPath):
        super().__init__(path)
        self.setAcceptHoverEvents(True)
        self._normal_pen = QPen(QColor(0, 0, 0, 1), 20.0)
        self._hover_pen = QPen(QColor(37, 99, 235, 48), 20.0)
        self._hover_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self._hover_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(self._normal_pen)

    def hoverEnterEvent(self, event) -> None:
        self.setPen(self._hover_pen)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.setPen(self._normal_pen)
        super().hoverLeaveEvent(event)


class _HoverRectItem(QGraphicsRectItem):
    """Мягкая подсветка шин, узлов и нагрузок при наведении."""

    def __init__(self, rect: QRectF):
        super().__init__(rect)
        self.setAcceptHoverEvents(True)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(QBrush(QColor(255, 255, 255, 1)))

    def hoverEnterEvent(self, event) -> None:
        self.setBrush(QBrush(QColor(37, 99, 235, 28)))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.setBrush(QBrush(QColor(255, 255, 255, 1)))
        super().hoverLeaveEvent(event)


class DiagramView(QGraphicsView):
    selectionRequested = Signal(str, str)

    def __init__(self, vm: ProjectViewModel, parent=None):
        self.diagram_scene = QGraphicsScene()
        super().__init__(self.diagram_scene, parent)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setBackgroundBrush(QColor("#FBFCFE"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._selection_geometries: dict[tuple[str, str], QPainterPath] = {}
        self._selection_overlay: QGraphicsPathItem | None = None
        self._bus_widths: dict[str, float] = {}
        self.diagram_scene.selectionChanged.connect(self._selection_changed)
        self.refresh(vm)

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def fit_all(self) -> None:
        rect = self.diagram_scene.itemsBoundingRect().adjusted(-45, -45, 45, 45)
        if not rect.isEmpty():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            self.scale(1.08, 1.08)

    def refresh(self, vm: ProjectViewModel) -> None:
        self.diagram_scene.clear()
        self._selection_overlay = None
        self._selection_geometries.clear()
        positions = self._layout(vm)
        mode = vm.mode
        incident: dict[str, list[str]] = defaultdict(list)
        for branch in vm.net.branches.values():
            if branch.node_from in vm.net.nodes:
                incident[branch.node_from].append(branch.id)
            if branch.node_to in vm.net.nodes:
                incident[branch.node_to].append(branch.id)
        self._bus_widths = {
            node_id: max(150.0, min(310.0, 92.0 + len(incident[node_id]) * 28.0))
            for node_id, node in vm.net.nodes.items()
            if node.kind == "bus" and not node_id.endswith("__star")
        }
        anchors = self._anchors(vm, positions, incident)

        # Сначала ветви, затем аппараты и шины. У каждой ветви есть широкая
        # невидимая область выбора — попадать ровно в тонкую линию не нужно.
        for branch in vm.net.branches.values():
            active = mode.is_closed(branch) if mode else branch.normally_closed
            start = anchors.get((branch.node_from, branch.id), positions.get(branch.node_from))
            end = anchors.get((branch.node_to, branch.id), positions.get(branch.node_to))
            if start is None and branch.node_from == "GRID" and end is not None:
                start = QPointF(end.x(), end.y() - 104)
            if end is None and branch.node_to == "GRID" and start is not None:
                end = QPointF(start.x(), start.y() - 104)
            if start is None or end is None:
                continue
            path = self._orthogonal_path(start, end)
            self._selection_geometries[("branch", branch.id)] = path
            item = QGraphicsPathItem(path)
            voltage = max(
                vm.net.nodes.get(branch.node_from).u_nom if branch.node_from in vm.net.nodes else 0,
                vm.net.nodes.get(branch.node_to).u_nom if branch.node_to in vm.net.nodes else 0,
            )
            color = (
                QColor(voltage_stroke(int(round(voltage * 1_000.0))))
                if active
                else QColor("#AEB8C6")
            )
            pen = QPen(color, 2.0 if active else 1.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            if not active:
                pen.setStyle(Qt.PenStyle.DashLine)
            elif branch.kind == "line" and getattr(branch, "line_type", "") == "overhead":
                pen.setStyle(Qt.PenStyle.DashLine)
            elif branch.kind == "tie" and not active:
                pen.setStyle(Qt.PenStyle.DashLine)
            item.setPen(pen)
            item.setZValue(0)
            self.diagram_scene.addItem(item)

            middle = path.pointAtPercent(0.5)
            has_breaker = bool(getattr(branch, "ct_ratio", None)) or branch.kind == "tie"
            if has_breaker:
                breaker_point = middle if branch.kind == "tie" else path.pointAtPercent(0.27)
                self._add_breaker(breaker_point, color, active)

            if branch.kind == "transformer":
                physical = next(
                    (transformer for transformer in vm.net.transformers3w.values()
                     if branch.id in transformer.branch_ids),
                    None,
                )
                if physical is None or branch.id == physical.id:
                    vertical = abs(end.y() - start.y()) >= abs(end.x() - start.x())
                    self._add_transformer(
                        middle, color, active, three_winding=physical is not None,
                        vertical=vertical,
                    )

            if branch.kind in ("generator", "source"):
                source_point = start if branch.node_from == "GRID" else end
                self._add_source_symbol(vm, branch, source_point, color, active)

            if branch.kind in ("generator", "transformer", "line"):
                label = QGraphicsSimpleTextItem(self._branch_caption(branch))
                selected = vm.selected_kind == "branch" and vm.selected_id == branch.id
                label.setBrush(QBrush(QColor(COLORS["blue"] if selected else "#4B586D")))
                label.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold if selected else QFont.Weight.Normal))
                bounds = label.boundingRect()
                label.setPos(middle.x() + 8, middle.y() - bounds.height() - 5)
                label.setZValue(3)
                self.diagram_scene.addItem(label)

            tooltip = (
                f"{branch.name}\n{'Включён' if active else 'Отключён'}\n"
                f"{branch.node_from} → {branch.node_to}\n"
                "Щёлкните в любом месте вдоль линии"
            )
            hit = _HoverPathItem(path)
            hit.setZValue(6)
            self._make_selectable(hit, "branch", branch.id, tooltip)
            self.diagram_scene.addItem(hit)

        for node_id, position in positions.items():
            node = vm.net.nodes[node_id]
            compact = node.id.endswith("__star")
            color = QColor(voltage_stroke(int(round(node.u_nom * 1_000.0))))
            if compact:
                dot = self.diagram_scene.addEllipse(
                    position.x() - 3.5, position.y() - 3.5, 7, 7,
                    QPen(color, 1.3), QBrush(QColor("#FFFFFF")),
                )
                dot.setZValue(3)
                geometry = QPainterPath()
                geometry.addEllipse(QRectF(position.x() - 12, position.y() - 12, 24, 24))
            elif node.kind == "bus":
                width = self._bus_widths.get(node_id, 160.0)
                bus = self.diagram_scene.addLine(
                    position.x() - width / 2, position.y(),
                    position.x() + width / 2, position.y(),
                    QPen(color, 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap),
                )
                bus.setZValue(2)
                self._add_text(
                    f"{node.name} · {node.u_nom:g} кВ", position.x() - width / 2,
                    position.y() - 24, 8.5, COLORS["text"], bold=True,
                )
                fault = self._fault_text(vm, node_id)
                if fault:
                    fault_item = self._add_text(
                        fault, position.x() + width / 2, position.y() - 23,
                        8.0, color.name(), bold=True,
                    )
                    fault_item.setX(position.x() + width / 2 - fault_item.boundingRect().width())
                geometry = QPainterPath()
                geometry.addRoundedRect(
                    QRectF(position.x() - width / 2 - 7, position.y() - 30,
                           width + 14, 46), 6, 6,
                )
            else:
                junction = self.diagram_scene.addEllipse(
                    position.x() - 4, position.y() - 4, 8, 8,
                    QPen(color, 1.5), QBrush(QColor("#FFFFFF")),
                )
                junction.setZValue(3)
                self._add_text(node.name, position.x() + 8, position.y() - 17,
                               7.5, COLORS["muted"])
                geometry = QPainterPath()
                geometry.addRoundedRect(
                    QRectF(position.x() - 13, position.y() - 17, 130, 34), 5, 5,
                )
            self._selection_geometries[("node", node_id)] = geometry
            hit_rect = geometry.boundingRect()
            hit = _HoverRectItem(hit_rect)
            hit.setZValue(5)
            self._make_selectable(hit, "node", node_id, f"{node.name}\n{node.u_nom:g} кВ")
            self.diagram_scene.addItem(hit)

        self._draw_loads(vm, positions)
        self.diagram_scene.setSceneRect(self.diagram_scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        self.set_selection(vm.selected_kind, vm.selected_id)
        self.fit_all()

    def _anchors(self, vm: ProjectViewModel, positions: dict[str, QPointF],
                 incident: dict[str, list[str]]) -> dict[tuple[str, str], QPointF]:
        anchors: dict[tuple[str, str], QPointF] = {}
        for node_id, branch_ids in incident.items():
            node = vm.net.nodes[node_id]
            center = positions[node_id]
            if node.kind != "bus" or node_id.endswith("__star"):
                for branch_id in branch_ids:
                    anchors[(node_id, branch_id)] = center
                continue
            width = self._bus_widths.get(node_id, 160.0)

            def other_x(branch_id: str) -> float:
                branch = vm.net.branches[branch_id]
                other = branch.node_to if branch.node_from == node_id else branch.node_from
                return positions.get(other, QPointF(center.x(), center.y() - 100)).x()

            ordered = sorted(branch_ids, key=lambda branch_id: (other_x(branch_id), branch_id))
            for index, branch_id in enumerate(ordered, 1):
                offset = -width / 2 + width * index / (len(ordered) + 1)
                anchors[(node_id, branch_id)] = QPointF(center.x() + offset, center.y())
        return anchors

    @staticmethod
    def _orthogonal_path(start: QPointF, end: QPointF) -> QPainterPath:
        path = QPainterPath(start)
        if abs(start.y() - end.y()) < 8:
            path.lineTo(end)
            return path
        middle_y = (start.y() + end.y()) / 2
        path.lineTo(start.x(), middle_y)
        path.lineTo(end.x(), middle_y)
        path.lineTo(end)
        return path

    def _add_breaker(self, point: QPointF, color: QColor, active: bool) -> None:
        breaker = self.diagram_scene.addRect(
            point.x() - 6, point.y() - 6, 12, 12,
            QPen(color, 1.7),
            QBrush(QColor("#FFFFFF") if active else QColor("#EEF1F5")),
        )
        breaker.setZValue(3)

    def _add_transformer(self, point: QPointF, color: QColor, active: bool,
                         *, three_winding: bool, vertical: bool) -> None:
        pen = QPen(color, 1.7)
        fill = QBrush(QColor("#FFFFFF") if active else QColor("#F3F5F7"))
        if three_winding:
            offsets = ((0, -8), (-8, 7), (8, 7)) if vertical else ((-8, 0), (7, -8), (7, 8))
        else:
            offsets = ((0, -6), (0, 6)) if vertical else ((-6, 0), (6, 0))
        radius = 10.0
        for dx, dy in offsets:
            circle = self.diagram_scene.addEllipse(
                point.x() + dx - radius, point.y() + dy - radius,
                radius * 2, radius * 2, pen, fill,
            )
            circle.setZValue(3)

    def _add_source_symbol(self, vm: ProjectViewModel, branch, point: QPointF,
                           color: QColor, active: bool) -> None:
        radius = 16.0
        circle = self.diagram_scene.addEllipse(
            point.x() - radius, point.y() - radius, radius * 2, radius * 2,
            QPen(color, 1.8),
            QBrush(QColor("#FFFFFF") if active else QColor("#F1F3F6")),
        )
        circle.setZValue(3)
        wave = QPainterPath(QPointF(point.x() - 9, point.y()))
        wave.cubicTo(point.x() - 6, point.y() - 7, point.x() - 3, point.y() - 7, point.x(), point.y())
        wave.cubicTo(point.x() + 3, point.y() + 7, point.x() + 6, point.y() + 7, point.x() + 9, point.y())
        wave_item = QGraphicsPathItem(wave)
        wave_item.setPen(QPen(color, 1.5))
        wave_item.setZValue(4)
        self.diagram_scene.addItem(wave_item)
        self._add_text(branch.name, point.x(), point.y() - 35, 8.5,
                       COLORS["text"], bold=True, centered=True)
        if isinstance(branch, GeneratorBranch):
            power = float(branch.p_nom or 0.0)
            self._add_text(f"{power:g} МВт", point.x(), point.y() + 20, 7.5,
                           COLORS["muted"], centered=True)

    def _draw_loads(self, vm: ProjectViewModel, positions: dict[str, QPointF]) -> None:
        by_node: dict[str, list] = defaultdict(list)
        for load in vm.net.loads.values():
            by_node[load.node].append(load)
        for node_id, loads in by_node.items():
            center = positions.get(node_id)
            if center is None:
                continue
            for index, load in enumerate(loads):
                x = center.x() + index * 70
                start_y, end_y = center.y() + 8, center.y() + 50
                path = QPainterPath(QPointF(x, start_y))
                path.lineTo(x, end_y)
                path.moveTo(x - 6, end_y - 8)
                path.lineTo(x, end_y)
                path.lineTo(x + 6, end_y - 8)
                item = QGraphicsPathItem(path)
                item.setPen(QPen(QColor(voltage_stroke(
                    int(round(vm.net.nodes[node_id].u_nom * 1_000.0))
                )), 1.6))
                item.setZValue(2)
                self.diagram_scene.addItem(item)
                self._add_text(load.name, x, end_y + 4, 7.0, COLORS["muted"], centered=True)
                geometry = QPainterPath()
                geometry.addRoundedRect(QRectF(x - 30, start_y - 5, 60, 75), 5, 5)
                self._selection_geometries[("load", load.id)] = geometry
                hit = _HoverRectItem(geometry.boundingRect())
                hit.setZValue(5)
                self._make_selectable(hit, "load", load.id,
                                      f"{load.name}\n{load.p_kw:g} кВт")
                self.diagram_scene.addItem(hit)

    def _add_text(self, text: str, x: float, y: float, size: float,
                  color: str, *, bold: bool = False,
                  centered: bool = False) -> QGraphicsSimpleTextItem:
        item = QGraphicsSimpleTextItem(text)
        item.setBrush(QBrush(QColor(color)))
        font = QFont("Segoe UI")
        font.setPointSizeF(size)
        font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
        item.setFont(font)
        if centered:
            x -= item.boundingRect().width() / 2
        item.setPos(x, y)
        item.setZValue(4)
        self.diagram_scene.addItem(item)
        return item

    def _fault_text(self, vm: ProjectViewModel, node_id: str) -> str:
        result = vm.current_result
        if result is None or vm.mode is None:
            return ""
        solver = result.ctx.solvers.get(vm.mode.id)
        if solver is None:
            return ""
        try:
            return f"Iкз {solver.at(node_id).i3:.2f} кА".replace(".", ",")
        except Exception:
            return ""

    @staticmethod
    def _branch_caption(branch) -> str:
        """Короткая подпись на схеме; полное имя остаётся в подсказке."""
        name = branch.name
        for suffix in (" (Восток)", " (Запад)", " (Южная)"):
            name = name.replace(suffix, "")
        name = name.replace(" 10 кВ", "")
        if branch.kind == "transformer" and " ПС " in name:
            head, tail = name.split(" ПС ", 1)
            side = ""
            if " (" in tail:
                side = " (" + tail.rsplit(" (", 1)[1]
            name = head + side
        if len(name) > 24:
            name = name[:21].rstrip() + "…"
        return name

    @staticmethod
    def _make_selectable(item: QGraphicsItem, kind: str, object_id: str,
                         tooltip: str) -> None:
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        item.setData(0, kind)
        item.setData(1, object_id)
        item.setToolTip(tooltip)

    def _layout(self, vm: ProjectViewModel) -> dict[str, QPointF]:
        graph: dict[str, list[str]] = defaultdict(list)
        roots: list[str] = []
        for branch in vm.net.branches.values():
            if branch.node_from == "GRID":
                roots.append(branch.node_to)
                continue
            if branch.node_to == "GRID":
                roots.append(branch.node_from)
                continue
            graph[branch.node_from].append(branch.node_to)
            graph[branch.node_to].append(branch.node_from)
        depth: dict[str, int] = {}
        queue = deque()
        for node_id in roots:
            if node_id in vm.net.nodes and node_id not in depth:
                depth[node_id] = 0
                queue.append(node_id)
        while queue:
            current = queue.popleft()
            for neighbour in graph[current]:
                if neighbour not in depth:
                    depth[neighbour] = depth[current] + 1
                    queue.append(neighbour)
        fallback = max(depth.values(), default=-1) + 1
        for node_id in vm.net.nodes:
            if node_id not in depth:
                depth[node_id] = fallback
                fallback += 1
        by_depth: dict[int, list[str]] = defaultdict(list)
        for node_id, level in depth.items():
            by_depth[level].append(node_id)
        positions: dict[str, QPointF] = {}
        for level in sorted(by_depth):
            node_ids = sorted(
                by_depth[level],
                key=lambda item: (-vm.net.nodes[item].u_nom, vm.net.nodes[item].name),
            )
            spacing = 238.0
            offset = -(len(node_ids) - 1) * spacing / 2
            for index, node_id in enumerate(node_ids):
                positions[node_id] = QPointF(offset + index * spacing, level * 132.0)
        return positions

    def set_selection(self, kind: str, object_id: str) -> None:
        if self._selection_overlay is not None:
            self.diagram_scene.removeItem(self._selection_overlay)
            self._selection_overlay = None
        geometry = self._selection_geometries.get((kind, object_id))
        if geometry is None:
            return
        overlay = QGraphicsPathItem(geometry)
        if kind == "branch":
            pen = QPen(QColor(37, 99, 235, 82), 14.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            overlay.setPen(pen)
            overlay.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        else:
            overlay.setPen(QPen(QColor(COLORS["blue"]), 1.8))
            overlay.setBrush(QBrush(QColor(37, 99, 235, 32)))
        overlay.setZValue(-1)
        self.diagram_scene.addItem(overlay)
        self._selection_overlay = overlay
        self.centerOn(geometry.boundingRect().center())

    def _selection_changed(self) -> None:
        selected = [item for item in self.diagram_scene.selectedItems()
                    if item.data(0) and item.data(1)]
        if not selected:
            return
        item = selected[0]
        kind, object_id = item.data(0), item.data(1)
        if kind and object_id:
            self.set_selection(str(kind), str(object_id))
            self.selectionRequested.emit(str(kind), str(object_id))


class DiagramPanel(QWidget):
    """Схема во вкладке «Анализ и расчёты»: та же сцена, что в редакторе.

    Собственной графики здесь больше нет. Переключение аппарата щелчком по
    схеме и цветовая индикация состояния ушли вместе с прежней автосхемой и
    возвращаются этапами B2 и B4; автоматическая раскладка — этап B9.
    """

    selectionRequested = Signal(str, str)

    def __init__(self, vm: ProjectViewModel, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 4)
        select = QPushButton("↖")
        select.setObjectName("primaryButton")
        hand = QPushButton("✋")
        zoom_out = QPushButton("−")
        zoom_in = QPushButton("+")
        fit = QPushButton("⛶")
        actual = QPushButton("100%")
        actual.setMinimumWidth(48)
        for button in (select, hand, zoom_out, zoom_in, fit, actual):
            button.setObjectName("toolButton" if button is not select else "primaryButton")
            toolbar.addWidget(button)
        actual.setObjectName("modeButton")
        self.title = QLabel(vm.net.name)
        self.title.setObjectName("sectionTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        toolbar.addWidget(self.title, 1)
        self.mode_badge = QLabel()
        self.mode_badge.setStyleSheet(
            f"background:{COLORS['blue_soft']}; color:{COLORS['blue']};"
            "border-radius:10px; padding:4px 9px; font-weight:600;"
        )
        toolbar.addWidget(self.mode_badge)
        self.view = AnalysisSchemeView(vm)
        self.view.selectionRequested.connect(self.selectionRequested.emit)
        zoom_in.clicked.connect(self.view.zoom_in)
        zoom_out.clicked.connect(self.view.zoom_out)
        fit.clicked.connect(self.view.reset_view)
        actual.clicked.connect(self.view.actual_size)
        hand.setToolTip("Панорамирование: перетаскивайте свободное поле схемы")
        select.setToolTip("Выбор оборудования и узлов для просмотра")
        self.empty_hint = QLabel(
            "Схема ещё не размещена. Откройте вкладку «Редактор схемы» и "
            "расставьте оборудование — здесь появится та же схема."
        )
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.empty_hint)
        footer = QHBoxLayout()
        footer.setContentsMargins(18, 3, 18, 8)
        footer.addWidget(QLabel("Просмотр: редактирование выполняется на "
                                "вкладке «Редактор схемы»"))
        footer.addStretch()
        footer.addWidget(QLabel("Колесо мыши — масштаб, перетаскивание — обзор"))
        layout.addLayout(footer)
        self._apply_empty_state()

    def _apply_empty_state(self) -> None:
        empty = self.view.is_empty
        self.empty_hint.setVisible(empty)
        self.view.setVisible(not empty)

    def refresh(self, vm: ProjectViewModel) -> None:
        self.mode_badge.setText(vm.mode.name if vm.mode else "Режим не выбран")
        self.view.refresh(vm)
        self._apply_empty_state()


class InspectorPanel(QWidget):
    def __init__(self, vm: ProjectViewModel, parent=None):
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setMinimumWidth(285)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("inspectorTitle")
        close = QPushButton("×")
        close.setObjectName("toolButton")
        top.addWidget(self.title, 1)
        top.addWidget(close)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        self.properties_table = _table(["Параметр", "Значение"])
        self.fault_table = _table(["Режим", "IA / IB / IC, кА", "UA / UB / UC, кВ", "Результат"])
        _fault_table_widths(self.fault_table)
        fault_page = QWidget()
        fault_layout = QVBoxLayout(fault_page)
        self.fault_title = QLabel()
        self.fault_title.setWordWrap(True)
        fault_layout.addWidget(self.fault_title)
        fault_layout.addWidget(self.fault_table)
        self.settings_table = _table(["Защита", "Iсз", "t", "Статус"])
        self.links_list = QListWidget()
        self.tabs.addTab(self.properties_table, "Параметры")
        self.tabs.addTab(fault_page, "ТКЗ")
        self.tabs.addTab(self.settings_table, "РЗА")
        self.tabs.addTab(self.links_list, "Связи")
        layout.addWidget(self.tabs, 1)
        info = QFrame()
        info.setObjectName("card")
        info_layout = QVBoxLayout(info)
        info_layout.addWidget(QLabel("Информация"))
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setObjectName("muted")
        info_layout.addWidget(self.info_label)
        layout.addWidget(info)
        close.clicked.connect(lambda: self.setVisible(False))
        self.refresh(vm)

    def refresh(self, vm: ProjectViewModel) -> None:
        self.title.setText(vm.selection_title())
        _set_rows(self.properties_table, [[row.label, row.value] for row in vm.properties()])
        fault_rows = []
        self.fault_title.setText(vm.fault_type_label() + "\nФазные величины в точке КЗ; Zповр = 0 Ом")
        for row in vm.fault_rows():
            fault_rows.append([row.mode_name, _phase_magnitudes(row.iabc_ka),
                               _phase_magnitudes(row.vabc_kv), _fault_status(row)])
        _set_fault_rows(self.fault_table, fault_rows)
        status_text = {OK: "Норма", FAIL: "Ошибка", UNRESOLVED: "Не определено"}
        _set_rows(self.settings_table, [
            [row.kind, row.current, row.time, status_text.get(row.status, row.status)]
            for row in vm.setting_rows()
        ])
        self.links_list.clear()
        obj = vm.selected_object()
        if vm.selected_kind == "branch" and obj is not None:
            self.links_list.addItem(f"От: {obj.node_from}")
            self.links_list.addItem(f"К: {obj.node_to}")
            if getattr(obj, "ct_node", None):
                self.links_list.addItem(f"ТТ установлен: {obj.ct_node}")
        elif vm.selected_kind == "node" and obj is not None:
            for branch in vm.net.branches.values():
                if obj.id in (branch.node_from, branch.node_to):
                    self.links_list.addItem(branch.name)
        elif vm.selected_kind == "load" and obj is not None:
            self.links_list.addItem(f"Узел подключения: {obj.node}")
        if self.links_list.count() == 0:
            self.links_list.addItem("Нет связанных объектов")
        mode_name = vm.mode.name if vm.mode else "—"
        self.info_label.setText(
            f"Текущий режим: {mode_name}\n"
            "Здесь параметры доступны для анализа. Изменение оборудования выполняется "
            "на вкладке «Редактор схемы»."
        )


class BottomPanel(QWidget):
    faultTypeSelected = Signal(str)

    def __init__(self, vm: ProjectViewModel, parent=None):
        super().__init__(parent)
        self.setObjectName("bottomPanel")
        self.setMinimumHeight(225)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 8)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.network_text = QLabel()
        self.network_text.setWordWrap(True)
        network_page = self._page(self.network_text)

        self.modes_table = _table(["Режим", "Система", "Работающие генераторы"])
        modes_page = self._page(self.modes_table)

        self.loads_table = _table(["Нагрузка", "Узел", "P, кВт", "cos φ"])
        loads_page = self._page(self.loads_table)

        self.fault_table = _table(["Точка КЗ", "Режим", "IA / IB / IC, кА", "UA / UB / UC, кВ", "Результат"])
        _fault_table_widths(self.fault_table)
        fault_page = QWidget()
        fault_layout = QVBoxLayout(fault_page)
        fault_controls = QHBoxLayout()
        fault_controls.addWidget(QLabel("Вид повреждения:"))
        self.fault_type_combo = QComboBox()
        self.fault_type_combo.setObjectName("faultTypeCombo")
        for kind, label in FAULT_TYPE_LABELS.items():
            self.fault_type_combo.addItem(label, kind.value)
        self.fault_type_combo.currentIndexChanged.connect(
            lambda index: self.faultTypeSelected.emit(str(self.fault_type_combo.itemData(index)))
            if index >= 0 else None
        )
        fault_controls.addWidget(self.fault_type_combo)
        fault_controls.addStretch()
        fault_layout.addLayout(fault_controls)
        fault_layout.addWidget(self.fault_table)
        self.fault_details = QPlainTextEdit()
        self.fault_details.setObjectName("faultDetails")
        self.fault_details.setReadOnly(True)
        self.fault_details.setMaximumHeight(115)
        fault_layout.addWidget(self.fault_details)

        self.settings_table = _table(["Выбранный объект", "Защита", "Iсз", "t", "Режим", "Статус"])
        settings_page = self._page(self.settings_table)

        self.selectivity_table = _table(["Нижестоящая", "Вышестоящая", "Режим", "Δt", "Статус"])
        selectivity_page = self._page(self.selectivity_table)

        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        report_page = self._page(self.report_text)

        for title, page in (
            ("Сеть", network_page), ("Режимы", modes_page), ("Нагрузки", loads_page),
            ("КЗ", fault_page), ("Уставки", settings_page),
            ("Селективность", selectivity_page), ("Отчёт", report_page),
        ):
            self.tabs.addTab(page, title)
        self.tabs.setCurrentIndex(3)
        self.refresh(vm)

    @staticmethod
    def _page(child: QWidget) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 7, 8, 4)
        layout.addWidget(child)
        return page

    def refresh(self, vm: ProjectViewModel) -> None:
        # ProjectViewModel.net validates the canonical fingerprint on every
        # access. Read once for these synchronous tables, then reacquire on
        # the next refresh; result consumers keep their own freshness guards.
        net = vm.net
        counts = vm.status_counts()
        warning = " • ".join(vm.warnings()) or "Исходные данные загружены"
        self.network_text.setText(
            f"<b>{net.name}</b><br>"
            f"{len(net.nodes)} узлов • {len(net.branches)} ветвей • "
            f"{len(net.loads)} нагрузок • {len(net.modes)} режимов<br><br>"
            f"Результаты: <span style='color:{COLORS['green']}'>{counts.get(OK, 0)} в норме</span> • "
            f"<span style='color:{COLORS['red']}'>{counts.get(FAIL, 0)} нарушений</span> • "
            f"<span style='color:{COLORS['amber']}'>{counts.get(UNRESOLVED, 0)} не определено</span>"
            f"<br><br><span style='color:{COLORS['amber']}'>{warning}</span>"
        )
        mode_rows = []
        for mode in net.modes.values():
            enabled = [branch.name for branch in net.branches.values()
                       if isinstance(branch, GeneratorBranch) and mode.is_closed(branch)]
            system_name = {
                "max": "Максимальная",
                "min": "Минимальная",
            }.get(str(mode.system).lower(), "Не указана")
            mode_rows.append([mode.name, system_name, ", ".join(enabled) or "Нет"])
        _set_rows(self.modes_table, mode_rows)
        _set_rows(self.loads_table, [[
            item.name, net.nodes[item.node].name if item.node in net.nodes else item.node,
            f"{item.p_kw:g}", f"{item.cos_phi:g}",
        ] for item in net.loads.values()])

        point = vm.selected_fault_node()
        point_name = net.nodes[point].name if point in net.nodes else "—"
        fault_rows = []
        self.fault_type_combo.blockSignals(True)
        self.fault_type_combo.setCurrentIndex(self.fault_type_combo.findData(vm.selected_fault_type.value))
        self.fault_type_combo.blockSignals(False)
        selected_fault_rows = vm.fault_rows()
        for item in selected_fault_rows:
            fault_rows.append([
                point_name, item.mode_name,
                _phase_magnitudes(item.iabc_ka), _phase_magnitudes(item.vabc_kv),
                _fault_status(item),
            ])
        _set_fault_rows(self.fault_table, fault_rows)
        self.fault_details.setPlainText(vm.fault_details_text(selected_fault_rows))
        status_text = {OK: "Норма", FAIL: "Ошибка", UNRESOLVED: "Не определено"}
        _set_rows(self.settings_table, [[
            vm.selection_title(), row.kind, row.current, row.time, row.mode,
            status_text.get(row.status, row.status),
        ] for row in vm.setting_rows()])
        pairs = vm.selectivity_pairs()
        _set_rows(self.selectivity_table, [[
            item.lower_name, item.upper_name, item.mode_name,
            "—" if item.dt is None else f"{item.dt:.2f} с".replace(".", ","),
            status_text.get(item.status, item.status),
        ] for item in pairs])
        self.report_text.setPlainText(vm.report_text())


class TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 580)
        layout = QVBoxLayout(self)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        layout.addWidget(editor)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    _projectInputChanged = Signal()

    @_presentation_render
    def __init__(self, vm: ProjectViewModel, *, project_settings: ProjectSettings | None = None):
        super().__init__()
        self.vm = vm
        # Only app.main opts into persistence. Embedded/test windows do not
        # alter the user's next startup, even if they open or save projects.
        self._project_settings = project_settings
        self.setWindowTitle(f"РЗА-Про — {vm.net.name}")
        self.setMinimumSize(1250, 760)
        self.resize(1680, 980)

        root = QWidget()
        root.setObjectName("rootWindow")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.header = HeaderWidget(vm)
        root_layout.addWidget(self.header)

        vertical = QSplitter(Qt.Orientation.Vertical)
        horizontal = QSplitter(Qt.Orientation.Horizontal)
        self.tree_panel = NetworkTreePanel(vm)
        self.diagram_panel = DiagramPanel(vm)
        self.inspector = InspectorPanel(vm)
        horizontal.addWidget(self.tree_panel)
        horizontal.addWidget(self.diagram_panel)
        horizontal.addWidget(self.inspector)
        horizontal.setStretchFactor(0, 0)
        horizontal.setStretchFactor(1, 1)
        horizontal.setStretchFactor(2, 0)
        horizontal.setSizes([280, 1080, 320])
        self.bottom = BottomPanel(vm)
        vertical.addWidget(horizontal)
        vertical.addWidget(self.bottom)
        vertical.setStretchFactor(0, 1)
        vertical.setStretchFactor(1, 0)
        vertical.setSizes([700, 250])
        root_layout.addWidget(vertical, 1)
        # Редактор — главный рабочий экран; вкладка анализа показывает ту же
        # сцену только для просмотра. Второй графики в продукте больше нет.
        self.editor_controller = ProjectEditorController(vm.project)
        self.editor_workspace = EditorWorkspaceWidget(self.editor_controller)
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setObjectName("workspaceTabs")
        self.workspace_tabs.addTab(self.editor_workspace, "Редактор схемы")
        self.workspace_tabs.addTab(root, "Анализ и расчёты")
        self.setCentralWidget(self.workspace_tabs)

        self.monochrome_action = QAction("Чёрно-белая схема", self)
        self.monochrome_action.setCheckable(True)
        self.monochrome_action.setToolTip(
            "Отключить цветовое кодирование классов напряжения для печати"
        )
        self.monochrome_action.toggled.connect(self._set_diagram_monochrome)
        view_menu = self.menuBar().addMenu("Вид")
        view_menu.addAction(self.monochrome_action)
        view_menu.addSeparator()
        for action in (
            self.editor_workspace.command_bar.labels_action,
            self.editor_workspace.command_bar.parameters_action,
            self.editor_workspace.command_bar.results_action,
        ):
            view_menu.addAction(action)
        # These display settings are shared by both views. Refreshing the
        # analysis scene must not launch a calculation or leave a stale label.
        self.editor_workspace.command_bar.labelDisplayRequested.connect(
            lambda options: self.diagram_panel.refresh(self.vm)
        )
        self.workspace_tabs.currentChanged.connect(
            lambda index: self.refresh() if index == 1 else None
        )
        # История покрывает ввод свойств, топологию, undo и redo, включая
        # команды без canvas.commandCompleted. Обновляем виджеты после
        # завершения команды редактора, без неявного запуска расчёта.
        self._projectInputChanged.connect(
            self._refresh_after_editor_change, Qt.ConnectionType.QueuedConnection
        )
        self._unsubscribe_editor = self.editor_controller.subscribe(self._editor_history_changed)
        self._closing = False

        self.header.modeSelected.connect(self._select_mode)
        self.bottom.faultTypeSelected.connect(self._select_fault_type)
        self.header.customRequested.connect(self._custom_mode)
        self.header.generatorChanged.connect(self._generator_changed)
        self.header.recalcRequested.connect(self._recalculate)
        self.header.reportRequested.connect(self._show_report)
        self.header.checksRequested.connect(self._show_checks)
        self.header.settingsRequested.connect(self._show_settings)
        self.tree_panel.selectionRequested.connect(self._select_object)
        self.diagram_panel.selectionRequested.connect(self._select_object)
        self.editor_workspace.saveRequested.connect(self._save_editor_project)
        self.editor_workspace.fileRequested.connect(self._open_project_dialog)
        self.editor_workspace.validateRequested.connect(self._validate_editor_project)
        self.editor_workspace.statusMessage.connect(self.statusBar().showMessage)
        self.editor_workspace.errorOccurred.connect(
            lambda message: self.statusBar().showMessage(message, 7000)
        )
        self.statusBar().showMessage(f"Открыт проект: {vm.path}")

        if vm.warnings():
            self.statusBar().showMessage(vm.warnings()[0])

    def _editor_history_changed(self, event) -> None:
        if event.change.electrical_changed or event.change.catalog_changed:
            self._projectInputChanged.emit()

    def _refresh_after_editor_change(self) -> None:
        if self._closing:
            return
        self.refresh()
        reason = self.vm.result_unavailable_reason()
        if reason:
            self.statusBar().showMessage(reason)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._unsubscribe_editor()
        super().closeEvent(event)

    def _set_diagram_monochrome(self, enabled: bool) -> None:
        mode = (
            DiagramColorMode.MONOCHROME
            if enabled
            else DiagramColorMode.COLOR
        )
        self.editor_workspace.scene.set_color_mode(mode)
        self.diagram_panel.view.set_color_mode(mode)
        self.statusBar().showMessage(
            "Схема: чёрно-белый режим"
            if enabled
            else "Схема: цвет по классам напряжения",
            3000,
        )

    def _remember_project_path(self) -> str:
        if self._project_settings is None:
            return ""
        try:
            if self._project_settings.remember_project(self.vm.path):
                return ""
        except OSError:
            pass
        return "Не удалось запомнить проект для следующего запуска."

    def _save_editor_project(self) -> None:
        """Сохранить обе канонические модели одним штатным project writer."""
        try:
            backup = save_project(self.vm.path, self.vm.project)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Не удалось сохранить проект",
                f"Файл: {self.vm.path}\n\n{exc}",
            )
            return
        persistence_notice = self._remember_project_path()
        self.editor_workspace.refresh()
        message = "Проект сохранён"
        if backup is not None:
            message += f"; резервная копия: {backup.name}"
        if persistence_notice:
            message += f". {persistence_notice}"
        self.statusBar().showMessage(message, 7000)

    def _open_project_dialog(self) -> None:
        """Открыть другой проект в новом полноценном рабочем окне."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть проект",
            str(self.vm.path.parent),
            "Проекты РЗА-Про (*.json);;Все файлы (*)",
        )
        if not path:
            return
        replacement = None
        try:
            replacement = MainWindow(
                ProjectViewModel.open(path), project_settings=self._project_settings
            )
            replacement.showMaximized()
        except Exception as exc:
            if replacement is not None:
                replacement.close()
            QMessageBox.critical(
                self,
                "Не удалось открыть проект",
                f"Файл: {path}\n\n{exc}",
            )
            return
        persistence_notice = replacement._remember_project_path()
        if persistence_notice:
            replacement.statusBar().showMessage(f"Проект открыт. {persistence_notice}")
        application = QApplication.instance()
        windows = getattr(application, "_rza_project_windows", [])
        windows.append(replacement)
        setattr(application, "_rza_project_windows", windows)
        self.close()

    def _validate_editor_project(self) -> None:
        problems = [
            *(str(item) for item in self.vm.project.electrical_model.validate_integrity()),
            *self.vm.project.diagram.validate_targets(self.vm.project.electrical_model),
            *self.vm.project.catalog_snapshots.validate_targets(
                self.vm.project.electrical_model
            ),
        ]
        if problems:
            self.statusBar().showMessage(
                f"Проверка завершена: найдено замечаний — {len(problems)}", 7000
            )
        else:
            self.statusBar().showMessage(
                "Проверка завершена: ошибок и предупреждений нет", 7000
            )

    @_presentation_render
    def refresh(self, *, diagram: bool = True) -> None:
        # Открытая вкладка инспектора и нижней панели переживает пересчёт.
        inspector_tab = self.inspector.tabs.currentIndex()
        bottom_tab = self.bottom.tabs.currentIndex()
        self.header.refresh(self.vm)
        if diagram:
            self.diagram_panel.refresh(self.vm)
        self.inspector.refresh(self.vm)
        self.bottom.refresh(self.vm)
        self.inspector.tabs.setCurrentIndex(inspector_tab)
        self.bottom.tabs.setCurrentIndex(bottom_tab)

    def _select_mode(self, mode_id: str) -> None:
        if mode_id not in self.vm.net.modes:
            QMessageBox.warning(self, "Режим", f"Режим «{mode_id}» отсутствует в проекте.")
            self.header.refresh(self.vm)
            return
        self.vm.select_mode(mode_id)
        self.refresh()
        self.statusBar().showMessage(f"Выбран режим: {self.vm.mode.name}", 4000)

    @_presentation_render
    def _select_fault_type(self, value: str) -> None:
        self.vm.select_fault_type(value)
        self.inspector.refresh(self.vm)
        self.bottom.refresh(self.vm)
        self.statusBar().showMessage(self.vm.fault_type_label(), 4000)

    def _custom_mode(self) -> None:
        self.vm.ensure_custom_mode()
        self.vm.recalculate()
        self.refresh()
        self.statusBar().showMessage(
            "Пользовательский режим создан в памяти. Выберите состав генераторов.", 5000
        )

    def _generator_changed(self, branch_id: str, enabled: bool) -> None:
        if self.vm.mode_id != "gui_custom":
            self.vm.ensure_custom_mode()
        self.vm.set_generator_enabled(branch_id, enabled)
        self.vm.recalculate()
        self.refresh()

    @_presentation_render
    def _select_object(self, kind: str, object_id: str) -> None:
        self.vm.select(kind, object_id)
        self.diagram_panel.view.set_selection(kind, object_id)
        self.inspector.setVisible(True)
        self.inspector.refresh(self.vm)
        self.bottom.refresh(self.vm)
        self.statusBar().showMessage(f"Выбран объект: {self.vm.selection_title()}", 3000)

    def _toggle_switch(self, switch_id: str) -> None:
        """Щелчок по выключателю: режим → проверка → топология → пересчёт → SVG.

        Пока идёт пересчёт, повторные щелчки игнорируются: смешанного состояния
        «новые положения аппаратов со старыми токами» не возникает.
        """
        if getattr(self, "_switching", False):
            return
        self._switching = True
        try:
            closed = self.vm.toggle_switch(switch_id)
        except (KeyError, ValueError) as error:
            self._switching = False
            QMessageBox.warning(self, "Переключение невозможно", str(error))
            self.refresh()
            return
        self._switching = False
        from ..core.model import parse_switch_id
        parsed = parse_switch_id(switch_id)
        branch = self.vm.net.branches.get(parsed[0]) if parsed else None
        if branch is not None:
            self.vm.select("branch", branch.id)
        self.refresh()
        name = branch.name if branch is not None else switch_id
        self.statusBar().showMessage(
            f"{name}: выключатель {'включён' if closed else 'отключён'} "
            f"(режим «{self.vm.mode.name}»)", 5000)

    def _recalculate(self) -> None:
        ok = self.vm.recalculate()
        self.refresh(diagram=False)
        if ok:
            counts = self.vm.status_counts()
            self.statusBar().showMessage(
                f"Пересчёт завершён: {counts.get(OK, 0)} в норме, "
                f"{counts.get(FAIL, 0)} нарушений, {counts.get(UNRESOLVED, 0)} не определено",
                7000,
            )
        else:
            QMessageBox.critical(self, "Расчёт заблокирован", self.vm.calculation_error)

    def _show_report(self) -> None:
        TextDialog("Сводка расчёта", self.vm.report_text(), self).exec()

    def _show_checks(self) -> None:
        lines = []
        for name, value, status in self.vm.selected_checks():
            mark = {OK: "✓", FAIL: "✗", UNRESOLVED: "?"}.get(status, "•")
            lines.append(f"{mark} {name}: {value}")
        if not lines:
            lines = [
                self.vm.result_unavailable_reason()
                or "Для просмотра проверок выберите на схеме или в дереве ветвь с защитой."
            ]
        TextDialog(f"Проверки — {self.vm.selection_title()}", "\n".join(lines), self).exec()

    def _show_settings(self) -> None:
        QMessageBox.information(
            self,
            "Настройки проекта",
            "Оборудование и его основные свойства изменяются на вкладке "
            "«Редактор схемы».\n\n"
            "Расширенные справочники терминалов и ручной ввод уставок защит "
            "будут добавлены на отдельных этапах.",
        )
