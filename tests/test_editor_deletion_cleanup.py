"""Deletion cleans drawing tails without deleting neighbouring connections."""
from dataclasses import dataclass
import json

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.electrical import ElectricalModel, LineKind, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import (
    EditorCommandError, NodeTarget, PhysicalLineInput, ProjectEditorController,
)
from rza_calc.editor.state import EditorMode, UNPLACED_EXTENSION_KEY
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict


MAIN = PageId("page.cleanup.main")
OTHER = PageId("page.cleanup.other")
U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller():
    return ProjectEditorController(_Project(
        ElectricalModel.with_builtins("Cleanup"),
        DiagramDocument.create("Cleanup", (
            DiagramPage(MAIN, "Main"), DiagramPage(OTHER, "Other"),
        )),
        ProjectCatalogSnapshots(),
    ))


def _node(controller, *, symbol="electrical_node", name="Point", x=100):
    return controller.add_electrical_node(
        name, page_id=MAIN, x=x, y=100, symbol_key=symbol, voltage_class_id=U10,
    )


def _load(controller, name="A", x=0):
    return controller.add_equipment(
        "builtin.load", name, page_id=MAIN, x=x, y=0,
        voltage_class_by_group={"main": U10},
    )


def _wire(controller, apparatus, point, *, page=MAIN, node_rep=None):
    return controller.connect_port_to_node(
        apparatus.port_ids[0], point.node_id, page_id=page,
        source_representation_id=apparatus.representation_id,
        node_representation_id=node_rep or point.representation_id,
    )


def _integrity(controller):
    assert not controller.model.validate_integrity()
    assert not controller.diagram.validate_targets(controller.model)


@pytest.mark.parametrize("command", ("apparatus", "wire"))
def test_last_connection_delete_removes_only_its_orphan_point(command):
    controller = _controller()
    point = _node(controller)
    untouched = _node(controller, name="Independent unfinished point", x=400)
    apparatus = _load(controller)
    wire = _wire(controller, apparatus, point)
    before_ports = dict(controller.model.ports)
    history = len(controller.journal)

    if command == "apparatus":
        result = controller.delete_from_project((apparatus.representation_id,))
    else:
        result = controller.delete_diagram_route(wire.route_id)
        assert dict(controller.model.ports) == before_ports
        assert apparatus.representation_id in controller.diagram.representations

    assert point.node_id not in controller.model.electrical_nodes
    assert point.representation_id not in controller.diagram.representations
    assert point.node_id in result.node_ids
    assert point.representation_id in result.representation_ids
    assert wire.route_id in result.route_ids
    assert untouched.node_id in controller.model.electrical_nodes
    assert untouched.representation_id in controller.diagram.representations
    assert len(controller.journal) == history + 1
    _integrity(controller)


@pytest.mark.parametrize("command", ("apparatus", "wire", "page"))
def test_degree_one_tail_disappears_but_neighbour_port_node_connection_survive(command):
    controller = _controller()
    point = _node(controller)
    first, neighbour = _load(controller), _load(controller, "B", 200)
    first_wire, neighbour_wire = _wire(controller, first, point), _wire(controller, neighbour, point)
    surviving_connection = controller.model.connection_for_port(neighbour.port_ids[0])
    surviving_node = controller.model.electrical_nodes[point.node_id]
    surviving_ports = {key: controller.model.ports[key] for key in neighbour.port_ids}
    before_fingerprint = electrical_model_fingerprint(controller.model)
    if command == "apparatus":
        result = controller.delete_from_project((first.representation_id,))
    elif command == "wire":
        result = controller.delete_diagram_route(first_wire.route_id)
    else:
        result = controller.remove_from_page((first.representation_id,), mark_as_unplaced=True)
        assert electrical_model_fingerprint(controller.model) == before_fingerprint

    assert neighbour.representation_id in controller.diagram.representations
    assert controller.model.connection_for_port(neighbour.port_ids[0]) == surviving_connection
    assert controller.model.electrical_nodes[point.node_id] == surviving_node
    assert {key: controller.model.ports[key] for key in neighbour.port_ids} == surviving_ports
    assert point.node_id not in result.node_ids
    assert point.representation_id in result.representation_ids
    assert neighbour_wire.route_id in result.route_ids
    assert not controller.diagram.routes
    assert point.node_id.value in controller.diagram.extensions[UNPLACED_EXTENSION_KEY]
    _integrity(controller)


