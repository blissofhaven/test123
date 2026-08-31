# -*- coding: utf-8 -*-
"""Domain-only coverage for automatic junction and inline placement rules."""
from __future__ import annotations

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.domain.electrical import (
    AC_POWER,
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    InsertRecloserResult,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    OperatingState,
    OperatingStateId,
    PLACEMENT_INLINE_SERIES_CAPABILITY,
    PLACEMENT_MANUAL_SELECTION_CAPABILITY,
    PortDefinition,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.auto.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _line(
    model: ElectricalModel,
    *,
    length_mm: int = 10_000_000,
    construction_segments: tuple[LineConstructionSegment, ...] | None = None,
):
    first = _node(model, "line.first")
    second = _node(model, "line.second")
    _, section, _ = model.create_logical_line(
        "ВЛ для автоматической вставки",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        length_mm,
        voltage_class_id=U10,
        inherited_properties={
            "r1_ohm_per_km": 0.4,
            "x1_ohm_per_km": 0.3,
        },
        construction_segments=construction_segments,
    )
    return first, second, section


def test_connect_ports_marks_only_the_automatically_created_node() -> None:
    model = ElectricalModel.with_builtins("Automatic port junction")
    first, _ = model.create_equipment(
        "builtin.load",
        "Нагрузка 1",
        voltage_class_by_group={"main": U10},
    )
    second, _ = model.create_equipment(
        "builtin.load",
        "Нагрузка 2",
        voltage_class_by_group={"main": U10},
    )
    model.connect_ports(first.port_ids[0], second.port_ids[0])

    automatic = model.node_for_port(first.port_ids[0])
    assert automatic is not None
    assert automatic.extensions["creation_origin"] == "automatic_port_to_port"

    manual = _node(model, "manual")
    assert "creation_origin" not in manual.extensions


def test_generic_breaker_insertion_keeps_distinct_nodes_and_switches_topology() -> None:
    model = ElectricalModel.with_builtins("Generic breaker")
    first, second, section = _line(model)

    inserted = model.insert_series_equipment_in_line(
        section.equipment_id,
        4_000_000,
        "builtin.circuit_breaker",
        "QF-1",
    )

    assert inserted.left_node_id != inserted.right_node_id
    assert model.electrical_nodes[inserted.left_node_id].extensions == {
        "junction_kind": "inline_device_left",
        "creation_origin": "automatic_inline_insertion",
        "physical_offset_mm": 4_000_000,
        "physical_position_confirmed": True,
    }
    assert (
        model.electrical_nodes[inserted.right_node_id]
        .extensions["creation_origin"]
        == "automatic_inline_insertion"
    )
    marker = model.equipment[inserted.equipment_id].extensions["line_insertion"]
    assert marker["kind"] == "inline_series"
    assert marker["equipment_type_id"] == "builtin.circuit_breaker"
    assert tuple(marker["terminal_roles"]) == ("a", "b")
    assert marker["left_node_id"] != marker["right_node_id"]
    assert marker["physical_position_confirmed"] is True

    closed = TopologyEngine().compile(model)
    assert closed.has_path(first.id, second.id)
    opened_state = OperatingState(
        OperatingStateId("state.auto.breaker.open"),
        "Выключатель отключён",
        {inserted.equipment_id: SwitchPosition.OPEN},
    )
    model.add_operating_state(opened_state)
    opened = TopologyEngine().compile(model, opened_state.id)
    assert not opened.has_path(first.id, second.id)


def test_generic_removal_restores_one_line_and_preserves_physical_length() -> None:
    model = ElectricalModel.with_builtins("Generic removal")
    first, second, section = _line(model)
    inserted = model.insert_series_equipment_in_line(
        section.equipment_id,
        4_000_000,
        "builtin.disconnector",
        "QS-1",
    )

    removed = model.remove_series_equipment_from_line(inserted.equipment_id)

    assert inserted.equipment_id not in model.equipment
    assert inserted.right_logical_line_id not in model.logical_lines
    assert inserted.left_node_id not in model.electrical_nodes
    assert inserted.right_node_id not in model.electrical_nodes
    assert model.line_sections[removed.merged_section_id].length_mm == 10_000_000
    assert TopologyEngine().compile(model).has_path(first.id, second.id)


def test_recloser_compatibility_wrappers_keep_old_result_contracts() -> None:
    model = ElectricalModel.with_builtins("Recloser compatibility")
    _, _, section = _line(model)
    inserted = model.insert_recloser_in_line(
        section.equipment_id,
        5_000_000,
        "Реклоузер-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
    )

    assert isinstance(inserted, InsertRecloserResult)
    marker = model.equipment[inserted.recloser_id].extensions["line_insertion"]
    assert marker["kind"] == "inline_series"
    assert marker["equipment_type_id"] == "builtin.recloser"
    assert tuple(marker["terminal_roles"]) == ("a", "b")

    removed = model.remove_recloser_from_line(inserted.recloser_id)
    assert removed.removed_recloser_id == inserted.recloser_id
    assert removed.merged_section_id in model.line_sections


def test_custom_registered_two_port_roles_are_used_without_graphics() -> None:
    model = ElectricalModel.with_builtins("Custom inline type")
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.inline.fuse"),
        1,
        "Плавкая вставка",
        "switch",
        (
            PortDefinition("upstream", "Вход", AC_POWER, voltage_group="main"),
            PortDefinition("downstream", "Выход", AC_POWER, voltage_group="main"),
        ),
        capabilities=frozenset(
            {"switch.position", PLACEMENT_INLINE_SERIES_CAPABILITY}
        ),
    )
    model.register_equipment_type(definition)
    _, _, section = _line(model)

    inserted = model.insert_series_equipment_in_line(
        section.equipment_id,
        3_000_000,
        definition.id,
        "FU-1",
    )

    assert inserted.terminal_roles == ("upstream", "downstream")
    assert {
        model.ports[item].role for item in model.equipment[inserted.equipment_id].port_ids
    } == {"upstream", "downstream"}


