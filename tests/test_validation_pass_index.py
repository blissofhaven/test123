"""A validation pass keeps all diagnostics while scanning connections once.

Deliberate direct-store corruption exercises the loader's validation boundary,
including same-revision changes which a persistent revision cache would miss.
"""
from collections import Counter
from dataclasses import replace

import pytest

from rza_calc.adapters import import_legacy_network
from rza_calc.core.model import LineBranch, Network, Node
from rza_calc.domain.electrical import (
    ConnectionId, ElectricalNodeId, EquipmentId, EquipmentTypeId,
    PortId, VoltageClassId,
)


def network_model(count=3):
    net = Network("Validation graph")
    for index in range(count + 1):
        net.add_node(Node(f"n{index}", f"N{index}", 10))
    for index in range(count):
        net.add_branch(LineBranch(f"l{index}", f"L{index}", f"n{index}", f"n{index + 1}",
                                  length_km=1, r0=.1, x0=.1, switchable=False))
    return import_legacy_network(net)


def by_legacy(values):
    return {item.extensions["legacy_calculation"]["legacy_id"]: item for item in values}


def corrupt(model, scenario):
    equipment = by_legacy(model.equipment.values())
    nodes = by_legacy(model.electrical_nodes.values())
    first = model.port_by_role(equipment["l0"].id, "from")
    last = model.port_by_role(equipment["l0"].id, "to")
    connection = model.connection_for_port(first.id)
    if scenario == "valid":
        pass
    elif scenario == "voltage_conflict":
        node = nodes["n0"]
        eq = equipment["l0"]
        group = "validation_main"
        key = (eq.type_id, eq.type_version)
        definition = model._equipment_types[key]
        model._equipment_types[key] = replace(definition, port_definitions=tuple(
            replace(port, voltage_group=group) for port in definition.port_definitions))
        model._equipment[eq.id] = replace(eq, voltage_class_by_group={
            group: VoltageClassId("builtin.voltage.ac.10kv")})
        model._electrical_nodes[node.id] = replace(node, declared_voltage_class_id=VoltageClassId("builtin.voltage.ac.110kv"))
    elif scenario == "duplicate_port":
        extra = replace(connection, id=ConnectionId("connection.duplicate"))
        model._connections[extra.id] = extra
    elif scenario == "same_equipment_node":
        other = model.connection_for_port(last.id)
        model._connections[other.id] = replace(other, electrical_node_id=connection.electrical_node_id)
    elif scenario == "missing_port":
        model._connections[connection.id] = replace(connection, port_id=PortId("port.missing"))
    elif scenario == "missing_node":
        model._connections[connection.id] = replace(connection, electrical_node_id=ElectricalNodeId("node.missing"))
    elif scenario == "missing_owner":
        model._ports[first.id] = replace(first, equipment_id=EquipmentId("equipment.missing"))
    elif scenario == "missing_type":
        eq = equipment["l0"]
        model._equipment[eq.id] = replace(eq, type_id=EquipmentTypeId("type.missing"))
    else:
        raise AssertionError(scenario)
    return model


EXPECTED = {
    "valid": {},
    "voltage_conflict": {"incompatible_connection": 1, "mixed_node_voltage_classes": 1},
    "duplicate_port": {"port_connected_twice": 1},
    "same_equipment_node": {"incompatible_connection": 2, "equipment_terminals_same_node": 1},
    "missing_port": {"dangling_connection_port": 1},
    "missing_node": {"dangling_connection_node": 1},
    "missing_owner": {"equipment_port_wrong_owner": 1, "missing_port_owner": 1, "incompatible_connection": 1},
    "missing_type": {"unknown_equipment_type": 1, "incompatible_connection": 3},
}


@pytest.mark.parametrize("scenario", EXPECTED)
def test_indexed_pass_keeps_corrupt_graph_diagnostics(scenario):
    model = corrupt(network_model(), scenario)
    before_revision = model.revision
    issues = model.validate_integrity()
    assert Counter(issue.code for issue in issues) == EXPECTED[scenario]
    assert model.revision == before_revision
    assert model.validate_integrity() == issues


def test_later_same_revision_corruption_and_repair_are_seen():
    model = network_model()
    revision = model.revision
    assert model.validate_integrity() == []
    before = dict(model._connections)
    corrupt(model, "same_equipment_node")
    assert model.revision == revision
    assert Counter(issue.code for issue in model.validate_integrity()) == EXPECTED["same_equipment_node"]
    model._connections = before
    assert model.revision == revision and model.validate_integrity() == []


def test_duplicate_port_uses_the_first_connection_for_endpoint_diagnostics():
    model = network_model()
    eq = by_legacy(model.equipment.values())["l0"]
    first, last = model.ports_of(eq.id)
    first_connection = model.connection_for_port(first.id)
    last_connection = model.connection_for_port(last.id)
    extra = replace(first_connection, id=ConnectionId("connection.duplicate.other.node"),
                    electrical_node_id=last_connection.electrical_node_id)
    model._connections[extra.id] = extra
    codes = Counter(issue.code for issue in model.validate_integrity())
    assert codes["port_connected_twice"] == 1
    assert codes["incompatible_connection"] >= 1
    # Existing connection_for_port semantics are insertion-order first, not
    # last-write wins: l0's first two endpoints still differ in this report.
    assert codes["equipment_terminals_same_node"] == 0


class CountedConnections(dict):
    def __init__(self, values):
        super().__init__(values)
        self.visits = 0

    def values(self):
        for value in super().values():
            self.visits += 1
            yield value


@pytest.mark.parametrize("branches", (8, 64))
def test_one_integrity_pass_has_linear_full_connection_scans(branches):
    model = network_model(branches)
    store = CountedConnections(model._connections)
    model._connections = store
    assert model.validate_integrity() == []
    # Work on the relevant local node/equipment groups is unrestricted; the
    # complete store must not be scanned again for every terminal.
    assert store.visits <= 4 * len(store), (store.visits, len(store))
