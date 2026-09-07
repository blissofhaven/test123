"""One explicit connection gesture, with atomic electrical meaning."""
from dataclasses import replace

import pytest
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu
from PySide6.QtTest import QTest

from rza_calc.domain.diagram import (DiagramRouteKind, DiagramRoute, DiagramRouteId,
    RouteAnchorKind, RouteEndpointAnchor, RouteWaypoint, RouteWaypointId)
from rza_calc.domain.electrical import DataConfirmation, LineKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict
from rza_calc.editor.controller import EditorCommandError, NodeTarget, PhysicalLineInput, PortTarget
from test_ui_direct_connections import U10, _controller, _mouse, _state, canvas_factory
from test_stage02_connection_gestures import menu_reply, assert_one_menu, answer_real_menu


def _node_pair():
    controller = _controller()
    first = controller.add_electrical_node("Шина A", x=0, y=0, symbol_key="busbar", width=160, height=12, voltage_class_id=U10)
    second = controller.add_electrical_node("Шина B", x=240, y=160, symbol_key="busbar", width=160, height=12, voltage_class_id=U10)
    return controller, first, second


def test_node_wire_merges_explicitly_and_undo_restores_all_references():
    controller, first, second = _node_pair()
    before = (electrical_model_fingerprint(controller.model), controller.diagram)
    result = controller.connect_from_node(NodeTarget(first.node_id, first.representation_id), NodeTarget(second.node_id, second.representation_id))
    assert result.node_id == first.node_id
    assert second.node_id not in controller.model.electrical_nodes
    assert not controller.diagram.validate_targets(controller.model)
    assert controller.diagram.routes[result.route_id].kind is DiagramRouteKind.NODE_CONNECTION
    controller.undo()
    assert (electrical_model_fingerprint(controller.model), controller.diagram) == before
    controller.redo()
    assert not controller.diagram.validate_targets(controller.model)


def test_node_wire_cannot_short_out_an_apparatus():
    controller, first, second = _node_pair()
    breaker = controller.add_equipment("builtin.circuit_breaker", "QF", x=100, y=100, voltage_class_by_group={"main": U10})
    controller.connect_port_to_node(breaker.port_ids[0], first.node_id)
    controller.connect_port_to_node(breaker.port_ids[1], second.node_id)
    before = (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal))
    with pytest.raises(EditorCommandError, match="одного оборудования"):
        controller.connect_from_node(NodeTarget(first.node_id, first.representation_id), NodeTarget(second.node_id, second.representation_id))
    assert (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)) == before


def test_reconnect_target_apparatus_cannot_replace_a_line_branch_port():
    controller, first, second = _node_pair()
    breaker = controller.add_equipment("builtin.circuit_breaker", "QF", x=100, y=100, voltage_class_by_group={"main": U10})
    line = controller.create_physical_line("КЛ", LineKind.CABLE, PortTarget(breaker.port_ids[0], breaker.representation_id), NodeTarget(first.node_id, first.representation_id), physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
    route = controller.diagram.routes[line.route_id]
    before = (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal))
    with pytest.raises(EditorCommandError, match="конца линии"):
        controller.reconnect_port(breaker.port_ids[0], NodeTarget(second.node_id, second.representation_id))
    assert (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)) == before
    assert controller.diagram.routes[line.route_id].start_anchor.branch_port_id == route.start_anchor.branch_port_id


@pytest.mark.parametrize("choice", ["wire", "cable", "overhead", None])
def test_bus_drag_in_connection_tool_opens_real_menu(canvas_factory, menu_reply, choice):
    controller, first, second = _node_pair()
    canvas = canvas_factory(controller)
    before = _state(canvas)
    calls = menu_reply(canvas, choice)
    canvas.activate_connection_tool()
    start, end = QPointF(0, 0), QPointF(240, 160)
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    _mouse(canvas, "release", end)
    assert_one_menu(canvas, calls, before)
    if choice is None:
        assert _state(canvas) == before
    else:
        assert len(controller.diagram.routes) == 1
        assert not controller.diagram.validate_targets(controller.model)
        controller.undo()
        assert _state(canvas) == before


