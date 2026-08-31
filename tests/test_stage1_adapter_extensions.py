# -*- coding: utf-8 -*-
"""Adapter-only coverage for canonical reclosers and line sections."""
from dataclasses import replace
from types import MethodType

import pytest

from rza_calc.adapters.legacy_calculation import (
    LegacyCalculationAdapterError,
    adapt_to_calculation,
)
from rza_calc.core.model import LineBranch, TieBranch
from rza_calc.domain.electrical import (
    AC_POWER,
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    LineKind,
    OperatingState,
    OperatingStateId,
    PortDefinition,
    PortId,
    PropertyDefinition,
    SwitchPosition,
    VoltageClassId,
)


VOLTAGE = VoltageClassId("builtin.voltage.ac.10kv")


def _model_with_two_nodes(name: str = "Adapter extensions") -> ElectricalModel:
    model = ElectricalModel.with_builtins(name)
    model.add_node(ElectricalNode(
        ElectricalNodeId("node_a"),
        "A",
        declared_voltage_class_id=VOLTAGE,
    ))
    model.add_node(ElectricalNode(
        ElectricalNodeId("node_b"),
        "B",
        declared_voltage_class_id=VOLTAGE,
    ))
    return model


def _register_two_port_type(
    model: ElectricalModel,
    *,
    type_id: str,
    behavior_key: str,
    roles: tuple[str, str],
    capabilities: frozenset[str] = frozenset(),
) -> EquipmentTypeDefinition:
    definition = EquipmentTypeDefinition(
        EquipmentTypeId(type_id),
        1,
        type_id,
        behavior_key,
        tuple(
            PortDefinition(role, role, AC_POWER, voltage_group="main")
            for role in roles
        ),
        capabilities=capabilities,
    )
    model.register_equipment_type(definition)
    return definition


def _create_and_connect(
    model: ElectricalModel,
    definition: EquipmentTypeDefinition,
    *,
    equipment_id: str,
    roles: tuple[str, str],
    properties: dict,
    normal_position: SwitchPosition | None = None,
    allow_unowned_line_section: bool = False,
):
    equipment, _ = model.create_equipment(
        definition.id,
        equipment_id,
        equipment_id=EquipmentId(equipment_id),
        port_ids_by_role={
            role: PortId(f"{equipment_id}_{role}") for role in roles
        },
        properties=properties,
        voltage_class_by_group={"main": VOLTAGE},
        normal_position=normal_position,
        _allow_unowned_line_section=allow_unowned_line_section,
    )
    for role, node_id in zip(roles, ("node_a", "node_b")):
        model.connect_port(
            model.port_by_role(equipment.id, role).id,
            ElectricalNodeId(node_id),
            connection_id=ConnectionId(f"{equipment_id}_{role}_connection"),
        )
    return equipment


def test_recloser_uses_explicit_whitelist_and_equipment_state():
    model = _model_with_two_nodes("Recloser")
    definition = model.equipment_type(
        EquipmentTypeId("builtin.recloser"), 1
    )
    recloser = _create_and_connect(
        model,
        definition,
        equipment_id="recloser_1",
        roles=("a", "b"),
        properties={
            "manufacturer": "Vendor",
            "model": "R-10",
            "rated_current_a": 630.0,
            "rated_breaking_current_a": 12_500.0,
            "full_opening_time_s": 0.08,
            "auto_reclose_settings": {"dead_times_s": [0.5, 5.0]},
            "custom_parameters": {
                "future_catalog_property": "preserve only in Domain"
            },
        },
        normal_position=SwitchPosition.CLOSED,
    )
    model.add_operating_state(OperatingState(
        OperatingStateId("opened"),
        "Open",
        {recloser.id: SwitchPosition.OPEN},
    ))
    model.add_operating_state(OperatingState(
        OperatingStateId("closed"),
        "Closed",
        {recloser.id: SwitchPosition.CLOSED},
    ))

    result = adapt_to_calculation(model)
    branch_id = result.trace.domain_equipment_to_legacy[recloser.id.value][0]
    branch = result.network.branches[branch_id]
    modes = {mode.name: mode for mode in result.network.modes.values()}

    assert isinstance(branch, TieBranch)
    assert branch.switchable is True
    assert branch.normally_closed is True
    assert branch.breaker_t_off == pytest.approx(0.08)
    assert not hasattr(branch, "rated_current_a")
    assert not result.network.branch_conducting(branch, modes["Open"])
    assert result.network.branch_conducting(branch, modes["Closed"])
    assert modes["Open"].states == {branch_id: False}
    assert any(
        diagnostic.code == "recloser_properties_not_used"
        for diagnostic in result.diagnostics
    )


