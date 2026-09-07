# -*- coding: utf-8 -*-
"""Stage-2 graph queries, persistence, compatibility and scale tests."""
from __future__ import annotations

from pathlib import Path

from rza_calc.adapters import adapt_to_calculation, import_legacy_network
from rza_calc.core.model import (
    GRID,
    LineBranch,
    Mode,
    Network,
    Node,
    SourceBranch,
    Transformer3W,
)
from rza_calc.domain.electrical import (
    Connection,
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeId,
    OperatingState,
    OperatingStateId,
    PortId,
    PortInstance,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.io.electrical_model import (
    electrical_model_from_dict,
    electrical_model_to_dict,
)
from rza_calc.io.project import load_project
from rza_calc.topology import Conductivity, TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")
ROOT = Path(__file__).resolve().parent.parent


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _connect_equipment(
    model: ElectricalModel,
    token: str,
    type_id: str,
    roles_and_nodes: tuple[tuple[str, ElectricalNode], ...],
    *,
    normal_position=None,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.{token}")
    model.create_equipment(
        type_id,
        token,
        equipment_id=equipment_id,
        port_ids_by_role={
            role: PortId(f"port.{token}.{role}") for role, _ in roles_and_nodes
        },
        voltage_class_by_group={"main": U10},
        normal_position=normal_position,
        properties=(
            {
                "manufacturer": "Test",
                "model": "R10",
                "rated_voltage_v": 10_000,
                "rated_current_a": 630,
            }
            if type_id == "builtin.recloser" else None
        ),
    )
    for role, node in roles_and_nodes:
        model.connect_port(
            model.port_by_role(equipment_id, role).id,
            node.id,
            connection_id=ConnectionId(f"connection.{token}.{role}"),
        )
    return equipment_id


def test_feeder_projection_keeps_parallel_and_ring_non_tree_links() -> None:
    model = ElectricalModel.with_builtins("feeder projection")
    a, b, c = (_node(model, token) for token in ("a", "b", "c"))
    source_id = _connect_equipment(
        model, "source", "builtin.external_grid", (("terminal", a),)
    )
    ab_one = _connect_equipment(
        model, "ab.1", "builtin.line", (("from", a), ("to", b))
    )
    ab_two = _connect_equipment(
        model, "ab.2", "builtin.line", (("from", a), ("to", b))
    )
    bc = _connect_equipment(
        model, "bc", "builtin.line", (("from", b), ("to", c))
    )
    ca = _connect_equipment(
        model, "ca", "builtin.line", (("from", c), ("to", a))
    )
    snapshot = TopologyEngine().compile(model)
    feeder = snapshot.feeder_tree(source_id)

    assert feeder.root_node_id == a.id
    assert set(feeder.node_ids) == {a.id, b.id, c.id}
    assert feeder.non_tree_link_ids
    all_projected = set(feeder.tree_link_ids) | set(feeder.non_tree_link_ids)
    active_ids = {
        link.id for link in snapshot.links.values() if link.active
    }
    assert all_projected == active_ids
    assert {ab_one, ab_two, bc, ca} == {
        snapshot.links[link_id].equipment_id for link_id in all_projected
    }
    assert snapshot.components[snapshot.component_by_node[a.id]].cycle_rank == 2


def test_save_reload_keeps_exact_semantic_topology_and_state() -> None:
    model = ElectricalModel.with_builtins("roundtrip topology")
    a, b, c = (_node(model, token) for token in ("rt.a", "rt.b", "rt.c"))
    source_id = _connect_equipment(
        model, "rt.source", "builtin.external_grid", (("terminal", a),)
    )
    del source_id
    line_id = _connect_equipment(
        model, "rt.line", "builtin.line", (("from", a), ("to", b))
    )
    del line_id
    recloser_id = _connect_equipment(
        model,
        "rt.recloser",
        "builtin.recloser",
        (("a", b), ("b", c)),
        normal_position=SwitchPosition.CLOSED,
    )
    # Recloser catalog parameters are required by its domain contract.
    model.update_equipment_properties(
        recloser_id,
        {
            "manufacturer": "Roundtrip",
            "model": "R10",
            "rated_voltage_v": 10_000,
            "rated_current_a": 630,
        },
    )
    state = OperatingState(
        OperatingStateId("state.rt.open"),
        "Open",
        {recloser_id: SwitchPosition.OPEN},
    )
    model.add_operating_state(state)

    first = TopologyEngine().compile(model, state.id)
    restored = electrical_model_from_dict(electrical_model_to_dict(model))
    second = TopologyEngine().compile(restored, state.id)
    assert first.semantic_signature() == second.semantic_signature()
    assert first.topology_fingerprint == second.topology_fingerprint


def test_legacy_endpoint_switch_state_is_not_lost() -> None:
    network = Network("legacy endpoint")
    network.add_node(Node("A", "A", 10.0))
    network.add_node(Node("B", "B", 10.0))
    network.add_branch(SourceBranch("S", "Source", GRID, "A"))
    network.add_branch(LineBranch(
        "L",
        "Line",
        "A",
        "B",
        switchable=True,
        normally_closed=True,
        length_km=1.0,
        r0=0.4,
        x0=0.3,
    ))
    network.add_mode(Mode(
        "endpoint_open",
        "Endpoint open",
        states={"SW:L:from": False},
    ))
    model = import_legacy_network(network)
    state_id = next(iter(model.operating_states))
    nodes_by_legacy_id = {
        node.extensions["legacy_calculation"]["legacy_id"]: node.id
        for node in model.electrical_nodes.values()
    }
    snapshot = TopologyEngine().compile(model, state_id)
    assert snapshot.is_valid
    assert snapshot.is_energized(nodes_by_legacy_id["A"])
    assert not snapshot.is_energized(nodes_by_legacy_id["B"])
    legacy_line = next(
        equipment
        for equipment in model.equipment.values()
        if model.equipment_type(
            equipment.type_id, equipment.type_version
        ).behavior_key == "legacy.line"
    )
    link = next(
        item for item in snapshot.links.values()
        if item.equipment_id == legacy_line.id
    )
    assert not link.active


def test_legacy_three_winding_mode_can_disconnect_one_winding_only() -> None:
    network = Network("legacy 3w")
    for node_id, voltage in (("HV", 110.0), ("MV", 35.0), ("LV", 10.0)):
        network.add_node(Node(node_id, node_id, voltage))
    network.add_branch(SourceBranch("S", "Source", GRID, "HV"))
    network.add_transformer3w(Transformer3W(
        "T",
        "T",
        "HV",
        "MV",
        "LV",
        40_000,
        110.0,
        35.0,
        10.0,
        10.0,
        17.0,
        6.0,
        switchable=True,
        normally_closed=True,
    ))
    network.add_mode(Mode("mv_out", "MV out", states={"T_mv": False}))
    model = import_legacy_network(network)
    state_id = next(iter(model.operating_states))
    nodes_by_legacy_id = {
        node.extensions["legacy_calculation"]["legacy_id"]: node.id
        for node in model.electrical_nodes.values()
    }
    snapshot = TopologyEngine().compile(model, state_id)
    hv, mv, lv = (
        nodes_by_legacy_id[role] for role in ("HV", "MV", "LV")
    )
    assert snapshot.component_by_node[hv] == snapshot.component_by_node[lv]
    assert snapshot.component_by_node[hv] != snapshot.component_by_node[mv]
    assert snapshot.is_energized(hv)
    assert snapshot.is_energized(lv)
    assert not snapshot.is_energized(mv)


def test_legacy_three_winding_with_one_remaining_winding_is_not_a_link() -> None:
    network = Network("legacy 3w single winding")
    for node_id, voltage in (("HV", 110.0), ("MV", 35.0), ("LV", 10.0)):
        network.add_node(Node(node_id, node_id, voltage))
    network.add_branch(SourceBranch("S", "Source", GRID, "HV"))
    network.add_transformer3w(Transformer3W(
        "T", "T", "HV", "MV", "LV", 40_000,
        110.0, 35.0, 10.0, 10.0, 17.0, 6.0,
        switchable=True,
        normally_closed=True,
    ))
    network.add_mode(Mode(
        "only_hv", "Only HV", states={"T_mv": False, "T_lv": False}
    ))
    model = import_legacy_network(network)
    snapshot = TopologyEngine().compile(model, next(iter(model.operating_states)))
    transformer = next(
        equipment
        for equipment in model.equipment.values()
        if model.equipment_type(
            equipment.type_id, equipment.type_version
        ).behavior_key == "legacy.transformer_3w"
    )
    link = snapshot.links[snapshot.link_ids_by_equipment[transformer.id][0]]

    assert snapshot.is_valid
    assert not link.active
    assert link.conductivity is Conductivity.OPEN
    assert link.active_node_ids == ()


def test_legacy_three_winding_partial_availability_roundtrips_per_leg() -> None:
    network = Network("legacy 3w partial availability")
    for node_id, voltage in (("HV", 110.0), ("MV", 35.0), ("LV", 10.0)):
        network.add_node(Node(node_id, node_id, voltage))
    network.add_branch(SourceBranch("S", "Source", GRID, "HV"))
    network.add_transformer3w(Transformer3W(
        "T", "T", "HV", "MV", "LV", 40_000,
        110.0, 35.0, 10.0, 10.0, 17.0, 6.0,
        switchable=True,
        normally_closed=True,
    ))
    network.add_mode(Mode(
        "mv_repair",
        "MV repair",
        availability={"T_mv": False},
    ))
    model = import_legacy_network(network)
    state_id = next(iter(model.operating_states))
    state = model.operating_states[state_id]
    assert state.availability == {}
    assert state.extensions["legacy_calculation"]["availability"] == {
        "T_mv": False
    }

    snapshot = TopologyEngine().compile(model, state_id)
    nodes_by_legacy_id = {
        node.extensions["legacy_calculation"]["legacy_id"]: node.id
        for node in model.electrical_nodes.values()
    }
    assert snapshot.component_by_node[nodes_by_legacy_id["HV"]] == (
        snapshot.component_by_node[nodes_by_legacy_id["LV"]]
    )
    assert snapshot.component_by_node[nodes_by_legacy_id["HV"]] != (
        snapshot.component_by_node[nodes_by_legacy_id["MV"]]
    )

    restored_mode = adapt_to_calculation(model, snapshot).network.modes["mv_repair"]
    assert not restored_mode.is_available("T_mv")
    assert restored_mode.is_available("T")
    assert restored_mode.is_available("T_lv")


def test_real_migrated_examples_compile_all_modes_without_blockers() -> None:
    for filename in ("gtes_sever.json", "ps_severnaya.json"):
        project = load_project(ROOT / "tests" / "fixtures" / "legacy_projects" / filename)
        engine = TopologyEngine()
        states = tuple(project.electrical_model.operating_states)
        assert states
        snapshots = tuple(
            engine.compile(project.electrical_model, state_id)
            for state_id in states
        )
        assert all(snapshot.is_valid for snapshot in snapshots)
        assert all("GRID" not in {item.value for item in snapshot.sources}
                   for snapshot in snapshots)


def test_iterative_compiler_and_path_query_handle_5000_node_chain() -> None:
    """Large fixture bypasses command O(n²) ID checks, not domain records."""
    count = 5_000
    model = ElectricalModel.with_builtins("large chain")
    model._electrical_nodes = {
        ElectricalNodeId(f"node.large.{index:05d}"): ElectricalNode(
            ElectricalNodeId(f"node.large.{index:05d}"),
            declared_voltage_class_id=U10,
        )
        for index in range(count)
    }
    line_type = model.equipment_type(EquipmentTypeId("builtin.line"))
    for index in range(count - 1):
        equipment_id = EquipmentId(f"equipment.large.{index:05d}")
        from_port = PortId(f"port.large.{index:05d}.from")
        to_port = PortId(f"port.large.{index:05d}.to")
        model._equipment[equipment_id] = EquipmentInstance(
            equipment_id,
            line_type.id,
            line_type.schema_version,
            f"L{index}",
            (from_port, to_port),
        )
        model._ports[from_port] = PortInstance(from_port, equipment_id, "from")
        model._ports[to_port] = PortInstance(to_port, equipment_id, "to")
        first_node = ElectricalNodeId(f"node.large.{index:05d}")
        second_node = ElectricalNodeId(f"node.large.{index + 1:05d}")
        first_connection = ConnectionId(f"connection.large.{index:05d}.from")
        second_connection = ConnectionId(f"connection.large.{index:05d}.to")
        model._connections[first_connection] = Connection(
            first_connection, from_port, first_node
        )
        model._connections[second_connection] = Connection(
            second_connection, to_port, second_node
        )
    source_type = model.equipment_type(EquipmentTypeId("builtin.external_grid"))
    source_id = EquipmentId("equipment.large.source")
    source_port = PortId("port.large.source.terminal")
    model._equipment[source_id] = EquipmentInstance(
        source_id,
        source_type.id,
        source_type.schema_version,
        "Source",
        (source_port,),
        voltage_class_by_group={"main": U10},
    )
    model._ports[source_port] = PortInstance(source_port, source_id, "terminal")
    source_connection = ConnectionId("connection.large.source")
    model._connections[source_connection] = Connection(
        source_connection, source_port, ElectricalNodeId("node.large.00000")
    )
    model._restore_revision(1)

    snapshot = TopologyEngine().compile(model)
    first = ElectricalNodeId("node.large.00000")
    last = ElectricalNodeId(f"node.large.{count - 1:05d}")
    assert snapshot.is_valid
    assert len(snapshot.node_ids) == count
    assert len(snapshot.components) == 1
    assert snapshot.is_energized(last)
    assert len(snapshot.find_path(first, last)) == count - 1
