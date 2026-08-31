"""Explicit voltage selection in a new in-memory apparatus, then full undo."""
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
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from test_voltage_inspector import _select, _combo, _choose, U10, U110

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
errors, frames = [], []
def report_error(kind, value, tb):
    message = "".join(traceback.format_exception(kind, value, tb))
    errors.append(message)
    print(message, file=sys.stderr, flush=True)
sys.excepthook = report_error
raw = DEMO.read_bytes()
window = MainWindow(ProjectViewModel.open(DEMO))
window.setWindowTitle("ПРОВЕРКА БЕЗ СОХРАНЕНИЯ — напряжения сторон трансформатора")
window.resize(1760, 1000)
window.show()
window.raise_()
window.activateWindow()
assert QTest.qWaitForWindowExposed(window, 5000)
workspace = window.editor_workspace
canvas, controller = workspace.canvas, workspace.controller
before = (electrical_model_fingerprint(controller.model), dict(controller.model.ports),
          dict(controller.diagram.representations), dict(controller.diagram.routes))

def capture(name):
    app.processEvents()
    path = OUT / (name + ".png")
    assert window.grab().save(str(path), "PNG")
    assert not errors and DEMO.read_bytes() == raw
    tree = workspace.inspector.tree
    frames.append({"phase": name, "png": str(path), "inspector_width": workspace.inspector.width(),
                   "viewport_width": tree.viewport().width(), "column_widths": [tree.columnWidth(i) for i in range(4)]})

try:
    added = controller.add_equipment("builtin.transformer_2w", "Новый Т — явный выбор ВН/НН", x=-2000, y=-2000)
    canvas.refresh()
    canvas.view.actual_size()
    canvas.view.centerOn(-2000, -2000)
    canvas.scene.select_representations((added.representation_id,))
    _select(workspace, added.representation_id)
    hv = _combo(workspace, "equipment.voltage_class.hv")
    lv = _combo(workspace, "equipment.voltage_class.lv")
    assert hv.currentIndex() == lv.currentIndex() == -1
    assert not controller.model.equipment[added.equipment_id].voltage_class_by_group
    capture("native-voltage-unknown")
    _choose(hv, U110)
    _select(workspace, added.representation_id)
    _choose(_combo(workspace, "equipment.voltage_class.lv"), U10)
    _select(workspace, added.representation_id)
    equipment = controller.model.equipment[added.equipment_id]
    assert equipment.voltage_class_by_group == {"hv": U110, "lv": U10}
    assert equipment.port_ids == added.port_ids
    assert "rated_voltage_v" not in equipment.properties
    canvas.view.centerOn(-2000, -2000)
    capture("native-voltage-hv110-lv10")
    for _ in range(3):
        canvas.undo()
    after = (electrical_model_fingerprint(controller.model), dict(controller.model.ports),
             dict(controller.diagram.representations), dict(controller.diagram.routes))
    assert after == before and DEMO.read_bytes() == raw and not errors
    result = {"pid": os.getpid(), "source_sha256": hashlib.sha256(raw).hexdigest(),
              "unknown_fields_have_no_default": True, "explicit_hv": "110 kV", "explicit_lv": "10 kV",
              "same_apparatus_port_ids": True, "undo_restores_entire_original_project": True,
              "project_saved": False, "qt_callback_errors": errors, "frames": frames}
    (OUT / "native-voltage-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
finally:
    window.hide()
    window.deleteLater()
    app.processEvents()
