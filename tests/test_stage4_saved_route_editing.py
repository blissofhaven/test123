# -*- coding: utf-8 -*-
"""Редактирование уже сохранённой ортогональной трассы этапа 4."""
from __future__ import annotations

import os
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rza_calc.adapters.legacy_calculation import AdapterDiagnostic  # noqa: E402
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.electrical import (  # noqa: E402
    DataConfirmation,
    ElectricalModel,
    LineKind,
    VoltageClassId,
)
from rza_calc.editor.controller import (  # noqa: E402
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.editor.validation import ProjectValidationService  # noqa: E402
from rza_calc.gui.editor_scene import EditorCanvas  # noqa: E402


_APP: QApplication | None = None
U10 = VoltageClassId("builtin.voltage.ac.10kv")


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
    page = DiagramPage(PageId("page.stage4.saved-route"), "Основная схема")
    return ProjectEditorController(
        _Project(
            ElectricalModel.with_builtins("Редактирование трассы"),
            DiagramDocument.create(
                "Однолинейная схема",
                (page,),
                document_id=DiagramDocumentId("diagram.stage4.saved-route"),
            ),
            ProjectCatalogSnapshots(),
        )
    )


def _connected_route(controller: ProjectEditorController):
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Нагрузка 2", x=180, y=0, voltage_class_by_group={"main": U10})
    waypoints = (
        RouteWaypoint(RouteWaypointId("waypoint.saved.start"), 0, 0),
        RouteWaypoint(RouteWaypointId("waypoint.saved.corner.1"), 60, 0),
        RouteWaypoint(RouteWaypointId("waypoint.saved.corner.2"), 60, 80),
        RouteWaypoint(RouteWaypointId("waypoint.saved.corner.3"), 180, 80),
        RouteWaypoint(RouteWaypointId("waypoint.saved.end"), 180, 0),
    )
    result = controller.connect_ports(
        first.port_ids[0],
        second.port_ids[0],
        route_waypoints=waypoints,
        first_representation_id=first.representation_id,
        second_representation_id=second.representation_id,
    )
    assert result.route_id is not None
    return result


def _assert_orthogonal(route) -> None:
    for first, second in zip(route.waypoints, route.waypoints[1:]):
        assert (first.x == second.x) ^ (first.y == second.y)


def test_add_manual_waypoint_to_saved_route_is_one_graphical_command() -> None:
    _app()
    controller = _controller()
    connected = _connected_route(controller)
    canvas = EditorCanvas(controller)
    topology_before = controller.model.connectivity_signature()
    electrical_revision_before = controller.model.revision
    history_before = len(controller.journal)

    canvas.scene._route_items_by_id[connected.route_id].add_user_waypoint(
        QPointF(105, 45)
    )

    route = controller.diagram.routes[connected.route_id]
    manual = [item for item in route.waypoints if item.source.value == "user"]
    assert len(manual) == 1
    assert manual[0].pinned is True
    assert (manual[0].x, manual[0].y) == (105.0, 45.0)
    _assert_orthogonal(route)
    assert controller.model.connectivity_signature() == topology_before
    assert controller.model.revision == electrical_revision_before
    assert len(controller.journal) == history_before + 1
    controller.undo()
    assert not [
        item
        for item in controller.diagram.routes[connected.route_id].waypoints
        if item.source.value == "user"
    ]


def test_move_internal_segment_pins_its_corners_without_topology_change() -> None:
    _app()
    controller = _controller()
    connected = _connected_route(controller)
    canvas = EditorCanvas(controller)
    route_item = canvas.scene._route_items_by_id[connected.route_id]
    route_item.setSelected(True)
    assert 2 in route_item._segment_handles
    topology_before = controller.model.connectivity_signature()
    electrical_revision_before = controller.model.revision
    history_before = len(controller.journal)

    route_item.commit_segment_move(2, QPointF(120, 120))

    route = controller.diagram.routes[connected.route_id]
    moved = [
        item
        for item in route.waypoints
        if item.source.value == "user" and item.pinned
    ]
    assert {(item.x, item.y) for item in moved} == {(60.0, 120.0), (180.0, 120.0)}
    assert {item.id for item in moved} == {
        RouteWaypointId("waypoint.saved.corner.2"),
        RouteWaypointId("waypoint.saved.corner.3"),
    }
    _assert_orthogonal(route)
    assert controller.model.connectivity_signature() == topology_before
    assert controller.model.revision == electrical_revision_before
    assert len(controller.journal) == history_before + 1


def test_short_internal_segment_also_has_a_move_handle() -> None:
    _app()
    controller = _controller()
    first = controller.add_equipment("builtin.load", "Короткий A", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Короткий B", x=100, y=0, voltage_class_by_group={"main": U10})
    connected = controller.connect_ports(
        first.port_ids[0],
        second.port_ids[0],
        route_waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.short.start"), 0, 0),
            RouteWaypoint(RouteWaypointId("waypoint.short.1"), 50, 0),
            RouteWaypoint(RouteWaypointId("waypoint.short.2"), 50, 10),
            RouteWaypoint(RouteWaypointId("waypoint.short.3"), 100, 10),
            RouteWaypoint(RouteWaypointId("waypoint.short.end"), 100, 0),
        ),
    )
    assert connected.route_id is not None
    canvas = EditorCanvas(controller)
    route_item = canvas.scene._route_items_by_id[connected.route_id]
    route_item.setSelected(True)

    assert 1 in route_item._segment_handles
    route_item.commit_segment_move(1, QPointF(70, 5))

    route = controller.diagram.routes[connected.route_id]
    assert {(item.x, item.y) for item in route.waypoints if item.source.value == "user"} >= {
        (70.0, 0.0),
        (70.0, 10.0),
    }
    _assert_orthogonal(route)


def test_context_delete_of_node_connection_does_not_require_equipment_id() -> None:
    _app()
    controller = _controller()
    connected = _connected_route(controller)
    canvas = EditorCanvas(controller)

    canvas._route_context_action(
        "delete_route",
        {"route_id": connected.route_id, "equipment_id": None},
    )

    assert connected.route_id not in controller.diagram.routes
    assert not controller.model.connections


def test_route_delete_uses_confirmation_when_workspace_requires_it() -> None:
    _app()
    controller = _controller()
    connected = _connected_route(controller)
    canvas = EditorCanvas(controller, confirm_deletions=True)
    requested: list[object] = []
    canvas.routeDeleteConfirmationRequested.connect(requested.append)

    canvas.delete_route(connected.route_id)

    assert requested == [connected.route_id]
    assert connected.route_id in controller.diagram.routes
    canvas.delete_route(connected.route_id, confirmed=True)
    assert connected.route_id not in controller.diagram.routes


def test_delete_second_node_route_preserves_shared_existing_port_and_route() -> None:
    _app()
    controller = _controller()
    first = controller.add_equipment("builtin.load", "A", x=0, y=0, voltage_class_by_group={"main": U10})
    shared = controller.add_equipment("builtin.load", "B", x=180, y=0, voltage_class_by_group={"main": U10})
    third = controller.add_equipment("builtin.load", "D", x=90, y=120, voltage_class_by_group={"main": U10})
    original = controller.connect_ports(first.port_ids[0], shared.port_ids[0])
    added = controller.connect_ports(third.port_ids[0], shared.port_ids[0])
    assert original.route_id is not None and added.route_id is not None
    original_connections = {
        port_id: controller.model.connection_for_port(port_id).id
        for port_id in (first.port_ids[0], shared.port_ids[0])
    }

    controller.delete_diagram_route(added.route_id)

    assert original.route_id in controller.diagram.routes
    assert added.route_id not in controller.diagram.routes
    assert controller.model.connection_for_port(third.port_ids[0]) is None
    for port_id, connection_id in original_connections.items():
        assert controller.model.connection_for_port(port_id).id == connection_id
    assert len(controller.model.connections) == 2
    controller.undo()
    assert added.route_id in controller.diagram.routes
    assert controller.model.connection_for_port(third.port_ids[0]) is not None


def _main_physical_line(controller: ProjectEditorController):
    start = controller.add_electrical_node("Начало ВЛ", x=0, y=0, voltage_class_id=U10)
    end = controller.add_electrical_node("Конец ВЛ", x=400, y=0, voltage_class_id=U10)
    return controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(
            10_000_000,
            DataConfirmation.CONFIRMED,
        ),
    )


