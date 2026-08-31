"""Own native window; capture real direct-terminal gestures without saving."""
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

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from rza_calc.domain.electrical import VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.connection_tool import ConnectionTargetFeedback
from rza_calc.editor.connection_voltage import endpoint_voltage
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from test_ui_direct_connections import _mouse

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
errors, frames = [], []
sys.excepthook = lambda kind, value, tb: errors.append("".join(traceback.format_exception(kind, value, tb)))
digest = hashlib.sha256(DEMO.read_bytes()).hexdigest()
window = MainWindow(ProjectViewModel.open(DEMO))
window.setWindowTitle("ПРОВЕРКА БЕЗ СОХРАНЕНИЯ — протяжка от вывода")
window.resize(1760, 1000)
window.show()
window.raise_()
window.activateWindow()
assert QTest.qWaitForWindowExposed(window, 5000)
QTest.qWait(120)
canvas = window.editor_workspace.canvas
controller = canvas.controller
before = (electrical_model_fingerprint(controller.model), controller.model.connectivity_signature(),
          dict(controller.diagram.representations), dict(controller.diagram.routes), len(controller.journal))
canvas.view.actual_size()
canvas.set_snap_enabled(False)


def unchanged():
    assert before == (electrical_model_fingerprint(controller.model), controller.model.connectivity_signature(),
                      dict(controller.diagram.representations), dict(controller.diagram.routes), len(controller.journal))
    assert digest == hashlib.sha256(DEMO.read_bytes()).hexdigest()
    assert not errors, errors


def capture(name, widget=window):
    app.processEvents()
    path = OUT / (name + ".png")
    assert widget.grab().save(str(path), "PNG")
    unchanged()
    frames.append({"phase": name, "png": str(path), "electrical_and_graphical_document_unchanged": True})


def begin_pair(first, second):
    start, end = QPointF(first.scenePos()), QPointF(second.scenePos())
    canvas.view.centerOn((start + end) / 2)
    canvas.view.setFocus()
    app.processEvents()
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    assert canvas.scene._connection_press_position is not None
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    return end


try:
    ports = [port for item in canvas.scene._items_by_id.values()
             if item._canonical_key not in {"line", "line_section"} for port in item._port_items.values()]
    low = [port for port in ports if endpoint_voltage(controller.model, port.port_id).voltage_class_id == VoltageClassId("builtin.voltage.ac.10kv")]
    high = [port for port in ports if endpoint_voltage(controller.model, port.port_id).voltage_class_id == VoltageClassId("builtin.voltage.ac.110kv")]
    distance = lambda pair: (pair[0].scenePos() - pair[1].scenePos()).manhattanLength()
    first, second = min(((a, b) for a in low for b in high), key=distance)
    end = begin_pair(first, second)
    assert controller.model.port_voltage_class(first.port_id) is None
    assert controller.model.port_voltage_class(second.port_id) is None
    assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.INCOMPATIBLE
    mismatch = {"source": first.port_id.value, "target": second.port_id.value,
                "message": canvas.scene._connection_target.message}
    capture("native-direct-voltage-rejected")
    _mouse(canvas, "release", end)
    assert not canvas.scene.connection_active
    unchanged()

    candidates = [(a, b) for a in low for b in low if a.parentItem() is not b.parentItem()
                  and controller.model.connection_for_port(a.port_id) is not None
                  and controller.model.connection_for_port(b.port_id) is not None
                  and controller.model.connection_for_port(a.port_id).electrical_node_id
                      != controller.model.connection_for_port(b.port_id).electrical_node_id]
    first, second = min(candidates, key=distance)
    end = begin_pair(first, second)
    assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.COMPATIBLE
    capture("native-direct-compatible-preview")
    menus = []

    def cancel_menu():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        menus.append([action.text() for action in menu.actions() if not action.isSeparator()])
        assert len(menus[-1]) == 4
        assert canvas.scene._connection_press_position is None
        assert not canvas.scene.connection_active
        capture("native-direct-type-menu", menu)
        capture("native-direct-type-menu-parent")
        QTest.keyClick(menu, Qt.Key.Key_Escape)

    QTimer.singleShot(80, cancel_menu)
    _mouse(canvas, "release", end)
    assert menus
    capture("native-direct-menu-cancelled")
    unchanged()
    result = {"pid": os.getpid(), "platform": "windows", "source_sha256": digest,
              "electrical_fingerprint": before[0], "mismatch": mismatch,
              "menu_actions": menus[0], "frames": frames, "qt_callback_errors": errors,
              "project_saved": False, "journal_unchanged": True}
    (OUT / "native-direct-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
finally:
    window.hide()
    window.deleteLater()
    app.processEvents()
