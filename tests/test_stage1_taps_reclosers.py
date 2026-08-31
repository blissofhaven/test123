# -*- coding: utf-8 -*-
"""Acceptance coverage for Stage 1 line taps, sections and reclosers."""
from __future__ import annotations

import json

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.core.electrical import ActiveTopology
from rza_calc.domain.electrical import (
    ConnectionId,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentTypeId,
    LineKind,
    LogicalLineId,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.io.electrical_model import (
    electrical_model_from_dict,
    electrical_model_to_dict,
)


VOLTAGE_10_KV = VoltageClassId("builtin.voltage.ac.10kv")


def _model(name: str = "Stage 1 taps") -> ElectricalModel:
    return ElectricalModel.with_builtins(name)


def _add_node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node_{token}"),
        token,
        declared_voltage_class_id=VOLTAGE_10_KV,
    )
    model.add_node(node)
    return node


def _create_line(
    model: ElectricalModel,
    token: str,
    start: ElectricalNode,
    finish: ElectricalNode,
    length_mm: int = 10_000,
    *,
    line_kind: LineKind = LineKind.OVERHEAD,
    inherited_properties: dict | None = None,
    section_properties: dict | None = None,
):
    return model.create_logical_line(
        f"Line {token}",
        line_kind,
        start.id,
        finish.id,
        length_mm,
        inherited_properties=inherited_properties,
        section_properties=section_properties,
        logical_line_id=LogicalLineId(f"logical_{token}"),
        section_equipment_id=EquipmentId(f"section_{token}"),
        port_ids_by_role={
            "from": PortId(f"port_{token}_from"),
            "to": PortId(f"port_{token}_to"),
        },
        connection_ids=(
            ConnectionId(f"connection_{token}_from"),
            ConnectionId(f"connection_{token}_to"),
        ),
    )


def _split(
    model: ElectricalModel,
    section_id: EquipmentId,
    offset_mm: int,
    token: str,
):
    return model.split_line_section(
        section_id,
        offset_mm,
        tap_node_id=ElectricalNodeId(f"tap_{token}"),
        first_section_id=EquipmentId(f"split_{token}_first"),
        second_section_id=EquipmentId(f"split_{token}_second"),
        first_port_ids_by_role={
            "from": PortId(f"split_{token}_first_from"),
            "to": PortId(f"split_{token}_first_to"),
        },
        second_port_ids_by_role={
            "from": PortId(f"split_{token}_second_from"),
            "to": PortId(f"split_{token}_second_to"),
        },
        connection_ids=tuple(
            ConnectionId(f"split_{token}_connection_{index}")
            for index in range(4)
        ),
    )


def _section_endpoints(
    model: ElectricalModel, section_id: EquipmentId
) -> tuple[ElectricalNodeId, ElectricalNodeId]:
    return tuple(
        model.node_for_port(model.port_by_role(section_id, role).id).id
        for role in ("from", "to")
    )


def _connections_at(
    model: ElectricalModel, node_id: ElectricalNodeId
) -> tuple:
    return tuple(
        connection
        for connection in model.connections.values()
        if connection.electrical_node_id == node_id
    )


def _plain_snapshot(model: ElectricalModel) -> dict:
    return json.loads(json.dumps(electrical_model_to_dict(model), ensure_ascii=False))


def _identity_sets(model: ElectricalModel) -> dict[str, set[str]]:
    return {
        "logical_lines": {item.value for item in model.logical_lines},
        "line_sections": {item.value for item in model.line_sections},
        "equipment": {item.value for item in model.equipment},
        "ports": {item.value for item in model.ports},
        "nodes": {item.value for item in model.electrical_nodes},
        "connections": {item.value for item in model.connections},
        "states": {item.value for item in model.operating_states},
    }


def _node_components(model: ElectricalModel) -> list[frozenset[ElectricalNodeId]]:
    adjacency = {node_id: set() for node_id in model.electrical_nodes}
    for section_id in model.line_sections:
        first, second = _section_endpoints(model, section_id)
        adjacency[first].add(second)
        adjacency[second].add(first)
    components: list[frozenset[ElectricalNodeId]] = []
    seen: set[ElectricalNodeId] = set()
    for start in adjacency:
        if start in seen:
            continue
        stack = [start]
        group: set[ElectricalNodeId] = set()
        while stack:
            current = stack.pop()
            if current in group:
                continue
            group.add(current)
            stack.extend(adjacency[current] - group)
        seen.update(group)
        components.append(frozenset(group))
    return components


