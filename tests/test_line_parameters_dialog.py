"""User catalog choices produce read-only proposals with explicit confirmation."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from rza_calc.domain.catalog import CatalogCategoryId, CatalogEntry, CatalogEntryId, CatalogOrigin, UserCatalog
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, EquipmentTypeId
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.gui.line_parameters import LineParametersDialog, ask_line_parameters
from rza_calc.io.catalog import user_catalog_to_dict


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _entry(kind="cable", name="Марка A", **properties):
    return CatalogEntry(
        CatalogEntryId.new(), CatalogOrigin.USER, CatalogCategoryId("lines"),
        name, EquipmentTypeId("builtin.line_section." + kind), 1,
        properties={"conductor_mark": name, "r1_ohm_per_km": .27,
                    "x1_ohm_per_km": .09, **properties},
        source="Пользовательская таблица, строка 7",
    )


def _project(*entries):
    return SimpleNamespace(electrical_model=ElectricalModel.with_builtins(), user_catalog=UserCatalog(entries))


def _dialog(project=None, kind="cable"):
    return LineParametersDialog(None, SimpleNamespace(name="КЛ-12", line_kind=kind), project or _project())


def _mode(dialog, mode):
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(mode))


def _fingerprint(project):
    return ElectricalModelMemento.capture(project.electrical_model), user_catalog_to_dict(project.user_catalog)


def test_blank_manual_and_explicit_draft_do_not_fabricate_parameters(app):
    dialog = _dialog()
    manual = dialog.parameters()
    assert manual.name == "КЛ-12" and manual.accepted
    assert manual.physical.length_mm is None
    assert manual.physical.length_confirmation is DataConfirmation.UNCONFIRMED
    assert manual.physical.impedance_confirmation is DataConfirmation.UNCONFIRMED
    assert "conductor_mark" not in manual.physical.properties
    assert "r1_ohm_per_km" not in manual.physical.properties
    dialog.fields["conductor_mark"].setText("Незавершённая запись")
    dialog.length_edit.setText("ошибка")
    dialog.remember_check.setChecked(True)
    _mode(dialog, "draft")
    draft = dialog.parameters()
    assert draft.physical.length_mm is None
    assert not draft.physical.properties
    assert draft.catalog_entry is None and not draft.remember_catalog_entry


@pytest.mark.parametrize("kind", ["cable", "overhead"])
def test_manual_remember_is_a_proposal_not_a_catalog_mutation(app, kind):
    project = _project()
    before = _fingerprint(project)
    dialog = _dialog(project, kind)
    dialog.fields["conductor_mark"].setText("Моя марка 3×120")
    dialog.fields["material"].setText("Al")
    dialog.fields["cross_section_mm2"].setText("120")
    dialog.fields["r1_ohm_per_km"].setText("0,253")
    dialog.fields["x1_ohm_per_km"].setText("0.08")
    dialog.fields["source"].setText("Паспорт П-17, таблица 3")
    dialog.length_edit.setText("123,456")
    dialog.parallel_spin.setValue(2)
    dialog.length_confirm.setChecked(True)
    dialog.impedance_confirm.setChecked(True)
    dialog.remember_check.setChecked(True)
    result = dialog.parameters()
    assert result.physical.length_mm == 123456
    assert result.physical.properties["parallel_count"] == 2
    assert result.physical.properties["r1_ohm_per_km"] == pytest.approx(.253)
    assert result.physical.length_confirmation is DataConfirmation.CONFIRMED
    assert result.physical.impedance_confirmation is DataConfirmation.CONFIRMED
    assert result.remember_catalog_entry
    entry = result.catalog_entry
    assert entry.origin is CatalogOrigin.USER
    assert entry.equipment_type_id.value == "builtin.line_section." + kind
    assert entry.source == "Паспорт П-17, таблица 3"
    assert not {"parallel_count", "length_mm", "length_km"} & entry.properties.keys()
    parameters = entry.instance_parameters()
    kwargs = parameters.as_create_kwargs()
    kwargs.pop("extensions")
    project.electrical_model.validate_equipment_parameters(parameters.type_id, **kwargs)
    assert _fingerprint(project) == before


def test_catalog_filters_native_kind_preserves_extra_data_and_requires_confirmation(app):
    cable = _entry(r2_ohm_per_km=.271, x2_ohm_per_km=.091,
                   r0_ohm_per_km=.9, x0_ohm_per_km=.3,
                   zero_sequence_connection="series", custom_parameters={"page": 17})
    overhead = _entry("overhead", "АС 70")
    old_version = replace(_entry(), equipment_type_version=2)
    project = _project(cable, overhead, old_version)
    before = _fingerprint(project)
    dialog = _dialog(project)
    assert dialog.entries == (cable,)
    _mode(dialog, "catalog")
    dialog.length_edit.setText("1000")
    result = dialog.parameters()
    assert result.catalog_entry is cable and not result.remember_catalog_entry
    assert result.physical.length_mm == 1_000_000
    assert result.physical.impedance_confirmation is DataConfirmation.UNCONFIRMED
    assert result.physical.length_confirmation is DataConfirmation.UNCONFIRMED
    assert result.catalog_entry.properties["r0_ohm_per_km"] == .9
    assert result.catalog_entry.properties["custom_parameters"]["page"] == 17
    assert dialog.fields["r1_ohm_per_km"].isReadOnly()
    assert not dialog.remember_check.isEnabled()
    assert _fingerprint(project) == before


def test_confirmation_is_reset_when_parameters_or_selected_brand_change(app):
    project = _project(_entry(), _entry(name="Марка B", r1_ohm_per_km=.4))
    dialog = _dialog(project)
    dialog.length_confirm.setChecked(True)
    dialog.length_edit.setText("50")
    assert not dialog.length_confirm.isChecked()
    _mode(dialog, "catalog")
    dialog.impedance_confirm.setChecked(True)
    dialog.catalog_combo.setCurrentIndex(1)
    assert not dialog.impedance_confirm.isChecked()
    _mode(dialog, "manual")
    dialog.impedance_confirm.setChecked(True)
    dialog.fields["r1_ohm_per_km"].setText("0.6")
    assert not dialog.impedance_confirm.isChecked()


def test_mode_roundtrip_restores_unsaved_manual_brand(app):
    dialog = _dialog(_project(_entry()))
    dialog.fields["conductor_mark"].setText("Мой незаконченный ввод")
    dialog.fields["source"].setText("Лист 21")
    dialog.parallel_spin.setValue(3)
    _mode(dialog, "catalog")
    _mode(dialog, "draft")
    _mode(dialog, "manual")
    assert dialog.fields["conductor_mark"].text() == "Мой незаконченный ввод"
    assert dialog.fields["source"].text() == "Лист 21"
    assert dialog.parallel_spin.value() == 3


@pytest.mark.parametrize("length", ["0", "-3", "NaN", "inf", "1.0001", "метр", "1e99999", "1.0000000000000000000000000000000001"])
def test_bad_lengths_do_not_close_dialog_or_mutate_project(app, length):
    project = _project()
    before = _fingerprint(project)
    dialog = _dialog(project)
    dialog.length_edit.setText(length)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.proposal is None
    assert dialog.error_label.text()
    assert _fingerprint(project) == before


@pytest.mark.parametrize("key,value", [
    ("r1_ohm_per_km", "-1"), ("r1_ohm_per_km", "nan"),
    ("x1_ohm_per_km", "inf"), ("cross_section_mm2", "0"),
    ("cross_section_mm2", "мм2"),
])
def test_bad_brand_numbers_are_rejected(app, key, value):
    dialog = _dialog()
    dialog.fields[key].setText(value)
    with pytest.raises(ValueError):
        dialog.parameters()


def test_zero_resistance_and_signed_reactance_are_not_missing(app):
    dialog = _dialog()
    dialog.fields["r1_ohm_per_km"].setText("0")
    dialog.fields["x1_ohm_per_km"].setText("-0,1")
    dialog.fields["source"].setText("Проверенный эквивалент")
    dialog.impedance_confirm.setChecked(True)
    result = dialog.parameters()
    assert result.physical.properties["r1_ohm_per_km"] == 0
    assert result.physical.properties["x1_ohm_per_km"] == -.1
    assert result.physical.impedance_confirmation is DataConfirmation.CONFIRMED


@pytest.mark.parametrize("count", [0, -1, True, 2_147_483_648])
def test_invalid_catalog_parallel_count_is_not_silently_replaced(app, count):
    dialog = _dialog(_project(_entry(parallel_count=count)))
    _mode(dialog, "catalog")
    with pytest.raises(ValueError, match="параллельных"):
        dialog.parameters()


@pytest.mark.parametrize("case", ["empty_catalog", "confirmed_length_missing", "confirmed_impedance_missing", "remember_without_brand", "remember_without_source"])
def test_missing_required_choice_is_reported_without_faking_data(app, case):
    dialog = _dialog()
    if case == "empty_catalog":
        _mode(dialog, "catalog")
    elif case == "confirmed_length_missing":
        dialog.length_confirm.setChecked(True)
    elif case == "confirmed_impedance_missing":
        dialog.impedance_confirm.setChecked(True)
    else:
        dialog.remember_check.setChecked(True)
        dialog.fields["source" if case == "remember_without_brand" else "conductor_mark"].setText("заполнено")
    with pytest.raises(ValueError):
        dialog.parameters()


def test_cancel_function_keeps_entire_project_and_catalog_unchanged(app):
    project = _project(_entry())
    before = _fingerprint(project)
    draft = SimpleNamespace(name="КЛ-13", line_kind="cable")
    def cancel():
        dialog = next(widget for widget in QApplication.topLevelWidgets()
                      if isinstance(widget, LineParametersDialog) and widget.isVisible())
        dialog.fields["conductor_mark"].setText("Не сохранять")
        dialog.remember_check.setChecked(True)
        dialog.reject()
    QTimer.singleShot(0, cancel)
    result = ask_line_parameters(None, draft, project)
    assert not result.accepted
    assert result.catalog_entry is None and not result.remember_catalog_entry
    assert _fingerprint(project) == before


@pytest.mark.parametrize("kind", ["cable", "overhead"])
def test_dialog_catalog_creation_undo_save_reload_and_reselect(app, tmp_path, kind):
    from rza_calc.domain.electrical import LineKind, VoltageClassId
    from rza_calc.domain.fingerprint import electrical_model_fingerprint
    from rza_calc.editor import NodeTarget, ProjectEditorController
    from rza_calc.io.project import load_project, save_project

    fixture = Path(__file__).resolve().parent / "fixtures/legacy_projects/four_fault_types.json"
    project = load_project(fixture)
    controller = ProjectEditorController(project)
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first = controller.add_electrical_node("Новый ввод", x=900, y=0, voltage_class_id=voltage)
    second = controller.add_electrical_node("Новый потребитель", x=1300, y=0, voltage_class_id=voltage)
    initial = _fingerprint(project)
    journal_count = len(controller.journal)
    dialog = _dialog(project, kind)
    dialog.fields["conductor_mark"].setText("Пользовательская марка 120")
    dialog.fields["source"].setText("Ведомость кабелей, строка 2")
    dialog.fields["r1_ohm_per_km"].setText("0.25")
    dialog.fields["x1_ohm_per_km"].setText("0.08")
    dialog.length_edit.setText("123.456")
    dialog.length_confirm.setChecked(True)
    dialog.impedance_confirm.setChecked(True)
    dialog.remember_check.setChecked(True)
    proposal = dialog.parameters()
    assert _fingerprint(project) == initial
    made = controller.create_physical_line(
        proposal.name, LineKind(kind), NodeTarget(first.node_id, first.representation_id),
        NodeTarget(second.node_id, second.representation_id), physical=proposal.physical,
        catalog_entry=proposal.catalog_entry, remember_catalog_entry=proposal.remember_catalog_entry,
    )
    assert len(controller.journal) == journal_count + 1
    assert project.user_catalog.get(proposal.catalog_entry.id) == proposal.catalog_entry
    committed = _fingerprint(project)
    binding = project.catalog_snapshots.bindings[made.section_id]
    controller.undo()
    # Revisions advance during restore, so compare structural fingerprint here.
    assert made.section_id not in controller.model.equipment
    assert proposal.catalog_entry.id not in project.user_catalog.entries
    controller.redo()
    assert _fingerprint(project) == committed
    path = tmp_path / (kind + "-with-user-catalog.json")
    save_project(path, project)
    restored = load_project(path)
    assert electrical_model_fingerprint(restored.electrical_model) == electrical_model_fingerprint(project.electrical_model)
    assert user_catalog_to_dict(restored.user_catalog) == user_catalog_to_dict(project.user_catalog)
    assert restored.catalog_snapshots.bindings[made.section_id] == binding
    assert restored.electrical_model.line_sections[made.section_id].length_mm == 123456
    reopened = _dialog(restored, kind)
    _mode(reopened, "catalog")
    reopened.length_edit.setText("20")
    chosen = reopened.parameters()
    assert chosen.catalog_entry == proposal.catalog_entry
    assert chosen.physical.length_mm == 20000  # Previous section length never became template data.
    assert chosen.physical.impedance_confirmation is DataConfirmation.UNCONFIRMED
    assert chosen.physical.length_confirmation is DataConfirmation.UNCONFIRMED
    assert not chosen.remember_catalog_entry
