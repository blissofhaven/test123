# -*- coding: utf-8 -*-
"""Stage 2 acceptance tests for voltage zones and blocking diagnostics."""
from __future__ import annotations

import unittest

from rza_calc.domain.electrical import (
    AC_POWER,
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    OperatingState,
    OperatingStateId,
    PortDefinition,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.topology import DiagnosticSeverity, TopologyEngine, VoltageStatus


VOLTAGE_110_KV = VoltageClassId("builtin.voltage.ac.110kv")
VOLTAGE_35_KV = VoltageClassId("builtin.voltage.ac.35kv")
VOLTAGE_10_KV = VoltageClassId("builtin.voltage.ac.10kv")
VOLTAGE_6_KV = VoltageClassId("builtin.voltage.ac.6kv")
_ASSERT = unittest.TestCase()


def _model(name: str) -> ElectricalModel:
    return ElectricalModel.with_builtins(name)


def _node(
    model: ElectricalModel,
    token: str,
    voltage_id: VoltageClassId | None,
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
    voltage_id: VoltageClassId,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.{token}")
    model.create_equipment(
        "builtin.external_grid",
        token,
        equipment_id=equipment_id,
        port_ids_by_role={"terminal": PortId(f"port.{token}.terminal")},
        voltage_class_by_group={"main": voltage_id},
    )
    _connect(model, equipment_id, "terminal", node.id)
    return equipment_id


def _state(
    model: ElectricalModel,
    token: str = "base",
    positions: dict[EquipmentId, SwitchPosition] | None = None,
) -> OperatingStateId:
    state_id = OperatingStateId(f"state.{token}")
    model.add_operating_state(OperatingState(state_id, token, positions or {}))
    return state_id


def _line(
    model: ElectricalModel,
    token: str,
    first: ElectricalNode,
    second: ElectricalNode,
    *,
    voltage_id: VoltageClassId | None,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.{token}")
    model.create_equipment(
        "builtin.line",
        token,
        equipment_id=equipment_id,
        port_ids_by_role={
            "from": PortId(f"port.{token}.from"),
            "to": PortId(f"port.{token}.to"),
        },
        voltage_class_by_group=(
            {"main": voltage_id} if voltage_id is not None else {}
        ),
    )
    _connect(model, equipment_id, "from", first.id)
    _connect(model, equipment_id, "to", second.id)
    return equipment_id


def _resolution(snapshot, node: ElectricalNode):
    return snapshot.voltage_by_node[node.id]


def _assert_voltage(snapshot, node: ElectricalNode, voltage_id: VoltageClassId) -> None:
    result = _resolution(snapshot, node)
    _ASSERT.assertIs(result.status, VoltageStatus.RESOLVED)
    _ASSERT.assertEqual(result.voltage_class_id, voltage_id)
    _ASSERT.assertEqual(result.voltage_class_ids, (voltage_id,))


def _diagnostics(snapshot, code: str):
    return tuple(item for item in snapshot.diagnostics if item.code == code)


def test_transformer_2w_is_one_active_component_but_two_voltage_zones() -> None:
    model = _model("two winding transformer")
    hv = _node(model, "transformer_hv", VOLTAGE_110_KV)
    lv = _node(model, "transformer_lv", VOLTAGE_10_KV)
    source_id = _source(model, "source.110kv", hv, VOLTAGE_110_KV)
    transformer_id = EquipmentId("equipment.transformer_2w")
    model.create_equipment(
        "builtin.transformer_2w",
        "T1",
        equipment_id=transformer_id,
        port_ids_by_role={
            "hv": PortId("port.transformer_2w.hv"),
            "lv": PortId("port.transformer_2w.lv"),
        },
        voltage_class_by_group={"hv": VOLTAGE_110_KV, "lv": VOLTAGE_10_KV},
    )
    _connect(model, transformer_id, "hv", hv.id)
    _connect(model, transformer_id, "lv", lv.id)
    snapshot = TopologyEngine().compile(model, _state(model))

    _ASSERT.assertTrue(snapshot.is_valid)
    _ASSERT.assertEqual(
        snapshot.component_by_node[hv.id], snapshot.component_by_node[lv.id]
    )
    _ASSERT.assertTrue(snapshot.is_energized(lv.id))
    _ASSERT.assertEqual(snapshot.sources_for(lv.id), (source_id,))
    _assert_voltage(snapshot, hv, VOLTAGE_110_KV)
    _assert_voltage(snapshot, lv, VOLTAGE_10_KV)
    _ASSERT.assertEqual(_diagnostics(snapshot, "voltage_conflict"), ())


def test_semantic_signature_preserves_transformer_port_to_side_mapping() -> None:
    def build(*, swapped: bool):
        model = _model("same transformer identity")
        first = _node(model, "signature.first", VOLTAGE_10_KV)
        second = _node(model, "signature.second", VOLTAGE_10_KV)
        transformer_id = EquipmentId("equipment.signature.transformer")
        model.create_equipment(
            "builtin.transformer_2w",
            "T signature",
            equipment_id=transformer_id,
            port_ids_by_role={
                "hv": PortId("port.signature.transformer.hv"),
                "lv": PortId("port.signature.transformer.lv"),
            },
            voltage_class_by_group={
                "hv": VOLTAGE_10_KV,
                "lv": VOLTAGE_10_KV,
            },
        )
        _connect(model, transformer_id, "hv", second.id if swapped else first.id)
        _connect(model, transformer_id, "lv", first.id if swapped else second.id)
        return TopologyEngine().compile(model)

    original = build(swapped=False)
    swapped = build(swapped=True)

    _ASSERT.assertNotEqual(original.port_to_node, swapped.port_to_node)
    _ASSERT.assertNotEqual(original.semantic_signature(), swapped.semantic_signature())
    _ASSERT.assertNotEqual(original.topology_fingerprint, swapped.topology_fingerprint)


def test_transformer_3w_has_star_connectivity_without_a_false_ring() -> None:
    model = _model("three winding transformer")
    hv = _node(model, "transformer3_hv", VOLTAGE_110_KV)
    mv = _node(model, "transformer3_mv", VOLTAGE_35_KV)
    lv = _node(model, "transformer3_lv", VOLTAGE_10_KV)
    source_id = _source(model, "source.110kv", hv, VOLTAGE_110_KV)
    transformer_id = EquipmentId("equipment.transformer_3w")
    model.create_equipment(
        "builtin.transformer_3w",
        "T3",
        equipment_id=transformer_id,
        port_ids_by_role={
            "hv": PortId("port.transformer_3w.hv"),
            "mv": PortId("port.transformer_3w.mv"),
            "lv": PortId("port.transformer_3w.lv"),
        },
        voltage_class_by_group={
            "hv": VOLTAGE_110_KV,
            "mv": VOLTAGE_35_KV,
            "lv": VOLTAGE_10_KV,
        },
    )
    _connect(model, transformer_id, "hv", hv.id)
    _connect(model, transformer_id, "mv", mv.id)
    _connect(model, transformer_id, "lv", lv.id)
    snapshot = TopologyEngine().compile(model, _state(model))

    component_id = snapshot.component_by_node[hv.id]
    _ASSERT.assertEqual(component_id, snapshot.component_by_node[mv.id])
    _ASSERT.assertEqual(component_id, snapshot.component_by_node[lv.id])
    _ASSERT.assertFalse(snapshot.components[component_id].is_meshed)
    _ASSERT.assertEqual(snapshot.sources_for(mv.id), (source_id,))
    _ASSERT.assertEqual(snapshot.sources_for(lv.id), (source_id,))
    _assert_voltage(snapshot, hv, VOLTAGE_110_KV)
    _assert_voltage(snapshot, mv, VOLTAGE_35_KV)
    _assert_voltage(snapshot, lv, VOLTAGE_10_KV)
    _ASSERT.assertEqual(_diagnostics(snapshot, "voltage_conflict"), ())


def test_nominal_voltage_inheritance_remains_across_an_open_switch() -> None:
    model = _model("nominal voltage across open switch")
    upstream = _node(model, "upstream", VOLTAGE_10_KV)
    downstream = _node(model, "downstream", None)
    source_id = _source(model, "source.10kv", upstream, VOLTAGE_10_KV)
    switch_id = EquipmentId("equipment.switch.open")
    model.create_equipment(
        "builtin.circuit_breaker",
        "Open QF",
        equipment_id=switch_id,
        port_ids_by_role={
            "a": PortId("port.switch.open.a"),
            "b": PortId("port.switch.open.b"),
        },
        voltage_class_by_group={"main": VOLTAGE_10_KV},
        normal_position=SwitchPosition.OPEN,
    )
    _connect(model, switch_id, "a", upstream.id)
    _connect(model, switch_id, "b", downstream.id)
    state_id = _state(model, "open", {switch_id: SwitchPosition.OPEN})
    snapshot = TopologyEngine().compile(model, state_id)

    _ASSERT.assertEqual(snapshot.sources_for(upstream.id), (source_id,))
    _ASSERT.assertEqual(snapshot.sources_for(downstream.id), ())
    _ASSERT.assertFalse(snapshot.is_energized(downstream.id))
    _assert_voltage(snapshot, downstream, VOLTAGE_10_KV)


def test_same_voltage_line_reports_a_true_conflict_with_exact_equipment_id() -> None:
    model = _model("same-voltage conflict")
    ten_kv = _node(model, "conflict_10kv", VOLTAGE_10_KV)
    six_kv = _node(model, "conflict_6kv", VOLTAGE_6_KV)
    line_id = _line(
        model,
        "line.conflict",
        ten_kv,
        six_kv,
        voltage_id=None,
    )
    snapshot = TopologyEngine().compile(model, _state(model))

    _ASSERT.assertFalse(snapshot.is_valid)
    for node in (ten_kv, six_kv):
        resolution = _resolution(snapshot, node)
        _ASSERT.assertIs(resolution.status, VoltageStatus.CONFLICT)
        _ASSERT.assertEqual(
            set(resolution.voltage_class_ids),
            {VOLTAGE_10_KV, VOLTAGE_6_KV},
        )
    conflicts = _diagnostics(snapshot, "voltage_conflict")
    _ASSERT.assertTrue(conflicts)
    _ASSERT.assertIs(conflicts[0].severity, DiagnosticSeverity.ERROR)
    _ASSERT.assertEqual(conflicts[0].equipment_id, line_id)


def test_source_less_island_is_preserved_and_reported_as_warning() -> None:
    model = _model("source-less island")
    first = _node(model, "island_a", VOLTAGE_10_KV)
    second = _node(model, "island_b", VOLTAGE_10_KV)
    _line(model, "line.island", first, second, voltage_id=VOLTAGE_10_KV)
    snapshot = TopologyEngine().compile(model, _state(model))

    _ASSERT.assertTrue(snapshot.is_valid)
    _ASSERT.assertEqual(
        snapshot.component_by_node[first.id], snapshot.component_by_node[second.id]
    )
    _ASSERT.assertEqual(snapshot.sources_for(first.id), ())
    _ASSERT.assertFalse(snapshot.is_energized(second.id))
    warnings = _diagnostics(snapshot, "component_without_source")
    _ASSERT.assertTrue(warnings)
    _ASSERT.assertTrue(
        all(item.severity is DiagnosticSeverity.WARNING for item in warnings)
    )


def test_required_unconnected_port_blocks_snapshot_without_a_ghost_link() -> None:
    model = _model("required port")
    connected = _node(model, "connected", VOLTAGE_10_KV)
    line_id = EquipmentId("equipment.incomplete_line")
    missing_port_id = PortId("port.incomplete_line.to")
    model.create_equipment(
        "builtin.line",
        "Incomplete line",
        equipment_id=line_id,
        port_ids_by_role={
            "from": PortId("port.incomplete_line.from"),
            "to": missing_port_id,
        },
        voltage_class_by_group={"main": VOLTAGE_10_KV},
    )
    _connect(model, line_id, "from", connected.id)
    snapshot = TopologyEngine().compile(model, _state(model))

    _ASSERT.assertFalse(snapshot.is_valid)
    diagnostics = _diagnostics(snapshot, "required_port_unconnected")
    _ASSERT.assertTrue(diagnostics)
    _ASSERT.assertIs(diagnostics[0].severity, DiagnosticSeverity.ERROR)
    _ASSERT.assertEqual(diagnostics[0].equipment_id, line_id)
    _ASSERT.assertEqual(diagnostics[0].port_id, missing_port_id)
    _ASSERT.assertEqual(
        tuple(link for link in snapshot.links.values() if link.equipment_id == line_id),
        (),
    )


def test_unknown_behavior_is_fail_closed_and_returns_blocking_diagnostic() -> None:
    model = _model("unsupported behavior")
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("custom.type.unsupported"),
        1,
        "Unsupported test equipment",
        "custom.unsupported_behavior",
        (
            PortDefinition("from", "From", AC_POWER, voltage_group="main"),
            PortDefinition("to", "To", AC_POWER, voltage_group="main"),
        ),
    )
    model.register_equipment_type(definition)
    first = _node(model, "unsupported_a", VOLTAGE_10_KV)
    second = _node(model, "unsupported_b", VOLTAGE_10_KV)
    equipment_id = EquipmentId("equipment.unsupported")
    model.create_equipment(
        definition.id,
        "Unsupported",
        equipment_id=equipment_id,
        port_ids_by_role={
            "from": PortId("port.unsupported.from"),
            "to": PortId("port.unsupported.to"),
        },
        voltage_class_by_group={"main": VOLTAGE_10_KV},
    )
    _connect(model, equipment_id, "from", first.id)
    _connect(model, equipment_id, "to", second.id)
    snapshot = TopologyEngine().compile(model, _state(model))

    _ASSERT.assertFalse(snapshot.is_valid)
    diagnostics = _diagnostics(snapshot, "unsupported_behavior")
    _ASSERT.assertTrue(diagnostics)
    _ASSERT.assertIs(diagnostics[0].severity, DiagnosticSeverity.ERROR)
    _ASSERT.assertEqual(diagnostics[0].equipment_id, equipment_id)
    _ASSERT.assertNotEqual(
        snapshot.component_by_node[first.id], snapshot.component_by_node[second.id]
    )
    _ASSERT.assertEqual(
        tuple(
            link
            for link in snapshot.links.values()
            if link.equipment_id == equipment_id
        ),
        (),
    )
