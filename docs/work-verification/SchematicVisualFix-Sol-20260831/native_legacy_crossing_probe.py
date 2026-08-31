"""Native synthetic crossing evidence, explicitly separate from the user demo."""
from __future__ import annotations

import faulthandler
import os
from pathlib import Path
import sys
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "windows"
faulthandler.enable(all_threads=True)
ROOT = Path(r"C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening")
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (DiagramDocument, DiagramPage, PageId, GraphicalRepresentation,
                                    GraphicalRepresentationId, RepresentationTargetKind)
from rza_calc.domain.electrical import ElectricalModel
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import ProjectEditorController
from rza_calc.gui.editor_scene import DiagramObjectItem, EditorCanvas
from rza_calc.gui.theme import STYLESHEET


def static_center_marker(item):
    return next((primitive for primitive in item.symbol_geometry().primitives
                 if primitive.direction_marker), None)


app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
model = ElectricalModel.with_builtins("Synthetic legacy-line crossing")
page = DiagramPage(PageId("synthetic-crossing-page"), "Синтетическая проверка")
representations = []
for index, angle in enumerate((0, 90)):
    equipment, _ = model.create_equipment("builtin.line", f"Линия {index + 1}")
    representations.append(GraphicalRepresentation(
        GraphicalRepresentationId(f"synthetic-crossing-line-{index}"), page.id,
        RepresentationTargetKind.EQUIPMENT, equipment_id=equipment.id,
        x=300, y=200, rotation_deg=angle, symbol_key="builtin.line",
        extensions={"stage3_graphics": {"width": 160, "height": 24, "label_visible": False}},
    ))
document = DiagramDocument.create("Synthetic crossing", (page,), representations)
controller = ProjectEditorController(SimpleNamespace(
    electrical_model=model, diagram=document, catalog_snapshots=ProjectCatalogSnapshots()))
fingerprint = electrical_model_fingerprint(model)
window = QMainWindow()
root = QWidget()
layout = QVBoxLayout(root)
caption = QLabel()
caption.setWordWrap(True)
layout.addWidget(caption)
canvas = EditorCanvas(controller)
layout.addWidget(canvas)
window.setCentralWidget(root)
window.setWindowTitle("Синтетическая проверка стрелки на мостике — не пользовательский проект")
window.resize(1120, 850)
window.show()
window.raise_()
window.activateWindow()
assert QTest.qWaitForWindowExposed(window, 5000)
canvas.scene.set_grid(visible=False)
canvas.view.set_zoom(3.4)
canvas.view.centerOn(300, 200)
saved = getattr(DiagramObjectItem, "direction_marker", None)


def capture(tag, text):
    caption.setText(text)
    for item in canvas.scene._items_by_id.values():
        item.update()  # Invalidate DeviceCoordinateCache after the display-only override.
    canvas.scene.update()
    app.processEvents()
    QTest.qWait(100)
    path = OUT / f"native-legacy-arrow-crossing-{tag}.png"
    assert window.grab().save(str(path), "PNG")
    print(path, flush=True)
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert controller.diagram.representations == document.representations


try:
    # If the fix has already landed, reproduce its old static-marker behavior
    # only in this isolated in-memory display. No source/project is rewritten.
    if saved is not None:
        DiagramObjectItem.direction_marker = static_center_marker
    capture("before", "ДО: прежняя неподвижная стрелка в центре. Синтетическое пересечение двух линий; электрического соединения здесь нет.")
    if saved is not None:
        DiagramObjectItem.direction_marker = saved
        capture("after", "ПОСЛЕ: стрелка на видимом прямом участке. Та же синтетическая схема; электрическая модель и выводы не изменены.")
    else:
        print("Relocated-marker implementation not available yet; only before evidence captured.", flush=True)
finally:
    if saved is not None:
        DiagramObjectItem.direction_marker = saved
    window.close()
    app.processEvents()
