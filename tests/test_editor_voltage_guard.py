"""Explicit wiring adopts a known peer only into a blank equipment group.

Real conflicts, two unknown ends and existing unknown nodes still reject
atomically. Preview must predict commit without changing the live model.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rza_calc.domain.electrical import ElectricalNodeId, LineKind, VoltageClassId
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor import NewNodeTarget, NodeTarget, PhysicalLineInput, PortTarget, ProjectEditorController
from rza_calc.editor.connection_voltage import check_connection_voltage, endpoint_voltage
from rza_calc.editor.controller import EditorCommandError
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.project import load_project
from rza_calc.topology import TopologyEngine
from test_ui_connected_commands import _controller, _native_line


U10 = VoltageClassId("builtin.voltage.ac.10kv")
U110 = VoltageClassId("builtin.voltage.ac.110kv")
DEMO = Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json"


def _load(controller, name, voltage, x):
    return controller.add_equipment("builtin.load", name, x=x, y=0,
                                    voltage_class_by_group={} if voltage is None else {"main": voltage})


def _node(controller, name, voltage, x):
    return controller.add_electrical_node(name, x=x, y=200, voltage_class_id=voltage)


def _saved(controller):
    return (ElectricalModelMemento.capture(controller.model), diagram_to_dict(controller.diagram),
            controller.journal, controller.can_undo, controller.can_redo)


def _reject(controller, callback):
    before = _saved(controller)
    with pytest.raises(EditorCommandError, match="напряжени"):
        callback()
    assert _saved(controller) == before


@pytest.mark.parametrize("first_voltage,second_voltage", [(U10, U110), (None, U10), (U10, None), (None, None)])
@pytest.mark.parametrize("target_kind", ("port", "node"))
def test_explicit_connections_adopt_only_a_blank_equipment_group_and_reject_conflicts(first_voltage, second_voltage, target_kind):
    controller = _controller()
    first = _load(controller, "A", first_voltage, 0)
    if target_kind == "port":
        second = _load(controller, "B", second_voltage, 200)
        target = PortTarget(second.port_ids[0])
        command = lambda: controller.connect_ports(first.port_ids[0], second.port_ids[0])
    else:
        second = _node(controller, "B", second_voltage, 200)
        target = NodeTarget(second.node_id)
        command = lambda: controller.connect_port_to_node(first.port_ids[0], second.node_id)
    before = _saved(controller)
    check = controller.validate_connection(first.port_ids[0], target)
    adoption_allowed = ((first_voltage is None and second_voltage == U10)
                        or (target_kind == "port" and first_voltage == U10 and second_voltage is None))
    if adoption_allowed:
        assert check.valid and check.effective_voltage_id == U10
        assert _saved(controller) == before  # Preview is genuinely read-only.
        command()
        assert endpoint_voltage(controller.model, first.port_ids[0]).voltage_class_id == U10
        assert endpoint_voltage(controller.model, second.port_ids[0] if target_kind == "port" else second.node_id).voltage_class_id == U10
        assert controller.model.port_voltage_class(first.port_ids[0]) == U10
        if target_kind == "port":
            assert controller.model.port_voltage_class(second.port_ids[0]) == U10
        assert len(controller.journal) == len(before[2]) + 1
        after = ElectricalModelMemento.capture(controller.model)
        controller.undo()
        assert ElectricalModelMemento.capture(controller.model) == before[0]
        controller.redo()
        assert ElectricalModelMemento.capture(controller.model) == after
        return
    assert not check.valid and check.severity == "error"
    assert "напряжени" in check.message
    assert _saved(controller) == before
    _reject(controller, command)


def test_unknown_standalone_drafts_remain_creatable_without_connecting_them():
    controller = _controller()
    load = _load(controller, "Без класса", None, 0)
    node = _node(controller, "Без класса", None, 200)
    assert load.equipment_id in controller.model.equipment and node.node_id in controller.model.electrical_nodes
    assert not controller.model.connections
    assert not endpoint_voltage(controller.model, load.port_ids[0]).valid
    assert not endpoint_voltage(controller.model, node.node_id).valid


def test_permissive_domain_import_still_preserves_legacy_unknown_port_inference():
    controller = _controller()
    known = _load(controller, "10", U10, 0)
    unknown = _load(controller, "Импортируемый без декларации", None, 200)
    model = controller.model
    model.connect_ports(known.port_ids[0], unknown.port_ids[0])
    node = model.node_for_port(unknown.port_ids[0])
    assert node is not None and node.declared_voltage_class_id == U10
    assert model.port_voltage_class(unknown.port_ids[0]) is None
    assert endpoint_voltage(model, unknown.port_ids[0]).voltage_class_id == U10


def test_new_free_endpoint_inherits_known_source_without_guessing_existing_unknown_voltage():
    controller = _controller()
    load = _load(controller, "10 кВ", U10, 0)
    result = controller.finish_port_on_new_node(load.port_ids[0], NewNodeTarget("Продолжение", x=200, y=0))
    assert controller.model.electrical_nodes[result.node_id].declared_voltage_class_id == U10
    assert controller.model.port_voltage_class(load.port_ids[0]) == U10
    controller.undo()
    assert result.node_id not in controller.model.electrical_nodes
    unknown = _load(controller, "Не задано", None, 200)
    _reject(controller, lambda: controller.finish_port_on_new_node(unknown.port_ids[0], NewNodeTarget("Нельзя")))
    _reject(controller, lambda: controller.finish_port_on_new_node(load.port_ids[0], NewNodeTarget("110", voltage_class_id=U110)))


@pytest.mark.parametrize("kind", ("existing_unknown", "mismatch", "both_new_unknown"))
def test_physical_line_rejects_invalid_voltage_without_allocating_nodes_or_branches(kind):
    controller = _controller()
    known = _node(controller, "10", U10, 0)
    other = _node(controller, "B", None if kind == "existing_unknown" else U110, 300)
    start, end = NodeTarget(known.node_id), NodeTarget(other.node_id)
    if kind == "both_new_unknown":
        start, end = NewNodeTarget("A", x=0, y=0), NewNodeTarget("B", x=300, y=0)
    _reject(controller, lambda: controller.create_physical_line(
        "Недопустимая", LineKind.CABLE, start, end, physical=PhysicalLineInput(1_000_000)))


@pytest.mark.parametrize("reverse", (False, True))
def test_physical_line_free_end_inherits_known_source_in_both_orders(reverse):
    controller = _controller()
    known = _node(controller, "10", U10, 0)
    endpoints = (NodeTarget(known.node_id), NewNodeTarget("Новый", x=400, y=0))
    if reverse:
        endpoints = endpoints[::-1]
    result = controller.create_physical_line("КЛ", LineKind.CABLE, *endpoints, physical=PhysicalLineInput(1_000_000))
    assert controller.model.logical_lines[result.logical_line_id].voltage_class_id == U10
    assert all(controller.model.electrical_nodes[node_id].declared_voltage_class_id == U10
               for node_id in (result.start_node_id, result.end_node_id))


@pytest.mark.parametrize("target_kind", ("port", "node", "new_node"))
def test_reconnect_rejects_changed_voltage_before_old_connection_is_removed(target_kind):
    controller = _controller()
    load = _load(controller, "10", U10, 0)
    old = _node(controller, "Исходный", U10, 0)
    controller.connect_port_to_node(load.port_ids[0], old.node_id)
    if target_kind == "port":
        target = PortTarget(_load(controller, "110", U110, 300).port_ids[0])
    elif target_kind == "node":
        target = NodeTarget(_node(controller, "110", U110, 300).node_id)
    else:
        target = NewNodeTarget("110", x=300, y=200, voltage_class_id=U110)
    _reject(controller, lambda: controller.reconnect_port(load.port_ids[0], target))


@pytest.mark.parametrize("target_kind", ("port", "node", "new_node"))
def test_tap_creation_rejects_target_voltage_before_splitting_a_real_line(target_kind):
    controller, _, _, line = _native_line()
    if target_kind == "port":
        target = PortTarget(_load(controller, "110", U110, 300).port_ids[0])
    elif target_kind == "node":
        target = NodeTarget(_node(controller, "110", U110, 300).node_id)
    else:
        target = NewNodeTarget("110", x=300, y=200, voltage_class_id=U110)
    _reject(controller, lambda: controller.create_tap(line.section_id, 2_000_000, "Отпайка", LineKind.CABLE,
                                                    target, physical=PhysicalLineInput(100_000)))


def test_reconnect_to_physical_route_cannot_bypass_voltage_guard_by_creating_a_tap():
    controller, _, _, line = _native_line()
    load = _load(controller, "110", U110, 300)
    old = _node(controller, "110", U110, 300)
    controller.connect_port_to_node(load.port_ids[0], old.node_id)
    _reject(controller, lambda: controller.reconnect_port_to_tap(load.port_ids[0], line.section_id, 2_000_000))


def test_new_equipment_attachment_cannot_bypass_an_explicit_wrong_voltage():
    controller, _, _, line = _native_line()
    _reject(controller, lambda: controller.attach_equipment_to_line(
        line.section_id, 2_000_000, "builtin.load", "110", terminal_role="terminal",
        voltage_class_by_group={"main": U110}))


def test_existing_node_with_known_resolved_zone_is_not_misclassified_as_unknown():
    controller = _controller()
    node = _node(controller, "Без декларации", None, 200)
    assigned = _load(controller, "Явные 10", U10, 0)
    # Permissive domain loading remains allowed. The editor must inspect its
    # authoritative resolved zone, not silently fill the node's declaration.
    model = controller.model
    model.connect_port(assigned.port_ids[0], node.node_id)
    controller = ProjectEditorController(controller._project)
    other = _load(controller, "Тоже 10", U10, 400)
    assert endpoint_voltage(model, node.node_id).voltage_class_id == U10
    result = controller.connect_port_to_node(other.port_ids[0], node.node_id)
    assert result.node_id == node.node_id
    assert model.electrical_nodes[node.node_id].declared_voltage_class_id is None


def test_actual_legacy_ports_resolve_like_colour_and_lv_cannot_reconnect_to_hv_bus():
    digest = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    controller = ProjectEditorController(load_project(DEMO))
    model = controller.model
    snapshot = TopologyEngine().compile(model)
    assert all(model.port_voltage_class(port_id) is None for port_id in model.ports)
    for port_id in model.ports:
        assert endpoint_voltage(model, port_id).voltage_class_id == snapshot.voltage_zone_of(port_id).resolution.voltage_class_id
    transformer = next(row for row in model.equipment.values() if row.name == "Т2 Центральная 110/10")
    low_port = model.port_by_role(transformer.id, "to").id
    high_node = next(node.id for node in model.electrical_nodes.values()
                     if "СЕВЕРНАЯ" in node.name and node.declared_voltage_class_id == U110)
    _reject(controller, lambda: controller.reconnect_port(low_port, NodeTarget(high_node)))
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == digest


def test_resolved_voltage_cache_is_reused_within_revision_and_invalidated_after_reconnect(monkeypatch):
    import rza_calc.editor.connection_voltage as guard
    controller = _controller()
    first = _node(controller, "10", U10, 0)
    second = _node(controller, "110", U110, 200)
    load = _load(controller, "Импорт без декларации", None, 400)
    model = controller.model
    model.connect_port(load.port_ids[0], first.node_id)
    calls = []
    original = guard._ENGINE.compile
    monkeypatch.setattr(guard._ENGINE, "compile", lambda model: (calls.append(model.revision), original(model))[1])
    for _ in range(100):
        assert endpoint_voltage(model, load.port_ids[0]).voltage_class_id == U10
    assert len(calls) == 1
    # Use the permissive import/domain path solely to test cache invalidation;
    # the guarded UI operation would correctly reject this voltage change.
    model.reconnect_port(load.port_ids[0], second.node_id)
    assert endpoint_voltage(model, load.port_ids[0]).voltage_class_id == U110
    assert len(calls) == 2 and calls[0] != calls[1]


def test_conflicting_imported_voltage_zone_rejects_even_a_direct_known_node():
    controller = _controller()
    first = _node(controller, "10", U10, 0)
    second = _node(controller, "110", U110, 300)
    line = controller.add_equipment("builtin.line", "Импорт с конфликтом", x=150, y=0)
    model = controller.model
    model.connect_port(line.port_ids[0], first.node_id)
    model.connect_port(line.port_ids[1], second.node_id)
    result = endpoint_voltage(model, first.node_id)
    assert not result.valid and "противоречиво" in result.message
    assert not check_connection_voltage(model, first.node_id, U10).valid


@pytest.mark.parametrize("kind", ("equipment", "node"))
def test_explicit_assignment_to_isolated_draft_keeps_ids_and_is_one_undo_step(kind):
    controller = _controller()
    item = _load(controller, "Без класса", None, 0) if kind == "equipment" else _node(controller, "Без класса", None, 0)
    before = ElectricalModelMemento.capture(controller.model)
    diagram = controller.diagram
    journal = len(controller.journal)
    if kind == "equipment":
        action = lambda: controller.set_equipment_voltage_class(item.equipment_id, "main", U10)
        endpoint = item.port_ids[0]
    else:
        action = lambda: controller.set_node_voltage_class(item.node_id, U10)
        endpoint = item.node_id
    assert not endpoint_voltage(controller.model, endpoint).valid
    action()
    after = ElectricalModelMemento.capture(controller.model)
    assert endpoint_voltage(controller.model, endpoint).voltage_class_id == U10
    assert set(after.equipment) == set(before.equipment) and after.ports == before.ports
    assert set(after.electrical_nodes) == set(before.electrical_nodes) and after.connections == before.connections
    assert controller.diagram == diagram and len(controller.journal) == journal + 1
    action()
    assert len(controller.journal) == journal + 1  # reselecting same class is not an edit
    controller.undo()
    assert not endpoint_voltage(controller.model, endpoint).valid
    assert controller.model.equipment == before.equipment and controller.model.electrical_nodes == before.electrical_nodes
    controller.redo()
    assert endpoint_voltage(controller.model, endpoint).voltage_class_id == U10


def test_connected_group_cannot_be_relabelled_even_from_its_free_terminal():
    controller = _controller()
    breaker = controller.add_equipment("builtin.circuit_breaker", "Q", voltage_class_by_group={"main": U10})
    node = _node(controller, "Шина", U10, 200)
    controller.connect_port_to_node(breaker.port_ids[0], node.node_id)
    assert controller.model.connection_for_port(breaker.port_ids[1]) is None
    _reject(controller, lambda: controller.set_equipment_voltage_class(breaker.equipment_id, "main", U110))
    _reject(controller, lambda: controller.set_node_voltage_class(node.node_id, U110))


def test_transformer_windings_are_assigned_independently_without_inventing_other_voltage():
    controller = _controller()
    transformer = controller.add_equipment("builtin.transformer_2w", "T")
    controller.set_equipment_voltage_class(transformer.equipment_id, "hv", U110)
    assert dict(controller.model.equipment[transformer.equipment_id].voltage_class_by_group) == {"hv": U110}
    hv = controller.model.port_by_role(transformer.equipment_id, "hv").id
    bus = _node(controller, "110", U110, 300)
    controller.connect_port_to_node(hv, bus.node_id)
    controller.set_equipment_voltage_class(transformer.equipment_id, "lv", U10)
    assert dict(controller.model.equipment[transformer.equipment_id].voltage_class_by_group) == {"hv": U110, "lv": U10}
    _reject(controller, lambda: controller.set_equipment_voltage_class(transformer.equipment_id, "hv", U10))


@pytest.mark.parametrize("kind", ("unknown_class", "unknown_group", "node_unknown_class", "analysis", "rated_mismatch"))
def test_invalid_explicit_assignment_is_atomic(kind):
    from rza_calc.editor.state import EditorMode
    controller = _controller()
    item = controller.add_equipment("builtin.recloser" if kind == "rated_mismatch" else "builtin.load", "Q")
    node = _node(controller, "Узел", None, 300)
    if kind == "analysis":
        controller.set_mode(EditorMode.ANALYSIS)
    voltage = VoltageClassId("not.registered") if kind == "unknown_class" else U110
    callback = lambda: controller.set_equipment_voltage_class(item.equipment_id,
                  "not.a.group" if kind == "unknown_group" else "main", voltage)
    if kind == "node_unknown_class":
        callback = lambda: controller.set_node_voltage_class(node.node_id, VoltageClassId("not.registered"))
    before = _saved(controller)
    with pytest.raises(EditorCommandError):
        callback()
    assert _saved(controller) == before
