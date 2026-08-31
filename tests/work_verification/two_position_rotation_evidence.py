"""Actual Qt editor frames for two-position corner rotation, not a mockup.

Uses offscreen QWidget rendering and real QTest mouse events. This is automated
UI evidence, not physical-mouse acceptance, and never saves source examples.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QFontDatabase, QImageReader
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar

from test_rotation_handle import (
    U10, begin_drag, controller_for, document_signature, electrical_signature,
    mouse_at, select_object,
)
from rza_calc.editor import EditorMode
from rza_calc.domain.electrical import VoltageClassId
from rza_calc.gui.editor_panels import EditorWorkspaceWidget
from rza_calc.gui.theme import STYLESHEET


OUTPUT = ROOT / "docs" / "work-verification" / "VisualMerge-Sol-20260831" / "two-position-rotation"


def save_frame(window, filename):
    QApplication.processEvents()
    pixmap = window.grab()
    path = OUTPUT / filename
    if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Cannot save Qt frame: {path}")
    if not QImageReader(str(path)).canRead():
        raise RuntimeError(f"Cannot decode Qt frame: {path}")
    return {"file": filename, "width": pixmap.width(), "height": pixmap.height(),
            "bytes": path.stat().st_size}


def window_for(controller):
    window = QMainWindow()
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    window.setCentralWidget(workspace)
    window.setStatusBar(QStatusBar(window))
    workspace.statusMessage.connect(window.statusBar().showMessage)
    window.setWindowTitle("РЗА — два положения и поворот мышью")
    window.resize(1550, 950)
    window.show()
    QApplication.processEvents()
    workspace.side_panel.tabs.setCurrentIndex(1)
    workspace.canvas.view.set_zoom(2.0)
    workspace.canvas.view.centerOn(300, 260)
    workspace.canvas.view.setFocus()
    return window, workspace


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for filename in ("segoeui.ttf", "arial.ttf", "arialbd.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLESHEET)
    controller = controller_for("evidence")
    transformer = controller.add_equipment(
        "builtin.transformer_2w", "Т1 110/10 кВ", x=280, y=160,
        rotation_deg=90, width=100, height=60,
        voltage_class_by_group={"hv": VoltageClassId("builtin.voltage.ac.110kv"), "lv": U10},
    )
    breaker = controller.add_equipment(
        "builtin.circuit_breaker", "QF1 — ввод 10 кВ", x=280, y=330,
        rotation_deg=90, width=100, height=60,
        voltage_class_by_group={"main": U10},
    )
    controller.connect_ports(transformer.port_ids[-1], breaker.port_ids[0])
    window, workspace = window_for(controller)
    canvas = workspace.canvas
    records = []
    try:
        select_object(canvas, breaker.representation_id)
        point = canvas.scene._rotation_handle.scenePos()
        mouse_at(canvas, point + QPointF(30, 30))
        mouse_at(canvas, point)
        window.statusBar().showMessage("90° · Наведите на круговую стрелку у угла и тяните мышью")
        records.append(save_frame(window, "01-handle-hover-90.png"))
        canvas.view.set_zoom(1.0)
        canvas.view.centerOn(280, 260)
        QApplication.processEvents()
        point = canvas.scene._rotation_handle.scenePos()
        mouse_at(canvas, point + QPointF(30, 30))
        mouse_at(canvas, point)
        window.statusBar().showMessage("Масштаб 100% · Маркер поворота сохраняет размер при изменении масштаба")
        records.append(save_frame(window, "06-handle-hover-100-percent.png"))
        canvas.view.set_zoom(2.0)
        canvas.view.centerOn(300, 260)
        QApplication.processEvents()
        baseline = document_signature(controller), electrical_signature(controller)
        end = begin_drag(canvas, breaker.representation_id)
        assert canvas.scene._items_by_id[breaker.representation_id].rotation() == 180
        assert (document_signature(controller), electrical_signature(controller)) == baseline
        window.statusBar().showMessage("Предпросмотр 180° · Кнопка мыши удерживается · Изменение ещё не записано")
        records.append(save_frame(window, "02-drag-preview-180.png"))
        mouse_at(canvas, end, "release")
        assert controller.diagram.representations[breaker.representation_id].rotation_deg == 180
        assert electrical_signature(controller) == baseline[1]
        assert len(controller.journal) == len(baseline[0][-1]) + 1
        window.statusBar().showMessage("180° · Поворот завершён одной командой · Электрические связи сохранены")
        records.append(save_frame(window, "03-committed-180.png"))
        canvas.set_mode(EditorMode.ANALYSIS)
        assert not canvas.scene._rotation_handle.isVisible()
        window.statusBar().showMessage("Режим анализа · Маркер поворота скрыт · Изменение схемы заблокировано")
        records.append(save_frame(window, "04-analysis-read-only.png"))
    finally:
        window.close()

    collision = controller_for("collision-evidence")
    wide = collision.add_equipment(
        "builtin.transformer_2w", "Т1 — широкий символ", x=260, y=260,
        rotation_deg=90, width=160, height=40,
    )
    collision.add_equipment("builtin.circuit_breaker", "QF2", x=350, y=260,
                            rotation_deg=90, width=40, height=40)
    window, workspace = window_for(collision)
    try:
        workspace.canvas.view.set_zoom(1.0)
        workspace.canvas.view.centerOn(300, 260)
        QApplication.processEvents()
        before = document_signature(collision), electrical_signature(collision)
        end = begin_drag(workspace.canvas, wide.representation_id)
        window.statusBar().showMessage("Предпросмотр пересечения · Поворот запрещён из-за наложения объектов")
        records.append(save_frame(window, "05-collision-preview-rejected.png"))
        mouse_at(workspace.canvas, end, "release")
        assert (document_signature(collision), electrical_signature(collision)) == before
        assert workspace.canvas.scene._items_by_id[wide.representation_id].rotation() == 90
    finally:
        window.close()
    summary = {
        "runtime": "Actual EditorWorkspaceWidget / DiagramGraphicsScene with QTest events",
        "qpa_platform": app.platformName(), "physical_mouse_acceptance": False,
        "source_examples_modified": False,
        "preview_read_only": True, "commit_history_entries": 1,
        "electrical_fingerprint_and_port_ids_preserved": True,
        "analysis_handle_hidden": True, "collision_rejection_atomic": True,
        "frames": records,
    }
    (OUTPUT / "evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
