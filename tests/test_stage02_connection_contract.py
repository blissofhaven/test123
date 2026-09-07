"""Accepted connection contract: exact drawing coordinates and safe voltage adoption.

Ten drawing units is the collision threshold, twenty is reserved for the
explicit separation command. Geometry never changes electrical identity.
"""
from __future__ import annotations

import json
import math

import pytest

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor import NodeTarget
from rza_calc.editor.connection_voltage import endpoint_voltage
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict
from test_bus_connection_spacing import PAGE, U10, _bus_point, _connect, _geometry, _load, _setup
from test_editor_voltage_guard import U110, _reject, _saved


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
@pytest.mark.parametrize("requested_offset,expected_offset", ((2.4, 10.0), (9.9, 10.0), (10.0, 10.0), (12.0, 12.0), (19.0, 19.0)))
def test_bus_attachment_uses_exact_threshold_and_smallest_required_displacement(rotation, requested_offset, expected_offset):
    controller, bus = _setup(width=240, rotation=rotation)
    first = _connect(controller, bus, _load(controller, 150), .5)
    stable = controller.diagram.routes[first.route_id]
    apparatus = _load(controller, 650)
    second = _connect(controller, bus, apparatus, .5 + requested_offset / 240)
    route = controller.diagram.routes[second.route_id]
    expected_fraction = .5 + expected_offset / 240
    expected_point = _bus_point(controller, bus, expected_fraction)
    assert float(route.end_anchor.anchor_key) == pytest.approx(expected_fraction, abs=1e-12)
    assert (route.waypoints[-1].x, route.waypoints[-1].y) == pytest.approx(expected_point, abs=1e-9)
    assert math.hypot(route.waypoints[-1].x - stable.waypoints[-1].x,
                      route.waypoints[-1].y - stable.waypoints[-1].y) == pytest.approx(expected_offset, abs=1e-9)
    assert controller.diagram.routes[first.route_id] == stable
    assert route.start_anchor.target_port_id == apparatus.port_ids[0]
    assert route.end_anchor.electrical_node_id == bus.node_id
    assert controller.model.connection_for_port(apparatus.port_ids[0]).electrical_node_id == bus.node_id


def test_moving_bus_attachment_preserves_complete_model_and_route_identity_through_history_and_json():
    controller, bus = _setup(width=240)
    first = _connect(controller, bus, _load(controller, 150), .3)
    second = _connect(controller, bus, _load(controller, 650), .35)
    before_model = electrical_model_to_dict(controller.model)
    fingerprint = electrical_model_fingerprint(controller.model)
    signature = controller.model.connectivity_signature()
    before_routes = dict(controller.diagram.routes)
    journal = len(controller.journal)
    preview = controller.preview_bus_attachment_move(first.route_id, at_start=False, fraction=.6)
    assert electrical_model_to_dict(controller.model) == before_model
    assert dict(controller.diagram.routes) == before_routes
    assert len(controller.journal) == journal
    assert len(preview) == 1 and preview[0].id == first.route_id
    assert controller.move_bus_attachment(first.route_id, at_start=False, fraction=.6) == (first.route_id,)
    moved = controller.diagram.routes[first.route_id]
    assert _geometry(moved) == _geometry(preview[0])
    assert moved.start_anchor == preview[0].start_anchor and moved.end_anchor == preview[0].end_anchor
    assert moved.start_anchor == before_routes[first.route_id].start_anchor
    assert moved.end_anchor.electrical_node_id == before_routes[first.route_id].end_anchor.electrical_node_id
    assert controller.diagram.routes[second.route_id] == before_routes[second.route_id]
    assert set(controller.diagram.routes) == set(before_routes)
    assert electrical_model_to_dict(controller.model) == before_model
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert controller.model.connectivity_signature() == signature
    assert len(controller.journal) == journal + 1
    after_routes = dict(controller.diagram.routes)
    controller.undo()
    assert dict(controller.diagram.routes) == before_routes
    assert electrical_model_to_dict(controller.model) == before_model
    controller.redo()
    assert dict(controller.diagram.routes) == after_routes
    restored_model = electrical_model_from_dict(json.loads(json.dumps(electrical_model_to_dict(controller.model))))
    restored_diagram = diagram_from_dict(json.loads(json.dumps(diagram_to_dict(controller.diagram))), restored_model)
    assert restored_diagram == controller.diagram
    assert electrical_model_fingerprint(restored_model) == fingerprint
    assert restored_model.connectivity_signature() == signature


