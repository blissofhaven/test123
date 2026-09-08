"""Overview movement persists as diagram presentation, preserving electrical data."""
import hashlib
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtWidgets import QApplication
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.overview_panel import OVERVIEW_LAYOUT_KEY
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import save_project, load_project


def test_drag_layout_save_reload_undo_preserves_every_electrical_endpoint(tmp_path, monkeypatch):
    from rza_calc.core.short_circuit import ShortCircuitSolver
    def forbidden(*a, **kw):
        raise AssertionError('Moving overview cards must not calculate')
    monkeypatch.setattr(ShortCircuitSolver, '__init__', forbidden)
    app = QApplication.instance() or QApplication([])
    path = Path(__file__).resolve().parents[1] / 'rza_calc/examples/compact_training.json'
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    vm = ProjectViewModel.open(path, calculate=False)
    window = MainWindow(vm, start_in_overview=True)
    panel = window.overview_workspace
    try:
        before = vm.project.diagram
        fp = electrical_model_fingerprint(vm.project.electrical_model)
        positions = panel.canvas.positions()
        key = next(iter(positions))
        positions[key] = (positions[key][0] + 47, positions[key][1] - 23)
        panel._remember_positions(positions)
        app.processEvents()
        assert electrical_model_fingerprint(vm.project.electrical_model) == fp
        assert vm.project.diagram.pages == before.pages
        assert vm.project.diagram.representations == before.representations
        assert vm.project.diagram.routes == before.routes
        assert tuple(vm.project.diagram.extensions[OVERVIEW_LAYOUT_KEY]['positions'][key]) == positions[key]
        destination = tmp_path / 'layout.json'
        save_project(destination, vm.project)
        reloaded = load_project(destination)
        assert electrical_model_fingerprint(reloaded.electrical_model) == fp
        assert tuple(reloaded.diagram.extensions[OVERVIEW_LAYOUT_KEY]['positions'][key]) == positions[key]
        window.editor_controller.undo()
        app.processEvents()
        assert OVERVIEW_LAYOUT_KEY not in vm.project.diagram.extensions
        assert electrical_model_fingerprint(vm.project.electrical_model) == fp
        assert vm.project.diagram.representations == before.representations
        assert vm.project.diagram.routes == before.routes
        window.editor_controller.redo()
        app.processEvents()
        assert tuple(vm.project.diagram.extensions[OVERVIEW_LAYOUT_KEY]['positions'][key]) == positions[key]
        assert vm.result is None
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


def test_overview_card_collision_chooses_an_available_position():
    from rza_calc.gui.overview_canvas import OverviewCanvas, OverviewCard
    app = QApplication.instance() or QApplication([])
    canvas = OverviewCanvas()
    try:
        canvas.set_network((OverviewCard('a', 'А', 'substation'), OverviewCard('b', 'Б', 'ktp')), ())
        a, b = canvas.cards['a'], canvas.cards['b']
        candidate = canvas.free_position(a, b.pos())
        assert not a.boundingRect().translated(candidate).intersects(b.sceneBoundingRect())
        a.setPos(candidate)
        assert not a.sceneBoundingRect().intersects(b.sceneBoundingRect())
    finally:
        canvas.close()
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
