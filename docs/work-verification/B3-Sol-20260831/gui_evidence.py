"""Windows Qt evidence for B3; no source project is saved or overwritten."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

os.environ["QT_QPA_PLATFORM"] = "windows"
ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from test_ui_b3_label_geometry import measure_scene_conflicts


def visible(scene):
    return [item._label for item in scene._label_owners() if item._label.isVisible()]


def render(scene, rect, filename):
    image = QImage(int(rect.width()), int(rect.height()), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scene.render(painter, QRectF(0, 0, rect.width(), rect.height()), rect)
    painter.end()
    assert image.save(str(OUTPUT / filename))


def main():
    sources = sorted((ROOT / "rza_calc/examples").glob("*.json"))
    before_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    vm = ProjectViewModel.open(ROOT / "rza_calc/examples/energoraion.json")
    fingerprint = electrical_model_fingerprint(vm.project.electrical_model)
    ports = tuple(vm.project.electrical_model.ports.values())
    window = MainWindow(vm)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1920, 1100)
    window.show()
    QTest.qWait(100)
    workspace = window.editor_workspace
    workspace.view.actual_size()
    scene = workspace.scene
    before_conflicts = len(measure_scene_conflicts(scene))
    workspace.command_bar.auto_labels_action.trigger()
    workspace.view.actual_size()
    app.processEvents()
    after_conflicts = measure_scene_conflicts(scene)
    assert not after_conflicts, after_conflicts[:10]
    assert len(scene._items_by_id) == 157
    assert len(visible(scene)) == 120
    rectangles = [label.sceneBoundingRect() for label in visible(scene)]
    candidates = [QRectF(rect.center().x() - 700, rect.center().y() - 450, 1400, 900) for rect in rectangles]
    dense = max(candidates, key=lambda candidate: sum(candidate.intersects(rect) for rect in rectangles))
    render(scene, dense, "01-dense-labels-color.png")
    workspace.view.centerOn(dense.center())
    app.processEvents()
    assert window.grab().save(str(OUTPUT / "02-editor-window.png"))
    window.monochrome_action.setChecked(True)
    render(scene, dense, "03-dense-labels-monochrome.png")
    window.monochrome_action.setChecked(False)
    workspace.view.set_zoom(0.59)
    assert not visible(scene)
    workspace.view.fit_all()
    assert not visible(scene)
    app.processEvents()
    assert window.grab().save(str(OUTPUT / "04-overview-labels-hidden.png"))
    workspace.view.actual_size()
    assert len(visible(scene)) == 120
    window.workspace_tabs.setCurrentIndex(1)
    app.processEvents()
    analysis = window.diagram_panel.view
    analysis.view.actual_size()
    analysis.view.centerOn(dense.center())
    assert len(visible(analysis.scene)) == 120
    assert not measure_scene_conflicts(analysis.scene)
    render(analysis.scene, dense, "05-analysis-dense.png")
    assert electrical_model_fingerprint(vm.project.electrical_model) == fingerprint
    assert tuple(vm.project.electrical_model.ports.values()) == ports
    assert {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources} == before_hashes
    result = {
        "qt_platform": app.platformName(), "physical_mouse_acceptance": False,
        "representations": 157, "routes": len(scene._route_items_by_id),
        "visible_labels_at_100_percent": 120, "clearance_scene_units": 4,
        "legacy_pinned_conflicts_before_explicit_reset": before_conflicts,
        "auto_layout_conflicts": len(after_conflicts),
        "analysis_layout_conflicts": 0,
        "hidden_below_zoom": 0.6, "source_files_unchanged": before_hashes,
        "electrical_fingerprint_unchanged": fingerprint, "ports_unchanged": True,
        "dense_region": [dense.x(), dense.y(), dense.width(), dense.height()],
    }
    (OUTPUT / "gui-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    window.close()
    app.quit()


if __name__ == "__main__":
    main()
