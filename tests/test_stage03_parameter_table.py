"""Typed spreadsheet gestures exercise real project transactions and stable IDs."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from rza_calc.domain.catalog import UserCatalog
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.history import ProjectMemento
from rza_calc.editor.parameter_editing import ParameterValue
from rza_calc.gui.parameter_table import EquipmentParameterTableDialog, page_equipment_ids
from rza_calc.io.project import load_project, save_project


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _sources():
    model = ElectricalModel.with_builtins("Таблица")
    project = SimpleNamespace(electrical_model=model, user_catalog=UserCatalog(),
        catalog_snapshots=ProjectCatalogSnapshots(),
        diagram=DiagramDocument.create("Схема", (DiagramPage(PageId("page.table"), "Первый лист"),)))
    controller = ProjectEditorController(project)
    first = controller.add_equipment("builtin.external_grid", "Б — источник", x=0, y=0,
        voltage_class_by_group={"main": VoltageClassId("builtin.voltage.ac.10kv")},
        properties={"s_kz_max": 500.0, "s_kz_min": 250.0})
    second = controller.add_equipment("builtin.external_grid", "А — источник", x=240, y=0,
        voltage_class_by_group={"main": VoltageClassId("builtin.voltage.ac.10kv")},
        properties={"s_kz_max": 600.0, "s_kz_min": 300.0})
    return controller, first.equipment_id, second.equipment_id


def _column(dialog, key):
    return dialog.model.FIXED_COLUMNS + next(i for i, spec in enumerate(dialog.model.specs) if spec.key == key)


def _cell(dialog, eid, key):
    source = dialog.model.index(next(i for i, row in enumerate(dialog.model.rows) if row[0].id == eid), _column(dialog, key))
    return dialog.proxy.mapFromSource(source)


def test_sorted_tsv_is_local_and_targets_ids_not_names(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    before = ProjectMemento.capture(controller._project)
    journal = controller.journal
    dialog.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    anchor = dialog.proxy.index(0, _column(dialog, "s_kz_max"))
    expected_ids = [dialog.proxy.index(row, 0).data(dialog.model.ID_ROLE) for row in range(2)]
    dialog.paste_text("750,5\n820.25", anchor)
    assert dialog.model.drafts[(expected_ids[0], "s_kz_max")].value == 750.5
    assert dialog.model.drafts[(expected_ids[1], "s_kz_max")].value == 820.25
    assert ProjectMemento.capture(controller._project) == before
    assert controller.journal == journal
    dialog.reject()
    assert ProjectMemento.capture(controller._project) == before


def test_tsv_blank_keeps_existing_draft_and_outside_block_is_atomic(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    cell = _cell(dialog, first, "s_kz_max")
    dialog.paste_text("740", cell)
    retained = dict(dialog.model.drafts)
    dialog.paste_text("", cell)
    assert dialog.model.drafts == retained
    with pytest.raises(ValueError, match="выходит"):
        dialog.paste_text("1\n2\n3", dialog.proxy.index(0, cell.column()))
    assert dialog.model.drafts == retained


def test_bad_cell_blocks_whole_apply_and_can_be_corrected(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    before = ProjectMemento.capture(controller._project)
    dialog.paste_text("nan", _cell(dialog, first, "s_kz_max"))
    dialog.paste_text("800", _cell(dialog, second, "s_kz_max"))
    assert len(dialog.model.errors) == 1
    dialog.preview_changes()
    assert not dialog.apply_button.isEnabled()
    assert ProjectMemento.capture(controller._project) == before
    dialog.paste_text("700", _cell(dialog, first, "s_kz_max"))
    assert not dialog.model.errors
    dialog.preview_changes()
    assert dialog.preview.valid, dialog.preview.diagnostics
    assert dialog.apply_button.isEnabled()
    length = len(controller.journal)
    dialog.apply_changes()
    assert len(controller.journal) == length + 1
    assert controller.equipment_parameter_snapshot(first).fields["s_kz_max"].value == 700
    assert controller.equipment_parameter_snapshot(second).fields["s_kz_max"].value == 800
    controller.undo()
    assert ProjectMemento.capture(controller._project) == before


def test_preview_stamp_rejects_external_edit_instead_of_overwriting(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    dialog.paste_text("700", _cell(dialog, first, "s_kz_max"))
    dialog.preview_changes()
    assert dialog.apply_button.isEnabled()
    controller.rename_equipment(first, "Чужое новое имя")
    before = ProjectMemento.capture(controller._project)
    dialog.apply_changes()
    assert ProjectMemento.capture(controller._project) == before
    assert "изменился" in dialog.hint.text()
    assert not dialog.apply_button.isEnabled()


def test_current_page_filter_contains_route_equipment_and_all_project_switch(app):
    project = load_project(Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json")
    controller = ProjectEditorController(project)
    visible = page_equipment_ids(controller)
    route_ids = {route.equipment_id for route in controller.diagram.routes.values() if route.equipment_id}
    assert route_ids <= visible
    empty = controller.create_page("Пустой лист")
    controller.set_active_page(empty)
    dialog = EquipmentParameterTableDialog(controller)
    assert dialog.proxy.rowCount() == 0
    dialog.scope_combo.setCurrentIndex(1)
    assert dialog.proxy.rowCount() == len(controller.model.equipment)


def test_legacy_line_units_roundtrip_and_clear_preserve_other_sequence_values(app, tmp_path):
    project = load_project(Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json")
    controller = ProjectEditorController(project)
    eid = next(row.id for row in controller.model.equipment.values() if row.type_id.value == "compat.rza_calc.line")
    original = controller.model.equipment[eid]
    dialog = EquipmentParameterTableDialog(controller)
    dialog.scope_combo.setCurrentIndex(1)
    dialog.family_combo.setCurrentIndex(dialog.family_combo.findData("line"))
    length_key = next(spec.key for spec in dialog.model.specs if "length" in spec.key)
    spec = next(spec for spec in dialog.model.specs if spec.key == length_key)
    assert "m" in (spec.unit, *spec.display_units)
    fp = electrical_model_fingerprint(controller.model)
    dialog.model.set_display_unit(length_key, "m")
    assert electrical_model_fingerprint(controller.model) == fp
    dialog.paste_text("2500,125", _cell(dialog, eid, length_key))
    source_index = dialog.proxy.mapToSource(_cell(dialog, eid, "r0_ohm_per_km"))
    dialog.model.clear_cell(source_index)
    dialog.preview_changes()
    assert dialog.preview.valid, dialog.preview.diagnostics
    dialog.apply_changes()
    branch = project.network.branches["L1"]
    assert branch.length_km == pytest.approx(2.500125)
    assert branch.r0 == .1 and branch.x0 == .08
    assert branch.r0_ohm_per_km is None
    assert branch.x0_ohm_per_km == .3 and branch.r2_ohm_per_km == .12
    assert controller.model.equipment[eid].port_ids == original.port_ids
    path = tmp_path / "table-edited.json"
    save_project(path, project)
    reopened = load_project(path)
    assert reopened.network.branches["L1"].length_km == pytest.approx(2.500125)
    assert electrical_model_fingerprint(reopened.electrical_model) == electrical_model_fingerprint(controller.model)


def test_explicit_source_confirmation_survives_batch_and_units_do_not_confirm(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    index = dialog.proxy.mapToSource(_cell(dialog, first, "s_kz_max"))
    dialog.model.set_parameter_value(index, ParameterValue(700.0, "Паспорт А, стр. 2", DataConfirmation.CONFIRMED, "manual"))
    dialog.preview_changes()
    assert dialog.preview.valid
    dialog.apply_changes()
    actual = controller.equipment_parameter_snapshot(first).fields["s_kz_max"]
    assert actual.value == 700 and actual.source == "Паспорт А, стр. 2"
    assert actual.confirmation == DataConfirmation.CONFIRMED


def test_bulk_snapshot_is_read_once_and_not_rebuilt_by_table_reads(app, monkeypatch):
    controller, first, second = _sources()
    calls = []
    original = controller.equipment_parameter_snapshots
    monkeypatch.setattr(controller, "equipment_parameter_snapshots", lambda ids: (calls.append(tuple(ids)) or original(ids)))
    dialog = EquipmentParameterTableDialog(controller)
    for repeat in range(30):
        for row in range(dialog.model.rowCount()):
            dialog.model.data(dialog.model.index(row, 0))
    assert len(calls) == 1


def test_each_row_uses_its_own_semantic_ct_port_choices(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    selected_port = controller.model.equipment[second].port_ids[0]
    other_port = controller.model.equipment[first].port_ids[0]
    index = _cell(dialog, second, "ct_port")
    dialog.paste_text(selected_port.value, index)
    assert not dialog.model.errors
    assert dialog.model.drafts[(second, "ct_port")].value == selected_port.value
    dialog.paste_text(other_port.value, index)
    assert (second, "ct_port") in dialog.model.errors


def test_missing_filter_follows_explicit_sequence_assumptions_in_local_draft(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    row = next(i for i, record in enumerate(dialog.model.rows) if record[0].id == first)
    assert dialog.model.row_missing(row)
    for key, value in (("negative_sequence_equal_positive", True), ("zero_sequence_connection", "blocked")):
        index = dialog.proxy.mapToSource(_cell(dialog, first, key))
        dialog.model.set_parameter_value(index, ParameterValue(value, "Допущение для проверки"))
    assert not dialog.model.row_missing(row)
    dialog.missing_check.setChecked(True)
    assert dialog.proxy.rowCount() == 1
    assert dialog.proxy.index(0, 0).data(dialog.model.ID_ROLE) == second


def test_max_min_headers_are_distinguishable_and_localized(app):
    controller, first, second = _sources()
    dialog = EquipmentParameterTableDialog(controller)
    maximum = dialog.model.headerData(_column(dialog, "s_kz_max"), Qt.Orientation.Horizontal)
    minimum = dialog.model.headerData(_column(dialog, "s_kz_min"), Qt.Orientation.Horizontal)
    assert maximum != minimum and "Максимальный" in maximum and "Минимальный" in minimum
    assert "МВА" in maximum


def test_clear_inherited_native_parameter_is_explicit_missing_not_parent_value(app):
    from rza_calc.domain.electrical import ElectricalNode, ElectricalNodeId, LineKind
    model = ElectricalModel.with_builtins("Унаследованная линия")
    for name in ("a", "b"):
        model.add_node(ElectricalNode(ElectricalNodeId("node." + name), name,
            declared_voltage_class_id=VoltageClassId("builtin.voltage.ac.10kv")))
    logical, section, _ = model.create_logical_line("Линия", LineKind.CABLE,
        ElectricalNodeId("node.a"), ElectricalNodeId("node.b"), 1000000,
        inherited_properties={"r1_ohm_per_km": .2, "x1_ohm_per_km": .1})
    project = SimpleNamespace(electrical_model=model, user_catalog=UserCatalog(),
        catalog_snapshots=ProjectCatalogSnapshots(),
        diagram=DiagramDocument.create("Схема", (DiagramPage(PageId("page.native"), "Лист"),)))
    controller = ProjectEditorController(project)
    before = ProjectMemento.capture(project)
    dialog = EquipmentParameterTableDialog(controller)
    dialog.scope_combo.setCurrentIndex(1)
    index = dialog.proxy.mapToSource(_cell(dialog, section.equipment_id, "r1_ohm_per_km"))
    dialog.model.clear_cell(index)
    dialog.preview_changes()
    assert dialog.preview.valid, dialog.preview.diagnostics
    assert next(c for c in dialog.preview.changes if c.key == "r1_ohm_per_km").after.value is None
    dialog.apply_changes()
    assert controller.model.effective_equipment_properties(section.equipment_id)["r1_ohm_per_km"] is None
    assert controller.model.logical_lines[logical.id].inherited_properties["r1_ohm_per_km"] == .2
    controller.undo()
    assert ProjectMemento.capture(project) == before


def test_window_table_action_publishes_staleness_for_all_fault_types_without_recalculation(app, monkeypatch):
    import rza_calc.gui.view_model as vm_module
    from rza_calc.core.fault_types import FaultType
    from rza_calc.gui.main_window import MainWindow
    vm = vm_module.ProjectViewModel.open(Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json")
    vm.select("node", "FAULT")
    old_result = vm.current_result
    assert old_result is not None
    window = MainWindow(vm)
    calls = []
    monkeypatch.setattr(vm_module, "run", lambda *args, **kwargs: calls.append(args))
    def interact(dialog):
        eid = next(row[0].id for row in dialog.model.rows if row[2] == "line")
        dialog.family_combo.setCurrentIndex(dialog.family_combo.findData("line"))
        dialog.model.set_display_unit("length_mm", "m")
        dialog.paste_text("3000", _cell(dialog, eid, "length_mm"))
        dialog.preview_changes()
        assert dialog.preview.valid
        dialog.apply_changes()
        return 0
    monkeypatch.setattr(EquipmentParameterTableDialog, "exec", interact)
    try:
        window.parameter_table_action.trigger()
        app.processEvents()
        assert vm.current_result is None
        for kind in FaultType:
            vm.select_fault_type(kind)
            assert all(row.iabc_ka is None and row.i3_ka is None for row in vm.fault_rows())
        window.editor_controller.undo()
        app.processEvents()
        assert vm.current_result is old_result
        assert not calls
    finally:
        window.close()