def test_recloser_requires_normal_position_at_the_adapter_boundary():
    model = _model_with_two_nodes("Invalid recloser")
    definition = _register_two_port_type(
        model,
        type_id="user.invalid_recloser",
        behavior_key="recloser",
        roles=("a", "b"),
        capabilities=frozenset({"switch.position"}),
    )
    recloser = _create_and_connect(
        model,
        definition,
        equipment_id="invalid_recloser",
        roles=("a", "b"),
        properties={},
        normal_position=SwitchPosition.CLOSED,
    )
    # Simulate the pre-LineSection/recloser-invariant Domain revision: the
    # adapter keeps its own boundary check and must not depend on that newer
    # aggregate validation being present.
    model._equipment[recloser.id] = replace(recloser, normal_position=None)
    model.validate_integrity = MethodType(lambda self: [], model)

    with pytest.raises(LegacyCalculationAdapterError) as captured:
        adapt_to_calculation(model)

    assert [
        diagnostic.code for diagnostic in captured.value.diagnostics
    ] == ["recloser_normal_position_missing"]


def test_line_section_prefers_domain_queries_and_maps_positive_sequence_only():
    model = _model_with_two_nodes("LineSection Domain API")
    logical_line, section, _ = model.create_logical_line(
        "Cable line",
        LineKind.CABLE,
        ElectricalNodeId("node_a"),
        ElectricalNodeId("node_b"),
        1_250_000,
        inherited_properties={
            "conductor_mark": "inherited mark",
            "material": "Al",
            "parallel_count": 2,
            "r1_ohm_per_km": 0.125,
            "x1_ohm_per_km": 0.081,
            "r2_ohm_per_km": 0.126,
            "x2_ohm_per_km": 0.082,
            "r0_ohm_per_km": 0.9,
            "x0_ohm_per_km": 1.7,
            "capacitive_current_a_per_km": 1.25,
            "custom_parameters": {"future_catalog_property": 42},
        },
        section_properties={
            "conductor_mark": "АПвПу2г",
            "cross_section_mm2": 240.0,
        },
        section_equipment_id=EquipmentId("section_equipment"),
        port_ids_by_role={
            "from": PortId("section_equipment_from"),
            "to": PortId("section_equipment_to"),
        },
        connection_ids=(
            ConnectionId("section_equipment_from_connection"),
            ConnectionId("section_equipment_to_connection"),
        ),
    )
    section_equipment = model.equipment[section.equipment_id]
    assert logical_line.line_kind == LineKind.CABLE
    assert model.effective_equipment_properties(section_equipment.id)[
        "conductor_mark"
    ] == "АПвПу2г"

    result = adapt_to_calculation(model)
    branch_id = result.trace.domain_equipment_to_legacy[
        section_equipment.id.value
    ][0]
    branch = result.network.branches[branch_id]

    assert isinstance(branch, LineBranch)
    assert branch.length_km == pytest.approx(1.25)
    assert branch.line_type == "cable"
    assert branch.brand == "АПвПу2г"
    assert branch.section_mm2 == pytest.approx(240.0)
    assert branch.n_parallel == 2
    assert branch.r0 == pytest.approx(0.125)
    assert branch.x0 == pytest.approx(0.081)
    assert branch.ic_per_km == pytest.approx(1.25)
    assert branch.switchable is False
    assert any(
        diagnostic.code == "line_section_properties_not_used"
        and "r0_ohm_per_km" in diagnostic.message
        for diagnostic in result.diagnostics
    )


