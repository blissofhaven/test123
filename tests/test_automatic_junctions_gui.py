# -*- coding: utf-8 -*-
"""GUI-контракт автоматической отпайки без расчёта позиции по пикселям."""
from __future__ import annotations

import os
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

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
from rza_calc.editor.connection_tool import (  # noqa: E402
    ConnectionTargetFeedback,
    ConnectionTargetKind,
)
from rza_calc.editor.controller import (  # noqa: E402
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.editor.placement import EquipmentPlacementKind  # noqa: E402
from rza_calc.gui.editor_scene import EditorCanvas  # noqa: E402


_APP: QApplication | None = None
U10 = VoltageClassId("builtin.voltage.ac.10kv")
TAP_HINT = "Отпустите кнопку, чтобы создать отпайку"


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
    page = DiagramPage(PageId("page.automatic-junctions"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins("Автоматические узлы"),
        DiagramDocument.create(
            "Автоматические узлы",
            (page,),
            document_id=DiagramDocumentId("diagram.automatic-junctions"),
        ),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def _line_fixture() -> tuple[ProjectEditorController, object, object]:
    controller = _controller()
    start = controller.add_electrical_node(
        "Начало магистрали",
        x=0.0,
        y=0.0,
        voltage_class_id=U10,
    )
    finish = controller.add_electrical_node(
        "Конец магистрали",
        x=400.0,
        y=0.0,
        voltage_class_id=U10,
    )
    main = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(finish.node_id),
        physical=PhysicalLineInput(
            10_000_000,
            DataConfirmation.CONFIRMED,
        ),
    )
    return controller, main, finish


def _begin_branch(canvas: EditorCanvas, x: float = 200.0, y: float = 200.0):
    assert canvas.scene.begin_physical_line(
        name="Новая отпайка",
        line_kind=LineKind.CABLE,
        scene_pos=QPointF(x, y),
    )
    source = canvas.scene._physical_line_tool.source
    assert source is not None
    assert source.kind is ConnectionTargetKind.FREE
    return source


def test_physical_line_target_is_zoom_invariant_and_preview_marks_split() -> None:
    _app()
    controller, _, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    messages: list[str] = []
    canvas.scene.connectionStatusMessage.connect(messages.append)
    _begin_branch(canvas)

    for zoom in (0.25, 1.0, 4.0):
        canvas.view.set_zoom(zoom)
        canvas.scene.update_physical_line_cursor(
            QPointF(200.0, 8.0 / zoom)
        )
        target = canvas.scene._physical_line_tool.target
        assert target is not None
        assert target.kind is ConnectionTargetKind.PHYSICAL_LINE
        assert target.message == TAP_HINT
        assert target.x == 200.0
        assert target.y == 0.0
        assert canvas.scene._preview_item.isVisible()
        assert canvas.scene._preview_item._junction_orientation == "horizontal"
        route_item = canvas.scene._route_items_by_id[next(iter(
            canvas.scene._route_items_by_id
        ))]
        assert route_item._target_feedback is ConnectionTargetFeedback.COMPATIBLE
        assert messages[-1] == TAP_HINT

        canvas.scene.update_physical_line_cursor(
            QPointF(200.0, 10.0 / zoom)
        )
        assert canvas.scene._physical_line_tool.target is None


def test_escape_clears_preview_without_model_diagram_or_history_mutation() -> None:
    _app()
    controller, _, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    _begin_branch(canvas)
    before_signature = controller.model.connectivity_signature()
    before_model_revision = controller.model.revision
    before_diagram_revision = controller.diagram.revision
    before_journal = tuple(controller.journal)

    canvas.scene.update_physical_line_cursor(QPointF(200.0, 0.0))
    assert canvas.scene._physical_line_tool.target is not None
    assert canvas.scene._preview_item.isVisible()
    canvas.show()
    canvas.view.setFocus()
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()

    assert not canvas.scene.physical_line_active
    assert not canvas.scene._preview_item.isVisible()
    assert controller.model.connectivity_signature() == before_signature
    assert controller.model.revision == before_model_revision
    assert controller.diagram.revision == before_diagram_revision
    assert tuple(controller.journal) == before_journal
    canvas.close()


def test_finishing_physical_line_on_line_creates_one_atomic_tap(monkeypatch) -> None:
    _app()
    controller, main, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    _begin_branch(canvas)
    branch_physical = PhysicalLineInput(
        750_000,
        DataConfirmation.CONFIRMED,
    )
    monkeypatch.setattr(
        canvas,
        "_ask_new_physical_line_parameters",
        lambda draft: (True, draft.name, branch_physical),
    )
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    before_signature = controller.model.connectivity_signature()
    before_model_revision = controller.model.revision
    before_diagram_revision = controller.diagram.revision
    history_before = len(controller.journal)

    canvas.scene.update_physical_line_cursor(QPointF(200.0, 0.0))
    target = canvas.scene._physical_line_tool.target
    assert target is not None and target.kind is ConnectionTargetKind.PHYSICAL_LINE
    assert controller.model.connectivity_signature() == before_signature
    assert controller.model.revision == before_model_revision
    assert controller.diagram.revision == before_diagram_revision
    assert len(controller.journal) == history_before

    assert canvas.scene.finish_physical_line()

    assert len(controller.journal) == history_before + 1
    assert main.section_id not in controller.model.line_sections
    assert len(controller.model.logical_lines[main.logical_line_id].section_equipment_ids) == 2
    assert len(controller.model.line_sections) == 3
    taps = [
        node
        for node in controller.model.electrical_nodes.values()
        if node.extensions.get("junction_kind") == "line_tap"
    ]
    assert len(taps) == 1
    tap = taps[0]
    assert sum(
        row.electrical_node_id == tap.id
        for row in controller.model.connections.values()
    ) == 3

    branch_lines = [
        line
        for line in controller.model.logical_lines.values()
        if line.id != main.logical_line_id
    ]
    assert len(branch_lines) == 1
    branch_section_id = branch_lines[0].section_equipment_ids[0]
    branch_section = controller.model.line_sections[branch_section_id]
    automatic_endpoints = [
        node
        for node in controller.model.electrical_nodes.values()
        if node.extensions.get("creation_origin") == "automatic_free_endpoint"
    ]
    assert len(automatic_endpoints) == 1
    branch_end = automatic_endpoints[0]
    branch_nodes = {
        controller.model.connection_for_port(port_id).electrical_node_id
        for port_id in controller.model.equipment[branch_section.equipment_id].port_ids
    }
    assert branch_nodes == {tap.id, branch_end.id}
    branch_route = next(
        route
        for route in controller.diagram.routes.values()
        if route.equipment_id == branch_section.equipment_id
    )
    assert (branch_route.waypoints[0].x, branch_route.waypoints[0].y) == (
        target.x,
        target.y,
    )
    assert (branch_route.waypoints[-1].x, branch_route.waypoints[-1].y) == (
        200.0,
        200.0,
    )


def test_split_offset_default_is_physical_and_not_cursor_fraction(monkeypatch) -> None:
    _app()
    controller, main, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    calls: list[tuple[object, ...]] = []

    def fake_get_double(*args):
        calls.append(args)
        return float(args[3]), True

    monkeypatch.setattr(QInputDialog, "getDouble", fake_get_double)
    first = canvas._ask_physical_split_offset(
        main.section_id,
        0.1,
        title="Отпайка",
    )
    second = canvas._ask_physical_split_offset(
        main.section_id,
        0.9,
        title="Отпайка",
    )

    assert first == second == (True, 5_000_000)
    assert [row[3] for row in calls] == [5_000.0, 5_000.0]
    assert all("положение курсора не используется" in str(row[2]) for row in calls)


def test_registry_preview_for_breaker_is_transient_and_highlights_line() -> None:
    _app()
    controller, _, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    before_signature = controller.model.connectivity_signature()
    before_revision = controller.model.revision
    before_journal = tuple(controller.journal)

    canvas.view._update_equipment_drop_feedback(
        {
            "target_kind": "equipment",
            "type_id": "builtin.circuit_breaker",
            "name": "QF preview",
        },
        QPointF(200.0, 0.0),
    )

    assert canvas.view._drop_feedback_kind is EquipmentPlacementKind.INLINE_SERIES
    assert canvas.view._drop_feedback_point == QPointF(200.0, 0.0)
    route_item = next(iter(canvas.scene._route_items_by_id.values()))
    assert route_item._target_feedback is ConnectionTargetFeedback.COMPATIBLE
    assert controller.model.connectivity_signature() == before_signature
    assert controller.model.revision == before_revision
    assert tuple(controller.journal) == before_journal


def test_drop_breaker_on_physical_line_uses_generic_inline_command(monkeypatch) -> None:
    _app()
    controller, main, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )

    canvas._add_equipment(
        {
            "target_kind": "equipment",
            "type_id": "builtin.circuit_breaker",
            "name": "QF-авто",
        },
        200.0,
        0.0,
    )

    breakers = [
        item for item in controller.model.equipment.values()
        if item.type_id.value == "builtin.circuit_breaker"
    ]
    assert len(breakers) == 1
    marker = breakers[0].extensions["line_insertion"]
    assert marker["kind"] == "inline_series"
    assert marker["left_node_id"] != marker["right_node_id"]
    assert main.section_id not in controller.model.line_sections
    assert len(controller.journal) >= 1


def test_drop_transformer_on_line_creates_branch_attachment(monkeypatch) -> None:
    _app()
    controller, main, _ = _line_fixture()
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 3_000_000),
    )
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda *args, **kwargs: (args[3][0], True),
    )

    canvas._add_equipment(
        {
            "target_kind": "equipment",
            "type_id": "builtin.transformer_2w",
            "name": "Т-авто",
        },
        200.0,
        0.0,
    )

    transformers = [
        item for item in controller.model.equipment.values()
        if item.type_id.value == "builtin.transformer_2w"
    ]
    assert len(transformers) == 1
    transformer = transformers[0]
    assert "line_insertion" not in transformer.extensions
    assert transformer.extensions["placement_origin"] == (
        "automatic_branch_attachment"
    )
    line = controller.model.logical_lines[main.logical_line_id]
    assert len(line.section_equipment_ids) == 2
    tap = next(
        item for item in controller.model.electrical_nodes.values()
        if item.extensions.get("junction_kind") == "line_tap"
    )
    assert sum(
        item.electrical_node_id == tap.id
        for item in controller.model.connections.values()
    ) == 3


