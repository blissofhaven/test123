"""User mode drafts, shared views, stored inputs and cancellable Qt work."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from pathlib import Path
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.electrical import DataConfirmation, EquipmentAvailability, SwitchPosition
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.operating_parameters import ModeValue
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.mode_dialog import OperatingModeDialog
from rza_calc.io.project import load_project, save_project
from rza_calc.calculation.input import load_calculation_input


@pytest.fixture(scope='module', autouse=True)
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture
def vm():
    return ProjectViewModel.open(Path(__file__).parent / 'fixtures/legacy_projects/four_fault_types.json')


@pytest.fixture
def window(vm):
    widget = MainWindow(vm)
    widget.show()
    QApplication.processEvents()
    yield widget
    if widget._calculation_worker is not None:
        widget._cancel_calculation()
        _until(lambda: widget._calculation_worker is None)
    widget.close()
    QApplication.processEvents()


def _until(condition, timeout=4000):
    deadline = time.monotonic() + timeout / 1000
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert condition(), 'Qt operation did not finish within its bounded wait'


def test_top_selector_and_editor_share_saved_mode_without_calculation(window, monkeypatch):
    vm = window.vm
    before, result = electrical_model_fingerprint(vm.project.electrical_model), vm.result
    monkeypatch.setattr(vm, 'recalculate', lambda: pytest.fail('Selecting a saved mode must not calculate'))
    index = next(i for i in range(window.mode_toolbar.selector.count())
                 if window.mode_toolbar.selector.itemData(i) != vm.selected_operating_mode().state_id)
    window.mode_toolbar.selector.setCurrentIndex(index)
    assert vm.selected_operating_mode().state_id == window.editor_controller.active_operating_state_id
    assert window.editor_workspace.scene._operating_state_id == window.editor_controller.active_operating_state_id
    assert vm.result is result and vm.current_result is result
    assert electrical_model_fingerprint(vm.project.electrical_model) == before


def test_mode_copy_apply_one_undo_and_save_roundtrip(vm, tmp_path):
    before = dict(vm.project.electrical_model.operating_states)
    history = len(vm.mode_controller.journal)
    dialog = OperatingModeDialog(vm)
    dialog.copy_mode()
    dialog.name_edit.setText('Проверенная копия')
    dialog._metadata_changed()
    assert vm.current_result is None
    assert dict(vm.project.electrical_model.operating_states) == before
    assert dialog.apply_changes(), dialog.message.toPlainText()
    assert len(vm.mode_controller.journal) == history + 1
    assert len(vm.project.electrical_model.operating_states) == len(before) + 1
    assert all(vm.project.electrical_model.operating_states[key] == value for key, value in before.items())
    output = tmp_path / 'mode-project.json'
    save_project(output, vm.project)
    loaded = load_project(output)
    assert dict(loaded.electrical_model.operating_states) == dict(vm.project.electrical_model.operating_states)
    vm.mode_controller.undo()
    assert dict(vm.project.electrical_model.operating_states) == before
    vm.mode_controller.redo()
    assert any(row.name == 'Проверенная копия' for row in vm.project.electrical_model.operating_states.values())
    dialog.close()


def test_project_save_does_not_apply_pending_mode_draft_and_reset_restores_result(vm, tmp_path):
    original, result = dict(vm.project.electrical_model.operating_states), vm.current_result
    vm.begin_mode_draft()
    vm.update_mode_draft(replace(vm.mode_draft, name='Не сохранённое имя'))
    assert vm.current_result is None and 'Черновик' in vm.result_unavailable_reason()
    output = tmp_path / 'ordinary-save.json'
    save_project(output, vm.project)
    assert dict(load_project(output).electrical_model.operating_states) == original
    assert vm.mode_draft is not None
    vm.reset_mode_draft()
    assert vm.current_result is result


def test_mode_source_value_and_external_current_keep_source_confirmation_and_units(vm, tmp_path):
    dialog = OperatingModeDialog(vm)
    dialog.edit_mode()
    sources = [row for row in dialog._parameter_rows if row[0] == 'sources']
    row = next(row for row in sources if row[2] == 'r2_ohm')
    dialog.set_parameter('sources', row[1], 'r2_ohm', ModeValue(0, 'Паспорт источника', DataConfirmation.CONFIRMED))
    current = next(row for row in dialog._parameter_rows if row[0] == 'working_currents')
    dialog.set_parameter('working_currents', current[1], None, ModeValue(125, 'Расчёт рабочего режима, версия 4', DataConfirmation.CONFIRMED))
    assert dialog.apply_changes(), dialog.message.toPlainText()
    output = tmp_path / 'parameters.json'
    save_project(output, vm.project)
    from rza_calc.domain.operating_parameters import operating_parameters
    loaded = load_project(output)
    p = operating_parameters(loaded.electrical_model.operating_states[vm.selected_operating_mode().state_id])
    assert p.sources[row[1]]['r2_ohm'].value == 0
    assert p.sources[row[1]]['r2_ohm'].source == 'Паспорт источника'
    assert p.working_currents[current[1]].value == 125
    assert p.working_currents[current[1]].confirmation is DataConfirmation.CONFIRMED
    dialog.close()


def test_invalid_and_stale_mode_drafts_do_not_write(vm):
    vm.begin_mode_draft()
    vm.update_mode_draft(replace(vm.mode_draft, name=''))
    before = electrical_model_fingerprint(vm.project.electrical_model)
    with pytest.raises(ValueError):
        vm.apply_mode_draft()
    assert electrical_model_fingerprint(vm.project.electrical_model) == before
    vm.reset_mode_draft()
    vm.begin_mode_draft()
    equipment = next(iter(vm.mode_controller.model.equipment.values()))
    vm.mode_controller.rename_equipment(equipment.id, equipment.name + ' актуальное')
    with pytest.raises((ValueError, RuntimeError)):
        vm.apply_mode_draft()
    assert vm.mode_draft is not None


def test_real_editor_switch_signal_changes_only_draft_then_apply(window):
    vm, controller = window.vm, window.editor_controller
    created = controller.add_equipment('builtin.circuit_breaker', 'QF режима', x=700, y=600)
    QApplication.processEvents()
    window.editor_workspace.canvas.refresh()
    before = electrical_model_fingerprint(controller.model)
    history = len(controller.journal)
    window.editor_workspace.scene.equipmentContextActionRequested.emit('switch', {
        'equipment_id': created.equipment_id, 'position': SwitchPosition.OPEN})
    assert vm.mode_draft.positions[created.equipment_id] is SwitchPosition.OPEN
    assert electrical_model_fingerprint(controller.model) == before and len(controller.journal) == history
    preview = window.editor_workspace.scene._model
    assert preview is not controller.model
    assert window.editor_workspace.scene._items_by_id[created.representation_id]._switch_open
    window.workspace_tabs.setCurrentIndex(1)
    assert window.diagram_panel.view.scene._model is preview
    window._apply_mode_draft()
    assert controller.model.operating_states[controller.active_operating_state_id].positions[created.equipment_id] is SwitchPosition.OPEN
    assert len(controller.journal) == history + 1
    controller.undo()
    QApplication.processEvents()
    assert electrical_model_fingerprint(controller.model) == before


def test_saved_calculation_input_is_the_completed_input_after_project_edit(vm, tmp_path):
    completed = vm.result.calculation_input
    original_stamp = completed.stamp
    path = tmp_path / 'calculated-input.json'
    vm.save_calculation_snapshot(path)
    equipment = next(iter(vm.mode_controller.model.equipment.values()))
    vm.mode_controller.rename_equipment(equipment.id, equipment.name + ' новое')
    assert vm.current_result is None
    with pytest.raises(ValueError, match='устарели'):
        vm.save_calculation_snapshot(tmp_path / 'stale-input.json')
    assert not (tmp_path / 'stale-input.json').exists()
    restored = load_calculation_input(path)
    assert restored.stamp == original_stamp
    assert not restored.is_current_for(vm.project)


def test_background_calculation_allows_viewing_and_cancel_never_publishes(window, monkeypatch):
    import rza_calc.core.engine as engine
    from rza_calc.calculation.input import CalculationCancelled
    def wait_for_cancel(captured, *, cancelled, progress):
        progress(0, 4, 'Проверка отмены')
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.005)
        raise CalculationCancelled()
    monkeypatch.setattr(engine, 'run_input', wait_for_cancel)
    window._recalculate()
    assert window.vm.calculation_busy
    assert window.editor_controller.mode.value == 'analysis'
    assert window.editor_workspace.canvas.view.isEnabled()
    assert not window.editor_workspace.inspector.isEnabled()
    assert window.vm.current_result is None
    worker = window._calculation_worker
    window._recalculate()
    assert window._calculation_worker is worker
    window._cancel_calculation()
    _until(lambda: window._calculation_worker is None)
    assert window.vm.result is None and 'отменён' in window.vm.calculation_error
    assert window.editor_controller.mode.value == 'edit'
    assert window.editor_workspace.inspector.isEnabled()


def test_background_success_uses_capture_and_rejects_late_generation(window):
    window._recalculate()
    generation, captured = window._calculation_run
    _until(lambda: window._calculation_worker is None)
    result = window.vm.current_result
    assert result is not None
    assert result.calculation_input.stamp == captured.stamp
    window.vm._calculation_generation += 1
    assert not window.vm.publish_background_calculation(generation, captured, None, 'Поздний старый ответ')
    assert window.vm.result is result and not window.vm.calculation_error


def test_canonical_freshness_is_checked_once_per_bounded_render(vm, monkeypatch):
    from rza_calc.calculation.input import CalculationInput
    original = CalculationInput.is_current_for
    calls = []
    monkeypatch.setattr(CalculationInput, 'is_current_for',
        lambda captured, project: (calls.append(captured), original(captured, project))[1])
    with vm.presentation_snapshot():
        before = len(calls)
        for _ in range(6):
            assert vm.current_result is vm.result
            vm.fault_rows()
        assert len(calls) == before == 1
    equipment = next(iter(vm.mode_controller.model.equipment.values()))
    vm.mode_controller.rename_equipment(equipment.id, equipment.name + ' новая')
    with vm.presentation_snapshot():
        assert vm.current_result is None
    assert len(calls) == 2


@pytest.mark.parametrize('field', ['algorithm_version', 'kernel_version'])
def test_obsolete_calculation_version_hides_results_even_when_input_matches(vm, field):
    assert vm.result.calculation_input.is_current_for(vm.project)
    vm.result.calculation_case = replace(vm.result.calculation_case, **{field: 'old-version'})
    assert vm.current_result is None
    with vm.presentation_snapshot():
        assert vm.current_result is None
        assert all(row.iabc_ka is None for row in vm.fault_rows())


def test_failed_background_attempt_and_changed_input_never_publish(window, monkeypatch):
    import rza_calc.core.engine as engine
    original = engine.run_input
    def fail(*args, **kwargs):
        raise ValueError('Контрольная ошибка worker')
    monkeypatch.setattr(engine, 'run_input', fail)
    window._recalculate()
    _until(lambda: window._calculation_worker is None)
    assert window.vm.result is None and 'Контрольная ошибка' in window.vm.calculation_error
    monkeypatch.setattr(engine, 'run_input', original)
    window._recalculate()
    window.vm.project.methodology.data['mtz']['k_ots']['value'] += .1
    _until(lambda: window._calculation_worker is None)
    assert window.vm.result is None and 'изменились' in window.vm.calculation_error


def test_partial_mode_with_parallel_error_displays_draft_position_and_cannot_apply(app):
    vm = ProjectViewModel.open(Path(__file__).parent / 'fixtures/legacy_projects/gtes_sever.json')
    window = MainWindow(vm)
    window.show()
    try:
        original = electrical_model_fingerprint(vm.project.electrical_model)
        window._toggle_switch('SW:VF1:from')
        preview = vm.preview_mode_draft()
        assert not preview.valid and preview.can_display
        assert window.editor_workspace.scene._model is not vm.project.electrical_model
        assert window.editor_workspace.scene._operating_state_id == preview.state_id
        assert vm.mode_draft.extra_positions['SW:VF1:from'] is False
        assert 'параллель' in window.mode_toolbar.status.text().lower()
        assert vm.current_result is None
        with pytest.raises(ValueError):
            vm.apply_mode_draft()
        assert electrical_model_fingerprint(vm.project.electrical_model) == original
        window._reset_mode_draft()
        assert vm.current_result is vm.result
    finally:
        window.close()


def test_toolbar_initialization_selection_and_electrical_refresh_do_not_build_full_mode_snapshots(vm, monkeypatch):
    monkeypatch.setattr(vm.mode_controller, 'operating_mode_snapshots',
        lambda: pytest.fail('The toolbar must not compile target/topology snapshots'))
    window = MainWindow(vm)
    try:
        other = next(row for row in vm.operating_mode_choices() if row.calculation_mode_id != vm.mode_id)
        vm.select_mode(other.calculation_mode_id)
        equipment = next(iter(vm.mode_controller.model.equipment.values()))
        vm.mode_controller.rename_equipment(equipment.id, equipment.name + ' изменено')
        QApplication.processEvents()
        assert window.mode_toolbar.selector.currentData() == other.state_id
        assert vm.current_result is None
        assert 'устар' in window.bottom.report_text.toPlainText().lower()
    finally:
        window.close()


@pytest.mark.parametrize('canvas_signal', [False, True])
def test_history_refresh_synchronizes_each_canvas_once_and_keeps_diagnostics(window, monkeypatch, canvas_signal):
    from rza_calc.editor.parameter_editing import ParameterPatch

    canvas = window.editor_workspace.canvas
    refresh = canvas.refresh
    calls = []
    monkeypatch.setattr(canvas, 'refresh', lambda: (calls.append(None), refresh())[1])
    window._refresh_mode_views()
    assert calls == [], 'An unchanged model, document and mode must not rebuild the canvas'
    equipment = next(iter(window.editor_controller.model.equipment.values()))
    window.editor_controller.rename_equipment(equipment.id, equipment.name + ' изменено')
    if canvas_signal:
        canvas.commandCompleted.emit(None)
    QApplication.processEvents()
    assert len(calls) == 1
    assert canvas.scene._model is window.editor_controller.model
    assert canvas.scene._document is window.editor_controller.diagram
    representation = next(row for row in window.editor_controller.diagram.representations.values()
                          if row.equipment_id == equipment.id and row.page_id == canvas.page_id)
    first_rendered = canvas.scene._items_by_id[representation.id]._target_state
    # A saved custom diagram label may intentionally differ from the name.
    assert first_rendered[0].name == equipment.name + ' изменено'
    first_revision = canvas.scene._model_revision
    assert first_revision == window.editor_controller.model.revision
    assert window.vm.current_result is None
    assert 'устар' in window.bottom.report_text.toPlainText().lower()
    window._refresh_mode_views()
    assert len(calls) == 1

    # A second in-place command must also synchronize after the initial mode
    # mismatch is gone. Change a real electrical input through the card API.
    snapshot = window.editor_controller.equipment_parameter_snapshot(equipment.id)
    value = snapshot.fields['r2_ohm']
    preview = window.editor_controller.preview_parameter_patch((ParameterPatch(
        equipment.id, {'r2_ohm': replace(value, value=(value.value or 0) + 0.25)},
    ),))
    assert preview.valid, preview.diagnostics
    window.editor_controller.apply_parameter_preview(preview)
    if canvas_signal:
        canvas.commandCompleted.emit(None)
    QApplication.processEvents()
    assert len(calls) == 2
    assert canvas.scene._model is window.editor_controller.model
    assert canvas.scene._model_revision == window.editor_controller.model.revision != first_revision
    rendered = canvas.scene._items_by_id[representation.id]._target_state
    assert rendered != first_rendered
    assert rendered[0] == window.editor_controller.model.equipment[equipment.id]
    assert window.vm.current_result is None
    window._refresh_mode_views()
    assert len(calls) == 2


@pytest.mark.parametrize('copy_mode', [False, True])
def test_saved_selected_canonical_mode_reopens_by_exact_calculation_id(vm, tmp_path, monkeypatch, copy_mode):
    other = next(row for row in vm.operating_mode_choices() if row.calculation_mode_id != vm.mode_id)
    vm.choose_operating_mode(other.state_id)
    if copy_mode:
        vm.begin_mode_draft(clone=True)
        vm.update_mode_draft(replace(vm.mode_draft, name='Сохранённый выбранный режим'))
        vm.apply_mode_draft()
    selected = vm.selected_mode_choice()
    output = tmp_path / 'selected-mode.json'
    save_project(output, vm.project)
    from rza_calc.editor.controller import ProjectEditorController
    monkeypatch.setattr(ProjectEditorController, 'operating_mode_snapshots',
        lambda _: pytest.fail('Restoring mode selection must remain lightweight'))
    restored = ProjectViewModel.open(output)
    window = MainWindow(restored)
    try:
        assert restored.mode_id == selected.calculation_mode_id
        assert restored.mode_controller.active_operating_state_id == selected.state_id
        assert window.mode_toolbar.selector.currentData() == selected.state_id
    finally:
        window.close()


def test_history_report_and_status_share_one_canonical_freshness_read(window, monkeypatch):
    from rza_calc.calculation.input import CalculationInput
    original = CalculationInput.is_current_for
    calls = []
    monkeypatch.setattr(CalculationInput, 'is_current_for',
        lambda captured, project: (calls.append(None), original(captured, project))[1])
    equipment = next(iter(window.editor_controller.model.equipment.values()))
    window.editor_controller.rename_equipment(equipment.id, equipment.name + ' новое')
    QApplication.processEvents()
    assert len(calls) == 1
    assert 'устар' in window.statusBar().currentMessage().lower()
    assert 'устар' in window.bottom.report_text.toPlainText().lower()


@pytest.mark.parametrize('saved_state', [None, 'state.deleted.previous-selection'])
def test_initial_mode_fallback_is_applied_before_first_editor_render(vm, monkeypatch, saved_state):
    from rza_calc.domain.electrical import VoltageClassId, thaw_json
    from rza_calc.editor.state import WORKSPACE_EXTENSION_KEY
    from rza_calc.gui.editor_scene import DiagramGraphicsScene

    controller = vm.mode_controller
    added = controller.add_equipment(
        'builtin.circuit_breaker', 'QF первого режима', x=700, y=600,
        normal_position=SwitchPosition.CLOSED,
        voltage_class_by_group={'main': VoltageClassId('builtin.voltage.ac.10kv')},
    )
    chosen = next(row for row in controller.operating_mode_choices() if row.calculation_mode_id == 'max')
    controller.model.set_switch_position(chosen.state_id, added.equipment_id, SwitchPosition.OPEN)
    extensions = thaw_json(vm.project.diagram.extensions)
    extensions.setdefault(WORKSPACE_EXTENSION_KEY, {})['active_operating_state_id'] = saved_state
    vm.project.diagram = replace(vm.project.diagram, extensions=extensions)
    before = electrical_model_fingerprint(vm.project.electrical_model)
    reopened = ProjectViewModel(vm.project, vm.path)
    assert reopened.mode_controller.active_operating_state_id is None
    journal = reopened.mode_controller.journal
    syncs = []
    original = DiagramGraphicsScene.sync_document

    def sync(scene, *args, **kwargs):
        syncs.append((scene, kwargs.get('operating_state_id')))
        return original(scene, *args, **kwargs)

    monkeypatch.setattr(DiagramGraphicsScene, 'sync_document', sync)
    window = MainWindow(reopened)
    try:
        window.show()
        QApplication.processEvents()
        editor = window.editor_workspace.scene
        analysis = window.diagram_panel.view.scene
        assert reopened.mode_id == chosen.calculation_mode_id
        assert window.mode_toolbar.selector.currentData() == chosen.state_id
        assert window.editor_controller.active_operating_state_id == chosen.state_id
        for scene in (editor, analysis):
            assert scene._operating_state_id == chosen.state_id
            assert scene._items_by_id[added.representation_id]._effective_position is SwitchPosition.OPEN
        assert [state for scene, state in syncs if scene is editor] == [chosen.state_id]
        assert any('Режим:' in issue.message for issue in editor._diagnostics)
        assert all('Основная модель' not in issue.message for issue in editor._diagnostics)
        assert reopened.project.electrical_model.equipment[added.equipment_id].normal_position is SwitchPosition.CLOSED
        assert electrical_model_fingerprint(reopened.project.electrical_model) == before
        assert reopened.mode_controller.journal == journal
    finally:
        window.close()