def test_line_section_has_safe_property_fallback_until_domain_queries_exist():
    model = _model_with_two_nodes("LineSection fallback")
    definition = _register_two_port_type(
        model,
        type_id="user.line_section_fallback",
        behavior_key="line_section",
        roles=("from", "to"),
    )
    section_equipment = _create_and_connect(
        model,
        definition,
        equipment_id="fallback_section",
        roles=("from", "to"),
        properties={
            "length_mm": 750_000,
            "line_type": "overhead",
            "conductor_mark": "АС-70",
            "cross_section_mm2": 70.0,
            "material": "Al",
            "parallel_count": 1,
            "r1_ohm_per_km": 0.42,
            "x1_ohm_per_km": 0.4,
        },
        allow_unowned_line_section=True,
    )
    # Emulate the older Domain surface which had neither the aggregate queries
    # nor the aggregate integrity rule.  Raw canonical properties are the
    # adapter's documented compatibility fallback in that environment.
    model.effective_equipment_properties = None
    model.line_section_for_equipment = None
    model.logical_line_for_section = None
    model.validate_integrity = MethodType(lambda self: [], model)

    result = adapt_to_calculation(model)
    branch_id = result.trace.domain_equipment_to_legacy[
        section_equipment.id.value
    ][0]
    branch = result.network.branches[branch_id]

    assert isinstance(branch, LineBranch)
    assert branch.length_km == pytest.approx(0.75)
    assert branch.line_type == "overhead"
    assert branch.brand == "АС-70"
    assert branch.r0 == pytest.approx(0.42)
    assert branch.x0 == pytest.approx(0.4)


def test_existing_native_line_and_switch_use_effective_properties_without_regression():
    model = _model_with_two_nodes("Existing behavior regression")
    line_definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.defaulted_line"),
        1,
        "Defaulted line",
        "line",
        (
            PortDefinition("from", "from", AC_POWER, voltage_group="main"),
            PortDefinition("to", "to", AC_POWER, voltage_group="main"),
        ),
        property_definitions=(
            PropertyDefinition("length_km", "number", default=3.0),
            PropertyDefinition("r0", "number", default=0.4),
            PropertyDefinition("x0", "number", default=0.5),
        ),
        allow_additional_properties=False,
    )
    model.register_equipment_type(line_definition)
    line, _ = model.create_equipment(
        line_definition.id,
        "Existing line",
        equipment_id=EquipmentId("existing_line"),
        port_ids_by_role={
            "from": PortId("existing_line_from"),
            "to": PortId("existing_line_to"),
        },
        voltage_class_by_group={"main": VOLTAGE},
    )
    for role, node_id in (("from", "node_a"), ("to", "node_b")):
        model.connect_port(
            model.port_by_role(line.id, role).id,
            ElectricalNodeId(node_id),
            connection_id=ConnectionId(f"existing_line_{role}_connection"),
        )
    switch, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "Existing switch",
        equipment_id=EquipmentId("existing_switch"),
        port_ids_by_role={
            "a": PortId("existing_switch_a"),
            "b": PortId("existing_switch_b"),
        },
        voltage_class_by_group={"main": VOLTAGE},
        normal_position=SwitchPosition.OPEN,
    )
    for role, node_id in (("a", "node_a"), ("b", "node_b")):
        model.connect_port(
            model.port_by_role(switch.id, role).id,
            ElectricalNodeId(node_id),
            connection_id=ConnectionId(f"existing_switch_{role}_connection"),
        )

    result = adapt_to_calculation(model)
    branch_id = result.trace.domain_equipment_to_legacy[line.id.value][0]
    branch = result.network.branches[branch_id]
    switch_branch_id = result.trace.domain_equipment_to_legacy[switch.id.value][0]
    switch_branch = result.network.branches[switch_branch_id]

    assert isinstance(branch, LineBranch)
    assert branch.length_km == pytest.approx(3.0)
    assert branch.r0 == pytest.approx(0.4)
    assert branch.x0 == pytest.approx(0.5)
    assert branch.line_type == "overhead"
    assert branch.switchable is False
    assert isinstance(switch_branch, TieBranch)
    assert switch_branch.switchable is True
    assert switch_branch.normally_closed is False