def test_explicit_node_accepts_four_independent_line_branches():
    model = _model("Four-way junction")
    hub = _add_node(model, "hub")
    endpoints = [_add_node(model, f"end_{index}") for index in range(4)]

    for index, endpoint in enumerate(endpoints):
        _create_line(model, f"spoke_{index}", hub, endpoint, 1_000 + index)

    hub_connections = _connections_at(model, hub.id)
    assert len(hub_connections) == 4
    assert len({item.port_id for item in hub_connections}) == 4
    assert len(model.logical_lines) == 4
    assert len(model.line_sections) == 4
    assert model.validate_integrity() == []

    # The compatibility adapter already supports line_section.  It must retain
    # all four physical branches at the same calculation node.
    adapted = adapt_to_calculation(model)
    legacy_hub = adapted.trace.domain_node_to_legacy[hub.id.value]
    assert sum(
        legacy_hub in (branch.node_from, branch.node_to)
        for branch in adapted.network.branches.values()
    ) == 4


def test_repeated_split_supports_one_two_and_three_or_more_taps():
    model = _model("Repeated split")
    start = _add_node(model, "start")
    finish = _add_node(model, "finish")
    line, original, _ = _create_line(model, "trunk", start, finish, 12_000)

    first = _split(model, original.equipment_id, 6_000, "one")
    assert model.logical_lines[line.id].section_equipment_ids == (
        first.first_section_id,
        first.second_section_id,
    )
    assert len(_connections_at(model, first.tap_node_id)) == 2

    second = _split(model, first.second_section_id, 2_000, "two")
    assert model.logical_lines[line.id].section_equipment_ids == (
        first.first_section_id,
        second.first_section_id,
        second.second_section_id,
    )

    third = _split(model, first.first_section_id, 1_000, "three")
    section_ids = model.logical_lines[line.id].section_equipment_ids
    assert section_ids == (
        third.first_section_id,
        third.second_section_id,
        second.first_section_id,
        second.second_section_id,
    )
    assert len(section_ids) == 4
    assert {
        first.tap_node_id,
        second.tap_node_id,
        third.tap_node_id,
    } <= set(model.electrical_nodes)
    assert sum(model.line_sections[item].length_mm for item in section_ids) == 12_000
    for previous, following in zip(section_ids, section_ids[1:]):
        assert _section_endpoints(model, previous)[1] == _section_endpoints(
            model, following
        )[0]
    assert model.validate_integrity() == []


def test_tap_can_be_created_on_an_existing_branch_line():
    model = _model("Tap on branch")
    start = _add_node(model, "source")
    finish = _add_node(model, "trunk_end")
    branch_end = _add_node(model, "branch_end")
    nested_end = _add_node(model, "nested_end")
    _, trunk_section, _ = _create_line(model, "main", start, finish, 10_000)

    first_split, first_branch, first_branch_section, _ = model.create_tap_line(
        trunk_section.equipment_id,
        4_000,
        "First branch",
        LineKind.CABLE,
        branch_end.id,
        3_000,
    )
    second_split, second_branch, _, _ = model.create_tap_line(
        first_branch_section.equipment_id,
        1_000,
        "Branch on branch",
        LineKind.OVERHEAD,
        nested_end.id,
        2_000,
    )

    assert second_split.removed_section_id == first_branch_section.equipment_id
    assert first_branch.id in model.logical_lines
    assert second_branch.id in model.logical_lines
    assert len(_connections_at(model, first_split.tap_node_id)) == 3
    assert len(_connections_at(model, second_split.tap_node_id)) == 3
    assert first_branch_section.equipment_id not in model.equipment
    assert model.validate_integrity() == []


def test_remove_branch_and_sections_cleans_membership_but_preserves_nodes():
    model = _model("Removal")
    start = _add_node(model, "remove_start")
    finish = _add_node(model, "remove_finish")
    branch_end = _add_node(model, "remove_branch_end")
    trunk, original, _ = _create_line(model, "remove_trunk", start, finish, 8_000)
    split, branch, branch_section, _ = model.create_tap_line(
        original.equipment_id,
        3_000,
        "Removable branch",
        LineKind.CABLE,
        branch_end.id,
        2_000,
    )
    tap_id = split.tap_node_id

    before = _plain_snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.remove_equipment(branch_section.equipment_id, cascade=True)
    with pytest.raises(DomainInvariantError):
        model.remove_logical_line(branch.id)
    assert _plain_snapshot(model) == before

    model.remove_logical_line(branch.id, cascade=True)
    assert branch.id not in model.logical_lines
    assert branch_section.equipment_id not in model.line_sections
    assert branch_section.equipment_id not in model.equipment
    assert tap_id in model.electrical_nodes
    assert branch_end.id in model.electrical_nodes
    assert len(_connections_at(model, tap_id)) == 2

    model.remove_line_section(split.first_section_id)
    assert split.first_section_id not in model.equipment
    assert model.logical_lines[trunk.id].section_equipment_ids == (
        split.second_section_id,
    )
    model.remove_line_section(split.second_section_id)
    assert trunk.id not in model.logical_lines
    assert not model.line_sections
    assert tap_id in model.electrical_nodes
    assert model.validate_integrity() == []


