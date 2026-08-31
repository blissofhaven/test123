"""Exercise all seven actual series pairs in our own Windows MainWindow."""
from __future__ import annotations
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

os.environ["QT_QPA_PLATFORM"] = "windows"
faulthandler.enable(all_threads=True)
ROOT = Path(r"C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening")
OUT = Path(__file__).resolve().parent
DEMO = ROOT / "rza_calc/examples/energoraion.json"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from test_ui_direct_connections import _actual_series_pairs, _geometry, _scene_geometry, _mouse

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
errors, frames, rows = [], [], []
def report_error(kind, value, tb):
    message = "".join(traceback.format_exception(kind, value, tb))
    errors.append(message)
    print(message, file=sys.stderr, flush=True)
sys.excepthook = report_error
digest = hashlib.sha256(DEMO.read_bytes()).hexdigest()
view_model = ProjectViewModel.open(DEMO)
window = MainWindow(view_model)
window.setWindowTitle("ПРОВЕРКА БЕЗ СОХРАНЕНИЯ — согласованная трасса")
window.resize(1760, 1000)
window.show()
window.raise_()
window.activateWindow()
assert QTest.qWaitForWindowExposed(window, 5000)
QTest.qWait(120)
canvas = window.editor_workspace.canvas
controller = canvas.controller
original = controller.diagram
electrical = (electrical_model_fingerprint(controller.model), controller.model.connectivity_signature(),
              dict(controller.model.ports), dict(controller.model.connections))
canvas.view.actual_size()
canvas.set_snap_enabled(False)


def check():
    assert electrical == (electrical_model_fingerprint(controller.model), controller.model.connectivity_signature(),
                          dict(controller.model.ports), dict(controller.model.connections))
    assert digest == hashlib.sha256(DEMO.read_bytes()).hexdigest()
    assert not errors, errors


def capture(name):
    app.processEvents()
    path = OUT / (name + ".png")
    assert window.grab().save(str(path), "PNG")
    check()
    frames.append({"phase": name, "png": str(path)})


try:
    # controller holds the same actual project as the user-facing MainWindow.
    pairs = _actual_series_pairs(controller._project)
    assert len(pairs) == 7
    for index, (node, breaker) in enumerate(pairs):
        item = canvas.scene._items_by_id[breaker.id]
        name = item._name
        canvas.view.centerOn(item.pos() + QPointF(0, -90))
        canvas.view.setFocus()
        app.processEvents()
        point = item.body_scene_rect().center()
        journal = len(controller.journal)
        if index == 0:
            capture("native-reflow-before")
        _mouse(canvas, "move", point + QPointF(20, 20))
        _mouse(canvas, "move", point)
        _mouse(canvas, "press", point)
        _mouse(canvas, "move", point + QPointF(0, -90))
        _mouse(canvas, "move", point + QPointF(0, -180))
        planned = controller.preview_move_representations((breaker.id,), 0, -180, bypass_snap=True)
        assert canvas.scene._start_positions
        if _scene_geometry(canvas) != _geometry(planned):
            print(json.dumps({"unexpected_preview": name, "item_position": [item.x(), item.y()],
                              "gesture": [identifier.value for identifier in canvas.scene._start_positions],
                              "wanted": [breaker.x, breaker.y - 180],
                              "representation_differences": [key.value for key, value in _scene_geometry(canvas)[0].items()
                                                             if value != _geometry(planned)[0][key]],
                              "route_differences": [key.value for key, value in _scene_geometry(canvas)[1].items()
                                                    if value != _geometry(planned)[1][key]]}, ensure_ascii=False), flush=True)
        assert _scene_geometry(canvas) == _geometry(planned)
        assert node.id in canvas.scene._body_drag_auxiliary_positions
        assert controller.diagram.representations == original.representations
        assert controller.diagram.routes == original.routes
        if index == 0:
            capture("native-reflow-preview")
        _mouse(canvas, "release", point + QPointF(0, -180))
        assert _geometry(controller.diagram) == _geometry(planned)
        assert len(controller.journal) == journal + 1
        check()
        if index == 0:
            capture("native-reflow-commit")
        canvas.undo()
        assert controller.diagram.representations == original.representations
        assert controller.diagram.routes == original.routes
        assert _scene_geometry(canvas) == _geometry(original)
        if index == 0:
            capture("native-reflow-undo")
        journal = len(controller.journal)
        point = item.body_scene_rect().center()
        _mouse(canvas, "move", point + QPointF(20, 20))
        _mouse(canvas, "move", point)
        _mouse(canvas, "press", point)
        for delta in (-60, -120, -40, -180, -80):
            _mouse(canvas, "move", point + QPointF(0, delta))
            assert canvas.scene._start_positions
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        app.processEvents()
        _mouse(canvas, "release", point + QPointF(0, -80))
        assert _scene_geometry(canvas) == _geometry(original)
        assert len(controller.journal) == journal
        assert not canvas.scene._body_drag_auxiliary_positions
        check()
        rows.append({"name": name, "breaker_id": breaker.id.value, "joint_id": node.id.value,
                     "preview_equals_commit_geometry": True, "undo_restores_all_ids_and_waypoints": True,
                     "five_moves_escape_restores_auxiliary_joint": True})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    result = {"pid": os.getpid(), "platform": "windows", "source_sha256": digest,
              "electrical_fingerprint": electrical[0], "pairs": rows, "frames": frames,
              "qt_callback_errors": errors, "project_saved": False,
              "final_representations_and_routes_equal_original": True}
    (OUT / "native-reflow-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
finally:
    window.hide()
    window.deleteLater()
    app.processEvents()
