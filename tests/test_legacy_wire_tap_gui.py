"""The displayed oilfield conductor supports a real, protected draft tap."""
from pathlib import Path

from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.legacy_line_split import REVIEW_MARKER, legacy_line_split_info
from rza_calc.gui.main_window import BottomPanel
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import load_project, save_project
from test_ui_direct_connections import canvas_factory, _state, _mouse
from test_stage02_connection_gestures import answer_real_menu, assert_one_menu


def test_real_oilfield_cp01_wire_tap_preserves_both_pages_and_blocks_protection(canvas_factory, tmp_path):
    path = Path(__file__).parents[1] / 'rza_calc/examples/oilfield_gtes.json'
    project = load_project(path)
    controller = ProjectEditorController(project)
    original = next(row for row in controller.model.equipment.values()
        if row.extensions.get('legacy_calculation', {}).get('legacy_id') == 'cp01_in1_line')
    original_views = [row for row in controller.diagram.routes.values() if row.equipment_id == original.id]
    assert len(original_views) == 2
    route = original_views[0]
    info = legacy_line_split_info(controller.model, original.id)
    assert info.protected
    hit = QPointF(*controller._route_midpoint(route))
    bus = controller.add_electrical_node('Новое присоединение', x=hit.x() + 180, y=hit.y() + 80,
        page_id=route.page_id, symbol_key='busbar', width=100, height=12,
        voltage_class_id=controller._line_connection_voltage(controller.model, original.id))
    canvas = canvas_factory(controller)
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    canvas.setFont(QFont('Segoe UI', 10))
    canvas.show_page(route.page_id)
    canvas.view.centerOn(hit + QPointF(50, 0))
    QApplication.processEvents()
    before = _state(canvas)
    journal = len(controller.journal)
    timers, positions = [], []
    calls = answer_real_menu(canvas, 'wire', timers)
    poll, watchdog = QTimer(canvas), QTimer(canvas)
    watchdog.setSingleShot(True)
    timers += [poll, watchdog]
    def answer_position():
        dialog = next((widget for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QInputDialog) and widget.isVisible() and widget.parent() is canvas), None)
        if dialog is None:
            return
        poll.stop()
        watchdog.stop()
        positions.append(dialog.labelText())
        dialog.grab().save(str(tmp_path / 'oilfield-tap-position.png'))
        dialog.setDoubleValue(info.length_mm / 4000)
        QTest.keyClick(dialog, Qt.Key.Key_Return)
    def timeout():
        poll.stop()
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QInputDialog) and widget.isVisible():
                widget.reject()
    poll.timeout.connect(answer_position)
    watchdog.timeout.connect(timeout)
    poll.start(1)
    watchdog.start(2500)
    try:
        canvas.activate_connection_tool()
        start = QPointF(hit.x() + 180, hit.y() + 80)
        _mouse(canvas, 'move', start)
        _mouse(canvas, 'press', start)
        assert canvas.scene.connection_active, f"point={canvas.view.mapFromScene(start)} viewport={canvas.view.viewport().rect()} mode={canvas.scene._mode} tool={canvas.view._tool_state.tool}"
        _mouse(canvas, 'move', (start + hit) / 2)
        _mouse(canvas, 'move', hit)
        assert canvas.scene._connection_tool.target is not None, (hit, canvas.scene._target_at(hit), canvas.view.mapFromScene(hit))
        assert canvas.scene._connection_tool.target.target_id == original.id.value
        assert canvas.scene._connection_tool.target.feedback.value == 'compatible', canvas.scene._connection_tool.target.message
        _mouse(canvas, 'release', hit)
        assert_one_menu(canvas, calls, before)
        assert len(positions) == 1 and 'зоны защиты' in positions[0]
        assert len(controller.journal) == journal + 1
        first = controller.model.equipment[original.id]
        assert first.port_ids == original.port_ids
        assert first.properties['legacy_payload']['ct_ratio'] == original.properties['legacy_payload']['ct_ratio']
        assert first.properties['legacy_payload']['prot'] == original.properties['legacy_payload']['prot']
        assert first.extensions[REVIEW_MARKER]['required']
        # Both old route IDs remain the from-half. Their far halves are shown
        # on the same two pages and retain each original QF target terminal.
        for old in original_views:
            first_view = controller.diagram.routes[old.id]
            assert first_view.start_anchor == old.start_anchor
            assert first_view.end_anchor.electrical_node_id == bus.node_id
            second = next(row for row in controller.diagram.routes.values()
                if row.page_id == old.page_id and row.equipment_id != original.id
                and row.start_anchor.electrical_node_id == bus.node_id
                and row.end_anchor.target_port_id == old.end_anchor.target_port_id)
            assert second.end_anchor.target_port_id == old.end_anchor.target_port_id
        assert not controller.diagram.validate_targets(controller.model)
        incident = [item for item in canvas.scene._route_items_by_id.values()
            if bus.node_id in (item.route.start_anchor.electrical_node_id, item.route.end_anchor.electrical_node_id)]
        assert any(hit in item._bridge_nodes for item in incident), 'The actual tap must be drawn as a filled T junction.'
        assert all(not item._display_path.elementAt(index).isCurveTo()
            for item in incident for index in range(item._display_path.elementCount())), 'A bound tap must not look like an unconnected crossing.'
        canvas.view.centerOn(hit + QPointF(50, 0))
        canvas.grab().save(str(tmp_path / 'oilfield-tap-result.png'))
        vm = ProjectViewModel(project, path)
        assert vm.current_result is None
        assert 'зон' in vm.result_unavailable_reason().lower()
        assert all(row.iabc_ka is None for row in vm.fault_rows())
        panel = BottomPanel(vm)
        try:
            panel.resize(1000, 330)
            panel.show()
            QApplication.processEvents()
            assert 'зон' in panel.fault_details.toPlainText().lower()
            panel.tabs.setCurrentIndex(3)
            panel.grab().save(str(tmp_path / 'oilfield-tap-blocker.png'))
        finally:
            panel.close()
        saved = tmp_path / 'oilfield-tap.json'
        save_project(saved, project)
        loaded = load_project(saved)
        assert loaded.diagram == controller.diagram
        assert electrical_model_fingerprint(loaded.electrical_model) == electrical_model_fingerprint(controller.model)
        assert loaded.electrical_model.equipment[original.id].extensions[REVIEW_MARKER]['required']
        controller.undo()
        assert _state(canvas) == before
        controller.redo()
        assert controller.model.equipment[original.id].extensions[REVIEW_MARKER]['required']
    finally:
        for timer in timers:
            timer.stop()
        canvas.scene.clearSelection()