def test_known_bus_adoption_affects_only_the_selected_transformer_winding_and_roundtrips():
    controller, _ = _setup()
    transformer = controller.add_equipment("builtin.transformer_2w", "T", page_id=PAGE, x=800)
    high = controller.add_electrical_node("HV", page_id=PAGE, x=800, y=200, voltage_class_id=U110)
    hv = controller.model.port_by_role(transformer.equipment_id, "hv").id
    lv = controller.model.port_by_role(transformer.equipment_id, "lv").id
    before = _saved(controller)
    check = controller.validate_connection(hv, NodeTarget(high.node_id))
    assert check.valid and check.effective_voltage_id == U110
    assert _saved(controller) == before
    controller.connect_port_to_node(hv, high.node_id)
    assert dict(controller.model.equipment[transformer.equipment_id].voltage_class_by_group) == {"hv": U110}
    assert endpoint_voltage(controller.model, hv).voltage_class_id == U110
    assert not endpoint_voltage(controller.model, lv).valid
    assert controller.model.ports == before[0].ports
    assert len(controller.journal) == len(before[2]) + 1
    after = ElectricalModelMemento.capture(controller.model)
    restored = electrical_model_from_dict(json.loads(json.dumps(electrical_model_to_dict(controller.model))))
    assert restored.ports == controller.model.ports
    assert dict(restored.equipment[transformer.equipment_id].voltage_class_by_group) == {"hv": U110}
    assert electrical_model_fingerprint(restored) == electrical_model_fingerprint(controller.model)
    controller.undo()
    assert ElectricalModelMemento.capture(controller.model) == before[0]
    controller.redo()
    assert ElectricalModelMemento.capture(controller.model) == after


def test_connected_unknown_declaration_must_not_adopt_a_conflicting_peer():
    controller, known10 = _setup()
    apparatus = controller.add_equipment("builtin.circuit_breaker", "Imported Q", page_id=PAGE, x=800)
    # A legacy imported group may be undeclared but electrically resolved.
    controller.model.connect_port(apparatus.port_ids[0], known10.node_id)
    controller = ProjectEditorController(controller._project)
    known110 = controller.add_electrical_node("HV", page_id=PAGE, x=900, voltage_class_id=U110)
    free = apparatus.port_ids[1]
    before = _saved(controller)
    check = controller.validate_connection(free, NodeTarget(known110.node_id))
    assert not check.valid
    assert _saved(controller) == before
    _reject(controller, lambda: controller.connect_port_to_node(free, known110.node_id))
    assert dict(controller.model.equipment[apparatus.equipment_id].voltage_class_by_group) == {}
    assert endpoint_voltage(controller.model, free).voltage_class_id == U10


def test_voltage_preview_allows_an_occupied_same_node_without_reconnecting_it():
    controller, bus = _setup()
    apparatus = _load(controller, 650)
    _connect(controller, bus, apparatus)
    before = _saved(controller)
    connection = controller.model.connection_for_port(apparatus.port_ids[0])
    check = controller.preview_connection_voltage(apparatus.port_ids[0], bus.node_id)
    assert check.valid and check.voltage_class_id == U10
    assert _saved(controller) == before
    assert controller.model.connection_for_port(apparatus.port_ids[0]) == connection


@pytest.mark.parametrize("reverse", (False, True))
def test_voltage_preview_can_propose_adoption_without_assigning_the_live_group(reverse):
    controller, bus = _setup()
    apparatus = controller.add_equipment("builtin.circuit_breaker", "Unassigned Q", page_id=PAGE, x=800)
    port = apparatus.port_ids[0]
    endpoints = (bus.node_id, port) if reverse else (port, bus.node_id)
    before = _saved(controller)
    for _ in range(3):
        check = controller.preview_connection_voltage(*endpoints)
        assert check.valid and check.voltage_class_id == U10
        assert _saved(controller) == before
    assert dict(controller.model.equipment[apparatus.equipment_id].voltage_class_by_group) == {}
    assert controller.model.connection_for_port(port) is None
    assert not endpoint_voltage(controller.model, port).valid


@pytest.mark.parametrize("imported_conflict", (False, True))
def test_voltage_preview_keeps_real_conflicts_rejected_without_relabelling(imported_conflict):
    controller, bus = _setup()
    other = controller.add_electrical_node("110 kV", page_id=PAGE, x=800, voltage_class_id=U110)
    if imported_conflict:
        line = controller.add_equipment("builtin.line", "Conflicting import", page_id=PAGE, x=650)
        controller.model.connect_port(line.port_ids[0], bus.node_id)
        controller.model.connect_port(line.port_ids[1], other.node_id)
        controller = ProjectEditorController(controller._project)
        endpoints = (bus.node_id, U10)
    else:
        endpoints = (bus.node_id, other.node_id)
    before = _saved(controller)
    check = controller.preview_connection_voltage(*endpoints)
    assert not check.valid and "напряжени" in check.message
    if imported_conflict:
        assert "противореч" in check.message
    assert _saved(controller) == before