@pytest.mark.parametrize("finish", ["release", "enter", "escape"])
@pytest.mark.parametrize("choice", ["wire", "cable"])
def test_wire_source_to_port_keeps_the_existing_conductor(canvas_factory, menu_reply, finish, choice):
    controller, first, second = _node_pair()
    load = controller.add_equipment("builtin.load", "Нагрузка", x=0, y=240, voltage_class_by_group={"main": U10})
    wire = controller.connect_port_to_node(load.port_ids[0], first.node_id,
        source_representation_id=load.representation_id, node_representation_id=first.representation_id)
    target = controller.add_equipment("builtin.load", "Приёмник", x=240, y=240, voltage_class_by_group={"main": U10})
    canvas = canvas_factory(controller)
    canvas.activate_connection_tool()
    old_route = controller.diagram.routes[wire.route_id]
    start = QPointF(old_route.waypoints[0].x, (old_route.waypoints[0].y + old_route.waypoints[-1].y) / 2)
    end = canvas.scene._items_by_id[target.representation_id].port_item(target.port_ids[0]).scenePos()
    before = _state(canvas)
    calls = menu_reply(canvas, choice)
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    if finish == "release":
        _mouse(canvas, "release", end)
    else:
        QTest.keyClick(canvas.view, Qt.Key.Key_Return if finish == "enter" else Qt.Key.Key_Escape)
        _mouse(canvas, "release", end)
    if finish == "escape":
        assert not calls
        assert _state(canvas) == before
    else:
        assert_one_menu(canvas, calls, before)
        assert controller.diagram.routes[wire.route_id].start_anchor == old_route.start_anchor
        created = next(route for route in controller.diagram.routes.values()
                       if route.end_anchor.target_port_id == target.port_ids[0])
        assert (created.waypoints[0].x, created.waypoints[0].y) == pytest.approx((start.x(), start.y()))
        assert not controller.diagram.validate_targets(controller.model)
        assert controller.model.connection_for_port(target.port_ids[0]) is not None
        controller.undo()
        assert _state(canvas) == before


def _legacy_direct_line():
    from rza_calc.adapters.legacy_calculation import import_legacy_network
    from rza_calc.core.model import Network, Node, LineBranch
    from rza_calc.editor.controller import ProjectEditorController
    net = Network("Legacy")
    net.add_node(Node("A", "A", 10))
    net.add_node(Node("B", "B", 10))
    net.add_branch(LineBranch(id="L", name="КЛ старая", node_from="A", node_to="B", length_km=2, r0=.1, x0=.2))
    project = _controller()._project
    project.electrical_model = import_legacy_network(net)
    controller = ProjectEditorController(project)
    equipment = next(eq for eq in controller.model.equipment.values()
                     if controller.model.equipment_type(eq.type_id, eq.type_version).behavior_key == "legacy.line")
    ends = []
    for index, port in enumerate(equipment.port_ids):
        qf = controller.add_equipment("builtin.circuit_breaker", "QF" + str(index),
                                     x=index * 240, y=0, voltage_class_by_group={"main": U10})
        node = controller.model.node_for_port(port)
        controller.connect_port_to_node(qf.port_ids[index], node.id)
        ends.append((qf, port, node.id))
    anchors, points = [], []
    for index, (qf, branch_port, node) in enumerate(ends):
        target_port = qf.port_ids[index]
        anchors.append(RouteEndpointAnchor(RouteAnchorKind.EQUIPMENT_PORT, qf.representation_id,
                                          node, branch_port_id=branch_port, target_port_id=target_port))
        point = controller._port_anchor_geometry(controller.model,
                    controller.diagram.representations[qf.representation_id], target_port)
        points.append(RouteWaypoint(RouteWaypointId.new(), point.x, point.y))
    route = DiagramRoute(DiagramRouteId.new(), next(iter(controller.diagram.pages)),
        DiagramRouteKind.EQUIPMENT_BRANCH, *anchors, equipment_id=equipment.id,
        waypoints=tuple(points))
    project.diagram = replace(project.diagram, routes={route.id: route})
    assert not project.diagram.validate_targets(controller.model)
    return ProjectEditorController(project), route, ends


def test_legacy_direct_route_moves_with_qf_and_preserves_electrical_identity():
    controller, route, ends = _legacy_direct_line()
    before = electrical_model_fingerprint(controller.model)
    qf = ends[0][0]
    controller.move_representations((qf.representation_id,), -80, 0)
    moved = controller.diagram.routes[route.id]
    assert moved.equipment_id == route.equipment_id
    assert moved.start_anchor == route.start_anchor
    assert moved.waypoints[0].x == route.waypoints[0].x - 80
    assert electrical_model_fingerprint(controller.model) == before
    assert not controller.diagram.validate_targets(controller.model)


