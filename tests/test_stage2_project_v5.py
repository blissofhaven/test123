# -*- coding: utf-8 -*-
"""Stage-2 service availability and project-v5 migration tests."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rza_calc.adapters import LegacyCalculationAdapterError, adapt_to_calculation
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID,
    LineBranch,
    Load,
    Mode,
    Network,
    Node,
    SourceBranch,
)
from rza_calc.domain import ProjectStructure
from rza_calc.domain.catalog import CatalogEntryId, RECLOSERS, builtin_system_catalog
from rza_calc.domain.electrical import (
    ConnectionId,
    DomainInvariantError,
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
from rza_calc.io.electrical_model import (
    ElectricalModelFormatError,
    electrical_model_from_dict,
    electrical_model_to_dict,
)
from rza_calc.io.project import (
    FORMAT_VERSION,
    ProjectData,
    SUPPORTED_FORMAT_VERSIONS,
    _migrate,
    load_project,
    save as save_legacy_project,
    save_project,
)
from rza_calc.topology import TopologyEngine


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_V3 = ROOT / "rza_calc" / "examples" / "ps_severnaya.json"
AVAILABILITY_CAPABILITY = "equipment.availability"
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _model_with_load_and_bus() -> tuple[ElectricalModel, EquipmentId, EquipmentId]:
    model = ElectricalModel.with_builtins("Availability")
    load, _ = model.create_equipment(
        "builtin.load",
        "Нагрузка",
        equipment_id=EquipmentId("load"),
        port_ids_by_role={"terminal": PortId("load_terminal")},
    )
    bus, _ = model.create_equipment(
        "builtin.busbar",
        "Шина",
        equipment_id=EquipmentId("bus"),
        port_ids_by_role={"terminal": PortId("bus_terminal")},
    )
    return model, load.id, bus.id


def _write_json(tmp_path: Path, raw: dict, name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_availability_defaults_empty_is_immutable_and_keeps_positional_api():
    state = OperatingState(
        OperatingStateId("normal"),
        "Нормальный",
        {},
        "max",
        "Описание",
        {"legacy": {"kept": True}},
    )

    assert state.availability == {}
    assert state.extensions["legacy"]["kept"] is True
    with pytest.raises(TypeError):
        state.availability[EquipmentId("load")] = (  # type: ignore[index]
            EquipmentAvailability.OUT_OF_SERVICE
        )


def test_native_capabilities_and_legacy_switch_semantics_are_independent():
    model = ElectricalModel.with_builtins("Capabilities")
    supported = {
        "builtin.external_grid",
        "builtin.generator",
        "builtin.line",
        "builtin.cable",
        "builtin.circuit_breaker",
        "builtin.transformer_2w",
        "builtin.transformer_3w",
        "builtin.load",
        "builtin.line_section.overhead",
        "builtin.line_section.cable",
        "builtin.recloser",
    }
    for type_id in supported:
        definition = model.equipment_type(EquipmentTypeId(type_id))
        assert AVAILABILITY_CAPABILITY in definition.capabilities
    for type_id in ("builtin.busbar", "builtin.connection_point"):
        definition = model.equipment_type(EquipmentTypeId(type_id))
        assert AVAILABILITY_CAPABILITY not in definition.capabilities

    native_switch = model.equipment_type(EquipmentTypeId("builtin.circuit_breaker"))
    legacy_definition = replace(
        native_switch,
        id=EquipmentTypeId("compat.test.legacy_switch"),
        behavior_key="legacy.tie",
        capabilities=frozenset({"legacy.switch.position"}),
    )
    model.register_equipment_type(legacy_definition)
    legacy, _ = model.create_equipment(
        legacy_definition.id,
        "Legacy switch",
        equipment_id=EquipmentId("legacy_switch"),
        port_ids_by_role={"a": PortId("legacy_a"), "b": PortId("legacy_b")},
        normal_position=SwitchPosition.CLOSED,
    )
    state = OperatingState(
        OperatingStateId("legacy_mode"),
        "Legacy mode",
        {legacy.id: SwitchPosition.OPEN},
    )
    model.add_operating_state(state)
    revision = model.revision

    with pytest.raises(DomainInvariantError):
        model.set_equipment_availability(
            state.id, legacy.id, EquipmentAvailability.OUT_OF_SERVICE
        )
    assert model.revision == revision
    assert model.operating_states[state.id].positions[legacy.id] == SwitchPosition.OPEN
    assert model.operating_states[state.id].availability == {}


def test_availability_command_is_validated_atomic_and_orthogonal_to_position():
    model = ElectricalModel.with_builtins("Command")
    breaker, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "QF",
        equipment_id=EquipmentId("qf"),
        port_ids_by_role={"a": PortId("qf_a"), "b": PortId("qf_b")},
        normal_position=SwitchPosition.CLOSED,
    )
    bus, _ = model.create_equipment(
        "builtin.busbar",
        "Шина",
        equipment_id=EquipmentId("bus"),
        port_ids_by_role={"terminal": PortId("bus_terminal")},
    )
    state = OperatingState(
        OperatingStateId("repair"),
        "Ремонт",
        {breaker.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(state)

    change = model.set_equipment_availability(
        state.id, breaker.id, "OUT_OF_SERVICE"
    )
    restored = model.operating_states[state.id]
    assert restored.availability[breaker.id] == EquipmentAvailability.OUT_OF_SERVICE
    assert restored.positions[breaker.id] == SwitchPosition.CLOSED
    assert change.changed_ids == (state.id.value, breaker.id.value)

    revision = model.revision
    snapshot = restored
    for state_id, equipment_id, value in (
        (state.id, bus.id, EquipmentAvailability.OUT_OF_SERVICE),
        (state.id, EquipmentId("missing"), EquipmentAvailability.OUT_OF_SERVICE),
        (OperatingStateId("missing"), breaker.id, EquipmentAvailability.OUT_OF_SERVICE),
        (state.id, breaker.id, "BROKEN"),
    ):
        with pytest.raises(DomainInvariantError):
            model.set_equipment_availability(state_id, equipment_id, value)
        assert model.revision == revision
        assert model.operating_states[state.id] == snapshot


def test_state_roundtrip_and_equipment_removal_cleanup_both_state_maps():
    model = ElectricalModel.with_builtins("Roundtrip")
    breaker, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "QF",
        equipment_id=EquipmentId("qf"),
        port_ids_by_role={"a": PortId("qf_a"), "b": PortId("qf_b")},
        normal_position=SwitchPosition.CLOSED,
    )
    state = OperatingState(
        OperatingStateId("repair"),
        "Ремонт",
        {breaker.id: SwitchPosition.OPEN},
        "min",
        "Выведен в ремонт",
        {"audit": "kept"},
        {breaker.id: EquipmentAvailability.OUT_OF_SERVICE},
    )
    model.add_operating_state(state)

    raw = electrical_model_to_dict(model)
    assert raw["operating_states"][0]["availability"] == {
        breaker.id.value: "OUT_OF_SERVICE"
    }
    restored = electrical_model_from_dict(raw)
    restored_state = restored.operating_states[state.id]
    assert restored_state.positions[breaker.id] == SwitchPosition.OPEN
    assert (
        restored_state.availability[breaker.id]
        == EquipmentAvailability.OUT_OF_SERVICE
    )
    assert restored_state.extensions["audit"] == "kept"

    change = restored.remove_equipment(breaker.id)
    cleaned = restored.operating_states[state.id]
    assert cleaned.positions == {}
    assert cleaned.availability == {}
    assert state.id.value in change.changed_ids
    assert restored.validate_integrity() == []


def test_add_state_rejects_dangling_and_unsupported_availability_atomically():
    model, load_id, bus_id = _model_with_load_and_bus()
    revision = model.revision
    for state in (
        OperatingState(
            OperatingStateId("dangling"),
            "Dangling",
            availability={
                EquipmentId("missing"): EquipmentAvailability.OUT_OF_SERVICE
            },
        ),
        OperatingState(
            OperatingStateId("unsupported"),
            "Unsupported",
            availability={bus_id: EquipmentAvailability.OUT_OF_SERVICE},
        ),
    ):
        with pytest.raises(DomainInvariantError):
            model.add_operating_state(state)
        assert model.revision == revision
        assert state.id not in model.operating_states

    model.add_operating_state(
        OperatingState(
            OperatingStateId("valid"),
            "Valid",
            availability={load_id: EquipmentAvailability.OUT_OF_SERVICE},
        )
    )


def test_v5_codec_rejects_unknown_invalid_dangling_and_unsupported_availability():
    model, load_id, bus_id = _model_with_load_and_bus()
    state = OperatingState(
        OperatingStateId("mode"),
        "Mode",
        availability={load_id: EquipmentAvailability.OUT_OF_SERVICE},
    )
    model.add_operating_state(state)
    valid = electrical_model_to_dict(model)

    unknown = deepcopy(valid)
    unknown["operating_states"][0]["availability_note"] = "future"
    with pytest.raises(ElectricalModelFormatError):
        electrical_model_from_dict(unknown)

    invalid = deepcopy(valid)
    invalid["operating_states"][0]["availability"][load_id.value] = "BROKEN"
    with pytest.raises(ElectricalModelFormatError):
        electrical_model_from_dict(invalid)

    dangling = deepcopy(valid)
    dangling["operating_states"][0]["availability"] = {
        "missing": "OUT_OF_SERVICE"
    }
    with pytest.raises(ElectricalModelFormatError):
        electrical_model_from_dict(dangling)

    unsupported = deepcopy(valid)
    unsupported["operating_states"][0]["availability"] = {
        bus_id.value: "OUT_OF_SERVICE"
    }
    with pytest.raises(ElectricalModelFormatError):
        electrical_model_from_dict(unsupported)


def test_v4_to_v6_migration_is_sequential_idempotent_and_preserves_catalog(tmp_path):
    project = load_project(EXAMPLE_V3)
    system_entry = builtin_system_catalog().by_category(RECLOSERS)[0]
    instance = system_entry.create_equipment_instance(
        EquipmentId("catalog_recloser"),
        (PortId("catalog_recloser_a"), PortId("catalog_recloser_b")),
    )
    catalog_entry = project.user_catalog.save_equipment_instance(
        instance,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.recloser.v5-migration"),
        display_name="Миграционный реклоузер",
        source="Stage 2 test",
        modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    v5_path = tmp_path / "source-v5.json"
    save_project(v5_path, project)
    raw_v4 = json.loads(v5_path.read_text(encoding="utf-8"))
    raw_v4["format_version"] = 4
    for key in ("diagram", "catalog_snapshots", "migration_journal"):
        raw_v4.pop(key)
    for state in raw_v4["electrical_model"]["operating_states"]:
        state.pop("availability")
    for definition in raw_v4["electrical_model"]["equipment_types"]:
        definition["capabilities"] = [
            item
            for item in definition["capabilities"]
            if item != AVAILABILITY_CAPABILITY
        ]
    custom_type = deepcopy(next(
        item
        for item in raw_v4["electrical_model"]["equipment_types"]
        if item["id"] == "builtin.external_grid"
    ))
    custom_type["id"] = "custom.external_grid"
    custom_type["capabilities"] = []
    raw_v4["electrical_model"]["equipment_types"].append(custom_type)

    untouched = deepcopy(raw_v4)
    migrated = _migrate(raw_v4, 4)
    assert raw_v4 == untouched
    assert migrated["format_version"] == 7
    assert _migrate(migrated, 7) == migrated
    assert migrated["catalogs"] == raw_v4["catalogs"]
    assert all(
        state["availability"] == {}
        for state in migrated["electrical_model"]["operating_states"]
    )
    definitions = {
        item["id"]: item
        for item in migrated["electrical_model"]["equipment_types"]
    }
    assert AVAILABILITY_CAPABILITY in definitions["builtin.external_grid"][
        "capabilities"
    ]
    assert AVAILABILITY_CAPABILITY not in definitions["custom.external_grid"][
        "capabilities"
    ]
    compat_definition_ids = {
        "compat.rza_calc.source",
        "compat.rza_calc.generator",
        "compat.rza_calc.line",
        "compat.rza_calc.transformer_2w",
        "compat.rza_calc.tie",
        "compat.rza_calc.branch",
        "compat.rza_calc.transformer_3w",
        "compat.rza_calc.load",
    }
    assert compat_definition_ids <= set(definitions)
    assert all(
        AVAILABILITY_CAPABILITY in definitions[type_id]["capabilities"]
        for type_id in compat_definition_ids
    )

    v4_path = _write_json(tmp_path, raw_v4, "source-v4.json")
    reopened = load_project(v4_path)
    assert reopened.source_format_version == 4
    assert catalog_entry.id in reopened.user_catalog.entries
    assert all(not state.availability for state in reopened.electrical_model.operating_states.values())
    assert (
        AVAILABILITY_CAPABILITY
        in reopened.electrical_model.equipment_type(
            EquipmentTypeId("builtin.external_grid")
        ).capabilities
    )
    assert (
        AVAILABILITY_CAPABILITY
        not in reopened.electrical_model.equipment_type(
            EquipmentTypeId("custom.external_grid")
        ).capabilities
    )
    compatibility_equipment = next(
        equipment
        for equipment in reopened.electrical_model.equipment.values()
        if equipment.type_id.value == "compat.rza_calc.line"
    )
    compatibility_state_id = next(iter(reopened.electrical_model.operating_states))
    reopened.electrical_model.set_equipment_availability(
        compatibility_state_id,
        compatibility_equipment.id,
        EquipmentAvailability.OUT_OF_SERVICE,
    )
    assert (
        reopened.electrical_model.operating_states[
            compatibility_state_id
        ].availability[compatibility_equipment.id]
        == EquipmentAvailability.OUT_OF_SERVICE
    )

    roundtrip = tmp_path / "roundtrip-v5.json"
    save_project(roundtrip, reopened)
    saved = json.loads(roundtrip.read_text(encoding="utf-8"))
    assert saved["format_version"] == FORMAT_VERSION == 7
    assert all(
        "availability" in state
        for state in saved["electrical_model"]["operating_states"]
    )
    assert saved["catalogs"] == raw_v4["catalogs"]
    assert load_project(roundtrip).source_format_version == 7
    assert SUPPORTED_FORMAT_VERSIONS == {1, 2, 3, 4, 5, 6, 7}


def test_v4_migration_rejects_premature_availability_field(tmp_path):
    project = load_project(EXAMPLE_V3)
    valid = tmp_path / "valid-v5.json"
    save_project(valid, project)
    raw = json.loads(valid.read_text(encoding="utf-8"))
    raw["format_version"] = 4
    # v4 cannot silently smuggle a v5 service-state decision.
    with pytest.raises(ValueError):
        load_project(_write_json(tmp_path, raw, "premature-v4.json"))


def test_v2_availability_roundtrip_and_project_view_refresh(tmp_path):
    network = Network("Legacy availability")
    network.add_node(Node("A", "A", 10.0))
    network.add_node(Node("B", "B", 10.0))
    network.add_branch(SourceBranch(
        "S", "Source", GRID, "A", s_kz_max=500.0, s_kz_min=300.0
    ))
    network.add_branch(LineBranch(
        "L", "Line", "A", "B", length_km=1.0, r0=0.4, x0=0.3
    ))
    network.add_load(Load("LD", "Load", "B", p_kw=100.0))
    network.add_mode(Mode(
        "repair",
        "Repair",
        availability={"S": False, "L": False, "LD": False},
    ))
    path = tmp_path / "legacy-v2.json"
    save_legacy_project(path, network)

    project = load_project(path)
    state = next(iter(project.electrical_model.operating_states.values()))
    by_legacy_id = {
        equipment.extensions["legacy_calculation"]["legacy_id"]: equipment
        for equipment in project.electrical_model.equipment.values()
    }
    assert set(state.availability) == {
        by_legacy_id[token].id for token in ("S", "L", "LD")
    }
    first_network = project.network
    first_mode = first_network.modes["repair"]
    assert not first_mode.is_available("S")
    assert not first_mode.is_available("L")
    assert not first_mode.is_available("LD")

    project.electrical_model.set_equipment_availability(
        state.id,
        by_legacy_id["S"].id,
        EquipmentAvailability.IN_SERVICE,
    )
    refreshed_network = project.network
    assert refreshed_network is not first_network
    assert refreshed_network.modes["repair"].is_available("S")
    assert not refreshed_network.modes["repair"].is_available("L")
    assert not refreshed_network.modes["repair"].is_available("LD")


def test_project_load_and_save_use_topology_inferred_voltage(tmp_path):
    model = ElectricalModel.with_builtins("Inferred voltage project")
    upstream = ElectricalNode(
        ElectricalNodeId("node.inferred.upstream"),
        "Upstream",
        declared_voltage_class_id=U10,
    )
    downstream = ElectricalNode(
        ElectricalNodeId("node.inferred.downstream"),
        "Downstream",
        declared_voltage_class_id=None,
    )
    model.add_node(upstream)
    model.add_node(downstream)
    source, _ = model.create_equipment(
        "builtin.external_grid",
        "Source",
        equipment_id=EquipmentId("equipment.inferred.source"),
        port_ids_by_role={"terminal": PortId("port.inferred.source")},
        voltage_class_by_group={"main": U10},
        properties={"s_kz_max": 500.0, "s_kz_min": 300.0},
    )
    line, _ = model.create_equipment(
        "builtin.line",
        "Line",
        equipment_id=EquipmentId("equipment.inferred.line"),
        port_ids_by_role={
            "from": PortId("port.inferred.line.from"),
            "to": PortId("port.inferred.line.to"),
        },
        voltage_class_by_group={"main": U10},
        properties={"length_km": 1.0, "r0": 0.4, "x0": 0.3},
    )
    for equipment_id, role, node_id in (
        (source.id, "terminal", upstream.id),
        (line.id, "from", upstream.id),
        (line.id, "to", downstream.id),
    ):
        model.connect_port(
            model.port_by_role(equipment_id, role).id,
            node_id,
            connection_id=ConnectionId(f"connection.{equipment_id.value}.{role}"),
        )
    model.add_operating_state(OperatingState(OperatingStateId("base"), "Base"))
    snapshot = TopologyEngine().compile(model)
    project = ProjectData(
        adapt_to_calculation(model, snapshot).network,
        Methodology.load(),
        {"name": model.name},
        ProjectStructure(),
        FORMAT_VERSION,
        model,
    )
    path = tmp_path / "inferred-voltage-v5.json"

    save_project(path, project)
    reopened = load_project(path)

    restored_downstream = reopened.electrical_model.electrical_nodes[downstream.id]
    assert restored_downstream.declared_voltage_class_id is None
    calculation_node = next(
        node for node in reopened.network.nodes.values()
        if node.name == "Downstream"
    )
    assert calculation_node.u_nom == 10.0


def test_project_adapter_rejects_any_ambiguous_registered_mode(tmp_path):
    model = ElectricalModel.with_builtins("All modes must be valid")
    first = ElectricalNode(
        ElectricalNodeId("node.modes.first"),
        "First",
        declared_voltage_class_id=U10,
    )
    second = ElectricalNode(
        ElectricalNodeId("node.modes.second"),
        "Second",
        declared_voltage_class_id=U10,
    )
    model.add_node(first)
    model.add_node(second)
    switch, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "QF",
        equipment_id=EquipmentId("equipment.modes.qf"),
        port_ids_by_role={
            "a": PortId("port.modes.qf.a"),
            "b": PortId("port.modes.qf.b"),
        },
        voltage_class_by_group={"main": U10},
        normal_position=None,
    )
    for role, node in (("a", first), ("b", second)):
        model.connect_port(
            model.port_by_role(switch.id, role).id,
            node.id,
            connection_id=ConnectionId(f"connection.modes.qf.{role}"),
        )
    valid_state = OperatingState(
        OperatingStateId("state.modes.valid"),
        "Valid",
        {switch.id: SwitchPosition.CLOSED},
    )
    ambiguous_state = OperatingState(
        OperatingStateId("state.modes.ambiguous"),
        "Ambiguous",
    )
    model.add_operating_state(valid_state)
    model.add_operating_state(ambiguous_state)
    valid_snapshot = TopologyEngine().compile(model, valid_state.id)
    project = ProjectData(
        adapt_to_calculation(model, valid_snapshot).network,
        Methodology.load(),
        {"name": model.name},
        ProjectStructure(),
        FORMAT_VERSION,
        model,
    )

    with pytest.raises(LegacyCalculationAdapterError) as error:
        save_project(tmp_path / "must-not-save.json", project)
    assert any(
        item.code == "topology.ambiguous_switch_position"
        for item in error.value.diagnostics
    )
