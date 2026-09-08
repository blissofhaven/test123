"""Navigation cards are a view of the network, never electrical equipment."""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from .outline_icons import outline_icon


STATUS_COLORS = {'energized': '#299B70', 'deenergized': '#8C98A7',
                 'mixed': '#D6A345', 'partial': '#D6A345', 'error': '#CD5965', 'unknown': '#94A0B0'}
VOLTAGE_COLORS = {110: '#B45C66', 35: '#AA709B', 10: '#5B829C', .4: '#78878B'}


def voltage_color(kv):
    return VOLTAGE_COLORS.get(float(kv), '#718399')


@dataclass(frozen=True)
class OverviewCard:
    key: str
    name: str
    kind: str
    voltages: tuple[float, ...] = ()
    caption: str = ''
    detail: str = ''
    status: str = 'unknown'
    tooltip: str = ''
    group: bool = False


@dataclass(frozen=True)
class OverviewWire:
    key: str
    source: str
    target: str
    label: str
    voltage: float | None = None
    tooltip: str = ''


class _CardItem(QGraphicsObject):
    def __init__(self, card, owner):
        super().__init__()
        self.card, self.owner = card, owner
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setToolTip(card.tooltip or card.name)
        self._hovered = False
        self._press_pos = QPointF()
        self.setZValue(2)

    def boundingRect(self):
        return QRectF(-2, -2, 244, 124 if not self.card.group else 86)

    def paint(self, painter, option, widget=None):
        card = self.card
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = 120 if not card.group else 82
        color = '#6C9FD0' if self.isSelected() else '#A9C3D7' if self._hovered else '#D8E3EB'
        pen = QPen(QColor(color), 1.6 if self.isSelected() else 1)
        if card.group:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor('#EEF6FD' if self.isSelected() else '#FFFFFF'))
        painter.drawRoundedRect(QRectF(0, 0, 240, height), 10, 10)
        outline_icon(card.kind, canvas=True).paint(painter, 14, 16, 24, 24)
        font = QFont('Segoe UI', 10)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor('#243B50'))
        title = painter.fontMetrics().elidedText(card.name, Qt.TextElideMode.ElideRight, 174)
        painter.drawText(QRectF(46, 12, 174, 26), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(STATUS_COLORS.get(card.status, STATUS_COLORS['unknown'])))
        painter.drawEllipse(QPointF(225, 13), 3.5, 3.5)
        painter.setFont(QFont('Segoe UI', 8))
        x = 46
        for kv in card.voltages:
            label = f'{kv:g}'.replace('.', ',') + ' кВ'
            width = painter.fontMetrics().horizontalAdvance(label) + 12
            c = QColor(voltage_color(kv))
            painter.setPen(QPen(c.lighter(155), .7))
            painter.setBrush(c.lighter(190))
            painter.drawRoundedRect(QRectF(x, 43, width, 21), 4, 4)
            painter.setPen(c.darker(125))
            painter.drawText(QRectF(x, 43, width, 21), Qt.AlignmentFlag.AlignCenter, label)
            x += width + 5
        painter.setPen(QColor('#596D7D'))
        caption_y = 44 if card.group else 75
        painter.drawText(QRectF(14, caption_y, 210, 18), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(card.caption, Qt.TextElideMode.ElideRight, 210))
        painter.setPen(QColor('#81909C'))
        painter.drawText(QRectF(14, caption_y + 18, 210, 18), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(card.detail, Qt.TextElideMode.ElideRight, 210))

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.owner.previewRequested.emit(self.card.key)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.owner.openRequested.emit(self.card.key)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.pos() != self._press_pos:
            # Only navigation cards move. No port, line route or apparatus moves.
            self.setPos(self.owner.free_position(self, self.pos()))
            self.owner.positionsChanged.emit(self.owner.positions())

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.owner.refresh_wires()
        return result


