"""An independent, read-only viewport of the project's saved diagram.

No VM, calculation service, controller or settings are used. The owner supplies
the canonical model (or its isolated mode draft), document and exact mode.
Opening is an explicit signal; preview selection never navigates the editor.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
    QFrame, QGraphicsRectItem, QGraphicsView, QHBoxLayout, QLabel,
    QPushButton, QToolTip, QVBoxLayout, QWidget,
)

from ..domain.diagram import DiagramDocument, DiagramRouteId, GraphicalRepresentationId, PageId
from ..domain.electrical import ElectricalModel, OperatingStateId
from .editor_scene import CanvasMode, DiagramGraphicsScene
from .preview_annotations import PreviewAnnotation


@dataclass(frozen=True, slots=True)
class PreviewTarget:
    page_id: PageId
    representation_ids: tuple[GraphicalRepresentationId, ...] = ()
    route_ids: tuple[DiagramRouteId, ...] = ()


class _PreviewView(QGraphicsView):
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setInteractive(False)
        self.setAcceptDrops(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor('#FFFFFF'))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setMinimumHeight(170)
        self.setAccessibleName('Мини-схема выбранного объекта — только просмотр')
        self._annotations = ()
        self._annotation_layout_key = None
        self._annotation_placements = ()
        self._annotation_glyph_paths = {}
        self.annotation_font = QFont(self.font())
        self.annotation_font.setPixelSize(11)

    def set_annotations(self, rows):
        rows = tuple(rows)
        if rows != self._annotations:
            self._annotations = rows
            self._annotation_layout_key = None
            self._annotation_glyph_paths.clear()
            self.viewport().update()

    def invalidate_annotation_geometry(self):
        self._annotation_layout_key = None
        self._annotation_glyph_paths.clear()

    def annotation_glyph_path(self, row):
        """Actual winding primitives and saved rotation, normalized to 32 px.

        This magnified sample lives in the annotation card; its dotted leader
        identifies the real apparatus without moving/covering physical ports.
        """
        if row.key in self._annotation_glyph_paths:
            return self._annotation_glyph_paths[row.key]
        path = QPainterPath()
        item = getattr(self.scene(), '_items_by_id', {}).get(row.representation_id)
        if row.glyph in {'transformer_2w','transformer_3w'} and item is not None:
            for primitive in item.symbol_geometry().primitives:
                if primitive.kind == 'circle':
                    path.addEllipse(QPointF(*primitive.center),primitive.radius,primitive.radius)
            path = QTransform().rotate(item.rotation()).map(path)
            bounds = path.boundingRect()
            if not bounds.isEmpty():
                scale = 32 / max(bounds.width(),bounds.height())
                path = QTransform.fromScale(scale,scale).map(path)
                center = path.boundingRect().center()
                path.translate(16-center.x(),16-center.y())
        self._annotation_glyph_paths[row.key] = path
        return path

    def annotation_layout(self):
        """Fixed-screen boxes; only viewport projection and font metrics here."""
        transform = self.viewportTransform()
        key = (self._annotations, self.viewport().size().width(), self.viewport().size().height(),
               transform.m11(), transform.m22(), transform.dx(), transform.dy(), self.annotation_font.key())
        if key == self._annotation_layout_key:
            return self._annotation_placements
        bounds = QRectF(self.viewport().rect()).adjusted(6, 6, -6, -28)
        metrics = QFontMetricsF(self.annotation_font)
        anchors = {row.key: QPointF(self.mapFromScene(QPointF(*row.anchor))) for row in self._annotations}
        placed, hidden = [], []
        if bounds.width() < 70 or bounds.height() < 25:
            self._annotation_layout_key, self._annotation_placements = key, ()
            return ()
        for row in sorted(self._annotations, key=lambda value: (value.priority, value.key)):
            point = anchors[row.key]
            if not QRectF(self.viewport().rect()).contains(point):
                continue
            glyph_width = 40 if row.glyph else 0
            text_rect = metrics.boundingRect(QRectF(0, 0, min(190, bounds.width() - 10 - glyph_width), 1000),
                                              int(Qt.TextFlag.TextWordWrap), row.text)
            width, height = text_rect.width() + 10 + glyph_width, max(text_rect.height() + 6, 40 if row.glyph else 0)
            offsets = ((8, -height-6), (8, 6), (-width-8, -height-6), (-width-8, 6),
                       (-width/2, -height-12), (-width/2, 12), (20, -height/2), (-width-20, -height/2))
            candidates = [QRectF(point.x()+x, point.y()+y, width, height) for x, y in offsets]
            # A bounded screen grid is the fallback for dense saved layouts.
            grid = [QRectF(x, y, width, height)
                    for y in range(int(bounds.top()), int(bounds.bottom()-height)+1, 18)
                    for x in range(int(bounds.left()), int(bounds.right()-width)+1, 24)]
            candidates += sorted(grid, key=lambda r: abs(r.center().x()-point.x()) + abs(r.center().y()-point.y()))
            box = next((r for r in candidates if bounds.contains(r)
                        and not any(r.adjusted(-3, -3, 3, 3).intersects(old) for _, old in placed)
                        and not any(other_key != row.key and r.adjusted(-2, -2, 2, 2).contains(anchor)
                                    for other_key, anchor in anchors.items())), None)
            if box is None:
                hidden.append(row)
            else:
                placed.append((row, box))
        if hidden:
            note = f'Ещё {len(hidden)} · выберите РУ'
            row = PreviewAnnotation('overflow', (0, 0), note,
                '\n\n'.join(value.text.replace('\n', ' · ') + '\n' + value.tooltip for value in hidden), 99)
            placed.append((row, QRectF(6, self.viewport().height()-24,
                min(self.viewport().width()-12, metrics.horizontalAdvance(note)+12), 19)))
        self._annotation_layout_key, self._annotation_placements = key, tuple(placed)
        return self._annotation_placements

    def annotation_at(self, point):
        return next((row for row, box in self.annotation_layout() if box.contains(QPointF(point))), None)

    def viewportEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.ToolTip:
            row = self.annotation_at(event.pos())
            if row is not None:
                # QToolTip auto-detects rich text; escape all project names.
                QToolTip.showText(event.globalPos(), '<qt>' + escape(row.tooltip or row.text).replace('\n', '<br>') + '</qt>', self.viewport())
                event.accept()
                return True
            QToolTip.hideText()
        return super().viewportEvent(event)

    def drawForeground(self, painter, rect):  # noqa: N802
        super().drawForeground(painter, rect)
        if not self._annotations:
            return
        painter.save()
        painter.resetTransform()
        painter.setFont(self.annotation_font)
        placements = self.annotation_layout()
        painter.setPen(QPen(QColor('#9CAABD'), .8, Qt.PenStyle.DotLine))
        for row, box in placements:
            if row.key != 'overflow':
                point = QPointF(self.mapFromScene(QPointF(*row.anchor)))
                end = QPointF(min(max(point.x(), box.left()), box.right()),
                              min(max(point.y(), box.top()), box.bottom()))
                painter.drawLine(point, end)
        for row, box in placements:
            painter.setPen(QPen(QColor('#DCE3ED'), .8))
            painter.setBrush(QColor('#FFFFFF'))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor('#33445A'))
            if row.glyph:
                painter.save()
                painter.translate(box.left()+4,box.center().y()-16)
                painter.setPen(QPen(QColor('#455468'),1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(self.annotation_glyph_path(row))
                painter.restore()
            painter.drawText(box.adjusted(45 if row.glyph else 5, 2, -5, -2),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, row.text)
        painter.restore()

    def resizeEvent(self, event):  # noqa: N802 - Qt event
        super().resizeEvent(event)
        self.resized.emit()

    def wheelEvent(self, event):  # noqa: N802 - Qt event
        # Let the containing inspector scroll; do not move this mini viewport.
        event.ignore()

    def keyPressEvent(self, event):  # noqa: N802 - Qt event
        event.ignore()

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt event
        event.accept()


class DiagramPreviewWidget(QFrame):
    """Call set_project and set_page; connect openRequested(PreviewTarget).

    Repeated selection on the same source/page only changes the local fit.
    Source changes render the current target again. The cache is display-only:
    document identity, model identity/revision and exact state are checked; no
    calculated values are stored or validated here.
    """

    openRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('diagramPreviewCard')
        self.setStyleSheet('QFrame#diagramPreviewCard { background: #FFFFFF; border: 1px solid #DCE3ED; border-radius: 8px; }')
        self._document = None
        self._model = None
        self._operating_state_id = None
        self._topology_state_available = False
        self._source_key = None
        self._rendered_key = None
        self._target = None
        self._fit_rect = QRectF()
        self._highlight = None
        self._target_title = ''
        self.title_label = QLabel('Мини-схема')
        self.title_label.setWordWrap(True)
        self.title_label.setObjectName('cardTitle')
        self.open_button = QPushButton('Открыть на схеме')
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._request_open)
        header = QHBoxLayout()
        header.addWidget(self.title_label, 1)
        header.addWidget(self.open_button)
        self.view = _PreviewView(self)
        self.scene = self._make_scene()
        self.view.setScene(self.scene)
        self.view.resized.connect(self.fit_preview)
        self.message_label = QLabel('Выберите объект или лист для просмотра.')
        self.message_label.setWordWrap(True)
        self.message_label.setObjectName('muted')
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.readonly_label = QLabel('Сохранённая схема · только просмотр')
        self.readonly_label.setObjectName('muted')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.message_label)
        layout.addWidget(self.readonly_label)
        self.view.hide()

    def _make_scene(self):
        scene = DiagramGraphicsScene(self)
        scene.set_mode(CanvasMode.ANALYSIS)
        scene.switching_enabled = False
        scene.point_fault_enabled = False
        scene.set_grid(visible=False)
        scene.set_label_options(show_labels=True, show_parameters=False, show_results=False)
        return scene

    @property
    def target(self) -> PreviewTarget | None:
        return self._target

    def set_project(self, document: DiagramDocument, model: ElectricalModel, *,
                    operating_state_id: OperatingStateId | None = None,
                    topology_state_available: bool = True) -> None:
        if not isinstance(document, DiagramDocument) or not isinstance(model, ElectricalModel):
            raise TypeError('Мини-схема принимает DiagramDocument и ElectricalModel.')
        if operating_state_id is not None and not isinstance(operating_state_id, OperatingStateId):
            raise TypeError('Режим мини-схемы должен быть OperatingStateId или None.')
        state_available = bool(topology_state_available)
        if operating_state_id is not None and operating_state_id not in model.operating_states:
            # Never silently substitute normal positions for a deleted mode.
            state_available = False
        key = (id(document), id(model), model.revision, operating_state_id, state_available)
        changed = key != self._source_key
        self._document, self._model = document, model
        self._operating_state_id = operating_state_id
        self._topology_state_available = state_available
        self._source_key = key
        if changed and self._target is not None:
            self._render()

    def set_page(self, page_id: PageId | str, *, representation_ids=(), route_ids=(),
                 title: str = '') -> bool:
        page_id = page_id if isinstance(page_id, PageId) else PageId(str(page_id))
        representations = tuple(dict.fromkeys(
            value if isinstance(value, GraphicalRepresentationId) else GraphicalRepresentationId(str(value))
            for value in representation_ids))
        routes = tuple(dict.fromkeys(value if isinstance(value, DiagramRouteId) else DiagramRouteId(str(value))
                                     for value in route_ids))
        self._target = PreviewTarget(page_id, representations, routes)
        self._target_title = title
        return self._render()

    def clear(self, message: str = 'Выберите объект или лист для просмотра.') -> None:
        self.view.set_annotations(())
        self._target = None
        self._target_title = ''
        self._fit_rect = QRectF()
        self._rendered_key = None
        self._highlight = None
        old_scene = self.scene
        self.scene = self._make_scene()
        self.view.setScene(self.scene)
        old_scene.deleteLater()
        self.title_label.setText('Мини-схема')
        self._show_message(message, can_open=False)

    def set_annotations(self, rows: tuple[PreviewAnnotation, ...]) -> None:
        """Overlay only; saved labels in this private scene are replaced locally."""
        if any(not isinstance(row, PreviewAnnotation) for row in rows):
            raise TypeError('Подписи мини-схемы должны быть PreviewAnnotation.')
        rows = tuple(rows)
        had_annotations = bool(self.view._annotations)
        self.view.set_annotations(rows)
        self.scene.set_label_options(show_labels=not bool(rows), show_parameters=False, show_results=False)
        if had_annotations != bool(rows) and self._target is not None:
            # Refit after hiding long saved labels, without reloading the page.
            self._render()

    def _show_message(self, message, *, can_open=False):
        self.view.hide()
        self.message_label.setText(message)
        self.message_label.show()
        self.open_button.setEnabled(can_open)

    def _render(self) -> bool:
        target, document, model = self._target, self._document, self._model
        if target is None or document is None or model is None:
            self._show_message('Сохранённая схема ещё не передана для просмотра.')
            return False
        page = document.pages.get(target.page_id)
        if page is None:
            self._show_message('Лист не найден. Выберите существующий лист.')
            return False
        self.title_label.setText(self._target_title or page.name)
        for identifier in target.representation_ids:
            representation = document.representations.get(identifier)
            if representation is None or representation.page_id != target.page_id:
                self._show_message('Изображение выбранного объекта на этом листе не найдено.')
                return False
        for identifier in target.route_ids:
            if identifier not in document.routes or document.routes[identifier].page_id != target.page_id:
                self._show_message('Трасса выбранного объекта на этом листе не найдена.')
                return False
        key = self._source_key, target.page_id
        if key != self._rendered_key:
            self._remove_highlight()
            self.view.invalidate_annotation_geometry()
            self.scene.sync_document(document, model, page_id=target.page_id,
                                     operating_state_id=self._operating_state_id,
                                     topology_state_available=self._topology_state_available)
            self.scene.set_mode(CanvasMode.ANALYSIS)
            self._rendered_key = key
        self._remove_highlight()
        objects = self.scene._items_by_id
        routes = self.scene._route_items_by_id
        all_items = [*objects.values(), *routes.values()]
        visible = [item for item in all_items if item.isVisible()]
        if not visible:
            self._show_message('На этом листе ещё нет рисунка.', can_open=True)
            return True
        wanted = [objects[key] for key in target.representation_ids if key in objects]
        wanted += [routes[key] for key in target.route_ids if key in routes]
        bounds = QRectF()
        for item in wanted or visible:
            rect = item.sceneBoundingRect()
            for child in item.childItems():
                if child.isVisible():
                    rect = rect.united(child.sceneBoundingRect())
            bounds = rect if bounds.isNull() else bounds.united(rect)
        self._fit_rect = bounds.adjusted(-32, -32, 32, 32)
        if wanted:
            marker = QGraphicsRectItem(bounds.adjusted(-7, -7, 7, 7))
            pen = QPen(QColor('#6D8BB7'), 1.25)
            pen.setCosmetic(True)
            marker.setPen(pen)
            marker.setBrush(Qt.BrushStyle.NoBrush)
            marker.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            marker.setZValue(2000)
            self.scene.addItem(marker)
            self._highlight = marker
        unknown_state = not self._topology_state_available or self.scene.topology_snapshot is None
        self.message_label.setVisible(unknown_state)
        self.message_label.setText('Состояние режима не определено.' if unknown_state else '')
        self.view.show()
        self.open_button.setEnabled(True)
        self.fit_preview()
        return True

    def _remove_highlight(self):
        if self._highlight is not None:
            self.scene.removeItem(self._highlight)
            self._highlight = None

    def fit_preview(self):
        """Only viewport math; never reload the document or query the model."""
        if self._fit_rect.isEmpty() or self.view.isHidden():
            return
        self.view.fitInView(self._fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        # Same renderer controls small-scale label visibility/bridge display.
        self.scene.set_label_zoom(abs(self.view.transform().m11()))

    def _request_open(self):
        if self._target is not None and self.open_button.isEnabled():
            self.openRequested.emit(self._target)


__all__ = ['DiagramPreviewWidget', 'PreviewTarget']
