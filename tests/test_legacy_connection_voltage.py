"""Legacy nominal voltage survives deliberate disconnection, never by drawing order."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from rza_calc.adapters import import_legacy_network
from rza_calc.core.model import Network, Node, TransformerBranch, Transformer3W, LineBranch, TieBranch
from rza_calc.domain.electrical import DomainInvariantError, VoltageClass, VoltageClassId, thaw_json
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor.connection_voltage import endpoint_voltage, check_connection_voltage
from rza_calc.editor.controller import EditorCommandError, ProjectEditorController
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict
from rza_calc.io.project import load_project, save_project


DEMO = Path(__file__).resolve().parent.parent / 'rza_calc/examples/energoraion.json'
# Authorized bus-attachment drawing layout; electrical fingerprint is unchanged.
DEMO_SHA256 = '6bebd1052a76a17070393bf46ceaa776141d0bc58a3c6884f6e993547bee2215'
DEMO_FINGERPRINT = 'b275e47230495c199e8c4ad0158838af0f3a3c14a2767fc85a98bbe7a1164445'
U04 = VoltageClassId('builtin.voltage.ac.0_4kv')
U10 = VoltageClassId('builtin.voltage.ac.10kv')
U35 = VoltageClassId('builtin.voltage.ac.35kv')
U110 = VoltageClassId('builtin.voltage.ac.110kv')


def _bytes(model, *, ignore_revision=False):
    payload = electrical_model_to_dict(model)
    if ignore_revision:
        # Undo restores content, not the monotonically increasing cache revision.
        payload.pop('revision')
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')


def _actual():
    controller = ProjectEditorController(load_project(DEMO))
    equipment = next(row for row in controller.model.equipment.values()
                     if row.name == 'КТП Ф-1 630 кВ·А (Центральная)')
    ports = {port.role: port.id for port in controller.model.ports_of(equipment.id)}
    routes = {role: next(route.id for route in controller.diagram.routes.values()
              if port_id in (route.start_anchor.target_port_id, route.end_anchor.target_port_id))
              for role, port_id in ports.items()}
    return controller, equipment, ports, routes


def _legacy(kind='two', *, reverse=False, nameplate=(10, .4), node_voltages=(10, .4)):
    network = Network('Legacy voltage test')
    for key, value in zip(('first', 'second'), node_voltages):
        network.add_node(Node(key, key, value))
    first, second = ('second', 'first') if reverse else ('first', 'second')
    if kind == 'two':
        network.add_branch(TransformerBranch('T', 'T', first, second, s_nom=630,
                                             u_hv=nameplate[0], u_lv=nameplate[1], uk=5.5))
    elif kind == 'three':
        network.add_node(Node('third', 'third', 35))
        network.add_transformer3w(Transformer3W('T', 'T', 'first', 'third', 'second',
                                                25000, node_voltages[0], 35, node_voltages[1], 10, 17, 6))
    else:
        branch = LineBranch if kind == 'line' else TieBranch
        network.add_branch(branch('T', 'T', first, second))
    model = import_legacy_network(network)
    equipment = next(iter(model.equipment.values()))
    return model, equipment, {port.role: port.id for port in model.ports_of(equipment.id)}


def _detached(model, ports):
    result = ElectricalModelMemento.capture(model).to_model()
    for port_id in ports:
        connection = result.connection_for_port(port_id)
        if connection is not None:
            result.remove_connection(connection.id)
    return result


def _payload(model, equipment_id, **changes):
    saved = ElectricalModelMemento.capture(model)
    equipment = model.equipment[equipment_id]
    properties = thaw_json(equipment.properties)
    properties['legacy_payload'].update(changes)
    return replace(saved, equipment={**saved.equipment, equipment_id: replace(equipment, properties=properties)}).to_model()


def _state(controller):
    return (_bytes(controller.model), diagram_to_dict(controller.diagram), controller.journal,
            controller.can_undo, controller.can_redo)


def test_querying_every_original_legacy_port_preserves_frozen_content_and_fingerprint():
    controller, _, _, _ = _actual()
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == DEMO_SHA256
    assert electrical_model_fingerprint(controller.model) == DEMO_FINGERPRINT
    before = _state(controller)
    assert len(controller.model.ports) == 160
    for port_id in controller.model.ports:
        assert endpoint_voltage(controller.model, port_id).valid
    assert _state(controller) == before
    assert electrical_model_fingerprint(controller.model) == DEMO_FINGERPRINT


def test_actual_plain_wire_deletion_retains_nominal_side_and_undo_restores_exact_content():
    controller, equipment, ports, routes = _actual()
    model = controller.model
    original = _bytes(model, ignore_revision=True)
    original_diagram = controller.diagram
    original_ids = set(model.ports), set(model.equipment), set(model.electrical_nodes)
    node_id = model.node_for_port(ports['from']).id
    journal = len(controller.journal)
    controller.delete_diagram_route(routes['from'])
    assert len(controller.journal) == journal + 1
    assert controller.model.connection_for_port(ports['from']) is None
    assert endpoint_voltage(controller.model, ports['from']).voltage_class_id == U10
    assert endpoint_voltage(controller.model, ports['to']).voltage_class_id == U04
    declaration = controller.model.equipment[equipment.id].extensions['editor_port_nominals']['from']
    assert declaration == {'voltage_class_id': U10.value, 'source': 'connected_nominal_zone'}
    assert (set(model.ports), set(model.equipment), set(model.electrical_nodes)) == original_ids
    after_delete = _state(controller)
    for wrong_class in (U04, U110):
        assert not check_connection_voltage(model, ports['from'], wrong_class).valid
    assert _state(controller) == after_delete
    controller.connect_port_to_node(ports['from'], node_id)
    assert model.node_for_port(ports['from']).id == node_id
    controller.undo()
    assert model.connection_for_port(ports['from']) is None
    controller.undo()
    assert _bytes(model, ignore_revision=True) == original
    assert controller.diagram.representations == original_diagram.representations
    assert controller.diagram.routes == original_diagram.routes
    assert electrical_model_fingerprint(model) == DEMO_FINGERPRINT
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == DEMO_SHA256


def test_actual_both_sides_can_be_deleted_in_either_order_without_losing_their_classes():
    for order in (('from', 'to'), ('to', 'from')):
        controller, equipment, ports, routes = _actual()
        expected = {'from': U10, 'to': U04}
        for role in order:
            controller.delete_diagram_route(routes[role])
        assert all(controller.model.connection_for_port(port) is None for port in ports.values())
        for role, port in ports.items():
            assert endpoint_voltage(controller.model, port).voltage_class_id == expected[role]
        assert set(controller.model.equipment[equipment.id].extensions['editor_port_nominals']) == set(ports)
        controller.undo()
        controller.undo()
        assert electrical_model_fingerprint(controller.model) == DEMO_FINGERPRINT


def test_deleted_legacy_connection_and_saved_nominal_reopen_losslessly(tmp_path):
    controller, equipment, ports, routes = _actual()
    for route_id in routes.values():
        controller.delete_diagram_route(route_id)
    expected_bytes, fingerprint = _bytes(controller.model), electrical_model_fingerprint(controller.model)
    output = tmp_path / 'detached-legacy.json'
    save_project(output, controller._project)
    reopened = load_project(output)
    assert _bytes(reopened.electrical_model) == expected_bytes
    assert electrical_model_fingerprint(reopened.electrical_model) == fingerprint
    assert reopened.diagram.routes == controller.diagram.routes
    assert reopened.diagram.representations == controller.diagram.representations
    assert endpoint_voltage(reopened.electrical_model, ports['from']).voltage_class_id == U10
    assert endpoint_voltage(reopened.electrical_model, ports['to']).voltage_class_id == U04
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == DEMO_SHA256


@pytest.mark.parametrize('reverse', (False, True))
@pytest.mark.parametrize('passport_reverse', (False, True))
@pytest.mark.parametrize('detached_role', ('from', 'to'))
def test_already_detached_2w_uses_the_connected_peer_not_from_to_or_passport_order(reverse, passport_reverse, detached_role):
    model, equipment, ports = _legacy(reverse=reverse, nameplate=(.4, 10) if passport_reverse else (10, .4))
    expected = model.node_for_port(ports[detached_role]).declared_voltage_class_id
    model = _detached(model, (ports[detached_role],))  # old file without a capture extension
    before = _bytes(model)
    assert endpoint_voltage(model, ports[detached_role]).voltage_class_id == expected
    assert _bytes(model) == before and 'editor_port_nominals' not in model.equipment[equipment.id].extensions


@pytest.mark.parametrize('reverse', (False, True))
def test_capture_both_reversed_2w_sides_preserves_actual_connected_classes(reverse):
    from rza_calc.editor.legacy_voltage import preserve_disconnected_legacy_voltages
    before, equipment, ports = _legacy(reverse=reverse)
    expected = {role: before.node_for_port(port).declared_voltage_class_id for role, port in ports.items()}
    after = _detached(before, ports.values())
    original_before, original_after = _bytes(before), _bytes(after)
    captured = preserve_disconnected_legacy_voltages(before, after)
    assert _bytes(before) == original_before and _bytes(after) == original_after
    for role, port in ports.items():
        assert endpoint_voltage(captured, port).voltage_class_id == expected[role]
        assert captured.equipment[equipment.id].extensions['editor_port_nominals'][role]['voltage_class_id'] == expected[role].value
    assert set(captured.ports) == set(after.ports) and captured.connections == after.connections


@pytest.mark.parametrize('role', ('from', 'to'))
def test_two_detached_uncaptured_2w_sides_remain_ambiguous(role):
    model, _, ports = _legacy()
    model = _detached(model, ports.values())
    assert not endpoint_voltage(model, ports[role]).valid


def test_nameplate_stage_tolerance_is_not_used_to_invent_an_unknown_nominal_class():
    model, _, ports = _legacy(nameplate=(115, 11), node_voltages=(110, 10))
    model = _detached(model, (ports['from'],))
    before = _bytes(model)
    assert not endpoint_voltage(model, ports['from']).valid
    assert not check_connection_voltage(model, ports['from'], U110).valid
    assert _bytes(model) == before


@pytest.mark.parametrize('nameplate', ((10, 10), (10, None), (10, '0.4'), (10, True), (10, -0.4)))
def test_ambiguous_or_malformed_nameplate_does_not_become_a_side_class(nameplate):
    model, equipment, ports = _legacy()
    model = _payload(model, equipment.id, u_hv=nameplate[0], u_lv=nameplate[1])
    model = _detached(model, (ports['from'],))
    assert not endpoint_voltage(model, ports['from']).valid


def test_duplicate_class_in_untrusted_snapshot_is_not_resolved_by_first_match():
    model, _, ports = _legacy()
    duplicate = VoltageClass(VoltageClassId('duplicate.10kv'), 10000, 'Другая 10 кВ')
    with pytest.raises(DomainInvariantError):
        model.register_voltage_class(duplicate)  # normal domain API correctly rejects this
    saved = ElectricalModelMemento.capture(model)
    model = replace(saved, voltage_classes={**saved.voltage_classes, duplicate.id: duplicate}).to_model()
    model = _detached(model, (ports['from'],))
    assert not endpoint_voltage(model, ports['from']).valid


@pytest.mark.parametrize('role,expected', (('hv', U110), ('mv', U35), ('lv', U10)))
def test_3w_has_explicit_semantic_sides_even_when_all_terminals_are_disconnected(role, expected):
    model, _, ports = _legacy('three', node_voltages=(110, 10))
    model = _detached(model, ports.values())
    before = _bytes(model)
    assert endpoint_voltage(model, ports[role]).voltage_class_id == expected
    assert _bytes(model) == before


@pytest.mark.parametrize('kind', ('line', 'tie'))
@pytest.mark.parametrize('role', ('from', 'to'))
def test_legacy_conductive_pair_can_use_its_known_other_terminal_without_a_transformer_rule(kind, role):
    model, _, ports = _legacy(kind, node_voltages=(10, 10))
    model = _detached(model, (ports[role],))
    before = _bytes(model)
    assert endpoint_voltage(model, ports[role]).voltage_class_id == U10
    assert _bytes(model) == before


@pytest.mark.parametrize('rotation', (0, 90, 180, 270))
def test_saved_disconnected_side_voltage_does_not_follow_symbol_rotation(rotation):
    controller, _, ports, routes = _actual()
    controller.delete_diagram_route(routes['from'])
    representation = next(row for row in controller.diagram.representations.values()
                          if row.equipment_id == controller.model.ports[ports['from']].equipment_id)
    # Graphical-only test setup: no rerouting or calculation data assignment.
    controller._project.diagram = replace(controller.diagram, representations={**controller.diagram.representations,
        representation.id: replace(representation, rotation_deg=rotation)})
    before = _bytes(controller.model)
    assert endpoint_voltage(controller.model, ports['from']).voltage_class_id == U10
    assert endpoint_voltage(controller.model, ports['to']).voltage_class_id == U04
    assert _bytes(controller.model) == before


def test_explicit_legacy_choice_is_undoable_and_cannot_modify_a_connected_terminal():
    controller, equipment, ports, routes = _actual()
    for route_id in routes.values():
        controller.delete_diagram_route(route_id)
    before = _bytes(controller.model, ignore_revision=True)
    journal = len(controller.journal)
    controller.set_legacy_port_voltage_class(ports['from'], U110)
    assert len(controller.journal) == journal + 1
    assert endpoint_voltage(controller.model, ports['from']).voltage_class_id == U110
    assert endpoint_voltage(controller.model, ports['to']).voltage_class_id == U04
    assert controller.model.equipment[equipment.id].extensions['editor_port_nominals']['from']['source'] == 'explicit'
    controller.undo()
    assert _bytes(controller.model, ignore_revision=True) == before
    controller.redo()
    assert endpoint_voltage(controller.model, ports['from']).voltage_class_id == U110
    controller.undo()
    controller.undo()  # restore the connected second side
    connected = next(port for port in ports.values() if controller.model.connection_for_port(port) is not None)
    state = _state(controller)
    with pytest.raises(EditorCommandError):
        controller.set_legacy_port_voltage_class(connected, U110)
    assert _state(controller) == state


def test_invalid_explicit_class_and_rejected_connection_leave_no_capture_side_effect():
    controller, _, ports, routes = _actual()
    before = _state(controller)
    with pytest.raises(EditorCommandError):
        controller.set_legacy_port_voltage_class(ports['from'], VoltageClassId('missing.class'))
    assert _state(controller) == before
    controller.delete_diagram_route(routes['from'])
    wrong_node = next(node.id for node in controller.model.electrical_nodes.values()
                      if node.declared_voltage_class_id == U110)
    before = _state(controller)
    with pytest.raises(EditorCommandError):
        controller.connect_port_to_node(ports['from'], wrong_node)
    assert _state(controller) == before


def test_legacy_nominals_roundtrip_through_canonical_serializer_without_adapter_mutation():
    controller, _, ports, routes = _actual()
    controller.delete_diagram_route(routes['from'])
    raw = electrical_model_to_dict(controller.model)
    reopened = electrical_model_from_dict(raw)
    assert _bytes(reopened) == _bytes(controller.model)
    assert electrical_model_fingerprint(reopened) == electrical_model_fingerprint(controller.model)
    assert endpoint_voltage(reopened, ports['from']).voltage_class_id == U10


def _stored(model, equipment_id, entries):
    saved = ElectricalModelMemento.capture(model)
    equipment = model.equipment[equipment_id]
    return replace(saved, equipment={**saved.equipment, equipment_id: replace(
        equipment, extensions={**equipment.extensions, 'editor_port_nominals': entries})}).to_model()


@pytest.mark.parametrize('entry', (
    None, 'invalid', {},
    {'source': 'unverified', 'voltage_class_id': U10.value},
    {'source': {'bad': 'object'}, 'voltage_class_id': U10.value},
    {'source': 'explicit', 'voltage_class_id': None},
    {'source': 'explicit', 'voltage_class_id': 10000},
    {'source': 'explicit', 'voltage_class_id': ''},
    {'source': 'explicit', 'voltage_class_id': ' '},
    {'source': 'explicit', 'voltage_class_id': ' invalid '},
    {'source': 'explicit', 'voltage_class_id': 'invalid\nclass'},
    {'source': 'explicit', 'voltage_class_id': 'removed.voltage.class'},
))
def test_malformed_or_missing_saved_class_remains_unknown_without_exception_or_auto_heal(entry):
    model, equipment, ports = _legacy()
    model = _detached(model, ports.values())
    model = _stored(model, equipment.id, {'from': entry})
    before = _bytes(model)
    result = endpoint_voltage(model, ports['from'])
    assert not result.valid
    assert not check_connection_voltage(model, ports['from'], U10).valid
    assert _bytes(model) == before


def test_saved_class_conflicting_with_known_connected_zone_is_not_used_as_a_bypass():
    model, equipment, ports = _legacy()
    model = _stored(model, equipment.id, {'from': {'source': 'explicit', 'voltage_class_id': U110.value}})
    before = _bytes(model)
    assert not endpoint_voltage(model, ports['from']).valid
    assert not check_connection_voltage(model, ports['from'], U110).valid
    assert not check_connection_voltage(model, ports['from'], U10).valid
    assert _bytes(model) == before


def test_explicit_network_class_is_not_silently_rewritten_after_a_nameplate_edit():
    from rza_calc.editor.legacy_voltage import set_legacy_port_voltage
    model, equipment, ports = _legacy()
    model = _detached(model, ports.values())
    model = set_legacy_port_voltage(model, ports['from'], U10)
    model = _payload(model, equipment.id, u_hv=35)
    before = _bytes(model)
    # The electrical guard checks declared network class, not the full
    # nameplate acceptance performed by the calculation validator.
    assert endpoint_voltage(model, ports['from']).voltage_class_id == U10
    assert model.equipment[equipment.id].properties['legacy_payload']['u_hv'] == 35
    assert _bytes(model) == before


def test_unknown_and_conflicting_previous_voltages_are_not_frozen_as_facts():
    from rza_calc.editor.legacy_voltage import preserve_disconnected_legacy_voltages
    model, equipment, ports = _legacy()
    saved = ElectricalModelMemento.capture(model)
    unknown = replace(saved, electrical_nodes={key: replace(node, declared_voltage_class_id=None)
                      for key, node in saved.electrical_nodes.items()}).to_model()
    after = _detached(unknown, ports.values())
    captured = preserve_disconnected_legacy_voltages(unknown, after)
    assert 'editor_port_nominals' not in captured.equipment[equipment.id].extensions
    conflict = _stored(model, equipment.id, {'from': {'source': 'explicit', 'voltage_class_id': U110.value}})
    after = _detached(conflict, (ports['from'],))
    captured = preserve_disconnected_legacy_voltages(conflict, after)
    assert captured.equipment[equipment.id].extensions == after.equipment[equipment.id].extensions


def test_removing_an_entire_legacy_apparatus_does_not_restore_it_via_voltage_capture():
    from rza_calc.editor.legacy_voltage import preserve_disconnected_legacy_voltages
    before, equipment, ports = _legacy()
    after = ElectricalModelMemento.capture(before).to_model()
    after.remove_equipment(equipment.id, cascade=True)
    captured = preserve_disconnected_legacy_voltages(before, after)
    assert captured is after
    assert equipment.id not in captured.equipment and not set(ports.values()) & set(captured.ports)


def test_graphics_command_does_not_add_nominal_metadata_or_change_the_frozen_model():
    controller, equipment, _, _ = _actual()
    representation = next(row for row in controller.diagram.representations.values()
                          if row.equipment_id == equipment.id)
    before = _bytes(controller.model)
    controller.set_label(representation.id, label_x=70, label_y=-80)
    assert _bytes(controller.model) == before
    assert electrical_model_fingerprint(controller.model) == DEMO_FINGERPRINT
    assert not any('editor_port_nominals' in row.extensions for row in controller.model.equipment.values())


@pytest.mark.parametrize('kind', ('two', 'line'))
def test_conflicting_saved_class_of_the_connected_peer_is_not_authoritative_for_fallback(kind):
    model, equipment, ports = _legacy(kind, node_voltages=(10, .4) if kind == 'two' else (10, 10))
    model = _detached(model, (ports['from'],))
    model = _stored(model, equipment.id, {'to': {'source': 'explicit', 'voltage_class_id': U110.value}})
    before = _bytes(model)
    assert not endpoint_voltage(model, ports['to']).valid
    assert not endpoint_voltage(model, ports['from']).valid
    assert _bytes(model) == before


def test_nonlegacy_type_without_a_voltage_group_cannot_use_legacy_inference_or_assignment():
    from rza_calc.editor.legacy_voltage import is_legacy_port, set_legacy_port_voltage
    model, equipment, ports = _legacy()
    model = _detached(model, (ports['from'],))
    saved = ElectricalModelMemento.capture(model)
    key = (equipment.type_id, equipment.type_version)
    model = replace(saved, equipment_types={**saved.equipment_types, key: replace(
        saved.equipment_types[key], behavior_key='custom.transformer_2w')}).to_model()
    before = _bytes(model)
    assert model.port_definition(ports['from']).voltage_group is None
    assert not is_legacy_port(model, ports['from'])
    assert not endpoint_voltage(model, ports['from']).valid
    with pytest.raises(DomainInvariantError):
        set_legacy_port_voltage(model, ports['from'], U10)
    assert _bytes(model) == before


def test_native_group_port_requires_its_native_voltage_setter_not_legacy_metadata():
    from test_ui_connected_commands import _controller
    controller = _controller()
    equipment = controller.add_equipment('builtin.load', 'Нативная нагрузка', x=0, y=0)
    port_id = equipment.port_ids[0]
    before = _state(controller)
    assert not endpoint_voltage(controller.model, port_id).valid
    with pytest.raises(EditorCommandError):
        controller.set_legacy_port_voltage_class(port_id, U10)
    assert _state(controller) == before
    assert 'editor_port_nominals' not in controller.model.equipment[equipment.equipment_id].extensions


def test_analysis_mode_cannot_assign_a_disconnected_legacy_terminal():
    from rza_calc.editor.state import EditorMode
    controller, _, ports, routes = _actual()
    controller.delete_diagram_route(routes['from'])
    controller.set_mode(EditorMode.ANALYSIS)
    before = _state(controller)
    with pytest.raises(EditorCommandError):
        controller.set_legacy_port_voltage_class(ports['from'], U110)
    assert _state(controller) == before
    assert endpoint_voltage(controller.model, ports['from']).voltage_class_id == U10


def test_manual_setter_cannot_claim_a_connected_measurement_provenance():
    from rza_calc.editor.legacy_voltage import set_legacy_port_voltage
    model, _, ports = _legacy()
    model = _detached(model, ports.values())
    before = _bytes(model)
    with pytest.raises(DomainInvariantError):
        set_legacy_port_voltage(model, ports['from'], U10, source='connected_nominal_zone')
    assert _bytes(model) == before