class OverviewCanvas(QGraphicsView):
    previewRequested = Signal(str)
    openRequested = Signal(str)
    positionsChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setObjectName('networkOverviewCanvas')
        self.setBackgroundBrush(QColor('#F8FBFD'))
        self.setFrameShape(self.Shape.NoFrame)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.cards = {}
        self.wires = ()
        self._wire_items = []
        self._building = False

    def set_network(self, cards, wires, *, positions=None):
        self._building = True
        self.scene().clear()
        self.cards = {}
        self._wire_items = []
        self.wires = tuple(wires)
        # Rank only explicit inter-object links. Cycles stay in a finite layer.
        ranks = {row.key: 0 for row in cards}
        incoming = {row.key: set() for row in cards}
        for wire in self.wires:
            if wire.target in incoming and wire.source in ranks and wire.source != wire.target:
                incoming[wire.target].add(wire.source)
        pending = set(ranks)
        for _ in range(len(ranks)):
            ready = sorted(key for key in pending if not incoming[key] & pending)
            if not ready:
                break
            for key in ready:
                ranks[key] = max((ranks[p] + 1 for p in incoming[key]), default=0)
                pending.remove(key)
        row_counts = {}
        positions = positions or {}
        compact_chain = 2 <= len(cards) <= 6 and len(set(ranks.values())) == len(cards)
        for card in cards:
            item = _CardItem(card, self)
            self.cards[card.key] = item
            self.scene().addItem(item)
            rank = ranks[card.key]
            column = row_counts.get(rank, 0)
            row_counts[rank] = column + 1
            if compact_chain:
                column = rank % 2 if (rank // 2) % 2 == 0 else 1 - rank % 2
                default_position = (column * 400, (rank // 2) * 246)
            else:
                default_position = (column * 300, rank * 196)
            position = positions.get(card.key, default_position)
            item.setPos(*position)
        self._building = False
        self.refresh_wires()
        self.scene().setSceneRect(self.scene().itemsBoundingRect().adjusted(-90, -30, 140, 40))

    def refresh_wires(self):
        if self._building:
            return
        for item in self._wire_items:
            self.scene().removeItem(item)
        self._wire_items = []
        pairs = {}
        for row in self.wires:
            pairs.setdefault((row.source, row.target), []).append(row)
        for (source, target), rows in pairs.items():
            if source not in self.cards or target not in self.cards:
                continue
            a, b = self.cards[source], self.cards[target]
            for index, row in enumerate(rows):
                horizontal = abs(b.x() - a.x()) > abs(b.y() - a.y())
                offset = (index - (len(rows) - 1) / 2) * (44 if horizontal else 116)
                down = b.y() >= a.y()
                right = b.x() >= a.x()
                if horizontal:
                    start = a.pos() + QPointF(240 if right else 0, 60 + offset)
                    end = b.pos() + QPointF(0 if right else 240, 60 + offset)
                else:
                    start = a.pos() + QPointF(120 + offset, 120 if down else 0)
                    end = b.pos() + QPointF(120 + offset, 0 if down else 120)
                path = QPainterPath(start)
                middle = (start.y() + end.y()) / 2
                if horizontal:
                    midx = (start.x() + end.x()) / 2
                    path.cubicTo(QPointF(midx, start.y()), QPointF(midx, end.y()), end)
                elif abs(start.x() - end.x()) < .01:
                    path.lineTo(end)
                else:
                    path.cubicTo(QPointF(start.x(), middle), QPointF(end.x(), middle), end)
                pen = QPen(QColor(voltage_color(row.voltage) if row.voltage is not None else '#B3BFCA'), 1.4)
                line = QGraphicsPathItem(path)
                line.setPen(pen)
                line.setZValue(0)
                line.setToolTip(row.tooltip)
                self.scene().addItem(line)
                self._wire_items.append(line)
                # Open arrowhead: direction is the declared connection, not load flow.
                center = path.pointAtPercent(.5)
                tangent = path.pointAtPercent(.51) - path.pointAtPercent(.49)
                length = hypot(tangent.x(), tangent.y()) or 1
                tangent /= length
                normal = QPointF(-tangent.y(), tangent.x())
                arrow_path = QPainterPath(center - tangent * 5 + normal * 3)
                arrow_path.lineTo(center)
                arrow_path.lineTo(center - tangent * 5 - normal * 3)
                arrow = QGraphicsPathItem(arrow_path)
                arrow.setPen(pen)
                self.scene().addItem(arrow)
                self._wire_items.append(arrow)
                label = QGraphicsSimpleTextItem(row.label)
                label.setFont(QFont('Segoe UI', 8))
                label.setBrush(QColor('#6E8090'))
                label.setPos(center.x() - label.boundingRect().width() / 2 if horizontal else center.x() + 7,
                             center.y() - 19 if horizontal else center.y() - 10)
                self.scene().addItem(label)
                self._wire_items.append(label)

    def free_position(self, item, proposed):
        p = QPointF(round(proposed.x() / 10) * 10, round(proposed.y() / 10) * 10)
        others = [other.sceneBoundingRect().adjusted(-12, -12, 12, 12)
                  for other in self.cards.values() if other is not item]
        for radius in range(31):
            offsets = [(0, 0)] if radius == 0 else [
                (dx * 20, dy * 20) for dx in range(-radius, radius + 1)
                for dy in range(-radius, radius + 1) if max(abs(dx), abs(dy)) == radius]
            for dx, dy in offsets:
                candidate = p + QPointF(dx, dy)
                rect = item.boundingRect().translated(candidate)
                if not any(rect.intersects(other) for other in others):
                    return candidate
        return item._press_pos

    def positions(self):
        return {key: (item.x(), item.y()) for key, item in self.cards.items()}

    def select_card(self, key):
        self.scene().clearSelection()
        if key in self.cards:
            self.cards[key].setSelected(True)

    def fit_all(self):
        bounds = self.scene().itemsBoundingRect().adjusted(-40, -30, 80, 30)
        if not bounds.isEmpty():
            self.fitInView(bounds, Qt.AspectRatioMode.KeepAspectRatio)
            if self.transform().m11() > 1.15:
                self.resetTransform()
                self.scale(1.15, 1.15)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if .15 <= self.transform().m11() * factor <= 3:
            self.scale(factor, factor)
        event.accept()