def test_legacy_direct_route_endpoint_reconnects_its_branch_port_without_menu(canvas_factory, menu_reply):
    controller, route, ends = _legacy_direct_line()
    target = controller.add_electrical_node("Новая шина", x=120, y=180, symbol_key="busbar", width=160, height=12, voltage_class_id=U10)
    canvas = canvas_factory(controller)
    item = canvas.scene._route_items_by_id[route.id]
    item.setSelected(True)
    QApplication.processEvents()
    handle = item._endpoint_handles[False]
    equipment = controller.model.equipment[route.equipment_id]
    calls = menu_reply(canvas, "wire")
    before = _state(canvas)
    start, end = handle.scenePos(), QPointF(120, 180)
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    _mouse(canvas, "release", end)
    assert not calls
    moved = controller.diagram.routes[route.id]
    assert moved.end_anchor.branch_port_id == route.end_anchor.branch_port_id
    assert moved.end_anchor.electrical_node_id == target.node_id
    updated_equipment = controller.model.equipment[route.equipment_id]
    assert (updated_equipment.id, updated_equipment.port_ids, updated_equipment.properties,
            updated_equipment.name, updated_equipment.type_id) == (
            equipment.id, equipment.port_ids, equipment.properties, equipment.name, equipment.type_id)
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert _state(canvas) == before
    canvas.scene.clearSelection()