def test_remove_inserted_breaker_confirms_and_restores_line(monkeypatch) -> None:
    _app()
    controller, main, _ = _line_fixture()
    inserted = controller.insert_series_equipment(
        main.section_id,
        4_000_000,
        "builtin.circuit_breaker",
        "QF-удаление",
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    canvas._equipment_context_action(
        "remove_series_equipment_from_line",
        {"equipment_id": inserted.equipment_id},
    )

    assert inserted.equipment_id not in controller.model.equipment
    assert len(controller.model.line_sections) == 1
    assert next(iter(controller.model.line_sections.values())).length_mm == 10_000_000


def test_physical_line_ending_on_node_connection_reuses_existing_node(monkeypatch) -> None:
    _app()
    controller = _controller()
    first = controller.add_equipment(
        "builtin.load", "Нагрузка 1", x=0.0, y=0.0,
        voltage_class_by_group={"main": U10},
    )
    second = controller.add_equipment(
        "builtin.load", "Нагрузка 2", x=400.0, y=0.0,
        voltage_class_by_group={"main": U10},
    )
    connected = controller.connect_ports(first.port_ids[0], second.port_ids[0])
    assert connected.route_id is not None
    existing_node_id = connected.node_id
    canvas = EditorCanvas(controller)
    assert canvas.scene.begin_physical_line(
        name="Новая ВЛ",
        line_kind=LineKind.OVERHEAD,
        scene_pos=QPointF(200.0, 200.0),
    )
    monkeypatch.setattr(
        canvas,
        "_ask_new_physical_line_parameters",
        lambda draft: (
            True,
            draft.name,
            PhysicalLineInput(1_000_000, DataConfirmation.CONFIRMED),
        ),
    )
    node_ids_before = set(controller.model.electrical_nodes)

    canvas.scene.update_physical_line_cursor(QPointF(200.0, 0.0))
    target = canvas.scene._physical_line_tool.target
    assert target is not None
    assert target.kind is ConnectionTargetKind.NODE_CONNECTION
    assert target.target_id == existing_node_id.value
    assert canvas.scene.finish_physical_line()

    assert existing_node_id in controller.model.electrical_nodes
    assert len(set(controller.model.electrical_nodes) - node_ids_before) == 1
    assert len(controller.model.line_sections) == 1
    assert sum(
        item.electrical_node_id == existing_node_id
        for item in controller.model.connections.values()
    ) == 3
