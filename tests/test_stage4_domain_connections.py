# -*- coding: utf-8 -*-
"""Доменный контракт Этапа 4: соединения, линии, отпайки и реклоузер."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
    RouteWaypoint,
    RouteWaypointId,
    RouteWaypointSource,
)
from rza_calc.domain.electrical import (
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor import (
    NewNodeTarget,
    NodeTarget,
    PhysicalLineInput,
    PortTarget,
    ProjectEditorController,
)
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")
U35 = VoltageClassId("builtin.voltage.ac.35kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project() -> _Project:
    page = DiagramPage(PageId("page.stage4.main"), "Основная схема")
    return _Project(
        ElectricalModel.with_builtins("Этап 4"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,),
            document_id=DiagramDocumentId("diagram.stage4"),
        ),
        ProjectCatalogSnapshots(),
    )


def _node(model: ElectricalModel, token: str, voltage=U10) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.stage4.{token}"),
        token,
        declared_voltage_class_id=voltage,
    )
    model.add_node(node)
    return node


def test_unknown_line_length_is_not_replaced_by_fictitious_millimetres() -> None:
    model = ElectricalModel.with_builtins("Неизвестная длина")
    first = _node(model, "unknown.first")
    second = _node(model, "unknown.second")

    _, section, _ = model.create_logical_line(
        "ВЛ без подтверждённой длины",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        None,
        voltage_class_id=U10,
    )

    stored = model.line_sections[section.equipment_id]
    assert stored.length_mm is None
    assert stored.construction_segments[0].length_confirmation is DataConfirmation.UNCONFIRMED
    adaptation = adapt_to_calculation(model)
    branch = next(iter(adaptation.network.branches.values()))
    assert branch.length_km == 0.0
    assert branch.calculation_block_reason
    assert any(item.code == "line_data_unconfirmed" for item in adaptation.diagnostics)


def test_confirmation_is_typed_and_explicit_impedance_can_be_confirmed() -> None:
    segment = LineConstructionSegment(
        LineConstructionSegmentId("segment.stage4.confirmed"),
        LineKind.CABLE,
        1_250_000,
        {"r1_ohm_per_km": 0.24, "x1_ohm_per_km": 0.08},
    )
    assert segment.length_confirmation is DataConfirmation.CONFIRMED
    assert segment.impedance_confirmation is DataConfirmation.CONFIRMED

    uncertain = LineConstructionSegment(
        LineConstructionSegmentId("segment.stage4.uncertain"),
        LineKind.CABLE,
        1_250_000,
        {"r1_ohm_per_km": 0.24, "x1_ohm_per_km": 0.08},
        length_confirmation=DataConfirmation.UNCONFIRMED,
        impedance_confirmation=DataConfirmation.UNCONFIRMED,
    )
    assert uncertain.length_confirmation is DataConfirmation.UNCONFIRMED
    assert uncertain.impedance_confirmation is DataConfirmation.UNCONFIRMED


def test_two_terminals_of_one_branch_cannot_be_connected_to_one_node() -> None:
    model = ElectricalModel.with_builtins("Самопетля")
    breaker, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "Q1",
        normal_position=SwitchPosition.CLOSED,
    )
    with pytest.raises(DomainInvariantError, match="одного оборудования"):
        model.connect_ports(breaker.port_ids[0], breaker.port_ids[1])
    assert not model.connections
    assert not model.electrical_nodes


def test_merge_nodes_preflights_branch_self_loop_without_partial_mutation() -> None:
    model = ElectricalModel.with_builtins("Слияние узлов")
    first = _node(model, "merge.first")
    second = _node(model, "merge.second")
    breaker, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "Q1",
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(breaker.port_ids[0], first.id)
    model.connect_port(breaker.port_ids[1], second.id)
    before = model.connectivity_signature()

    with pytest.raises(DomainInvariantError, match="одного оборудования"):
        model.merge_nodes(first.id, second.id)

    assert model.connectivity_signature() == before
    assert second.id in model.electrical_nodes


def test_controller_connects_ports_atomically_and_undo_restores_draft() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    first = controller.add_equipment("builtin.circuit_breaker", "Q1", voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.disconnector", "QS1", voltage_class_by_group={"main": U10})
    before = project.electrical_model.connectivity_signature()

    compatibility = controller.validate_connection(
        first.port_ids[1], PortTarget(second.port_ids[0])
    )
    assert compatibility.valid
    result = controller.connect_ports(first.port_ids[1], second.port_ids[0])
    assert result.node_id in project.electrical_model.electrical_nodes
    assert len(result.connection_ids) == 2

    controller.undo()
    assert project.electrical_model.connectivity_signature() == before


def test_controller_reconnect_preserves_equipment_port_and_connection_ids() -> None:
    project = _project()
    model = project.electrical_model
    first = _node(model, "reconnect.first")
    second = _node(model, "reconnect.second")
    third = _node(model, "reconnect.third")
    controller = ProjectEditorController(project)
    breaker = controller.add_equipment(
        "builtin.circuit_breaker", "Q1", voltage_class_by_group={"main": U10}
    )
    controller.connect_port_to_node(breaker.port_ids[0], first.id)
    controller.connect_port_to_node(breaker.port_ids[1], second.id)
    old = model.connection_for_port(breaker.port_ids[1])
    assert old is not None

    result = controller.reconnect_port(breaker.port_ids[1], NodeTarget(third.id))
    current = model.connection_for_port(breaker.port_ids[1])
    assert result.equipment_id == breaker.equipment_id
    assert current is not None and current.id == old.id
    assert current.electrical_node_id == third.id

    controller.undo()
    restored = model.connection_for_port(breaker.port_ids[1])
    assert restored is not None and restored.id == old.id
    assert restored.electrical_node_id == second.id


def test_public_physical_line_command_creates_logical_line_and_free_node() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    source = controller.add_equipment(
        "builtin.external_grid",
        "Источник",
        voltage_class_by_group={"main": U10},
    )

    result = controller.create_physical_line(
        "ВЛ-10 кВ №1",
        LineKind.OVERHEAD,
        PortTarget(source.port_ids[0]),
        NewNodeTarget("Конец ВЛ", x=500.0, y=100.0, voltage_class_id=U10),
        physical=PhysicalLineInput(
            length_mm=None,
            length_confirmation=DataConfirmation.UNCONFIRMED,
        ),
    )

    assert result.logical_line_id in project.electrical_model.logical_lines
    assert result.section_id in project.electrical_model.line_sections
    assert result.end_node_id in project.electrical_model.electrical_nodes
    assert any(
        item.equipment_id == result.section_id
        for item in project.diagram.routes.values()
    )
    assert project.electrical_model.line_sections[result.section_id].length_mm is None


def test_tap_with_unknown_length_is_one_project_command_and_keeps_main_line() -> None:
    project = _project()
    first = _node(project.electrical_model, "tap.first")
    second = _node(project.electrical_model, "tap.second")
    branch_end = _node(project.electrical_model, "tap.branch")
    controller = ProjectEditorController(project)
    main = controller.create_physical_line(
        "ВЛ основная",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
    )
    before = project.electrical_model.connectivity_signature()

    result = controller.create_tap(
        main.section_id,
        None,
        "Отпайка",
        LineKind.CABLE,
        NodeTarget(branch_end.id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
    )

    line = project.electrical_model.logical_lines[main.logical_line_id]
    assert line.section_equipment_ids == (
        result.first_section_id,
        result.second_section_id,
    )
    assert len([
        item for item in project.electrical_model.connections.values()
        if item.electrical_node_id == result.tap_node_id
    ]) == 3

    controller.undo()
    assert project.electrical_model.connectivity_signature() == before
    assert main.section_id in project.electrical_model.line_sections


def test_insert_recloser_creates_two_nodes_shared_feeder_and_one_undo() -> None:
    project = _project()
    first = _node(project.electrical_model, "insert.first")
    second = _node(project.electrical_model, "insert.second")
    controller = ProjectEditorController(project)
    main = controller.create_physical_line(
        "ВЛ с реклоузером",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(
            10_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    before = project.electrical_model.connectivity_signature()

    inserted = controller.insert_recloser(
        main.section_id,
        4_000_000,
        "Р-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
    )

    assert inserted.left_node_id != inserted.right_node_id
    assert inserted.recloser_id in project.electrical_model.equipment
    left_line = project.electrical_model.logical_lines[inserted.left_logical_line_id]
    right_line = project.electrical_model.logical_lines[inserted.right_logical_line_id]
    assert left_line.feeder_id is not None
    assert left_line.feeder_id == right_line.feeder_id

    state = OperatingState(
        OperatingStateId("state.stage4.insert"),
        "Проверка реклоузера",
        {inserted.recloser_id: SwitchPosition.OPEN},
    )
    project.electrical_model.add_operating_state(state)
    opened = TopologyEngine().compile(project.electrical_model, state.id)
    assert not opened.has_path(inserted.left_node_id, inserted.right_node_id)

    # Внешнее изменение режима не входит в историю контроллера, поэтому
    # удаляем его перед проверкой точного undo составной команды.
    project.electrical_model._operating_states.pop(state.id)
    project.electrical_model._revision -= 1
    controller.undo()
    assert project.electrical_model.connectivity_signature() == before
    assert main.section_id in project.electrical_model.line_sections


def test_route_only_edit_has_undo_redo_and_never_changes_topology() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    first = controller.add_equipment("builtin.load", "Нагрузка-1", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Нагрузка-2", x=200, y=0, voltage_class_by_group={"main": U10})
    terminals = [controller._port_anchor_geometry(
        controller.model, controller.diagram.representations[item.representation_id], item.port_ids[0]
    ) for item in (first, second)]
    initial_points = tuple(RouteWaypoint(RouteWaypointId.new(), x, y) for x, y in (
        (terminals[0].x, terminals[0].y), (terminals[0].x, -60),
        (terminals[1].x, -60), (terminals[1].x, terminals[1].y),
    ))
    connected = controller.connect_ports(
        first.port_ids[0], second.port_ids[0], route_waypoints=initial_points)
    assert connected.route_id is not None
    original = project.diagram.routes[connected.route_id]
    signature = project.electrical_model.connectivity_signature()
    changed_points = (
        original.waypoints[0],
        RouteWaypoint(
            RouteWaypointId.new(),
            terminals[0].x,
            -100,
            RouteWaypointSource.USER,
            True,
        ),
        RouteWaypoint(RouteWaypointId.new(), terminals[1].x, -100),
        original.waypoints[-1],
    )
    assert [(p.x, p.y) for p in (changed_points[0], changed_points[-1])] == [
        (p.x, p.y) for p in terminals]

    controller.reroute_diagram_route(connected.route_id, changed_points)
    assert project.diagram.routes[connected.route_id].waypoints == changed_points
    assert project.electrical_model.connectivity_signature() == signature
    controller.undo()
    assert project.diagram.routes[connected.route_id] == original
    assert project.electrical_model.connectivity_signature() == signature
    controller.redo()
    assert project.diagram.routes[connected.route_id].waypoints == changed_points


def test_move_updates_only_incident_route_and_is_one_undo_step() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    first = controller.add_equipment("builtin.load", "A", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "B", x=200, y=0, voltage_class_by_group={"main": U10})
    third = controller.add_equipment("builtin.load", "C", x=0, y=200, voltage_class_by_group={"main": U10})
    fourth = controller.add_equipment("builtin.load", "D", x=200, y=200, voltage_class_by_group={"main": U10})
    # Give this geometry test real terminal coordinates, not the legacy
    # unspecified-route default at the middle of a load symbol.
    terminals = [controller._port_anchor_geometry(
        controller.model, controller.diagram.representations[item.representation_id], item.port_ids[0]
    ) for item in (first, second)]
    incident = controller.connect_ports(first.port_ids[0], second.port_ids[0], route_waypoints=tuple(
        RouteWaypoint(RouteWaypointId.new(), point.x, point.y) for point in terminals))
    unrelated = controller.connect_ports(third.port_ids[0], fourth.port_ids[0])
    assert incident.route_id is not None and unrelated.route_id is not None
    before_representation = project.diagram.representations[first.representation_id]
    before_incident = project.diagram.routes[incident.route_id]
    before_unrelated = project.diagram.routes[unrelated.route_id]
    signature = project.electrical_model.connectivity_signature()

    controller.move_representations((first.representation_id,), 40, 20, bypass_snap=True)

    moved = project.diagram.routes[incident.route_id]
    assert moved.waypoints[0].x == before_incident.waypoints[0].x + 40
    assert moved.waypoints[0].y == before_incident.waypoints[0].y + 20
    assert project.diagram.routes[unrelated.route_id] == before_unrelated
    assert project.electrical_model.connectivity_signature() == signature
    controller.undo()
    assert project.diagram.representations[first.representation_id] == before_representation
    assert project.diagram.routes[incident.route_id] == before_incident
    assert project.diagram.routes[unrelated.route_id] == before_unrelated


def test_copy_paste_and_delete_cascade_routes_with_fresh_ids() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    first = controller.add_equipment("builtin.load", "A", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "B", x=200, y=0, voltage_class_by_group={"main": U10})
    connected = controller.connect_ports(first.port_ids[0], second.port_ids[0])
    assert connected.route_id is not None
    source_route = project.diagram.routes[connected.route_id]

    clipboard = controller.copy(
        (first.representation_id, second.representation_id)
    )
    assert tuple(item.id for item in clipboard.routes) == (connected.route_id,)
    pasted = controller.paste(offset_x=40, offset_y=80)
    assert len(pasted.route_ids) == 1
    copied_route = project.diagram.routes[pasted.route_ids[0]]
    assert copied_route.id != source_route.id
    assert copied_route.electrical_node_id != source_route.electrical_node_id
    assert {
        item.id for item in copied_route.waypoints
    }.isdisjoint({item.id for item in source_route.waypoints})
    assert project.diagram.validate_targets(project.electrical_model) == ()

    removed = controller.remove_from_page(
        (pasted.representation_ids[0],), mark_as_unplaced=True
    )
    assert pasted.route_ids[0] in removed.route_ids
    assert pasted.route_ids[0] not in project.diagram.routes
    controller.undo()
    assert pasted.route_ids[0] in project.diagram.routes

    deleted = controller.delete_from_project(
        (first.representation_id, second.representation_id)
    )
    assert connected.route_id in deleted.route_ids
    assert connected.route_id not in project.diagram.routes


def test_switch_equipment_creates_active_sparse_state_and_is_undoable() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    recloser = controller.add_equipment(
        "builtin.recloser",
        "Р-1",
        voltage_class_by_group={"main": U10},
    )
    assert controller.effective_switch_position(recloser.equipment_id) is SwitchPosition.CLOSED

    state_id = controller.switch_equipment(
        recloser.equipment_id,
        SwitchPosition.OPEN,
        confirmed=True,
    )

    assert controller.active_operating_state_id == state_id
    assert controller.effective_switch_position(recloser.equipment_id) is SwitchPosition.OPEN
    assert project.electrical_model.operating_states[state_id].positions == {
        recloser.equipment_id: SwitchPosition.OPEN
    }
    controller.undo()
    assert controller.active_operating_state_id is None
    assert controller.workspace_state.active_operating_state_id is None
    assert state_id not in project.electrical_model.operating_states
    assert controller.effective_switch_position(recloser.equipment_id) is SwitchPosition.CLOSED


def test_existing_port_reconnects_to_line_tap_with_same_connection_id() -> None:
    project = _project()
    first = _node(project.electrical_model, "port.tap.first")
    second = _node(project.electrical_model, "port.tap.second")
    old_target = _node(project.electrical_model, "port.tap.old")
    controller = ProjectEditorController(project)
    line = controller.create_physical_line(
        "ВЛ для подключения порта",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(
            1_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    load = controller.add_equipment(
        "builtin.load", "Нагрузка отпайки", x=300, y=200, voltage_class_by_group={"main": U10}
    )
    connected = controller.connect_port_to_node(load.port_ids[0], old_target.id)
    old_connection_id = connected.connection_ids[0]
    before = project.electrical_model.connectivity_signature()

    result = controller.reconnect_port_to_tap(
        load.port_ids[0],
        line.section_id,
        400_000,
    )

    connection = project.electrical_model.connection_for_port(load.port_ids[0])
    assert connection is not None and connection.id == old_connection_id
    assert connection.electrical_node_id == result.tap_node_id
    assert len([
        item for item in project.electrical_model.connections.values()
        if item.electrical_node_id == result.tap_node_id
    ]) == 3
    controller.undo()
    assert project.electrical_model.connectivity_signature() == before
    assert line.section_id in project.electrical_model.line_sections


def test_controller_remove_tap_merges_routes_and_undo_restores_tap() -> None:
    project = _project()
    first = _node(project.electrical_model, "remove.tap.first")
    second = _node(project.electrical_model, "remove.tap.second")
    branch_end = _node(project.electrical_model, "remove.tap.branch")
    controller = ProjectEditorController(project)
    line = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(
            1_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    tap = controller.create_tap(
        line.section_id,
        400_000,
        "Отпайка",
        LineKind.CABLE,
        NodeTarget(branch_end.id),
        physical=PhysicalLineInput(
            100_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.1},
            DataConfirmation.CONFIRMED,
        ),
    )
    tap_signature = project.electrical_model.connectivity_signature()

    removed = controller.remove_tap(
        tap.main_logical_line_id,
        tap.tap_node_id,
        branch_line_ids=(tap.branch_logical_line_id,),
    )

    assert removed.merged_section_id in project.electrical_model.line_sections
    assert removed.route_id in project.diagram.routes
    assert project.diagram.validate_targets(project.electrical_model) == ()
    controller.undo()
    assert project.electrical_model.connectivity_signature() == tap_signature
    assert tap.tap_node_id in project.electrical_model.electrical_nodes


def test_controller_removes_one_of_multiple_tap_branches_without_collapsing_node() -> None:
    project = _project()
    first = _node(project.electrical_model, "remove.multi.first")
    second = _node(project.electrical_model, "remove.multi.second")
    branch_end_1 = _node(project.electrical_model, "remove.multi.branch.1")
    branch_end_2 = _node(project.electrical_model, "remove.multi.branch.2")
    controller = ProjectEditorController(project)
    line = controller.create_physical_line(
        "Основная ВЛ с многоветвевой отпайкой",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(
            1_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    tap = controller.create_tap(
        line.section_id,
        400_000,
        "Первая отпайка",
        LineKind.CABLE,
        NodeTarget(branch_end_1.id),
        physical=PhysicalLineInput(
            100_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.1},
            DataConfirmation.CONFIRMED,
        ),
    )
    second_branch = controller.create_physical_line(
        "Вторая отпайка того же узла",
        LineKind.OVERHEAD,
        NodeTarget(tap.tap_node_id),
        NodeTarget(branch_end_2.id),
        physical=PhysicalLineInput(
            120_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.3, "x1_ohm_per_km": 0.2},
            DataConfirmation.CONFIRMED,
        ),
    )
    before = project.electrical_model.connectivity_signature()

    removed = controller.remove_tap(tap.branch_section_id)

    assert removed.removed_tap_node_id is None
    assert removed.merged_section_id is None
    assert tap.branch_logical_line_id not in project.electrical_model.logical_lines
    assert tap.tap_node_id in project.electrical_model.electrical_nodes
    assert second_branch.logical_line_id in project.electrical_model.logical_lines
    assert len([
        connection
        for connection in project.electrical_model.connections.values()
        if connection.electrical_node_id == tap.tap_node_id
    ]) == 3
    assert len(
        project.electrical_model.logical_lines[tap.main_logical_line_id]
        .section_equipment_ids
    ) == 2
    controller.undo()
    assert project.electrical_model.connectivity_signature() == before


def test_confirm_length_and_split_physical_line_are_atomic_public_commands() -> None:
    project = _project()
    first = _node(project.electrical_model, "split.public.first")
    second = _node(project.electrical_model, "split.public.second")
    controller = ProjectEditorController(project)
    line = controller.create_physical_line(
        "ВЛ с уточняемой длиной",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(
            None,
            DataConfirmation.UNCONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    initial_route = project.diagram.routes[line.route_id]

    confirmed = controller.confirm_line_length(line.section_id, 1_000_000)

    assert confirmed.length_mm == 1_000_000
    section = project.electrical_model.line_sections[line.section_id]
    assert section.length_mm == 1_000_000
    assert project.diagram.routes[line.route_id] == initial_route
    controller.undo()
    assert project.electrical_model.line_sections[line.section_id].length_mm is None
    controller.redo()

    before_split = project.electrical_model.connectivity_signature()
    split = controller.split_physical_line(line.section_id, 400_000)

    assert line.section_id not in project.electrical_model.line_sections
    assert len(split.route_ids) == 2
    assert all(route_id in project.diagram.routes for route_id in split.route_ids)
    assert line.route_id not in project.diagram.routes
    assert project.diagram.validate_targets(project.electrical_model) == ()
    controller.undo()
    assert project.electrical_model.connectivity_signature() == before_split
    assert line.section_id in project.electrical_model.line_sections
    assert project.diagram.routes[line.route_id] == initial_route


def test_remove_recloser_from_line_merges_and_undo_restores_insertion() -> None:
    project = _project()
    first = _node(project.electrical_model, "remove.recloser.first")
    second = _node(project.electrical_model, "remove.recloser.second")
    controller = ProjectEditorController(project)
    line = controller.create_physical_line(
        "ВЛ с реклоузером",
        LineKind.OVERHEAD,
        NodeTarget(first.id),
        NodeTarget(second.id),
        physical=PhysicalLineInput(
            1_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    inserted = controller.insert_recloser(
        line.section_id,
        400_000,
        "Р-удаляемый",
    )
    inserted_signature = project.electrical_model.connectivity_signature()

    removed = controller.remove_recloser_from_line(inserted.recloser_id)

    assert inserted.recloser_id not in project.electrical_model.equipment
    assert inserted.right_logical_line_id not in project.electrical_model.logical_lines
    assert removed.merged_section_id in project.electrical_model.line_sections
    assert removed.route_id in project.diagram.routes
    assert project.diagram.validate_targets(project.electrical_model) == ()
    controller.undo()
    assert project.electrical_model.connectivity_signature() == inserted_signature
    assert inserted.recloser_id in project.electrical_model.equipment
