"""Catalog GUI keeps templates separate and applies only reviewed stable refs."""
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from rza_calc.domain.catalog import CatalogCategoryId, CatalogEntry, CatalogEntryId, CatalogOrigin
from rza_calc.domain.electrical import DataConfirmation, EquipmentTypeId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.history import ProjectMemento
from rza_calc.editor.parameter_editing import ParameterPatch, ParameterValue, PROVENANCE_KEY
from rza_calc.gui.project_parameter_catalog import CatalogEntryEditorDialog, ProjectParameterCatalogDialog
from rza_calc.io.project import load_project, save_project


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _project():
    project = load_project(Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json")
    controller = ProjectEditorController(project)
    eid = next(row.id for row in controller.model.equipment.values() if row.type_id.value == "compat.rza_calc.line")
    return project, controller, eid


def _entry(**properties):
    return CatalogEntry(CatalogEntryId("catalog.stage3.cable"), CatalogOrigin.USER,
        CatalogCategoryId("lines"), "Кабель А", EquipmentTypeId("builtin.line_section.cable"), 1,
        source="Паспорт кабеля А", properties={"conductor_mark": "Кабель А", "r1_ohm_per_km": .25,
            "x1_ohm_per_km": .12, **properties},
        extensions={PROVENANCE_KEY: {"r1_ohm_per_km": {
            "source": "Паспорт А, стр. 4", "confirmation": "confirmed", "origin": "catalog"}}})


def _dialog(controller, eid, entry=None):
    if entry:
        controller.save_parameter_catalog_entry(entry)
    return ProjectParameterCatalogDialog(controller, (eid,))


def _diff_item(dialog, key):
    return next(dialog.diff.topLevelItem(i) for i in range(dialog.diff.topLevelItemCount())
        if dialog.diff.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) is not None
        and dialog.diff.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole).key == key)


def _select_only(dialog, key):
    for index in range(dialog.diff.topLevelItemCount()):
        item = dialog.diff.topLevelItem(index)
        ref = item.data(0, Qt.ItemDataRole.UserRole)
        if ref:
            item.setCheckState(0, Qt.CheckState.Checked if ref.key == key else Qt.CheckState.Unchecked)


def test_save_brand_is_one_separate_command_and_does_not_apply_to_circuit(app, tmp_path):
    project, controller, eid = _project()
    dialog = _dialog(controller, eid)
    fingerprint = electrical_model_fingerprint(controller.model)
    journal = len(controller.journal)
    entry = _entry()
    dialog.save_entry(entry)
    assert len(controller.journal) == journal + 1
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert not project.catalog_snapshots.bindings
    path = tmp_path / "catalog-only.json"
    save_project(path, project)
    assert load_project(path).user_catalog.get(entry.id) == entry
    controller.undo()
    assert not project.user_catalog.entries
    assert electrical_model_fingerprint(controller.model) == fingerprint


def test_catalog_diff_and_cancel_are_readonly_and_apply_only_checked_parameter(app):
    project, controller, eid = _project()
    dialog = _dialog(controller, eid, _entry(parallel_count=8))
    before = ProjectMemento.capture(project)
    dialog.preview_changes()
    assert dialog.preview.valid, dialog.preview.incompatible
    assert ProjectMemento.capture(project) == before
    assert not any(change.key in {"length_mm", "parallel_count"} for change in dialog.preview.changes)
    _select_only(dialog, "r1_ohm_per_km")
    journal = len(controller.journal)
    old_ports = controller.model.equipment[eid].port_ids
    dialog.apply_selected()
    assert len(controller.journal) == journal + 1
    line = project.network.branches["L1"]
    assert line.r0 == .25 and line.x0 == .08
    assert line.length_km == 2 and line.n_parallel == 1
    assert controller.model.equipment[eid].port_ids == old_ports
    state = controller.equipment_parameter_snapshot(eid).fields["r1_ohm_per_km"]
    assert state.confirmation == DataConfirmation.CONFIRMED
    assert state.source == "Паспорт А, стр. 4"
    controller.undo()
    assert ProjectMemento.capture(project) == before


@pytest.mark.parametrize("origin", ["manual", "instance"])
def test_manual_override_without_source_is_preserved_by_default(app, origin):
    project, controller, eid = _project()
    preview = controller.preview_parameter_patch((ParameterPatch(eid,
        {"r1_ohm_per_km": ParameterValue(.33, origin=origin)}),))
    assert preview.valid
    controller.apply_parameter_preview(preview)
    dialog = _dialog(controller, eid, _entry())
    dialog.preview_changes()
    item = _diff_item(dialog, "r1_ohm_per_km")
    assert item.checkState(0) == Qt.CheckState.Unchecked
    assert not item.flags() & Qt.ItemFlag.ItemIsEnabled
    dialog.preserve_manual.setChecked(False)
    assert dialog.preview is None
    dialog.preview_changes()
    _select_only(dialog, "r1_ohm_per_km")
    dialog.apply_selected()
    assert project.network.branches["L1"].r0 == .25


