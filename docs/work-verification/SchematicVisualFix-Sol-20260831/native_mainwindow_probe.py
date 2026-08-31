"""Separate Windows/MainWindow QTest session; never save the user project."""
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
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui import editor_scene
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel


def identity(controller):
    model = controller.model
    return (electrical_model_fingerprint(model), model.revision,
            model.connectivity_signature(), dict(model.equipment),
            dict(model.electrical_nodes), dict(model.connections), dict(model.ports),
            frozenset(controller.diagram.representations), frozenset(controller.diagram.routes),
            {route.id: (route.start_anchor, route.end_anchor) for route in controller.diagram.routes.values()})


def interior_junctions(displays):
    routes = [display for display in displays.values() if display.wire.key.startswith("route:")]
    return [(display.wire.key, point) for display in routes for point in display.junctions
            if not any(other.wire.node_id == display.wire.node_id
                       and point in (other.wire.points[0], other.wire.points[-1])
                       for other in routes)]


app = QApplication([])
app.setApplicationName("РЗА-Про — проверка без сохранения")
app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
errors = []
sys.excepthook = lambda kind, value, tb: errors.append("".join(traceback.format_exception(kind, value, tb)))
source_hash = hashlib.sha256(DEMO.read_bytes()).hexdigest()
window = MainWindow(ProjectViewModel.open(DEMO))
window.setWindowTitle("ПРОВЕРКА БЕЗ СОХРАНЕНИЯ — РЗА-Про")
window.resize(1760, 1000)
window.show()
window.raise_()
window.activateWindow()
assert QTest.qWaitForWindowExposed(window, 5000)
QTest.qWait(200)
canvas = window.editor_workspace.canvas
controller = canvas.controller
initial_identity = identity(controller)
initial_representations = dict(controller.diagram.representations)
initial_routes = dict(controller.diagram.routes)
assert len(initial_representations) == 157 and len(initial_routes) == 160
window.editor_workspace.command_bar.snap_action.setChecked(False)
captured = {}
planner = editor_scene.build_wire_displays


def capture(wires):
    result = planner(wires)
    captured.clear()
    captured.update(result)
    return result


editor_scene.build_wire_displays = capture
phases = []


def snapshot(name):
    app.processEvents()
    QTest.qWait(60)
    pixmap = window.grab()
    assert not pixmap.isNull()
    path = OUT / (name + ".png")
    assert pixmap.save(str(path), "PNG")
    assert not errors, errors
    assert identity(controller) == initial_identity
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == source_hash
    false_nodes = interior_junctions(captured)
    assert not false_nodes, false_nodes
    collision_ids = sorted(canvas.scene._drag_collision_ids)
    if name in {"native-bus-out-preview", "native-bus-back-preview"}:
        assert not collision_ids, collision_ids
    record = {"phase": name, "png": str(path), "size": [pixmap.width(), pixmap.height()],
              "journal_count": len(controller.journal), "false_interior_junctions": false_nodes,
              "collision_preview_ids": collision_ids,
              "electrical_identity_unchanged": True, "source_unchanged": True,
              "zoom": canvas.view.zoom_factor}
    phases.append(record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def mouse(kind, point):
    pos = canvas.view.mapFromScene(point)
    if kind == "move":
        QTest.mouseMove(canvas.view.viewport(), pos, delay=5)
    else:
        {"press": QTest.mousePress, "release": QTest.mouseRelease}[kind](
            canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    app.processEvents()


try:
    canvas.scene._refresh_route_bridges()
    canvas.view.fit_all()
    snapshot("native-mainwindow-full")
    item = next(item for item in canvas.scene._items_by_id.values()
                if item._name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ")
    initial_position = QPointF(item.pos())
    canvas.view.actual_size()
    canvas.scene.select_representations((item.representation_id,))
    canvas.view.centerOn(item.pos())
    canvas.view.viewportChanged.emit(canvas.view.viewport_state())
    canvas.view.setFocus()
    app.processEvents()
    snapshot("native-bus-before")
    before_journal = len(controller.journal)
    for step, delta in enumerate((40.0, -40.0), start=1):
        canvas.view.centerOn(item.pos())
        canvas.view.viewportChanged.emit(canvas.view.viewport_state())
        canvas.view.setFocus()
        app.processEvents()
        point = item.body_scene_rect().center() + QPointF(40, 0)
        hit = canvas.scene.resolve_hit_target(point, canvas.view.transform())
        assert hit.kind is editor_scene.HitTestKind.BODY and hit.item is item
        stored = controller.diagram
        mouse("move", point + QPointF(0, 25))
        mouse("move", point)
        mouse("press", point)
        mouse("move", point + QPointF(delta / 2, 0))
        mouse("move", point + QPointF(delta, 0))
        assert controller.diagram is stored
        snapshot(f"native-bus-{'out' if step == 1 else 'back'}-preview")
        mouse("release", point + QPointF(delta, 0))
        assert len(controller.journal) == before_journal + step
        expected = initial_position + (QPointF(40, 0) if step == 1 else QPointF())
        assert item.pos() == expected
        snapshot(f"native-bus-{'out' if step == 1 else 'back'}-commit")
    canvas.undo()
    canvas.undo()
    assert dict(controller.diagram.representations) == initial_representations
    assert dict(controller.diagram.routes) == initial_routes
    assert item.pos() == initial_position
    snapshot("native-bus-undo-restored")
    lines = [row for row in canvas.scene._items_by_id.values()
             if row._canonical_key in {"line", "line_section"}]
    line = next((row for row in lines if "Ф-2" in row._name and "Централь" in row._name), lines[0])
    canvas.scene.select_representations((line.representation_id,))
    canvas.view.set_zoom(3.0)
    canvas.view.centerOn(line.pos())
    canvas.view.viewportChanged.emit(canvas.view.viewport_state())
    snapshot("native-line-arrow-detail")
    canvas.scene.clearSelection()
    canvas.view.set_zoom(1.8)
    canvas.view.centerOn(line.pos())
    canvas.view.viewportChanged.emit(canvas.view.viewport_state())
    mouse("move", canvas.view.mapToScene(canvas.view.viewport().rect().topLeft()))
    snapshot("native-line-arrow-unselected")
    print(json.dumps({"platform": app.platformName(), "pid": os.getpid(),
                      "source_sha256": source_hash, "model_fingerprint": initial_identity[0],
                      "legacy_line_arrows": sum(any(p.direction_marker for p in row.symbol_geometry().primitives)
                                                 for row in lines),
                      "line_detail_name": line._name, "qt_callback_errors": errors,
                      "phase_count": len(phases), "saved_project": False}, ensure_ascii=False), flush=True)
finally:
    editor_scene.build_wire_displays = planner
    window.close()
    app.processEvents()
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == source_hash
