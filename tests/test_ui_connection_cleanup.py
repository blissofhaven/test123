"""Explicit bus endpoints, whole legacy-line ownership and transient cleanup."""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.diagram import DiagramRouteKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.connection_tool import ConnectionTargetFeedback, ConnectionTargetKind
from rza_calc.editor.tool_state import EditorTool
from rza_calc.gui.editor_scene import HitTestKind, PortVisualState
from rza_calc.io.project import load_project
from test_ui_direct_connections import canvas_factory, _controller, _pair, _mouse, _state, U10


@pytest.mark.parametrize("operation", ("cancel", "scene_sync", "canvas_refresh", "delete_source"))
@pytest.mark.parametrize("physical", (False, True))
def test_cancel_rebuild_delete_clear_real_active_draft_not_persistent_ports(canvas_factory, operation, physical):
    canvas, ports = _pair(canvas_factory)
    scene = canvas.scene
    start = ports[0].scenePos()
    if physical:
        assert scene.begin_physical_line(scene_pos=start, name="ВЛ", line_kind="overhead")
        scene.update_physical_line_cursor(start + QPointF(70, -70))
    else:
        scene.begin_connection(ports[0])
        assert scene.connection_active
        scene.update_connection_cursor(start + QPointF(70, -70))
    assert scene._preview_item.isVisible()
    # All supported cancellation/rebuild paths must clear even stale red state.
    scene._items_by_id[ports[1].representation_id].set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE)
    if operation == "cancel":
        scene.cancel_connection(announce=False)
        scene.cancel_physical_line(announce=False)
    elif operation == "scene_sync":
        scene.sync_document(canvas.controller.diagram, canvas.controller.model)
    elif operation == "canvas_refresh":
        canvas.refresh()
    else:
        canvas.delete_from_project((ports[0].representation_id,), confirmed=True)
    assert not scene.connection_active and not scene._preview_item.isVisible()
    assert not scene._preview_item._vertices and scene._connection_target is None
    assert all(item._target_feedback is ConnectionTargetFeedback.NEUTRAL for item in scene._items_by_id.values())
    assert all(item._target_feedback is ConnectionTargetFeedback.NEUTRAL for item in scene._route_items_by_id.values())
    remaining = scene._items_by_id[ports[1].representation_id]
    assert remaining.port_item(ports[1].port_id) is not None


def test_inactive_cancel_still_removes_stale_overlay_and_red_feedback(canvas_factory):
    canvas, ports = _pair(canvas_factory)
    scene = canvas.scene
    scene.begin_connection(ports[0])
    scene.update_connection_cursor(ports[0].scenePos() + QPointF(70, -70))
    scene._connection_tool.cancel()  # stale renderer after a terminated state owner
    assert scene._preview_item.isVisible() and not scene.connection_active
    scene._items_by_id[ports[1].representation_id].set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE)
    scene.cancel_connection(announce=False)
    assert not scene._preview_item.isVisible() and scene._connection_target is None
    assert all(item._target_feedback is ConnectionTargetFeedback.NEUTRAL for item in scene._items_by_id.values())


@pytest.mark.parametrize("bus_width", (240, 239))
def test_bus_preview_allocates_distinct_contacts_and_matches_two_commits(canvas_factory, monkeypatch, bus_width):
    #  Протяжка спрашивает «Провод / ВЛ / КЛ» с 31.08.2026, а этот тест ведёт
    #  настоящую мышь. Без ответа модальное меню останавливало и сам тест, и
    #  весь Qt-набор за ним. Отвечаем «Провод»: проверяется геометрия точек на
    #  шине, а не меню — его состав закреплён в test_drag_creates_line.
    from rza_calc.gui.editor_scene import EditorCanvas

    monkeypatch.setattr(EditorCanvas, "_ask_dragged_connection_kind",
                        lambda self, screen_pos: "wire")
    controller = _controller()
    bus = controller.add_electrical_node("Шина", x=0, y=0, symbol_key="busbar_horizontal", width=bus_width, height=12, voltage_class_id=U10)
    first = controller.add_equipment("builtin.circuit_breaker", "Вход", x=-180, y=-120, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "Выход", x=-260, y=-200, voltage_class_by_group={"main": U10})
    canvas = canvas_factory(controller)
    canvas.view.centerOn(-50, -70)
    contact_points = []
    originals = tuple(controller.model.equipment[row.equipment_id].port_ids for row in (first, second))
    for apparatus in (first, second):
        port = canvas.scene._items_by_id[apparatus.representation_id].port_item(apparatus.port_ids[0])
        start = port.scenePos()
        bus_item = canvas.scene._items_by_id[bus.representation_id]
        # Magnetism is measured from visible ink. After restoring thin buses,
        # the old hard-coded y=-19 is outside that ink plus the 12px margin.
        cursor = bus_item.mapToScene(QPointF(0, bus_item.symbol_ink_rect().top()
                                           - 11 / canvas.view.zoom_factor))
        _mouse(canvas, "press", start)
        _mouse(canvas, "move", cursor)
        target = canvas.scene._connection_target
        assert target is not None and target.kind is ConnectionTargetKind.BUS
        assert target.feedback is ConnectionTargetFeedback.COMPATIBLE
        assert target.y == 0
        assert canvas.scene._preview_item._endpoint_kind is ConnectionTargetKind.BUS
        preview = tuple((point.x, point.y) for point in canvas.scene._preview_item._vertices)
        contact_points.append((target.x, target.y))
        _mouse(canvas, "release", cursor)
        route = next(row for row in controller.diagram.routes.values()
                     if row.start_anchor.target_port_id == port.port_id)
        assert tuple((point.x, point.y) for point in route.waypoints) == preview
    # New connections use the minimum nonmerging distance of ten units.
    # The explicit separation command retains its separate twenty-unit rule.
    assert abs(contact_points[0][0] - contact_points[1][0]) == pytest.approx(10.0, abs=1e-8)
    assert tuple(controller.model.equipment[row.equipment_id].port_ids for row in (first, second)) == originals
    assert len(canvas.scene._bus_attachment_handles) == 2


