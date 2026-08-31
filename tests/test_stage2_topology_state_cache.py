# -*- coding: utf-8 -*-
"""Stage-2 operating state, cache, immutability and adapter boundary tests."""
from __future__ import annotations

from dataclasses import replace

import pytest
import rza_calc.topology.engine as topology_engine_module

from rza_calc.adapters import LegacyCalculationAdapterError, adapt_to_calculation
from rza_calc.domain.electrical import (
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentAvailability,
    EquipmentId,
    EquipmentTypeId,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.topology import (
    Conductivity,
    ConcurrentTopologyMutationError,
    TopologyCompilationError,
    TopologyEngine,
    TopologyQueryError,
)


U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _node(model: ElectricalModel, token: str, voltage=U10) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.{token}"),
        token,
        declared_voltage_class_id=voltage,
    )
    model.add_node(node)
    return node


def _equipment(
    model: ElectricalModel,
    token: str,
    type_id: str,
    roles: tuple[str, ...],
    nodes: tuple[ElectricalNode, ...],
    *,
    normal_position: SwitchPosition | None = None,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.{token}")
    model.create_equipment(
        type_id,
        token,
        equipment_id=equipment_id,
        port_ids_by_role={role: PortId(f"port.{token}.{role}") for role in roles},
        voltage_class_by_group={"main": U10},
        normal_position=normal_position,
        properties=(
            {"s_kz_max": 500.0, "s_kz_min": 300.0}
            if type_id == "builtin.external_grid"
            else {"length_km": 1.0, "r0": 0.4, "x0": 0.3}
            if type_id == "builtin.line"
            else None
        ),
    )
    for role, node in zip(roles, nodes):
        model.connect_port(
            model.port_by_role(equipment_id, role).id,
            node.id,
            connection_id=ConnectionId(f"connection.{token}.{role}"),
        )
    return equipment_id


def _source(model: ElectricalModel, token: str, node: ElectricalNode) -> EquipmentId:
    return _equipment(
        model, token, "builtin.external_grid", ("terminal",), (node,)
    )


def _state(
    model: ElectricalModel,
    token: str,
    *,
    positions=None,
    availability=None,
) -> OperatingStateId:
    state = OperatingState(
        OperatingStateId(f"state.{token}"),
        token,
        positions or {},
        availability=availability or {},
    )
    model.add_operating_state(state)
    return state.id


def test_availability_is_orthogonal_and_maps_to_calculation_modes() -> None:
    model = ElectricalModel.with_builtins("availability")
    upstream = _node(model, "upstream")
    downstream = _node(model, "downstream")
    source_id = _source(model, "source", upstream)
    line_id = _equipment(
        model,
        "line",
        "builtin.line",
        ("from", "to"),
        (upstream, downstream),
    )
    load_id = _equipment(
        model, "load", "builtin.load", ("terminal",), (downstream,)
    )
    line_out = _state(
        model,
        "line_out",
        availability={line_id: EquipmentAvailability.OUT_OF_SERVICE},
    )
    source_out = _state(
        model,
        "source_out",
        availability={source_id: EquipmentAvailability.OUT_OF_SERVICE},
    )
    load_out = _state(
        model,
        "load_out",
        availability={load_id: EquipmentAvailability.OUT_OF_SERVICE},
    )
    engine = TopologyEngine()

    line_snapshot = engine.compile(model, line_out)
    line = next(item for item in line_snapshot.links.values() if item.equipment_id == line_id)
    assert not line.active
    assert line.availability == EquipmentAvailability.OUT_OF_SERVICE
    assert line.conductivity == Conductivity.OPEN
    assert line_snapshot.is_energized(upstream.id)
    assert not line_snapshot.is_energized(downstream.id)

    source_snapshot = engine.compile(model, source_out)
    assert not source_snapshot.sources[source_id].active
    assert not source_snapshot.is_energized(upstream.id)

    load_snapshot = engine.compile(model, load_out)
    assert load_snapshot.is_energized(downstream.id)
    adapted = adapt_to_calculation(model, load_snapshot)
    load_mode = next(item for item in adapted.network.modes.values() if item.name == "load_out")
    legacy_load_id = adapted.trace.domain_equipment_to_legacy[load_id.value][0]
    legacy_line_id = adapted.trace.domain_equipment_to_legacy[line_id.value][0]
    legacy_source_id = adapted.trace.domain_equipment_to_legacy[source_id.value][0]
    assert load_mode.availability[legacy_load_id] is False
    assert legacy_load_id not in {
        load.id for load in adapted.network.downstream_loads(
            adapted.network.branches[legacy_line_id], load_mode
        )
    }
    line_mode = next(item for item in adapted.network.modes.values() if item.name == "line_out")
    assert not adapted.network.branch_conducting(
        adapted.network.branches[legacy_line_id], line_mode
    )
    source_mode = next(item for item in adapted.network.modes.values() if item.name == "source_out")
    assert not adapted.network.branch_conducting(
        adapted.network.branches[legacy_source_id], source_mode
    )
    assert adapted.network.validate() == []


def test_sparse_state_uses_normal_position_and_ambiguous_switch_fails_open() -> None:
    model = ElectricalModel.with_builtins("sparse positions")
    first, second = _node(model, "first"), _node(model, "second")
    ambiguous_id = _equipment(
        model,
        "ambiguous",
        "builtin.circuit_breaker",
        ("a", "b"),
        (first, second),
    )
    explicit = _state(
        model,
        "explicit",
        positions={ambiguous_id: SwitchPosition.CLOSED},
    )
    engine = TopologyEngine()

    normal = engine.compile(model)
    assert not normal.is_valid
    assert any(item.code == "ambiguous_switch_position" for item in normal.diagnostics)
    assert not next(iter(normal.links.values())).active
    with pytest.raises(TopologyCompilationError):
        normal.require_valid()

    closed = engine.compile(model, explicit)
    assert closed.is_valid
    assert next(iter(closed.links.values())).active


def test_switch_capability_with_non_switch_behavior_is_blocking_and_fail_closed() -> None:
    for capability in ("switch.position", "legacy.switch.position"):
        token = capability.replace(".", "_")
        model = ElectricalModel.with_builtins(f"behavior mismatch {capability}")
        first, second = _node(model, f"{token}.a"), _node(model, f"{token}.b")
        line_definition = model.equipment_type(EquipmentTypeId("builtin.line"))
        custom_definition = replace(
            line_definition,
            id=EquipmentTypeId(f"custom.{token}.conductor"),
            capabilities=frozenset((*line_definition.capabilities, capability)),
        )
        model.register_equipment_type(custom_definition)
        equipment_id = _equipment(
            model,
            token,
            custom_definition.id.value,
            ("from", "to"),
            (first, second),
            normal_position=SwitchPosition.OPEN,
        )

        snapshot = TopologyEngine().compile(model)

        assert not snapshot.is_valid
        assert any(
            item.code == "switch_position_behavior_mismatch"
            and item.equipment_id == equipment_id
            for item in snapshot.diagnostics
        )
        assert not snapshot.has_path(first.id, second.id)
        assert equipment_id not in snapshot.link_ids_by_equipment


def test_cache_keys_use_model_identity_revision_and_full_detached_state() -> None:
    def build(token: str):
        model = ElectricalModel.with_builtins(token)
        first, second = _node(model, f"{token}.a"), _node(model, f"{token}.b")
        switch_id = _equipment(
            model,
            f"{token}.qf",
            "builtin.circuit_breaker",
            ("a", "b"),
            (first, second),
            normal_position=SwitchPosition.CLOSED,
        )
        return model, switch_id, first, second

    first_model, switch_id, first, second = build("same")
    second_model, _, _, _ = build("same")
    assert first_model.revision == second_model.revision
    engine = TopologyEngine()

    cached = engine.compile(first_model)
    assert engine.compile(first_model) is cached
    other_model = engine.compile(second_model)
    assert other_model is not cached

    state_id = OperatingStateId("state.detached")
    opened = OperatingState(
        state_id, "Detached", {switch_id: SwitchPosition.OPEN}
    )
    closed = OperatingState(
        state_id, "Detached", {switch_id: SwitchPosition.CLOSED}
    )
    open_snapshot = engine.compile(first_model, opened)
    closed_snapshot = engine.compile(first_model, closed)
    assert open_snapshot is not closed_snapshot
    assert not open_snapshot.has_path(first.id, second.id)
    assert closed_snapshot.has_path(first.id, second.id)

    first_model.rename_equipment(switch_id, "Renamed without electrical change")
    after_revision = engine.compile(first_model)
    assert after_revision is not cached
    assert after_revision.topology_fingerprint == cached.topology_fingerprint


def test_cache_hit_rechecks_revision_after_model_view_capture() -> None:
    model = ElectricalModel.with_builtins("cache race")
    first, second = _node(model, "cache.race.a"), _node(model, "cache.race.b")
    line_id = _equipment(
        model,
        "cache.race.line",
        "builtin.line",
        ("from", "to"),
        (first, second),
    )
    engine = TopologyEngine()
    engine.compile(model)
    original_model_view = topology_engine_module._model_view
    mutated = False

    def capture_then_mutate(subject):
        nonlocal mutated
        view = original_model_view(subject)
        if not mutated:
            mutated = True
            subject.rename_equipment(line_id, "mutated after capture")
        return view

    topology_engine_module._model_view = capture_then_mutate
    try:
        with pytest.raises(ConcurrentTopologyMutationError):
            engine.compile(model)
    finally:
        topology_engine_module._model_view = original_model_view


def test_snapshot_is_deeply_immutable_and_unknown_queries_are_typed() -> None:
    model = ElectricalModel.with_builtins("immutable")
    first, second = _node(model, "immutable.a"), _node(model, "immutable.b")
    _equipment(
        model,
        "immutable.line",
        "builtin.line",
        ("from", "to"),
        (first, second),
    )
    snapshot = TopologyEngine().compile(model)
    signature = snapshot.semantic_signature()

    with pytest.raises(TypeError):
        snapshot.component_by_node[first.id] = next(iter(snapshot.components))
    with pytest.raises(TypeError):
        snapshot.links[next(iter(snapshot.links))] = next(iter(snapshot.links.values()))
    with pytest.raises(TopologyQueryError):
        snapshot.neighbors(ElectricalNodeId("node.missing"))

    model.rename_equipment(EquipmentId("equipment.immutable.line"), "Changed")
    assert snapshot.semantic_signature() == signature


def test_adapter_accepts_inferred_voltage_and_rejects_stale_snapshot() -> None:
    model = ElectricalModel.with_builtins("adapter snapshot")
    upstream = _node(model, "adapter.upstream", U10)
    downstream = _node(model, "adapter.downstream", None)
    _source(model, "adapter.source", upstream)
    line_id = _equipment(
        model,
        "adapter.line",
        "builtin.line",
        ("from", "to"),
        (upstream, downstream),
    )
    snapshot = TopologyEngine().compile(model)
    assert snapshot.voltage_by_node[downstream.id].voltage_class_id == U10

    with pytest.raises(LegacyCalculationAdapterError):
        adapt_to_calculation(model)
    result = adapt_to_calculation(model, snapshot)
    legacy_downstream = result.trace.domain_node_to_legacy[downstream.id.value]
    assert result.network.nodes[legacy_downstream].u_nom == 10.0

    model.rename_equipment(line_id, "New line name")
    with pytest.raises(LegacyCalculationAdapterError) as error:
        adapt_to_calculation(model, snapshot)
    assert error.value.diagnostics[0].code == "topology_snapshot_incompatible"


def test_missing_registered_state_returns_partial_invalid_snapshot() -> None:
    model = ElectricalModel.with_builtins("missing state")
    node = _node(model, "missing.state")
    snapshot = TopologyEngine().compile(model, OperatingStateId("state.missing"))
    assert node.id in snapshot.component_by_node
    assert not snapshot.is_valid
    assert any(item.code == "operating_state_not_found" for item in snapshot.diagnostics)
