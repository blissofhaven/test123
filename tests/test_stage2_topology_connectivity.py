# -*- coding: utf-8 -*-
"""Stage 2 acceptance tests for active electrical connectivity.

These tests deliberately construct only the canonical Domain Model.  They do
not use SVG coordinates, the legacy calculation network or a synthetic GRID
node.  Assertions use stable domain IDs so a topology result can always be
traced back to the user's equipment.
"""
from __future__ import annotations

import unittest

from rza_calc.domain.electrical import (
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.topology import DiagnosticSeverity, TopologyEngine


VOLTAGE_10_KV = VoltageClassId("builtin.voltage.ac.10kv")
_ASSERT = unittest.TestCase()


def _model(name: str) -> ElectricalModel:
    return ElectricalModel.with_builtins(name)


def _node(
    model: ElectricalModel,
    token: str,
    voltage_id: VoltageClassId | None = VOLTAGE_10_KV,
) -> ElectricalNode:
    result = ElectricalNode(
        ElectricalNodeId(f"node.{token}"),
        token,
        declared_voltage_class_id=voltage_id,
    )
    model.add_node(result)
    return result


def _connect(
    model: ElectricalModel,
    equipment_id: EquipmentId,
    role: str,
    node_id: ElectricalNodeId,
) -> None:
    model.connect_port(
        model.port_by_role(equipment_id, role).id,
        node_id,
        connection_id=ConnectionId(f"connection.{equipment_id.value}.{role}"),
    )


def _source(
    model: ElectricalModel,
    token: str,
    node: ElectricalNode,
    *,
    generator: bool = False,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.{token}")
    role = "terminal"
    model.create_equipment(
        "builtin.generator" if generator else "builtin.external_grid",
        token,
        equipment_id=equipment_id,
        port_ids_by_role={role: PortId(f"port.{token}.{role}")},
        voltage_class_by_group={"main": VOLTAGE_10_KV},
    )
    _connect(model, equipment_id, role, node.id)
    return equipment_id


def _two_terminal(
    model: ElectricalModel,
    token: str,
    first: ElectricalNode,
    second: ElectricalNode,
    *,
    type_id: str = "builtin.line",
    roles: tuple[str, str] = ("from", "to"),
    position: SwitchPosition | None = None,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.{token}")
    properties = None
    if type_id == "builtin.recloser":
        properties = {
            "manufacturer": "Acceptance",
            "model": "R-10",
            "rated_voltage_v": 10_000,
            "rated_current_a": 630,
        }
    model.create_equipment(
        type_id,
        token,
        equipment_id=equipment_id,
        port_ids_by_role={
            role: PortId(f"port.{token}.{role}") for role in roles
        },
        properties=properties,
        voltage_class_by_group={"main": VOLTAGE_10_KV},
        normal_position=position,
    )
    _connect(model, equipment_id, roles[0], first.id)
    _connect(model, equipment_id, roles[1], second.id)
    return equipment_id


def _state(
    model: ElectricalModel,
    token: str,
    positions: dict[EquipmentId, SwitchPosition] | None = None,
) -> OperatingStateId:
    state_id = OperatingStateId(f"state.{token}")
    model.add_operating_state(
        OperatingState(state_id, token, positions or {})
    )
    return state_id


def _component(snapshot, node_id: ElectricalNodeId):
    return snapshot.components[snapshot.component_by_node[node_id]]


def _equipment_ids(rows) -> tuple[EquipmentId, ...]:
    return tuple(row.equipment_id for row in rows)


def _path_equipment_ids(path) -> tuple[EquipmentId, ...]:
    _ASSERT.assertIsNotNone(path)
    return path.equipment_ids


def test_radial_switch_closed_and_open_change_only_active_connectivity() -> None:
    model = _model("radial switch")
    source_bus = _node(model, "source")
    before_switch = _node(model, "before_switch")
    load_bus = _node(model, "load")
    source_id = _source(model, "source.primary", source_bus)
    line_id = _two_terminal(model, "line.incoming", source_bus, before_switch)
    switch_id = _two_terminal(
        model,
        "switch.feeder",
        before_switch,
        load_bus,
        type_id="builtin.circuit_breaker",
        roles=("a", "b"),
        position=SwitchPosition.CLOSED,
    )
    closed_id = _state(model, "closed", {switch_id: SwitchPosition.CLOSED})
    open_id = _state(model, "open", {switch_id: SwitchPosition.OPEN})
    engine = TopologyEngine()

    closed = engine.compile(model, closed_id)
    opened = engine.compile(model, open_id)
    switch_link_id = opened.link_ids_by_equipment[switch_id][0]

    _ASSERT.assertTrue(closed.is_valid)
    _ASSERT.assertEqual(
        closed.component_by_node[source_bus.id],
        closed.component_by_node[load_bus.id],
    )
    _ASSERT.assertEqual(closed.sources_for(load_bus.id), (source_id,))
    _ASSERT.assertTrue(closed.is_energized(load_bus.id))
    _ASSERT.assertEqual(
        _path_equipment_ids(closed.find_path(source_bus.id, load_bus.id)),
        (line_id, switch_id),
    )
    closed_component = closed.component_of(source_bus.id)
    _ASSERT.assertIn(switch_link_id, closed_component.link_ids)
    _ASSERT.assertIn(switch_id, closed_component.equipment_ids)

    _ASSERT.assertNotEqual(
        opened.component_by_node[source_bus.id],
        opened.component_by_node[load_bus.id],
    )
    _ASSERT.assertEqual(opened.sources_for(load_bus.id), ())
    _ASSERT.assertFalse(opened.is_energized(load_bus.id))
    _ASSERT.assertIsNone(opened.find_path(source_bus.id, load_bus.id))
    switch_links = opened.links_between(before_switch.id, load_bus.id)
    _ASSERT.assertEqual(_equipment_ids(switch_links), (switch_id,))
    _ASSERT.assertFalse(switch_links[0].active)
    for node_id in (before_switch.id, load_bus.id):
        component = opened.component_of(node_id)
        _ASSERT.assertNotIn(switch_link_id, component.link_ids)
        _ASSERT.assertNotIn(switch_id, component.equipment_ids)


def test_recloser_uses_the_same_graph_rules_as_a_real_switch() -> None:
    model = _model("recloser")
    upstream = _node(model, "upstream")
    downstream = _node(model, "downstream")
    source_id = _source(model, "source.primary", upstream)
    recloser_id = _two_terminal(
        model,
        "recloser.branch",
        upstream,
        downstream,
        type_id="builtin.recloser",
        roles=("a", "b"),
        position=SwitchPosition.CLOSED,
    )
    open_id = _state(model, "recloser_open", {recloser_id: SwitchPosition.OPEN})
    closed_id = _state(
        model, "recloser_closed", {recloser_id: SwitchPosition.CLOSED}
    )
    engine = TopologyEngine()

    opened = engine.compile(model, open_id)
    closed = engine.compile(model, closed_id)

    _ASSERT.assertEqual(opened.sources_for(downstream.id), ())
    _ASSERT.assertFalse(opened.is_energized(downstream.id))
    _ASSERT.assertEqual(closed.sources_for(downstream.id), (source_id,))
    _ASSERT.assertTrue(closed.is_energized(downstream.id))
    _ASSERT.assertEqual(
        _equipment_ids(closed.links_between(upstream.id, downstream.id)),
        (recloser_id,),
    )


def test_one_electrical_node_accepts_arbitrary_independent_branches() -> None:
    model = _model("arbitrary junction")
    hub = _node(model, "hub")
    source_id = _source(model, "source.hub", hub)
    endpoints = [_node(model, f"end_{index}") for index in range(5)]
    line_ids = tuple(
        _two_terminal(model, f"line.spoke_{index}", hub, endpoint)
        for index, endpoint in enumerate(endpoints)
    )
    snapshot = TopologyEngine().compile(model, _state(model, "base"))

    _ASSERT.assertTrue(snapshot.is_valid)
    _ASSERT.assertEqual(
        set(_equipment_ids(snapshot.neighbors(hub.id))), set(line_ids)
    )
    for endpoint in endpoints:
        _ASSERT.assertEqual(snapshot.sources_for(endpoint.id), (source_id,))
        _ASSERT.assertEqual(
            snapshot.component_by_node[hub.id],
            snapshot.component_by_node[endpoint.id],
        )


def test_ring_preserves_supply_over_alternate_path_and_detects_cycle() -> None:
    model = _model("ring")
    first = _node(model, "ring_a")
    second = _node(model, "ring_b")
    third = _node(model, "ring_c")
    source_id = _source(model, "source.ring", first)
    recloser_id = _two_terminal(
        model,
        "recloser.ring",
        first,
        second,
        type_id="builtin.recloser",
        roles=("a", "b"),
        position=SwitchPosition.CLOSED,
    )
    alternate_one = _two_terminal(model, "line.ring_ac", first, third)
    alternate_two = _two_terminal(model, "line.ring_cb", third, second)
    open_id = _state(model, "ring_open", {recloser_id: SwitchPosition.OPEN})
    closed_id = _state(model, "ring_closed", {recloser_id: SwitchPosition.CLOSED})
    engine = TopologyEngine()

    opened = engine.compile(model, open_id)
    closed = engine.compile(model, closed_id)

    _ASSERT.assertEqual(opened.sources_for(second.id), (source_id,))
    _ASSERT.assertEqual(
        _path_equipment_ids(opened.find_path(first.id, second.id)),
        (alternate_one, alternate_two),
    )
    _ASSERT.assertFalse(_component(opened, first.id).is_meshed)
    _ASSERT.assertTrue(_component(closed, first.id).is_meshed)


def test_parallel_lines_are_not_collapsed_in_neighbors_or_links_between() -> None:
    model = _model("parallel")
    first = _node(model, "parallel_a")
    second = _node(model, "parallel_b")
    first_line = _two_terminal(model, "line.parallel_1", first, second)
    second_line = _two_terminal(model, "line.parallel_2", first, second)
    snapshot = TopologyEngine().compile(model, _state(model, "base"))

    _ASSERT.assertEqual(
        set(_equipment_ids(snapshot.links_between(first.id, second.id))),
        {first_line, second_line},
    )
    _ASSERT.assertEqual(
        set(_equipment_ids(snapshot.neighbors(first.id))),
        {first_line, second_line},
    )
    _ASSERT.assertTrue(_component(snapshot, first.id).is_meshed)


def test_two_independent_sources_remain_exact_and_never_become_grid() -> None:
    model = _model("independent sources")
    left = _node(model, "left")
    right = _node(model, "right")
    left_source = _source(model, "source.left", left)
    right_source = _source(model, "source.right", right)
    snapshot = TopologyEngine().compile(model, _state(model, "base"))

    _ASSERT.assertNotEqual(
        snapshot.component_by_node[left.id], snapshot.component_by_node[right.id]
    )
    _ASSERT.assertEqual(snapshot.sources_for(left.id), (left_source,))
    _ASSERT.assertEqual(snapshot.sources_for(right.id), (right_source,))
    actual_ids = snapshot.sources_for(left.id) + snapshot.sources_for(right.id)
    _ASSERT.assertEqual(set(actual_ids), {left_source, right_source})
    _ASSERT.assertNotIn("GRID", {item.value for item in actual_ids})


def test_open_switch_can_have_independently_energized_sources_on_both_sides() -> None:
    model = _model("open point between sources")
    left = _node(model, "left_source_bus")
    right = _node(model, "right_source_bus")
    left_source = _source(model, "source.left", left)
    right_source = _source(model, "source.right", right)
    switch_id = _two_terminal(
        model,
        "switch.open_point",
        left,
        right,
        type_id="builtin.circuit_breaker",
        roles=("a", "b"),
        position=SwitchPosition.OPEN,
    )
    open_id = _state(model, "open", {switch_id: SwitchPosition.OPEN})
    closed_id = _state(model, "closed", {switch_id: SwitchPosition.CLOSED})
    engine = TopologyEngine()

    opened = engine.compile(model, open_id)
    closed = engine.compile(model, closed_id)

    _ASSERT.assertTrue(opened.is_energized(left.id))
    _ASSERT.assertTrue(opened.is_energized(right.id))
    _ASSERT.assertEqual(opened.sources_for(left.id), (left_source,))
    _ASSERT.assertEqual(opened.sources_for(right.id), (right_source,))
    _ASSERT.assertNotEqual(
        opened.component_by_node[left.id], opened.component_by_node[right.id]
    )
    _ASSERT.assertEqual(
        set(closed.sources_for(left.id)), {left_source, right_source}
    )
    _ASSERT.assertEqual(
        closed.component_by_node[left.id], closed.component_by_node[right.id]
    )


def test_local_generator_energizes_its_island_without_an_external_grid() -> None:
    model = _model("generator island")
    generator_bus = _node(model, "generator_bus")
    load_bus = _node(model, "generator_load")
    generator_id = _source(model, "generator.local", generator_bus, generator=True)
    _two_terminal(model, "line.generator_feeder", generator_bus, load_bus)
    snapshot = TopologyEngine().compile(model, _state(model, "base"))

    _ASSERT.assertTrue(snapshot.is_energized(load_bus.id))
    _ASSERT.assertEqual(snapshot.sources_for(load_bus.id), (generator_id,))
    _ASSERT.assertNotIn(
        "GRID", {item.value for item in snapshot.sources_for(load_bus.id)}
    )


def test_insertion_order_does_not_change_the_semantic_snapshot() -> None:
    def build(reverse: bool) -> tuple[ElectricalModel, OperatingStateId]:
        model = _model(f"deterministic {reverse}")
        tokens = ("a", "b", "c")
        if reverse:
            tokens = tuple(reversed(tokens))
        nodes = {token: _node(model, token) for token in tokens}
        source_id = _source(model, "source.primary", nodes["a"])
        del source_id
        edges = (
            ("line.ab", nodes["a"], nodes["b"]),
            ("line.bc", nodes["b"], nodes["c"]),
        )
        if reverse:
            edges = tuple(reversed(edges))
        for token, first, second in edges:
            _two_terminal(model, token, first, second)
        return model, _state(model, "base")

    first_model, first_state = build(False)
    second_model, second_state = build(True)
    engine = TopologyEngine()

    first = engine.compile(first_model, first_state)
    second = engine.compile(second_model, second_state)

    _ASSERT.assertEqual(first.semantic_signature(), second.semantic_signature())


def test_diagnostics_use_typed_severity_values() -> None:
    model = _model("diagnostic severity")
    isolated = _node(model, "isolated")
    snapshot = TopologyEngine().compile(model, _state(model, "base"))

    diagnostics = [
        item
        for item in snapshot.diagnostics
        if item.code == "component_without_source"
        and item.electrical_node_id == isolated.id
    ]
    _ASSERT.assertTrue(diagnostics)
    _ASSERT.assertIs(diagnostics[0].severity, DiagnosticSeverity.WARNING)
