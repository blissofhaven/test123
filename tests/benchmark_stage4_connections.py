# -*- coding: utf-8 -*-
"""Измеряемый стенд Этапа 4: 1000+ объектов, 1500+ связей, 200 отпаек.

Скрипт не задаёт искусственных порогов для конкретного компьютера. Он
печатает фактические времена, а также проверяет размер и целостность модели.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramPage,
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.electrical import (  # noqa: E402
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineKind,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.model import ProjectStructure  # noqa: E402
from rza_calc.editor.connection_tool import ConnectionToolState  # noqa: E402
from rza_calc.editor.controller import ProjectEditorController  # noqa: E402
from rza_calc.editor.orthogonal_routing import (  # noqa: E402
    RouteDirection,
    RouteVertex,
    RoutingObstacle,
    RoutingRequest,
    build_orthogonal_route,
)
from rza_calc.gui.editor_scene import (  # noqa: E402
    DiagramGraphicsScene,
    DiagramGraphicsView,
)
from rza_calc.io.project import load_project, save_project  # noqa: E402
from rza_calc.topology import TopologyEngine  # noqa: E402


EXAMPLE = ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json"
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.benchmark.stage4.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _endpoints(
    model: ElectricalModel, equipment_id: EquipmentId
) -> tuple[ElectricalNodeId, ElectricalNodeId]:
    equipment = model.equipment[equipment_id]
    rows = []
    for port_id in equipment.port_ids:
        connection = model.connection_for_port(port_id)
        if connection is not None:
            rows.append(connection.electrical_node_id)
    if len(rows) != 2:
        raise AssertionError(f"Оборудование {equipment_id.value} не имеет двух концов")
    return rows[0], rows[1]


def build_large_model() -> ElectricalModel:
    model = ElectricalModel.with_builtins("Стенд Этапа 4")
    start = _node(model, "main.start")
    finish = _node(model, "main.finish")
    main, section, _ = model.create_logical_line(
        "Магистральная ВЛ",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        201_000_000,
        voltage_class_id=U10,
        inherited_properties={"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
    )
    current_section = section.equipment_id
    branch_sections: list[EquipmentId] = []
    tap_node_ids: list[ElectricalNodeId] = []
    for index in range(200):
        branch_end = _node(model, f"tap.{index:03d}.end")
        split, _, branch, _ = model.create_tap_line(
            current_section,
            1_000_000,
            f"Отпайка {index + 1}",
            LineKind.CABLE,
            branch_end.id,
            1_000_000,
            branch_inherited_properties={
                "r1_ohm_per_km": 0.2,
                "x1_ohm_per_km": 0.1,
            },
        )
        branch_sections.append(branch.equipment_id)
        tap_node_ids.append(split.tap_node_id)
        current_section = split.second_section_id

    for index, branch_id in enumerate(branch_sections[:50]):
        model.insert_recloser_in_line(
            branch_id,
            500_000,
            f"Реклоузер {index + 1}",
            properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
        )

    usable_nodes = tuple(model.electrical_nodes)
    for index in range(498):
        breaker, _ = model.create_equipment(
            "builtin.circuit_breaker",
            f"QF-{index + 1}",
            voltage_class_by_group={"main": U10},
            normal_position=SwitchPosition.CLOSED,
        )
        first = usable_nodes[index % len(usable_nodes)]
        second = usable_nodes[(index + 1) % len(usable_nodes)]
        model.connect_port(breaker.port_ids[0], first)
        model.connect_port(breaker.port_ids[1], second)

    source, _ = model.create_equipment(
        "builtin.external_grid",
        "Источник стенда",
        voltage_class_by_group={"main": U10},
    )
    model.connect_port(source.port_ids[0], start.id)

    assert len(model.equipment) == 1_000
    assert len(model.connections) >= 1_500
    assert len(tap_node_ids) == 200
    assert sum(
        item.type_id.value == "builtin.recloser"
        for item in model.equipment.values()
    ) == 50
    assert len(model.logical_lines[main.id].section_equipment_ids) == 201
    assert not [item for item in model.validate_integrity() if item.severity == "error"]
    return model


def build_large_diagram(model: ElectricalModel) -> DiagramDocument:
    page = DiagramPage(PageId("page.benchmark.stage4"), "Большая схема")
    representations: list[GraphicalRepresentation] = []
    node_representation: dict[ElectricalNodeId, GraphicalRepresentationId] = {}
    for index, node in enumerate(model.electrical_nodes.values()):
        representation_id = GraphicalRepresentationId(
            f"representation.benchmark.node.{index:04d}"
        )
        node_representation[node.id] = representation_id
        representations.append(GraphicalRepresentation(
            representation_id,
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node.id,
            x=float((index % 40) * 80),
            y=float((index // 40) * 80),
            symbol_key="electrical_node",
            label=node.name,
        ))
    for index, equipment in enumerate(model.equipment.values()):
        representations.append(GraphicalRepresentation(
            GraphicalRepresentationId(
                f"representation.benchmark.equipment.{index:04d}"
            ),
            page.id,
            RepresentationTargetKind.EQUIPMENT,
            equipment_id=equipment.id,
            x=float((index % 50) * 70),
            y=float(1_200 + (index // 50) * 70),
            symbol_key=equipment.type_id.value,
            label=equipment.name,
        ))

    routes: list[DiagramRoute] = []
    for index, section in enumerate(model.line_sections.values()):
        equipment = model.equipment[section.equipment_id]
        start_node, finish_node = _endpoints(model, equipment.id)
        from_port = model.port_by_role(equipment.id, "from").id
        to_port = model.port_by_role(equipment.id, "to").id
        y = float(index * 6)
        waypoints = tuple(
            RouteWaypoint(
                RouteWaypointId(f"waypoint.benchmark.{index:04d}.{point_index}"),
                x,
                point_y,
            )
            for point_index, (x, point_y) in enumerate((
                (0.0, y),
                (20.0, y),
                (20.0, y + 12.0),
                (50.0, y + 12.0),
                (50.0, y + 24.0),
                (80.0, y + 24.0),
            ))
        )
        routes.append(DiagramRoute(
            DiagramRouteId(f"route.benchmark.{index:04d}"),
            page.id,
            DiagramRouteKind.EQUIPMENT_BRANCH,
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                node_representation[start_node],
                start_node,
                branch_port_id=from_port,
            ),
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                node_representation[finish_node],
                finish_node,
                branch_port_id=to_port,
            ),
            equipment_id=equipment.id,
            waypoints=waypoints,
            routing_algorithm_version=2,
        ))
    return DiagramDocument.create(
        "Большая схема Этапа 4",
        (page,),
        representations,
        routes=routes,
    )


def run_benchmark() -> dict[str, float | int]:
    started = perf_counter()
    model = build_large_model()
    diagram = build_large_diagram(model)
    build_seconds = perf_counter() - started

    project = load_project(EXAMPLE)
    project.electrical_model = model
    project.diagram = diagram
    project.catalog_snapshots = ProjectCatalogSnapshots()
    project.structure = ProjectStructure()
    project.refresh_calculation_view(force=True)

    target = Path(tempfile.mkdtemp()) / "stage4-benchmark.json"
    started = perf_counter()
    save_project(target, project)
    save_seconds = perf_counter() - started
    started = perf_counter()
    reopened = load_project(target)
    open_seconds = perf_counter() - started

    application = QApplication.instance() or QApplication([])
    scene = DiagramGraphicsScene()
    started = perf_counter()
    scene.sync_document(reopened.diagram, reopened.electrical_model)
    scene_open_seconds = perf_counter() - started
    view = DiagramGraphicsView(scene)
    started = perf_counter()
    for value in (0.25, 0.5, 1.0, 1.5, 2.0, 1.0) * 8:
        view.set_zoom(value)
    zoom_seconds = perf_counter() - started
    started = perf_counter()
    for _ in range(100):
        view.translate(2.0, 1.0)
    pan_seconds = perf_counter() - started

    connection_tool = ConnectionToolState()
    started = perf_counter()
    connection_tool.begin(
        source_port_id="port.benchmark.preview",
        source_representation_id="representation.benchmark.preview",
        x=0.0,
        y=0.0,
        direction=RouteDirection.RIGHT,
    )
    connection_tool.update(400.0, 180.0)
    connection_start_seconds = perf_counter() - started

    obstacles = tuple(
        RoutingObstacle(40.0 + index * 25.0, -20.0, 55.0 + index * 25.0, 80.0)
        for index in range(12)
    )
    started = perf_counter()
    for index in range(100):
        build_orthogonal_route(RoutingRequest(
            RouteVertex(0.0, float(index)),
            RouteVertex(500.0, 180.0 + float(index)),
            RouteDirection.RIGHT,
            RouteDirection.LEFT,
            obstacles=obstacles,
        ))
    local_reroute_seconds = perf_counter() - started

    engine = TopologyEngine()
    started = perf_counter()
    topology = engine.compile(reopened.electrical_model)
    topology_seconds = perf_counter() - started
    nodes = tuple(reopened.electrical_model.electrical_nodes)
    started = perf_counter()
    for index in range(min(200, len(nodes) - 1)):
        topology.has_path(nodes[index], nodes[index + 1])
    local_topology_queries_seconds = perf_counter() - started

    controller = ProjectEditorController(reopened)
    first = controller.add_equipment("builtin.load", "Тест завершения 1")
    second = controller.add_equipment("builtin.load", "Тест завершения 2")
    started = perf_counter()
    controller.connect_ports(first.port_ids[0], second.port_ids[0])
    connection_finish_seconds = perf_counter() - started

    return {
        "equipment": len(reopened.electrical_model.equipment),
        "connections": len(reopened.electrical_model.connections),
        "tap_nodes": sum(
            node.extensions.get("junction_kind") == "line_tap"
            for node in reopened.electrical_model.electrical_nodes.values()
        ),
        "reclosers": sum(
            item.type_id.value == "builtin.recloser"
            for item in reopened.electrical_model.equipment.values()
        ),
        "routes": len(reopened.diagram.routes),
        "orthogonal_segments": sum(
            len(item.waypoints) - 1 for item in reopened.diagram.routes.values()
        ),
        "build_seconds": round(build_seconds, 6),
        "save_seconds": round(save_seconds, 6),
        "open_seconds": round(open_seconds, 6),
        "scene_open_seconds": round(scene_open_seconds, 6),
        "zoom_48_operations_seconds": round(zoom_seconds, 6),
        "pan_100_operations_seconds": round(pan_seconds, 6),
        "connection_start_seconds": round(connection_start_seconds, 6),
        "connection_finish_seconds": round(connection_finish_seconds, 6),
        "local_reroute_100_operations_seconds": round(local_reroute_seconds, 6),
        "topology_compile_seconds": round(topology_seconds, 6),
        "local_topology_queries_seconds": round(
            local_topology_queries_seconds, 6
        ),
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), ensure_ascii=False, indent=2))
