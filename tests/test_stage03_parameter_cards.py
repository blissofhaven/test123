"""Cards edit local drafts; explicit details and inter-sheet navigation differ."""
from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.editor_panels import EditorWorkspaceWidget
from test_ui_direct_connections import _controller, U10
from test_drawing_rules_navigation_cursor import linked_canvas


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_explicit_details_is_separate_from_single_selection_and_post_insert_focus(app, monkeypatch):
    controller = _controller()
    result = controller.add_equipment("builtin.load", "Нагрузка", x=0, y=0,
                                      voltage_class_by_group={"main": U10})
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    opened = []
    monkeypatch.setattr(workspace, "open_equipment_card", opened.append)
    workspace.resize(1400, 800)
    workspace.show()
    workspace.view.actual_size()
    workspace.view.centerOn(0, 0)
    QApplication.processEvents()
    point = workspace.view.mapFromScene(QPointF(0, 0))
    before = electrical_model_fingerprint(controller.model), len(controller.journal)
    try:
        QTest.mouseClick(workspace.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        workspace.canvas.propertiesRequested.emit(result.representation_id)
        assert not opened
        QTest.mouseDClick(workspace.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert opened == [result.equipment_id]
        assert (electrical_model_fingerprint(controller.model), len(controller.journal)) == before
    finally:
        workspace.close()


def test_field_editor_parses_from_schema_not_missing_value_type_and_resets_confirmation(app):
    from rza_calc.domain.electrical import DataConfirmation
    from rza_calc.editor.parameter_editing import ParameterValue
    from rza_calc.editor.parameter_schema import FieldSpec
    from rza_calc.gui.equipment_parameters import ParameterFieldEditor
    spec = FieldSpec("r", "R", "number", "ohm")
    editor = ParameterFieldEditor(spec, ParameterValue(None))
    editor.value_edit.setText("0")
    assert editor.value().value == 0
    editor.source_edit.setText("Паспорт, стр. 2")
    editor.confirmation_check.setChecked(True)
    assert editor.value().confirmation is DataConfirmation.CONFIRMED
    editor.value_edit.setText("0,25")
    assert editor.value().value == .25
    assert editor.value().confirmation is DataConfirmation.UNCONFIRMED
    editor.value_edit.setText("")
    assert editor.value().value is None


def _native_line(composite=False):
    from rza_calc.domain.electrical import DataConfirmation, LineConstructionSegmentId, LineKind
    from rza_calc.editor.controller import PhysicalLineInput, PortTarget, ProjectEditorController
    controller = _controller()
    first = controller.add_equipment("builtin.circuit_breaker", "QF1", x=0, y=0,
                                    voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "QF2", x=400, y=0,
                                     voltage_class_by_group={"main": U10})
    result = controller.create_physical_line("КЛ", LineKind.CABLE,
        PortTarget(first.port_ids[-1], first.representation_id),
        PortTarget(second.port_ids[0], second.representation_id),
        physical=PhysicalLineInput(100_000, DataConfirmation.UNCONFIRMED,
            {"r1_ohm_per_km": .3, "x1_ohm_per_km": .1}))
    equipment_id = next(iter(controller.model.line_sections))
    if composite:
        section = controller.model.line_sections[equipment_id]
        original = section.construction_segments[0]
        controller.model.replace_line_construction_segments(equipment_id, (
            replace(original, length_mm=40_000),
            replace(original, id=LineConstructionSegmentId.new(), length_mm=60_000,
                    properties={"r1_ohm_per_km": .9, "x1_ohm_per_km": .2})))
        controller = ProjectEditorController(controller._project)
    return controller, equipment_id, result


def test_card_cancel_and_invalid_number_never_change_project_or_history(app):
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line()
    before = electrical_model_fingerprint(controller.model), len(controller.journal), controller.diagram
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    editor = dialog.editors["r1_ohm_per_km"]
    editor.value_edit.setText("не число")
    assert not dialog.apply_changes()
    assert dialog.error_label.text()
    dialog.reject()
    assert (electrical_model_fingerprint(controller.model), len(controller.journal), controller.diagram) == before


def test_card_applies_one_draft_command_preserving_ports_topology_and_geometry(app):
    from rza_calc.domain.electrical import DataConfirmation
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line()
    before = controller.model.connectivity_signature(), controller.diagram
    ports = controller.model.equipment[equipment_id].port_ids
    fingerprint, count = electrical_model_fingerprint(controller.model), len(controller.journal)
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    for key, value in (("r1_ohm_per_km", "0,45"), ("x1_ohm_per_km", "0,12")):
        dialog.editors[key].value_edit.setText(value)
    preview = dialog.preview_changes()
    assert preview.valid
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert dialog.apply_changes()
    assert len(controller.journal) == count + 1
    assert controller.model.effective_equipment_properties(equipment_id)["r1_ohm_per_km"] == .45
    assert controller.model.line_sections[equipment_id].construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED
    assert controller.model.equipment[equipment_id].port_ids == ports
    assert (controller.model.connectivity_signature(), controller.diagram) == before
    controller.undo()
    assert electrical_model_fingerprint(controller.model) == fingerprint
    controller.redo()
    assert controller.model.effective_equipment_properties(equipment_id)["x1_ohm_per_km"] == .12
    dialog.close()


def test_card_rejects_external_edit_instead_of_overwriting_it(app):
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line()
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    dialog.editors["r1_ohm_per_km"].value_edit.setText("0,7")
    controller.rename_equipment(equipment_id, "Внешняя правка")
    before = electrical_model_fingerprint(controller.model), len(controller.journal)
    assert not dialog.apply_changes()
    assert "изменил" in dialog.error_label.text().lower()
    assert (electrical_model_fingerprint(controller.model), len(controller.journal)) == before
    dialog.close()


def test_card_analysis_mode_is_read_only(app):
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line()
    controller.set_mode("analysis")
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    assert not dialog.apply_button.isEnabled()
    assert not dialog.editors["r1_ohm_per_km"].isEnabled()
    count = len(controller.journal)
    assert not dialog.apply_changes()
    assert len(controller.journal) == count
    dialog.close()


def test_construction_segment_selector_retains_drafts_and_edits_only_target_segment(app):
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line(composite=True)
    original = controller.model.line_sections[equipment_id]
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    length_spec = next(spec for spec in dialog.snapshot.specs if spec.scope == "line_section")
    assert not dialog.editors[length_spec.key].isEnabled()
    dialog.segment_combo.setCurrentIndex(1)
    dialog.editors["r1_ohm_per_km"].value_edit.setText("0,6")
    dialog.segment_combo.setCurrentIndex(2)
    assert dialog.editors["r1_ohm_per_km"].value().value == .9
    dialog.segment_combo.setCurrentIndex(1)
    assert dialog.editors["r1_ohm_per_km"].value().value == .6
    count = len(controller.journal)
    assert dialog.apply_changes()
    updated = controller.model.line_sections[equipment_id]
    assert updated.construction_segments[0].properties["r1_ohm_per_km"] == .6
    assert updated.construction_segments[1] == original.construction_segments[1]
    assert updated.length_mm == original.length_mm
    assert len(controller.journal) == count + 1
    dialog.close()


def test_display_unit_change_keeps_canonical_value_confirmation_and_clean_draft(app):
    from rza_calc.domain.electrical import DataConfirmation
    from rza_calc.editor.parameter_editing import ParameterValue
    from rza_calc.editor.parameter_schema import FieldSpec
    from rza_calc.gui.equipment_parameters import ParameterFieldEditor
    original = ParameterValue(1250, "Паспорт", DataConfirmation.CONFIRMED)
    editor = ParameterFieldEditor(FieldSpec("length", "Длина", "number", "m", display_units=("m", "km")), original)
    editor.unit_combo.setCurrentIndex(editor.unit_combo.findData("km"))
    assert float(editor.value_edit.text().replace(",", ".")) == 1.25
    assert editor.value() == original
    assert not editor.is_changed()
    editor.value_edit.setText("1,5")
    assert editor.value().value == 1500
    assert editor.value().confirmation is DataConfirmation.UNCONFIRMED


def test_quick_fields_keep_unapplied_selection_draft_and_apply_once(app):
    from rza_calc.gui.equipment_parameters import ParameterQuickEditor
    controller, equipment_id, _ = _native_line()
    other = next(eid for eid in controller.model.equipment if eid != equipment_id)
    quick = ParameterQuickEditor(controller)
    quick.set_equipment(equipment_id)
    key = next(key for key, editor in quick.editors.items() if editor.spec.value_kind == "number")
    quick.editors[key].value_edit.setText("125")
    count = len(controller.journal)
    quick.set_equipment(other)
    quick.set_equipment(equipment_id)
    assert quick.editors[key].value_edit.text() == "125"
    assert len(controller.journal) == count
    assert quick.apply_changes()
    assert len(controller.journal) == count + 1
    assert not any(editor.is_changed() for editor in quick.editors.values())
    quick.close()


def test_quick_draft_transfers_to_card_without_applying_it(app):
    from rza_calc.gui.equipment_parameters import ParameterQuickEditor, EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line()
    quick = ParameterQuickEditor(controller)
    quick.set_equipment(equipment_id)
    key = next(iter(quick.editors))
    editor = quick.editors[key]
    value = "125" if editor.spec.value_kind == "number" else "Изменённое имя"
    editor.value_edit.setText(value)
    before = electrical_model_fingerprint(controller.model), len(controller.journal)
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    quick.transfer_draft_to(dialog)
    assert dialog.editors[key].draft_state() == editor.draft_state()
    dialog.reject()
    assert (electrical_model_fingerprint(controller.model), len(controller.journal)) == before
    assert editor.is_changed()
    quick.close()


def test_real_physical_route_double_click_opens_one_equipment_card_and_cancel_is_pure(app):
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    controller, equipment_id, _ = _native_line()
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1500, 900)
    workspace.show()
    workspace.view.actual_size()
    workspace.view.centerOn(200, 0)
    QApplication.processEvents()
    route = next(row for row in controller.diagram.routes.values() if row.equipment_id == equipment_id)
    a, b = max(zip(route.waypoints, route.waypoints[1:]), key=lambda pair:
               abs(pair[0].x-pair[1].x) + abs(pair[0].y-pair[1].y))
    point = QPointF((a.x+b.x)/2, (a.y+b.y)/2)
    before = electrical_model_fingerprint(controller.model), len(controller.journal)
    opened = []
    timer = QTimer()
    timer.setSingleShot(True)
    def dismiss():
        dialog = QApplication.activeModalWidget()
        opened.append((isinstance(dialog, EquipmentParameterCardDialog), getattr(dialog, "equipment_id", None)))
        if isinstance(dialog, QDialog):
            dialog.reject()
    timer.timeout.connect(dismiss)
    try:
        timer.start(20)
        QTest.mouseDClick(workspace.view.viewport(), Qt.MouseButton.LeftButton,
                         pos=workspace.view.mapFromScene(point))
        assert opened == [(True, equipment_id)]
        assert (electrical_model_fingerprint(controller.model), len(controller.journal)) == before
    finally:
        timer.stop()
        workspace.close()


def test_linked_double_click_keeps_navigation_priority_over_equipment_card(linked_canvas):
    canvas, source, target = linked_canvas
    details = []
    canvas.equipmentDetailsRequested.connect(details.append)
    QTest.mouseDClick(canvas.view.viewport(), Qt.MouseButton.LeftButton,
                     pos=canvas.view.mapFromScene(QPointF(source.x, source.y)))
    assert canvas.page_id == target.page_id
    assert not details
    assert canvas.navigate_back()
    assert canvas.page_id == source.page_id


def test_confirmation_requires_source_and_is_not_added_by_plain_apply(app):
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog
    from rza_calc.domain.electrical import DataConfirmation
    controller, equipment_id, _ = _native_line()
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    editor = dialog.editors["r1_ohm_per_km"]
    editor.value_edit.setText("0,4")
    editor.confirmation_check.setChecked(True)
    count = len(controller.journal)
    assert not dialog.apply_changes()
    assert "источник" in dialog.error_label.text().lower()
    assert len(controller.journal) == count
    editor.source_edit.setText("Паспорт, таблица 3")
    assert not editor.confirmation_check.isChecked()
    editor.confirmation_check.setChecked(True)
    assert dialog.apply_changes()
    stored = controller.equipment_parameter_snapshot(equipment_id).fields["r1_ohm_per_km"]
    assert stored.confirmation is DataConfirmation.CONFIRMED
    assert stored.source == "Паспорт, таблица 3"
    assert not dialog.apply_changes()
    assert len(controller.journal) == count + 1
    dialog.close()


def test_quick_stale_draft_is_rejected_until_explicit_reset(app):
    from rza_calc.gui.equipment_parameters import ParameterQuickEditor
    controller, equipment_id, _ = _native_line()
    quick = ParameterQuickEditor(controller)
    quick.set_equipment(equipment_id)
    quick.editors["name"].value_edit.setText("Старый черновик")
    controller.rename_equipment(equipment_id, "Текущее имя")
    count = len(controller.journal)
    assert not quick.apply_changes()
    assert "изменил" in quick.message.text().lower()
    assert len(controller.journal) == count
    quick.reset_changes()
    assert quick.editors["name"].value().value == "Текущее имя"
    assert not quick.editors["name"].is_changed()
    quick.close()


@pytest.mark.parametrize("quick", (False, True))
def test_erasing_inherited_value_saves_unknown_instead_of_reviving_parent(app, quick):
    from rza_calc.editor.controller import ProjectEditorController
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog, ParameterQuickEditor
    controller, equipment_id, _ = _native_line()
    line_id = controller.model.line_sections[equipment_id].logical_line_id
    controller.model.set_line_inherited_property(line_id, "conductor_mark", "Марка родительской линии")
    controller = ProjectEditorController(controller._project)
    editor = ParameterQuickEditor(controller) if quick else EquipmentParameterCardDialog(controller, equipment_id)
    if quick:
        editor.set_equipment(equipment_id)
    assert editor.editors["conductor_mark"].value().value == "Марка родительской линии"
    editor.editors["conductor_mark"].value_edit.clear()
    assert editor.apply_changes()
    assert controller.model.effective_equipment_properties(equipment_id)["conductor_mark"] is None
    assert controller.model.logical_lines[line_id].inherited_properties["conductor_mark"] == "Марка родительской линии"
    controller.undo()
    assert controller.model.effective_equipment_properties(equipment_id)["conductor_mark"] == "Марка родительской линии"
    editor.close()


def test_readiness_contains_all_fault_lists_and_distinguishes_maximum_minimum(app):
    from rza_calc.editor.parameter_schema import ReadinessIssue
    from rza_calc.gui.equipment_parameters import EquipmentParameterCardDialog, format_parameter_readiness
    issues = (ReadinessIssue("s_kz_max", "missing", "Не задана мощность", ("3ph",)),
              ReadinessIssue("s_kz_min", "missing", "Не задана мощность", ("3ph",)),
              ReadinessIssue("r0_ohm", "missing", "Последний параметр Z0", ("1ph_g", "2ph_g")))
    text = format_parameter_readiness(issues)
    for label in ("КЗ (3)", "КЗ (2)", "КЗ (1)", "КЗ (2,1)", "Максимальный режим", "Минимальный режим"):
        assert label in text
    assert text.count("Последний параметр Z0") == 2
    controller, equipment_id, _ = _native_line()
    dialog = EquipmentParameterCardDialog(controller, equipment_id)
    dialog.readiness_label.setText(text)
    assert dialog.readiness_label.toPlainText() == text
    assert dialog.readiness_label.maximumHeight() <= 160
    dialog.close()