def test_full_bus_is_incompatible_without_crash_or_extra_connection(canvas_factory):
    controller = _controller()
    bus = controller.add_electrical_node("Короткая шина", x=0, y=0, symbol_key="busbar_horizontal", width=20, height=12, voltage_class_id=U10)
    items = [controller.add_equipment("builtin.circuit_breaker", str(i), x=-200 - 200*i, y=-120,
             voltage_class_by_group={"main": U10}) for i in range(4)]
    from rza_calc.editor.controller import NodeTarget
    # A twenty-unit bus has three valid positions at the accepted ten-unit
    # threshold. The fourth connection, not the third, must fail atomically.
    for fraction, item in zip((0, .5, 1), items[:3]):
        controller.connect_port_to_node(item.port_ids[0], bus.node_id,
            node_representation_id=bus.representation_id, target_anchor_key=str(fraction))
    canvas = canvas_factory(controller)
    source = canvas.scene._items_by_id[items[3].representation_id].port_item(items[3].port_ids[0])
    before = _state(canvas)
    scene = canvas.scene
    scene.begin_connection(source)
    scene.update_connection_cursor(QPointF())
    assert scene._connection_target.feedback is ConnectionTargetFeedback.INCOMPATIBLE
    assert "нет свободного места" in scene._connection_target.message
    assert _state(canvas) == before
    scene.cancel_connection(announce=False)


@pytest.mark.parametrize("finish", ("commit_undo", "escape"))
def test_actual_legacy_physical_line_grab_incident_stroke_moves_same_owner(canvas_factory, finish):
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/energoraion.json"
    raw = path.read_bytes()
    canvas = canvas_factory(ProjectEditorController(load_project(path)))
    owner = next(item for item in canvas.scene._items_by_id.values()
                 if item._name == "ВЛ-110 Северная — Южная (резерв)")
    route = next(item for item in canvas.scene._route_items_by_id.values()
                 if canvas.scene._legacy_physical_line_owner(item) is owner)
    candidates = [(QPointF((a.x+b.x)/2, (a.y+b.y)/2))
                  for a, b in zip(route.route.waypoints, route.route.waypoints[1:])
                  if abs(a.x-b.x)+abs(a.y-b.y) > 60]
    point = next(point for point in candidates
                 if canvas.scene.resolve_hit_target(point, canvas.view.transform()).via_route is route)
    canvas.view.centerOn(point + QPointF(0, -40))
    before, journal = _state(canvas), len(canvas.controller.journal)
    initial = QPointF(owner.pos())
    _mouse(canvas, "press", point)
    assert canvas.scene.selected_representation_ids() == (owner.representation_id,)
    assert route._physical_owner_selected and not route.isSelected()
    assert canvas.scene._start_positions and not canvas.scene._connected_drag
    _mouse(canvas, "move", point + QPointF(0, -60))
    assert owner.pos() == initial + QPointF(0, -60)
    assert _state(canvas) == before
    if finish == "escape":
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        _mouse(canvas, "release", point + QPointF(0, -60))
        assert len(canvas.controller.journal) == journal
    else:
        _mouse(canvas, "release", point + QPointF(0, -60))
        assert canvas.scene.mouseGrabberItem() is None, "The delegated line grab must end before another operation"
        assert len(canvas.controller.journal) == journal + 1
        assert _state(canvas)[:2] == before[:2]
        assert set(canvas.controller.diagram.routes) == set(before[3])
        assert canvas.controller.diagram.representations[owner.representation_id].y == initial.y() - 60
        canvas.undo()
    assert _state(canvas) == before and path.read_bytes() == raw
    assert owner.direction_marker() is not None
