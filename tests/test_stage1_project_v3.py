# -*- coding: utf-8 -*-
"""Stage 1 acceptance: legacy migration, v3 persistence and calculation boundary."""
from __future__ import annotations

import json
import hashlib
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.adapters import (  # noqa: E402
    LegacyCalculationAdapterError,
    adapt_to_calculation,
    import_legacy_network,
)
from rza_calc.core.model import Network  # noqa: E402
from rza_calc.domain import (  # noqa: E402
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
    thaw_json,
)
from rza_calc.io.project import (  # noqa: E402
    FORMAT_VERSION,
    ProjectFormatError,
    _load_network,
    load_project,
    save,
    save_project,
)

ROOT = Path(__file__).resolve().parent.parent
GTES = ROOT / "rza_calc" / "examples" / "gtes_sever.json"
PS = ROOT / "rza_calc" / "examples" / "ps_severnaya.json"
LEGACY_GTES = ROOT / "rza_calc" / "examples" / "gtes_sever_v1.json"
LEGACY_PS = ROOT / "rza_calc" / "examples" / "ps_severnaya_v1.json"
LEGACY_EXAMPLES = (LEGACY_PS, LEGACY_GTES)
LEGACY_SHA256 = {
    "gtes_sever_v1.json": "ac88723c7ebb6dbafea40cd857c33e2848bc75e1f7718b382fd0c24f9677af86",
    "ps_severnaya_v1.json": "140067751b657df9dec4ef691ede03e8b7e9570860144e5f7b55c5c14c9012f9",
}


def _raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_network(path: Path) -> Network:
    raw = _raw(path)
    return _load_network(raw, raw["project"]["name"])


