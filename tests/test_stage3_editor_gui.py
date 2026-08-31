# -*- coding: utf-8 -*-
"""Offscreen-проверки настоящего QGraphicsScene редактора Этапа 3."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from PySide6.QtWidgets import QApplication, QGraphicsScene  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
)
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, LineKind, VoltageClassId  # noqa: E402
from rza_calc.editor import EditorMode, ProjectEditorController  # noqa: E402
from rza_calc.gui.editor_panels import (  # noqa: E402
    EditorWorkspaceWidget,
    EquipmentLibraryTree,
)
from rza_calc.gui.editor_scene import EditorCanvas  # noqa: E402
from rza_calc.gui.strings import RU, ui_text  # noqa: E402


_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller() -> ProjectEditorController:
    page = DiagramPage(PageId("page.gui.main"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins("Редактор GUI"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,),
            document_id=DiagramDocumentId("diagram.gui"),
        ),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def _library_payload(workspace: EditorWorkspaceWidget, title: str) -> dict:
    tree = workspace.side_panel.library

    def visit(item):
        if item.text(0) == title:
            return item.data(0, EquipmentLibraryTree.PAYLOAD_ROLE)
        for index in range(item.childCount()):
            found = visit(item.child(index))
            if found:
                return found
        return None

    for index in range(tree.topLevelItemCount()):
        result = visit(tree.topLevelItem(index))
        if result:
            return result
    raise AssertionError(f"В библиотеке нет элемента '{title}'.")


def test_workspace_uses_real_qgraphics_scene_and_project_diagram() -> None:
    _app()
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка-1", x=20, y=40)
    workspace = EditorWorkspaceWidget(controller)

    assert isinstance(workspace.scene, QGraphicsScene)
    assert added.representation_id in workspace.scene._items_by_id
    item = workspace.scene._items_by_id[added.representation_id]
    assert item.representation.equipment_id == added.equipment_id
    assert item.pos().x() == controller.diagram.representations[added.representation_id].x
    assert item.pos().y() == controller.diagram.representations[added.representation_id].y


def test_library_hides_manual_node_and_buses_create_real_electrical_nodes() -> None:
    _app()
    controller = _controller()
    workspace = EditorWorkspaceWidget(controller)
    equipment_before = len(controller.model.equipment)

    try:
        with pytest.raises(AssertionError, match="В библиотеке нет элемента"):
            _library_payload(workspace, ui_text("equipment.connection_point"))
        assert not controller.model.electrical_nodes

        for index, title in enumerate((
            ui_text("equipment.busbar_horizontal"),
            ui_text("equipment.busbar_vertical"),
        )):
            payload = _library_payload(workspace, title)
            assert payload["target_kind"] == "electrical_node"
            workspace.canvas._add_equipment(payload, 13 + index * 600, 27)

        assert len(controller.model.electrical_nodes) == 2
        assert len(controller.model.equipment) == equipment_before
        representations = tuple(controller.diagram.representations.values())
        assert len(representations) == 2
        assert all(row.electrical_node_id is not None for row in representations)
        horizontal = next(row for row in representations if row.label == ui_text("equipment.busbar_horizontal"))
        vertical = next(row for row in representations if row.label == ui_text("equipment.busbar_vertical"))
        assert horizontal.rotation_deg == 180.0
        assert vertical.rotation_deg == 90.0
        assert controller.model.connectivity_signature() == ()
    finally:
        workspace.close()


@pytest.mark.parametrize("kind", (LineKind.OVERHEAD, LineKind.CABLE))
def test_physical_line_library_item_creates_real_branch_ports_and_route(kind) -> None:
    _app()
    controller = _controller()
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first = controller.add_equipment("builtin.circuit_breaker", "Начало", x=0, y=0,
                                     voltage_class_by_group={"main": voltage})
    second = controller.add_equipment("builtin.circuit_breaker", "Конец", x=240, y=0,
                                      voltage_class_by_group={"main": voltage})
    workspace = EditorWorkspaceWidget(controller)
    try:
        with pytest.raises(AssertionError, match="В библиотеке нет элемента"):
            _library_payload(workspace, ui_text("equipment.line"))
        payload = _library_payload(workspace, ui_text("equipment.overhead_line" if kind is LineKind.OVERHEAD else "equipment.cable_line"))
        assert payload["target_kind"] == "physical_line"
        first_port = workspace.scene._items_by_id[first.representation_id].port_item(first.port_ids[-1])
        second_port = workspace.scene._items_by_id[second.representation_id].port_item(second.port_ids[0])
        journal = len(controller.journal)
        workspace.canvas._add_equipment(payload, first_port.scenePos().x(), first_port.scenePos().y())
        assert workspace.scene.physical_line_active
        workspace.scene.update_physical_line_cursor(second_port.scenePos())
        assert workspace.scene.finish_physical_line()
        assert len(controller.journal) == journal+1
        assert len(controller.model.equipment) == 3
        section = next(iter(controller.model.line_sections.values()))
        equipment = controller.model.equipment[section.equipment_id]
        assert len(equipment.port_ids) == 2
        assert controller.model.logical_lines[section.logical_line_id].line_kind is kind
        assert section.length_mm is None
        assert section.construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED
        route, = controller.diagram.routes.values()
        assert route.equipment_id == equipment.id
        assert len(controller.diagram.representations) == 2, "The physical conductor is its route, not a second standalone symbol"
        assert workspace._selected_route_ids == (route.id,)
        controller.undo()
        assert not controller.model.line_sections and not controller.diagram.routes
        assert set(controller.model.equipment) == {first.equipment_id, second.equipment_id}
    finally:
        workspace.close()


def test_group_move_commits_once_and_analysis_mode_locks_geometry() -> None:
    _app()
    controller = _controller()
    first = controller.add_equipment("builtin.load", "Нагрузка-1", x=20, y=20)
    second = controller.add_equipment("builtin.load", "Нагрузка-2", x=80, y=40)
    canvas = EditorCanvas(controller)
    ids = (first.representation_id, second.representation_id)
    topology = controller.model.connectivity_signature()
    electrical_revision = controller.model.revision
    before_revision = controller.diagram.revision

    canvas._move(ids, 40.0, 20.0, False)

    assert controller.diagram.revision == before_revision + 1
    assert controller.model.connectivity_signature() == topology
    assert controller.model.revision == electrical_revision
    assert controller.diagram.representations[first.representation_id].x == 60.0
    assert controller.diagram.representations[second.representation_id].x == 120.0

    controller.set_mode(EditorMode.ANALYSIS)
    canvas.refresh()
    assert all(
        not item.flags() & item.GraphicsItemFlag.ItemIsMovable
        for item in canvas.scene._items_by_id.values()
    )


def test_resize_label_and_zoom_are_read_from_stage3_graphics() -> None:
    _app()
    controller = _controller()
    added = controller.add_equipment("builtin.transformer_2w", "Т-1", x=40, y=60)
    canvas = EditorCanvas(controller)
    controller.resize_representation(added.representation_id, 160.0, 90.0)
    controller.set_label(
        added.representation_id,
        text="Трансформатор связи",
        label_x=15.0,
        label_y=-55.0,
        visible=False,
    )
    canvas.refresh()

    item = canvas.scene._items_by_id[added.representation_id]
    assert item._width == 160.0
    assert item._height == 90.0
    assert item._label.text() == "Трансформатор связи"
    assert not item._label.isVisible()
    canvas.view.set_zoom(2.0)
    assert abs(canvas.view.zoom_factor - 2.0) < 1e-9
    canvas.view.actual_size()
    assert abs(canvas.view.zoom_factor - 1.0) < 1e-9


def test_central_editor_catalog_contains_russian_visible_strings() -> None:
    required = (
        "panel.project", "panel.equipment", "panel.properties",
        "panel.problems", "panel.diagnostics", "panel.journal",
        "mode.edit", "mode.analysis", "action.save", "action.undo",
        "action.redo", "action.grid", "action.snap", "action.validate",
        "confirm.delete.title", "confirm.delete.text",
    )
    assert all(key in RU and any("а" <= char.casefold() <= "я" or char in "ёЁ" for char in RU[key])
               for key in required)


def test_main_window_opens_editor_first_and_keeps_legacy_analysis() -> None:
    _app()
    from rza_calc.gui.main_window import MainWindow
    from rza_calc.gui.view_model import ProjectViewModel

    source = (
        Path(__file__).resolve().parent.parent
        / "rza_calc"
        / "examples"
        / "gtes_sever.json"
    )
    vm = ProjectViewModel.open(source)
    window = MainWindow(vm)
    try:
        assert window.workspace_tabs.count() == 2
        assert window.workspace_tabs.currentIndex() == 0
        assert window.workspace_tabs.tabText(0) == "Редактор схемы"
        assert window.workspace_tabs.tabText(1) == "Анализ и расчёты"
        assert window.editor_controller.model is vm.project.electrical_model
        assert window.editor_workspace.controller is window.editor_controller
    finally:
        window.close()