def test_context_tap_stays_preview_until_user_finishes_branch(monkeypatch) -> None:
    _app()
    controller = _controller()
    line = _main_physical_line(controller)
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    monkeypatch.setattr(
        canvas,
        "_ask_branch_parameters",
        lambda: (
            True,
            "Отпайка к КТП",
            LineKind.CABLE,
            PhysicalLineInput(800_000, DataConfirmation.CONFIRMED),
        ),
    )
    topology_before = controller.model.connectivity_signature()
    history_before = len(controller.journal)

    canvas._route_context_action(
        "add_tap",
        {
            "route_id": line.route_id,
            "equipment_id": line.section_id,
            "scene_x": 160.0,
            "scene_y": 0.0,
            "route_fraction": 0.4,
        },
    )

    assert canvas.scene.physical_line_active
    assert canvas._pending_tap_branch is not None
    assert controller.model.connectivity_signature() == topology_before
    assert len(controller.journal) == history_before
    canvas.scene.update_physical_line_cursor(QPointF(260, 160))
    canvas.scene._physical_line_tool.add_manual_vertex(220, 80)
    canvas.scene.update_physical_line_cursor(QPointF(260, 160))
    assert canvas.scene.finish_physical_line(free_target=True)

    main = controller.model.logical_lines[line.logical_line_id]
    assert len(main.section_equipment_ids) == 2
    assert len(controller.model.line_sections) == 3
    assert len(controller.diagram.routes) == 3
    assert any(
        waypoint.source.value == "user" and waypoint.pinned
        for route in controller.diagram.routes.values()
        if route.equipment_id not in set(main.section_equipment_ids)
        for waypoint in route.waypoints
    )
    assert len(controller.journal) == history_before + 1