def test_catalog_review_rejects_stale_selection_after_external_change(app):
    project, controller, eid = _project()
    dialog = _dialog(controller, eid, _entry())
    dialog.preview_changes()
    _select_only(dialog, "r1_ohm_per_km")
    controller.rename_equipment(eid, "Изменено вне окна")
    before = ProjectMemento.capture(project)
    dialog.apply_selected()
    assert ProjectMemento.capture(project) == before
    assert "изменился" in dialog.error_label.text()


def test_wrong_line_kind_is_explicitly_rejected_without_writes(app):
    project, controller, eid = _project()
    entry = replace(_entry(), equipment_type_id=EquipmentTypeId("builtin.line_section.overhead"))
    dialog = _dialog(controller, eid, entry)
    before = ProjectMemento.capture(project)
    dialog.preview_changes()
    assert dialog.preview.incompatible
    assert not dialog.apply_button.isEnabled()
    assert ProjectMemento.capture(project) == before


def test_template_typed_edit_preserves_extra_fields_and_is_local_until_saved(app):
    project, controller, eid = _project()
    entry = _entry(custom_parameters={"passport_page": 17}, r2_ohm_per_km=.26,
                   x2_ohm_per_km=.13, r0_ohm_per_km=.8, x0_ohm_per_km=.4)
    before = ProjectMemento.capture(project)
    editor = CatalogEntryEditorDialog(controller, entry.equipment_type_id, entry=entry)
    editor.editors["r1_ohm_per_km"].value_edit.setText("0,29")
    candidate = editor.candidate()
    assert candidate.properties["r1_ohm_per_km"] == .29
    assert candidate.properties["custom_parameters"]["passport_page"] == 17
    assert candidate.properties["r0_ohm_per_km"] == .8
    assert candidate.extensions[PROVENANCE_KEY]["r1_ohm_per_km"]["confirmation"] == "unconfirmed"
    assert ProjectMemento.capture(project) == before
    editor.reject()
    assert ProjectMemento.capture(project) == before


def test_new_legacy_template_retains_line_kind_but_not_instance_length(app):
    project, controller, eid = _project()
    equipment = controller.model.equipment[eid]
    editor = CatalogEntryEditorDialog(controller, equipment.type_id,
        equipment.type_version, equipment_id=eid)
    editor.name_edit.setText("Марка из исходной линии")
    editor.source_edit.setText("Учебная проверка")
    entry = editor.candidate()
    payload = entry.properties["legacy_payload"]
    assert payload["line_type"] == "cable"
    assert payload["r0"] == .1 and payload["x0"] == .08
    assert "length_km" not in payload and "n_parallel" not in payload


@pytest.mark.parametrize("type_id", ["builtin.circuit_breaker", "compat.rza_calc.line"])
def test_template_half_ct_pair_is_rejected_before_catalog_save(app, type_id):
    project, controller, eid = _project()
    before = ProjectMemento.capture(project)
    editor = CatalogEntryEditorDialog(controller, EquipmentTypeId(type_id))
    editor.name_edit.setText("Марка с ТТ")
    editor.source_edit.setText("Паспорт")
    editor.editors["ct_primary_a"].value_edit.setText("100")
    with pytest.raises(ValueError, match="оба|пара|ТТ"):
        editor.candidate()
    assert ProjectMemento.capture(project) == before


@pytest.mark.parametrize("type_id", ["builtin.circuit_breaker", "compat.rza_calc.line"])
def test_template_clearing_both_ct_values_removes_ratio_without_losing_unknown_data(app, type_id):
    project, controller, eid = _project()
    native = type_id == "builtin.circuit_breaker"
    entry = CatalogEntry(CatalogEntryId("catalog.ct.clear"), CatalogOrigin.USER,
        CatalogCategoryId("ct"), "Марка", EquipmentTypeId(type_id), 1, source="Паспорт",
        properties={} if native else {"legacy_payload": {"line_type": "cable", "ct_ratio": [100, 5],
            "vendor_data": {"passport_page": 4}}},
        extensions={"rza_calc.ct_parameters": {"ct_ratio": [100, 5]}, "vendor_data": {"revision": 7}} if native else {})
    editor = CatalogEntryEditorDialog(controller, entry.equipment_type_id, entry=entry)
    for key in ("ct_primary_a", "ct_secondary_a"):
        editor.editors[key].value_edit.setText("")
    candidate = editor.candidate()
    container = candidate.extensions.get("rza_calc.ct_parameters", {}) if native else candidate.properties["legacy_payload"]
    assert container.get("ct_ratio") is None
    if native:
        assert candidate.extensions["vendor_data"]["revision"] == 7
    else:
        assert container["vendor_data"]["passport_page"] == 4


