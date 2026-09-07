"""A rewired compatibility branch retains its CT on the same physical port."""
from dataclasses import replace
from pathlib import Path

import pytest

from rza_calc.adapters import adapt_to_calculation, import_legacy_network
from rza_calc.core.model import LineBranch, Network, Node, Transformer3W
from rza_calc.domain.electrical import DomainInvariantError, ElectricalNode, ElectricalNodeId, VoltageClassId, thaw_json
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor.controller import EditorCommandError, NewNodeTarget, NodeTarget, ProjectEditorController
from rza_calc.editor.legacy_ct import CT_BINDINGS_KEY, preserve_legacy_ct_bindings
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict
from rza_calc.io.project import load_project, save_project

DEMO = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/energoraion.json"
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _content(model):
    data = electrical_model_to_dict(model)
    data.pop("revision")
    return data


def _actual():
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    equipment = next(row for row in controller.model.equipment.values()
                     if row.name == "КТП Ф-1 630 кВ·А (Центральная)")
    port = controller.model.port_by_role(equipment.id, "from")
    route = next(row for row in controller.diagram.routes.values()
                 if port.id in (row.start_anchor.target_port_id, row.end_anchor.target_port_id))
    bus = next(row for row in controller.model.electrical_nodes.values()
               if row.name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ")
    bus_rep = next(row for row in controller.diagram.representations.values()
                   if row.electrical_node_id == bus.id)
    return controller, equipment, port, route, bus, bus_rep


def _assert_only_ct_payload_changed(old, new, expected):
    old_payload = thaw_json(old.properties["legacy_payload"])
    new_payload = thaw_json(new.properties["legacy_payload"])
    assert new_payload.pop("ct_node") == expected
    old_payload.pop("ct_node")
    assert new_payload == old_payload
    assert new.id == old.id and new.port_ids == old.port_ids
    assert new.type_id == old.type_id and new.normal_position == old.normal_position


def test_actual_cf1_delete_save_reopen_connect_refresh_preserves_ct_port_and_undo(tmp_path):
    original_bytes = DEMO.read_bytes()
    controller, equipment, port, route, bus, bus_rep = _actual()
    original_model, original_diagram = _content(controller.model), controller.diagram
    controller.delete_diagram_route(route.id)
    disconnected = controller.model.equipment[equipment.id]
    binding = disconnected.extensions[CT_BINDINGS_KEY]["ct_node"]
    assert binding == {"port_id": port.id.value, "role": "from", "node_id": "CF1_end"}
    assert disconnected.properties["legacy_payload"]["ct_node"] == "CF1_end"
    assert disconnected.properties["legacy_payload"]["ct_ratio"] == equipment.properties["legacy_payload"]["ct_ratio"]
    assert controller.model.connection_for_port(port.id) is None
    target = tmp_path / "disconnected.json"
    save_project(target, controller._project)
    restored = ProjectEditorController(load_project(target))
    for current in (controller, restored):
        before, count = _content(current.model), len(current.journal)
        current.connect_port_to_node(port.id, bus.id, node_representation_id=bus_rep.id,
                                     target_anchor_key="0.5")
        assert len(current.journal) == count + 1
        changed = current.model.equipment[equipment.id]
        _assert_only_ct_payload_changed(equipment, changed, "c10_1")
        assert changed.extensions[CT_BINDINGS_KEY]["ct_node"]["port_id"] == port.id.value
        current._project.refresh_calculation_view()
        assert not [row for row in current._project.adapter_diagnostics if row.severity == "error"]
        assert current._project.network.branches["CF1_T"].ct_node == "c10_1"
        after = _content(current.model)
        current.undo()
        assert _content(current.model) == before
        current.redo()
        assert _content(current.model) == after
        current._project.refresh_calculation_view()
    controller.undo()
    controller.undo()
    assert _content(controller.model) == original_model
    assert dict(controller.diagram.routes) == dict(original_diagram.routes)
    assert dict(controller.diagram.representations) == dict(original_diagram.representations)
    assert DEMO.read_bytes() == original_bytes


@pytest.mark.parametrize("native", (False, True))
def test_actual_direct_reconnect_updates_only_ct_reference_and_uses_public_node_identity(native):
    controller, equipment, port, _, bus, bus_rep = _actual()
    before, count = _content(controller.model), len(controller.journal)
    target = NewNodeTarget("Новый узел ТТ", 1800, 220, voltage_class_id=U10) if native else NodeTarget(bus.id, bus_rep.id, "0.5")
    controller.reconnect_port(port.id, target)
    assert len(controller.journal) == count + 1
    controller._project.refresh_calculation_view()
    # The native mapping comes from the real full adapter, not the helper.
    adapted = adapt_to_calculation(controller.model)
    node = controller.model.node_for_port(port.id)
    identity = adapted.trace.domain_node_to_legacy[node.id.value]
    assert identity.startswith("N_") if native else identity == "c10_1"
    _assert_only_ct_payload_changed(equipment, controller.model.equipment[equipment.id], identity)
    assert adapted.network.branches["CF1_T"].ct_node == identity
    controller.undo()
    assert _content(controller.model) == before


def _legacy(*, three=False, overrides=False):
    network = Network("CT semantic ports")
    for name, voltage in (("H", 110 if three else 10), ("M", 35), ("L", 10), ("NEW", 110 if three else 10)):
        network.add_node(Node(name, name, voltage))
    if three:
        transformer = Transformer3W("T", "T", "H", "M", "L", 25000, 110, 35, 10,
                                    10, 17, 6, ct_ratio=(600, 5), ct_node="H")
        network.add_transformer3w(transformer)
        if overrides:
            network.branches["T_mv"].ct_ratio = (300, 5)
            network.branches["T_mv"].ct_node = "M"
            network.branches["T_lv"].ct_ratio = (100, 5)
            network.branches["T_lv"].ct_node = transformer.star_node_id
    else:
        network.add_branch(LineBranch("T", "T", "H", "L", ct_ratio=(600, 5), ct_node="H"))
    model = import_legacy_network(network)
    equipment = next(iter(model.equipment.values()))
    return model, equipment


def _move(model, port_id, node_id):
    draft = ElectricalModelMemento.capture(model).to_model()
    connection = draft.connection_for_port(port_id)
    if connection:
        draft.remove_connection(connection.id)
    if node_id is not None:
        draft.connect_port(port_id, node_id)
    return preserve_legacy_ct_bindings(model, draft)


@pytest.mark.parametrize("three", (False, True))
def test_other_terminal_change_never_relocates_ct_or_adds_binding(three):
    model, equipment = _legacy(three=three)
    port = model.port_by_role(equipment.id, "mv" if three else "to")
    changed = _move(model, port.id, None)
    assert changed.equipment[equipment.id] == equipment
    assert changed.equipment[equipment.id].properties["legacy_payload"]["ct_node"] == "H"


def test_three_winding_main_and_generated_cts_keep_distinct_physical_ports_and_star():
    model, equipment = _legacy(three=True, overrides=True)
    original = electrical_model_to_dict(model)
    hv = model.port_by_role(equipment.id, "hv").id
    mv = model.port_by_role(equipment.id, "mv").id
    new = next(row.id for row in model.electrical_nodes.values() if row.name == "NEW")
    changed = _move(model, hv, new)
    assert changed.equipment[equipment.id].properties["legacy_payload"]["ct_node"] == "NEW"
    marker = changed.equipment[equipment.id].extensions["legacy_calculation"]["generated_overrides"]["branches"]
    assert marker["mv"]["ct_node"] == "M"
    assert marker["lv"]["ct_node"] == "T__star"
    disconnected = _move(changed, mv, None)
    binding = disconnected.equipment[equipment.id].extensions[CT_BINDINGS_KEY]["generated.mv"]
    assert binding["port_id"] == mv.value and binding["role"] == "mv"
    reopened = electrical_model_from_dict(electrical_model_to_dict(disconnected))
    old_m = next(row.id for row in model.electrical_nodes.values() if row.name == "M")
    # A fresh 35 kV node proves the override follows mv, not main CT/hv.
    new_m = ElectricalNode(ElectricalNodeId.new(), "NEW_M",
                           declared_voltage_class_id=model.electrical_nodes[old_m].declared_voltage_class_id)
    reopened.add_node(new_m)
    restored = _move(reopened, mv, new_m.id)
    adapted = adapt_to_calculation(restored)
    assert adapted.network.branches["T"].ct_node == "NEW"
    assert adapted.network.branches["T_mv"].ct_node == adapted.trace.domain_node_to_legacy[new_m.id.value]
    assert adapted.network.branches["T_lv"].ct_node == "T__star"
    assert adapted.network.branches["T_mv"].ct_ratio == (300, 5)
    assert electrical_model_to_dict(model) == original


def test_node_merge_rebinds_all_affected_legacy_ct_ports_not_only_requested_port():
    model, equipment = _legacy()
    other, _ = model.create_equipment("compat.rza_calc.line", "Other", properties={
        "legacy_payload": thaw_json(equipment.properties["legacy_payload"])},
        extensions={"legacy_calculation": {**thaw_json(equipment.extensions["legacy_calculation"]), "legacy_id": "OTHER"}})
    source = model.node_for_port(model.port_by_role(equipment.id, "from").id)
    target = next(row for row in model.electrical_nodes.values() if row.name == "NEW")
    model.connect_port(model.port_by_role(other.id, "from").id, source.id)
    other_end = model.node_for_port(model.port_by_role(equipment.id, "to").id)
    model.connect_port(model.port_by_role(other.id, "to").id, other_end.id)
    draft = ElectricalModelMemento.capture(model).to_model()
    # Equivalent to a connection command joining whole endpoint nodes.
    for row in tuple(draft.connections.values()):
        if row.electrical_node_id == source.id:
            draft.remove_connection(row.id)
            draft.connect_port(row.port_id, target.id)
    changed = preserve_legacy_ct_bindings(model, draft)
    for identifier in (equipment.id, other.id):
        assert changed.equipment[identifier].properties["legacy_payload"]["ct_node"] == "NEW"
        assert changed.equipment[identifier].extensions[CT_BINDINGS_KEY]["ct_node"]["port_id"] == model.port_by_role(identifier, "from").id.value


@pytest.mark.parametrize("corruption", ("missing_3w_id", "unknown_generated_ct"))
def test_corrupt_generated_ct_is_domain_error_not_uncaught_qt_key_error(corruption):
    model, equipment = _legacy(three=True, overrides=True)
    extensions = thaw_json(equipment.extensions)
    if corruption == "missing_3w_id":
        extensions["legacy_calculation"].pop("legacy_id")
    else:
        extensions["legacy_calculation"]["generated_overrides"]["branches"]["lv"]["ct_node"] = "UNKNOWN_STAR"
    saved = ElectricalModelMemento.capture(model)
    model = replace(saved, equipment={equipment.id: replace(equipment, extensions=extensions)}).to_model()
    original = electrical_model_to_dict(model)
    port = model.port_by_role(equipment.id, "hv").id
    new = next(row.id for row in model.electrical_nodes.values() if row.name == "NEW")
    with pytest.raises(DomainInvariantError, match="ТТ"):
        _move(model, port, new)
    assert electrical_model_to_dict(model) == original


@pytest.mark.parametrize("corruption", ("unknown_ct", "ambiguous", "stale_binding"))
def test_unresolvable_ct_never_guesses_a_port_and_controller_rejects_atomically(corruption):
    controller, equipment, port, route, _, _ = _actual()
    if corruption == "unknown_ct":
        payload = thaw_json(equipment.properties["legacy_payload"])
        payload["ct_node"] = "NONEXISTENT"
        controller.model.set_equipment_property(equipment.id, "legacy_payload", payload)
    elif corruption == "ambiguous":
        opposite = controller.model.port_by_role(equipment.id, "to").id
        node = controller.model.node_for_port(opposite)
        saved = ElectricalModelMemento.capture(controller.model)
        extensions = thaw_json(node.extensions)
        extensions["legacy_calculation"]["legacy_id"] = "CF1_end"
        controller._project.electrical_model = replace(saved, electrical_nodes={
            **saved.electrical_nodes, node.id: replace(node, extensions=extensions)}).to_model()
    else:
        saved = ElectricalModelMemento.capture(controller.model)
        changed = replace(equipment, extensions={**equipment.extensions, CT_BINDINGS_KEY: {
            "ct_node": {"port_id": port.id.value, "role": "from", "node_id": "STALE"}}})
        controller._project.electrical_model = replace(saved, equipment={**saved.equipment, equipment.id: changed}).to_model()
    # Reset only test history after constructing intentionally invalid input.
    controller = ProjectEditorController(controller._project)
    before = _content(controller.model), controller.diagram, len(controller.journal)
    with pytest.raises(EditorCommandError, match="ТТ"):
        controller.delete_diagram_route(route.id)
    assert (_content(controller.model), controller.diagram, len(controller.journal)) == before


def test_readonly_and_graphics_commands_do_not_persist_ct_bindings_or_change_fingerprint():
    controller, equipment, _, _, _, _ = _actual()
    original, fingerprint = dict(controller.model.equipment), electrical_model_fingerprint(controller.model)
    assert preserve_legacy_ct_bindings(controller.model, controller.model) is controller.model
    representation = next(row for row in controller.diagram.representations.values() if row.equipment_id == equipment.id)
    controller.set_label(representation.id, text="Only drawing")
    controller._project.refresh_calculation_view()
    assert dict(controller.model.equipment) == original
    assert electrical_model_fingerprint(controller.model) == fingerprint


@pytest.mark.parametrize("voltage", ("builtin.voltage.ac.0_4kv", "builtin.voltage.ac.110kv"))
def test_wrong_voltage_reconnect_rejection_does_not_write_ct_metadata(voltage):
    controller, _, port, _, _, _ = _actual()
    before = _content(controller.model), controller.diagram, len(controller.journal)
    with pytest.raises(EditorCommandError):
        controller.reconnect_port(port.id, NewNodeTarget("Wrong", 1800, 220, voltage_class_id=VoltageClassId(voltage)))
    assert (_content(controller.model), controller.diagram, len(controller.journal)) == before
