"""Manual routing actions are reachable from the real route context menu."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import NodeTarget
from rza_calc.gui.editor_scene import EditorCanvas
from test_stage4_editor_interaction import _controller, _U10


@pytest.fixture
def routed_canvas():
    app = QApplication.instance() or QApplication([])
    controller = _controller()
    first = controller.add_electrical_node("Начало", x=0, y=0, voltage_class_id=_U10)
    second = controller.add_electrical_node("Конец", x=200, y=160, voltage_class_id=_U10)
    controller.connect_from_node(NodeTarget(first.node_id, first.representation_id),
                                 NodeTarget(second.node_id, second.representation_id))
    route = next(iter(controller.diagram.routes.values()))
    controller.move_route_segment(route.id, 0, dx=0, dy=80)
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 700)
    canvas.show()
    canvas.view.actual_size()
    canvas.view.centerOn(100, 80)
    QApplication.processEvents()
    yield canvas, route.id
    canvas.close()
    QApplication.processEvents()


def _choose(canvas, route_id, text):
    route = canvas.controller.diagram.routes[route_id]
    a, b = max(zip(route.waypoints, route.waypoints[1:]), key=lambda ab: abs(ab[0].x-ab[1].x)+abs(ab[0].y-ab[1].y))
    point = canvas.view.mapFromScene(QPointF((a.x+b.x)/2, (a.y+b.y)/2))
    picked = []
    def act():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        if text is None:
            picked.append("cancel")
            menu.close()
        else:
            action = next(a for a in menu.actions() if a.text() == text)
            picked.append(action.text())
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
    QTimer.singleShot(0, act)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, canvas.view.viewport().mapToGlobal(point))
    QApplication.sendEvent(canvas.view.viewport(), event)
    assert picked == [text if text else "cancel"]


def test_menu_pin_unpin_autoroute_cancel_keeps_topology_and_one_history_each(routed_canvas):
    canvas, route_id = routed_canvas
    controller = canvas.controller
    before = electrical_model_fingerprint(controller.model)
    original = controller.diagram.routes[route_id]
    count = len(controller.journal)
    _choose(canvas, route_id, None)
    assert len(controller.journal) == count and controller.diagram.routes[route_id] == original
    _choose(canvas, route_id, "Закрепить изгибы")
    assert len(controller.journal) == count + 1
    assert all(p.pinned for p in controller.diagram.routes[route_id].waypoints[1:-1])
    _choose(canvas, route_id, "Освободить изгибы")
    assert len(controller.journal) == count + 2
    assert not any(p.pinned for p in controller.diagram.routes[route_id].waypoints)
    _choose(canvas, route_id, "Построить короткий маршрут")
    final = controller.diagram.routes[route_id]
    assert (final.start_anchor, final.end_anchor) == (original.start_anchor, original.end_anchor)
    assert len(final.waypoints) <= len(original.waypoints)
    assert electrical_model_fingerprint(controller.model) == before
    assert not controller.diagram.validate_targets(controller.model)


def test_real_segment_drag_previews_same_obstacle_detour_as_commit(monkeypatch):
    app = QApplication.instance() or QApplication([])
    controller = _controller()
    first = controller.add_electrical_node("Начало", x=0, y=0, voltage_class_id=_U10)
    second = controller.add_electrical_node("Конец", x=240, y=0, voltage_class_id=_U10)
    controller.connect_from_node(NodeTarget(first.node_id, first.representation_id),
                                 NodeTarget(second.node_id, second.representation_id))
    obstacle = controller.add_equipment("builtin.circuit_breaker", "Соседний QF", x=120, y=80)
    route = next(iter(controller.diagram.routes.values()))
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 700)
    canvas.show()
    canvas.view.actual_size()
    canvas.view.centerOn(120, 0)
    canvas.set_snap_enabled(False)
    canvas._save_viewport(canvas.view.viewport_state())
    QApplication.processEvents()
    calls = []
    original = canvas.scene._route_segment_constraints
    def counted(identifier):
        calls.append(identifier)
        return original(identifier)
    canvas.scene._route_segment_constraints = counted
    before, count = electrical_model_fingerprint(controller.model), len(controller.journal)
    row_before = controller.diagram.representations[obstacle.representation_id]
    viewport = canvas.view.viewport()
    start = canvas.view.mapFromScene(QPointF(100, 0))
    end = canvas.view.mapFromScene(QPointF(100, 80))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, canvas.view.mapFromScene(QPointF(100, 40)))
    QTest.mouseMove(viewport, end)
    gesture = canvas.scene._connected_drag
    assert gesture is not None and gesture.preview_routes
    preview = gesture.preview_routes[0]
    assert calls == [route.id]
    assert controller.diagram.routes[route.id] == route
    assert electrical_model_fingerprint(controller.model) == before
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    assert len(controller.journal) == count + 1
    actual = controller.diagram.routes[route.id]
    assert tuple((p.x, p.y) for p in actual.waypoints) == tuple((p.x, p.y) for p in preview.waypoints)
    assert (actual.start_anchor, actual.end_anchor) == (route.start_anchor, route.end_anchor)
    assert controller.diagram.representations[obstacle.representation_id] == row_before
    assert not any(a.y == b.y == 80 and min(a.x,b.x) < 120 < max(a.x,b.x)
                   for a,b in zip(actual.waypoints, actual.waypoints[1:]))
    assert electrical_model_fingerprint(controller.model) == before
    canvas.undo()
    assert controller.diagram.routes[route.id] == route
    canvas.close()


@pytest.mark.parametrize("tool_kind", ["wire", "cable", "overhead"])
def test_clicked_draft_goal_shapes_route_but_does_not_persist_an_implicit_lock(tool_kind):
    from rza_calc.editor.connection_tool import (ConnectionTarget, ConnectionTargetKind,
        ConnectionTargetFeedback, ConnectionToolState, PhysicalLineToolState)
    from rza_calc.editor.orthogonal_routing import RouteDirection
    target = ConnectionTarget(ConnectionTargetKind.FREE, 260, 120,
                               feedback=ConnectionTargetFeedback.COMPATIBLE)
    if tool_kind == "wire":
        tool = ConnectionToolState()
        tool.begin(source_port_id="port.test", source_representation_id="rep.test",
                   x=0, y=0, direction=RouteDirection.RIGHT)
    else:
        tool = PhysicalLineToolState()
        tool.begin(name="Линия", line_kind=tool_kind,
                   source=ConnectionTarget(ConnectionTargetKind.FREE, 0, 0,
                       direction=RouteDirection.RIGHT, feedback=ConnectionTargetFeedback.COMPATIBLE))
    tool.update(100, 80)
    tool.add_manual_vertex(100, 80)
    tool.update(260, 120, target=target)
    draft = tool.draft()
    assert draft.manual_vertices[0].pinned
    assert any((p.x,p.y) == (100,80) for p in draft.vertices)
    persisted = EditorCanvas._persisted_waypoints(draft)
    assert any((p.x,p.y) == (100,80) for p in persisted)
    assert not any(p.pinned for p in persisted)


@pytest.mark.parametrize("explicit_pin", [False, True])
def test_drag_user_bend_preserves_only_explicit_saved_lock_by_id(routed_canvas, explicit_pin):
    canvas, route_id = routed_canvas
    controller = canvas.controller
    item = canvas.scene._route_items_by_id[route_id]
    item.add_user_waypoint(QPointF(100, 80))
    route = controller.diagram.routes[route_id]
    added = next(p for p in route.waypoints if (p.x,p.y) == (100,80))
    assert not added.pinned
    if explicit_pin:
        _choose(canvas, route_id, "Закрепить изгибы")
    item = canvas.scene._route_items_by_id[route_id]
    item.setSelected(True)
    handle = item._waypoint_handles[added.id]
    start = canvas.view.mapFromScene(handle.scenePos())
    end = canvas.view.mapFromScene(QPointF(120,100))
    count = len(controller.journal)
    fingerprint = electrical_model_fingerprint(controller.model)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas.view.viewport(), end)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    moved = next(p for p in controller.diagram.routes[route_id].waypoints if p.id == added.id)
    assert (moved.x,moved.y) == (120,100)
    assert moved.pinned is explicit_pin
    assert len(controller.journal) == count + 1
    assert electrical_model_fingerprint(controller.model) == fingerprint
    if explicit_pin:
        previous = {p.id for p in route.waypoints[1:-1]}
        for p in controller.diagram.routes[route_id].waypoints:
            if p.id in previous:
                assert p.pinned
