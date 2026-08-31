# -*- coding: utf-8 -*-
"""Acceptance tests for the canonical electrical Domain Model (Stage 1)."""
from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.domain.electrical import (  # noqa: E402
    AC_POWER,
    Connection,
    ConnectionId,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    OperatingState,
    OperatingStateId,
    PortId,
    PortDefinition,
    PortInstance,
    PropertyDefinition,
    SwitchPosition,
    VoltageClass,
    VoltageClassId,
    deterministic_id,
)
from rza_calc.io.electrical_model import (  # noqa: E402
    ElectricalModelFormatError,
    electrical_model_from_dict,
    electrical_model_to_dict,
)


def _model() -> ElectricalModel:
    return ElectricalModel.with_builtins("Stage 1 acceptance")


def _voltage(model: ElectricalModel, nominal_voltage_v: int) -> VoltageClass:
    return model.ensure_voltage_class(nominal_voltage_v)


def _node(
    model: ElectricalModel,
    node_id: str,
    nominal_voltage_v: int | None = None,
) -> ElectricalNode:
    voltage_id = (
        _voltage(model, nominal_voltage_v).id
        if nominal_voltage_v is not None
        else None
    )
    node = ElectricalNode(
        ElectricalNodeId(node_id),
        node_id,
        declared_voltage_class_id=voltage_id,
    )
    model.add_node(node)
    return node


def _create(
    model: ElectricalModel,
    type_id: str,
    equipment_id: str,
    name: str,
    roles: tuple[str, ...],
    *,
    voltages: dict[str, VoltageClassId] | None = None,
    normal_position: SwitchPosition | None = None,
):
    port_ids = {
        role: PortId(f"{equipment_id}_{role}")
        for role in roles
    }
    equipment, _ = model.create_equipment(
        type_id,
        name,
        equipment_id=EquipmentId(equipment_id),
        port_ids_by_role=port_ids,
        voltage_class_by_group=voltages,
        normal_position=normal_position,
    )
    return equipment


def _snapshot(model: ElectricalModel) -> tuple:
    def ids(items) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in items))

    states = tuple(sorted(
        (
            state_id.value,
            tuple(sorted(
                (equipment_id.value, position.value)
                for equipment_id, position in state.positions.items()
            )),
        )
        for state_id, state in model.operating_states.items()
    ))
    return (
        model.revision,
        ids(model.equipment),
        ids(model.ports),
        ids(model.electrical_nodes),
        ids(model.connections),
        ids(model.operating_states),
        states,
        model.connectivity_signature(),
    )


def test_stable_ids_are_unique_immutable_and_deterministic():
    generated = [EquipmentId.new() for _ in range(256)]
    assert len(generated) == len(set(generated))
    assert all(item.value.startswith("eq_") for item in generated)
    assert all(":" not in item.value for item in generated)

    first = deterministic_id(EquipmentId, "legacy", "branch-1")
    second = deterministic_id(EquipmentId, "legacy", "branch-1")
    other = deterministic_id(EquipmentId, "legacy", "branch-2")
    assert first == second
    assert first != other
    assert deterministic_id(EquipmentId, "a/b", "c") != deterministic_id(
        EquipmentId, "a", "b/c"
    )

    with pytest.raises(FrozenInstanceError):
        first.value = "renamed"  # type: ignore[misc]


def test_cross_kind_duplicate_and_grid_ids_are_rejected_atomically():
    model = _model()
    model.add_node(ElectricalNode(ElectricalNodeId("shared"), "Шины"))
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        _create(
            model,
            "builtin.line",
            "shared",
            "Линия с конфликтующим ID",
            ("from", "to"),
        )
    assert _snapshot(model) == before

    with pytest.raises(DomainInvariantError):
        model.add_node(ElectricalNode(ElectricalNodeId("GRID"), "GRID"))
    assert _snapshot(model) == before