def test_conductor_junction_survives_move_serialization_and_undo():
    controller, first, second = _node_pair()
    load = controller.add_equipment("builtin.load", "Первый", x=0, y=240, voltage_class_by_group={"main": U10})
    original = controller.connect_port_to_node(load.port_ids[0], first.node_id,
        source_representation_id=load.representation_id, node_representation_id=first.representation_id)
    other = controller.add_equipment("builtin.load", "Отпайка", x=240, y=240, voltage_class_by_group={"main": U10})
    route = controller.diagram.routes[original.route_id]
    before = (electrical_model_fingerprint(controller.model), controller.diagram)
    tap_target = NodeTarget(first.node_id, x=0, y=120, route_id=route.id)
    created = controller.create_physical_line("КЛ", LineKind.CABLE, tap_target,
        PortTarget(other.port_ids[0], other.representation_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
    line = controller.diagram.routes[created.route_id]
    tap_id = line.start_anchor.representation_id
    tap = controller.diagram.representations[tap_id]
    assert (tap.x, tap.y, tap.electrical_node_id) == (0, 120, first.node_id)
    assert len([row for row in controller.diagram.routes.values()
                if any(anchor.representation_id == tap_id for anchor in (row.start_anchor, row.end_anchor))]) == 3
    assert original.route_id in controller.diagram.routes
    graph = controller.diagram
    fingerprint = electrical_model_fingerprint(controller.model)
    controller.move_representations((other.representation_id,), 80, 0)
    assert controller.diagram.routes[line.id].start_anchor == line.start_anchor
    assert controller.diagram.representations[tap_id] == tap
    assert electrical_model_fingerprint(controller.model) == fingerprint
    restored_model = electrical_model_from_dict(electrical_model_to_dict(controller.model))
    restored_diagram = diagram_from_dict(diagram_to_dict(controller.diagram))
    assert restored_diagram == controller.diagram
    assert not restored_diagram.validate_targets(restored_model)
    controller.undo()
    assert controller.diagram == graph
    controller.undo()
    assert (electrical_model_fingerprint(controller.model), controller.diagram) == before


@pytest.mark.parametrize("choice", ["wire", "overhead"])
def test_port_release_on_conductor_anchors_at_the_hit_not_its_bus(canvas_factory, menu_reply, choice):
    controller, first, second = _node_pair()
    load = controller.add_equipment("builtin.load", "Первый", x=0, y=240, voltage_class_by_group={"main": U10})
    wire = controller.connect_port_to_node(load.port_ids[0], first.node_id,
        source_representation_id=load.representation_id, node_representation_id=first.representation_id)
    other = controller.add_equipment("builtin.load", "Отпайка", x=240, y=240, voltage_class_by_group={"main": U10})
    canvas = canvas_factory(controller)
    port = canvas.scene._items_by_id[other.representation_id].port_item(other.port_ids[0])
    before = _state(canvas)
    calls = menu_reply(canvas, choice)
    start, end = port.scenePos(), QPointF(0, 120)
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    _mouse(canvas, "release", end)
    assert_one_menu(canvas, calls, before)
    attached = next(row for row in controller.diagram.routes.values()
                    if any(anchor.target_port_id == other.port_ids[0] for anchor in (row.start_anchor, row.end_anchor)))
    assert any((point.x, point.y) == (0, 120) for point in (attached.waypoints[0], attached.waypoints[-1]))
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert _state(canvas) == before


def test_bus_same_node_line_insertion_is_symmetric_and_preserves_other_taps(canvas_factory, menu_reply):
    controller, bus, second = _node_pair()
    first = controller.add_equipment("builtin.load", "Первый", x=0, y=240, voltage_class_by_group={"main": U10})
    peer = controller.add_equipment("builtin.load", "Другой", x=240, y=240, voltage_class_by_group={"main": U10})
    controller.connect_port_to_node(first.port_ids[0], bus.node_id)
    controller.connect_port_to_node(peer.port_ids[0], bus.node_id)
    canvas = canvas_factory(controller)
    before = _state(canvas)
    calls = menu_reply(canvas, "cable")
    canvas.activate_connection_tool()
    start = QPointF(-60, 0)
    end = canvas.scene._items_by_id[first.representation_id].port_item(first.port_ids[0]).scenePos()
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    _mouse(canvas, "release", end)
    assert_one_menu(canvas, calls, before)
    assert len(controller.model.line_sections) == 1
    assert controller.model.node_for_port(peer.port_ids[0]).id == bus.node_id
    assert controller.model.node_for_port(first.port_ids[0]).id != bus.node_id
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert _state(canvas) == before


def _catalog_fixture():
    from rza_calc.domain.catalog import (CatalogEntry, CatalogEntryId, CatalogOrigin,
        CatalogCategoryId, UserCatalog)
    from rza_calc.domain.electrical import EquipmentTypeId
    controller, first, second = _node_pair()
    controller._project.user_catalog = UserCatalog()
    controller = type(controller)(controller._project)
    entry = CatalogEntry(CatalogEntryId.new(), CatalogOrigin.USER,
        CatalogCategoryId("cable_lines"), "Моя марка", EquipmentTypeId("builtin.line_section.cable"), 1,
        properties={"conductor_mark": "Моя марка", "r1_ohm_per_km": .15, "x1_ohm_per_km": .08},
        source="Введено пользователем по паспорту № 1")
    return controller, first, second, entry


def test_line_and_user_catalog_entry_commit_and_undo_together():
    controller, first, second, entry = _catalog_fixture()
    result = controller.create_physical_line("КЛ", LineKind.CABLE,
        NodeTarget(first.node_id, first.representation_id), NodeTarget(second.node_id, second.representation_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
        catalog_entry=entry, remember_catalog_entry=True)
    assert controller._project.user_catalog.entries[entry.id] == entry
    binding = controller._project.catalog_snapshots.bindings[result.section_id]
    assert binding.entry.source == entry.source
    assert binding.source_catalog_id == controller._project.user_catalog.id
    assert controller.model.equipment[result.section_id].properties["r1_ohm_per_km"] == .15
    section = controller.model.line_sections[result.section_id]
    assert section.length_mm is None
    assert section.construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED
    controller.undo()
    assert not controller._project.user_catalog.entries
    assert not controller._project.catalog_snapshots.bindings
    assert result.section_id not in controller.model.equipment
    controller.redo()
    assert controller._project.user_catalog.entries[entry.id] == entry
    assert controller._project.catalog_snapshots.bindings[result.section_id] == binding


def test_failed_line_creation_does_not_leave_a_saved_catalog_entry():
    controller, first, second, entry = _catalog_fixture()
    before = (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal))
    with pytest.raises(EditorCommandError, match="разных узла"):
        controller.create_physical_line("КЛ", LineKind.CABLE,
            NodeTarget(first.node_id, first.representation_id), NodeTarget(first.node_id, first.representation_id),
            physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
            catalog_entry=entry, remember_catalog_entry=True)
    assert not controller._project.user_catalog.entries
    assert not controller._project.catalog_snapshots.bindings
    assert (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)) == before


def test_cancel_line_parameters_after_type_choice_leaves_the_whole_gesture_uncommitted(canvas_factory):
    controller, first, second = _node_pair()
    canvas = canvas_factory(controller)
    before = _state(canvas)
    history_size = len(controller.journal)
    timers = []
    try:
        calls = answer_real_menu(canvas, "cable", timers, line_reply="cancel")
        QTest.mouseClick(canvas.connect_button, Qt.MouseButton.LeftButton)
        _mouse(canvas, "move", QPointF(0, 0))
        _mouse(canvas, "press", QPointF(0, 0))
        _mouse(canvas, "move", QPointF(120, 80))
        _mouse(canvas, "move", QPointF(240, 160))
        _mouse(canvas, "release", QPointF(240, 160))
        assert_one_menu(canvas, calls, before)
        assert _state(canvas) == before
        assert len(controller.journal) == history_size
        assert not canvas.scene.connection_active
    finally:
        for timer in timers:
            timer.stop()


@pytest.mark.parametrize("kind", ["wire", "cable"])
def test_real_bus_start_moves_only_the_new_attachment_away_from_an_occupied_point(canvas_factory, menu_reply, kind):
    controller, bus, second = _node_pair()
    first = controller.add_equipment("builtin.load", "Первый", x=0, y=240, voltage_class_by_group={"main": U10})
    original = controller.connect_port_to_node(first.port_ids[0], bus.node_id)
    peer = controller.add_equipment("builtin.load", "Другой", x=240, y=240, voltage_class_by_group={"main": U10})
    canvas = canvas_factory(controller)
    old_route = controller.diagram.routes[original.route_id]
    before = _state(canvas)
    calls = menu_reply(canvas, kind)
    QTest.mouseClick(canvas.connect_button, Qt.MouseButton.LeftButton)
    start = QPointF(old_route.waypoints[-1].x, old_route.waypoints[-1].y)
    end = canvas.scene._items_by_id[peer.representation_id].port_item(peer.port_ids[0]).scenePos()
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    _mouse(canvas, "release", end)
    assert_one_menu(canvas, calls, before)
    assert controller.diagram.routes[original.route_id] == old_route
    created = next(row for row in controller.diagram.routes.values() if row.id != original.route_id)
    assert abs(created.waypoints[0].x - start.x()) == pytest.approx(10)
    assert created.waypoints[0].y == start.y()
    controller.undo()
    assert _state(canvas) == before


@pytest.mark.parametrize("source", ["bus", "wire"])
def test_context_start_connection_is_explicit_and_escape_changes_nothing(canvas_factory, source):
    controller, bus, other = _node_pair()
    load = controller.add_equipment("builtin.load", "Первый", x=0, y=240, voltage_class_by_group={"main": U10})
    controller.connect_port_to_node(load.port_ids[0], bus.node_id)
    canvas = canvas_factory(controller)
    before = _state(canvas)
    point = QPointF(0, 0 if source == "bus" else 120)
    seen = []
    poll, watchdog = QTimer(canvas), QTimer(canvas)
    watchdog.setSingleShot(True)
    def answer():
        for menu in QApplication.topLevelWidgets():
            if isinstance(menu, QMenu) and menu.isVisible():
                action = next((row for row in menu.actions() if row.text() == "Начать соединение"), None)
                if action is not None:
                    poll.stop()
                    seen.append(True)
                    menu.setActiveAction(action)
                    QTest.keyClick(menu, Qt.Key.Key_Return)
                    watchdog.stop()
                    return
    def timeout():
        poll.stop()
        for menu in QApplication.topLevelWidgets():
            if isinstance(menu, QMenu) and menu.isVisible():
                menu.close()
    poll.timeout.connect(answer)
    watchdog.timeout.connect(timeout)
    poll.start(1)
    watchdog.start(1500)
    try:
        local = canvas.view.mapFromScene(point)
        QApplication.sendEvent(canvas.view.viewport(), QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, local, canvas.view.viewport().mapToGlobal(local)))
        assert seen == [True]
        assert canvas.scene.connection_active
        assert _state(canvas) == before
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert not canvas.scene.connection_active
        assert _state(canvas) == before
        canvas.scene.clearSelection()
    finally:
        poll.stop()
        watchdog.stop()
