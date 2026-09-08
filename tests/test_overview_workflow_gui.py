"""Independent real-window overview gestures on the saved compact example."""
from dataclasses import replace
from pathlib import Path
import hashlib
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.calculation.point_fault import PointFaultTarget
from rza_calc.core.fault_types import FaultSpec, FaultType
from rza_calc.domain.electrical import EquipmentAvailability, SwitchPosition
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.overview_panel import KEY_ROLE, STATUS_ROLE, VOLTAGE_ROLE, REPAIR_ROLE
from rza_calc.gui.view_model import ProjectViewModel


EXAMPLE = Path(__file__).resolve().parents[1] / 'rza_calc/examples/compact_training.json'


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


def _until(condition, timeout=10000):
    deadline = time.monotonic() + timeout / 1000
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert condition(), 'Bounded Qt wait timed out'


@pytest.fixture
def overview_window(monkeypatch, request):
    from rza_calc.gui import project_settings, view_model
    from rza_calc.core import engine
    from rza_calc.core.short_circuit import ShortCircuitSolver
    def forbidden(*args, **kwargs):
        raise AssertionError('Navigation must not calculate or access user settings')
    monkeypatch.setattr(project_settings, 'QSettings', forbidden)
    monkeypatch.setattr(view_model, 'run_input', forbidden)
    monkeypatch.setattr(view_model, 'run', forbidden)
    monkeypatch.setattr(engine, 'run_input', forbidden)
    if not getattr(request, 'param', False):
        monkeypatch.setattr(ShortCircuitSolver, '__init__', forbidden)
    before_sha = hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
    vm = ProjectViewModel.open(EXAMPLE, calculate=False)
    before_fp = electrical_model_fingerprint(vm.project.electrical_model)
    before_diagram = vm.project.diagram
    window = MainWindow(vm, project_settings=None, start_in_overview=True)
    window.resize(1680, 980)
    window.show()
    QApplication.processEvents()
    window.overview_before_fp = before_fp
    window.overview_before_diagram = before_diagram
    yield window
    if window._point_worker is not None:
        window._cancel_calculation()
        _until(lambda: window._point_worker is None)
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    assert hashlib.sha256(EXAMPLE.read_bytes()).hexdigest() == before_sha


