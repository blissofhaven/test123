"""Explicit point faults: real Qt menus, frozen worker and transient overlays."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from rza_calc.calculation.point_fault import PointFaultTarget
from rza_calc.core.fault_types import FaultType, FaultSpec
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.point_fault import PointFaultDialog, point_choices, outcome_row, point_details_html
from rza_calc.gui.view_model import ProjectViewModel

ALL = tuple(FaultSpec(kind) for kind in FaultType)


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(monkeypatch):
    from rza_calc.gui import project_settings, view_model
    monkeypatch.setattr(project_settings, 'QSettings', lambda *a, **k: pytest.fail('No user settings'))
    monkeypatch.setattr(view_model, 'run_input', lambda *a, **k: pytest.fail('No implicit full calculation'))
    vm = ProjectViewModel.open(Path(__file__).parent / 'fixtures/legacy_projects/four_fault_types.json', calculate=False)
    widget = MainWindow(vm, project_settings=None)
    widget.resize(1440, 960)
    controller = widget.editor_controller
    node = next(n for n in controller.model.electrical_nodes.values() if n.name.startswith('Точка КЗ'))
    rid = controller.place_existing_node(node.id, x=0, y=0, symbol_key='busbar')
    widget.point_test_node = node.id
    widget.point_test_rid = rid
    widget._refresh_mode_views()
    widget.show()
    QApplication.processEvents()
    yield widget
    if widget._point_worker is not None:
        widget._cancel_calculation()
        _until(lambda: widget._point_worker is None)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def _until(condition, timeout=4000):
    deadline = time.monotonic() + timeout / 1000
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert condition(), 'Bounded Qt wait timed out'


def _context(window):
    return {'node_id': window.point_test_node, 'representation_id': window.point_test_rid,
            'local_point': (0, 0)}


def _run(window, specs=ALL):
    window._start_point_fault(PointFaultTarget(node_id=window.point_test_node), specs, _context(window))
    _until(lambda: window._point_worker is None)
    assert window.vm.current_point_fault_result is not None, window.vm.point_query_error
    return window.vm.current_point_fault_result


def _actual_menu_dialog(window, screen, item, *, accept=True, side=None):
    window.workspace_tabs.setCurrentIndex(screen)
    owner = window.editor_workspace.canvas if screen == 0 else window.diagram_panel.view
    point = item.mapToScene(item.boundingRect().center())
    owner.view.centerOn(point)
    QApplication.processEvents()
    events = []
    timer, watchdog = QTimer(), QTimer()
    def answer():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, PointFaultDialog) and widget.isVisible():
                if any(row[0] == 'dialog' for row in events):
                    continue
                events.append(('dialog', widget.mode_label.text(), len(widget.specs)))
                if side is not None:
                    widget.point_selector.setCurrentIndex(side)
                if accept:
                    if not widget.calculate_button.isEnabled():
                        events.append(('disabled', widget.message.text()))
                        widget.reject()
                    else:
                        QTest.mouseClick(widget.calculate_button, Qt.MouseButton.LeftButton)
                else:
                    QTest.keyClick(widget, Qt.Key.Key_Escape)
                timer.stop()
                watchdog.stop()
                return
            if isinstance(widget, QMenu) and widget.isVisible():
                action = next((a for a in widget.actions() if a.text() == 'Рассчитать КЗ здесь…'), None)
                events.append(('menu', action is not None and action.isEnabled()))
                if action is not None and action.isEnabled():
                    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=widget.actionGeometry(action).center())
                else:
                    widget.close()
                return
    def timeout():
        events.append(('timeout',))
        timer.stop()
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, (QMenu, PointFaultDialog)):
                widget.close()
    timer.timeout.connect(answer)
    timer.start(15)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(timeout)
    watchdog.start(2500)
    owner.scene.contextMenuEvent(SimpleNamespace(scenePos=lambda: point, screenPos=lambda: QPoint(220, 220),
        widget=lambda: owner.view.viewport(), modifiers=lambda: Qt.KeyboardModifier.NoModifier, accept=lambda: None))
    timer.stop()
    watchdog.stop()
    assert not any(row[0] == 'timeout' for row in events), events
    assert not any(row[0] == 'disabled' for row in events), events
    return events


@pytest.mark.parametrize('screen', [0, 1], ids=['editor', 'analysis'])
def test_real_bus_context_four_faults_without_full_calculation(window, screen):
    before = electrical_model_fingerprint(window.editor_controller.model)
    diagram, history = window.editor_controller.diagram, len(window.editor_controller.journal)
    window.workspace_tabs.setCurrentIndex(screen)
    QApplication.processEvents()
    scene = window.editor_workspace.scene if screen == 0 else window.diagram_panel.view.scene
    events = _actual_menu_dialog(window, screen, scene._items_by_id[window.point_test_rid])
    assert ('menu', True) in events
    assert next(row for row in events if row[0] == 'dialog')[2] == 4
    _until(lambda: window._point_worker is None)
    result = window.vm.current_point_fault_result
    assert result is not None and len(result.outcomes) == 4
    solver = ShortCircuitSolver(window.vm.net, window.vm.mode, window.vm.project.methodology)
    for row in result.outcomes:
        assert row.available, row.message
        expected = solver.fault_at(result.node_id, row.spec)
        assert row.fault.iabc_ka == pytest.approx(expected.iabc_ka)
    assert window.vm.result is None
    assert electrical_model_fingerprint(window.editor_controller.model) == before
    actual = window.editor_controller.diagram
    assert (actual.pages, actual.representations, actual.routes) == (diagram.pages, diagram.representations, diagram.routes)
    assert len(window.editor_controller.journal) == history
    assert all(overlay.item is not None and len(overlay.item.rows) == 4 for overlay in window._point_overlays)
    assert 'полный расчёт' in window.bottom.fault_views.tabText(0)
    assert 'Точечный расчёт готов' in window.bottom.point_result_notice.text()
    assert 'полному расчёту' in window.inspector.fault_source_notice.text()


@pytest.mark.parametrize('screen', [0, 1])
def test_real_menu_cancel_does_not_start_worker_or_create_result(window, screen):
    window.workspace_tabs.setCurrentIndex(screen)
    QApplication.processEvents()
    scene = window.editor_workspace.scene if screen == 0 else window.diagram_panel.view.scene
    _actual_menu_dialog(window, screen, scene._items_by_id[window.point_test_rid], accept=False)
    assert window._point_worker is None and window.vm.current_point_fault_result is None
    assert not getattr(window.vm, 'point_query_busy', False)


def test_line_side_is_explicit_and_no_pixel_offset_is_a_target(window):
    model = window.editor_controller.model
    line = next(e for e in model.equipment.values() if str(e.type_id) == 'compat.rza_calc.line')
    context = {'equipment_id': line.id, 'physical_route': True, 'route_fraction': .271}
    dialog = PointFaultDialog(model, context, 'Режим MAX')
    assert dialog.target is None and not dialog.calculate_button.isEnabled()
    assert all(choice.target.node_id is None for choice in dialog.choices)
    assert {choice.target.port_id for choice in dialog.choices} == set(line.port_ids)
    assert any(choice.label.startswith('В начале') for choice in dialog.choices)
    assert any(choice.label.startswith('В конце') for choice in dialog.choices)
    dialog.point_selector.setCurrentIndex(1)
    assert dialog.calculate_button.isEnabled()
    for check in dialog.checks.values():
        check.setChecked(False)
    assert not dialog.calculate_button.isEnabled()
    dialog.checks[FaultType.LINE_LINE].setChecked(True)
    assert dialog.specs == (FaultSpec(FaultType.LINE_LINE),)
    dialog.close()


@pytest.mark.parametrize('screen', [0, 1])
def test_connected_port_menu_uses_that_exact_physical_side(window, screen):
    controller = window.editor_controller
    line = next(e for e in controller.model.equipment.values() if str(e.type_id) == 'compat.rza_calc.source')
    rid = controller.place_existing_equipment(line.id, x=300, y=200)
    window._refresh_mode_views()
    window.workspace_tabs.setCurrentIndex(screen)
    QApplication.processEvents()
    scene = window.editor_workspace.scene if screen == 0 else window.diagram_panel.view.scene
    port = next(pid for pid in line.port_ids if controller.model.node_for_port(pid) is not None)
    item = scene._items_by_id[rid]._port_items[port]
    events = _actual_menu_dialog(window, screen, item)
    assert ('menu', True) in events
    _until(lambda: window._point_worker is None)
    assert window.vm.current_point_fault_result.request.target == PointFaultTarget(port_id=port)


def test_unconnected_port_does_not_invent_node(window):
    created = window.editor_controller.add_equipment('builtin.circuit_breaker', 'Свободный QF', x=400, y=300)
    pid = created.port_ids[0]
    dialog = PointFaultDialog(window.editor_controller.model, {'port_id': pid}, 'MAX')
    assert not dialog.choices and not dialog.calculate_button.isEnabled()
    assert 'подключ' in dialog.message.text()
    dialog.close()


def test_overlay_geometry_moves_without_query_and_electrical_edit_hides_numbers(window, monkeypatch):
    result = _run(window)
    overlay = window._point_overlays[0]
    before = QPointF(overlay.item.pos())
    from rza_calc.calculation import point_fault
    monkeypatch.setattr(point_fault, 'run_point_faults', lambda *a, **k: pytest.fail('Geometry must not calculate'))
    window.editor_controller.move_representations((window.point_test_rid,), 100, 40)
    window.editor_workspace.canvas.refresh()
    QApplication.processEvents()
    assert window.vm.current_point_fault_result is result
    assert overlay.item.pos() == before + QPointF(100, 40)
    equipment = next(iter(window.editor_controller.model.equipment.values()))
    window.editor_controller.rename_equipment(equipment.id, equipment.name + ' новое')
    QApplication.processEvents()
    assert window.vm.current_point_fault_result is None
    assert all(view.item is None for view in window._point_overlays)


def test_mode_and_draft_hide_overlay_and_current_cache_does_not_recalculate(window, monkeypatch):
    result = _run(window)
    from rza_calc.calculation import point_fault
    monkeypatch.setattr(point_fault, 'run_point_faults', lambda *a, **k: pytest.fail('Same snapshot result must be reusable'))
    assert _run(window) is result
    previous = window.vm.selected_mode_choice().state_id
    other = next(row.state_id for row in window.vm.operating_mode_choices() if row.state_id != previous)
    window._choose_operating_mode(other)
    assert all(overlay.item is None for overlay in window._point_overlays)
    window._choose_operating_mode(previous)
    assert window.vm.current_point_fault_result is result
    window.vm.begin_mode_draft()
    window._refresh_mode_views()
    assert all(overlay.item is None for overlay in window._point_overlays)
    window._start_point_fault(result.request.target, ALL)
    assert window._point_worker is None and window.vm.mode_draft is not None


def test_cancel_worker_keeps_view_available_and_late_reply_cannot_publish(window, monkeypatch):
    from rza_calc.calculation import point_fault
    from rza_calc.calculation.input import CalculationCancelled
    calls = []
    def wait(request, *, prepared_mode, cancelled, progress):
        calls.append(request)
        while not cancelled():
            time.sleep(.003)
        raise CalculationCancelled()
    monkeypatch.setattr(point_fault, 'run_point_faults', wait)
    target = PointFaultTarget(node_id=window.point_test_node)
    window._start_point_fault(target, ALL, _context(window))
    first = window._point_worker
    generation, request = window._point_run
    window._start_point_fault(target, ALL, _context(window))
    window._recalculate()
    assert window._point_worker is first and window._calculation_worker is None
    assert window.editor_workspace.canvas.view.isEnabled()
    assert not window.editor_workspace.inspector.isEnabled()
    assert not window.mode_toolbar.selector.isEnabled()
    window._cancel_calculation()
    _until(lambda: window._point_worker is None)
    assert len(calls) <= 1 and window.vm.result is None and window.vm.current_point_fault_result is None
    assert 'отмен' in window.vm.point_query_error
    assert window.editor_workspace.inspector.isEnabled()
    assert not window.vm.publish_point_fault_query(generation, request, error='Поздний ответ')


def test_two_ground_phase_max_is_named_and_both_phases_remain_in_details(window):
    result = _run(window)
    row = next(row for row in result.outcomes if row.spec.kind is FaultType.LINE_LINE_GROUND)
    text, reason = outcome_row(row)
    assert 'max(|IB|, |IC|)' in text and not reason
    assert f'{max(abs(row.fault.iabc_ka[1]), abs(row.fault.iabc_ka[2])):.3f}' in text
    details = point_details_html(result)
    for value in row.fault.iabc_ka:
        assert f'{abs(value):.6f}' in details
    window._show_point_fault_details()
    assert window._point_details.isVisible()
    window.vm.begin_mode_draft()
    window._refresh_mode_views()
    assert 'Прежние числа скрыты' in window._point_details.text.toPlainText()


def test_fault_specific_error_is_visible_without_dropping_successful_rows(window, monkeypatch):
    original = ShortCircuitSolver.fault_at
    from rza_calc.core.short_circuit import ShortCircuitStatusError
    def one_missing(self, node, spec):
        if spec.kind is FaultType.LINE_GROUND:
            raise ShortCircuitStatusError('MISSING_SEQUENCE_DATA', 'Для земли нужны R0 и X0 линии')
        return original(self, node, spec)
    monkeypatch.setattr(ShortCircuitSolver, 'fault_at', one_missing)
    result = _run(window)
    assert sum(row.available for row in result.outcomes) == 3
    row = next(row for row in result.outcomes if not row.available)
    assert outcome_row(row)[0] == 'КЗ(1): нет результата'
    assert 'R0 и X0' in point_details_html(result)
    assert any('КЗ(1): нет результата' in text for text in window._point_overlays[0].item.rows)


def test_point_branch_measurements_without_full_result_and_no_silent_full_fallback(window):
    result = _run(window, (FaultSpec(FaultType.LINE_LINE),))
    bottom = window.bottom
    bottom.fault_views.setCurrentIndex(1)
    assert bottom.fault_measurement_source.currentData() == 'point'
    assert bottom.point_fault_type_combo.count() == 1
    assert bottom.point_fault_type_combo.currentData().kind is FaultType.LINE_LINE
    assert result.node_name in bottom.fault_branch_title.text()
    assert result.mode_name in bottom.fault_branch_title.text()
    assert bottom.fault_measurement_table.rowCount() >= 2 and window.vm.result is None
    equipment = next(iter(window.editor_controller.model.equipment.values()))
    window.editor_controller.rename_equipment(equipment.id, equipment.name + ' изменён')
    QApplication.processEvents()
    assert bottom.fault_measurement_source.currentData() == 'point'
    assert bottom.fault_measurement_table.rowCount() == 0
    assert 'Нет актуального' in bottom.fault_measurement_details.toPlainText()
    bottom.fault_views.setCurrentIndex(0)
    assert bottom.fault_type_combo.isEnabled()


def test_new_point_overrides_measurement_context_only_with_explicit_source_selector(window):
    from rza_calc.calculation.input import capture_project_input
    from rza_calc.core.engine import run_input
    vm = window.vm
    vm.result = run_input(capture_project_input(vm.project))
    vm.select('node', next(node.id for node in vm.net.nodes.values() if node.name.startswith('Шины источника')))
    vm.select_fault_type(FaultType.LINE_GROUND)
    old_full = vm.current_result
    result = _run(window, (FaultSpec(FaultType.THREE_PHASE),))
    bottom = window.bottom
    bottom.fault_views.setCurrentIndex(1)
    assert vm.current_result is old_full
    assert bottom.fault_measurement_source.currentData() == 'point'
    assert result.node_name in bottom.fault_branch_title.text()
    assert 'Трёхфазное' in bottom.fault_branch_title.text()
    bottom.fault_measurement_source.setCurrentIndex(bottom.fault_measurement_source.findData('full'))
    assert 'Однофазное' in bottom.fault_branch_title.text()
    assert result.node_name not in bottom.fault_branch_title.text()
    assert _run(window, (FaultSpec(FaultType.THREE_PHASE),)) is result
    assert bottom.fault_measurement_source.currentData() == 'point'
    assert result.node_name in bottom.fault_branch_title.text()


def test_close_waits_for_point_worker_cancel_without_partial_publish(window, monkeypatch):
    from rza_calc.calculation import point_fault
    from rza_calc.calculation.input import CalculationCancelled
    def wait(request, *, prepared_mode, cancelled, progress):
        while not cancelled():
            time.sleep(.003)
        raise CalculationCancelled()
    monkeypatch.setattr(point_fault, 'run_point_faults', wait)
    window._start_point_fault(PointFaultTarget(node_id=window.point_test_node), ALL, _context(window))
    assert window._point_worker is not None
    window.close()
    _until(lambda: window._point_worker is None)
    assert not window.isVisible() and window.vm.current_point_fault_result is None


def test_auto_open_setting_can_be_disabled_without_user_settings(window):
    assert not window.auto_open_project_action.isEnabled()
    calls = []
    window._project_settings = SimpleNamespace(set_auto_open_last_project=lambda enabled: calls.append(enabled) or True)
    window.auto_open_project_action.setEnabled(True)
    window.auto_open_project_action.setChecked(True)
    window.auto_open_project_action.setChecked(False)
    assert calls == [True, False]
    assert 'выбор проекта' in window.statusBar().currentMessage()
    window._project_settings.set_auto_open_last_project = lambda enabled: False
    window.auto_open_project_action.setChecked(True)
    assert not window.auto_open_project_action.isChecked()
    assert 'Не удалось' in window.statusBar().currentMessage()


@pytest.mark.parametrize('screen', [0, 1])
def test_physical_route_menu_chooses_end_without_inventing_internal_node(window, screen):
    from rza_calc.domain.electrical import LineKind, DataConfirmation
    from rza_calc.editor.controller import NodeTarget, NewNodeTarget, PhysicalLineInput
    controller = window.editor_controller
    created = controller.create_physical_line('Кабель к новой шине', LineKind.CABLE,
        NodeTarget(window.point_test_node, window.point_test_rid),
        NewNodeTarget('Дальний конец', x=250, y=150), physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
    window._refresh_mode_views()
    window.workspace_tabs.setCurrentIndex(screen)
    QApplication.processEvents()
    scene = window.editor_workspace.scene if screen == 0 else window.diagram_panel.view.scene
    route_item = scene._route_items_by_id[created.route_id]
    # Mid-route hit must offer real ends, not calculate a new node at 50%.
    midpoint = route_item._path.pointAtPercent(.5)
    context = scene.point_fault_context(route_item, midpoint)
    assert context['physical_route'] and context['node_id'] is None
    choices = point_choices(controller.model, context)
    target = next(c.target for c in choices if c.label.startswith('В конце'))
    expected = controller.model.port_by_role(created.section_id, 'to').id
    assert target.port_id == expected
    window._start_point_fault(target, ALL, context)
    _until(lambda: window._point_worker is None)
    result = window.vm.current_point_fault_result
    assert result is not None and result.request.target == target
    overlay = window._point_overlays[screen]
    QApplication.processEvents()
    assert overlay.item is not None and overlay.item.isVisible()
    refreshed_route = scene._route_items_by_id[created.route_id]
    assert overlay.item.pos() == refreshed_route._path.pointAtPercent(1) + QPointF(18, -18)
