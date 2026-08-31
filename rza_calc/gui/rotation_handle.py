"""Vector, screen-sized rotation grip used by the real schematic scene."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsSimpleTextItem


class RotationHandleItem(QGraphicsObject):
    """An editing overlay, never equipment or part of DiagramDocument."""

    def __init__(self):
        super().__init__()
        self.representation_id = None
        self._hovered = False
        self._dragging = False
        self._blocked = False
        self._losing_grab = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setZValue(1200.0)
        self.setToolTip("Потяните для поворота: вертикально 90° / горизонтально 180°. Esc — отмена")
        self._caption = QGraphicsSimpleTextItem(self)
        self._caption.setFont(QFont("Arial", 9))
        self._caption.setPos(16.0, -10.0)
        self._caption.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._caption.setVisible(False)
        self.setVisible(False)

    def boundingRect(self):  # noqa: N802
        return QRectF(-13.0, -13.0, 26.0, 26.0)

    def shape(self):
        path = QPainterPath()
        path.addEllipse(self.boundingRect())
        return path

    def set_feedback(self, *, dragging=False, angle=None, blocked=False):
        self._dragging, self._blocked = bool(dragging), bool(blocked)
        self.setCursor(Qt.CursorShape.ClosedHandCursor if dragging else Qt.CursorShape.OpenHandCursor)
        self._caption.setText((f"{int(angle)}°" + (" · нет места" if blocked else "")) if angle is not None else "")
        self._caption.setBrush(QColor("#DC2626" if blocked else "#1D4ED8"))
        self._caption.setVisible(bool(dragging))
        self.update()

    def paint(self, painter: QPainter, option, widget=None):
        del option, widget
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#DC2626" if self._blocked else "#2563EB")
        pen = QPen(color, 1.6)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QColor("#DBEAFE" if self._hovered or self._dragging else "#FFFFFF"))
        painter.drawEllipse(QPointF(), 10.0, 10.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(-5.5, -5.5, 11.0, 11.0), 30 * 16, 285 * 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([QPointF(5.7, -5.8), QPointF(6.8, -0.5), QPointF(1.8, -2.0)]))

    def hoverEnterEvent(self, event):  # noqa: N802
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # noqa: N802
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        scene = self.scene()
        if event.button() == Qt.MouseButton.LeftButton and scene is not None:
            if scene.begin_object_rotation(self.representation_id, event.scenePos()):
                event.accept()
                return
        event.ignore()

    def mouseMoveEvent(self, event):  # noqa: N802
        scene = self.scene()
        if scene is not None:
            scene.preview_object_rotation(event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        scene = self.scene()
        if scene is not None and event.button() == Qt.MouseButton.LeftButton:
            scene.preview_object_rotation(event.scenePos())
            scene.finish_object_rotation(commit=True)
        event.accept()

    def sceneEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.UngrabMouse and self._dragging:
            scene = self.scene()
            if scene is not None:
                self._losing_grab = True
                try:
                    scene.finish_object_rotation(commit=False)
                finally:
                    self._losing_grab = False
        return super().sceneEvent(event)
