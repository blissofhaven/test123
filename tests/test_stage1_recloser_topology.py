# -*- coding: utf-8 -*-
"""Graph-level smoke tests for recloser placement before the Stage 2 engine."""
from __future__ import annotations

from rza_calc.adapters import adapt_to_calculation
from rza_calc.core.electrical import ActiveTopology
from rza_calc.domain.electrical import (
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineKind,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)


VOLTAGE = VoltageClassId("builtin.voltage.ac.10kv")


def _model(name: str) -> ElectricalModel:
    return ElectricalModel.with_builtins(name)


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    result = ElectricalNode(
        ElectricalNodeId(f"node_{token}"),
        token,
        declared_voltage_class_id=VOLTAGE,
    )
    model.add_node(result)
    return result


def _source(model: ElectricalModel, token: str, node: ElectricalNode):
    equipment, _ = model.create_equipment(
        "builtin.external_grid",
        f"Source {token}",
        equipment_id=EquipmentId(f"source_{token}"),
        port_ids_by_role={"terminal": PortId(f"source_{token}_terminal")},
        voltage_class_by_group={"main": VOLTAGE},
    )
    model.connect_port(model.port_by_role(equipment.id, "terminal").id, node.id)
    return equipment


def _recloser(
    model: ElectricalModel, token: str, first: ElectricalNode, second: ElectricalNode
):
    equipment, _ = model.create_equipment(
        "builtin.recloser",
        f"Recloser {token}",
        equipment_id=EquipmentId(f"recloser_{token}"),
        port_ids_by_role={
            "a": PortId(f"recloser_{token}_a"),
            "b": PortId(f"recloser_{token}_b"),
        },
        properties={
            "manufacturer": "Test",
            "model": "R-10",
            "rated_voltage_v": 10_000,
            "rated_current_a": 630,
        },
        voltage_class_by_group={"main": VOLTAGE},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(model.port_by_role(equipment.id, "a").id, first.id)
    model.connect_port(model.port_by_role(equipment.id, "b").id, second.id)
    return equipment


def _line(
    model: ElectricalModel,
    token: str,
    first: ElectricalNode,
    second: ElectricalNode,
    length_mm: int = 1_000_000,
):
    return model.create_logical_line(
        f"Line {token}",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        length_mm,
        inherited_properties={
            "conductor_mark": "AC-70",
            "r1_ohm_per_km": 0.4,
            "x1_ohm_per_km": 0.35,
        },
    )


def _modes(model: ElectricalModel, recloser_id: EquipmentId):
    opened = OperatingState(
        OperatingStateId("open"),
        "Open",
        {recloser_id: SwitchPosition.OPEN},
    )
    closed = OperatingState(
        OperatingStateId("closed"),
        "Closed",
        {recloser_id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(opened)
    model.add_operating_state(closed)
    adapted = adapt_to_calculation(model)
    modes = {item.name: item for item in adapted.network.modes.values()}
    return adapted, modes


def _legacy_node(adapted, node: ElectricalNode) -> str:
    return adapted.trace.domain_node_to_legacy[node.id.value]


def test_recloser_after_tap_disconnects_only_main_continuation():
    model = _model("Recloser after tap")
    source_node = _node(model, "source")
    tap = _node(model, "tap")
    after_recloser = _node(model, "after_recloser")
    main_end = _node(model, "main_end")
    branch_end = _node(model, "branch_end")
    _source(model, "primary", source_node)
    _line(model, "before_tap", source_node, tap)
    _line(model, "tap_branch", tap, branch_end)
    recloser = _recloser(model, "main", tap, after_recloser)
    _line(model, "after_recloser", after_recloser, main_end)

    adapted, modes = _modes(model, recloser.id)
    opened = ActiveTopology(adapted.network, modes["Open"])
    closed = ActiveTopology(adapted.network, modes["Closed"])
    tap_id = _legacy_node(adapted, tap)
    branch_id = _legacy_node(adapted, branch_end)
    end_id = _legacy_node(adapted, main_end)
    assert opened.node_energized(tap_id)
    assert opened.node_energized(branch_id)
    assert not opened.node_energized(end_id)
    assert closed.node_energized(end_id)


def test_recloser_on_tap_preserves_the_main_line():
    model = _model("Recloser on tap")
    source_node = _node(model, "source")
    tap = _node(model, "tap")
    main_end = _node(model, "main_end")
    branch_start = _node(model, "branch_start")
    branch_end = _node(model, "branch_end")
    _source(model, "primary", source_node)
    _line(model, "main_before", source_node, tap)
    _line(model, "main_after", tap, main_end)
    recloser = _recloser(model, "tap", tap, branch_start)
    _line(model, "branch_after_recloser", branch_start, branch_end)

    adapted, modes = _modes(model, recloser.id)
    opened = ActiveTopology(adapted.network, modes["Open"])
    closed = ActiveTopology(adapted.network, modes["Closed"])
    assert opened.node_energized(_legacy_node(adapted, main_end))
    assert not opened.node_energized(_legacy_node(adapted, branch_end))
    assert closed.node_energized(_legacy_node(adapted, branch_end))


def test_multiple_taps_after_open_recloser_use_alternative_source():
    model = _model("Alternative source after recloser")
    primary_bus = _node(model, "primary")
    downstream_start = _node(model, "downstream_start")
    downstream_end = _node(model, "downstream_end")
    ktp_one = _node(model, "ktp_one")
    ktp_two = _node(model, "ktp_two")
    _source(model, "primary", primary_bus)
    _source(model, "alternative", downstream_end)
    recloser = _recloser(model, "feeder", primary_bus, downstream_start)
    _, original, _ = _line(
        model, "downstream", downstream_start, downstream_end, 12_000_000
    )
    first = model.split_line_section(original.equipment_id, 4_000_000)
    second = model.split_line_section(first.second_section_id, 4_000_000)
    _line(model, "ktp_one", model.electrical_nodes[first.tap_node_id], ktp_one)
    _line(model, "ktp_two", model.electrical_nodes[second.tap_node_id], ktp_two)

    adapted, modes = _modes(model, recloser.id)
    opened = ActiveTopology(adapted.network, modes["Open"])
    closed = ActiveTopology(adapted.network, modes["Closed"])
    primary_id = _legacy_node(adapted, primary_bus)
    downstream_id = _legacy_node(adapted, downstream_start)
    ktp_ids = {
        _legacy_node(adapted, ktp_one),
        _legacy_node(adapted, ktp_two),
    }
    primary_sources = set(opened.sources_for(primary_id))
    downstream_sources = set(opened.sources_for(downstream_id))
    assert primary_sources and downstream_sources
    assert primary_sources.isdisjoint(downstream_sources)
    assert opened.component_of[primary_id] != opened.component_of[downstream_id]
    assert all(opened.node_energized(item) for item in ktp_ids)
    assert all(set(opened.sources_for(item)) == downstream_sources for item in ktp_ids)
    assert closed.component_of[primary_id] == closed.component_of[downstream_id]
    assert set(closed.sources_for(downstream_id)) == (
        primary_sources | downstream_sources
    )


def test_open_recloser_in_meshed_ring_keeps_supply_via_alternative_path():
    model = _model("Meshed ring with recloser")
    source_bus = _node(model, "ring_source")
    remote_bus = _node(model, "ring_remote")
    alternate_bus = _node(model, "ring_alternate")
    source = _source(model, "ring", source_bus)
    recloser = _recloser(model, "ring", source_bus, remote_bus)
    _line(model, "ring_path_one", source_bus, alternate_bus)
    _line(model, "ring_path_two", alternate_bus, remote_bus)

    adapted, modes = _modes(model, recloser.id)
    opened = ActiveTopology(adapted.network, modes["Open"])
    closed = ActiveTopology(adapted.network, modes["Closed"])
    source_id = _legacy_node(adapted, source_bus)
    remote_id = _legacy_node(adapted, remote_bus)
    alternate_id = _legacy_node(adapted, alternate_bus)
    recloser_branch_id = adapted.trace.domain_equipment_to_legacy[
        recloser.id.value
    ][0]
    recloser_branch = adapted.network.branches[recloser_branch_id]

    assert not adapted.network.branch_conducting(recloser_branch, modes["Open"])
    assert adapted.network.branch_conducting(recloser_branch, modes["Closed"])
    assert opened.component_of[source_id] == opened.component_of[remote_id]
    assert opened.component_of[source_id] == opened.component_of[alternate_id]
    assert opened.node_energized(remote_id)
    assert set(opened.sources_for(remote_id)) == {
        adapted.trace.domain_equipment_to_legacy[source.id.value][0]
    }
    assert closed.component_of[source_id] == closed.component_of[remote_id]
    assert closed.node_energized(remote_id)
