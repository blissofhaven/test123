# -*- coding: utf-8 -*-
"""Автоматизированные Windows/Qt-кадры для automatic junctions.

Это диагностический harness, а не ручная мышиная приёмка. Он запускает
настоящий ``EditorCanvas`` через Windows QPA, проверяет состояние модели до
commit и сохраняет PNG реального QWidget-окна.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImageReader  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
)
from rza_calc.domain.electrical import (  # noqa: E402
    DataConfirmation,
    ElectricalModel,
    LineKind,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor.connection_tool import (  # noqa: E402
    ConnectionTargetFeedback,
    ConnectionTargetKind,
)
from rza_calc.editor.controller import (  # noqa: E402
    NodeTarget,
    PhysicalLineInput,
    PortTarget,
    ProjectEditorController,
)
from rza_calc.editor.placement import EquipmentPlacementKind  # noqa: E402
from rza_calc.gui.editor_scene import EditorCanvas  # noqa: E402
from rza_calc.gui.theme import STYLESHEET  # noqa: E402


U10 = VoltageClassId("builtin.voltage.ac.10kv")
TAP_HINT = "Отпустите кнопку, чтобы создать отпайку"
INLINE_HINT = "Отпустите кнопку, чтобы вставить аппарат в линию"


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


class _EvidenceWindow(QMainWindow):
    def __init__(self, controller: ProjectEditorController):
        super().__init__()
        self.canvas = EditorCanvas(controller)
        self.banner = QLabel("Автоматизированная Windows/Qt-проверка")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setMinimumHeight(42)
        self.banner.setStyleSheet(
            "QLabel { background: #E8F1FF; color: #123A63; "
            "font-size: 16px; font-weight: 600; padding: 8px; "
            "border-bottom: 1px solid #9CBCE1; }"
        )
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.banner)
        layout.addWidget(self.canvas, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar(self))
        self.canvas.statusMessage.connect(self.show_message)
        self.canvas.errorOccurred.connect(
            lambda message: self.show_message(f"ОШИБКА: {message}")
        )
        self.resize(1440, 820)

    def show_message(self, message: str) -> None:
        self.banner.setText(message)
        self.statusBar().showMessage(message)


def _controller() -> tuple[ProjectEditorController, object]:
    page = DiagramPage(PageId("page.evidence.automatic-junctions"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins("Windows GUI: автоматические узлы"),
        DiagramDocument.create(
            "Автоматические узлы",
            (page,),
            document_id=DiagramDocumentId("diagram.evidence.automatic-junctions"),
        ),
        ProjectCatalogSnapshots(),
    )
    controller = ProjectEditorController(project)
    start = controller.add_electrical_node(
        "Начало ВЛ", x=100.0, y=200.0, voltage_class_id=U10
    )
    finish = controller.add_electrical_node(
        "Конец ВЛ", x=1000.0, y=200.0, voltage_class_id=U10
    )
    main = controller.create_physical_line(
        "ВЛ-10 кВ №1",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(finish.node_id),
        physical=PhysicalLineInput(
            10_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    return controller, main


def _show(window: _EvidenceWindow) -> None:
    window.setWindowTitle(
        "РЗА-Про — автоматизированная проверка автоматических узлов"
    )
    window.show()
    window.raise_()
    window.activateWindow()
    if not QTest.qWaitForWindowExposed(window, 5000):
        raise AssertionError("Окно EditorCanvas не стало видимым через Windows QPA.")
    QTest.qWait(300)
    window.canvas.view.set_zoom(1.0)
    window.canvas.view.centerOn(550.0, 340.0)
    QTest.qWait(200)


def _save(window: _EvidenceWindow, path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    QApplication.processEvents()
    QTest.qWait(120)
    pixmap = window.grab()
    if pixmap.isNull() or pixmap.width() < 1200 or pixmap.height() < 700:
        raise AssertionError(
            f"Некорректный кадр {path.name}: {pixmap.width()}x{pixmap.height()}"
        )
    if not pixmap.save(str(path), "PNG"):
        raise AssertionError(f"Qt не смог сохранить PNG: {path}")
    reader = QImageReader(str(path))
    if not reader.canRead():
        raise AssertionError(
            f"Сохранённый PNG не читается: {path}: {reader.errorString()}"
        )
    result = {
        "path": str(path.resolve()),
        "width": pixmap.width(),
        "height": pixmap.height(),
        "bytes": path.stat().st_size,
    }
    print(json.dumps(result, ensure_ascii=False))
    return result


def _assert_preview_did_not_mutate(
    controller: ProjectEditorController,
    *,
    fingerprint: str,
    revision: int,
    connectivity: tuple,
    journal: tuple,
) -> None:
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert controller.model.revision == revision
    assert controller.model.connectivity_signature() == connectivity
    assert tuple(controller.journal) == journal


def main() -> int:
    output = (
        ROOT
        / "docs"
        / "work-verification"
        / "automatic-junctions"
        / "screenshots"
    )
    app = QApplication.instance() or QApplication(
        ["automatic-junctions-windows-evidence"]
    )
    app.setApplicationName("РЗА-Про — автоматизированная Windows GUI-проверка")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)
    if QApplication.platformName().casefold() != "windows":
        raise SystemExit(
            "Ожидался Windows QPA, получено: " + QApplication.platformName()
        )

    controller, main_line = _controller()
    window = _EvidenceWindow(controller)
    records: list[dict[str, object]] = []
    try:
        _show(window)
        canvas = window.canvas
        scene = canvas.scene
        baseline_fingerprint = electrical_model_fingerprint(controller.model)
        baseline_revision = controller.model.revision
        baseline_connectivity = controller.model.connectivity_signature()
        baseline_journal = tuple(controller.journal)

        # 1. Preview будущей отпайки. Hit tolerance измеряется в экранных
        # пикселях: 8 px попадает, 10 px уже находится за пределом допуска.
        assert scene.begin_physical_line(
            name="Новая КЛ-отпайка",
            line_kind=LineKind.CABLE,
            scene_pos=QPointF(500.0, 500.0),
        )
        hit_results: dict[str, dict[str, bool]] = {}
        for zoom in (0.25, 1.0, 4.0):
            canvas.view.set_zoom(zoom)
            scene.update_physical_line_cursor(
                QPointF(500.0, 200.0 + 8.0 / zoom)
            )
            hit = scene._physical_line_tool.target
            inside = (
                hit is not None
                and hit.kind is ConnectionTargetKind.PHYSICAL_LINE
            )
            scene.update_physical_line_cursor(
                QPointF(500.0, 200.0 + 10.0 / zoom)
            )
            outside = scene._physical_line_tool.target is None
            assert inside and outside
            hit_results[str(zoom)] = {
                "8_screen_px_hits": inside,
                "10_screen_px_misses": outside,
            }

        canvas.view.set_zoom(1.0)
        canvas.view.centerOn(550.0, 340.0)
        scene.update_physical_line_cursor(QPointF(500.0, 200.0))
        target = scene._physical_line_tool.target
        assert target is not None
        assert target.kind is ConnectionTargetKind.PHYSICAL_LINE
        assert target.message == TAP_HINT
        assert scene._preview_item.isVisible()
        assert scene._preview_item._junction_orientation == "horizontal"
        route_item = next(iter(scene._route_items_by_id.values()))
        assert route_item._target_feedback is ConnectionTargetFeedback.COMPATIBLE
        window.show_message(TAP_HINT)
        _assert_preview_did_not_mutate(
            controller,
            fingerprint=baseline_fingerprint,
            revision=baseline_revision,
            connectivity=baseline_connectivity,
            journal=baseline_journal,
        )
        records.append(_save(window, output / "01-preview-auto-tap.png"))
        scene.cancel_physical_line()

        # 2. Preview последовательной вставки реклоузера через тот же реестр,
        # который используется обычным drag/drop библиотеки оборудования.
        payload = {
            "target_kind": "equipment",
            "type_id": "builtin.recloser",
            "type_version": 1,
            "name": "Реклоузер Р-1",
        }
        canvas.view.begin_placement(payload)
        canvas.view._cursor_scene_pos = QPointF(650.0, 200.0)
        canvas.view._update_equipment_drop_feedback(
            payload, QPointF(650.0, 200.0)
        )
        assert canvas.view._drop_feedback_kind is EquipmentPlacementKind.INLINE_SERIES
        assert canvas.view._drop_feedback_point == QPointF(650.0, 200.0)
        assert route_item._target_feedback is ConnectionTargetFeedback.COMPATIBLE
        window.show_message(INLINE_HINT)
        _assert_preview_did_not_mutate(
            controller,
            fingerprint=baseline_fingerprint,
            revision=baseline_revision,
            connectivity=baseline_connectivity,
            journal=baseline_journal,
        )
        canvas.view.viewport().update()
        records.append(_save(window, output / "02-preview-inline-recloser.png"))
        canvas.view.cancel_placement(announce=False)

        # 3. Commit только через публичные команды контроллера.
        load = controller.add_equipment(
            "builtin.load",
            "КТП-1",
            x=420.0,
            y=520.0,
            voltage_class_by_group={"main": U10},
        )
        tap = controller.create_tap(
            main_line.section_id,
            3_000_000,
            "КЛ к КТП-1",
            LineKind.CABLE,
            PortTarget(load.port_ids[0], load.representation_id),
            physical=PhysicalLineInput(
                800_000,
                DataConfirmation.CONFIRMED,
                {"r1_ohm_per_km": 0.31, "x1_ohm_per_km": 0.09},
                DataConfirmation.CONFIRMED,
            ),
            tap_x=420.0,
            tap_y=200.0,
        )
        before_recloser = electrical_model_fingerprint(controller.model)
        inserted = controller.insert_recloser(
            tap.second_section_id,
            2_000_000,
            "Реклоузер Р-1",
            properties={"rated_voltage_v": 10_000, "rated_current_a": 630},
            normal_position=SwitchPosition.CLOSED,
            x=680.0,
            y=200.0,
        )
        assert inserted.left_node_id != inserted.right_node_id
        assert inserted.left_node_id in controller.model.electrical_nodes
        assert inserted.right_node_id in controller.model.electrical_nodes
        assert controller.model.electrical_nodes[
            inserted.left_node_id
        ].extensions["junction_kind"] == "inline_device_left"
        assert controller.model.electrical_nodes[
            inserted.right_node_id
        ].extensions["junction_kind"] == "inline_device_right"
        assert len(controller.diagram.representations_for_node(tap.tap_node_id)) == 1
        # Terminal nodes существуют электрически, но не рисуются как лишние
        # самостоятельные точки возле символа последовательного аппарата.
        assert controller.diagram.representations_for_node(inserted.left_node_id) == ()
        assert controller.diagram.representations_for_node(inserted.right_node_id) == ()
        canvas.refresh(keep_selection=False)
        canvas.view.set_zoom(1.0)
        canvas.view.centerOn(550.0, 340.0)
        window.show_message(
            "Готово: видимая отпайка и реклоузер; два терминальных узла существуют в модели без лишних точек"
        )
        records.append(_save(window, output / "03-committed-tap-inline-recloser.png"))

        # 4. Ровно один undo отменяет последнюю атомарную вставку.
        controller.undo()
        assert inserted.recloser_id not in controller.model.equipment
        assert inserted.left_node_id not in controller.model.electrical_nodes
        assert inserted.right_node_id not in controller.model.electrical_nodes
        assert tap.tap_node_id in controller.model.electrical_nodes
        assert electrical_model_fingerprint(controller.model) == before_recloser
        canvas.refresh(keep_selection=False)
        canvas.view.set_zoom(1.0)
        canvas.view.centerOn(550.0, 340.0)
        window.show_message(
            "После отмены: реклоузер и два служебных узла удалены, отпайка и непрерывная ВЛ сохранены"
        )
        records.append(_save(window, output / "04-after-undo.png"))

        print(
            json.dumps(
                {
                    "platform": QApplication.platformName(),
                    "automated_not_manual": True,
                    "hit_tolerance": hit_results,
                    "preview_model_mutation": False,
                    "screenshots": records,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        window.close()
        QApplication.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
