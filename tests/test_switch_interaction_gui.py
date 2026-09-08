"""Real switching gestures share one mode draft in editor and analysis."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from rza_calc.adapters import import_legacy_network
from rza_calc.core.model import Node, TieBranch, TransformerBranch
from rza_calc.domain.electrical import EquipmentAvailability, SwitchPosition
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import ProjectData, load_project


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(monkeypatch):
    from rza_calc.gui import project_settings
    monkeypatch.setattr(project_settings, 'QSettings', lambda *a, **k: pytest.fail('No user settings in QA'))
    source = Path(__file__).parent / 'fixtures/legacy_projects/four_fault_types.json'
    base = load_project(source)
    net = base.network
    net.add_node(Node('TEST_END', 'Конец QF', 10))
    net.add_node(Node('TEST_LV', 'Низшая сторона', .4))
    net.add_branch(TieBranch('TEST_QF', 'Старый QF', 'FAULT', 'TEST_END', normally_closed=True))
    net.add_branch(TieBranch('TEST_WIRE', 'Обычная перемычка', 'FAULT', 'TEST_END', switchable=False))
    net.add_branch(TransformerBranch('TEST_T', 'Трансформатор', 'TEST_END', 'TEST_LV',
                                   s_nom=1000, u_hv=10, u_lv=.4, uk=6))
    project = ProjectData(net, base.methodology, {}, base.structure, 7, import_legacy_network(net))
    vm = ProjectViewModel(project, source)
    widget = MainWindow(vm, project_settings=None)
    widget.resize(1400, 900)
    widget.editor_controller.set_confirm_switching(False)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.close()
    QApplication.processEvents()


def _place(window, kind):
    controller = window.editor_controller
    if kind == 'native':
        created = controller.add_equipment('builtin.circuit_breaker', 'Новый QF', x=200, y=100)
        eid, rid = created.equipment_id, created.representation_id
    else:
        wanted = {'legacy': 'Старый QF', 'wire': 'Обычная перемычка', 'transformer': 'Трансформатор', 'line': 'LINE'}[kind]
        equipment = next(e for e in controller.model.equipment.values()
                         if e.name == wanted
                         or kind == 'line' and controller.model.equipment_type(e.type_id, e.type_version).behavior_key == 'legacy.line')
        eid = equipment.id
        rid = controller.place_existing_equipment(eid, x=200, y=100)
    window._refresh_mode_views()
    QApplication.processEvents()
    return eid, rid


def _surface(window, screen, rid):
    window.workspace_tabs.setCurrentIndex(screen)
    QApplication.processEvents()
    owner = window.editor_workspace.canvas if screen == 0 else window.diagram_panel.view
    item = owner.scene._items_by_id[rid]
    point = item.mapToScene(item.boundingRect().center())
    owner.view.resetTransform()
    owner.view.centerOn(point)
    QApplication.processEvents()
    return owner, owner.view.mapFromScene(point)


def _double(window, screen, rid):
    owner, point = _surface(window, screen, rid)
    QTest.mouseDClick(owner.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()


def _menu(window, screen, rid, text):
    owner, point = _surface(window, screen, rid)
    seen = []
    timer = QTimer()
    def choose():
        for menu in QApplication.topLevelWidgets():
            if isinstance(menu, QMenu) and menu.isVisible():
                seen.extend((a.text(), a.isEnabled()) for a in menu.actions())
                action = next((a for a in menu.actions() if text in a.text()), None)
                if action is not None and action.isEnabled():
                    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
                else:
                    menu.close()
                timer.stop()
    timer.timeout.connect(choose)
    timer.start(20)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: [w.close() for w in QApplication.topLevelWidgets() if isinstance(w, QMenu)])
    watchdog.start(2000)
    p = owner.view.mapToScene(point)
    owner.scene.contextMenuEvent(SimpleNamespace(scenePos=lambda: p, screenPos=lambda: QPoint(200, 200),
        widget=lambda: owner.view.viewport(), modifiers=lambda: Qt.KeyboardModifier.NoModifier, accept=lambda: None))
    timer.stop()
    watchdog.stop()
    assert seen, 'The real context menu did not appear'
    QApplication.processEvents()
    return seen


@pytest.mark.parametrize('screen', [0, 1], ids=['editor', 'analysis'])
@pytest.mark.parametrize('kind', ['native', 'legacy'])
def test_double_click_stages_only_and_apply_is_one_undo(window, screen, kind):
    eid, rid = _place(window, kind)
    controller, vm = window.editor_controller, window.vm
    before = electrical_model_fingerprint(controller.model)
    diagram, history = controller.diagram, len(controller.journal)
    owner, point = _surface(window, screen, rid)
    QTest.mouseClick(owner.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert vm.mode_draft is None
    assert owner.scene._items_by_id[rid].isSelected()
    _double(window, screen, rid)
    assert vm.mode_draft.positions[eid] is SwitchPosition.OPEN
    assert electrical_model_fingerprint(controller.model) == before
    assert controller.diagram == diagram and len(controller.journal) == history
    for scene in (window.editor_workspace.scene, window.diagram_panel.view.scene):
        assert scene._items_by_id[rid]._switch_open
    window._apply_mode_draft()
    assert vm.mode_draft is None
    assert controller.model.operating_states[controller.active_operating_state_id].positions[eid] is SwitchPosition.OPEN
    assert len(controller.journal) == history + 1
    controller.undo()
    QApplication.processEvents()
    assert electrical_model_fingerprint(controller.model) == before
    assert controller.diagram == diagram


@pytest.mark.parametrize('screen', [0, 1])
def test_confirmation_cancel_then_accept_and_reset(window, screen, monkeypatch):
    eid, rid = _place(window, 'legacy')
    window.editor_controller.set_confirm_switching(True)
    before = electrical_model_fingerprint(window.editor_controller.model)
    questions = []
    def answer(*args):
        questions.append(args[2])
        return QMessageBox.StandardButton.No if len(questions) == 1 else QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, 'question', answer)
    _double(window, screen, rid)
    assert len(questions) == 1 and window.vm.mode_draft is None
    _double(window, screen, rid)
    assert len(questions) == 2 and window.vm.mode_draft.positions[eid] is SwitchPosition.OPEN
    window._reset_mode_draft()
    assert window.vm.mode_draft is None
    assert electrical_model_fingerprint(window.editor_controller.model) == before
    assert not window.editor_workspace.scene._items_by_id[rid]._switch_open


@pytest.mark.parametrize('screen', [0, 1])
@pytest.mark.parametrize('guard', ['busy', 'readonly', 'unavailable'])
def test_blocked_gesture_does_not_change_draft_or_topology(window, screen, guard, monkeypatch):
    eid, rid = _place(window, 'legacy')
    if guard == 'unavailable':
        window.vm.begin_mode_draft()
        window.vm.update_mode_draft(replace(window.vm.mode_draft, availability={eid: EquipmentAvailability.OUT_OF_SERVICE}))
        window._refresh_mode_views()
    elif guard == 'busy':
        monkeypatch.setattr(window.vm, 'calculation_busy', True, raising=False)
        window._set_calculation_editing_enabled(False)
    owner, _ = _surface(window, screen, rid)
    if guard == 'readonly':
        owner.scene.switching_enabled = False
    draft = window.vm.mode_draft
    before = electrical_model_fingerprint(window.editor_controller.model)
    journal = len(window.editor_controller.journal)
    _double(window, screen, rid)
    assert window.vm.mode_draft == draft
    assert electrical_model_fingerprint(window.editor_controller.model) == before
    assert len(window.editor_controller.journal) == journal
    assert window.statusBar().currentMessage()
    assert ('Отключить', False) in _menu(window, screen, rid, 'Отключить')
    if guard == 'busy':
        window._stage_equipment_switch(eid, SwitchPosition.OPEN)
        assert window.vm.mode_draft == draft


@pytest.mark.parametrize('screen', [0, 1])
@pytest.mark.parametrize('kind', ['wire', 'line', 'transformer'])
def test_non_switch_equipment_keeps_double_click_card(window, screen, kind, monkeypatch):
    eid, rid = _place(window, kind)
    cards = []
    monkeypatch.setattr(window.editor_workspace, 'open_equipment_card', cards.append)
    before = electrical_model_fingerprint(window.editor_controller.model)
    _double(window, screen, rid)
    assert cards == [eid]
    assert window.vm.mode_draft is None
    assert electrical_model_fingerprint(window.editor_controller.model) == before


@pytest.mark.parametrize('screen', [0, 1])
def test_real_legacy_context_switch_and_properties_card(window, screen, monkeypatch):
    eid, rid = _place(window, 'legacy')
    cards = []
    monkeypatch.setattr(window.editor_workspace, 'open_equipment_card', cards.append)
    _menu(window, screen, rid, 'Отключить')
    assert window.vm.mode_draft.positions[eid] is SwitchPosition.OPEN
    _menu(window, screen, rid, 'Свойства')
    assert cards == [eid]
    window._reset_mode_draft()


@pytest.mark.parametrize('screen', [0, 1])
def test_stale_mode_preview_rejects_gesture_without_losing_draft(window, screen, monkeypatch):
    eid, rid = _place(window, 'legacy')
    window.vm.begin_mode_draft()
    window._refresh_mode_views()
    before = electrical_model_fingerprint(window.editor_controller.model)
    draft, diagram = window.vm.mode_draft, window.editor_controller.diagram
    def stale():
        raise RuntimeError('Черновик устарел: исходная модель изменилась')
    monkeypatch.setattr(window.vm, 'mode_preview_model', stale)
    _double(window, screen, rid)
    assert window.vm.mode_draft == draft and window.editor_controller.diagram == diagram
    assert electrical_model_fingerprint(window.editor_controller.model) == before
    assert 'Черновик устарел' in window.statusBar().currentMessage()