def test_builtin_equipment_has_expected_stable_owned_ports():
    model = _model()
    expected = {
        "builtin.external_grid": ("terminal",),
        "builtin.generator": ("terminal",),
        "builtin.busbar": ("terminal",),
        "builtin.connection_point": ("terminal",),
        "builtin.line": ("from", "to"),
        "builtin.cable": ("from", "to"),
        "builtin.circuit_breaker": ("a", "b"),
        "builtin.transformer_2w": ("hv", "lv"),
        "builtin.transformer_3w": ("hv", "mv", "lv"),
        "builtin.load": ("terminal",),
    }

    all_ids: set[str] = set()
    for index, (type_id, roles) in enumerate(expected.items()):
        equipment_id = f"equipment_{index}"
        normal = (
            SwitchPosition.CLOSED
            if type_id == "builtin.circuit_breaker"
            else None
        )
        equipment = _create(
            model, type_id, equipment_id, type_id, roles,
            normal_position=normal,
        )
        first_read = model.ports_of(equipment.id)
        second_read = model.ports_of(equipment.id)
        assert tuple(item.role for item in first_read) == roles
        assert tuple(item.id for item in first_read) == equipment.port_ids
        assert tuple(item.id for item in second_read) == equipment.port_ids
        assert all(item.equipment_id == equipment.id for item in first_read)
        all_ids.add(equipment.id.value)
        all_ids.update(item.id.value for item in first_read)

    expected_id_count = len(model.equipment) + len(model.ports)
    assert len(all_ids) == expected_id_count
    assert model.validate_integrity() == []


def test_add_equipment_rejects_wrong_port_owner_atomically():
    model = _model()
    equipment_id = EquipmentId("line")
    ports = (
        PortInstance(PortId("line_from"), EquipmentId("wrong_owner"), "from"),
        PortInstance(PortId("line_to"), equipment_id, "to"),
    )
    equipment = EquipmentInstance(
        equipment_id,
        EquipmentTypeId("builtin.line"),
        1,
        "Линия",
        tuple(item.id for item in ports),
    )
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.add_equipment_instance(equipment, ports)
    assert _snapshot(model) == before


def test_runtime_typed_ids_cannot_cross_entity_boundaries():
    model = _model()
    wrong_equipment_id = PortId("wrong_runtime_type")
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        PortInstance(
            PortId("typed_from"),
            wrong_equipment_id,  # type: ignore[arg-type]
            "from",
        )
    with pytest.raises(DomainInvariantError):
        EquipmentInstance(
            wrong_equipment_id,  # type: ignore[arg-type]
            EquipmentTypeId("builtin.line"),
            1,
            "Линия с неверным типом ID",
            (PortId("typed_from"), PortId("typed_to")),
        )
    assert _snapshot(model) == before


def test_connection_missing_references_are_rejected_atomically():
    model = _model()
    node = _node(model, "bus")
    load = _create(
        model, "builtin.load", "load", "Нагрузка", ("terminal",)
    )
    port = model.port_by_role(load.id, "terminal")

    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.add_connection(Connection(
            ConnectionId("missing_port_connection"),
            PortId("missing_port"),
            node.id,
        ))
    assert _snapshot(model) == before

    with pytest.raises(DomainInvariantError):
        model.add_connection(Connection(
            ConnectionId("missing_node_connection"),
            port.id,
            ElectricalNodeId("missing_node"),
        ))
    assert _snapshot(model) == before


def test_connection_capacity_and_duplicate_id_are_rejected_atomically():
    model = _model()
    first_node = _node(model, "bus_1")
    second_node = _node(model, "bus_2")
    first_load = _create(
        model, "builtin.load", "load_1", "Нагрузка 1", ("terminal",)
    )
    second_load = _create(
        model, "builtin.load", "load_2", "Нагрузка 2", ("terminal",)
    )
    first_port = model.port_by_role(first_load.id, "terminal")
    second_port = model.port_by_role(second_load.id, "terminal")
    connection = Connection(ConnectionId("connection"), first_port.id, first_node.id)
    model.add_connection(connection)

    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.add_connection(Connection(
            ConnectionId("second_connection"), first_port.id, second_node.id
        ))
    assert _snapshot(model) == before

    with pytest.raises(DomainInvariantError):
        model.add_connection(Connection(connection.id, second_port.id, second_node.id))
    assert _snapshot(model) == before