def test_remove_line_tap_collapses_equal_main_sections_atomically():
    model = _model("Collapse tap")
    start = _add_node(model, "collapse_start")
    finish = _add_node(model, "collapse_finish")
    branch_end = _add_node(model, "collapse_branch_end")
    trunk, original, _ = _create_line(model, "collapse_trunk", start, finish, 8_000)
    split, branch, _, _ = model.create_tap_line(
        original.equipment_id,
        3_000,
        "Temporary branch",
        LineKind.CABLE,
        branch_end.id,
        2_000,
    )
    model.set_section_override(split.second_section_id, "material", "Cu")
    before_failed_collapse = _plain_snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.remove_line_tap(
            trunk.id,
            split.tap_node_id,
            branch_line_ids=(branch.id,),
        )
    assert _plain_snapshot(model) == before_failed_collapse
    model.clear_section_override(split.second_section_id, "material")

    metadata_raw = _plain_snapshot(model)
    next(
        item for item in metadata_raw["equipment"]
        if item["id"] == split.second_section_id.value
    )["note"] = "Значимая граница участка"
    metadata_model = electrical_model_from_dict(metadata_raw)
    before_metadata_failure = _plain_snapshot(metadata_model)
    with pytest.raises(DomainInvariantError):
        metadata_model.remove_line_tap(
            trunk.id,
            split.tap_node_id,
            branch_line_ids=(branch.id,),
        )
    assert _plain_snapshot(metadata_model) == before_metadata_failure

    revision = model.revision
    model.remove_line_tap(
        trunk.id,
        split.tap_node_id,
        branch_line_ids=(branch.id,),
        merged_section_id=EquipmentId("collapsed_section"),
        merged_port_ids_by_role={
            "from": PortId("collapsed_from"),
            "to": PortId("collapsed_to"),
        },
        merged_connection_ids=(
            ConnectionId("collapsed_connection_from"),
            ConnectionId("collapsed_connection_to"),
        ),
    )
    assert model.revision == revision + 1
    assert branch.id not in model.logical_lines
    assert split.tap_node_id not in model.electrical_nodes
    assert split.first_section_id not in model.equipment
    assert split.second_section_id not in model.equipment
    assert model.logical_lines[trunk.id].section_equipment_ids == (
        EquipmentId("collapsed_section"),
    )
    assert model.line_sections[EquipmentId("collapsed_section")].length_mm == 8_000
    assert _section_endpoints(model, EquipmentId("collapsed_section")) == (
        start.id,
        finish.id,
    )
    assert model.validate_integrity() == []


def test_line_inheritance_section_override_clear_and_length_update():
    model = _model("Overrides")
    start = _add_node(model, "override_start")
    finish = _add_node(model, "override_finish")
    line, section, _ = _create_line(
        model,
        "overrides",
        start,
        finish,
        1_500_000,
        inherited_properties={
            "conductor_mark": "АС-70",
            "material": "Al",
            "parallel_count": 2,
            "r1_ohm_per_km": 0.42,
        },
        section_properties={"conductor_mark": "АС-95"},
    )

    effective = model.effective_equipment_properties(section.equipment_id)
    assert effective["conductor_mark"] == "АС-95"
    assert effective["material"] == "Al"
    assert effective["parallel_count"] == 2
    assert effective["capacitive_current_a_per_km"] == 0.0

    model.set_line_inherited_property(line.id, "material", "Cu")
    assert model.effective_equipment_properties(section.equipment_id)["material"] == "Cu"
    model.set_section_override(section.equipment_id, "material", "Al")
    model.set_line_inherited_property(line.id, "material", "Cu")
    assert model.effective_equipment_properties(section.equipment_id)["material"] == "Al"
    model.clear_section_override(section.equipment_id, "material")
    assert model.effective_equipment_properties(section.equipment_id)["material"] == "Cu"
    model.clear_section_override(section.equipment_id, "conductor_mark")
    assert model.effective_equipment_properties(section.equipment_id)[
        "conductor_mark"
    ] == "АС-70"

    revision = model.revision
    model.set_section_length(section.equipment_id, 2_000_000)
    assert model.line_sections[section.equipment_id].length_mm == 2_000_000
    assert model.revision == revision + 1

    before = _plain_snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.set_section_override(section.equipment_id, "parallel_count", "two")
    assert _plain_snapshot(model) == before
    assert model.validate_integrity() == []


