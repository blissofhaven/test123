"""Hover reuses one answer only while all canonical inputs still match."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from types import MappingProxyType

import pytest

from rza_calc.domain.electrical import (
    ElectricalModel, ElectricalNodeId, LineConstructionSegment,
    LineConstructionSegmentId, LineKind, LineSection, LogicalLine,
    LogicalLineId, OperatingState, OperatingStateId, PortId, PropertyDefinition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor.controller import NewNodeTarget, NodeTarget, PortTarget
from test_bus_connection_spacing import PAGE, U10, _connect, _load, _setup

U110 = VoltageClassId("builtin.voltage.ac.110kv")


@pytest.fixture
def connected():
    controller, bus = _setup()
    apparatus = _load(controller, 650)
    _connect(controller, bus, apparatus)
    other = controller.add_electrical_node("HV", page_id=PAGE, x=900, voltage_class_id=U110)
    return controller, apparatus, bus, other


def count_preflights(controller, monkeypatch):
    calls = []
    original = controller._guard_connection_voltage
    def guard(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(controller, "_guard_connection_voltage", guard)
    return calls


def test_same_semantic_pair_uses_one_preflight_and_only_one_entry(connected, monkeypatch):
    controller, apparatus, bus, other = connected
    calls = count_preflights(controller, monkeypatch)
    before = ElectricalModelMemento.capture(controller.model)
    port = apparatus.port_ids[0]
    result = controller.preview_connection_voltage(port, bus.node_id)
    assert result.valid and result.voltage_class_id == U10
    for index in range(25):
        assert controller.preview_connection_voltage(
            PortTarget(port, apparatus.representation_id, str(index)),
            NodeTarget(bus.node_id, bus.representation_id, str(index), index * 7, 0),
        ) is result
    assert len(calls) == 1
    assert not controller.preview_connection_voltage(port, other.node_id).valid
    assert controller.preview_connection_voltage(port, bus.node_id).valid
    assert len(calls) == 3, "Only the last pair is retained; this is not an unbounded map"
    assert ElectricalModelMemento.capture(controller.model) == before


def test_new_node_coordinates_do_not_change_voltage_but_endpoint_kind_does(connected, monkeypatch):
    controller, apparatus, bus, _ = connected
    calls = count_preflights(controller, monkeypatch)
    first = controller.preview_connection_voltage(apparatus.port_ids[0], NewNodeTarget(x=0, y=0))
    assert first.valid and first.voltage_class_id == U10
    for index in range(10):
        assert controller.preview_connection_voltage(apparatus.port_ids[0], NewNodeTarget(name=str(index), x=index, y=-index)) is first
    assert len(calls) == 1
    assert controller.preview_connection_voltage(apparatus.port_ids[0], NewNodeTarget(voltage_class_id=U110)).valid is False
    assert len(calls) == 2
    # A known/new node and a port with equal raw ID text are not interchangeable.
    assert controller.preview_connection_voltage(PortId(bus.node_id.value), U10).valid is False
    assert controller.preview_connection_voltage(ElectricalNodeId(bus.node_id.value), U10).valid
    assert len(calls) == 4
    assert not controller.preview_connection_voltage(None, U10).valid
    assert controller.preview_connection_voltage(NewNodeTarget(), U10).valid


@pytest.mark.parametrize("change", ("properties", "class", "ports", "connections", "node", "type", "extensions", "neutral", "name", "state", "logical_line", "line_section", "revision", "model"))
def test_all_changed_inputs_invalidate_even_with_same_revision(connected, monkeypatch, change):
    controller, apparatus, bus, other = connected
    calls = count_preflights(controller, monkeypatch)
    model = controller.model
    port = apparatus.port_ids[0]
    assert controller.preview_connection_voltage(port, bus.node_id).valid
    revision = model.revision
    if change == "properties":
        row = model.equipment[apparatus.equipment_id]
        model._equipment[row.id] = replace(row, properties={"nested": {"numbers": [1, 2, 3]}})
    elif change == "class":
        model._voltage_classes[U10] = replace(model.voltage_classes[U10], nominal_voltage_v=10_500)
    elif change == "ports":
        model._ports.pop(port)
    elif change == "connections":
        connection = model.connection_for_port(port)
        model._connections[connection.id] = replace(connection, electrical_node_id=other.node_id)
    elif change == "node":
        model._electrical_nodes[bus.node_id] = replace(model.electrical_nodes[bus.node_id], declared_voltage_class_id=U110)
    elif change == "type":
        row = model.equipment[apparatus.equipment_id]
        key = (row.type_id, row.type_version)
        model._equipment_types[key] = replace(model.equipment_types[key], display_name="Updated type")
    elif change == "extensions":
        model._extensions = ElectricalModel(extensions={"nested": {"a": [1, 2]}}).extensions
    elif change == "neutral":
        model._neutral = ElectricalModel(neutral={"new": "test"}).neutral
    elif change == "name":
        model._name = "Changed model name"
    elif change == "state":
        state = OperatingState(OperatingStateId("state.test"), "Test")
        model._operating_states[state.id] = state
    elif change in ("logical_line", "line_section"):
        line = LogicalLine(LogicalLineId("line.test"), "Test", LineKind.CABLE, U10, (apparatus.equipment_id,))
        if change == "logical_line":
            model._logical_lines[line.id] = line
        else:
            model._line_sections[apparatus.equipment_id] = LineSection(apparatus.equipment_id, line.id, 1000)
    elif change == "revision":
        model._revision += 1
    else:
        controller._project.electrical_model = ElectricalModelMemento.capture(model).to_model()
    assert controller.model.revision == revision + (change == "revision")
    result = controller.preview_connection_voltage(port, bus.node_id)
    assert len(calls) == 2
    if change in ("ports", "connections", "node"):
        assert not result.valid
    assert controller.preview_connection_voltage(port, bus.node_id) is result
    assert len(calls) == 2


def test_unknown_group_adoption_is_cached_without_becoming_live_or_authorizing_commit(monkeypatch):
    controller, bus = _setup()
    apparatus = controller.add_equipment("builtin.circuit_breaker", "Unknown Q", page_id=PAGE, x=800)
    port = apparatus.port_ids[0]
    calls = count_preflights(controller, monkeypatch)
    before = electrical_model_fingerprint(controller.model)
    journal = len(controller.journal)
    first = controller.preview_connection_voltage(port, bus.node_id)
    assert first.valid and first.voltage_class_id == U10
    for _ in range(15):
        assert controller.preview_connection_voltage(port, bus.node_id) is first
    assert len(calls) == 1
    assert electrical_model_fingerprint(controller.model) == before
    assert not controller.model.equipment[apparatus.equipment_id].voltage_class_by_group
    assert controller.model.connection_for_port(port) is None
    assert len(controller.journal) == journal
    controller.connect_port_to_node(port, bus.node_id)
    assert len(calls) == 2, "A successful preview must never bypass the commit preflight"
    assert controller.model.equipment[apparatus.equipment_id].voltage_class_by_group["main"] == U10
    assert len(controller.journal) == journal + 1


def test_rejected_preview_recovers_after_same_revision_conflict_repair(connected):
    controller, apparatus, bus, _ = connected
    original = controller.model.equipment[apparatus.equipment_id]
    controller.model._equipment[original.id] = replace(original, voltage_class_by_group={"main": U110})
    assert not controller.preview_connection_voltage(apparatus.port_ids[0], bus.node_id).valid
    controller.model._equipment[original.id] = original
    assert controller.preview_connection_voltage(apparatus.port_ids[0], bus.node_id).valid


def test_memento_records_and_nested_fields_are_immutable_and_detached(connected):
    controller, apparatus, _, _ = connected
    model = controller.model
    raw = {"nested": {"values": [1, {"x": 2}]}}
    original = model.equipment[apparatus.equipment_id]
    model._equipment[original.id] = replace(original, properties=raw, extensions=raw)
    model._extensions = ElectricalModel(extensions=raw).extensions
    key = (original.type_id, original.type_version)
    model._equipment_types[key] = replace(model.equipment_types[key],
        property_definitions=(PropertyDefinition("nested_default", default=raw),), extensions=raw)
    state = OperatingState(OperatingStateId("state.immutable"), "Immutable", extensions=raw)
    model._operating_states[state.id] = state
    line = LogicalLine(LogicalLineId("line.immutable"), "Immutable", LineKind.CABLE, U10,
                       (original.id,), inherited_properties=raw, extensions=raw)
    segment = LineConstructionSegment(LineConstructionSegmentId("segment.immutable"), LineKind.CABLE, 1000,
                                      properties=raw, extensions=raw)
    section = LineSection(original.id, line.id, construction_segments=(segment,), extensions=raw)
    model._logical_lines[line.id] = line
    model._line_sections[section.equipment_id] = section
    snapshot = ElectricalModelMemento.capture(model)

    def immutable(value):
        if is_dataclass(value):
            assert value.__dataclass_params__.frozen
            for field in fields(value):
                immutable(getattr(value, field.name))
        elif isinstance(value, Mapping):
            assert isinstance(value, MappingProxyType)
            for key, item in value.items():
                immutable(key)
                immutable(item)
        elif isinstance(value, (tuple, frozenset)):
            for item in value:
                immutable(item)
        else:
            assert value is None or isinstance(value, (str, int, float, bool, Enum))
    immutable(snapshot)
    raw["nested"]["values"][1]["x"] = 999
    assert snapshot.equipment[original.id].properties["nested"]["values"][1]["x"] == 2
    model._equipment[original.id] = original
    assert snapshot != ElectricalModelMemento.capture(model)
    assert snapshot.equipment[original.id].properties["nested"]["values"][1]["x"] == 2


def test_mouse_hover_same_terminal_does_not_recompile_topology(canvas_factory, monkeypatch):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from rza_calc.editor.connection_tool import ConnectionTargetFeedback
    from rza_calc.topology import TopologyEngine
    from test_ui_direct_connections import _pair, _mouse, _state
    canvas, ports = _pair(canvas_factory)
    before = _state(canvas)
    calls = []
    original = TopologyEngine._compile_snapshot
    def compile(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(TopologyEngine, "_compile_snapshot", compile)
    canvas.scene.begin_connection(ports[0])
    target = ports[1].scenePos()
    _mouse(canvas, "move", target)
    assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.COMPATIBLE
    first_count = len(calls)
    assert first_count > 0
    for index in range(20):
        _mouse(canvas, "move", target + QPointF((index % 3) - 1, 0))
        assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.COMPATIBLE
    assert len(calls) == first_count
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert _state(canvas) == before


# Reuse the existing real-Qt fixture, including its uncaught signal exception guard.
from test_ui_direct_connections import canvas_factory