def test_shared_point_with_two_surviving_neighbours_is_not_a_tail():
    controller = _controller()
    point = _node(controller)
    apparatus = [_load(controller, str(index), index * 200) for index in range(3)]
    wires = [_wire(controller, item, point) for item in apparatus]
    keep = {key: value for key, value in controller.model.connections.items()
            if value.port_id not in apparatus[0].port_ids}

    controller.delete_from_project((apparatus[0].representation_id,))

    assert dict(controller.model.connections) == keep
    assert point.representation_id in controller.diagram.representations
    assert all(wire.route_id in controller.diagram.routes for wire in wires[1:])
    _integrity(controller)


@pytest.mark.parametrize("command", ("apparatus", "wire", "page"))
def test_other_page_point_representation_prevents_automatic_cleanup(command):
    controller = _controller()
    point, apparatus = _node(controller), _load(controller)
    wire = _wire(controller, apparatus, point)
    other = controller.place_existing_node(point.node_id, page_id=OTHER, x=40, y=40)
    before = controller.diagram.representations[other]

    if command == "apparatus":
        controller.delete_from_project((apparatus.representation_id,))
    elif command == "wire":
        controller.delete_diagram_route(wire.route_id)
    else:
        controller.remove_from_page((apparatus.representation_id,), mark_as_unplaced=True)

    assert point.node_id in controller.model.electrical_nodes
    assert controller.diagram.representations[other] == before
    assert point.representation_id in controller.diagram.representations
    _integrity(controller)


@pytest.mark.parametrize("command", ("apparatus", "wire", "page"))
def test_busbar_is_not_an_orphan_point(command):
    controller = _controller()
    bus, apparatus = _node(controller, symbol="busbar"), _load(controller)
    wire = _wire(controller, apparatus, bus)
    before = controller.diagram.representations[bus.representation_id]
    if command == "apparatus":
        controller.delete_from_project((apparatus.representation_id,))
    elif command == "wire":
        controller.delete_diagram_route(wire.route_id)
    else:
        controller.remove_from_page((apparatus.representation_id,), mark_as_unplaced=True)
    assert bus.node_id in controller.model.electrical_nodes
    assert controller.diagram.representations[bus.representation_id] == before
    _integrity(controller)


def test_delete_physical_line_cleans_free_endpoints_not_unrelated_nodes():
    controller = _controller()
    start, end = _node(controller), _node(controller, x=500)
    untouched = _node(controller, x=800)
    line = controller.create_physical_line(
        "ВЛ", LineKind.OVERHEAD, NodeTarget(start.node_id), NodeTarget(end.node_id),
        page_id=MAIN, physical=PhysicalLineInput(1_000_000),
    )
    result = controller.delete_diagram_route(line.route_id)
    assert {start.node_id, end.node_id} <= set(result.node_ids)
    assert untouched.node_id in controller.model.electrical_nodes
    assert not controller.model.logical_lines
    assert not controller.model.line_sections
    assert not controller.diagram.routes
    _integrity(controller)


def test_surviving_physical_line_is_not_removed_as_degree_one_tail():
    controller = _controller()
    start, end = _node(controller), _node(controller, x=500)
    apparatus = _load(controller)
    _wire(controller, apparatus, start)
    line = controller.create_physical_line(
        "ВЛ", LineKind.OVERHEAD, NodeTarget(start.node_id), NodeTarget(end.node_id),
        page_id=MAIN, physical=PhysicalLineInput(1_000_000),
    )
    saved_route = controller.diagram.routes[line.route_id]
    controller.delete_from_project((apparatus.representation_id,))
    assert controller.diagram.routes[line.route_id] == saved_route
    assert start.representation_id in controller.diagram.representations
    assert end.representation_id in controller.diagram.representations
    _integrity(controller)


