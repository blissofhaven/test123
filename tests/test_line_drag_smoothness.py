"""Continuous conductor previews keep current ink and exact release semantics."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsView

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.editor_scene import DiagramGraphicsScene, DiagramGraphicsView, EditorCanvas, _route_vertices
from test_ui_connected_commands import _native_line


@pytest.fixture(scope="module", autouse=True)
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture(params=(False, True), ids=("segment", "bus"))
def conductor(request):
    controller, _, _, line = _native_line(bus=request.param)
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 700)
    canvas.show()
    canvas.view.actual_size()
    canvas.set_snap_enabled(True)
    QApplication.processEvents()
    item = canvas.scene._route_items_by_id[line.route_id]
    if request.param:
        item = canvas.scene._bus_attachment_handles[(line.route_id, True)]
        point, axis = item.scenePos(), QPointF(1, 0)
    else:
        a, b = item.route.waypoints[:2]
        point = QPointF((a.x + b.x) / 2, (a.y + b.y) / 2)
        axis = QPointF(0, 1) if a.y == b.y else QPointF(1, 0)
    canvas.view.centerOn(point)
    canvas.view.setFocus()
    QApplication.processEvents()
    yield canvas, item, point, axis
    canvas.close()
    QApplication.processEvents()


def _press(canvas, point):
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton,
                     pos=canvas.view.mapFromScene(point))
    QApplication.processEvents()
    assert canvas.scene._connected_drag is not None


def _release(canvas, point, modifiers=Qt.KeyboardModifier.NoModifier):
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton,
                       modifiers, canvas.view.mapFromScene(point))
    QApplication.processEvents()


def test_identical_snapped_preview_does_not_repeat_global_display_work(conductor, monkeypatch):
    canvas, item, point, axis = conductor
    _press(canvas, point)
    saved, journal = canvas.controller.diagram, len(canvas.controller.journal)
    fingerprint = electrical_model_fingerprint(canvas.controller.model)
    calls = []
    original = canvas.scene._refresh_route_bridges

    def refresh():
        calls.append(True)
        original()

    monkeypatch.setattr(canvas.scene, "_refresh_route_bridges", refresh)
    canvas.scene.update_connected_drag(point + axis * 41)
    first = tuple(canvas.scene._connected_drag.preview_routes)
    for distance in (42, 43, 44, 45, 46):
        canvas.scene.update_connected_drag(point + axis * distance)
    assert len(calls) == 1, "One snapped shape must have one full crossing/label rebuild"
    assert canvas.scene._connected_drag.preview_routes == first
    canvas.scene.update_connected_drag(point + axis * 62)
    assert len(calls) == 2
    assert canvas.scene._connected_drag.preview_routes != first
    assert canvas.controller.diagram is saved
    assert len(canvas.controller.journal) == journal
    assert electrical_model_fingerprint(canvas.controller.model) == fingerprint
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert canvas.scene._connected_drag is None
    assert canvas.controller.diagram is saved
    for route_id, route_item in canvas.scene._route_items_by_id.items():
        assert route_item._display_vertices == _route_vertices(saved.routes[route_id])
    _release(canvas, point + axis * 62)
    assert len(canvas.controller.journal) == journal


@pytest.mark.parametrize("alt", (False, True), ids=("snapped", "alt-unsnapped"))
def test_release_uses_final_position_and_modifier_without_a_move_event(conductor, alt):
    canvas, item, point, axis = conductor
    _press(canvas, point)
    original, count = canvas.controller.diagram, len(canvas.controller.journal)
    fingerprint = electrical_model_fingerprint(canvas.controller.model)
    gesture = canvas.scene._connected_drag
    route_id, segment, kind = gesture.route_id, gesture.segment_index, gesture.kind
    # The last delivered move is deliberately behind the eventual release.
    canvas.scene.update_connected_drag(point + axis * 21)
    modifiers = Qt.KeyboardModifier.AltModifier if alt else Qt.KeyboardModifier.NoModifier
    _release(canvas, point + axis * 47, modifiers)
    route = canvas.controller.diagram.routes[route_id]
    assert len(canvas.controller.journal) == count + 1
    assert electrical_model_fingerprint(canvas.controller.model) == fingerprint
    previous = original.routes[route_id]
    if kind == "bus":
        old_point, new_point = previous.waypoints[0], route.waypoints[0]
        expected = old_point.x + 47 if alt else round((old_point.x + 47) / 20) * 20
        assert new_point.x == pytest.approx(expected, abs=1)
        assert new_point.y == old_point.y
        assert route.end_anchor == previous.end_anchor
        assert route.waypoints[-1] == previous.waypoints[-1]
    else:
        a, b = previous.waypoints[segment:segment + 2]
        origin = a.y if a.y == b.y else a.x
        expected = origin + 47 if alt else round((origin + 47) / 20) * 20
        moved_segments = [(a, b) for a, b in zip(route.waypoints, route.waypoints[1:])
                          if abs((a.y if axis.y() else a.x) - expected) < 1]
        assert moved_segments, "The release must not commit the older move position"
        assert route.start_anchor == previous.start_anchor and route.end_anchor == previous.end_anchor
        assert route.waypoints[0] == previous.waypoints[0] and route.waypoints[-1] == previous.waypoints[-1]
    canvas.undo()
    assert dict(canvas.controller.diagram.routes) == dict(original.routes)
    canvas.redo()
    assert canvas.controller.diagram.routes[route_id] == route


def test_click_without_motion_does_not_snap_existing_geometry(conductor):
    canvas, _, point, _ = conductor
    _press(canvas, point)
    original, count = canvas.controller.diagram, len(canvas.controller.journal)
    _release(canvas, point)
    assert canvas.controller.diagram is original
    assert len(canvas.controller.journal) == count


def test_new_document_cancels_preview_and_next_gesture_uses_new_route(conductor):
    canvas, item, point, axis = conductor
    _press(canvas, point)
    gesture = canvas.scene._connected_drag
    route_id, kind = gesture.route_id, gesture.kind
    canvas.scene.update_connected_drag(point + axis * 41)
    canvas.controller.move_route_segment(route_id, 0, dx=60, dy=60)
    canvas.refresh()
    assert canvas.scene._connected_drag is None
    original, count = canvas.controller.diagram, len(canvas.controller.journal)
    source = original.routes[route_id]
    if kind == "bus":
        item = canvas.scene._bus_attachment_handles[(route_id, True)]
        point = item.scenePos()
    else:
        item = canvas.scene._route_items_by_id[route_id]
        a, b = source.waypoints[:2]
        point = QPointF((a.x + b.x) / 2, (a.y + b.y) / 2)
    assert canvas.scene.begin_connected_drag(item, point)
    canvas.scene.update_connected_drag(point + axis * 41)
    row = next(row for row in canvas.scene._connected_drag.preview_routes if row.id == route_id)
    assert row.waypoints != source.waypoints
    assert row.end_anchor == source.end_anchor
    assert row.waypoints[-1] == source.waypoints[-1]
    canvas.scene.cancel_connected_drag()
    assert canvas.controller.diagram is original
    assert len(canvas.controller.journal) == count
    assert canvas.scene._route_items_by_id[route_id]._display_vertices == _route_vertices(source)


class _PaintCountingView(DiagramGraphicsView):
    def __init__(self, scene):
        self.background_paints = 0
        super().__init__(scene)

    def drawBackground(self, painter, rect):
        self.background_paints += 1
        super().drawBackground(painter, rect)


def _grid_view():
    scene = DiagramGraphicsScene()
    scene.setSceneRect(-2000, -2000, 4000, 4000)
    view = _PaintCountingView(scene)
    view.resize(501, 389)
    view.show()
    view.centerOn(-31, -47)
    QApplication.processEvents()
    return scene, view


def _pixels(view):
    view.viewport().repaint()
    QApplication.processEvents()
    return view.viewport().grab().toImage()


def test_foreground_updates_reuse_grid_background():
    scene, view = _grid_view()
    try:
        assert view.cacheMode() == QGraphicsView.CacheModeFlag.CacheBackground
        item = scene.addRect(QRectF(-20, -20, 40, 40), brush=QColor("red"))
        QApplication.processEvents()
        before = view.background_paints
        for offset in (10, 30, 50):
            item.setPos(offset, offset)
            view.viewport().repaint()
            QApplication.processEvents()
        assert view.background_paints == before
    finally:
        view.close()


def test_cached_grid_matches_uncached_after_pan_zoom_resize_and_grid_changes():
    scene, view = _grid_view()
    control_scene, control = _grid_view()
    control.setCacheMode(QGraphicsView.CacheModeFlag.CacheNone)
    try:
        for index, action in enumerate((
            lambda s, v: None,
            lambda s, v: v.centerOn(-117.5, 88.5),
            lambda s, v: v.set_zoom(.349),
            lambda s, v: v.set_zoom(.351),
            lambda s, v: v.set_zoom(1.37),
            lambda s, v: v.resize(613, 417),
            lambda s, v: s.set_grid(visible=False),
            lambda s, v: s.set_grid(visible=True, size=13),
            lambda s, v: v.centerOn(-111.5, -77.5),
        )):
            action(scene, view)
            action(control_scene, control)
            assert _pixels(view) == _pixels(control), f"Grid rendering changed at transition {index}"
    finally:
        view.close()
        control.close()
