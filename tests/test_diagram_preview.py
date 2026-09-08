"""Saved-geometry preview cannot edit, switch, calculate or steer the editor."""
from dataclasses import replace
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.diagram import GraphicalRepresentationId, PageId
from rza_calc.domain.electrical import OperatingState, OperatingStateId, SwitchPosition
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.diagram_preview import DiagramPreviewWidget, PreviewTarget
from rza_calc.gui.editor_scene import EditorCanvas
from test_stage4_editor_interaction import _controller, _U10


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def sample(monkeypatch):
    from rza_calc.core import engine
    from rza_calc.calculation import point_fault
    from rza_calc.core.short_circuit import ShortCircuitSolver
    from rza_calc.core.sequence_network import SequenceFaultSolver
    from rza_calc.gui import project_settings
    def forbidden(*args, **kwargs):
        raise AssertionError('Preview must not calculate or access user settings')
    monkeypatch.setattr(engine, 'run_input', forbidden)
    monkeypatch.setattr(point_fault, 'run_point_faults', forbidden)
    monkeypatch.setattr(project_settings, 'QSettings', forbidden)
    for solver in (ShortCircuitSolver, SequenceFaultSolver):
        for name in ('at', 'fault_at', 'fault_network_at'):
            if hasattr(solver, name):
                monkeypatch.setattr(solver, name, forbidden)
    controller = _controller()
    first = next(iter(controller.diagram.pages))
    bus = controller.add_electrical_node('Шины 10 кВ', x=-100, y=-80,
        voltage_class_id=_U10, symbol_key='busbar', width=220, height=12)
    breaker = controller.add_equipment('builtin.circuit_breaker', 'QF 1', x=100, y=60)
    second = controller.create_page('Второй лист')
    original = controller.diagram.representations[breaker.representation_id]
    peer = replace(original, id=GraphicalRepresentationId.new(), page_id=second, x=1000, y=500)
    controller._project.diagram = replace(controller.diagram, representations={
        **controller.diagram.representations, peer.id: peer})
    closed = OperatingStateId('preview.closed')
    opened = OperatingStateId('preview.open')
    controller.model.add_operating_state(OperatingState(closed, 'Включён', positions={breaker.equipment_id: SwitchPosition.CLOSED}))
    controller.model.add_operating_state(OperatingState(opened, 'Отключён', positions={breaker.equipment_id: SwitchPosition.OPEN}))
    # These direct writes describe the loaded fixture, before its command history.
    controller = ProjectEditorController(controller._project)
    preview = DiagramPreviewWidget()
    preview.resize(450, 300)
    preview.show()
    preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
    QApplication.processEvents()
    yield controller, preview, first, second, bus, breaker, peer, closed, opened
    preview.close()
    preview.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def test_real_preview_gestures_are_readonly_and_open_is_only_signal(sample):
    controller, preview, first, second, bus, breaker, peer, closed, opened = sample
    editor = EditorCanvas(controller)
    editor.resize(800, 500)
    editor.show()
    editor.show_page(first)
    editor.scene.select_representations((bus.representation_id,))
    QApplication.processEvents()
    before_view = editor.view.viewport_state()
    before_fp = electrical_model_fingerprint(controller.model)
    before_document, history = controller.diagram, len(controller.journal)
    emitted = []
    for name in ('switchDraftRequested', 'pointFaultRequested'):
        if hasattr(preview.scene, name):
            getattr(preview.scene, name).connect(lambda *args, n=name: emitted.append(n))
    for name in ('equipmentContextActionRequested', 'equipmentDetailsRequested', 'linkedPageRequested',
                 'rotateRequested', 'routeDeleteRequested', 'representationMoved'):
        if hasattr(preview.scene, name):
            getattr(preview.scene, name).connect(lambda *args, n=name: emitted.append(n))
    requests = []
    preview.openRequested.connect(requests.append)
    try:
        assert preview.set_page(second, representation_ids=(peer.id,))
        QApplication.processEvents()
        item = preview.scene._items_by_id[peer.id]
        assert item.pos() == QPointF(peer.x, peer.y)
        point = preview.view.mapFromScene(item.sceneBoundingRect().center())
        QTest.mouseClick(preview.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseDClick(preview.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseClick(preview.view.viewport(), Qt.MouseButton.RightButton, pos=point)
        for key in (Qt.Key.Key_R, Qt.Key.Key_Delete, Qt.Key.Key_Right, Qt.Key.Key_Escape):
            QTest.keyClick(preview.view, key)
        assert not emitted and not requests
        assert preview.scene.selected_representation_ids() == ()
        QTest.mouseClick(preview.open_button, Qt.MouseButton.LeftButton)
        assert requests == [PreviewTarget(second, (peer.id,), ())]
        assert editor.page_id == first
        assert editor.scene.selected_representation_ids() == (bus.representation_id,)
        assert editor.view.viewport_state() == before_view
        assert preview.scene is not editor.scene
        assert controller.diagram == before_document
        assert electrical_model_fingerprint(controller.model) == before_fp
        assert len(controller.journal) == history
    finally:
        editor.close()
        editor.deleteLater()


def test_selection_and_resize_reuse_page_then_revision_and_mode_refresh(sample, monkeypatch):
    controller, preview, first, second, bus, breaker, peer, closed, opened = sample
    compile_calls = []
    original = preview.scene._topology_engine.compile
    def compile(*args, **kwargs):
        compile_calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(preview.scene._topology_engine, 'compile', compile)
    assert preview.set_page(first)
    assert len(compile_calls) == 1
    for width in [500, 320, 600]:
        preview.resize(width, 320)
        QApplication.processEvents()
        preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
        preview.set_page(first, representation_ids=(breaker.representation_id,))
        preview.set_page(first, representation_ids=(bus.representation_id,))
    assert len(compile_calls) == 1
    controller.model.rename_equipment(breaker.equipment_id, 'QF renamed')
    preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
    assert len(compile_calls) == 2
    assert preview.scene._model_revision == controller.model.revision
    assert preview.scene._items_by_id[breaker.representation_id]._model.equipment[breaker.equipment_id].name == 'QF renamed'
    # An explicitly saved drawing label remains authoritative for this view.
    assert preview.scene._items_by_id[breaker.representation_id]._label_content.display_name == 'QF 1'
    controller.model.rename_equipment(breaker.equipment_id, 'QF second rename')
    preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
    assert len(compile_calls) == 3
    assert preview.scene._model_revision == controller.model.revision
    assert preview.scene._items_by_id[breaker.representation_id]._model.equipment[breaker.equipment_id].name == 'QF second rename'
    preview.set_project(controller.diagram, controller.model, operating_state_id=opened)
    assert len(compile_calls) == 4
    assert preview.scene._items_by_id[breaker.representation_id]._switch_open
    assert preview.scene.topology_snapshot.operating_state.positions[breaker.equipment_id].position is SwitchPosition.OPEN
    preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
    assert not preview.scene._items_by_id[breaker.representation_id]._switch_open


def test_saved_geometry_change_and_second_view_do_not_relayout_source(sample):
    controller, preview, first, second, bus, breaker, peer, closed, opened = sample
    fingerprint = electrical_model_fingerprint(controller.model)
    assert preview.set_page(second, representation_ids=(peer.id,))
    original = controller.diagram.representations[breaker.representation_id]
    moved = replace(peer, x=1220, y=-340, rotation_deg=90)
    document = replace(controller.diagram, representations={**controller.diagram.representations, moved.id: moved})
    preview.set_project(document, controller.model, operating_state_id=closed)
    item = preview.scene._items_by_id[moved.id]
    assert item.pos() == QPointF(1220, -340)
    assert item.rotation() == 90
    assert document.representations[original.id] == original
    assert controller.diagram.representations[peer.id] == peer
    assert electrical_model_fingerprint(controller.model) == fingerprint


def test_missing_page_target_and_unknown_mode_never_leave_old_preview_current(sample):
    controller, preview, first, second, bus, breaker, peer, closed, opened = sample
    assert preview.set_page(first)
    assert not preview.set_page(PageId('missing.page'))
    assert preview.view.isHidden() and not preview.open_button.isEnabled()
    assert 'Лист не найден' in preview.message_label.text()
    assert not preview.set_page(second, representation_ids=(breaker.representation_id,))
    assert 'не найдено' in preview.message_label.text()
    preview.set_project(controller.diagram, controller.model,
                        operating_state_id=OperatingStateId('deleted.mode'))
    assert preview.set_page(first)
    assert 'не определено' in preview.message_label.text()
    assert not preview.scene._render_context.state_available
    assert preview.scene._operating_state_id is not closed
    preview.clear()
    assert preview.target is None and preview.view.isHidden()
    assert preview.scene._document is None
    assert not preview.open_button.isEnabled()
    assert preview.set_page(second)


def test_empty_page_is_honest_but_can_be_opened(sample):
    controller, preview, first, second, bus, breaker, peer, closed, opened = sample
    empty = controller.create_page('Ещё не нарисовано')
    preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
    assert preview.set_page(empty)
    assert preview.view.isHidden()
    assert 'ещё нет рисунка' in preview.message_label.text()
    assert preview.open_button.isEnabled()


def test_topology_failure_keeps_saved_drawing_with_explicit_unknown_state(sample, monkeypatch):
    from rza_calc.topology import TopologyError
    controller, preview, first, *_ = sample
    def unresolved(*args, **kwargs):
        raise TopologyError('Incomplete input')
    monkeypatch.setattr(preview.scene._topology_engine, 'compile', unresolved)
    assert preview.set_page(first)
    assert not preview.view.isHidden()
    assert preview.scene.topology_snapshot is None
    assert 'не определено' in preview.message_label.text()


def test_saved_physical_route_and_its_two_port_anchors_are_not_reconstructed(sample):
    from test_ui_interaction_gui import _physical_line
    controller, preview, first, second, bus, breaker, peer, closed, opened = sample
    created = _physical_line(controller, (400, 200), (800, 350))
    route = next(row for row in controller.diagram.routes.values() if row.equipment_id == created.section_id)
    before_document = controller.diagram
    before_fp = electrical_model_fingerprint(controller.model)
    before_ports = tuple(controller.model.ports.values())
    preview.set_project(controller.diagram, controller.model, operating_state_id=closed)
    assert preview.set_page(route.page_id, route_ids=(route.id,))
    shown = preview.scene._route_items_by_id[route.id]
    assert shown.route is route
    assert shown.route.start_anchor == route.start_anchor
    assert shown.route.end_anchor == route.end_anchor
    assert not shown._path.isEmpty()
    assert controller.diagram is before_document
    assert tuple(controller.model.ports.values()) == before_ports
    assert electrical_model_fingerprint(controller.model) == before_fp
