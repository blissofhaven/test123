# -*- coding: utf-8 -*-
"""Интерактивные гарантии редактора Этапа 4 без запуска оконного цикла."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    PageId,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.electrical import (  # noqa: E402
    DataConfirmation,
    ElectricalModel,
    LineKind,
    OperatingState,
    OperatingStateId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor.connection_tool import (  # noqa: E402
    ConnectionTarget,
    ConnectionTargetFeedback,
    ConnectionTargetKind,
    ConnectionToolState,
)
from rza_calc.editor.controller import (  # noqa: E402
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.editor.orthogonal_routing import (  # noqa: E402
    RouteDirection,
    RouteVertex,
    RouteVertexSource,
    RoutingObstacle,
    RoutingRequest,
    build_orthogonal_route,
    normalize_route,
)
from rza_calc.gui.editor_scene import (  # noqa: E402
    EditorCanvas,
    DiagramGraphicsScene,
    DiagramGraphicsView,
    DiagramObjectItem,
    DiagramRouteItem,
)
from rza_calc.gui.editor_panels import EditorWorkspaceWidget, EquipmentLibraryTree  # noqa: E402
from rza_calc.gui.strings import ui_text  # noqa: E402
from rza_calc.topology import TopologyEngine  # noqa: E402


_APP: QApplication | None = None
_U10 = VoltageClassId("builtin.voltage.ac.10kv")


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
    page = DiagramPage(PageId("page.stage4.main"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins("Редактор Этапа 4"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,),
            document_id=DiagramDocumentId("diagram.stage4"),
        ),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def _library_payload(workspace: EditorWorkspaceWidget, title: str) -> dict:
    tree = workspace.side_panel.library

    def find(item):
        if item.text(0) == title:
            return item.data(0, EquipmentLibraryTree.PAYLOAD_ROLE)
        for index in range(item.childCount()):
            result = find(item.child(index))
            if result:
                return result
        return None

    for index in range(tree.topLevelItemCount()):
        result = find(tree.topLevelItem(index))
        if result:
            return result
    raise AssertionError(f"В библиотеке нет элемента «{title}».")


def _assert_orthogonal(vertices: tuple[RouteVertex, ...]) -> None:
    assert len(vertices) >= 2
    for first, second in zip(vertices, vertices[1:]):
        assert (first.x == second.x) ^ (first.y == second.y)


def test_normalization_removes_zero_and_auto_collinear_but_keeps_user_point() -> None:
    vertices = normalize_route(
        (
            RouteVertex(0, 0),
            RouteVertex(0, 0),
            RouteVertex(10, 0),
            RouteVertex(20, 0, RouteVertexSource.USER, pinned=True),
            RouteVertex(30, 0),
            RouteVertex(30, 15),
        )
    )

    assert [item.point for item in vertices] == [(0.0, 0.0), (20.0, 0.0), (30.0, 0.0), (30.0, 15.0)]
    assert vertices[1].source is RouteVertexSource.USER
    assert vertices[1].pinned is True
    _assert_orthogonal(vertices)


def test_local_router_builds_orthogonal_detour_around_obstacle() -> None:
    obstacle = RoutingObstacle(35, -10, 65, 10)
    vertices = build_orthogonal_route(
        RoutingRequest(
            RouteVertex(0, 0),
            RouteVertex(100, 0),
            RouteDirection.RIGHT,
            RouteDirection.LEFT,
            obstacles=(obstacle,),
            clearance=5,
            port_stub=10,
        )
    )

    _assert_orthogonal(vertices)
    assert vertices[0].point == (0.0, 0.0)
    assert vertices[-1].point == (100.0, 0.0)
    for first, second in zip(vertices, vertices[1:]):
        if first.x == second.x:
            assert not (35 < first.x < 65 and max(min(first.y, second.y), -10) < min(max(first.y, second.y), 10))
        else:
            assert not (-10 < first.y < 10 and max(min(first.x, second.x), 35) < min(max(first.x, second.x), 65))


def test_connection_tool_backspace_and_cancel_leave_no_transient_state() -> None:
    tool = ConnectionToolState()
    tool.begin(
        source_port_id="port.source",
        source_representation_id="representation.source",
        x=0,
        y=0,
        direction=RouteDirection.RIGHT,
    )
    target = ConnectionTarget(
        ConnectionTargetKind.ELECTRICAL_NODE,
        100,
        40,
        target_id="node.target",
        feedback=ConnectionTargetFeedback.COMPATIBLE,
    )
    tool.update(100, 40, target=target)
    tool.add_manual_vertex(35, 20)

    assert tool.manual_vertices[-1].source is RouteVertexSource.USER
    assert tool.manual_vertices[-1].pinned is True
    assert tool.remove_last_manual_vertex() is True
    assert tool.remove_last_manual_vertex() is False
    tool.cancel()

    assert tool.active is False
    assert tool.source_port_id == ""
    assert tool.target is None
    assert tool.manual_vertices == []
    assert tool.preview_vertices == ()


def test_scene_port_handles_use_real_domain_port_ids_and_preview_is_read_only() -> None:
    _app()
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка", x=20, y=20)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[added.representation_id]
    port_item = item.port_item(added.port_ids[0])
    assert port_item is not None
    assert port_item.port_id == added.port_ids[0]

    topology_before = controller.model.connectivity_signature()
    model_revision_before = controller.model.revision
    diagram_revision_before = controller.diagram.revision
    history_before = len(controller.journal)
    scene.begin_connection(port_item)
    scene.update_connection_cursor(QPointF(180, 70))

    assert scene.connection_active is True
    assert scene._preview_item.isVisible()
    assert controller.model.connectivity_signature() == topology_before
    assert controller.model.revision == model_revision_before
    assert controller.diagram.revision == diagram_revision_before
    assert len(controller.journal) == history_before
    scene.cancel_connection()
    assert scene.connection_active is False
    assert not scene._preview_item.isVisible()


def test_port_hit_tolerance_is_constant_in_screen_pixels() -> None:
    _app()
    controller = _controller()
    source = controller.add_equipment("builtin.load", "Нагрузка-1", x=0, y=0)
    target = controller.add_equipment("builtin.load", "Нагрузка-2", x=160, y=0)
    scene = DiagramGraphicsScene()
    view = DiagramGraphicsView(scene)
    scene.sync_document(controller.diagram, controller.model)
    source_item = scene._items_by_id[source.representation_id].port_item(source.port_ids[0])
    target_item = scene._items_by_id[target.representation_id].port_item(target.port_ids[0])
    assert source_item is not None and target_item is not None
    scene.begin_connection(source_item)
    target_point = target_item.mapToScene(QPointF())

    view.set_zoom(1.0)
    first = scene._target_at(QPointF(target_point.x() + 10.0, target_point.y()))
    view.set_zoom(2.0)
    second = scene._target_at(QPointF(target_point.x() + 5.0, target_point.y()))

    assert first is not None and first.target_id == target.port_ids[0].value
    assert second is not None and second.target_id == target.port_ids[0].value


def test_bus_target_keeps_one_electrical_node_for_any_anchor_position() -> None:
    _app()
    controller = _controller()
    source = controller.add_equipment("builtin.load", "Нагрузка", x=-160, y=0)
    bus = controller.add_electrical_node(
        "Шины 10 кВ",
        x=100,
        y=0,
        symbol_key="busbar_horizontal",
        width=220,
        height=22,
    )
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    source_port = scene._items_by_id[source.representation_id].port_item(source.port_ids[0])
    assert source_port is not None
    scene.begin_connection(source_port)

    left = scene._target_at(QPointF(30, 0))
    right = scene._target_at(QPointF(175, 0))

    assert left is not None and left.kind is ConnectionTargetKind.BUS
    assert right is not None and right.kind is ConnectionTargetKind.BUS
    assert left.target_id == right.target_id == bus.node_id.value
    assert left.anchor_key != right.anchor_key


def test_canvas_connection_signal_commits_real_project_data_in_one_command() -> None:
    _app()
    controller = _controller()
    source = controller.add_equipment("builtin.load", "Нагрузка-1", x=0, y=0,
                                      voltage_class_by_group={"main": _U10})
    target = controller.add_equipment("builtin.load", "Нагрузка-2", x=180, y=0,
                                      voltage_class_by_group={"main": _U10})
    canvas = EditorCanvas(controller)
    source_port = canvas.scene._items_by_id[source.representation_id].port_item(source.port_ids[0])
    target_port = canvas.scene._items_by_id[target.representation_id].port_item(target.port_ids[0])
    assert source_port is not None and target_port is not None
    journal_before = len(controller.journal)

    canvas.scene.begin_connection(source_port)
    canvas.scene.update_connection_cursor(QPointF(60, 80))
    canvas.scene._connection_tool.add_manual_vertex(60, 80)
    canvas.scene.update_connection_cursor(target_port.mapToScene(QPointF()))
    canvas.scene._emit_connection_draft()

    assert len(controller.model.connections) == 2
    assert len(controller.model.electrical_nodes) == 1
    assert len(controller.diagram.routes) == 1
    route = next(iter(controller.diagram.routes.values()))
    manual = [
        item for item in route.waypoints
        if item.source.value == "user"
    ]
    assert len(manual) == 1 and not manual[0].pinned
    assert (manual[0].x, manual[0].y) == (60.0, 80.0)
    assert len(controller.journal) == journal_before + 1
    assert not canvas.scene.connection_active


def test_gui_physical_line_tool_creates_logical_line_and_line_section_in_one_command(
    monkeypatch,
) -> None:
    _app()
    controller = _controller()
    start = controller.add_electrical_node("Начало ВЛ", x=0, y=0, voltage_class_id=_U10)
    end = controller.add_electrical_node("Конец ВЛ", x=300, y=120, voltage_class_id=_U10)
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_new_physical_line_parameters",
        lambda draft: (
            True,
            "ВЛ-10 кВ №1",
            PhysicalLineInput(
                1_250_000,
                DataConfirmation.CONFIRMED,
                impedance_confirmation=DataConfirmation.UNCONFIRMED,
            ),
        ),
    )
    start_point = canvas.scene._items_by_id[start.representation_id].scenePos()
    end_point = canvas.scene._items_by_id[end.representation_id].scenePos()
    history_before = len(controller.journal)

    assert canvas.scene.begin_physical_line(
        name="Воздушная линия",
        line_kind=LineKind.OVERHEAD,
        scene_pos=start_point,
    )
    canvas.scene.update_physical_line_cursor(end_point)
    assert canvas.scene.finish_physical_line()

    assert len(controller.model.logical_lines) == 1
    assert len(controller.model.line_sections) == 1
    section = next(iter(controller.model.line_sections.values()))
    assert section.length_mm == 1_250_000
    route = next(iter(controller.diagram.routes.values()))
    assert route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
    assert route.equipment_id == section.equipment_id
    assert len(controller.journal) == history_before + 1


def test_library_has_explicit_overhead_and_cable_physical_line_tools() -> None:
    _app()
    controller = _controller()
    workspace = EditorWorkspaceWidget(controller)
    overhead = _library_payload(workspace, ui_text("equipment.overhead_line"))
    cable = _library_payload(workspace, ui_text("equipment.cable_line"))

    assert overhead["target_kind"] == cable["target_kind"] == "physical_line"
    assert overhead["type_id"] != cable["type_id"]
    equipment_before = len(controller.model.equipment)
    workspace.canvas._add_equipment(overhead, 10.0, 20.0)

    assert workspace.scene.physical_line_active
    assert not controller.model.logical_lines
    assert not controller.model.line_sections
    assert len(controller.model.equipment) == equipment_before
    workspace.scene.cancel_physical_line()


def test_gui_drop_on_physical_line_creates_real_tap_and_keeps_main_line(
    monkeypatch,
) -> None:
    _app()
    controller = _controller()
    start = controller.add_electrical_node("Начало", x=0, y=0, voltage_class_id=_U10)
    end = controller.add_electrical_node("Конец", x=400, y=0, voltage_class_id=_U10)
    main = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    load = controller.add_equipment("builtin.load", "КТП-1", x=200, y=180,
                                    voltage_class_by_group={"main": _U10})
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    monkeypatch.setattr(
        canvas,
        "_ask_dragged_connection_kind",
        lambda screen_pos: "cable",
    )
    monkeypatch.setattr(
        canvas,
        "_ask_new_physical_line_parameters",
        lambda draft: (
            True,
            "Отпайка к КТП-1",
            PhysicalLineInput(750_000, DataConfirmation.CONFIRMED),
        ),
    )
    source_port = canvas.scene._items_by_id[load.representation_id].port_item(load.port_ids[0])
    midpoint = QPointF(200, 0)
    assert source_port is not None
    history_before = len(controller.journal)
    original_sections = dict(controller.model.line_sections)
    original_connections = dict(controller.model.connections)
    original_diagram = controller.diagram

    canvas.scene.begin_connection(source_port)
    canvas.scene.update_connection_cursor(midpoint)
    assert canvas.scene._connection_target is not None
    assert canvas.scene._connection_target.kind is ConnectionTargetKind.PHYSICAL_LINE
    # A physical branch requires the user's explicit КЛ choice after drawing.
    # The no-screen-position seam commits a simple wire to the new tap.
    canvas.scene._emit_connection_draft(
        screen_pos=canvas.view.viewport().mapToGlobal(canvas.view.mapFromScene(midpoint))
    )

    main_line = controller.model.logical_lines[main.logical_line_id]
    assert len(main_line.section_equipment_ids) == 2
    assert main.section_id not in controller.model.line_sections
    assert len(controller.model.line_sections) == 3
    main_sections = [controller.model.line_sections[identifier]
                     for identifier in main_line.section_equipment_ids]
    assert [section.length_mm for section in main_sections] == [4_000_000, 6_000_000]
    branch, = [section for identifier, section in controller.model.line_sections.items()
               if identifier not in main_line.section_equipment_ids]
    assert controller.model.logical_lines[branch.logical_line_id].line_kind is LineKind.CABLE
    assert branch.length_mm == 750_000
    junctions = [
        node_id
        for node_id in controller.model.electrical_nodes
        if sum(
            connection.electrical_node_id == node_id
            for connection in controller.model.connections.values()
        ) == 3
    ]
    assert len(junctions) == 1
    assert len(controller.journal) == history_before + 1
    controller.diagram.require_valid_targets(controller.model)
    controller.undo()
    assert dict(controller.model.line_sections) == original_sections
    assert dict(controller.model.connections) == original_connections
    assert dict(controller.diagram.routes) == dict(original_diagram.routes)


def test_gui_reconnects_existing_port_to_physical_line_tap_atomically(
    monkeypatch,
) -> None:
    _app()
    controller = _controller()
    start = controller.add_electrical_node("Начало", x=0, y=0, voltage_class_id=_U10)
    end = controller.add_electrical_node("Конец", x=400, y=0, voltage_class_id=_U10)
    old_node = controller.add_electrical_node("Старый узел", x=300, y=180, voltage_class_id=_U10)
    main = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    load = controller.add_equipment("builtin.load", "Нагрузка", x=200, y=180,
                                    voltage_class_by_group={"main": _U10})
    connected = controller.connect_port_to_node(
        load.port_ids[0],
        old_node.node_id,
        source_representation_id=load.representation_id,
        node_representation_id=old_node.representation_id,
    )
    old_connection_id = connected.connection_ids[0]
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    source_port = canvas.scene._items_by_id[load.representation_id].port_item(
        load.port_ids[0]
    )
    assert source_port is not None
    history_before = len(controller.journal)

    canvas.scene.begin_connection(source_port)
    canvas.scene.update_connection_cursor(QPointF(200, 0))
    assert canvas.scene._connection_target is not None
    assert canvas.scene._connection_target.kind is ConnectionTargetKind.PHYSICAL_LINE
    canvas.scene._emit_connection_draft()

    connection = controller.model.connection_for_port(load.port_ids[0])
    assert connection is not None and connection.id == old_connection_id
    assert connection.electrical_node_id != old_node.node_id
    assert sum(
        item.electrical_node_id == connection.electrical_node_id
        for item in controller.model.connections.values()
    ) == 3
    assert main.section_id not in controller.model.line_sections
    assert len(controller.journal) == history_before + 1


def test_gui_context_action_inserts_real_recloser_into_line(monkeypatch) -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    end = controller.add_electrical_node(
        "Конец", x=400, y=0, voltage_class_id=voltage_id
    )
    main = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 6_000_000),
    )
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *args, **kwargs: ("Р-1", True),
    )
    history_before = len(controller.journal)
    route = controller.diagram.routes[main.route_id]

    canvas.scene.routeContextActionRequested.emit(
        "insert_recloser",
        {
            "route_id": route.id,
            "equipment_id": main.section_id,
            "page_id": route.page_id,
            "scene_x": 240.0,
            "scene_y": 0.0,
            "route_fraction": 0.6,
        },
    )

    reclosers = [
        equipment
        for equipment in controller.model.equipment.values()
        if equipment.type_id.value == "builtin.recloser"
    ]
    assert len(reclosers) == 1
    recloser = reclosers[0]
    assert len(recloser.port_ids) == 2
    assert all(controller.model.connection_for_port(port_id) is not None for port_id in recloser.port_ids)
    assert main.section_id not in controller.model.line_sections
    assert len(controller.model.line_sections) == 2
    assert len(controller.journal) == history_before + 1


def test_gui_switch_recloser_uses_active_mode_and_updates_symbol_and_topology() -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    end = controller.add_electrical_node(
        "Конец", x=400, y=0, voltage_class_id=voltage_id
    )
    main = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    inserted = controller.insert_recloser(
        main.section_id,
        5_000_000,
        "Р-1",
        page_id=controller.diagram.pages[next(iter(controller.diagram.pages))].id,
        x=200.0,
        y=0.0,
    )
    controller.set_confirm_switching(False)
    canvas = EditorCanvas(controller)
    representation = next(
        item
        for item in controller.diagram.representations.values()
        if item.equipment_id == inserted.recloser_id
    )
    original_position = (representation.x, representation.y)
    history_before = len(controller.journal)

    canvas.scene.equipmentContextActionRequested.emit(
        "switch",
        {
            "equipment_id": inserted.recloser_id,
            "position": SwitchPosition.OPEN,
        },
    )

    state_id = controller.active_operating_state_id
    assert state_id is not None
    assert controller.effective_switch_position(inserted.recloser_id) is SwitchPosition.OPEN
    assert not TopologyEngine().compile(controller.model, state_id).has_path(
        inserted.left_node_id,
        inserted.right_node_id,
    )
    assert canvas.scene._items_by_id[representation.id]._switch_open is True
    assert len(controller.journal) == history_before + 1

    canvas.scene.equipmentContextActionRequested.emit(
        "switch",
        {
            "equipment_id": inserted.recloser_id,
            "position": SwitchPosition.CLOSED,
        },
    )

    assert controller.effective_switch_position(inserted.recloser_id) is SwitchPosition.CLOSED
    assert TopologyEngine().compile(controller.model, state_id).has_path(
        inserted.left_node_id,
        inserted.right_node_id,
    )
    assert canvas.scene._items_by_id[representation.id]._switch_open is False
    unchanged = controller.diagram.representations[representation.id]
    assert (unchanged.x, unchanged.y) == original_position
    assert len(controller.journal) == history_before + 2


def test_bottom_panel_reports_only_active_operating_state_diagnostics() -> None:
    _app()
    controller = _controller()
    source_node = controller.add_electrical_node(
        "Узел источника", x=0, y=0, voltage_class_id=VoltageClassId(
            "builtin.voltage.ac.10kv"
        )
    )
    load_node = controller.add_electrical_node(
        "Узел нагрузки", x=300, y=0, voltage_class_id=VoltageClassId(
            "builtin.voltage.ac.10kv"
        )
    )
    source = controller.add_equipment(
        "builtin.external_grid",
        "Источник 10 кВ",
        x=-120,
        y=0,
        voltage_class_by_group={
            "main": VoltageClassId("builtin.voltage.ac.10kv")
        },
    )
    recloser = controller.add_equipment(
        "builtin.recloser",
        "Реклоузер Р-1",
        x=150,
        y=0,
        voltage_class_by_group={
            "main": VoltageClassId("builtin.voltage.ac.10kv")
        },
    )
    controller.connect_port_to_node(
        source.port_ids[0],
        source_node.node_id,
        source_representation_id=source.representation_id,
        node_representation_id=source_node.representation_id,
    )
    controller.connect_port_to_node(
        recloser.port_ids[0],
        source_node.node_id,
        source_representation_id=recloser.representation_id,
        node_representation_id=source_node.representation_id,
    )
    controller.connect_port_to_node(
        recloser.port_ids[1],
        load_node.node_id,
        source_representation_id=recloser.representation_id,
        node_representation_id=load_node.representation_id,
    )
    closed = OperatingState(
        OperatingStateId("state.stage4.panel.closed"),
        "Нормальная схема",
        {recloser.equipment_id: SwitchPosition.CLOSED},
    )
    opened = OperatingState(
        OperatingStateId("state.stage4.panel.open"),
        "Ремонт реклоузера",
        {recloser.equipment_id: SwitchPosition.OPEN},
    )
    controller.model.add_operating_state(closed)
    controller.model.add_operating_state(opened)
    controller = ProjectEditorController(controller._project)
    controller.set_confirm_switching(False)
    controller.switch_equipment(
        recloser.equipment_id,
        SwitchPosition.CLOSED,
        state_id=closed.id,
        confirmed=True,
    )
    workspace = EditorWorkspaceWidget(controller)

    closed_messages = [
        workspace.bottom_panel.problems.item(index).text()
        for index in range(workspace.bottom_panel.problems.count())
    ]
    assert not any(
        "Активный участок не связан ни с одним источником" in message
        for message in closed_messages
    )

    controller.switch_equipment(
        recloser.equipment_id,
        SwitchPosition.OPEN,
        state_id=opened.id,
        confirmed=True,
    )
    workspace.refresh()
    opened_messages = [
        workspace.bottom_panel.problems.item(index).text()
        for index in range(workspace.bottom_panel.problems.count())
    ]
    source_less = next(
        message
        for message in opened_messages
        if "Активный участок не связан ни с одним источником" in message
    )
    assert "Режим: «Ремонт реклоузера»" in source_less
    assert "Нормальная схема" not in source_less


def test_dragging_user_waypoint_changes_only_diagram_route_and_one_history_entry() -> None:
    _app()
    controller = _controller()
    source = controller.add_equipment("builtin.load", "Нагрузка-1", x=0, y=0,
                                      voltage_class_by_group={"main": _U10})
    target = controller.add_equipment("builtin.load", "Нагрузка-2", x=180, y=0,
                                      voltage_class_by_group={"main": _U10})
    # Use the real terminals and an unobstructed bend above the bodies. A
    # centre-to-centre fixture would be correctly rejected by body clearance.
    source_port = controller._port_anchor_geometry(controller.model,
        controller.diagram.representations[source.representation_id], source.port_ids[0])
    target_port = controller._port_anchor_geometry(controller.model,
        controller.diagram.representations[target.representation_id], target.port_ids[0])
    manual_id = RouteWaypointId("waypoint.stage4.user")
    route_points = (
        RouteWaypoint(RouteWaypointId("waypoint.stage4.start"), source_port.x, source_port.y),
        RouteWaypoint(RouteWaypointId("waypoint.stage4.a"), 60, source_port.y),
        RouteWaypoint(
            manual_id,
            60,
            -80,
            source="user",
            pinned=True,
        ),
        RouteWaypoint(RouteWaypointId("waypoint.stage4.b"), target_port.x, -80),
        RouteWaypoint(RouteWaypointId("waypoint.stage4.end"), target_port.x, target_port.y),
    )
    created = controller.connect_ports(
        source.port_ids[0],
        target.port_ids[0],
        route_waypoints=route_points,
        first_representation_id=source.representation_id,
        second_representation_id=target.representation_id,
    )
    assert created.route_id is not None
    canvas = EditorCanvas(controller)
    route_item = canvas.scene._route_items_by_id[created.route_id]
    topology_before = controller.model.connectivity_signature()
    electrical_revision_before = controller.model.revision
    diagram_revision_before = controller.diagram.revision
    history_before = len(controller.journal)

    route_item.commit_user_waypoint(manual_id, QPointF(90, -100))

    updated = controller.diagram.routes[created.route_id]
    moved = [item for item in updated.waypoints if item.source.value == "user"]
    assert len(moved) == 1
    assert moved[0].id == manual_id
    assert (moved[0].x, moved[0].y) == (90.0, -100.0)
    assert moved[0].pinned
    assert controller.model.connectivity_signature() == topology_before
    assert controller.model.revision == electrical_revision_before
    assert controller.diagram.revision == diagram_revision_before + 1
    assert len(controller.journal) == history_before + 1

    canvas.scene._route_items_by_id[created.route_id].remove_user_waypoint(manual_id)
    without_manual = controller.diagram.routes[created.route_id]
    assert not [item for item in without_manual.waypoints if item.source.value == "user"]
    assert controller.model.connectivity_signature() == topology_before
    assert controller.model.revision == electrical_revision_before
    assert len(controller.journal) == history_before + 2


def test_moving_equipment_atomically_reroutes_only_incident_endpoint() -> None:
    _app()
    controller = _controller()
    source = controller.add_equipment("builtin.load", "Нагрузка-1", x=0, y=0,
                                      voltage_class_by_group={"main": _U10})
    target = controller.add_equipment("builtin.load", "Нагрузка-2", x=200, y=0,
                                      voltage_class_by_group={"main": _U10})
    manual = RouteWaypoint(
        RouteWaypointId("waypoint.stage4.move.user"),
        100,
        80,
        source="user",
        pinned=True,
    )
    created = controller.connect_ports(
        source.port_ids[0],
        target.port_ids[0],
        route_waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.stage4.move.start"), 0, 0),
            RouteWaypoint(RouteWaypointId("waypoint.stage4.move.a"), 100, 0),
            manual,
            RouteWaypoint(RouteWaypointId("waypoint.stage4.move.b"), 200, 80),
            RouteWaypoint(RouteWaypointId("waypoint.stage4.move.end"), 200, 0),
        ),
    )
    assert created.route_id is not None
    canvas = EditorCanvas(controller)
    before_route = controller.diagram.routes[created.route_id]
    topology_before = controller.model.connectivity_signature()
    electrical_revision_before = controller.model.revision
    history_before = len(controller.journal)

    canvas._move((source.representation_id,), 40.0, 0.0, False)

    after_route = controller.diagram.routes[created.route_id]
    assert after_route.waypoints != before_route.waypoints
    preserved = [item for item in after_route.waypoints if item.id == manual.id]
    assert len(preserved) == 1
    assert (preserved[0].x, preserved[0].y) == (manual.x, manual.y)
    assert controller.model.connectivity_signature() == topology_before
    assert controller.model.revision == electrical_revision_before
    assert len(controller.journal) == history_before + 1


def test_graphical_route_crossing_does_not_create_electrical_connection() -> None:
    _app()
    controller = _controller()
    first = controller.add_electrical_node("Узел A", x=-60, y=0)
    first_copy_id = controller.place_existing_node(first.node_id, x=60, y=0)
    second = controller.add_electrical_node("Узел B", x=0, y=-60)
    second_copy_id = controller.place_existing_node(second.node_id, x=0, y=60)

    def node_route(
        route_id: str,
        node_id,
        start_id,
        end_id,
        points: tuple[tuple[float, float], ...],
    ) -> DiagramRoute:
        start = RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, start_id, node_id)
        end = RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, end_id, node_id)
        return DiagramRoute(
            DiagramRouteId(route_id),
            controller.diagram.representations[start_id].page_id,
            DiagramRouteKind.NODE_CONNECTION,
            start,
            end,
            electrical_node_id=node_id,
            waypoints=tuple(
                RouteWaypoint(RouteWaypointId(f"waypoint.{route_id}.{index}"), x, y)
                for index, (x, y) in enumerate(points)
            ),
        )

    routes = (
        node_route("route.horizontal", first.node_id, first.representation_id, first_copy_id, ((-60, 0), (60, 0))),
        node_route("route.vertical", second.node_id, second.representation_id, second_copy_id, ((0, -60), (0, 60))),
    )
    topology_before = controller.model.connectivity_signature()
    controller._project.diagram = replace(
        controller.diagram,
        routes={item.id: item for item in routes},
        revision=controller.diagram.revision + 1,
    )
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)

    crossing_items = scene.items(QPointF(0, 0))
    assert sum(isinstance(item, DiagramRouteItem) for item in crossing_items) == 2
    assert not any(isinstance(item, DiagramObjectItem) for item in crossing_items)
    assert controller.model.connectivity_signature() == topology_before == ()


def test_gui_split_line_uses_physical_offset_and_one_undo(monkeypatch) -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    end = controller.add_electrical_node(
        "Конец", x=400, y=0, voltage_class_id=voltage_id
    )
    line = controller.create_physical_line(
        "ВЛ для разделения",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    history_before = len(controller.journal)

    canvas._route_context_action(
        "split_line",
        {
            "route_id": line.route_id,
            "equipment_id": line.section_id,
            "page_id": controller.diagram.routes[line.route_id].page_id,
            "scene_x": 160.0,
            "scene_y": 0.0,
            "route_fraction": 0.4,
        },
    )

    assert line.section_id not in controller.model.line_sections
    assert sorted(
        section.length_mm for section in controller.model.line_sections.values()
    ) == [4_000_000, 6_000_000]
    assert len(controller.model.line_sections) == 2
    assert len(controller.diagram.routes) == 2
    assert len(controller.journal) == history_before + 1
    controller.undo()
    assert tuple(controller.model.line_sections) == (line.section_id,)
    assert tuple(controller.diagram.routes) == (line.route_id,)


def test_gui_confirm_length_records_millimetres_and_is_undoable(monkeypatch) -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    end = controller.add_electrical_node(
        "Конец", x=400, y=0, voltage_class_id=voltage_id
    )
    line = controller.create_physical_line(
        "ВЛ с неизвестной длиной",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        QInputDialog,
        "getDouble",
        lambda *args, **kwargs: (2_500.0, True),
    )

    canvas._route_context_action(
        "confirm_length",
        {
            "route_id": line.route_id,
            "equipment_id": line.section_id,
            "page_id": controller.diagram.routes[line.route_id].page_id,
            "scene_x": 200.0,
            "scene_y": 0.0,
            "route_fraction": 0.5,
        },
    )

    section = controller.model.line_sections[line.section_id]
    assert section.length_mm == 2_500_000
    assert all(
        segment.length_confirmation is DataConfirmation.CONFIRMED
        for segment in section.construction_segments
    )
    controller.undo()
    assert controller.model.line_sections[line.section_id].length_mm is None


def test_physical_line_endpoint_handle_reconnects_same_branch_and_ids() -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    old_end = controller.add_electrical_node(
        "Старый конец", x=400, y=0, voltage_class_id=voltage_id
    )
    new_end = controller.add_electrical_node(
        "Новый конец", x=400, y=200, voltage_class_id=voltage_id
    )
    line = controller.create_physical_line(
        "Переподключаемая ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(old_end.node_id),
        physical=PhysicalLineInput(3_000_000, DataConfirmation.CONFIRMED),
    )
    to_port = controller.model.port_by_role(line.section_id, "to").id
    old_connection = controller.model.connection_for_port(to_port)
    assert old_connection is not None
    history_before = len(controller.journal)
    canvas = EditorCanvas(controller)
    route_item = canvas.scene._route_items_by_id[line.route_id]
    route_item.setSelected(True)
    assert set(route_item._endpoint_handles) == {True, False}

    assert canvas.scene.begin_route_endpoint_reconnect(
        line.route_id, at_start=False
    )
    target_representation = controller.diagram.representations[
        new_end.representation_id
    ]
    canvas.scene.update_connection_cursor(
        QPointF(target_representation.x, target_representation.y)
    )
    assert canvas.scene._connection_target is not None
    assert (
        canvas.scene._connection_target.feedback
        is ConnectionTargetFeedback.COMPATIBLE
    )
    canvas.scene._emit_connection_draft()

    updated_connection = controller.model.connection_for_port(to_port)
    assert updated_connection is not None
    assert updated_connection.id == old_connection.id
    assert updated_connection.electrical_node_id == new_end.node_id
    assert line.section_id in controller.model.line_sections
    assert controller.diagram.routes[line.route_id].end_anchor.electrical_node_id == new_end.node_id
    assert len(controller.journal) == history_before + 1
    controller.undo()
    restored_connection = controller.model.connection_for_port(to_port)
    assert restored_connection is not None
    assert restored_connection.id == old_connection.id
    assert restored_connection.electrical_node_id == old_end.node_id


def test_drop_recloser_on_line_inserts_and_context_removal_merges(monkeypatch) -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    end = controller.add_electrical_node(
        "Конец", x=400, y=0, voltage_class_id=voltage_id
    )
    controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    history_before = len(controller.journal)

    canvas._add_equipment(
        {
            "target_kind": "equipment",
            "type_id": "builtin.recloser",
            "name": "Реклоузер из библиотеки",
            "symbol_key": "recloser",
        },
        200.0,
        0.0,
    )

    reclosers = [
        item
        for item in controller.model.equipment.values()
        if item.type_id.value == "builtin.recloser"
    ]
    assert len(reclosers) == 1
    recloser = reclosers[0]
    assert recloser.extensions.get("line_insertion")
    assert len(controller.model.line_sections) == 2
    assert len(controller.journal) == history_before + 1

    canvas._equipment_context_action(
        "remove_recloser_from_line",
        {"equipment_id": recloser.id},
    )

    assert recloser.id not in controller.model.equipment
    assert len(controller.model.line_sections) == 1
    merged = next(iter(controller.model.line_sections.values()))
    assert merged.length_mm == 10_000_000
    assert len(controller.journal) == history_before + 2
    controller.undo()
    assert recloser.id in controller.model.equipment
    assert len(controller.model.line_sections) == 2


def test_gui_delete_node_connection_disconnects_ports_and_undo_restores_ids() -> None:
    _app()
    controller = _controller()
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=0, y=0,
                                     voltage_class_by_group={"main": _U10})
    second = controller.add_equipment("builtin.load", "Нагрузка 2", x=200, y=0,
                                      voltage_class_by_group={"main": _U10})
    connected = controller.connect_ports(first.port_ids[0], second.port_ids[0])
    assert connected.route_id is not None
    node_id = connected.node_id
    connection_ids = connected.connection_ids
    history_before = len(controller.journal)
    canvas = EditorCanvas(controller)

    canvas.scene.routeDeleteRequested.emit(connected.route_id)

    assert connected.route_id not in controller.diagram.routes
    assert controller.model.connection_for_port(first.port_ids[0]) is None
    assert controller.model.connection_for_port(second.port_ids[0]) is None
    assert node_id not in controller.model.electrical_nodes
    assert first.equipment_id in controller.model.equipment
    assert second.equipment_id in controller.model.equipment
    assert len(controller.journal) == history_before + 1
    controller.undo()
    assert connected.route_id in controller.diagram.routes
    assert controller.model.connection_for_port(first.port_ids[0]).id == connection_ids[0]
    assert controller.model.connection_for_port(second.port_ids[0]).id == connection_ids[1]
    assert node_id in controller.model.electrical_nodes


def test_gui_delete_physical_route_removes_line_and_orphan_nodes_atomically() -> None:
    _app()
    controller = _controller()
    voltage_id = VoltageClassId("builtin.voltage.ac.10kv")
    start = controller.add_electrical_node(
        "Начало", x=0, y=0, voltage_class_id=voltage_id
    )
    end = controller.add_electrical_node(
        "Конец", x=300, y=0, voltage_class_id=voltage_id
    )
    line = controller.create_physical_line(
        "Удаляемая ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(end.node_id),
        physical=PhysicalLineInput(2_000_000, DataConfirmation.CONFIRMED),
    )
    canvas = EditorCanvas(controller)

    canvas._route_context_action(
        "delete_route",
        {
            "route_id": line.route_id,
            "equipment_id": line.section_id,
            "page_id": controller.diagram.routes[line.route_id].page_id,
        },
    )

    assert line.route_id not in controller.diagram.routes
    assert line.section_id not in controller.model.equipment
    assert line.logical_line_id not in controller.model.logical_lines
    assert start.node_id not in controller.model.electrical_nodes
    assert end.node_id not in controller.model.electrical_nodes
    assert start.representation_id not in controller.diagram.representations
    assert end.representation_id not in controller.diagram.representations
    controller.undo()
    assert line.route_id in controller.diagram.routes
    assert line.section_id in controller.model.line_sections
    assert line.logical_line_id in controller.model.logical_lines
    assert start.node_id in controller.model.electrical_nodes
    assert end.node_id in controller.model.electrical_nodes
    assert start.representation_id in controller.diagram.representations
    assert end.representation_id in controller.diagram.representations