def _write(tmp_path: Path, raw: dict, name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _legacy_id_map(model: ElectricalModel) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    stores = (
        ("node", model.electrical_nodes.values()),
        ("equipment", model.equipment.values()),
        ("state", model.operating_states.values()),
    )
    for category, items in stores:
        for item in items:
            marker = thaw_json(item.extensions).get("legacy_calculation", {})
            legacy_id = marker.get("legacy_id")
            if legacy_id is not None:
                result[(category, legacy_id)] = item.id.value
    return result


def test_legacy_adapter_is_exact_and_generated_three_winding_parts_stay_internal():
    for path in LEGACY_EXAMPLES:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == LEGACY_SHA256[path.name]
        original = _legacy_network(path)
        model = import_legacy_network(original)
        adapted = adapt_to_calculation(model).network
        assert asdict(adapted) == asdict(original), path.name
        assert adapted is not original

        generated_nodes = {
            item.star_node_id for item in original.transformers3w.values()
        }
        generated_branches = {
            branch_id
            for item in original.transformers3w.values()
            for branch_id in item.branch_ids
        }
        domain_node_legacy_ids = {
            thaw_json(item.extensions)["legacy_calculation"]["legacy_id"]
            for item in model.electrical_nodes.values()
        }
        equipment_markers = [
            thaw_json(item.extensions)["legacy_calculation"]
            for item in model.equipment.values()
        ]
        domain_equipment_legacy_ids = {
            marker["legacy_id"] for marker in equipment_markers
        }
        assert not (generated_nodes & domain_node_legacy_ids)
        assert not (
            generated_branches
            & {
                marker["legacy_id"]
                for marker in equipment_markers
                if marker["category"] == "branch"
            }
        )
        assert set(original.transformers3w) <= domain_equipment_legacy_ids
        assert "GRID" not in {item.id.value for item in model.electrical_nodes.values()}


def test_legacy_ids_and_connectivity_do_not_depend_on_names_or_dict_order():
    original = _legacy_network(LEGACY_GTES)
    changed = deepcopy(original)
    for store in (
        changed.nodes,
        changed.transformers3w,
        changed.branches,
        changed.loads,
        changed.modes,
    ):
        for item in store.values():
            item.name = "Переименовано: " + item.name
    changed.nodes = dict(reversed(tuple(changed.nodes.items())))
    changed.transformers3w = dict(reversed(tuple(changed.transformers3w.items())))
    changed.branches = dict(reversed(tuple(changed.branches.items())))
    changed.loads = dict(reversed(tuple(changed.loads.items())))
    changed.modes = dict(reversed(tuple(changed.modes.items())))

    first = import_legacy_network(original)
    second = import_legacy_network(changed)
    assert _legacy_id_map(first) == _legacy_id_map(second)
    assert first.connectivity_signature() == second.connectivity_signature()

    first_net = adapt_to_calculation(first).network
    second_net = adapt_to_calculation(second).network
    assert {
        key: (item.node_from, item.node_to, item.kind)
        for key, item in first_net.branches.items()
    } == {
        key: (item.node_from, item.node_to, item.kind)
        for key, item in second_net.branches.items()
    }
    assert {
        key: item.states for key, item in first_net.modes.items()
    } == {
        key: item.states for key, item in second_net.modes.items()
    }


def test_v1_to_v3_save_reload_is_idempotent_and_does_not_touch_source(tmp_path):
    source_bytes = LEGACY_GTES.read_bytes()
    first_project = load_project(LEGACY_GTES)
    first_ids = _legacy_id_map(first_project.electrical_model)
    first_path = tmp_path / "first-v3.json"
    save_project(
        first_path,
        first_project,
        methodology_file=str(first_project.methodology.path),
    )
    reopened = load_project(first_path)
    second_path = tmp_path / "second-v3.json"
    save_project(
        second_path,
        reopened,
        methodology_file=str(reopened.methodology.path),
    )

    assert _raw(first_path) == _raw(second_path)
    assert reopened.source_format_version == FORMAT_VERSION == 7
    assert _legacy_id_map(reopened.electrical_model) == first_ids
    assert LEGACY_GTES.read_bytes() == source_bytes


def test_v2_legacy_writer_migrates_to_v3_without_parallel_network_payload(tmp_path):
    original = _legacy_network(LEGACY_PS)
    v2_path = tmp_path / "legacy-v2.json"
    save(v2_path, original)
    assert _raw(v2_path)["format_version"] == 2

    project = load_project(v2_path)
    assert project.source_format_version == 2
    assert asdict(project.network) == asdict(original)
    v3_path = tmp_path / "canonical-v3.json"
    save_project(v3_path, project)
    raw = _raw(v3_path)
    assert raw["format_version"] == 7
    assert "electrical_model" in raw
    assert not ({"nodes", "branches", "transformers3w", "loads", "modes"} & set(raw))


def test_v3_rejects_unknown_fields_and_dangling_connections(tmp_path):
    project = load_project(PS)
    path = tmp_path / "valid-v3.json"
    save_project(path, project)
    raw = _raw(path)

    unknown = deepcopy(raw)
    unknown["electrical_model"]["unknown_section"] = []
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, unknown, "unknown-v3.json"))

    dangling = deepcopy(raw)
    dangling["electrical_model"]["connections"][0]["port_id"] = "missing_port"
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, dangling, "dangling-v3.json"))

    unknown_equipment_field = deepcopy(raw)
    unknown_equipment_field["electrical_model"]["equipment"][0]["canvas_x"] = 42
    with pytest.raises(ValueError):
        load_project(_write(
            tmp_path, unknown_equipment_field, "unknown-equipment-field-v3.json"
        ))

    missing_name = deepcopy(raw)
    del missing_name["project"]["name"]
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, missing_name, "missing-project-name-v3.json"))

    mismatched_name = deepcopy(raw)
    mismatched_name["project"]["name"] = "Другое имя"
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, mismatched_name, "mismatched-name-v3.json"))


def test_save_as_rebases_relative_methodology_reference(tmp_path):
    project = load_project(PS)
    methodology_path = project.methodology.path.resolve()
    target = tmp_path / "another" / "folder" / "saved-as.json"
    target.parent.mkdir(parents=True)

    save_project(target, project)

    reference = _raw(target)["methodology"]["file"]
    resolved_reference = (target.parent / reference).resolve()
    assert resolved_reference == methodology_path
    reopened = load_project(target)
    assert reopened.methodology.path.resolve() == methodology_path


def test_project_json_rejects_non_finite_numbers_on_load_and_save(tmp_path):
    raw = _raw(PS)
    raw["project"]["nested"] = {"bad": float("nan")}
    invalid_source = _write(tmp_path, raw, "nan-source.json")
    with pytest.raises(ProjectFormatError):
        load_project(invalid_source)

    project = load_project(PS)
    project.metadata["nested"] = {"bad": float("inf")}
    invalid_target = tmp_path / "infinity-target.json"
    with pytest.raises(ProjectFormatError):
        save_project(invalid_target, project)
    assert not invalid_target.exists()


def test_load_project_exposes_calculation_adapter_diagnostics(tmp_path):
    raw = _raw(LEGACY_PS)
    raw.pop("methodology", None)
    old_id = raw["branches"][0]["id"]
    unsafe_id = "unsafe:branch"
    raw["branches"][0]["id"] = unsafe_id
    for mode in raw["modes"]:
        mode["states"] = {
            (unsafe_id if key == old_id else key): value
            for key, value in mode["states"].items()
        }
    project = load_project(_write(tmp_path, raw, "unsafe-id-v1.json"))

    assert any(
        diagnostic.code == "legacy_switch_id_unsafe"
        for diagnostic in project.adapter_diagnostics
    )