def test_remove_from_page_keeps_all_electrical_content_even_for_last_point():
    controller = _controller()
    point, apparatus = _node(controller), _load(controller)
    _wire(controller, apparatus, point)
    before = electrical_model_to_dict(controller.model)
    controller.remove_from_page((apparatus.representation_id,), mark_as_unplaced=True)
    assert electrical_model_to_dict(controller.model) == before
    assert point.representation_id not in controller.diagram.representations
    assert {point.node_id.value, apparatus.equipment_id.value} <= set(
        controller.diagram.extensions[UNPLACED_EXTENSION_KEY])
    _integrity(controller)


def test_reconnect_cleans_old_free_point_but_keeps_new_target_and_port_ids():
    controller = _controller()
    old, target, apparatus = _node(controller), _node(controller, x=500), _load(controller)
    old_wire = _wire(controller, apparatus, old)
    ports_before = dict(controller.model.ports)
    moved = controller.reconnect_port(
        apparatus.port_ids[0], NodeTarget(target.node_id, representation_id=target.representation_id),
        page_id=MAIN, source_representation_id=apparatus.representation_id,
    )
    assert old.node_id not in controller.model.electrical_nodes
    assert old.representation_id not in controller.diagram.representations
    assert old_wire.route_id not in controller.diagram.routes
    assert moved.route_id in controller.diagram.routes
    assert target.representation_id in controller.diagram.representations
    assert dict(controller.model.ports) == ports_before
    assert controller.model.connection_for_port(apparatus.port_ids[0]).electrical_node_id == target.node_id
    controller.undo()
    assert old.node_id in controller.model.electrical_nodes
    assert old_wire.route_id in controller.diagram.routes
    _integrity(controller)


@pytest.mark.parametrize("degree", (1, 2))
def test_cleanup_undo_redo_and_json_roundtrip_preserve_ids_and_unplaced_marker(degree):
    controller = _controller()
    point = _node(controller)
    apparatus = [_load(controller, str(index), index * 200) for index in range(degree)]
    for item in apparatus:
        _wire(controller, item, point)
    before_model = electrical_model_fingerprint(controller.model)
    before_diagram = controller.diagram
    controller.delete_from_project((apparatus[0].representation_id,))
    after_model = electrical_model_fingerprint(controller.model)
    after_diagram = controller.diagram
    controller.undo()
    assert electrical_model_fingerprint(controller.model) == before_model
    assert controller.diagram.representations == before_diagram.representations
    assert controller.diagram.routes == before_diagram.routes
    controller.redo()
    assert electrical_model_fingerprint(controller.model) == after_model
    assert controller.diagram.representations == after_diagram.representations
    assert controller.diagram.routes == after_diagram.routes
    restored_model = electrical_model_from_dict(json.loads(json.dumps(electrical_model_to_dict(controller.model))))
    restored_diagram = diagram_from_dict(json.loads(json.dumps(diagram_to_dict(controller.diagram))), restored_model)
    assert electrical_model_fingerprint(restored_model) == after_model
    assert restored_diagram == controller.diagram
    _integrity(controller)


@pytest.mark.parametrize("command", ("apparatus", "wire", "page"))
def test_analysis_mode_rejects_delete_before_any_cleanup(command):
    controller = _controller()
    point, apparatus = _node(controller), _load(controller)
    wire = _wire(controller, apparatus, point)
    controller.set_mode(EditorMode.ANALYSIS)
    before_model, before_diagram = electrical_model_fingerprint(controller.model), controller.diagram
    with pytest.raises(EditorCommandError):
        if command == "apparatus":
            controller.delete_from_project((apparatus.representation_id,))
        elif command == "wire":
            controller.delete_diagram_route(wire.route_id)
        else:
            controller.remove_from_page((apparatus.representation_id,), mark_as_unplaced=True)
    assert electrical_model_fingerprint(controller.model) == before_model
    assert controller.diagram == before_diagram
