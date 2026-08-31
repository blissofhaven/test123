"""Offscreen evidence from the real Qt scene and application, never a mock UI.

The examples are loaded read-only. Synthetic equipment is created exclusively
in an in-memory diagnostic project. PNG/JSON output is restricted to the new
docs/work-verification/VisualMerge-Sol-20260831/visio-style directory. This is automated render evidence,
not manual mouse acceptance or a new electrical calculation method.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramDocumentId, DiagramPage, PageId
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, LineKind, SwitchPosition, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import NodeTarget, PhysicalLineInput, ProjectEditorController
from rza_calc.editor.symbols import DiagramColorMode
from rza_calc.gui.editor_scene import CanvasMode, DiagramGraphicsScene
from rza_calc.gui.theme import STYLESHEET
from rza_calc.io.project import load_project
from rza_calc.topology import TopologyEngine

OUTPUT = ROOT / "docs" / "work-verification" / "VisualMerge-Sol-20260831" / "visio-style"
EXAMPLE = ROOT / "rza_calc" / "examples" / "energoraion.json"


@dataclass
class EvidenceProject:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def controller_for(title: str) -> ProjectEditorController:
    page = DiagramPage(PageId("page.evidence.visio"), "Однолинейная схема")
    project = EvidenceProject(
        ElectricalModel.with_builtins(title),
        DiagramDocument.create(title, (page,), document_id=DiagramDocumentId("diagram.evidence.visio")),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def model_signature(model, document) -> tuple:
    """Snapshot includes electrical IDs, connectivity and the entire diagram."""
    return (
        electrical_model_fingerprint(model), model.revision,
        model.connectivity_signature(), tuple(model.equipment), tuple(model.ports),
        tuple(model.electrical_nodes), tuple(model.connections),
        repr(document),
    )


def render_scene(scene: DiagramGraphicsScene, path: Path, *, source: QRectF | None = None,
                 width: int = 1800, height: int = 1100) -> dict:
    QApplication.processEvents()
    if source is None:
        visible_bounds = QRectF()
        for item in scene.items():
            if item.isVisible() and item.opacity() > 0:
                visible_bounds = visible_bounds.united(item.sceneBoundingRect())
        source = visible_bounds.adjusted(-35, -35, 35, 35)
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        scene.render(painter, QRectF(20, 20, width-40, height-40), source,
                     Qt.AspectRatioMode.KeepAspectRatio)
    finally:
        painter.end()
    assert image.save(str(path), "PNG"), path
    assert not QImage(str(path)).isNull()
    return {"file": path.name, "width": width, "height": height,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _scene(controller, mode=CanvasMode.EDIT) -> DiagramGraphicsScene:
    scene = DiagramGraphicsScene()
    scene.set_mode(mode)
    scene.set_grid(visible=False)
    scene.sync_document(controller.diagram, controller.model)
    return scene


def palette_fixture() -> ProjectEditorController:
    """Six exact nominal classes; on/off for powered/unpowered circuits."""
    controller = controller_for("Visio: класс напряжения и положение независимы")
    voltage_rows = (("220kv", "220 кВ"), ("110kv", "110 кВ"), ("35kv", "35 кВ"),
                    ("10kv", "10 кВ"), ("6kv", "6 кВ"), ("0_4kv", "0,4 кВ"))
    for row, (key, name) in enumerate(voltage_rows):
        voltage = VoltageClassId("builtin.voltage.ac." + key)
        y = 100.0 + row*145.0
        for column, (position, powered) in enumerate(((SwitchPosition.CLOSED, True),
                (SwitchPosition.OPEN, True), (SwitchPosition.CLOSED, False), (SwitchPosition.OPEN, False))):
            x = 210.0 + column*325.0
            left = controller.add_electrical_node("A", x=x-55, y=y, voltage_class_id=voltage)
            right = controller.add_electrical_node("B", x=x+55, y=y, voltage_class_id=voltage)
            switch = controller.add_equipment("builtin.circuit_breaker", f"QF · {name}",
                x=x, y=y, width=80, height=50, voltage_class_by_group={"main": voltage},
                normal_position=position)
            controller.connect_port_to_node(switch.port_ids[0], left.node_id,
                                            source_representation_id=switch.representation_id)
            controller.connect_port_to_node(switch.port_ids[1], right.node_id,
                                            source_representation_id=switch.representation_id)
            if powered:
                source = controller.add_equipment("builtin.external_grid", "Сеть",
                    x=x-130, y=y, width=45, height=40, voltage_class_by_group={"main": voltage})
                controller.connect_port_to_node(source.port_ids[0], left.node_id,
                                                source_representation_id=source.representation_id)
    return controller


def crossing_fixture() -> tuple[ProjectEditorController, tuple]:
    controller = controller_for("Пересечение без соединения")
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    nodes = tuple(controller.add_electrical_node(name, x=x, y=y, voltage_class_id=voltage)
                  for name, x, y in (("A", 100, 300), ("B", 800, 300),
                                     ("C", 450, 80), ("D", 450, 520)))
    physical = PhysicalLineInput(1_000_000, DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3}, DataConfirmation.CONFIRMED)
    for first, second, name in ((nodes[0], nodes[1], "Линия A—B"), (nodes[2], nodes[3], "Линия C—D")):
        controller.create_physical_line(name, LineKind.OVERHEAD, NodeTarget(first.node_id),
                                         NodeTarget(second.node_id), physical=physical)
    topology = TopologyEngine().compile(controller.model)
    assert topology.has_path(nodes[0].node_id, nodes[1].node_id)
    assert topology.has_path(nodes[2].node_id, nodes[3].node_id)
    assert not topology.has_path(nodes[0].node_id, nodes[2].node_id)
    return controller, nodes


def rotation_fixture() -> ProjectEditorController:
    controller = controller_for("Положение при четырёх поворотах")
    voltage = VoltageClassId("builtin.voltage.ac.110kv")
    for row, position in enumerate((SwitchPosition.CLOSED, SwitchPosition.OPEN)):
        for column, angle in enumerate((0, 90, 180, 270)):
            x, y = 150 + column*300, 150 + row*280
            dx, dy = 80*round(math.cos(math.radians(angle))), 80*round(math.sin(math.radians(angle)))
            left = controller.add_electrical_node("A", x=x-dx, y=y-dy, voltage_class_id=voltage)
            right = controller.add_electrical_node("B", x=x+dx, y=y+dy, voltage_class_id=voltage)
            switch = controller.add_equipment("builtin.circuit_breaker", f"QF · {angle}°",
                x=x, y=y, width=120, height=70, rotation_deg=angle,
                voltage_class_by_group={"main": voltage}, normal_position=position)
            controller.connect_port_to_node(switch.port_ids[0], left.node_id,
                source_representation_id=switch.representation_id)
            controller.connect_port_to_node(switch.port_ids[1], right.node_id,
                source_representation_id=switch.representation_id)
    return controller


def bus_bridge_fixture() -> tuple[ProjectEditorController, tuple]:
    """Independent branch crosses each bus; another branch actually joins it."""
    controller = controller_for("Два направления мостика и реальные присоединения")
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    horizontal = controller.add_electrical_node("Шины 10 кВ · горизонтальные", x=300, y=220,
        voltage_class_id=voltage, symbol_key="busbar_horizontal", width=420, height=22)
    vertical = controller.add_electrical_node("Шины 10 кВ · вертикальные", x=850, y=220,
        voltage_class_id=voltage, symbol_key="busbar_vertical", width=22, height=420)
    physical = PhysicalLineInput(1_000_000, DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3}, DataConfirmation.CONFIRMED)
    for index, (coordinates, bus, load_at) in enumerate((
        (((400, 40), (400, 440)), horizontal, (160, 430)),
        (((650, 340), (1060, 340)), vertical, (1100, 100)),
    )):
        nodes = tuple(controller.add_electrical_node(f"{index+1}{name}", x=x, y=y,
                      voltage_class_id=voltage) for name, (x,y) in zip(("A", "B"), coordinates))
        controller.create_physical_line(f"Независимая линия {index+1}", LineKind.OVERHEAD,
            NodeTarget(nodes[0].node_id), NodeTarget(nodes[1].node_id), physical=physical)
        load = controller.add_equipment("builtin.load", f"Присоединение {index+1}",
            x=load_at[0], y=load_at[1], voltage_class_by_group={"main": voltage})
        controller.connect_port_to_node(load.port_ids[0], bus.node_id,
            source_representation_id=load.representation_id, node_representation_id=bus.representation_id)
    return controller, (horizontal, vertical)


def capture_main_window(records: list[dict]) -> dict:
    from rza_calc.gui.main_window import MainWindow
    from rza_calc.gui.view_model import ProjectViewModel
    vm = ProjectViewModel.open(EXAMPLE)
    initial = model_signature(vm.project.electrical_model, vm.project.diagram)
    window = MainWindow(vm)
    try:
        window.resize(1800, 1100)
        window.show()
        QApplication.processEvents()
        window.editor_workspace.canvas.view.fit_all()
        QApplication.processEvents()
        # fit_all intentionally records camera zoom/pan in workspace metadata.
        # Compare painting/tab selection against the already framed document.
        baseline = model_signature(vm.project.electrical_model, vm.project.diagram)
        assert baseline[:-1] == initial[:-1]
        for index, label in ((0, "editor"), (1, "analysis")):
            window.workspace_tabs.setCurrentIndex(index)
            QApplication.processEvents()
            path = OUTPUT / f"05-main-window-{label}.png"
            pixmap = window.grab()
            assert not pixmap.isNull() and pixmap.save(str(path), "PNG")
            records.append({"file": path.name, "width": pixmap.width(), "height": pixmap.height(),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        after = model_signature(vm.project.electrical_model, vm.project.diagram)
        if after != baseline:
            labels = ("fingerprint", "revision", "connectivity", "equipment_ids", "port_ids",
                      "node_ids", "connection_ids", "diagram")
            changed = [name for name, old, new in zip(labels, baseline, after) if old != new]
            raise AssertionError("MainWindow read-only smoke changed: " + ", ".join(changed))
        window.workspace_tabs.setCurrentIndex(0)
        canvas = window.editor_workspace.canvas
        target = next((item for item in canvas.scene._items_by_id.values()
                       if item._canonical_key == "circuit_breaker"), None)
        if target is None:
            target = next(item for item in canvas.scene._items_by_id.values()
                          if item._canonical_key == "transformer_2w")
        canvas.view.set_zoom(1.5)
        canvas.view.centerOn(target)
        QApplication.processEvents()
        closeup_baseline = model_signature(vm.project.electrical_model, vm.project.diagram)
        assert closeup_baseline[:-1] == initial[:-1]
        path = OUTPUT / "05-main-window-editor-closeup.png"
        pixmap = window.grab()
        assert not pixmap.isNull() and pixmap.save(str(path), "PNG")
        records.append({"file": path.name, "width": pixmap.width(), "height": pixmap.height(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        assert model_signature(vm.project.electrical_model, vm.project.diagram) == closeup_baseline
        return {"framing_updates_camera_metadata_in_memory": baseline[-1] != initial[-1],
                "painting_and_tab_selection_changed_document": False,
                "electrical_model_changed": False}
    finally:
        window.close()
        QApplication.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-main-window", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(["visio-style-evidence"])
    # The Windows offscreen QPA does not enumerate installed fonts. Register
    # local Segoe files explicitly so evidence contains readable Cyrillic.
    for filename in ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "arial.ttf", "arialbd.ttf"):
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
        if font_path.is_file():
            assert QFontDatabase.addApplicationFont(str(font_path)) >= 0
    app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)
    assert QApplication.platformName().casefold() == "offscreen"
    example_hash = hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
    records: list[dict] = []

    project = load_project(EXAMPLE)
    baseline = model_signature(project.electrical_model, project.diagram)
    for mode in (CanvasMode.EDIT, CanvasMode.ANALYSIS):
        scene = DiagramGraphicsScene()
        scene.set_mode(mode)
        scene.set_grid(visible=False)
        scene.sync_document(project.diagram, project.electrical_model)
        records.append(render_scene(scene, OUTPUT / f"01-real-demo-{mode.value}.png"))
    assert model_signature(project.electrical_model, project.diagram) == baseline

    controller = palette_fixture()
    baseline = model_signature(controller.model, controller.diagram)
    scene = _scene(controller)
    headers = ("Включён · питание есть", "Отключён · источник слева",
               "Включён · без питания", "Отключён · без питания")
    for column, title in enumerate(headers):
        label = scene.addText(title)
        label.setDefaultTextColor(QColor("#111827"))
        label.setPos(90 + column*325, 5)
    for color in (DiagramColorMode.COLOR, DiagramColorMode.MONOCHROME):
        scene.set_color_mode(color)
        records.append(render_scene(scene, OUTPUT / f"02-voltage-state-{color.value}.png"))
    assert model_signature(controller.model, controller.diagram) == baseline

    crossing, nodes = crossing_fixture()
    baseline = model_signature(crossing.model, crossing.diagram)
    scene = _scene(crossing, CanvasMode.ANALYSIS)
    records.append(render_scene(scene, OUTPUT / "03-crossing-no-node.png", width=1600, height=950))
    assert len(crossing.model.electrical_nodes) == 4
    assert model_signature(crossing.model, crossing.diagram) == baseline

    rotated = rotation_fixture()
    baseline = model_signature(rotated.model, rotated.diagram)
    records.append(render_scene(_scene(rotated), OUTPUT / "04-switch-rotation-closeup.png", width=1800, height=1000))
    unknown = _scene(rotated, CanvasMode.ANALYSIS)
    unknown.sync_document(rotated.diagram, rotated.model, topology_state_available=False)
    records.append(render_scene(unknown, OUTPUT / "04-switch-unknown-state.png", width=1800, height=1000))
    assert model_signature(rotated.model, rotated.diagram) == baseline

    buses, _ = bus_bridge_fixture()
    baseline = model_signature(buses.model, buses.diagram)
    records.append(render_scene(_scene(buses, CanvasMode.ANALYSIS), OUTPUT / "04-bus-bridges-two-directions.png", width=1800, height=1000))
    assert model_signature(buses.model, buses.diagram) == baseline

    main_window = None if args.skip_main_window else capture_main_window(records)
    assert hashlib.sha256(EXAMPLE.read_bytes()).hexdigest() == example_hash
    report = {"platform": QApplication.platformName(), "automated_not_manual": True,
        "uses_real_application_scene": True, "example_file_unchanged": True,
        "rendering_mutated_models": False, "crossing_electrical_nodes": 4,
        "independent_crossing_components": True, "main_window": main_window, "records": records}
    (OUTPUT / "evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
