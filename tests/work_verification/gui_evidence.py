# -*- coding: utf-8 -*-
"""Windows/Qt evidence harness for Safe Hardening GUI verification.

The harness uses the normal Windows QPA plugin, shows real QWidget windows,
waits for Qt event processing and captures the resulting windows.  It is an
automated diagnostic and must not be reported as a manual mouse acceptance.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
)
from rza_calc.domain.electrical import (
    DataConfirmation,
    ElectricalModel,
    LineKind,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor.controller import (
    NodeTarget,
    PhysicalLineInput,
    PortTarget,
    ProjectEditorController,
)
from rza_calc.gui.editor_panels import EditorWorkspaceWidget
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.topology import TopologyEngine


EXAMPLE = ROOT / "rza_calc" / "examples" / "gtes_sever.json"
U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller(token: str, title: str) -> ProjectEditorController:
    page = DiagramPage(PageId(f"page.evidence.{token}"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins(title),
        DiagramDocument.create(
            title,
            (page,),
            document_id=DiagramDocumentId(f"diagram.evidence.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def _physical(length_mm: int, *, r1: float = 0.4, x1: float = 0.3) -> PhysicalLineInput:
    return PhysicalLineInput(
        length_mm,
        DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": r1, "x1_ohm_per_km": x1},
        DataConfirmation.CONFIRMED,
    )


def _show_workspace(controller: ProjectEditorController, title: str) -> tuple[QMainWindow, EditorWorkspaceWidget]:
    window = QMainWindow()
    window.setObjectName("evidenceWindow")
    window.setWindowTitle(title)
    workspace = EditorWorkspaceWidget(controller)
    window.setCentralWidget(workspace)
    status = QStatusBar(window)
    status.showMessage("Автоматическая Windows/Qt-проверка: окно отображено штатным движком")
    window.setStatusBar(status)
    window.resize(1500, 850)
    window.show()
    window.raise_()
    window.activateWindow()
    QTest.qWaitForWindowExposed(window, 3000)
    QTest.qWait(300)
    workspace.canvas.view.fit_all()
    QTest.qWait(250)
    return window, workspace


def _save_window(window: QMainWindow, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    QApplication.processEvents()
    pixmap = window.grab()
    if pixmap.isNull() or pixmap.width() < 1000 or pixmap.height() < 600:
        raise AssertionError(
            f"Пустой или слишком маленький снимок: {pixmap.width()}x{pixmap.height()}"
        )
    if not pixmap.save(str(path), "PNG"):
        raise AssertionError(f"Qt не сохранил PNG: {path}")
    reader = QImageReader(str(path))
    if not reader.canRead():
        raise AssertionError(f"PNG невозможно повторно прочитать: {path}: {reader.errorString()}")
    print(f"PNG {path.name}: {pixmap.width()}x{pixmap.height()}, {path.stat().st_size} байт")


def _capture_main_window(output: Path) -> None:
    vm = ProjectViewModel.open(EXAMPLE)
    window = MainWindow(vm)
    try:
        window.resize(1500, 850)
        window.show()
        window.raise_()
        window.activateWindow()
        exposed = QTest.qWaitForWindowExposed(window, 3000)
        QTest.qWait(600)
        window.editor_workspace.canvas.view.fit_all()
        QTest.qWait(250)
        assert window.isVisible()
        assert window.workspace_tabs.tabText(0) == "Редактор схемы"
        assert window.workspace_tabs.tabText(1) == "Анализ и расчёты"
        _save_window(window, output / "01-normal-main-window-editor.png")
        window.workspace_tabs.setCurrentIndex(1)
        QTest.qWait(1200)
        _save_window(window, output / "01b-normal-main-window-analysis.png")
        print(f"Обычное окно: exposed={bool(exposed)}, platform={QApplication.platformName()}")
    finally:
        window.close()
        QApplication.processEvents()


def _build_tap_network() -> tuple[ProjectEditorController, object, object, object]:
    controller = _controller("taps", "Отпайки и реклоузеры — доказательный сценарий")

    source_1 = controller.add_equipment(
        "builtin.external_grid",
        "Источник 1",
        x=40.0,
        y=220.0,
        voltage_class_by_group={"main": U10},
    )
    bus = controller.add_electrical_node(
        "Шины 10 кВ",
        x=180.0,
        y=220.0,
        voltage_class_id=U10,
        symbol_key="busbar_horizontal",
        width=170.0,
        height=22.0,
    )
    breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-1",
        x=310.0,
        y=220.0,
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    line_start = controller.add_electrical_node(
        "Начало ВЛ",
        x=400.0,
        y=220.0,
        voltage_class_id=U10,
    )
    far_end = controller.add_electrical_node(
        "Конец магистрали",
        x=1260.0,
        y=220.0,
        voltage_class_id=U10,
    )
    controller.connect_port_to_node(source_1.port_ids[0], bus.node_id)
    controller.connect_port_to_node(breaker.port_ids[0], bus.node_id)
    controller.connect_port_to_node(breaker.port_ids[1], line_start.node_id)
    main = controller.create_physical_line(
        "ВЛ-10 кВ №1",
        LineKind.OVERHEAD,
        NodeTarget(line_start.node_id),
        NodeTarget(far_end.node_id),
        physical=_physical(12_000_000),
    )

    loads = tuple(
        controller.add_equipment(
            "builtin.load",
            name,
            x=x,
            y=600.0,
            voltage_class_by_group={"main": U10},
        )
        for name, x in (("КТП-1", 500.0), ("КТП-2", 820.0), ("КТП-3", 1080.0))
    )
    first_tap = controller.create_tap(
        main.section_id,
        2_000_000,
        "Отпайка к КТП-1",
        LineKind.CABLE,
        PortTarget(loads[0].port_ids[0], loads[0].representation_id),
        physical=_physical(650_000, r1=0.31, x1=0.09),
        tap_x=500.0,
        tap_y=220.0,
    )
    main_recloser = controller.insert_recloser(
        first_tap.second_section_id,
        2_000_000,
        "Реклоузер Р-магистраль",
        properties={"rated_voltage_v": 10_000, "rated_current_a": 630},
        x=670.0,
        y=220.0,
    )
    second_tap = controller.create_tap(
        main_recloser.right_section_id,
        2_000_000,
        "Отпайка к КТП-2",
        LineKind.CABLE,
        PortTarget(loads[1].port_ids[0], loads[1].representation_id),
        physical=_physical(800_000, r1=0.32, x1=0.09),
        tap_x=820.0,
        tap_y=220.0,
    )
    third_tap = controller.create_tap(
        second_tap.second_section_id,
        2_000_000,
        "Отпайка к КТП-3",
        LineKind.CABLE,
        PortTarget(loads[2].port_ids[0], loads[2].representation_id),
        physical=_physical(900_000, r1=0.33, x1=0.09),
        tap_x=1080.0,
        tap_y=220.0,
    )

    nested_load = controller.add_equipment(
        "builtin.load",
        "КТП-1А (отпайка от отпайки)",
        x=690.0,
        y=730.0,
        voltage_class_by_group={"main": U10},
    )
    branch_recloser = controller.insert_recloser(
        first_tap.branch_section_id,
        300_000,
        "Реклоузер Р-отпайка",
        properties={"rated_voltage_v": 10_000, "rated_current_a": 400},
        x=500.0,
        y=400.0,
    )
    nested_tap = controller.create_tap(
        branch_recloser.right_section_id,
        150_000,
        "Вложенная отпайка",
        LineKind.CABLE,
        PortTarget(nested_load.port_ids[0], nested_load.representation_id),
        physical=_physical(300_000, r1=0.38, x1=0.1),
        tap_x=500.0,
        tap_y=500.0,
    )

    source_2 = controller.add_equipment(
        "builtin.external_grid",
        "Источник 2 (резерв)",
        x=1510.0,
        y=220.0,
        voltage_class_by_group={"main": U10},
    )
    backup_breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-резерв",
        x=1380.0,
        y=220.0,
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    controller.connect_ports(
        source_2.port_ids[0],
        backup_breaker.port_ids[0],
        first_representation_id=source_2.representation_id,
        second_representation_id=backup_breaker.representation_id,
    )
    controller.connect_port_to_node(
        backup_breaker.port_ids[1],
        far_end.node_id,
        source_representation_id=backup_breaker.representation_id,
    )

    assert main_recloser.recloser_id in controller.model.equipment
    assert branch_recloser.recloser_id in controller.model.equipment
    assert nested_tap.tap_node_id in controller.model.electrical_nodes
    assert third_tap.tap_node_id in controller.model.electrical_nodes
    return controller, main_recloser, branch_recloser, far_end


def _capture_taps_and_reclosers(output: Path) -> None:
    controller, main_recloser, branch_recloser, far_end = _build_tap_network()
    window, workspace = _show_workspace(
        controller,
        "РЗА-Про — отпайки, реклоузеры и альтернативное питание",
    )
    try:
        item = workspace.scene._items_by_id.get(main_recloser.representation_id)
        assert item is not None
        assert item._switch_open is False
        item.setSelected(True)
        QTest.qWait(150)
        _save_window(window, output / "02-taps-reclosers-closed.png")
        workspace.canvas.view.set_zoom(1.5)
        workspace.canvas.view.centerOn(item)
        QTest.qWait(150)
        _save_window(window, output / "02b-recloser-closed-closeup.png")

        state_id = controller.switch_equipment(
            main_recloser.recloser_id,
            SwitchPosition.OPEN,
            confirmed=True,
        )
        workspace.refresh()
        workspace.canvas.view.fit_all()
        QTest.qWait(300)
        open_item = workspace.scene._items_by_id.get(main_recloser.representation_id)
        assert open_item is not None
        assert open_item._switch_open is True
        topology = TopologyEngine().compile(controller.model, state_id)
        assert not topology.has_path(main_recloser.left_node_id, main_recloser.right_node_id)
        assert topology.is_energized(main_recloser.left_node_id)
        assert topology.is_energized(main_recloser.right_node_id)
        assert topology.is_energized(far_end.node_id)
        position = controller.effective_switch_position(main_recloser.recloser_id, state_id)
        assert position is SwitchPosition.OPEN
        window.statusBar().showMessage(
            "Реклоузер Р-магистраль отключён; правая часть остаётся запитанной от Источника 2"
        )
        _save_window(window, output / "03-recloser-open-alternative-supply.png")
        workspace.canvas.view.set_zoom(1.5)
        workspace.canvas.view.centerOn(open_item)
        QTest.qWait(150)
        _save_window(window, output / "03b-recloser-open-closeup.png")
        print(
            "Топология: магистральный реклоузер разомкнут, обе стороны запитаны "
            "от разных источников; реклоузер на отпайке сохранён."
        )
    finally:
        window.close()
        QApplication.processEvents()


def _capture_crossing(output: Path) -> None:
    controller = _controller("crossing", "Пересечение без электрического соединения")
    left = controller.add_electrical_node(
        "Узел A", x=250.0, y=380.0, voltage_class_id=U10
    )
    right = controller.add_electrical_node(
        "Узел B", x=1150.0, y=380.0, voltage_class_id=U10
    )
    top = controller.add_electrical_node(
        "Узел C", x=700.0, y=100.0, voltage_class_id=U10
    )
    bottom = controller.add_electrical_node(
        "Узел D", x=700.0, y=680.0, voltage_class_id=U10
    )
    controller.create_physical_line(
        "ВЛ A—B (горизонтальная)",
        LineKind.OVERHEAD,
        NodeTarget(left.node_id),
        NodeTarget(right.node_id),
        physical=_physical(1_000_000),
    )
    controller.create_physical_line(
        "КЛ C—D (вертикальная)",
        LineKind.CABLE,
        NodeTarget(top.node_id),
        NodeTarget(bottom.node_id),
        physical=_physical(1_000_000, r1=0.25, x1=0.08),
    )
    topology = TopologyEngine().compile(controller.model)
    assert topology.has_path(left.node_id, right.node_id)
    assert topology.has_path(top.node_id, bottom.node_id)
    assert not topology.has_path(left.node_id, top.node_id)
    assert len(controller.model.electrical_nodes) == 4

    window, workspace = _show_workspace(
        controller,
        "РЗА-Про — графическое пересечение не создаёт электрическую связь",
    )
    try:
        window.statusBar().showMessage(
            "Две линии пересекаются только графически: в центре нет электрического узла"
        )
        _save_window(window, output / "04-crossing-without-connection.png")
        print("Пересечение: четыре узла, две независимые электрические компоненты.")
    finally:
        window.close()
        QApplication.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "work-verification" / "screenshots",
    )
    args = parser.parse_args()

    app = QApplication.instance() or QApplication(["safe-hardening-gui-evidence"])
    app.setApplicationName("РЗА-Про — автоматическая GUI-проверка")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    if QApplication.platformName().casefold() != "windows":
        raise SystemExit(
            "Для доказательного прогона требуется QT_QPA_PLATFORM=windows, "
            f"получено: {QApplication.platformName()}"
        )
    app.setQuitOnLastWindowClosed(False)

    output = args.output.resolve()
    _capture_main_window(output)
    _capture_taps_and_reclosers(output)
    _capture_crossing(output)
    print(f"Готово: 7 автоматических PNG в {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
