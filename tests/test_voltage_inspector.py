"""A strict unknown-voltage guard must still allow explicit draft setup."""
from pathlib import Path

from PySide6.QtWidgets import QComboBox
import pytest

from rza_calc.domain.electrical import VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.editor_panels import EditorWorkspaceWidget, PropertyField, PropertyInspectorPanel
from rza_calc.io.project import load_project
from test_stage4_editor_interaction import _app, _controller

U10 = VoltageClassId("builtin.voltage.ac.10kv")
U110 = VoltageClassId("builtin.voltage.ac.110kv")


@pytest.fixture
def workspace():
    _app()
    widget = EditorWorkspaceWidget(_controller())
    yield widget
    widget.close()


def _select(workspace, representation_id):
    workspace._selected_ids = (representation_id,)
    workspace._update_inspector()
    return {field.key: field for field in workspace._property_fields()[0]}


def _combo(workspace, key):
    # QTreeWidget disposes removed editors with deleteLater; findChild could
    # return an old hidden editor instead of the current row's editor.
    tree = workspace.inspector.tree
    for group_index in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(group_index)
        for index in range(group.childCount()):
            item = group.child(index)
            if item.data(0, workspace.inspector.FIELD_ROLE) == key:
                combo = tree.itemWidget(item, 1)
                assert isinstance(combo, QComboBox)
                return combo
    raise AssertionError(key)


def _choose(combo, voltage):
    index = combo.findData(voltage.value)
    assert index >= 0
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def test_opening_unknown_voltage_properties_never_assigns_a_default(workspace):
    controller = workspace.controller
    added = controller.add_equipment("builtin.circuit_breaker", "Новый Q")
    before = electrical_model_fingerprint(controller.model), controller.journal
    fields = _select(workspace, added.representation_id)
    field = fields["equipment.voltage_class.main"]
    combo = _combo(workspace, field.key)
    assert field.value is None and field.editable and field.required
    assert combo.currentIndex() == -1 and combo.isEnabled()
    assert before == (electrical_model_fingerprint(controller.model), controller.journal)


def test_explicit_group_choice_preserves_ids_and_undoes(workspace):
    controller = workspace.controller
    added = controller.add_equipment("builtin.circuit_breaker", "Новый Q")
    before = electrical_model_fingerprint(controller.model)
    _select(workspace, added.representation_id)
    _choose(_combo(workspace, "equipment.voltage_class.main"), U10)
    equipment = controller.model.equipment[added.equipment_id]
    assert equipment.voltage_class_by_group == {"main": U10}
    assert equipment.port_ids == added.port_ids
    assert not controller.model.connections
    assert "rated_voltage_v" not in equipment.properties
    controller.undo()
    assert electrical_model_fingerprint(controller.model) == before


def test_transformer_sides_are_separate_semantic_choices(workspace):
    controller = workspace.controller
    added = controller.add_equipment("builtin.transformer_3w", "Т1", rotation_deg=180)
    fields = _select(workspace, added.representation_id)
    keys = {key for key in fields if key.startswith("equipment.voltage_class.")}
    assert keys == {"equipment.voltage_class.hv", "equipment.voltage_class.mv", "equipment.voltage_class.lv"}
    _choose(_combo(workspace, "equipment.voltage_class.hv"), U110)
    _select(workspace, added.representation_id)
    _choose(_combo(workspace, "equipment.voltage_class.lv"), U10)
    equipment = controller.model.equipment[added.equipment_id]
    assert equipment.voltage_class_by_group == {"hv": U110, "lv": U10}
    assert equipment.port_ids == added.port_ids


def test_bus_voltage_is_explicit_and_readonly_after_connection(workspace):
    controller = workspace.controller
    bus = controller.add_electrical_node("Новая шина", x=200, symbol_key="busbar_horizontal")
    _select(workspace, bus.representation_id)
    assert _combo(workspace, "node.voltage_class").currentIndex() == -1
    _choose(_combo(workspace, "node.voltage_class"), U10)
    assert controller.model.electrical_nodes[bus.node_id].declared_voltage_class_id == U10
    load = controller.add_equipment("builtin.load", "Нагрузка", voltage_class_by_group={"main": U10})
    controller.connect_port_to_node(load.port_ids[0], bus.node_id)
    fields = _select(workspace, bus.representation_id)
    assert not fields["node.voltage_class"].editable
    assert not _combo(workspace, "node.voltage_class").isEnabled()
    fields = _select(workspace, load.representation_id)
    assert not fields["equipment.voltage_class.main"].editable


def test_view_mode_cannot_assign_voltage(workspace):
    from rza_calc.editor.state import EditorMode
    controller = workspace.controller
    added = controller.add_equipment("builtin.load", "Нагрузка")
    controller.set_mode(EditorMode.ANALYSIS)
    _select(workspace, added.representation_id)
    assert not _combo(workspace, "equipment.voltage_class.main").isEnabled()


def test_imported_transformer_shows_voltage_by_role_without_new_groups():
    _app()
    project = load_project(str(Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/energoraion.json"))
    controller = ProjectEditorController(project)
    before = electrical_model_fingerprint(controller.model)
    workspace = EditorWorkspaceWidget(controller)
    try:
        representation = next(row for row in controller.diagram.representations.values()
                              if row.equipment_id is not None
                              and "Т1 Северная" in controller.model.equipment[row.equipment_id].name)
        fields = _select(workspace, representation.id)
        voltages = [field for key, field in fields.items() if key.startswith("equipment.port_voltage.")]
        assert len(voltages) == 2 and all(not field.editable for field in voltages)
        assert {field.value for field in voltages} == {"110 кВ", "10 кВ"}
        assert not any(key.startswith("equipment.voltage_class.") for key in fields)
        assert electrical_model_fingerprint(controller.model) == before
    finally:
        workspace.close()


@pytest.mark.parametrize("width,source_hidden", [(330, True), (760, False)])
def test_inspector_values_and_units_stay_visible_at_panel_width(width, source_hidden):
    app = _app()
    panel = PropertyInspectorPanel()
    try:
        panel.resize(width, 400)
        panel.show()
        panel.set_fields("Свойства", (PropertyField(
            "test.current", "Очень длинное имя паспортного свойства выключателя",
            630.0, "Параметры", unit="А", source="Проверяемый паспорт",
        ),), editable=True)
        app.processEvents()
        tree = panel.tree
        header = tree.header()
        assert tree.isColumnHidden(3) is source_hidden
        assert not tree.isColumnHidden(1) and not tree.isColumnHidden(2)
        assert header.sectionSize(1) >= 90
        assert header.sectionViewportPosition(2) + header.sectionSize(2) <= tree.viewport().width()
        item = tree.topLevelItem(0).child(0)
        assert item.text(1) == "630" and item.text(2) == "А"
        assert "Проверяемый паспорт" in item.toolTip(1)
        assert "Очень длинное" in item.toolTip(0)
    finally:
        panel.close()
