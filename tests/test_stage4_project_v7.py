# -*- coding: utf-8 -*-
"""Формат проекта v7 и отдельная сохраняемая модель графических трасс."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rza_calc.core.methodology import Methodology
from rza_calc.domain.diagram import (
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
    RouteWaypointSource,
)
from rza_calc.domain.electrical import (
    DataConfirmation,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    LineKind,
    VoltageClassId,
)
from rza_calc.io.diagram import (
    DIAGRAM_FORMAT_VERSION,
    DiagramFormatError,
    diagram_from_dict,
    diagram_to_dict,
)
from rza_calc.io.electrical_model import electrical_model_to_dict
from rza_calc.io.project import (
    FORMAT_VERSION,
    load_project,
    migrate_project_file,
    save_project,
)


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_V6 = ROOT / "rza_calc" / "examples" / "gtes_sever.json"
VOLTAGE_10_KV = VoltageClassId("builtin.voltage.ac.10kv")


def _v6_raw() -> dict:
    value = json.loads(EXAMPLE_V6.read_text(encoding="utf-8"))
    value["methodology"] = Methodology.load().data
    assert value["format_version"] == 6
    assert value["diagram"]["format_version"] == 1
    return value


def _write_json(tmp_path: Path, value: dict, name: str = "project.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _route_fixture() -> tuple[ElectricalModel, DiagramDocument, DiagramRouteId]:
    model = ElectricalModel.with_builtins("Трассы v7")
    branch, _ = model.create_equipment(
        "builtin.circuit_breaker", "QF-1"
    )
    start = ElectricalNode(ElectricalNodeId("node.route.start"), "Начало")
    finish = ElectricalNode(ElectricalNodeId("node.route.finish"), "Конец")
    model.add_node(start)
    model.add_node(finish)
    model.connect_port(branch.port_ids[0], start.id)
    model.connect_port(branch.port_ids[1], finish.id)

    page = DiagramPage(PageId("page.route"), "Схема")
    start_representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.route.start"),
        page.id,
        RepresentationTargetKind.ELECTRICAL_NODE,
        electrical_node_id=start.id,
    )
    finish_representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.route.finish"),
        page.id,
        RepresentationTargetKind.ELECTRICAL_NODE,
        electrical_node_id=finish.id,
    )
    route_id = DiagramRouteId("route.physical.branch")
    route = DiagramRoute(
        route_id,
        page.id,
        DiagramRouteKind.EQUIPMENT_BRANCH,
        RouteEndpointAnchor(
            RouteAnchorKind.BUS,
            start_representation.id,
            start.id,
            branch_port_id=branch.port_ids[0],
            anchor_key="начало",
        ),
        RouteEndpointAnchor(
            RouteAnchorKind.BUS,
            finish_representation.id,
            finish.id,
            branch_port_id=branch.port_ids[1],
            anchor_key="конец",
        ),
        equipment_id=branch.id,
        waypoints=(
            RouteWaypoint(
                RouteWaypointId("waypoint.route.1"),
                0.0,
                0.0,
                RouteWaypointSource.AUTOMATIC,
            ),
            RouteWaypoint(
                RouteWaypointId("waypoint.route.2"),
                80.0,
                0.0,
                RouteWaypointSource.USER,
                True,
            ),
            RouteWaypoint(
                RouteWaypointId("waypoint.route.3"),
                80.0,
                60.0,
                RouteWaypointSource.AUTOMATIC,
            ),
        ),
        routing_algorithm_version=2,
    )
    diagram = DiagramDocument.create(
        "Схема",
        (page,),
        (start_representation, finish_representation),
        routes=(route,),
    )
    return model, diagram, route_id


def _v6_line_raw(*, confirmed_impedance: bool) -> dict:
    raw = _v6_raw()
    model = ElectricalModel.with_builtins("Миграция линии v6")
    start = ElectricalNode(
        ElectricalNodeId("node.v6.line.start"),
        "Начало",
        declared_voltage_class_id=VOLTAGE_10_KV,
    )
    finish = ElectricalNode(
        ElectricalNodeId("node.v6.line.finish"),
        "Конец",
        declared_voltage_class_id=VOLTAGE_10_KV,
    )
    model.add_node(start)
    model.add_node(finish)
    inherited = (
        {"r1_ohm_per_km": 0.42, "x1_ohm_per_km": 0.31}
        if confirmed_impedance
        else {}
    )
    model.create_logical_line(
        "ВЛ-10 кВ",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        12_000,
        voltage_class_id=VOLTAGE_10_KV,
        inherited_properties=inherited,
    )
    electrical = electrical_model_to_dict(model)
    for line in electrical["logical_lines"]:
        line.pop("feeder_id")
    for section in electrical["line_sections"]:
        for segment in section["construction_segments"]:
            segment.pop("length_confirmation")
            segment.pop("impedance_confirmation")
    raw["project"]["name"] = model.name
    raw["electrical_model"] = electrical
    return raw


def test_diagram_v2_roundtrip_keeps_distinct_branch_ports_and_nodes() -> None:
    model, diagram, route_id = _route_fixture()
    raw = diagram_to_dict(diagram)
    assert raw["format_version"] == DIAGRAM_FORMAT_VERSION == 2
    restored = diagram_from_dict(raw, model)
    route = restored.routes[route_id]
    assert route.start_anchor.branch_port_id != route.end_anchor.branch_port_id
    assert route.start_anchor.electrical_node_id != route.end_anchor.electrical_node_id
    assert route.start_anchor.target_port_id is None
    assert route.end_anchor.target_port_id is None
    assert route.waypoints[1].source is RouteWaypointSource.USER
    assert route.waypoints[1].pinned is True
    assert restored == diagram


def test_node_connection_can_bind_equipment_port_to_bus_without_bus_port() -> None:
    model = ElectricalModel.with_builtins("Соединение порта с шиной")
    load, _ = model.create_equipment("builtin.load", "Нагрузка")
    node = ElectricalNode(ElectricalNodeId("node.bus.connection"), "Шина")
    model.add_node(node)
    model.connect_port(load.port_ids[0], node.id)
    page = DiagramPage(PageId("page.bus.connection"), "Схема")
    load_representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.load.connection"),
        page.id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=load.id,
    )
    bus_representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.bus.connection"),
        page.id,
        RepresentationTargetKind.ELECTRICAL_NODE,
        electrical_node_id=node.id,
    )
    route = DiagramRoute(
        DiagramRouteId("route.node.connection"),
        page.id,
        DiagramRouteKind.NODE_CONNECTION,
        RouteEndpointAnchor(
            RouteAnchorKind.EQUIPMENT_PORT,
            load_representation.id,
            node.id,
            target_port_id=load.port_ids[0],
        ),
        RouteEndpointAnchor(
            RouteAnchorKind.BUS,
            bus_representation.id,
            node.id,
        ),
        electrical_node_id=node.id,
        waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.node.1"), 0.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.node.2"), 40.0, 0.0),
        ),
    )
    diagram = DiagramDocument.create(
        "Схема",
        (page,),
        (load_representation, bus_representation),
        routes=(route,),
    )
    assert diagram.validate_targets(model) == ()
    assert diagram_from_dict(diagram_to_dict(diagram), model) == diagram


def test_route_operations_are_atomic_and_representation_delete_cascades() -> None:
    _, diagram, route_id = _route_fixture()
    route = diagram.routes[route_id]
    without_route = diagram.remove_route(route_id)
    assert route_id not in without_route.routes
    restored = without_route.add_route(route)
    moved = restored.rerouted_route(
        route_id,
        (
            RouteWaypoint(RouteWaypointId("waypoint.route.new.1"), 0.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.route.new.2"), 100.0, 0.0),
        ),
    )
    assert moved.revision == restored.revision + 1
    with pytest.raises(Exception):
        moved.remove_representation(route.start_anchor.representation_id)
    cleaned = moved.remove_representation(
        route.start_anchor.representation_id,
        cascade_routes=True,
    )
    assert route_id not in cleaned.routes


def test_diagram_v2_codec_rejects_unknown_and_non_orthogonal_route() -> None:
    model, diagram, _ = _route_fixture()
    raw = diagram_to_dict(diagram)
    raw["routes"][0]["unknown"] = True
    with pytest.raises(DiagramFormatError):
        diagram_from_dict(raw, model)

    raw = diagram_to_dict(diagram)
    raw["routes"][0]["waypoints"][1]["x"] = 30.0
    raw["routes"][0]["waypoints"][1]["y"] = 30.0
    with pytest.raises(DiagramFormatError):
        diagram_from_dict(raw, model)


def test_v6_node_route_migration_is_deterministic(tmp_path: Path) -> None:
    raw = _v6_raw()
    page_id = raw["diagram"]["pages"][0]["id"]
    node_id = raw["electrical_model"]["electrical_nodes"][0]["id"]
    representation_id = "representation.v6.route"
    raw["diagram"]["representations"].append({
        "id": representation_id,
        "page_id": page_id,
        "target_kind": "electrical_node",
        "equipment_id": None,
        "electrical_node_id": node_id,
        "x": 0.0,
        "y": 0.0,
        "rotation_deg": 0.0,
        "z_index": 0,
        "symbol_key": "node",
        "label": "",
        "route_points": [
            {"x": 0.0, "y": 0.0},
            {"x": 50.0, "y": 0.0},
            {"x": 50.0, "y": 80.0},
        ],
        "extensions": {},
    })
    path = _write_json(tmp_path, raw, "route-v6.json")
    first = load_project(path)
    second = load_project(path)
    assert first.source_format_version == 6
    assert tuple(first.diagram.routes) == tuple(second.diagram.routes)
    assert len(first.diagram.routes) == 1
    migrated_route = next(iter(first.diagram.routes.values()))
    assert migrated_route.kind is DiagramRouteKind.NODE_CONNECTION
    assert all(item.source is RouteWaypointSource.USER for item in migrated_route.waypoints)
    assert all(item.pinned for item in migrated_route.waypoints)
    migrated_representation = first.diagram.representations[
        GraphicalRepresentationId(representation_id)
    ]
    assert migrated_representation.route_points == ()


def test_v6_ambiguous_route_is_preserved_and_marked_for_review(
    tmp_path: Path,
) -> None:
    raw = _v6_raw()
    page_id = raw["diagram"]["pages"][0]["id"]
    equipment_id = raw["electrical_model"]["equipment"][0]["id"]
    representation_id = "representation.v6.ambiguous"
    raw["diagram"]["representations"].append({
        "id": representation_id,
        "page_id": page_id,
        "target_kind": "equipment",
        "equipment_id": equipment_id,
        "electrical_node_id": None,
        "x": 0.0,
        "y": 0.0,
        "rotation_deg": 0.0,
        "z_index": 0,
        "symbol_key": "legacy",
        "label": "",
        "route_points": [{"x": 0.0, "y": 0.0}, {"x": 20.0, "y": 0.0}],
        "extensions": {},
    })
    source = _write_json(tmp_path, raw, "ambiguous-v6.json")
    project = load_project(source)
    assert project.diagram.routes == {}
    representation = project.diagram.representations[
        GraphicalRepresentationId(representation_id)
    ]
    assert len(representation.route_points) == 2
    review = representation.extensions["rza_calc.stage4_migration_review"]
    assert review["required"] is True
    assert review["legacy_route_points_preserved"] is True
    assert review["blocks_legacy_calculation"] is True

    blocker = next(
        item for item in project.calculation_blockers
        if item.code == "migration.electrical_route_review_required"
    )
    equipment = project.electrical_model.equipment[representation.equipment_id]
    assert blocker.object_id == equipment_id
    assert equipment.name in blocker.message
    assert "Требуется проверка после миграции v6" in blocker.message
    with pytest.raises(ValueError, match="Требуется проверка после миграции v6"):
        project.require_calculation_ready()

    project.refresh_calculation_view(force=True)
    assert any(
        item.code == "migration.electrical_route_review_required"
        for item in project.calculation_blockers
    )

    target = tmp_path / "ambiguous-v7.json"
    save_project(target, project)
    reopened = load_project(target)
    reopened_blocker = next(
        item for item in reopened.calculation_blockers
        if item.code == "migration.electrical_route_review_required"
    )
    assert reopened_blocker == blocker
    reopened_review = reopened.diagram.representations[
        GraphicalRepresentationId(representation_id)
    ].extensions["rza_calc.stage4_migration_review"]
    assert reopened_review["blocks_legacy_calculation"] is True


def test_v6_purely_graphical_review_does_not_block_legacy_calculation(
    tmp_path: Path,
) -> None:
    raw = _v6_raw()
    page_id = raw["diagram"]["pages"][0]["id"]
    node_id = raw["electrical_model"]["electrical_nodes"][0]["id"]
    representation_id = "representation.v6.graphical-review"
    raw["diagram"]["representations"].append({
        "id": representation_id,
        "page_id": page_id,
        "target_kind": "electrical_node",
        "equipment_id": None,
        "electrical_node_id": node_id,
        "x": 0.0,
        "y": 0.0,
        "rotation_deg": 0.0,
        "z_index": 0,
        "symbol_key": "node",
        "label": "",
        "route_points": [
            {"x": 0.0, "y": 0.0},
            {"x": 20.0, "y": 10.0},
        ],
        "extensions": {},
    })

    project = load_project(_write_json(tmp_path, raw, "graphical-review-v6.json"))
    representation = project.diagram.representations[
        GraphicalRepresentationId(representation_id)
    ]
    review = representation.extensions["rza_calc.stage4_migration_review"]
    assert review["required"] is True
    assert review["blocks_legacy_calculation"] is False
    assert not any(
        item.code == "migration.electrical_route_review_required"
        for item in project.calculation_blockers
    )
    project.require_calculation_ready()

    target = tmp_path / "graphical-review-v7.json"
    save_project(target, project)
    reopened = load_project(target)
    reopened_review = reopened.diagram.representations[
        GraphicalRepresentationId(representation_id)
    ].extensions["rza_calc.stage4_migration_review"]
    assert reopened_review["blocks_legacy_calculation"] is False
    assert not any(
        item.code == "migration.electrical_route_review_required"
        for item in reopened.calculation_blockers
    )


def test_v6_line_migration_adds_confirmation_without_using_geometry(
    tmp_path: Path,
) -> None:
    raw = _v6_line_raw(confirmed_impedance=True)
    project = load_project(_write_json(tmp_path, raw, "line-v6.json"))
    segment = next(iter(project.electrical_model.line_sections.values())).construction_segments[0]
    assert segment.length_mm == 12_000
    assert segment.length_confirmation is DataConfirmation.CONFIRMED
    assert segment.impedance_confirmation is DataConfirmation.CONFIRMED
    assert project.diagram.routes == {}


def test_unconfirmed_v7_line_roundtrips_but_blocks_legacy_calculation(
    tmp_path: Path,
) -> None:
    raw = _v6_line_raw(confirmed_impedance=False)
    source = _write_json(tmp_path, raw, "unconfirmed-v6.json")
    project = load_project(source)
    assert any(item.code == "line_data_unconfirmed" for item in project.calculation_blockers)
    target = tmp_path / "unconfirmed-v7.json"
    save_project(target, project)
    reopened = load_project(target)
    assert reopened.source_format_version == FORMAT_VERSION == 7
    assert any(item.code == "line_data_unconfirmed" for item in reopened.calculation_blockers)
    with pytest.raises(ValueError):
        reopened.require_calculation_ready()


def test_v6_in_place_migration_creates_backup_and_appends_journal(
    tmp_path: Path,
) -> None:
    path = _write_json(tmp_path, _v6_raw(), "in-place-v6.json")
    backup = migrate_project_file(path)
    assert backup is not None and backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["format_version"] == 6
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["format_version"] == 7
    assert migrated["diagram"]["format_version"] == 2
    assert any(
        row["from_version"] == 6 and row["to_version"] == 7
        for row in migrated["migration_journal"]
    )
    assert any(row["backup_file"] == backup.name for row in migrated["migration_journal"])


def test_v6_rejects_premature_v7_nested_fields(tmp_path: Path) -> None:
    raw = _v6_line_raw(confirmed_impedance=True)
    raw["electrical_model"]["logical_lines"][0]["feeder_id"] = None
    with pytest.raises(ValueError):
        load_project(_write_json(tmp_path, raw, "premature-v7.json"))


def test_full_project_v7_roundtrip_preserves_route_and_waypoint_ids(
    tmp_path: Path,
) -> None:
    project = load_project(EXAMPLE_V6)
    page = next(iter(project.diagram.pages.values()))
    node_id = next(iter(project.electrical_model.electrical_nodes))
    representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.project.v7.node"),
        page.id,
        RepresentationTargetKind.ELECTRICAL_NODE,
        electrical_node_id=node_id,
    )
    route = DiagramRoute(
        DiagramRouteId("route.project.v7.node"),
        page.id,
        DiagramRouteKind.NODE_CONNECTION,
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            representation.id,
            node_id,
        ),
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            representation.id,
            node_id,
        ),
        electrical_node_id=node_id,
        waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.project.v7.1"), 0.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.project.v7.2"), 30.0, 0.0),
        ),
    )
    project.diagram = DiagramDocument.create(
        project.diagram.name,
        project.diagram.pages.values(),
        (*project.diagram.representations.values(), representation),
        routes=(*project.diagram.routes.values(), route),
        document_id=project.diagram.id,
        extensions=project.diagram.extensions,
    )
    target = tmp_path / "route-roundtrip-v7.json"
    save_project(target, project)
    reopened = load_project(target)
    assert reopened.diagram.routes[route.id] == route
    assert tuple(
        point.id for point in reopened.diagram.routes[route.id].waypoints
    ) == tuple(point.id for point in route.waypoints)


def test_project_rejects_route_id_collision_with_electrical_domain(
    tmp_path: Path,
) -> None:
    project = load_project(EXAMPLE_V6)
    page = next(iter(project.diagram.pages.values()))
    node_id = next(iter(project.electrical_model.electrical_nodes))
    equipment_id = next(iter(project.electrical_model.equipment))
    representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.route.collision"),
        page.id,
        RepresentationTargetKind.ELECTRICAL_NODE,
        electrical_node_id=node_id,
    )
    route = DiagramRoute(
        DiagramRouteId(equipment_id.value),
        page.id,
        DiagramRouteKind.NODE_CONNECTION,
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            representation.id,
            node_id,
        ),
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            representation.id,
            node_id,
        ),
        electrical_node_id=node_id,
        waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.collision.1"), 0.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.collision.2"), 10.0, 0.0),
        ),
    )
    project.diagram = DiagramDocument.create(
        project.diagram.name,
        project.diagram.pages.values(),
        (*project.diagram.representations.values(), representation),
        routes=(route,),
        document_id=project.diagram.id,
        extensions=project.diagram.extensions,
    )
    with pytest.raises(ValueError, match="Постоянный ID"):
        save_project(tmp_path / "collision-v7.json", project)
