"""Default tap geometry must leave the retained conductor without overlapping it."""
from dataclasses import dataclass

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument, DiagramPage, PageId, RouteWaypoint, RouteWaypointId,
    RouteWaypointSource,
)
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, LineKind, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import NodeTarget, PhysicalLineInput, ProjectEditorController


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _setup(vertical=False):
    project = _Project(
        ElectricalModel.with_builtins("Default tap routing"),
        DiagramDocument.create("Diagram", (DiagramPage(PageId("page.tap"), "Page"),)),
        ProjectCatalogSnapshots(),
    )
    controller = ProjectEditorController(project)
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    orient = lambda x, y: (y, x) if vertical else (x, y)
    nodes = [controller.add_electrical_node(name, x=orient(x, y)[0], y=orient(x, y)[1],
             voltage_class_id=voltage) for name, x, y in
             (("Start", 0, 0), ("End", 600, 0), ("Target", 300, 200))]
    physical = PhysicalLineInput(1_000_000, DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3}, DataConfirmation.CONFIRMED)
    main = controller.create_physical_line("Main", LineKind.OVERHEAD,
        NodeTarget(nodes[0].node_id), NodeTarget(nodes[1].node_id), physical=physical)
    return controller, main, nodes[2], physical, orient


def _positive_collinear_overlap(first, second):
    """Independent interval intersection; a common T endpoint has zero length."""
    for a, b in zip(first, first[1:]):
        assert a.x == b.x or a.y == b.y
        for c, d in zip(second, second[1:]):
            assert c.x == d.x or c.y == d.y
            if a.y == b.y == c.y == d.y:
                if min(max(a.x, b.x), max(c.x, d.x)) > max(min(a.x, b.x), min(c.x, d.x)):
                    return True
            if a.x == b.x == c.x == d.x:
                if min(max(a.y, b.y), max(c.y, d.y)) > max(min(a.y, b.y), min(c.y, d.y)):
                    return True
    return False


@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.parametrize("kind", [LineKind.CABLE, LineKind.OVERHEAD])
def test_default_tap_does_not_overlap_retained_main_and_undo_is_atomic(vertical, kind):
    controller, main, target, physical, orient = _setup(vertical)
    before_diagram = controller.diagram
    before_fingerprint = electrical_model_fingerprint(controller.model)
    result = controller.create_tap(main.section_id, 200_000, "Tap", kind,
        NodeTarget(target.node_id), physical=physical,
        tap_x=orient(200, 0)[0], tap_y=orient(200, 0)[1])
    outgoing = next(route for route in controller.diagram.routes.values()
                    if route.equipment_id == result.branch_section_id)
    retained = [route for route in controller.diagram.routes.values()
                if route.equipment_id in {result.first_section_id, result.second_section_id}]
    assert len(retained) == 2
    assert (outgoing.waypoints[0].x, outgoing.waypoints[0].y) == orient(200, 0)
    assert (outgoing.waypoints[-1].x, outgoing.waypoints[-1].y) == orient(300, 200)
    assert all(not _positive_collinear_overlap(outgoing.waypoints, route.waypoints)
               for route in retained)
    assert outgoing.start_anchor.electrical_node_id == result.tap_node_id
    assert outgoing.end_anchor.electrical_node_id == target.node_id
    after_diagram = controller.diagram
    after_fingerprint = electrical_model_fingerprint(controller.model)
    controller.undo()
    assert controller.diagram == before_diagram
    assert electrical_model_fingerprint(controller.model) == before_fingerprint
    controller.redo()
    assert controller.diagram == after_diagram
    assert electrical_model_fingerprint(controller.model) == after_fingerprint


@pytest.mark.parametrize("kind", [LineKind.CABLE, LineKind.OVERHEAD])
def test_explicit_tap_route_preserves_waypoint_ids_sources_and_pins(kind):
    controller, main, target, physical, _ = _setup()
    points = tuple(RouteWaypoint(RouteWaypointId(f"waypoint.tap.explicit.{i}"), x, y,
        source=RouteWaypointSource.USER, pinned=i in {1, 2}) for i, (x, y) in enumerate(
        ((200, 0), (200, 100), (260, 100), (260, 200), (300, 200))))
    result = controller.create_tap(main.section_id, 200_000, "Tap", kind,
        NodeTarget(target.node_id), physical=physical, tap_x=200, tap_y=0,
        route_waypoints=points)
    outgoing = next(route for route in controller.diagram.routes.values()
                    if route.equipment_id == result.branch_section_id)
    assert outgoing.waypoints == points
