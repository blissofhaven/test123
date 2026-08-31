# -*- coding: utf-8 -*-
"""Автоматизированная Windows/Qt-приёмка взаимодействия UI-UX-1.

Harness открывает настоящие QWidget-окна через Windows QPA, отправляет
воспроизводимые события мыши/клавиатуры QTest и сохраняет PNG. Это не
физическое испытание рукой пользователя и в отчёте так не называется.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImageReader  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar  # noqa: E402

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
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor import EditorTool, ProjectEditorController  # noqa: E402
from rza_calc.editor.controller import NodeTarget, PhysicalLineInput  # noqa: E402
from rza_calc.gui.editor_panels import (  # noqa: E402
    EditorWorkspaceWidget,
    EquipmentLibraryTree,
)
from rza_calc.gui.theme import STYLESHEET  # noqa: E402
from rza_calc.topology import TopologyEngine  # noqa: E402


U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller(token: str, title: str) -> ProjectEditorController:
    page = DiagramPage(PageId(f"page.evidence.ui.{token}"), "Основная схема")
    return ProjectEditorController(_Project(
        ElectricalModel.with_builtins(title),
        DiagramDocument.create(
            title,
            (page,),
            document_id=DiagramDocumentId(f"diagram.evidence.ui.{token}"),
        ),
        ProjectCatalogSnapshots(),
    ))


def _window(controller: ProjectEditorController, title: str):
    window = QMainWindow()
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    window.setCentralWidget(workspace)
    window.setStatusBar(QStatusBar(window))
    workspace.statusMessage.connect(window.statusBar().showMessage)
    window.setWindowTitle(title)
    window.resize(1500, 850)
    window.show()
    window.raise_()
    window.activateWindow()
    if not QTest.qWaitForWindowExposed(window, 5000):
        raise AssertionError("Окно UI-UX-1 не стало видимым через Windows QPA.")
    QTest.qWait(250)
    workspace.canvas.view.actual_size()
    workspace.canvas.view.centerOn(400.0, 300.0)
    return window, workspace


def _save(window: QMainWindow, path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    QApplication.processEvents()
    QTest.qWait(100)
    pixmap = window.grab()
    if pixmap.isNull() or pixmap.width() < 1200 or pixmap.height() < 700:
        raise AssertionError(f"Некорректный кадр {path.name}.")
    if not pixmap.save(str(path), "PNG"):
        raise AssertionError(f"Не удалось сохранить {path}.")
    if not QImageReader(str(path)).canRead():
        raise AssertionError(f"Qt не может повторно прочитать {path}.")
    return {
        "file": path.name,
        "width": pixmap.width(),
        "height": pixmap.height(),
        "bytes": path.stat().st_size,
    }


def _click_scene(workspace: EditorWorkspaceWidget, x: float, y: float, *, double=False):
    view = workspace.canvas.view
    pos = view.mapFromScene(QPointF(x, y))
    action = QTest.mouseDClick if double else QTest.mouseClick
    action(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    QApplication.processEvents()


def _library_item(workspace: EditorWorkspaceWidget, title: str):
    tree = workspace.side_panel.library

    def visit(item):
        if item.text(0) == title and isinstance(
            item.data(0, EquipmentLibraryTree.PAYLOAD_ROLE), dict
        ):
            return item
        for index in range(item.childCount()):
            found = visit(item.child(index))
            if found is not None:
                return found
        return None

    for index in range(tree.topLevelItemCount()):
        found = visit(tree.topLevelItem(index))
        if found is not None:
            return found
    raise AssertionError(f"Нет элемента библиотеки «{title}».")


def _click_library(workspace: EditorWorkspaceWidget, title: str) -> None:
    tree = workspace.side_panel.library
    item = _library_item(workspace, title)
    tree.scrollToItem(item)
    QApplication.processEvents()
    QTest.mouseClick(
        tree.viewport(),
        Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(item).center(),
    )
    QApplication.processEvents()


def _physical() -> PhysicalLineInput:
    return PhysicalLineInput(
        1_000_000,
        DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
        DataConfirmation.CONFIRMED,
    )


def main() -> int:
    output = ROOT / "docs" / "work-verification" / "ui-interaction" / "screenshots"
    app = QApplication.instance() or QApplication(["ui-ux-1-windows-evidence"])
    app.setApplicationName("РЗА-Про — проверка UI-UX-1")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)
    if QApplication.platformName().casefold() != "windows":
        raise SystemExit("Для проверки требуется Windows QPA.")

    records: list[dict[str, object]] = []
    results: dict[str, object] = {
        "platform": QApplication.platformName(),
        "automated_qtest": True,
        "physical_mouse_acceptance": False,
    }

    # 1–2. Выбор и двойной щелчок.
    controller = _controller("selection", "Выбор и свойства")
    controller.add_equipment("builtin.circuit_breaker", "QF1", x=150, y=180)
    transformer = controller.add_equipment(
        "builtin.transformer_2w", "Т1", x=420, y=180
    )
    window, workspace = _window(controller, "UI-UX-1 — выбор и свойства")
    try:
        _click_scene(workspace, 150, 180)
        _click_scene(workspace, 420, 180)
        assert workspace._selected_ids == (transformer.representation_id,)
        assert workspace.inspector.title.text().startswith("Выбрано: Т1")
        records.append(_save(window, output / "01-selection-transformer.png"))
        _click_scene(workspace, 420, 180, double=True)
        assert workspace._selected_ids == (transformer.representation_id,)
        assert workspace.inspector.tree.hasFocus()
        records.append(_save(window, output / "02-double-click-properties.png"))
        results["selection_and_double_click"] = "passed"
    finally:
        window.close()

    # 3. Одноразовая вставка.
    controller = _controller("once", "Одноразовая вставка")
    window, workspace = _window(controller, "UI-UX-1 — одноразовая вставка")
    try:
        _click_library(workspace, "Выключатель")
        assert workspace.canvas.view.tool_state.tool is EditorTool.PLACE_EQUIPMENT_ONCE
        _click_scene(workspace, 120, 180)
        for x in (280, 420, 560, 700, 840):
            _click_scene(workspace, x, 300)
        assert len(controller.model.equipment) == 1
        assert workspace.canvas.view.tool_state.tool is EditorTool.SELECT
        workspace.canvas.view.fit_all()
        records.append(_save(window, output / "03-one-shot-placement.png"))
        results["one_shot"] = "passed: 1 object after 6 canvas clicks"
    finally:
        window.close()

    # 4. Явная многократная вставка.
    controller = _controller("repeat", "Многократная вставка")
    window, workspace = _window(controller, "UI-UX-1 — многократная вставка")
    try:
        workspace.command_bar.repeat_placement_action.setChecked(True)
        _click_library(workspace, "Нагрузка")
        for x in (120, 300, 480):
            _click_scene(workspace, x, 180)
        QTest.keyClick(workspace.canvas.view, Qt.Key.Key_Escape)
        _click_scene(workspace, 660, 180)
        assert len(controller.model.equipment) == 3
        workspace.canvas.view.fit_all()
        records.append(_save(window, output / "04-explicit-repeat-placement.png"))
        results["repeat"] = "passed: 3 objects, Esc blocks fourth"
    finally:
        window.close()

    # 5. Дискретное вращение и смысловые порты.
    controller = _controller("rotation", "Поворот и смысловые порты")
    breaker = controller.add_equipment("builtin.circuit_breaker", "QF1", x=160, y=200)
    transformer = controller.add_equipment(
        "builtin.transformer_2w", "Т1", x=480, y=200
    )
    window, workspace = _window(controller, "UI-UX-1 — поворот 90°")
    try:
        before = electrical_model_fingerprint(controller.model)
        transformer_ports = tuple(
            controller.model.port_definition(item).role
            for item in controller.model.equipment[transformer.equipment_id].port_ids
        )
        workspace.scene.select_representations((breaker.representation_id,))
        for _ in range(4):
            QTest.keyClick(workspace.canvas.view, Qt.Key.Key_R)
        workspace.scene.select_representations((transformer.representation_id,))
        QTest.keyClick(workspace.canvas.view, Qt.Key.Key_R)
        assert controller.diagram.representations[breaker.representation_id].rotation_deg == 0
        assert controller.diagram.representations[transformer.representation_id].rotation_deg == 90
        assert transformer_ports == ("hv", "lv")
        assert electrical_model_fingerprint(controller.model) == before
        workspace.canvas.view.fit_all()
        records.append(_save(window, output / "05-rotation-semantic-ports.png"))
        results["rotation"] = "passed: 0/90/180/270, HV/LV unchanged"
    finally:
        window.close()

    # 6. Автоориентация реклоузера по горизонтальной и вертикальной линии.
    controller = _controller("auto", "Автоматическая ориентация")
    a = controller.add_electrical_node("A", x=100, y=150, voltage_class_id=U10)
    b = controller.add_electrical_node("B", x=550, y=150, voltage_class_id=U10)
    c = controller.add_electrical_node("C", x=800, y=80, voltage_class_id=U10)
    d = controller.add_electrical_node("D", x=800, y=560, voltage_class_id=U10)
    horizontal = controller.create_physical_line(
        "Горизонтальная ВЛ", LineKind.OVERHEAD,
        NodeTarget(a.node_id), NodeTarget(b.node_id), physical=_physical(),
    )
    vertical = controller.create_physical_line(
        "Вертикальная ВЛ", LineKind.OVERHEAD,
        NodeTarget(c.node_id), NodeTarget(d.node_id), physical=_physical(),
    )
    first = controller.insert_recloser(
        horizontal.section_id, 500_000, "Реклоузер Г", x=325, y=150
    )
    second = controller.insert_recloser(
        vertical.section_id, 500_000, "Реклоузер В", x=800, y=320
    )
    assert controller.diagram.representations[first.representation_id].rotation_deg == 0
    assert controller.diagram.representations[second.representation_id].rotation_deg == 90
    window, workspace = _window(controller, "UI-UX-1 — автоориентация реклоузеров")
    try:
        workspace.canvas.view.fit_all()
        records.append(_save(window, output / "06-auto-orientation-lines.png"))
        results["auto_orientation"] = "passed: horizontal=0, vertical=90"
    finally:
        window.close()

    # 7. Красный preview коллизии без Domain-объекта.
    controller = _controller("collision", "Предотвращение наложений")
    controller.add_equipment("builtin.transformer_2w", "Т1", x=300, y=250)
    window, workspace = _window(controller, "UI-UX-1 — недопустимое наложение")
    try:
        workspace.canvas.view.begin_placement({
            "target_kind": "equipment",
            "type_id": "builtin.transformer_2w",
            "name": "Т2",
        })
        view = workspace.canvas.view
        # На Windows QTest не посылает второй move, если системный курсор уже
        # оказался в той же глобальной точке после закрытия предыдущего окна.
        QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(40, 40)))
        point = view.mapFromScene(QPointF(300, 250))
        QTest.mouseMove(view.viewport(), point, delay=50)
        QTest.qWait(100)
        assert workspace.canvas.view._placement_valid is False
        records.append(_save(window, output / "07-invalid-collision-preview.png"))
        _click_scene(workspace, 300, 250)
        assert len(controller.model.equipment) == 1
        results["collision"] = "passed: red preview, commit rejected"
    finally:
        window.close()

    # 8. Пересечение без электрического узла.
    controller = _controller("crossing", "Пересечение линий")
    left = controller.add_electrical_node("Лево", x=120, y=300, voltage_class_id=U10)
    right = controller.add_electrical_node("Право", x=760, y=300, voltage_class_id=U10)
    top = controller.add_electrical_node("Верх", x=440, y=80, voltage_class_id=U10)
    bottom = controller.add_electrical_node("Низ", x=440, y=560, voltage_class_id=U10)
    controller.create_physical_line(
        "Линия 1", LineKind.OVERHEAD,
        NodeTarget(left.node_id), NodeTarget(right.node_id), physical=_physical(),
    )
    controller.create_physical_line(
        "Линия 2", LineKind.CABLE,
        NodeTarget(top.node_id), NodeTarget(bottom.node_id), physical=_physical(),
    )
    topology = TopologyEngine().compile(controller.model)
    assert not topology.has_path(left.node_id, top.node_id)
    window, workspace = _window(controller, "UI-UX-1 — пересечение без соединения")
    try:
        workspace.canvas.view.fit_all()
        records.append(_save(window, output / "08-crossing-without-node.png"))
        results["crossing"] = "passed: no electrical path"
    finally:
        window.close()

    results["screenshots"] = records
    summary = output.parent / "windows-qtest-results.json"
    summary.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(results, ensure_ascii=False))
    print(f"Готово: {len(records)} PNG; результат: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