def test_manual_placement_requires_explicit_confirmation() -> None:
    model = ElectricalModel.with_builtins("Manual placement")
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.manual.two_port"),
        1,
        "Аппарат с ручным выбором размещения",
        "switch",
        (
            PortDefinition("in", "Вход", AC_POWER, voltage_group="main"),
            PortDefinition("out", "Выход", AC_POWER, voltage_group="main"),
        ),
        capabilities=frozenset(
            {"switch.position", PLACEMENT_MANUAL_SELECTION_CAPABILITY}
        ),
    )
    model.register_equipment_type(definition)
    _, _, section = _line(model)
    revision_before = model.revision
    fingerprint_before = electrical_model_fingerprint(model)

    with pytest.raises(DomainInvariantError):
        model.insert_series_equipment_in_line(
            section.equipment_id,
            2_000_000,
            definition.id,
            "Ручной аппарат",
        )

    assert model.revision == revision_before
    assert electrical_model_fingerprint(model) == fingerprint_before
    inserted = model.insert_series_equipment_in_line(
        section.equipment_id,
        2_000_000,
        definition.id,
        "Ручной аппарат",
        manual_placement_confirmed=True,
    )
    marker = model.equipment[inserted.equipment_id].extensions["line_insertion"]
    assert marker["manual_placement_confirmed"] is True
    removed = model.remove_series_equipment_from_line(inserted.equipment_id)
    assert removed.removed_equipment_id == inserted.equipment_id


@pytest.mark.parametrize("invalid_kind", ["non_inline", "one_port"])
def test_invalid_inline_type_is_rejected_atomically(invalid_kind: str) -> None:
    model = ElectricalModel.with_builtins(f"Atomic rejection {invalid_kind}")
    _, _, section = _line(model)
    type_id: EquipmentTypeId | str = "builtin.transformer_2w"
    if invalid_kind == "one_port":
        definition = EquipmentTypeDefinition(
            EquipmentTypeId("user.invalid.inline.one_port"),
            1,
            "Некорректный однопортовый аппарат",
            "load",
            (PortDefinition("terminal", "Вывод", AC_POWER, voltage_group="main"),),
            capabilities=frozenset({PLACEMENT_INLINE_SERIES_CAPABILITY}),
        )
        model.register_equipment_type(definition)
        type_id = definition.id
    revision_before = model.revision
    fingerprint_before = electrical_model_fingerprint(model)

    with pytest.raises(DomainInvariantError):
        model.insert_series_equipment_in_line(
            section.equipment_id,
            4_000_000,
            type_id,
            "Недопустимый аппарат",
        )

    assert model.revision == revision_before
    assert electrical_model_fingerprint(model) == fingerprint_before
    assert section.equipment_id in model.line_sections


def test_known_length_split_without_offset_never_fabricates_lengths() -> None:
    model = ElectricalModel.with_builtins("Unconfirmed physical split")
    source_segments = (
        LineConstructionSegment(
            LineConstructionSegmentId("segment.auto.overhead"),
            LineKind.OVERHEAD,
            400_000,
            {"conductor_mark": "АС-70", "r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            length_confirmation=DataConfirmation.CONFIRMED,
            impedance_confirmation=DataConfirmation.CONFIRMED,
        ),
        LineConstructionSegment(
            LineConstructionSegmentId("segment.auto.cable"),
            LineKind.CABLE,
            600_000,
            {"conductor_mark": "АПвП", "r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.1},
            length_confirmation=DataConfirmation.CONFIRMED,
            impedance_confirmation=DataConfirmation.CONFIRMED,
        ),
    )
    _, _, section = _line(
        model,
        length_mm=1_000_000,
        construction_segments=source_segments,
    )

    split = model.split_line_section(section.equipment_id, None)
    branches = (
        model.line_sections[split.first_section_id],
        model.line_sections[split.second_section_id],
    )

    assert model.electrical_nodes[split.tap_node_id].extensions == {
        "junction_kind": "line_tap",
        "creation_origin": "automatic_line_split",
        "physical_offset_mm": None,
        "physical_position_confirmed": False,
    }
    assert all(item.length_mm is None for item in branches)
    assert all(
        segment.length_mm is None
        and segment.length_confirmation is DataConfirmation.UNCONFIRMED
        and segment.impedance_confirmation is DataConfirmation.UNCONFIRMED
        for branch in branches
        for segment in branch.construction_segments
    )
    for side, branch in zip(("before", "after"), branches, strict=True):
        provenance = branch.extensions["unconfirmed_physical_split"]
        assert provenance["physical_position_confirmed"] is False
        assert provenance["source_total_length_mm"] == 1_000_000
        assert [
            item["length_mm"] for item in provenance["source_construction_segments"]
        ] == [400_000, 600_000]
        assert (
            branch.construction_segments[0]
            .extensions["unconfirmed_physical_split"]["side"]
            == side
        )

    adapted = adapt_to_calculation(model)
    for equipment_id in (split.first_section_id, split.second_section_id):
        branch_ids = adapted.trace.domain_equipment_to_legacy[equipment_id.value]
        assert branch_ids
        assert all(
            adapted.network.branches[item].calculation_block_reason
            for item in branch_ids
        )