def test_connect_ports_voltage_failure_is_atomic():
    model = _model()
    voltage_10 = _voltage(model, 10_000)
    voltage_35 = _voltage(model, 35_000)
    first = _create(
        model,
        "builtin.line",
        "line_10",
        "КЛ-10 кВ",
        ("from", "to"),
        voltages={"main": voltage_10.id},
    )
    second = _create(
        model,
        "builtin.line",
        "line_35",
        "КЛ-35 кВ",
        ("from", "to"),
        voltages={"main": voltage_35.id},
    )
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.connect_ports(
            model.port_by_role(first.id, "from").id,
            model.port_by_role(second.id, "from").id,
            node_id=ElectricalNodeId("conflicting_bus"),
            connection_ids=(ConnectionId("c1"), ConnectionId("c2")),
        )
    assert _snapshot(model) == before


def test_remove_connection_preserves_endpoints_and_leaves_no_orphans():
    model = _model()
    node = _node(model, "bus")
    load = _create(
        model, "builtin.load", "load", "Нагрузка", ("terminal",)
    )
    port = model.port_by_role(load.id, "terminal")
    connection, _ = model.connect_port(
        port.id, node.id, connection_id=ConnectionId("connection")
    )

    change = model.remove_connection(connection.id)
    assert change.removed_ids == (connection.id.value,)
    assert connection.id not in model.connections
    assert load.id in model.equipment
    assert port.id in model.ports
    assert node.id in model.electrical_nodes
    assert model.connection_for_port(port.id) is None
    assert model.validate_integrity() == []

    model.connect_port(
        port.id, node.id, connection_id=ConnectionId("replacement_connection")
    )
    assert model.node_for_port(port.id) == node