def test_cancel_context_tap_removes_preview_without_project_changes(monkeypatch) -> None:
    _app()
    controller = _controller()
    line = _main_physical_line(controller)
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 5_000_000),
    )
    monkeypatch.setattr(
        canvas,
        "_ask_branch_parameters",
        lambda: (
            True,
            "Отменяемая отпайка",
            LineKind.OVERHEAD,
            PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
        ),
    )
    topology_before = controller.model.connectivity_signature()
    diagram_before = controller.diagram
    history_before = len(controller.journal)

    canvas._route_context_action(
        "add_tap",
        {
            "route_id": line.route_id,
            "equipment_id": line.section_id,
            "scene_x": 200.0,
            "scene_y": 0.0,
            "route_fraction": 0.5,
        },
    )
    canvas.scene.update_physical_line_cursor(QPointF(280, 140))
    canvas.scene.cancel_physical_line()

    assert not canvas.scene.physical_line_active
    assert canvas._pending_tap_branch is None
    assert controller.model.connectivity_signature() == topology_before
    assert controller.diagram == diagram_before
    assert len(controller.journal) == history_before


def test_context_tap_with_unknown_main_length_stays_unconfirmed(monkeypatch) -> None:
    _app()
    controller = _controller()
    start = controller.add_electrical_node("Начало неизвестной ВЛ", x=0, y=0, voltage_class_id=U10)
    end = controller.add_electrical_node("Конец неизвестной ВЛ", x=400, y=0, voltage_class_id=U10)
    line = controller.create_physical_line(
        "ВЛ с неизвестной длиной",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, None),
    )
    monkeypatch.setattr(
        canvas,
        "_ask_branch_parameters",
        lambda: (
            True,
            "Отпайка без подтверждённой длины",
            LineKind.CABLE,
            PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
        ),
    )

    canvas._route_context_action(
        "add_tap",
        {
            "route_id": line.route_id,
            "equipment_id": line.section_id,
            "scene_x": 180.0,
            "scene_y": 0.0,
            "route_fraction": 0.45,
        },
    )
    canvas.scene.update_physical_line_cursor(QPointF(260, 140))
    assert canvas.scene.finish_physical_line(free_target=True)

    assert len(controller.model.line_sections) == 3
    assert all(
        section.length_mm is None
        for section in controller.model.line_sections.values()
    )
    assert all(
        segment.length_confirmation is DataConfirmation.UNCONFIRMED
        for section in controller.model.line_sections.values()
        for segment in section.construction_segments
    )