def _card_click(window, key, *, double=False):
    overview = window.overview_workspace
    card = overview.canvas.cards[key]
    point = overview.canvas.mapFromScene(card.mapToScene(QPointF(100, 50)))
    assert overview.canvas.viewport().rect().contains(point)
    (QTest.mouseDClick if double else QTest.mouseClick)(overview.canvas.viewport(),
        Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()


def _tree_click(window, key, *, double=False):
    tree = window.overview_workspace.tree
    item = window.overview_workspace._tree_items[key]
    parent = item.parent()
    while parent is not None:
        parent.setExpanded(True)
        parent = parent.parent()
    tree.scrollToItem(item)
    QApplication.processEvents()
    rect = tree.visualItemRect(item)
    assert not rect.isEmpty()
    point = rect.center()
    point.setX(min(130, tree.columnWidth(0) - 5))
    (QTest.mouseDClick if double else QTest.mouseClick)(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()


def _geometry(document):
    return dict(document.pages), dict(document.representations), dict(document.routes)


def _view_state(view):
    center = view.mapToScene(view.viewport().rect().center())
    return view.transform().m11(), center.x(), center.y()


def test_desktop_starts_overview_without_changing_saved_electrical_or_diagram_data(overview_window):
    window = overview_window
    overview = window.overview_workspace
    assert window.workspace_tabs.currentWidget() is overview
    assert window.workspace_tabs.widget(0) is window.editor_workspace
    assert window.workspace_tabs.tabText(1) == 'Анализ и расчёты'
    assert len(overview.projection.facilities) == 4
    assert len(overview.canvas.cards) == 4
    assert window.vm.result is None and window.vm.current_result is None
    assert window.vm.current_point_fault_result is None
    assert not window.vm.calculation_error and not window.vm.mode_draft
    assert not window.editor_controller.journal
    assert electrical_model_fingerprint(window.editor_controller.model) == window.overview_before_fp
    assert _geometry(window.editor_controller.diagram) == _geometry(window.overview_before_diagram)
    assert overview.preview.target is None
    for facility in overview.projection.facilities:
        if facility.upstream_ids:
            links = [wire for wire in overview.canvas.wires if wire.target == facility.target.key]
            assert len(links) == 2  # one dual-input facility card, two real links


@pytest.mark.parametrize('surface', ['card', 'tree'])
def test_single_click_previews_only_and_miniature_cannot_control_equipment(overview_window, surface):
    window = overview_window
    overview, editor = window.overview_workspace, window.editor_workspace.canvas
    facility = overview.projection.facilities[-1]
    before = window.editor_controller.diagram
    page, viewport = editor.page_id, editor.view.viewport_state()
    (_card_click if surface == 'card' else _tree_click)(window, facility.target.key)
    assert window.workspace_tabs.currentWidget() is overview
    assert overview._selected_key == facility.target.key
    assert overview.preview.target.page_id == facility.preferred_page_id
    assert facility.name in overview.summary.text()
    assert facility.name in window.navigation_path.text()
    assert editor.page_id == page and editor.view.viewport_state() == viewport
    assert window.editor_controller.diagram == before
    assert window.vm.mode_draft is None
    preview = overview.preview
    item = next(iter(preview.scene._items_by_id.values()))
    point = preview.view.mapFromScene(item.sceneBoundingRect().center())
    QTest.mouseDClick(preview.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseClick(preview.view.viewport(), Qt.MouseButton.RightButton, pos=point)
    QTest.keyClick(preview.view, Qt.Key.Key_Delete)
    assert window.vm.mode_draft is None and not window.editor_controller.journal
    assert window.workspace_tabs.currentWidget() is overview
    assert electrical_model_fingerprint(window.editor_controller.model) == window.overview_before_fp


@pytest.mark.parametrize('surface', ['card', 'tree'])
def test_double_open_back_preserves_overview_selection_and_detail_viewport(overview_window, surface):
    window = overview_window
    overview, editor = window.overview_workspace, window.editor_workspace.canvas
    facility = overview.projection.facilities[-1]
    (_card_click if surface == 'card' else _tree_click)(window, facility.target.key)
    overview.canvas.scale(1.13, 1.13)
    overview.canvas.centerOn(overview.canvas.cards[facility.target.key])
    QApplication.processEvents()
    overview_view = _view_state(overview.canvas)
    (_card_click if surface == 'card' else _tree_click)(window, facility.target.key, double=True)
    assert window.workspace_tabs.currentWidget() is window.editor_workspace
    assert editor.page_id == facility.preferred_page_id
    editor.view.actual_size()
    editor.view.centerOn(100, 120)
    rid = next(row.id for row in window.editor_controller.diagram.representations.values()
               if row.page_id == editor.page_id)
    editor.scene.select_representations((rid,))
    QApplication.processEvents()
    detail_view = editor.view.viewport_state()
    QTest.mouseClick(window.overview_back_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert window.workspace_tabs.currentWidget() is overview
    assert overview._selected_key == facility.target.key
    restored = _view_state(overview.canvas)
    assert restored[0] == pytest.approx(overview_view[0], abs=1e-6)
    assert restored[1:] == pytest.approx(overview_view[1:], abs=2)
    (_card_click if surface == 'card' else _tree_click)(window, facility.target.key, double=True)
    assert editor.page_id == facility.preferred_page_id
    assert editor.scene.selected_representation_ids() == (rid,)
    assert editor.view.viewport_state().zoom == detail_view.zoom
    assert editor.view.viewport_state().center_x == pytest.approx(detail_view.center_x, abs=2)
    assert editor.view.viewport_state().center_y == pytest.approx(detail_view.center_y, abs=2)
    assert electrical_model_fingerprint(window.editor_controller.model) == window.overview_before_fp
    assert _geometry(window.editor_controller.diagram) == _geometry(window.overview_before_diagram)


def test_hover_and_repeated_same_page_selection_do_not_fingerprint_or_run_calculations(overview_window, monkeypatch):
    from rza_calc.domain import fingerprint
    window = overview_window
    overview = window.overview_workspace
    facility = overview.projection.facilities[0]
    _card_click(window, facility.target.key)
    original = fingerprint.electrical_model_fingerprint
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    for name, module in tuple(sys.modules.items()):
        if name.startswith('rza_calc') and module is not None:
            for attr, value in tuple(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, attr, counted)
    page = window.editor_workspace.canvas.page_id
    selected = overview._selected_key
    point = overview.canvas.mapFromScene(overview.canvas.cards[facility.target.key].mapToScene(QPointF(80, 30)))
    QTest.mouseMove(overview.canvas.viewport(), point)
    QApplication.processEvents()
    assert overview._selected_key == selected
    for _ in range(3):
        _card_click(window, facility.target.key)
        _tree_click(window, facility.target.key)
    assert calls == []
    assert window.editor_workspace.canvas.page_id == page


def test_explicit_page_choice_survives_double_open_and_invalid_target_does_not_open(overview_window):
    window = overview_window
    overview = window.overview_workspace
    facility = next(f for f in overview.projection.facilities if len(f.page_ids) > 1)
    _card_click(window, facility.target.key)
    chosen = next(page for page in facility.page_ids if page != facility.preferred_page_id)
    overview.pages.setCurrentIndex(overview.pages.findData(chosen))
    QApplication.processEvents()
    assert overview.preview.target.page_id == chosen
    overview.open_target('missing:exact-target')
    assert window.workspace_tabs.currentWidget() is overview
    assert overview._selected_key == facility.target.key
    _card_click(window,facility.target.key,double=True)
    assert window.workspace_tabs.currentWidget() is window.editor_workspace
    assert window.editor_workspace.canvas.page_id == chosen
    window.show_network_overview()
    QApplication.processEvents()
    assert overview.pages.currentData() == chosen
    assert overview.preview.target.page_id == chosen
    assert not window.editor_controller.journal


def test_explicit_route_on_visited_page_is_visible_but_whole_page_restores_offscreen_selection(overview_window):
    window=overview_window
    overview=window.overview_workspace
    editor=window.editor_workspace.canvas
    facility=overview.projection.facilities[-1]
    _card_click(window,facility.target.key,double=True)
    page=editor.page_id
    route_id=next(route.id for route in window.editor_controller.diagram.routes.values() if route.page_id==page)
    route=editor.scene._route_items_by_id[route_id]
    rid=next(rep.id for rep in window.editor_controller.diagram.representations.values() if rep.page_id==page)
    editor.scene.select_representations((rid,))
    editor.view.actual_size()
    editor.view.centerOn(route.sceneBoundingRect().center()+QPointF(3000,3000))
    QApplication.processEvents()
    saved=editor.view.viewport_state()
    visible=editor.view.mapToScene(editor.view.viewport().rect()).boundingRect()
    assert not visible.intersects(route.sceneBoundingRect())
    assert not visible.intersects(editor.scene._items_by_id[rid].sceneBoundingRect())
    window.show_network_overview()
    QApplication.processEvents()
    _card_click(window,facility.target.key,double=True)
    assert editor.scene.selected_representation_ids()==(rid,)
    assert editor.view.viewport_state().zoom==saved.zoom
    assert editor.view.viewport_state().center_x==pytest.approx(saved.center_x,abs=2)
    assert editor.view.viewport_state().center_y==pytest.approx(saved.center_y,abs=2)
    window.show_network_overview()
    QApplication.processEvents()
    # The real preview Open action emits a route-only PreviewTarget.
    overview.preview.set_page(page,route_ids=(route_id,))
    QTest.mouseClick(overview.preview.open_button,Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert window.workspace_tabs.currentWidget() is window.editor_workspace
    visible=editor.view.mapToScene(editor.view.viewport().rect()).boundingRect()
    assert visible.intersects(editor.scene._route_items_by_id[route_id].sceneBoundingRect())
    assert electrical_model_fingerprint(window.editor_controller.model)==window.overview_before_fp
    assert not window.editor_controller.journal


def test_ru_filter_preserves_page_and_shows_only_scoped_annotations(overview_window):
    window = overview_window
    overview = window.overview_workspace
    facility = overview.projection.facilities[-1]
    _card_click(window,facility.target.key)
    page = overview.preview.target.page_id
    assert overview.voltage_filter.itemText(0) == 'Весь объект'
    ru = next(row for row in facility.target.children if row.nominal_kv == .4)
    overview.voltage_filter.setCurrentIndex(overview.voltage_filter.findData(ru.key))
    QApplication.processEvents()
    assert overview.preview.target.page_id == page
    assert overview.preview.target.representation_ids
    rows = overview.preview.view._annotations
    sections = [row for row in rows if row.key.startswith('section:')]
    assert {row.text for row in sections} == {'СШ 1 · 0,4 кВ', 'СШ 2 · 0,4 кВ'}
    assert window.workspace_tabs.currentWidget() is overview
    assert not window.editor_controller.journal
    section = ru.children[1]
    outgoing = next(child for child in section.children if child.role == 'outgoing')
    for child in (section,outgoing):
        _tree_click(window,child.key)
        annotations = overview.preview.view._annotations
        assert [row.text for row in annotations if row.key.startswith('feeder:')] == ['Ф2']


def test_tree_icons_are_neutral_and_status_voltage_repair_are_separate_roles(overview_window):
    window = overview_window
    overview = window.overview_workspace
    assert overview.tree.iconSize().width() == overview.tree.iconSize().height() == 16
    for facility in overview.projection.facilities:
        item = overview._tree_items[facility.target.key]
        assert item.text(0) == facility.name
        assert not item.icon(0).isNull(), (facility.kind, facility.name)
        assert item.text(1) == ''
        assert item.data(1, STATUS_ROLE) == facility.status.code
        assert item.data(0, STATUS_ROLE) is None
        assert item.toolTip(1)
    equipment = next(target for target in overview.projection.targets.values()
        if target.kind == 'equipment' and target.equipment_ids
        and window.editor_controller.model.equipment_type(
            window.editor_controller.model.equipment[target.equipment_ids[0]].type_id,
            window.editor_controller.model.equipment[target.equipment_ids[0]].type_version
        ).behavior_key == 'legacy.tie'
        and window.editor_controller.model.equipment[target.equipment_ids[0]].properties['legacy_payload']['switchable'] is True)
    item = overview._tree_items[equipment.key]
    assert not item.icon(0).isNull()
    initial_icon = item.icon(0).pixmap(16, 16).toImage()
    eid = equipment.equipment_ids[0]
    window.vm.begin_mode_draft()
    window.vm.update_mode_draft(replace(window.vm.mode_draft,
        availability={**window.vm.mode_draft.availability, eid: EquipmentAvailability.OUT_OF_SERVICE}))
    window._refresh_mode_views()
    QApplication.processEvents()
    item = overview._tree_items[equipment.key]
    assert item.data(1, REPAIR_ROLE) is True
    assert item.data(1, STATUS_ROLE) == 'deenergized'
    assert item.icon(0).pixmap(16, 16).toImage() == initial_icon
    assert 'Черновик' in overview.mode_label.text()
    assert window.vm.current_result is None and not window.editor_controller.journal
    assert electrical_model_fingerprint(window.editor_controller.model) == window.overview_before_fp
    window._reset_mode_draft()


@pytest.mark.parametrize('overview_window', [True], indirect=True)
def test_current_point_rows_belong_to_selected_facility_and_disappear_after_mode_change(overview_window):
    window = overview_window
    overview = window.overview_workspace
    facility = overview.projection.facilities[0]
    _card_click(window, facility.target.key)
    node = next(node for node in window.editor_controller.model.electrical_nodes.values()
                if node.id in facility.target.node_ids and '10 кВ, 1 СШ' in node.name)
    rep = next(row for row in window.editor_controller.diagram.representations.values()
               if row.electrical_node_id == node.id and row.page_id == facility.preferred_page_id)
    spec = FaultSpec(FaultType.LINE_LINE_GROUND)
    window._start_point_fault(PointFaultTarget(node_id=node.id), (spec,),
        {'node_id': node.id, 'representation_id': rep.id, 'local_point': (0, 0)})
    _until(lambda: window._point_worker is None)
    result = window.vm.current_point_fault_result
    assert result is not None, window.vm.point_query_error
    assert result.outcomes[0].available
    window.show_network_overview()
    overview.select_target(facility.target.key)
    tab = next(tab for tab in overview.results.snapshot.tabs if tab.key == 'fault')
    assert len(tab.rows) == 1
    assert tab.rows[0][0] == result.node_name
    assert tab.rows[0][1] == 'Двухфазное КЗ на землю (B–C)'
    assert float(tab.rows[0][2]) == pytest.approx(max(abs(i) for i in result.outcomes[0].fault.iabc_ka), abs=.001)
    other = next(f for f in overview.projection.facilities if node.id not in f.target.node_ids)
    overview.select_target(other.target.key)
    assert not next(tab for tab in overview.results.snapshot.tabs if tab.key == 'fault').rows
    overview.select_target(facility.target.key)
    choices = window.mode_toolbar.selector
    next_index = (choices.currentIndex() + 1) % choices.count()
    choices.setCurrentIndex(next_index)
    QApplication.processEvents()
    assert window.vm.current_point_fault_result is None
    assert overview._point_result is None
    assert not next(tab for tab in overview.results.snapshot.tabs if tab.key == 'fault').rows
    assert 'Нет актуального' in overview.details['КЗ'].text()
    assert window.vm.result is None