def test_connected_equipment_requires_cascade_and_cleans_state():
    model = _model()
    first_node = _node(model, "bus_a")
    second_node = _node(model, "bus_b")
    breaker = _create(
        model,
        "builtin.circuit_breaker",
        "qf",
        "QF",
        ("a", "b"),
        normal_position=SwitchPosition.CLOSED,
    )
    first_port = model.port_by_role(breaker.id, "a")
    second_port = model.port_by_role(breaker.id, "b")
    model.connect_port(
        first_port.id,
        first_node.id,
        connection_id=ConnectionId("qf_a_connection"),
    )
    model.connect_port(
        second_port.id,
        second_node.id,
        connection_id=ConnectionId("qf_b_connection"),
    )
    state = OperatingState(
        OperatingStateId("normal"),
        "Нормальный",
        {breaker.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(state)

    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.remove_equipment(breaker.id)
    assert _snapshot(model) == before

    change = model.remove_equipment(breaker.id, cascade=True)
    assert breaker.id not in model.equipment
    assert first_port.id not in model.ports
    assert second_port.id not in model.ports
    assert not model.connections
    assert model.operating_states[state.id].positions == {}
    assert state.id.value in change.changed_ids
    assert model.validate_integrity() == []


def test_connected_node_requires_cascade_and_leaves_port_unattached():
    model = _model()
    node = _node(model, "bus")
    load = _create(
        model, "builtin.load", "load", "Нагрузка", ("terminal",)
    )
    port = model.port_by_role(load.id, "terminal")
    connection, _ = model.connect_port(
        port.id, node.id, connection_id=ConnectionId("connection")
    )

    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.remove_node(node.id)
    assert _snapshot(model) == before

    model.remove_node(node.id, cascade=True)
    assert node.id not in model.electrical_nodes
    assert connection.id not in model.connections
    assert port.id in model.ports
    assert model.connection_for_port(port.id) is None
    assert model.validate_integrity() == []


def test_rename_preserves_connectivity_ids_and_allows_duplicate_names():
    model = _model()
    first_node = _node(model, "bus_a", 10_000)
    second_node = _node(model, "bus_b", 10_000)
    voltage = _voltage(model, 10_000)
    first = _create(
        model,
        "builtin.line",
        "line_1",
        "Одинаковое имя",
        ("from", "to"),
        voltages={"main": voltage.id},
    )
    second = _create(
        model,
        "builtin.line",
        "line_2",
        "Одинаковое имя",
        ("from", "to"),
        voltages={"main": voltage.id},
    )
    model.connect_port(
        model.port_by_role(first.id, "from").id,
        first_node.id,
        connection_id=ConnectionId("line_1_from_connection"),
    )
    model.connect_port(
        model.port_by_role(first.id, "to").id,
        second_node.id,
        connection_id=ConnectionId("line_1_to_connection"),
    )
    signature = model.connectivity_signature()
    port_ids = first.port_ids

    model.rename_equipment(first.id, "КЛ после переименования")
    renamed = model.equipment[first.id]
    assert renamed.id == first.id
    assert renamed.port_ids == port_ids
    assert model.connectivity_signature() == signature
    assert model.equipment[second.id].name == "Одинаковое имя"
    assert model.validate_integrity() == []

    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.rename_equipment(first.id, "   ")
    assert _snapshot(model) == before


def test_voltage_registry_has_required_levels_and_accepts_extension():
    model = _model()
    assert {
        item.nominal_voltage_v for item in model.voltage_classes.values()
    } == {220_000, 110_000, 35_000, 10_000, 6_000, 400}

    custom = VoltageClass(
        VoltageClassId("user.voltage.ac.20kv"), 20_000, "20 кВ"
    )
    model.register_voltage_class(custom)
    assert model.ensure_voltage_class(20_000) == custom

    line = _create(
        model,
        "builtin.line",
        "line_20",
        "КЛ-20 кВ",
        ("from", "to"),
        voltages={"main": custom.id},
    )
    node = ElectricalNode(
        ElectricalNodeId("bus_20"),
        "Шины 20 кВ",
        declared_voltage_class_id=custom.id,
    )
    model.add_node(node)
    port = model.port_by_role(line.id, "from")
    model.connect_port(
        port.id,
        node.id,
        connection_id=ConnectionId("line_20_from_connection"),
    )
    assert model.port_voltage_class(port.id) == custom.id

    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.register_voltage_class(VoltageClass(
            VoltageClassId("duplicate.voltage.ac.20kv"), 20_000, "Другие 20 кВ"
        ))
    assert _snapshot(model) == before


def test_transformer_hv_lv_ports_keep_roles_and_voltage_sides():
    model = _model()
    voltage_hv = _voltage(model, 110_000)
    voltage_lv = _voltage(model, 10_000)
    transformer, _ = model.create_equipment(
        "builtin.transformer_2w",
        "Т-1",
        equipment_id=EquipmentId("transformer"),
        port_ids_by_role={
            "lv": PortId("transformer_lv"),
            "hv": PortId("transformer_hv"),
        },
        voltage_class_by_group={"hv": voltage_hv.id, "lv": voltage_lv.id},
    )
    hv = model.port_by_role(transformer.id, "hv")
    lv = model.port_by_role(transformer.id, "lv")
    assert transformer.port_ids == (hv.id, lv.id)
    assert hv.id == PortId("transformer_hv")
    assert lv.id == PortId("transformer_lv")
    assert model.port_voltage_class(hv.id) == voltage_hv.id
    assert model.port_voltage_class(lv.id) == voltage_lv.id

    hv_node = _node(model, "bus_110", 110_000)
    lv_node = _node(model, "bus_10", 10_000)
    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        model.connect_port(
            hv.id,
            lv_node.id,
            connection_id=ConnectionId("wrong_hv_connection"),
        )
    assert _snapshot(model) == before

    model.connect_port(
        hv.id, hv_node.id, connection_id=ConnectionId("hv_connection")
    )
    model.connect_port(
        lv.id, lv_node.id, connection_id=ConnectionId("lv_connection")
    )
    assert model.node_for_port(hv.id) == hv_node
    assert model.node_for_port(lv.id) == lv_node
    assert model.validate_integrity() == []


def test_operating_state_is_keyed_by_real_switch_equipment():
    model = _model()
    breaker = _create(
        model,
        "builtin.circuit_breaker",
        "qf",
        "QF",
        ("a", "b"),
        normal_position=SwitchPosition.CLOSED,
    )
    state = OperatingState(
        OperatingStateId("normal"),
        "Нормальный",
        {breaker.id: SwitchPosition.OPEN},
    )
    model.add_operating_state(state)

    stored = model.operating_states[state.id]
    assert tuple(stored.positions) == (breaker.id,)
    assert stored.positions[breaker.id] is SwitchPosition.OPEN
    model.rename_equipment(breaker.id, "QF после переименования")
    assert model.operating_states[state.id].positions[breaker.id] is SwitchPosition.OPEN

    model.set_switch_position(state.id, breaker.id, SwitchPosition.CLOSED)
    assert model.operating_states[state.id].positions[breaker.id] is SwitchPosition.CLOSED
    assert model.validate_integrity() == []


def test_non_switch_and_missing_equipment_states_are_rejected_atomically():
    model = _model()
    line = _create(
        model, "builtin.line", "line", "Линия", ("from", "to")
    )
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.add_operating_state(OperatingState(
            OperatingStateId("line_state"),
            "Недопустимый режим",
            {line.id: SwitchPosition.OPEN},
        ))
    assert _snapshot(model) == before

    with pytest.raises(DomainInvariantError):
        model.add_operating_state(OperatingState(
            OperatingStateId("missing_state"),
            "Повреждённый режим",
            {EquipmentId("missing_equipment"): SwitchPosition.OPEN},
        ))
    assert _snapshot(model) == before

    with pytest.raises(DomainInvariantError):
        _create(
            model,
            "builtin.line",
            "line_with_position",
            "Линия с положением",
            ("from", "to"),
            normal_position=SwitchPosition.CLOSED,
        )
    assert _snapshot(model) == before


def test_normal_switch_position_requires_open_closed_value():
    model = _model()
    before = _snapshot(model)
    with pytest.raises(DomainInvariantError):
        _create(
            model,
            "builtin.circuit_breaker",
            "qf",
            "QF",
            ("a", "b"),
            normal_position=True,  # type: ignore[arg-type]
        )
    assert _snapshot(model) == before


def test_public_collections_are_read_only_and_valid_integrity_is_clean():
    model = _model()
    load = _create(
        model, "builtin.load", "load", "Нагрузка", ("terminal",)
    )
    node = _node(model, "bus")
    port = model.port_by_role(load.id, "terminal")
    model.connect_port(port.id, node.id, connection_id=ConnectionId("connection"))

    with pytest.raises(TypeError):
        model.equipment[EquipmentId("other")] = load  # type: ignore[index]
    with pytest.raises(TypeError):
        del model.connections[ConnectionId("connection")]  # type: ignore[attr-defined]
    assert model.validate_integrity() == []


def test_integrity_reports_missing_owner_and_dangling_connection_refs():
    model = _model()
    orphan_port = PortInstance(
        PortId("orphan_port"), EquipmentId("missing_owner"), "terminal"
    )
    damaged_connection = Connection(
        ConnectionId("damaged_connection"),
        PortId("missing_port"),
        ElectricalNodeId("missing_node"),
    )
    model._ports[orphan_port.id] = orphan_port
    model._connections[damaged_connection.id] = damaged_connection

    issues = model.validate_integrity()
    codes = {issue.code for issue in issues}
    assert "missing_port_owner" in codes
    assert "dangling_connection_port" in codes
    assert "dangling_connection_node" in codes


def test_integrity_reports_equipment_port_missing_from_port_store():
    model = _model()
    line = _create(
        model, "builtin.line", "line", "Линия", ("from", "to")
    )
    missing_port_id = line.port_ids[0]
    del model._ports[missing_port_id]

    issues = model.validate_integrity()
    assert any(issue.object_id == missing_port_id.value for issue in issues)


def test_integrity_reports_state_attached_to_non_switch_equipment():
    model = _model()
    line = _create(
        model, "builtin.line", "line", "Линия", ("from", "to")
    )
    state = OperatingState(
        OperatingStateId("damaged_state"),
        "Повреждённый режим",
        {line.id: SwitchPosition.OPEN},
    )
    model._operating_states[state.id] = state

    issues = model.validate_integrity()
    assert any(issue.object_id == state.id.value for issue in issues)


def test_connect_ports_wrong_runtime_connection_id_is_atomic():
    model = _model()
    first = _create(model, "builtin.load", "load_a", "A", ("terminal",))
    second = _create(model, "builtin.load", "load_b", "B", ("terminal",))
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.connect_ports(
            model.port_by_role(first.id, "terminal").id,
            model.port_by_role(second.id, "terminal").id,
            node_id=ElectricalNodeId("new_bus"),
            connection_ids=(
                PortId("wrong_connection_type"),  # type: ignore[arg-type]
                ConnectionId("second_connection"),
            ),
        )
    assert _snapshot(model) == before


def test_undeclared_node_rejects_mixed_port_voltage_classes():
    model = _model()
    node = _node(model, "unlabelled_bus")
    voltage_10 = _voltage(model, 10_000)
    voltage_35 = _voltage(model, 35_000)
    first = _create(
        model,
        "builtin.load",
        "load_10",
        "10 кВ",
        ("terminal",),
        voltages={"main": voltage_10.id},
    )
    second = _create(
        model,
        "builtin.load",
        "load_35",
        "35 кВ",
        ("terminal",),
        voltages={"main": voltage_35.id},
    )
    model.connect_port(
        model.port_by_role(first.id, "terminal").id,
        node.id,
        connection_id=ConnectionId("first_connection"),
    )
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.connect_port(
            model.port_by_role(second.id, "terminal").id,
            node.id,
            connection_id=ConnectionId("second_connection"),
        )
    assert _snapshot(model) == before


def test_merge_nodes_checks_ports_already_attached_to_keep_node():
    model = _model()
    keep = _node(model, "keep")
    remove = _node(model, "remove", 10_000)
    voltage_35 = _voltage(model, 35_000)
    voltage_10 = _voltage(model, 10_000)
    load_35 = _create(
        model,
        "builtin.load",
        "load_35",
        "35 кВ",
        ("terminal",),
        voltages={"main": voltage_35.id},
    )
    load_10 = _create(
        model,
        "builtin.load",
        "load_10",
        "10 кВ",
        ("terminal",),
        voltages={"main": voltage_10.id},
    )
    model.connect_port(
        model.port_by_role(load_35.id, "terminal").id,
        keep.id,
        connection_id=ConnectionId("keep_connection"),
    )
    model.connect_port(
        model.port_by_role(load_10.id, "terminal").id,
        remove.id,
        connection_id=ConnectionId("remove_connection"),
    )
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.merge_nodes(keep.id, remove.id)
    assert _snapshot(model) == before


def test_node_and_connection_same_text_id_are_rejected_atomically():
    model = _model()
    first = _create(model, "builtin.load", "load_a", "A", ("terminal",))
    second = _create(model, "builtin.load", "load_b", "B", ("terminal",))
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.connect_ports(
            model.port_by_role(first.id, "terminal").id,
            model.port_by_role(second.id, "terminal").id,
            node_id=ElectricalNodeId("same_id"),
            connection_ids=(
                ConnectionId("same_id"),
                ConnectionId("other_connection"),
            ),
        )
    assert _snapshot(model) == before


def test_custom_port_allowed_voltage_is_enforced_atomically():
    model = _model()
    voltage_10 = _voltage(model, 10_000)
    voltage_35 = _voltage(model, 35_000)
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.type.10kv_only"),
        1,
        "Оборудование только 10 кВ",
        "bus",
        (
            PortDefinition(
                "terminal",
                "Вывод",
                AC_POWER,
                voltage_group="main",
                allowed_voltage_class_ids=frozenset({voltage_10.id}),
            ),
        ),
    )
    model.register_equipment_type(definition)
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.create_equipment(
            definition.id,
            "Ошибочные 35 кВ",
            equipment_id=EquipmentId("wrong_voltage"),
            port_ids_by_role={"terminal": PortId("wrong_voltage_terminal")},
            voltage_class_by_group={"main": voltage_35.id},
        )
    assert _snapshot(model) == before


def test_property_definition_rejects_invalid_default_before_registration():
    model = _model()
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        PropertyDefinition("rating", "number", default="not-a-number")

    assert _snapshot(model) == before


def test_physical_line_section_cannot_be_created_without_logical_line():
    model = _model()
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.create_equipment(
            "builtin.line_section.overhead",
            "Orphan section",
            equipment_id=EquipmentId("orphan_section"),
            port_ids_by_role={
                "from": PortId("orphan_section_from"),
                "to": PortId("orphan_section_to"),
            },
            voltage_class_by_group={
                "main": VoltageClassId("builtin.voltage.ac.10kv")
            },
        )

    assert _snapshot(model) == before
    assert model.validate_integrity() == []


def test_operating_state_rejects_unknown_system_before_model_mutation():
    model = _model()
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        OperatingState(
            OperatingStateId("invalid_system"),
            "Invalid system",
            system="typo",
        )

    assert _snapshot(model) == before


def test_strict_codec_rejects_unknown_operating_system():
    model = _model()
    model.add_operating_state(
        OperatingState(OperatingStateId("valid_system"), "Valid system")
    )
    raw = json.loads(json.dumps(electrical_model_to_dict(model)))
    raw["operating_states"][0]["system"] = "typo"

    with pytest.raises(ElectricalModelFormatError):
        electrical_model_from_dict(raw)


def test_connect_ports_uses_connection_id_for_unattached_side():
    model = _model()
    node = _node(model, "bus")
    first = _create(model, "builtin.load", "load_a", "A", ("terminal",))
    second = _create(model, "builtin.load", "load_b", "B", ("terminal",))
    first_port = model.port_by_role(first.id, "terminal")
    second_port = model.port_by_role(second.id, "terminal")
    model.connect_port(
        first_port.id,
        node.id,
        connection_id=ConnectionId("existing_connection"),
    )

    model.connect_ports(
        first_port.id,
        second_port.id,
        connection_ids=(
            ConnectionId("unused_first_side_id"),
            ConnectionId("second_side_id"),
        ),
    )
    assert model.connection_for_port(first_port.id).id == ConnectionId(
        "existing_connection"
    )
    assert model.connection_for_port(second_port.id).id == ConnectionId(
        "second_side_id"
    )


def test_merge_nodes_inherits_voltage_and_preserves_connection_ids():
    model = _model()
    voltage = _voltage(model, 10_000)
    keep = _node(model, "keep")
    remove = _node(model, "remove", 10_000)
    first = _create(
        model,
        "builtin.load",
        "load_a",
        "A",
        ("terminal",),
        voltages={"main": voltage.id},
    )
    second = _create(
        model,
        "builtin.load",
        "load_b",
        "B",
        ("terminal",),
        voltages={"main": voltage.id},
    )
    first_connection, _ = model.connect_port(
        model.port_by_role(first.id, "terminal").id,
        keep.id,
        connection_id=ConnectionId("keep_connection"),
    )
    second_connection, _ = model.connect_port(
        model.port_by_role(second.id, "terminal").id,
        remove.id,
        connection_id=ConnectionId("remove_connection"),
    )

    model.merge_nodes(keep.id, remove.id)
    assert model.electrical_nodes[keep.id].declared_voltage_class_id == voltage.id
    assert remove.id not in model.electrical_nodes
    assert set(model.connections) == {first_connection.id, second_connection.id}
    assert all(
        item.electrical_node_id == keep.id for item in model.connections.values()
    )
    assert model.validate_integrity() == []


def test_invalid_switch_update_is_atomic_domain_error():
    model = _model()
    breaker = _create(
        model,
        "builtin.circuit_breaker",
        "qf",
        "QF",
        ("a", "b"),
        normal_position=SwitchPosition.CLOSED,
    )
    state = OperatingState(
        OperatingStateId("normal"),
        "Нормальный",
        {breaker.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(state)
    before = _snapshot(model)

    with pytest.raises(DomainInvariantError):
        model.set_switch_position(
            state.id, breaker.id, "INVALID"  # type: ignore[arg-type]
        )
    assert _snapshot(model) == before


def test_every_publicly_accepted_aggregate_roundtrips_and_metadata_is_read_only():
    model = ElectricalModel.with_builtins(
        "Сериализуемый проект",
        neutral={"10": "isolated"},
        extensions={"nested": {"values": [1, 2, 3]}},
    )
    voltage = _voltage(model, 10_000)
    load = _create(
        model,
        "builtin.load",
        "load",
        "Нагрузка",
        ("terminal",),
        voltages={"main": voltage.id},
    )
    node = _node(model, "bus", 10_000)
    model.connect_port(
        model.port_by_role(load.id, "terminal").id,
        node.id,
        connection_id=ConnectionId("connection"),
    )
    raw = json.loads(json.dumps(
        electrical_model_to_dict(model), ensure_ascii=False
    ))
    restored = electrical_model_from_dict(raw)

    assert electrical_model_to_dict(restored) == raw
    assert restored.connectivity_signature() == model.connectivity_signature()
    assert restored.revision == model.revision
    for attribute, value in (
        ("name", ""),
        ("revision", -1),
        ("neutral", {}),
        ("extensions", {}),
    ):
        with pytest.raises(AttributeError):
            setattr(model, attribute, value)
    with pytest.raises(DomainInvariantError):
        ElectricalModel("", neutral={"10": "isolated"})
    with pytest.raises(DomainInvariantError):
        ElectricalModel("Проект", neutral={"10": 42})  # type: ignore[dict-item]