def test_edit_existing_legacy_brand_keeps_visible_line_kind(app):
    project, controller, eid = _project()
    entry = CatalogEntry(CatalogEntryId("catalog.old.overhead"), CatalogOrigin.USER,
        CatalogCategoryId("lines"), "АС", EquipmentTypeId("compat.rza_calc.line"), 1,
        source="Паспорт", properties={"legacy_payload": {"line_type": "overhead", "r0": .3, "x0": .4}})
    editor = CatalogEntryEditorDialog(controller, entry.equipment_type_id, entry=entry)
    assert editor.editors["line_type"].value().value == "overhead"
    editor.name_edit.setText("АС, проверенная запись")
    assert editor.candidate().properties["legacy_payload"]["line_type"] == "overhead"


@pytest.mark.parametrize("family,native_type,key,new_value", [
    ("source", "builtin.external_grid", "s_kz_max", 750.0),
    ("generator", "builtin.generator", "s_nom", 9500.0),
    ("transformer", "builtin.transformer_2w", "uk", 12.0),
    ("cable", "builtin.line_section.cable", "r1_ohm_per_km", .35),
    ("overhead", "builtin.line_section.overhead", "r1_ohm_per_km", .45),
])
def test_native_brand_to_legacy_family_updates_real_input_and_roundtrips(app, tmp_path, family, native_type, key, new_value):
    from rza_calc.adapters.legacy_calculation import import_legacy_network
    from rza_calc.core.methodology import Methodology
    from rza_calc.core.model import GRID, GeneratorBranch, LineBranch, Network, Node, SourceBranch, TransformerBranch
    from rza_calc.domain import ProjectStructure
    from rza_calc.io.project import FORMAT_VERSION, ProjectData
    net = Network("Перенос марки")
    for name, voltage in (("a", 110), ("b", 10), ("c", 10)):
        net.add_node(Node(name, name, voltage))
    branch = {
        "source": lambda: SourceBranch("target", "Система", GRID, "b", s_kz_max=500, s_kz_min=250),
        "generator": lambda: GeneratorBranch("target", "Генератор", GRID, "b", s_nom=7500, u_nom=10),
        "transformer": lambda: TransformerBranch("target", "Трансформатор", "a", "b", s_nom=10000, u_hv=110, u_lv=10, uk=10),
        "cable": lambda: LineBranch("target", "КЛ", "b", "c", line_type="cable", length_km=2, r0=.1, x0=.08),
        "overhead": lambda: LineBranch("target", "ВЛ", "b", "c", line_type="overhead", length_km=2, r0=.1, x0=.4),
    }[family]()
    net.add_branch(branch)
    model = import_legacy_network(net)
    project = ProjectData(net, Methodology.load(), {}, ProjectStructure(), FORMAT_VERSION, model)
    controller = ProjectEditorController(project)
    eid = next(iter(model.equipment))
    ports, connectivity = dict(model.ports), model.connectivity_signature()
    entry = CatalogEntry(CatalogEntryId("catalog." + family), CatalogOrigin.USER, CatalogCategoryId("test"),
        "Проверочная марка", EquipmentTypeId(native_type), 1, source="Независимая ведомость", properties={key: new_value})
    controller.save_parameter_catalog_entry(entry)
    dialog = ProjectParameterCatalogDialog(controller, (eid,))
    dialog.scope_combo.setCurrentIndex(1)
    dialog.preview_changes()
    assert dialog.preview.valid, dialog.preview.incompatible
    _select_only(dialog, key)
    dialog.apply_selected()
    actual = project.network.branches["target"]
    assert getattr(actual, "r0" if key == "r1_ohm_per_km" else key) == new_value, dialog.error_label.text()
    binding = project.catalog_snapshots.bindings[eid]
    assert binding.entry.equipment_type_id == EquipmentTypeId(native_type)
    assert binding.effective_properties == controller.model.effective_equipment_properties(eid)
    assert dict(controller.model.ports) == ports and controller.model.connectivity_signature() == connectivity
    path = tmp_path / (family + ".json")
    save_project(path, project)
    reopened = load_project(path)
    assert reopened.catalog_snapshots.bindings[eid] == binding
    assert getattr(reopened.network.branches["target"], "r0" if key == "r1_ohm_per_km" else key) == new_value
