# -*- coding: utf-8 -*-
"""Воспроизводимый offscreen-бенчмарк взаимодействий этапа UI-UX-1.

Стенд использует детерминированную модель Этапа 4: ровно 1000 экземпляров
оборудования, не менее 1500 электрических соединений и 1500 сохраняемых
графических трасс. Окно не показывается. Помимо лёгких Qt-preview операций,
стенд измеряет полный поворот через ``ProjectEditorController`` с локальной
перестройкой трасс, историей, undo/redo, создание и повторное использование
снимка коллизий, а также массовое выделение в настоящем рабочем поле.

Для каждого жеста печатаются медианное и худшее время отдельной реакции;
абсолютные пороги намеренно не задаются, поскольку результат зависит от
компьютера и Qt-платформы.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import ceil
from pathlib import Path
from statistics import median
from time import perf_counter, perf_counter_ns
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, Qt, qVersion  # noqa: E402
from PySide6.QtGui import QPainterPath  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    RepresentationTargetKind,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor.collision import (  # noqa: E402
    DiagramCollisionService,
    geometry_for_equipment_preview,
)
from rza_calc.editor.controller import ProjectEditorController  # noqa: E402
from rza_calc.gui.editor_panels import EditorWorkspaceWidget  # noqa: E402
from rza_calc.gui.editor_scene import (  # noqa: E402
    DiagramGraphicsScene,
    DiagramGraphicsView,
)

try:  # Прямой запуск: python tests/benchmark_ui_interaction.py
    from benchmark_stage4_connections import (  # type: ignore[import-not-found]  # noqa: E402
        build_large_diagram,
        build_large_model,
    )
except ImportError:  # Импорт как tests.benchmark_ui_interaction
    from tests.benchmark_stage4_connections import (  # type: ignore[import-not-found]  # noqa: E402
        build_large_diagram,
        build_large_model,
    )


SAMPLE_COUNT = 100
COLLISION_SNAPSHOT_SAMPLE_COUNT = 5
CONTROLLER_ROTATION_SAMPLE_COUNT = 100
WORKSPACE_SELECTION_SAMPLE_COUNT = 3


@dataclass
class _ControllerProject:
    electrical_model: Any
    diagram: Any
    catalog_snapshots: ProjectCatalogSnapshots


def _summary(samples_ms: list[float]) -> dict[str, int | float]:
    ordered = sorted(samples_ms)
    p95_index = max(0, min(len(ordered) - 1, ceil(len(ordered) * 0.95) - 1))
    return {
        "измерений": len(ordered),
        "медиана_мс": round(median(ordered), 6),
        "p95_мс": round(ordered[p95_index], 6),
        "худшее_мс": round(ordered[-1], 6),
    }


def _measure(
    count: int,
    operation: Callable[[int], Any],
) -> dict[str, int | float]:
    samples_ms: list[float] = []
    for index in range(count):
        started = perf_counter_ns()
        operation(index)
        samples_ms.append((perf_counter_ns() - started) / 1_000_000.0)
    return _summary(samples_ms)


def _diagram_geometry_signature(diagram: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted(
        (
            item.id.value,
            item.page_id.value,
            item.x,
            item.y,
            item.rotation_deg,
            item.z_index,
        )
        for item in diagram.representations.values()
    ))


def _prepare_ui_layout(diagram: Any) -> Any:
    """Разнести корпуса, не меняя ID, маршруты или электрическую модель."""

    equipment_index = 0
    representations = {}
    for representation_id, item in diagram.representations.items():
        if item.equipment_id is None:
            representations[representation_id] = item
            continue
        representations[representation_id] = replace(
            item,
            x=float((equipment_index % 40) * 140),
            y=float(1_800 + (equipment_index // 40) * 140),
        )
        equipment_index += 1
    return replace(diagram, representations=representations)


def _with_connected_rotation_target(
    model: Any,
    diagram: Any,
) -> tuple[Any, GraphicalRepresentationId, tuple[DiagramRouteId, ...]]:
    """Добавить две реальные портовые трассы существующему выключателю.

    Большой стенд уже содержит подключённые аппараты, но его исходные
    агрегированные трассы заканчиваются на представлениях электрических узлов.
    Для честного измерения локального reroute создаём две графические трассы,
    чьи якоря явно ссылаются на смысловые ``PortId`` выбранного аппарата.
    Электрическая модель при этом не изменяется.
    """

    equipment = next(
        item
        for item in model.equipment.values()
        if item.type_id.value == "builtin.circuit_breaker"
    )
    equipment_representation = next(
        item
        for item in diagram.representations.values()
        if item.equipment_id == equipment.id
    )
    node_representations = {
        item.electrical_node_id: item
        for item in diagram.representations.values()
        if item.electrical_node_id is not None
    }
    routes = dict(diagram.routes)
    route_ids: list[DiagramRouteId] = []
    for index, port_id in enumerate(equipment.port_ids):
        connection = model.connection_for_port(port_id)
        if connection is None:
            raise AssertionError("Выбранный аппарат должен быть подключён с двух сторон.")
        node_representation = node_representations[connection.electrical_node_id]
        route_id = DiagramRouteId(
            f"route.benchmark.ui.rotation.{index}"
        )
        middle_x = (
            equipment_representation.x + node_representation.x
        ) / 2.0
        points = (
            (equipment_representation.x, equipment_representation.y),
            (middle_x, equipment_representation.y),
            (middle_x, node_representation.y),
            (node_representation.x, node_representation.y),
        )
        routes[route_id] = DiagramRoute(
            route_id,
            equipment_representation.page_id,
            DiagramRouteKind.NODE_CONNECTION,
            RouteEndpointAnchor(
                RouteAnchorKind.EQUIPMENT_PORT,
                equipment_representation.id,
                connection.electrical_node_id,
                target_port_id=port_id,
                anchor_key=model.ports[port_id].role,
            ),
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                node_representation.id,
                connection.electrical_node_id,
            ),
            electrical_node_id=connection.electrical_node_id,
            waypoints=tuple(
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.rotation.{index}.{point_index}"
                    ),
                    x,
                    y,
                )
                for point_index, (x, y) in enumerate(points)
            ),
            routing_algorithm_version=2,
        )
        route_ids.append(route_id)
    if len(route_ids) != 2:
        raise AssertionError("Для стенда поворота нужны ровно две портовые трассы.")
    return (
        replace(diagram, routes=routes, revision=diagram.revision + 1),
        equipment_representation.id,
        tuple(route_ids),
    )


def _ensure_diagram_route_scale(model: Any, diagram: Any) -> Any:
    """Довести графический стенд до 1500 валидных сохраняемых трасс."""

    routes = dict(diagram.routes)
    representations = dict(diagram.representations)
    node_representations = {
        item.electrical_node_id: item
        for item in diagram.representations.values()
        if item.electrical_node_id is not None
    }
    routed_equipment = {
        item.equipment_id
        for item in routes.values()
        if item.equipment_id is not None
    }

    branch_index = 0
    for equipment in model.equipment.values():
        if equipment.id in routed_equipment:
            continue
        endpoints = []
        for port_id in equipment.port_ids:
            connection = model.connection_for_port(port_id)
            if connection is not None:
                endpoints.append((port_id, connection.electrical_node_id))
        if (
            len(endpoints) != 2
            or endpoints[0][1] == endpoints[1][1]
            or any(node_id not in node_representations for _, node_id in endpoints)
        ):
            continue
        base_x = float(7_000 + (branch_index % 40) * 90)
        base_y = float(100 + (branch_index // 40) * 70)
        route_id = DiagramRouteId(
            f"route.benchmark.ui.branch.{branch_index:04d}"
        )
        routes[route_id] = DiagramRoute(
            route_id,
            diagram.pages[next(iter(diagram.pages))].id,
            DiagramRouteKind.EQUIPMENT_BRANCH,
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                node_representations[endpoints[0][1]].id,
                endpoints[0][1],
                branch_port_id=endpoints[0][0],
            ),
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                node_representations[endpoints[1][1]].id,
                endpoints[1][1],
                branch_port_id=endpoints[1][0],
            ),
            equipment_id=equipment.id,
            waypoints=(
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.branch.{branch_index:04d}.0"
                    ),
                    base_x,
                    base_y,
                ),
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.branch.{branch_index:04d}.1"
                    ),
                    base_x + 40.0,
                    base_y,
                ),
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.branch.{branch_index:04d}.2"
                    ),
                    base_x + 40.0,
                    base_y + 30.0,
                ),
            ),
            routing_algorithm_version=2,
        )
        routed_equipment.add(equipment.id)
        branch_index += 1

    node_rows = tuple(sorted(
        node_representations.items(), key=lambda row: row[0].value
    ))
    link_index = 0
    while len(routes) < 1_500:
        node_id, original = node_rows[link_index % len(node_rows)]
        base_x = float(11_000 + (link_index % 30) * 90)
        base_y = float(100 + (link_index // 30) * 80)
        representation_id = GraphicalRepresentationId(
            f"representation.benchmark.ui.node-link.{link_index:04d}"
        )
        representations[representation_id] = GraphicalRepresentation(
            representation_id,
            original.page_id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_id,
            x=base_x + 60.0,
            y=base_y + 30.0,
            symbol_key="electrical_node",
            label=f"Межсхемное представление {link_index + 1}",
        )
        route_id = DiagramRouteId(
            f"route.benchmark.ui.node-link.{link_index:04d}"
        )
        routes[route_id] = DiagramRoute(
            route_id,
            original.page_id,
            DiagramRouteKind.NODE_CONNECTION,
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                original.id,
                node_id,
            ),
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                representation_id,
                node_id,
            ),
            electrical_node_id=node_id,
            waypoints=(
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.node-link.{link_index:04d}.0"
                    ),
                    base_x,
                    base_y,
                ),
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.node-link.{link_index:04d}.1"
                    ),
                    base_x + 30.0,
                    base_y,
                ),
                RouteWaypoint(
                    RouteWaypointId(
                        f"waypoint.benchmark.ui.node-link.{link_index:04d}.2"
                    ),
                    base_x + 30.0,
                    base_y + 30.0,
                ),
            ),
            routing_algorithm_version=2,
        )
        link_index += 1

    return replace(
        diagram,
        representations=representations,
        routes=routes,
    )


def run_benchmark() -> dict[str, object]:
    build_started = perf_counter()
    model = build_large_model()
    diagram = _prepare_ui_layout(
        _ensure_diagram_route_scale(model, build_large_diagram(model))
    )
    (
        diagram,
        rotation_representation_id,
        rotation_route_ids,
    ) = _with_connected_rotation_target(model, diagram)
    build_ms = (perf_counter() - build_started) * 1_000.0

    equipment_count = len(model.equipment)
    connection_count = len(model.connections)
    if equipment_count != 1_000:
        raise AssertionError(
            f"Ожидалось 1000 объектов оборудования, получено {equipment_count}."
        )
    if connection_count < 1_500:
        raise AssertionError(
            "Ожидалось не менее 1500 электрических соединений, "
            f"получено {connection_count}."
        )
    if len(diagram.routes) < 1_500:
        raise AssertionError(
            "Ожидалось не менее 1500 графических трасс, "
            f"получено {len(diagram.routes)}."
        )

    fingerprint_before = electrical_model_fingerprint(model)
    model_revision_before = model.revision
    diagram_revision_before = diagram.revision
    diagram_geometry_before = _diagram_geometry_signature(diagram)

    collision_started = perf_counter()
    collision = DiagramCollisionService(diagram, model)
    collision_snapshot_ms = (perf_counter() - collision_started) * 1_000.0
    collision_snapshot_metric = _measure(
        COLLISION_SNAPSHOT_SAMPLE_COUNT,
        lambda _index: DiagramCollisionService(diagram, model),
    )

    rotation_equipment_id = diagram.representations[
        rotation_representation_id
    ].equipment_id
    if rotation_equipment_id is None:
        raise AssertionError("Цель поворота должна представлять оборудование.")
    rotation_equipment = model.equipment[rotation_equipment_id]
    preview_definition = model.equipment_type(
        rotation_equipment.type_id,
        rotation_equipment.type_version,
    )
    page_id = next(iter(diagram.pages))
    preview_allowed = 0

    def preview_once(index: int) -> None:
        nonlocal preview_allowed
        candidate = geometry_for_equipment_preview(
            preview_definition,
            page_id=page_id,
            x=float((index % 40) * 140 + 35),
            y=float(1_800 + (index // 40) * 140 + 35),
            rotation_deg=float((index % 4) * 90),
            display_name="Предпросмотр выключателя",
        )
        result = collision.check_placement(candidate)
        preview_allowed += int(result.allowed)

    preview_reuse_metric = _measure(SAMPLE_COUNT, preview_once)

    controller_project = _ControllerProject(
        model,
        diagram,
        ProjectCatalogSnapshots(),
    )
    controller = ProjectEditorController(controller_project)
    controller_fingerprint = electrical_model_fingerprint(controller.model)

    application = QApplication.instance() or QApplication([])
    scene = DiagramGraphicsScene()
    scene_started = perf_counter()
    scene.sync_document(diagram, model)
    scene_sync_ms = (perf_counter() - scene_started) * 1_000.0
    view = DiagramGraphicsView(scene)
    view.resize(1280, 720)

    equipment_representation_ids = tuple(sorted(
        (
            item.id
            for item in diagram.representations.values()
            if (
                item.equipment_id is not None
                and collision.geometries[item.id].body_collision_shape is not None
            )
        ),
        key=lambda item: item.value,
    ))
    if len(equipment_representation_ids) < SAMPLE_COUNT:
        raise AssertionError("Недостаточно представлений для 100 взаимодействий.")
    all_equipment_representation_ids = tuple(sorted(
        (
            item.id
            for item in diagram.representations.values()
            if item.equipment_id is not None
        ),
        key=lambda item: item.value,
    ))
    if len(all_equipment_representation_ids) != equipment_count:
        raise AssertionError(
            "Каждый объект оборудования должен иметь представление на стенде."
        )

    move_allowed = 0

    def move_once(index: int) -> None:
        nonlocal move_allowed
        group = tuple(
            equipment_representation_ids[
                (index * 3 + offset) % len(equipment_representation_ids)
            ]
            for offset in range(3)
        )
        dx = float((index % 5) * 4 + 4)
        dy = float(((index // 5) % 3 - 1) * 4)
        result = collision.check_move(group, dx, dy)
        move_allowed += int(result.allowed)

        originals = {
            representation_id: QPointF(scene._items_by_id[representation_id].pos())
            for representation_id in group
        }
        for representation_id, original in originals.items():
            scene._items_by_id[representation_id].setPos(
                original + QPointF(dx, dy)
            )
        application.processEvents()
        for representation_id, original in originals.items():
            scene._items_by_id[representation_id].setPos(original)

    move_metric = _measure(SAMPLE_COUNT, move_once)

    route_anchor_ids = tuple(sorted(
        {
            anchor.representation_id
            for route in diagram.routes.values()
            for anchor in (route.start_anchor, route.end_anchor)
        },
        key=lambda item: item.value,
    ))
    if len(route_anchor_ids) < SAMPLE_COUNT:
        raise AssertionError("Недостаточно привязок трасс для 100 перемещений.")

    def route_move_once(index: int) -> None:
        representation_id = route_anchor_ids[index]
        item = scene._items_by_id[representation_id]
        original = QPointF(item.pos())
        scene._start_positions = {representation_id: QPointF(original)}
        item.setPos(original + QPointF(12.0, 8.0))
        scene._preview_incident_routes()
        application.processEvents()
        item.setPos(original)
        scene._preview_incident_routes()
        scene._start_positions.clear()

    route_move_metric = _measure(SAMPLE_COUNT, route_move_once)

    rotation_allowed = 0

    def rotate_once(index: int) -> None:
        nonlocal rotation_allowed
        representation_id = equipment_representation_ids[index]
        geometry = collision.geometries[representation_id]
        target_angle = (geometry.rotation_deg + 90.0) % 360.0
        result = collision.check_rotation(representation_id, target_angle)
        rotation_allowed += int(result.allowed)

        item = scene._items_by_id[representation_id]
        original = item.rotation()
        item.setRotation(target_angle)
        application.processEvents()
        item.setRotation(original)

    rotation_metric = _measure(SAMPLE_COUNT, rotate_once)

    zoom_values = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 1.0)

    def zoom_once(index: int) -> None:
        view.set_zoom(zoom_values[index % len(zoom_values)])
        application.processEvents()

    zoom_metric = _measure(SAMPLE_COUNT, zoom_once)

    selection_counts: list[int] = []

    def marquee_once(index: int) -> None:
        column = index % 10
        row = (index // 10) % 10
        path = QPainterPath()
        path.addRect(QRectF(
            float(column * 520),
            float(1_760 + row * 320),
            420.0,
            260.0,
        ))
        scene.clearSelection()
        scene.setSelectionArea(path)
        application.processEvents()
        selection_counts.append(len(scene.selectedItems()))

    marquee_metric = _measure(SAMPLE_COUNT, marquee_once)

    geometries = tuple(collision.geometries.values())

    def local_query_once(index: int) -> None:
        geometry = geometries[index % len(geometries)]
        collision.index.query(page_id, geometry.selection_shape)

    local_query_metric = _measure(SAMPLE_COUNT, local_query_once)

    # Настоящий путь Ctrl+A измеряется на полной offscreen-сцене. Отдельно
    # измеряется пакетная синхронизация дерева, Inspector и счётчика
    # диагностики в EditorWorkspaceWidget.
    scene_all_ids = tuple(sorted(
        scene._items_by_id,
        key=lambda item: item.value,
    ))
    ctrl_a_samples: list[float] = []
    ctrl_a_counts: list[int] = []
    for _index in range(WORKSPACE_SELECTION_SAMPLE_COUNT):
        scene.clearSelection()
        application.processEvents()
        started = perf_counter_ns()
        QTest.keyClick(
            view,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.ControlModifier,
        )
        application.processEvents()
        ctrl_a_samples.append((perf_counter_ns() - started) / 1_000_000.0)
        ctrl_a_counts.append(len(scene.selected_representation_ids()))
    if any(count != len(scene_all_ids) for count in ctrl_a_counts):
        raise AssertionError("Ctrl+A выбрал не все объекты offscreen-сцены.")

    controller_rotate_samples: list[float] = []
    controller_undo_samples: list[float] = []
    controller_redo_samples: list[float] = []
    rerouted_rotation_count = 0
    undo_exact_count = 0
    redo_exact_count = 0
    controller_journal_before = len(controller.journal)

    for _index in range(CONTROLLER_ROTATION_SAMPLE_COUNT):
        representation_before = controller.diagram.representations[
            rotation_representation_id
        ]
        routes_before = tuple(
            controller.diagram.routes[route_id]
            for route_id in rotation_route_ids
        )
        target_angle = (representation_before.rotation_deg + 90.0) % 360.0

        started = perf_counter_ns()
        rotated_to = controller.rotate_representation(
            rotation_representation_id,
            target_angle,
        )
        controller_rotate_samples.append(
            (perf_counter_ns() - started) / 1_000_000.0
        )
        representation_after = controller.diagram.representations[
            rotation_representation_id
        ]
        routes_after = tuple(
            controller.diagram.routes[route_id]
            for route_id in rotation_route_ids
        )
        if routes_after != routes_before:
            rerouted_rotation_count += 1
        if rotated_to != target_angle or representation_after.rotation_deg != target_angle:
            raise AssertionError("Контроллер не зафиксировал требуемый угол.")
        if electrical_model_fingerprint(controller.model) != controller_fingerprint:
            raise AssertionError("Поворот изменил электрический fingerprint.")

        started = perf_counter_ns()
        controller.undo()
        controller_undo_samples.append(
            (perf_counter_ns() - started) / 1_000_000.0
        )
        if (
            controller.diagram.representations[rotation_representation_id]
            == representation_before
            and tuple(
                controller.diagram.routes[route_id]
                for route_id in rotation_route_ids
            )
            == routes_before
        ):
            undo_exact_count += 1
        if electrical_model_fingerprint(controller.model) != controller_fingerprint:
            raise AssertionError(
                "Отмена поворота изменила электрический отпечаток."
            )

        started = perf_counter_ns()
        controller.redo()
        controller_redo_samples.append(
            (perf_counter_ns() - started) / 1_000_000.0
        )
        if (
            controller.diagram.representations[rotation_representation_id]
            == representation_after
            and tuple(
                controller.diagram.routes[route_id]
                for route_id in rotation_route_ids
            )
            == routes_after
        ):
            redo_exact_count += 1
        if electrical_model_fingerprint(controller.model) != controller_fingerprint:
            raise AssertionError(
                "Повтор поворота изменил электрический отпечаток."
            )

    if rerouted_rotation_count != CONTROLLER_ROTATION_SAMPLE_COUNT:
        raise AssertionError("Полный поворот не перестроил портовые трассы.")
    if undo_exact_count != CONTROLLER_ROTATION_SAMPLE_COUNT:
        raise AssertionError("Отмена не восстановила угол и трассы точно.")
    if redo_exact_count != CONTROLLER_ROTATION_SAMPLE_COUNT:
        raise AssertionError("Повтор не восстановил угол и трассы точно.")

    workspace_started = perf_counter()
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1500, 900)
    application.processEvents()
    workspace_build_ms = (perf_counter() - workspace_started) * 1_000.0
    workspace_all_ids = tuple(sorted(
        workspace.scene._items_by_id,
        key=lambda item: item.value,
    ))
    if len(workspace_all_ids) < equipment_count:
        raise AssertionError("Рабочее поле содержит меньше объектов, чем модель.")

    batch_selection_samples: list[float] = []
    batch_selection_counts: list[int] = []
    for _index in range(WORKSPACE_SELECTION_SAMPLE_COUNT):
        workspace.scene.clearSelection()
        application.processEvents()
        started = perf_counter_ns()
        workspace.scene.select_representations(all_equipment_representation_ids)
        application.processEvents()
        batch_selection_samples.append(
            (perf_counter_ns() - started) / 1_000_000.0
        )
        batch_selection_counts.append(
            len(workspace.scene.selected_representation_ids())
        )
    if any(count != equipment_count for count in batch_selection_counts):
        raise AssertionError("Пакетная команда выбрала не все 1000 объектов оборудования.")

    scene.clearSelection()
    view.set_zoom(1.0)
    application.processEvents()

    fingerprint_after = electrical_model_fingerprint(model)
    diagram_geometry_after = _diagram_geometry_signature(diagram)
    invariants = {
        "электрический_отпечаток_не_изменён": fingerprint_after == fingerprint_before,
        "ревизия_электрической_модели_не_изменена": model.revision == model_revision_before,
        "ревизия_схемы_не_изменена": diagram.revision == diagram_revision_before,
        "геометрия_схемы_не_изменена": diagram_geometry_after == diagram_geometry_before,
        "поворот_отмена_повтор_не_изменили_электрический_отпечаток": (
            electrical_model_fingerprint(controller.model)
            == controller_fingerprint
        ),
        "каждый_полный_поворот_перестроил_трассы": (
            rerouted_rotation_count == CONTROLLER_ROTATION_SAMPLE_COUNT
        ),
        "отмена_точно_восстановила_угол_и_трассы": (
            undo_exact_count == CONTROLLER_ROTATION_SAMPLE_COUNT
        ),
        "повтор_точно_восстановил_угол_и_трассы": (
            redo_exact_count == CONTROLLER_ROTATION_SAMPLE_COUNT
        ),
        "массовое_выделение_не_изменило_электрическую_модель": (
            electrical_model_fingerprint(controller.model)
            == controller_fingerprint
        ),
        "история_содержит_поворот_отмену_и_повтор": (
            len(controller.journal) - controller_journal_before
            == CONTROLLER_ROTATION_SAMPLE_COUNT * 3
        ),
    }
    if not all(invariants.values()):
        raise AssertionError(
            "UI-бенчмарк изменил электрическую модель или документ схемы."
        )

    result: dict[str, object] = {
        "окружение": {
            "python": platform.python_version(),
            "qt": qVersion(),
            "платформа": platform.platform(),
            "qt_платформа": os.environ.get("QT_QPA_PLATFORM", ""),
            "интерактивное_окно_показано": False,
        },
        "масштаб_стенда": {
            "оборудование": equipment_count,
            "электрические_соединения": connection_count,
            "графические_представления": len(diagram.representations),
            "графические_маршруты": len(diagram.routes),
            "представления_с_твёрдым_корпусом": len(equipment_representation_ids),
        },
        "подготовка_мс": {
            "модель_и_схема": round(build_ms, 3),
            "первый_снимок_коллизий": round(collision_snapshot_ms, 3),
            "синхронизация_сцены": round(scene_sync_ms, 3),
            "создание_рабочего_поля_без_вывода_окна": round(workspace_build_ms, 3),
        },
        "реакции": {
            "групповые_перемещения_100": move_metric,
            "перемещения_с_перестройкой_трасс_100": route_move_metric,
            "лёгкие_предпросмотры_поворота_100": rotation_metric,
            f"полные_повороты_через_контроллер_{CONTROLLER_ROTATION_SAMPLE_COUNT}": _summary(
                controller_rotate_samples
            ),
            f"отмена_после_полного_поворота_{CONTROLLER_ROTATION_SAMPLE_COUNT}": _summary(
                controller_undo_samples
            ),
            f"повтор_после_полного_поворота_{CONTROLLER_ROTATION_SAMPLE_COUNT}": _summary(
                controller_redo_samples
            ),
            "создание_нового_снимка_коллизий_5": collision_snapshot_metric,
            "предпросмотр_с_повторным_использованием_снимка_100": (
                preview_reuse_metric
            ),
            "масштабирование_100": zoom_metric,
            "рамочное_выделение_100": marquee_metric,
            "пространственный_поиск_100": local_query_metric,
            "ctrl_a_полной_сцены_без_вывода_окна_3": _summary(ctrl_a_samples),
            "пакетное_выделение_1000_в_рабочем_поле_3": _summary(
                batch_selection_samples
            ),
        },
        "результаты_операций": {
            "перемещений_разрешено": move_allowed,
            "перемещений_отклонено": SAMPLE_COUNT - move_allowed,
            "поворотов_разрешено": rotation_allowed,
            "поворотов_отклонено": SAMPLE_COUNT - rotation_allowed,
            "предпросмотров_размещения_разрешено": preview_allowed,
            "предпросмотров_размещения_отклонено": SAMPLE_COUNT - preview_allowed,
            "полных_поворотов_с_перестройкой_трасс": rerouted_rotation_count,
            "отмена_восстановила_точно": undo_exact_count,
            "повтор_восстановил_точно": redo_exact_count,
            "записей_поворот_отмена_повтор_добавлено_в_журнал": (
                len(controller.journal) - controller_journal_before
            ),
            "рамкой_выбрано_минимум": min(selection_counts, default=0),
            "рамкой_выбрано_максимум": max(selection_counts, default=0),
            "ctrl_a_выбрано_объектов_сцены": ctrl_a_counts[-1],
            "пакетно_выбрано_оборудования": batch_selection_counts[-1],
        },
        "ограничения_измерения": {
            "ctrl_a_рабочего_поля": (
                "Фактическое сочетание клавиш измерено на полной сцене без "
                "вывода окна; синхронизация дерева, Inspector и счётчика "
                "диагностики измерена отдельно единым пакетным выбором."
            ),
        },
        "инварианты": invariants,
    }

    workspace.close()
    workspace.deleteLater()
    view.close()
    view.deleteLater()
    scene.deleteLater()
    application.processEvents()
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = run_benchmark()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