def test_unknown_electrical_behavior_is_never_silently_omitted():
    model = ElectricalModel.with_builtins("Unknown behavior")
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.unsupported"),
        1,
        "Неизвестное оборудование",
        "future.unsupported_behavior",
        (PortDefinition("terminal", "Вывод", AC_POWER),),
    )
    model.register_equipment_type(definition)
    model.create_equipment(
        definition.id,
        "X",
        equipment_id=EquipmentId("unknown_equipment"),
        port_ids_by_role={"terminal": PortId("unknown_terminal")},
    )

    with pytest.raises(LegacyCalculationAdapterError):
        adapt_to_calculation(model)


def test_adapter_rejects_supported_behavior_with_unrepresentable_extra_port():
    model = ElectricalModel.with_builtins("Line with tap")
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.line_with_tap"),
        1,
        "Линия с ответвлением",
        "line",
        (
            PortDefinition("from", "Начало", AC_POWER),
            PortDefinition("to", "Конец", AC_POWER),
            PortDefinition("tap", "Ответвление", AC_POWER),
        ),
    )
    model.register_equipment_type(definition)
    line, _ = model.create_equipment(
        definition.id,
        "Линия с ответвлением",
        equipment_id=EquipmentId("line_with_tap"),
        port_ids_by_role={
            "from": PortId("line_with_tap_from"),
            "to": PortId("line_with_tap_to"),
            "tap": PortId("line_with_tap_tap"),
        },
    )
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    for role in ("from", "to", "tap"):
        node = ElectricalNode(
            ElectricalNodeId(f"node_{role}"),
            role,
            declared_voltage_class_id=voltage,
        )
        model.add_node(node)
        model.connect_port(
            model.port_by_role(line.id, role).id,
            node.id,
            connection_id=ConnectionId(f"connection_{role}"),
        )

    with pytest.raises(LegacyCalculationAdapterError):
        adapt_to_calculation(model)


def test_remove_legacy_equipment_prunes_authoritative_raw_mode_states():
    model = import_legacy_network(_legacy_network(LEGACY_PS))
    transformer = next(
        item
        for item in model.equipment.values()
        if thaw_json(item.extensions).get("legacy_calculation", {}).get("legacy_id")
        == "T1"
    )

    model.remove_equipment(transformer.id, cascade=True)

    assert model.validate_integrity() == []
    for state in model.operating_states.values():
        raw_states = thaw_json(state.extensions)["legacy_calculation"]["states"]
        assert "T1" not in raw_states
        assert not any(key.startswith("SW:T1:") for key in raw_states)
    adapted = adapt_to_calculation(model).network
    assert "T1" not in adapted.branches
    assert all("T1" not in mode.states for mode in adapted.modes.values())
    assert adapted.validate() == []


def test_native_switch_positions_are_mapped_to_fresh_calculation_modes():
    model = ElectricalModel.with_builtins("Native switch")
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first_node = ElectricalNode(
        ElectricalNodeId("node_a"), "A", declared_voltage_class_id=voltage
    )
    second_node = ElectricalNode(
        ElectricalNodeId("node_b"), "B", declared_voltage_class_id=voltage
    )
    model.add_node(first_node)
    model.add_node(second_node)
    breaker, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "QF",
        equipment_id=EquipmentId("qf"),
        port_ids_by_role={"a": PortId("qf_a"), "b": PortId("qf_b")},
        voltage_class_by_group={"main": voltage},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(
        model.port_by_role(breaker.id, "a").id,
        first_node.id,
        connection_id=ConnectionId("qf_a_connection"),
    )
    model.connect_port(
        model.port_by_role(breaker.id, "b").id,
        second_node.id,
        connection_id=ConnectionId("qf_b_connection"),
    )
    opened = OperatingState(
        OperatingStateId("opened"),
        "Отключён",
        {breaker.id: SwitchPosition.OPEN},
    )
    closed = OperatingState(
        OperatingStateId("closed"),
        "Включён",
        {breaker.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(opened)
    model.add_operating_state(closed)

    first = adapt_to_calculation(model)
    second = adapt_to_calculation(model)
    assert first.network is not second.network
    branch = next(iter(first.network.branches.values()))
    modes = {item.name: item for item in first.network.modes.values()}
    assert not first.network.branch_conducting(branch, modes["Отключён"])
    assert first.network.branch_conducting(branch, modes["Включён"])
    assert first.trace.domain_equipment_to_legacy[breaker.id.value] == (branch.id,)
