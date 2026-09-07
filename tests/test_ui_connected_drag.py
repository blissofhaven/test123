"""Connected conductor gestures exercise the real diagram without saving it."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import ProjectEditorController
from rza_calc.editor.tool_state import EditorTool
from rza_calc.editor.connection_tool import ConnectionTargetKind
from rza_calc.gui.editor_scene import (
    BusAttachmentHandle, CanvasMode, EditorCanvas, HitTestKind, PortVisualState,
    _route_vertices,
)
from rza_calc.io.project import load_project

DEMO = Path(__file__).resolve().parent.parent / "tests/fixtures/legacy_projects/energoraion.json"
_APP = None


def _identity(controller):
    model = controller.model
    return (electrical_model_fingerprint(model), model.revision,
            model.connectivity_signature(), dict(model.equipment), dict(model.ports),
            dict(model.electrical_nodes), dict(model.connections),
            frozenset(controller.diagram.representations), frozenset(controller.diagram.routes),
            {route.id: tuple((a.kind, a.representation_id, a.electrical_node_id,
                              a.target_port_id, a.branch_port_id)
                             for a in (route.start_anchor, route.end_anchor))
             for route in controller.diagram.routes.values()})


@pytest.fixture
def canvas(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb:
                        errors.append("".join(traceback.format_exception(kind, value, tb))))
    digest = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    widget = EditorCanvas(ProjectEditorController(load_project(DEMO)))
    original = _identity(widget.controller)
    widget.resize(1100, 800)
    widget.show()
    QApplication.processEvents()
    widget.view.actual_size()
    widget.set_snap_enabled(False)
    widget.view.setFocus()
    yield widget
    try:
        assert _identity(widget.controller) == original
        assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == digest
        assert not errors, "\n".join(errors)
    finally:
        widget.close()
        QApplication.processEvents()


def _mouse(canvas, kind, point):
    position = canvas.view.mapFromScene(point)
    if kind == "move":
        QTest.mouseMove(canvas.view.viewport(), position, delay=1)
    else:
        {"press": QTest.mousePress, "release": QTest.mouseRelease}[kind](
            canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    QApplication.processEvents()


def _begin(canvas, kind):
    if kind == "bus":
        item = next(item for item in canvas.scene._bus_attachment_handles.values()
                    if item.isVisible() and item.parentItem()._name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ"
                    and abs(item.pos().x()) < 50)
        point = item.scenePos()
        delta = QPointF(40, 15)  # perpendicular motion must be projected away
    else:
        item, point, delta = None, None, None
        for route in canvas.scene._route_items_by_id.values():
            for a, b in zip(route.route.waypoints, route.route.waypoints[1:]):
                if abs(a.x - b.x) + abs(a.y - b.y) < 80:
                    continue
                candidate = QPointF((a.x + b.x) / 2, (a.y + b.y) / 2)
                hit = canvas.scene.resolve_hit_target(candidate, canvas.view.transform())
                if hit.kind is HitTestKind.GRAPHICAL_CONNECTION and hit.item is route:
                    item, point = route, candidate
                    delta = QPointF(0, 40) if a.y == b.y else QPointF(40, 0)
                    break
            if item is not None:
                break
        assert item is not None
    canvas.view.centerOn(point)
    canvas.view.viewportChanged.emit(canvas.view.viewport_state())
    canvas.view.setFocus()
    QApplication.processEvents()
    _mouse(canvas, "move", point + QPointF(20, 20))
    _mouse(canvas, "move", point)
    hit = canvas.scene.resolve_hit_target(point, canvas.view.transform())
    assert hit.item is item
    if kind == "bus":
        assert hit.kind is HitTestKind.HANDLE and isinstance(item, BusAttachmentHandle)
    _mouse(canvas, "press", point)
    assert canvas.scene._connected_drag is not None
    assert canvas.scene.mouseGrabberItem() is item
    assert not canvas.scene._start_positions
    return item, point, delta


def _assert_saved_display(canvas):
    for key, item in canvas.scene._items_by_id.items():
        saved = canvas.controller.diagram.representations[key]
        assert item.pos() == QPointF(saved.x, saved.y)
    for key, item in canvas.scene._route_items_by_id.items():
        assert item._display_vertices == _route_vertices(canvas.controller.diagram.routes[key])


def _ink_count(item):
    image = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.translate(16, 16)
    item.paint(painter, None)
    painter.end()
    return sum(image.pixelColor(x, y).alpha() != 0 for x in range(32) for y in range(32))


def test_service_nodes_and_line_port_circles_are_hidden_without_removing_objects(canvas):
    nodes = [item for item in canvas.scene._items_by_id.values()
             if item.representation.electrical_node_id is not None and item._canonical_key == "connection_point"]
    assert nodes
    for node in nodes:
        degree = sum(row.electrical_node_id == node.representation.electrical_node_id
                     for row in canvas.controller.model.connections.values())
        if degree < 3:
            assert node._service_node_hidden and node.shape().isEmpty()
            assert not node._label.isVisible()
            assert _ink_count(node) == 0
        else:
            assert not node._service_node_hidden
    assert len(canvas.scene._items_by_id) == 157
    line = next(item for item in canvas.scene._items_by_id.values() if item._canonical_key in {"line", "line_section"})
    port = next(iter(line._port_items.values()))
    assert port.shape().isEmpty() and port.port_id in canvas.controller.model.ports
    assert _ink_count(port) == 0
    port.set_visual_state(PortVisualState.SOURCE)
    assert not port.shape().isEmpty()
    assert _ink_count(port) > 0
    port.set_visual_state(PortVisualState.NORMAL)
    apparatus = next(item for item in canvas.scene._items_by_id.values() if item._canonical_key == "circuit_breaker")
    assert all(_ink_count(port) > 0 for port in apparatus._port_items.values())
    canvas.scene.set_developer_overlay(True)
    assert all(not node._service_node_hidden and not node.shape().isEmpty() for node in nodes)
    assert _ink_count(port) > 0
    assert not port.shape().isEmpty()
    canvas.scene.set_developer_overlay(False)
    hidden = next(node for node in nodes if node._service_node_hidden)
    hidden.set_diagnostics((SimpleNamespace(severity="warning", message="Служебная проверка"),))
    assert not hidden._diagnostic_badge.isVisible()
    canvas.scene.set_developer_overlay(True)
    assert hidden._diagnostic_badge.isVisible()
    canvas.scene.set_developer_overlay(False)
    assert not hidden._diagnostic_badge.isVisible()


def test_hidden_line_port_does_not_hijack_select_but_explicit_wire_tool_reveals_it(canvas):
    line = next(item for item in canvas.scene._items_by_id.values()
                if item._name == "Ф-4 10 кВ (Северная)")
    port = next(iter(line._port_items.values()))
    point = port.scenePos()
    canvas.view.centerOn(point)
    canvas.view.setFocus()
    QApplication.processEvents()
    hit = canvas.scene.resolve_hit_target(point, canvas.view.transform())
    assert hit.kind is HitTestKind.BODY and hit.item is line
    _mouse(canvas, "move", point)
    _mouse(canvas, "press", point)
    assert not canvas.scene.connection_active
    assert canvas.scene.mouseGrabberItem() is line
    _mouse(canvas, "release", point)
    canvas.view._tool_state.activate(EditorTool.DRAW_CONNECTION)
    canvas.view._emit_tool_state()
    assert port.interaction_enabled() and not port.shape().isEmpty()
    assert canvas.scene.resolve_hit_target(point, canvas.view.transform()).item is port
    _mouse(canvas, "press", point)
    assert canvas.scene.connection_active
    _mouse(canvas, "release", point)
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert len(canvas.controller.journal) == 0


def test_physical_line_placement_reveals_existing_line_ports_before_first_click(canvas):
    line = next(item for item in canvas.scene._items_by_id.values()
                if item._canonical_key in {"line", "line_section"})
    port = next(iter(line._port_items.values()))
    node = next(item for item in canvas.scene._items_by_id.values()
                if getattr(item, "_service_node_hidden", False))
    assert port.shape().isEmpty()
    assert node.shape().isEmpty()
    canvas.view.begin_placement({"target_kind": "physical_line", "type_id": "physical_line.overhead"})
    assert not canvas.scene.connection_active
    assert port.interaction_enabled() and not port.shape().isEmpty()
    assert not node.shape().isEmpty() and _ink_count(node) > 0
    canvas.view.cancel_placement()
    assert port.shape().isEmpty()
    assert node.shape().isEmpty() and _ink_count(node) == 0


def test_explicit_physical_line_reuses_hidden_source_and_target_node_ids(canvas):
    nodes = [item for item in canvas.scene._items_by_id.values()
             if getattr(item, "_service_node_hidden", False)]
    start, end = nodes[:2]
    assert start.shape().isEmpty() and end.shape().isEmpty()
    before = _identity(canvas.controller)
    assert canvas.scene.begin_physical_line(name="Не сохранять", line_kind="overhead", scene_pos=start.scenePos())
    source = canvas.scene._physical_line_tool.source
    assert source.kind is ConnectionTargetKind.ELECTRICAL_NODE
    assert source.target_id == start.representation.electrical_node_id.value
    assert not end.shape().isEmpty() and _ink_count(end) > 0
    canvas.scene.update_physical_line_cursor(end.scenePos())
    target = canvas.scene._physical_line_tool.target
    assert target is not None and target.kind is ConnectionTargetKind.ELECTRICAL_NODE
    assert target.target_id == end.representation.electrical_node_id.value
    canvas.scene.cancel_physical_line()
    assert start.shape().isEmpty() and end.shape().isEmpty()
    assert _identity(canvas.controller) == before
    assert len(canvas.controller.journal) == 0


@pytest.fixture
def isolated_canvas():
    from test_stage4_editor_interaction import _app, _controller
    _app()
    widget = EditorCanvas(_controller())
    yield widget
    widget.close()
    QApplication.processEvents()


@pytest.mark.parametrize("zoom", (.5, 1.0, 2.0))
def test_hidden_node_source_hit_is_limited_to_twelve_screen_pixels(isolated_canvas, zoom):
    widget = isolated_canvas
    node = widget.controller.add_electrical_node("Существующий узел", x=0, y=0)
    widget.refresh()
    widget.view.set_zoom(zoom)
    item = widget.scene._items_by_id[node.representation_id]
    assert item.shape().isEmpty()
    for x, y, accepted in ((0, 0, True), (8, 8, True), (12, 0, True),
                            (12.01, 0, False), (24, 24, False), (25, 25, False)):
        target = widget.scene._physical_endpoint_at(QPointF(x / zoom, y / zoom))
        if accepted:
            assert target is not None and target.kind is ConnectionTargetKind.ELECTRICAL_NODE
            assert target.target_id == node.node_id.value
        else:
            assert target is None
    before = _identity(widget.controller)
    assert widget.scene.begin_physical_line(name="Свободное начало", line_kind="overhead",
                                           scene_pos=QPointF(24 / zoom, 24 / zoom))
    assert widget.scene._physical_line_tool.source.kind is ConnectionTargetKind.FREE
    widget.scene.cancel_physical_line()
    assert _identity(widget.controller) == before


@pytest.mark.parametrize("zoom", (.5, 1.0, 2.0))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_bus_hit_uses_projected_segment_distance_at_any_zoom_and_rotation(isolated_canvas, zoom, angle):
    widget = isolated_canvas
    bus = widget.controller.add_electrical_node("Длинная шина", x=0, y=0,
                                              symbol_key="busbar_horizontal", width=200, height=20)
    widget.controller.rotate_representation(bus.representation_id, angle)
    widget.refresh()
    widget.view.set_zoom(zoom)
    item = widget.scene._items_by_id[bus.representation_id]
    before = _identity(widget.controller)
    # The user-approved magnet is 12 screen pixels beyond the visible band,
    # not beyond a label/hit bounding box. Terminal/node tolerance is unchanged.
    ink = item.symbol_ink_rect()
    for local, accepted in ((QPointF(ink.right() + 8 / zoom, 0), True),
                            (QPointF(ink.right() + 13 / zoom, 0), False),
                            (QPointF(40, ink.bottom() + 8 / zoom), True),
                            (QPointF(40, ink.bottom() + 13 / zoom), False),
                            (QPointF(ink.right() + 9 / zoom, ink.bottom() + 9 / zoom), False)):
        target = widget.scene._physical_endpoint_at(item.mapToScene(local))
        if accepted:
            assert target is not None and target.kind is ConnectionTargetKind.BUS
            assert target.target_id == bus.node_id.value
        else:
            assert target is None
    assert _identity(widget.controller) == before


@pytest.mark.parametrize("is_branch", (False, True))
def test_only_local_three_way_junction_gets_an_extra_dot(canvas, is_branch):
    by_node = {}
    for item in canvas.scene._route_items_by_id.values():
        by_node.setdefault(item.route.electrical_node_id, []).append(item)
    first, second = next(values[:2] for values in by_node.values() if len(values) >= 2)
    from rza_calc.editor.orthogonal_routing import RouteVertex
    first.set_temporary_vertices((RouteVertex(10000, 10000), RouteVertex(10200, 10000)), refresh_bridges=False)
    second.set_temporary_vertices(
        (RouteVertex(10100, 10000), RouteVertex(10100, 10100)) if is_branch
        else (RouteVertex(10200, 10000), RouteVertex(10300, 10000)), refresh_bridges=False)
    try:
        canvas.scene._refresh_route_bridges()
        if is_branch:
            assert sum(QPointF(10100, 10000) in item._bridge_nodes for item in (first, second)) == 1
        else:
            assert not first._bridge_nodes and not second._bridge_nodes
    finally:
        first.clear_temporary_route(refresh_bridges=False)
        second.clear_temporary_route(refresh_bridges=False)
        canvas.scene._refresh_route_bridges()


@pytest.mark.parametrize("kind", ("bus", "route"))
def test_direct_connected_drag_is_one_command_with_exact_undo_and_redo(canvas, kind):
    item, point, delta = _begin(canvas, kind)
    before = canvas.controller.diagram
    _mouse(canvas, "move", point + delta / 2)
    _mouse(canvas, "move", point + delta)
    assert canvas.controller.diagram is before
    assert canvas.scene._connected_drag.preview_routes
    assert any(row.waypoints != before.routes[row.id].waypoints
               for row in canvas.scene._connected_drag.preview_routes)
    _mouse(canvas, "release", point + delta)
    after = canvas.controller.diagram
    assert dict(after.representations) == dict(before.representations)
    assert dict(after.routes) != dict(before.routes)
    assert len(canvas.controller.journal) == 1
    assert canvas.scene._connected_drag is None
    _assert_saved_display(canvas)
    if kind == "route":
        previous, current = before.routes[item.route.id], after.routes[item.route.id]
        assert current.start_anchor == previous.start_anchor and current.end_anchor == previous.end_anchor
        assert current.waypoints[0] == previous.waypoints[0]
        assert current.waypoints[-1] == previous.waypoints[-1]
    canvas.undo()
    assert dict(canvas.controller.diagram.routes) == dict(before.routes)
    _assert_saved_display(canvas)
    canvas.redo()
    assert dict(canvas.controller.diagram.routes) == dict(after.routes)
    _assert_saved_display(canvas)


@pytest.mark.parametrize("kind", ("bus", "route"))
@pytest.mark.parametrize("terminal", ("escape", "analysis", "focus_out", "pan", "placement", "grab_loss", "sync"))
def test_connected_drag_cancel_never_commits_on_a_late_release(canvas, kind, terminal):
    item, point, delta = _begin(canvas, kind)
    before = canvas.controller.diagram
    _mouse(canvas, "move", point + delta / 2)
    _mouse(canvas, "move", point + delta)
    assert canvas.scene._connected_drag.preview_routes
    if terminal == "escape":
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    elif terminal == "analysis":
        canvas.set_mode(CanvasMode.ANALYSIS)
    elif terminal == "focus_out":
        canvas.view.clearFocus()
    elif terminal == "pan":
        QTest.keyPress(canvas.view, Qt.Key.Key_Space)
    elif terminal == "placement":
        canvas.view.begin_placement({"target_kind": "equipment", "type_id": "builtin.load"})
    elif terminal == "grab_loss":
        item.ungrabMouse()
    else:
        canvas.refresh()
    QApplication.processEvents()
    assert canvas.scene._connected_drag is None
    _assert_saved_display(canvas)
    _mouse(canvas, "release", point + delta)
    if terminal == "pan":
        QTest.keyRelease(canvas.view, Qt.Key.Key_Space)
    # Mode and viewport settings are valid view-state writes, not drag commands.
    assert dict(canvas.controller.diagram.representations) == dict(before.representations)
    assert dict(canvas.controller.diagram.routes) == dict(before.routes)
    assert len(canvas.controller.journal) == 0
    _assert_saved_display(canvas)


@pytest.mark.parametrize("kind", ("bus", "route"))
def test_connected_noop_click_has_no_history(canvas, kind):
    _, point, _ = _begin(canvas, kind)
    before = canvas.controller.diagram
    _mouse(canvas, "release", point)
    assert canvas.controller.diagram is before
    assert len(canvas.controller.journal) == 0
    assert canvas.scene._connected_drag is None


@pytest.mark.parametrize("kind", ("bus", "route"))
def test_drag_out_and_back_before_release_restores_preview_and_is_noop(canvas, kind):
    item, point, delta = _begin(canvas, kind)
    before = canvas.controller.diagram
    _mouse(canvas, "move", point + delta)
    assert canvas.scene._connected_drag is not None
    _mouse(canvas, "move", point)
    assert canvas.scene._connected_drag is not None
    assert canvas.scene.mouseGrabberItem() is item
    _assert_saved_display(canvas)
    _mouse(canvas, "release", point)
    assert canvas.controller.diagram is before
    assert len(canvas.controller.journal) == 0


def test_existing_segment_handle_dispatches_to_the_same_connected_gesture(canvas):
    # The saved demo currently has straight/L-shaped routes. First create a
    # dogleg through the real command, then use its pre-existing segment handle.
    seed = next(iter(canvas.controller.diagram.routes.values()))
    a, b = seed.waypoints[:2]
    canvas.controller.move_route_segment(seed.id, 0, dx=40 if a.x == b.x else 0,
                                         dy=40 if a.y == b.y else 0)
    canvas.refresh()
    journal_before = len(canvas.controller.journal)
    route = next(item for item in canvas.scene._route_items_by_id.values() if item._segment_handles)
    route.setSelected(True)
    handle = next(iter(route._segment_handles.values()))
    point = handle.scenePos()
    canvas.view.centerOn(point)
    canvas.view.setFocus()
    QApplication.processEvents()
    hit = canvas.scene.resolve_hit_target(point, canvas.view.transform())
    assert hit.item is handle and hit.kind is HitTestKind.HANDLE
    _mouse(canvas, "press", point)
    assert canvas.scene._connected_drag is not None
    assert canvas.scene._connected_drag.segment_index == handle.segment_index
    assert canvas.scene.mouseGrabberItem() is route
    delta = QPointF(0, 40) if handle.horizontal else QPointF(40, 0)
    before = canvas.controller.diagram
    _mouse(canvas, "move", point + delta)
    assert canvas.controller.diagram is before
    _mouse(canvas, "release", point + delta)
    assert len(canvas.controller.journal) == journal_before + 1
    assert canvas.controller.diagram.routes[route.route.id].waypoints != before.routes[route.route.id].waypoints
    _assert_saved_display(canvas)


def test_native_physical_line_can_be_grabbed_by_its_shaft_without_reconnecting(canvas):
    from test_ui_b3_label_geometry import _native_line_scene
    controller, line, unused_scene = _native_line_scene()
    widget = EditorCanvas(controller)
    before = _identity(controller)
    route_before = controller.diagram.routes[line.route_id]
    count = len(controller.journal)
    try:
        widget.resize(900, 650)
        widget.show()
        QApplication.processEvents()
        widget.view.actual_size()
        widget.set_snap_enabled(False)
        point = QPointF(300, 0)
        widget.view.centerOn(point)
        widget.view.setFocus()
        QApplication.processEvents()
        hit = widget.scene.resolve_hit_target(point, widget.view.transform())
        assert hit.kind is HitTestKind.PHYSICAL_LINE
        _mouse(widget, "press", point)
        assert widget.scene._connected_drag is not None
        _mouse(widget, "move", point + QPointF(0, 60))
        assert controller.diagram.routes[line.route_id] == route_before
        _mouse(widget, "release", point + QPointF(0, 60))
        updated = controller.diagram.routes[line.route_id]
        assert updated.waypoints != route_before.waypoints
        assert (updated.start_anchor, updated.end_anchor) == (route_before.start_anchor, route_before.end_anchor)
        assert len(controller.journal) == count + 1
        assert _identity(controller) == before
        _assert_saved_display(widget)
        widget.undo()
        assert controller.diagram.routes[line.route_id] == route_before
    finally:
        widget.close()
        QApplication.processEvents()