def test_diagnostics_are_visible_near_object_and_adapter_status_is_actual() -> None:
    _app()
    controller = _controller()
    unconnected = controller.add_equipment(
        "builtin.load", "Неподключённая нагрузка", x=40, y=120
    )
    line = _main_physical_line(controller)
    canvas = EditorCanvas(controller)
    adapter_issue = AdapterDiagnostic(
        "error",
        "line_data_unconfirmed",
        "Длина или сопротивления линии не подтверждены.",
        line.section_id.value,
    )
    diagnostics = ProjectValidationService().validate(
        controller.model,
        controller.diagram,
        adapter_diagnostics=(adapter_issue,),
    )

    canvas.scene.set_diagnostics(diagnostics)
    canvas.scene.set_adapter_diagnostics((adapter_issue,), available=True)

    object_item = canvas.scene._items_by_id[unconnected.representation_id]
    assert object_item._diagnostic_badge.isVisible()
    assert "Не подключён обязательный электрический порт" in (
        object_item._diagnostic_badge.toolTip()
    )
    route_item = canvas.scene._route_items_by_id[line.route_id]
    assert route_item._diagnostic_badge.isVisible()
    assert "заблокирован" in route_item._adapter_status
    assert "Длина или сопротивления" in route_item._debug.text()
    assert "Подтверждённость сопротивлений: не подтверждено" in route_item._debug.text()
    assert "unconfirmed" not in route_item._debug.text()
    assert "confirmed" not in route_item._debug.text()

    equipment_issue = AdapterDiagnostic(
        "error",
        "migration.electrical_route_review_required",
        "Требуется проверка перенесённого оборудования.",
        unconnected.equipment_id.value,
    )
    canvas.scene.set_adapter_diagnostics((equipment_issue,), available=True)
    assert "заблокирован" in object_item._adapter_status
    assert "Требуется проверка" in object_item._debug.text()


def test_inline_diagnostic_follows_same_equipment_to_another_page() -> None:
    _app()
    controller = _controller()
    equipment = controller.add_equipment(
        "builtin.load", "Нагрузка на двух страницах", x=20, y=40
    )
    second_page = controller.create_page("Вторая страница")
    second_representation = controller.place_existing_equipment(
        equipment.equipment_id,
        page_id=second_page,
        x=220,
        y=120,
    )
    diagnostics = ProjectValidationService().validate(
        controller.model,
        controller.diagram,
    )
    canvas = EditorCanvas(controller)
    canvas.show_page(
        controller.diagram.representations[equipment.representation_id].page_id
    )
    canvas.scene.set_diagnostics(diagnostics)
    assert canvas.scene._items_by_id[
        equipment.representation_id
    ]._diagnostic_badge.isVisible()

    canvas.show_page(second_page)

    assert canvas.scene._items_by_id[
        second_representation
    ]._diagnostic_badge.isVisible()


def test_developer_overlay_switch_updates_existing_objects_and_routes() -> None:
    _app()
    controller = _controller()
    equipment = controller.add_equipment(
        "builtin.load", "Нагрузка для диагностики", x=40, y=100
    )
    line = _main_physical_line(controller)
    canvas = EditorCanvas(controller)
    object_item = canvas.scene._items_by_id[equipment.representation_id]
    route_item = canvas.scene._route_items_by_id[line.route_id]
    assert not object_item._debug.isVisible()
    assert not route_item._debug.isVisible()

    canvas.scene.set_developer_overlay(True)

    assert object_item._debug.isVisible()
    assert route_item._debug.isVisible()
    canvas.scene.set_developer_overlay(False)
    assert not object_item._debug.isVisible()
    assert not route_item._debug.isVisible()