def test_complex_tap_model_roundtrips_with_all_stable_ids():
    model = _model("Round-trip")
    start = _add_node(model, "roundtrip_start")
    finish = _add_node(model, "roundtrip_finish")
    branch_end = _add_node(model, "roundtrip_branch")
    rec_a = _add_node(model, "roundtrip_rec_a")
    rec_b = _add_node(model, "roundtrip_rec_b")
    line, original, _ = _create_line(model, "roundtrip", start, finish, 9_000)
    split = _split(model, original.equipment_id, 3_000, "roundtrip")
    _create_line(
        model,
        "roundtrip_branch",
        model.electrical_nodes[split.tap_node_id],
        branch_end,
        2_000,
        line_kind=LineKind.CABLE,
    )

    recloser, _ = model.create_equipment(
        "builtin.recloser",
        "REC round-trip",
        equipment_id=EquipmentId("roundtrip_recloser"),
        port_ids_by_role={
            "a": PortId("roundtrip_recloser_a"),
            "b": PortId("roundtrip_recloser_b"),
        },
        voltage_class_by_group={"main": VOLTAGE_10_KV},
        properties={"manufacturer": "User catalog", "rated_current_a": 630},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(
        model.port_by_role(recloser.id, "a").id,
        rec_a.id,
        connection_id=ConnectionId("roundtrip_recloser_connection_a"),
    )
    model.connect_port(
        model.port_by_role(recloser.id, "b").id,
        rec_b.id,
        connection_id=ConnectionId("roundtrip_recloser_connection_b"),
    )
    model.add_operating_state(OperatingState(
        OperatingStateId("roundtrip_open"),
        "Open",
        {recloser.id: SwitchPosition.OPEN},
    ))

    expected_ids = _identity_sets(model)
    expected_signature = model.connectivity_signature()
    raw = _plain_snapshot(model)
    restored = electrical_model_from_dict(raw)

    assert _identity_sets(restored) == expected_ids
    assert restored.connectivity_signature() == expected_signature
    assert electrical_model_to_dict(restored) == raw
    assert restored.logical_lines[line.id].section_equipment_ids == (
        split.first_section_id,
        split.second_section_id,
    )
    assert restored.operating_states[OperatingStateId("roundtrip_open")].positions[
        recloser.id
    ] == SwitchPosition.OPEN
    assert restored.validate_integrity() == []


def test_visual_crossing_without_shared_node_creates_no_electrical_link():
    model = _model("Crossing")
    a = _add_node(model, "cross_a")
    b = _add_node(model, "cross_b")
    c = _add_node(model, "cross_c")
    d = _add_node(model, "cross_d")
    _create_line(model, "cross_first", a, b, 1_000)
    _create_line(model, "cross_second", c, d, 1_000)

    # A future Diagram Model may draw these routes through the same pixel.
    # With no explicit shared ElectricalNode the Domain Model has two components.
    components = {frozenset(item.value for item in group)
                  for group in _node_components(model)}
    assert components == {
        frozenset({a.id.value, b.id.value}),
        frozenset({c.id.value, d.id.value}),
    }
    assert set(_section_endpoints(model, EquipmentId("section_cross_first"))).isdisjoint(
        _section_endpoints(model, EquipmentId("section_cross_second"))
    )
    assert model.validate_integrity() == []


def test_invalid_split_is_atomic_even_after_staged_partial_work():
    model = _model("Atomic split")
    start = _add_node(model, "atomic_start")
    finish = _add_node(model, "atomic_finish")
    _, section, _ = _create_line(model, "atomic", start, finish, 1_000)

    for invalid_offset in (0, 1_000, -1, True, 1.5):
        before = _plain_snapshot(model)
        with pytest.raises(DomainInvariantError):
            model.split_line_section(
                section.equipment_id,
                invalid_offset,  # type: ignore[arg-type]
            )
        assert _plain_snapshot(model) == before

    # The first staged connection is valid; the second collides with an
    # existing ID.  None of the staged node/equipment/connection records may
    # leak into the real aggregate.
    before = _plain_snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.split_line_section(
            section.equipment_id,
            500,
            tap_node_id=ElectricalNodeId("tap_atomic_failure"),
            first_section_id=EquipmentId("atomic_failure_first"),
            second_section_id=EquipmentId("atomic_failure_second"),
            first_port_ids_by_role={
                "from": PortId("atomic_failure_first_from"),
                "to": PortId("atomic_failure_first_to"),
            },
            second_port_ids_by_role={
                "from": PortId("atomic_failure_second_from"),
                "to": PortId("atomic_failure_second_to"),
            },
            connection_ids=(
                ConnectionId("atomic_failure_new_connection"),
                ConnectionId("connection_atomic_from"),
                ConnectionId("atomic_failure_third_connection"),
                ConnectionId("atomic_failure_fourth_connection"),
            ),
        )
    assert _plain_snapshot(model) == before


def test_recloser_is_distinct_stateful_equipment_and_switching_never_deletes_it():
    model = _model("Canonical recloser")
    first_node = _add_node(model, "recloser_a")
    second_node = _add_node(model, "recloser_b")
    recloser, _ = model.create_equipment(
        "builtin.recloser",
        "REC-1",
        equipment_id=EquipmentId("recloser"),
        port_ids_by_role={"a": PortId("recloser_a"), "b": PortId("recloser_b")},
        voltage_class_by_group={"main": VOLTAGE_10_KV},
        properties={
            "manufacturer": "User catalog",
            "model": "REC-10",
            "rated_voltage_v": 10_000,
            "rated_current_a": 630,
            "full_opening_time_s": 0.08,
            "protection_settings": {"enabled": False},
            "auto_reclose_settings": {"enabled": True},
        },
        normal_position=SwitchPosition.CLOSED,
    )
    for role, node, connection_id in (
        ("a", first_node, "recloser_connection_a"),
        ("b", second_node, "recloser_connection_b"),
    ):
        model.connect_port(
            model.port_by_role(recloser.id, role).id,
            node.id,
            connection_id=ConnectionId(connection_id),
        )
    opened = OperatingState(
        OperatingStateId("recloser_open"),
        "Open",
        {recloser.id: SwitchPosition.OPEN},
    )
    closed = OperatingState(
        OperatingStateId("recloser_closed"),
        "Closed",
        {recloser.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(opened)
    model.add_operating_state(closed)

    definition = model.equipment_type(recloser.type_id, recloser.type_version)
    assert definition.id == EquipmentTypeId("builtin.recloser")
    assert definition.id != EquipmentTypeId("builtin.circuit_breaker")
    assert definition.behavior_key == "recloser"
    assert "switch.position" in definition.capabilities

    persistent_ids = (
        set(model.equipment),
        set(model.ports),
        set(model.connections),
        recloser.port_ids,
    )
    model.set_switch_position(opened.id, recloser.id, SwitchPosition.CLOSED)
    model.set_switch_position(opened.id, recloser.id, SwitchPosition.OPEN)
    assert (
        set(model.equipment),
        set(model.ports),
        set(model.connections),
        model.equipment[recloser.id].port_ids,
    ) == persistent_ids

    raw = _plain_snapshot(model)
    restored = electrical_model_from_dict(raw)
    assert electrical_model_to_dict(restored) == raw
    assert restored.equipment[recloser.id].normal_position == SwitchPosition.CLOSED
    assert restored.operating_states[opened.id].positions[recloser.id] == SwitchPosition.OPEN
    assert restored.operating_states[closed.id].positions[recloser.id] == SwitchPosition.CLOSED

    # The current adapter explicitly supports canonical reclosers, so a small
    # compatibility smoke-test is honest here.  It is not a Stage 2 engine.
    adapted = adapt_to_calculation(restored)
    branch_id = adapted.trace.domain_equipment_to_legacy[recloser.id.value][0]
    legacy_a = adapted.trace.domain_node_to_legacy[first_node.id.value]
    legacy_b = adapted.trace.domain_node_to_legacy[second_node.id.value]
    modes = {mode.name: mode for mode in adapted.network.modes.values()}
    branch = adapted.network.branches[branch_id]
    assert not adapted.network.branch_conducting(branch, modes["Open"])
    assert adapted.network.branch_conducting(branch, modes["Closed"])
    opened_topology = ActiveTopology(adapted.network, modes["Open"])
    closed_topology = ActiveTopology(adapted.network, modes["Closed"])
    assert opened_topology.component_of[legacy_a] != opened_topology.component_of[legacy_b]
    assert closed_topology.component_of[legacy_a] == closed_topology.component_of[legacy_b]
    assert restored.validate_integrity() == []
