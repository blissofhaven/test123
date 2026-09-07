"""A hidden two-leg joint must not pull a wire back to its old location."""
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from rza_calc.domain.diagram import RouteWaypoint, RouteWaypointId, RouteWaypointSource
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.connection_reflow import reflow_degree_two_connections
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.orthogonal_routing import RouteVertex, RoutingObstacle
from rza_calc.io.project import load_project, save_project

EXAMPLE = Path(__file__).parents[1] / "tests/fixtures/legacy_projects/energoraion.json"


@pytest.fixture
def pair():
    project = load_project(EXAMPLE)
    model = project.electrical_model
    by_node = defaultdict(list)
    for route in project.diagram.routes.values():
        for anchor in (route.start_anchor, route.end_anchor):
            if anchor.target_port_id is None:
                by_node[anchor.representation_id].append(route)
    for node in project.diagram.representations.values():
        routes = by_node[node.id]
        if node.symbol_key != "connection_point" or len(routes) != 2:
            continue
        ends = [route.end_anchor if route.start_anchor.representation_id == node.id
                else route.start_anchor for route in routes]
        if not all(end.target_port_id is not None for end in ends):
            continue
        equipment = [model.equipment[project.diagram.representations[end.representation_id].equipment_id]
                     for end in ends]
        behaviors = [model.equipment_type(item.type_id, item.type_version).behavior_key for item in equipment]
        if not any("transformer" in value for value in behaviors) or "legacy.tie" not in behaviors:
            continue
        breaker = project.diagram.representations[ends[behaviors.index("legacy.tie")].representation_id]
        return project, node, tuple(routes), breaker
    raise AssertionError("Expected a transformer-to-input-breaker pair in the demo")


def _run(project, representations, routes, moved, *, model=None, obstacles=()):
    def endpoint(anchor, fallback):
        geometry = ProjectEditorController._port_anchor_geometry(
            project.electrical_model, representations[anchor.representation_id], anchor.target_port_id)
        return RouteVertex(geometry.x, geometry.y), geometry.direction
    return reflow_degree_two_connections(model or project.electrical_model, representations, routes,
                                         moved, endpoint_geometry=endpoint,
                                         obstacles_for_pair=lambda first, second: obstacles)


def _one_wire(node, routes):
    first, second = sorted(routes, key=lambda route: route.id.value)
    a, b = list(first.waypoints), list(second.waypoints)
    if first.start_anchor.representation_id == node.id:
        a.reverse()
    if second.end_anchor.representation_id == node.id:
        b.reverse()
    assert (a[-1].x, a[-1].y) == (b[0].x, b[0].y) == (node.x, node.y)
    return [(point.x, point.y) for point in a + b[1:]]


@pytest.mark.parametrize("dx,dy", ((0, -180), (160, -240), (-200, -240), (0, 180)))
def test_apparatus_move_reflows_both_legs_without_backtracking_or_domain_changes(pair, dx, dy):
    project, node, original_routes, breaker = pair
    before_fingerprint = electrical_model_fingerprint(project.electrical_model)
    representations = dict(project.diagram.representations)
    representations[breaker.id] = replace(breaker, x=breaker.x + dx, y=breaker.y + dy)
    result_reps, result_routes = _run(project, representations, project.diagram.routes, {breaker.id})
    updated = [result_routes[route.id] for route in original_routes]
    points = _one_wire(result_reps[node.id], updated)
    for a, b in zip(points, points[1:]):
        assert a != b and (a[0] == b[0] or a[1] == b[1])
    for a, b, c in zip(points, points[1:], points[2:]):
        if a[0] == b[0] == c[0]:
            assert (b[1] - a[1]) * (c[1] - b[1]) >= 0
        if a[1] == b[1] == c[1]:
            assert (b[0] - a[0]) * (c[0] - b[0]) >= 0
    for before, after in zip(original_routes, updated):
        assert (before.id, before.start_anchor, before.end_anchor) == (after.id, after.start_anchor, after.end_anchor)
        assert before.waypoints[0].id == after.waypoints[0].id
        assert before.waypoints[-1].id == after.waypoints[-1].id
    changed = {key for key in representations if representations[key] != result_reps[key]}
    assert changed <= {node.id}
    assert result_reps[breaker.id] == representations[breaker.id]
    assert electrical_model_fingerprint(project.electrical_model) == before_fingerprint
    assert project.diagram.representations[node.id] == node


@pytest.mark.parametrize("reason", ("node_moved", "pinned_bend", "node_locked", "bus", "extra_route", "unrelated_move"))
def test_explicit_geometry_and_non_series_nodes_are_not_automatically_relocated(pair, reason):
    project, node, pair_routes, breaker = pair
    reps, routes = dict(project.diagram.representations), dict(project.diagram.routes)
    moved = {breaker.id}
    reps[breaker.id] = replace(breaker, y=breaker.y - 180)
    if reason == "node_moved":
        moved.add(node.id)
    elif reason == "pinned_bend":
        route = pair_routes[0]
        points = list(route.waypoints)
        if len(points) == 2:
            a, b = points
            points.insert(1, RouteWaypoint(RouteWaypointId.new(), (a.x + b.x) / 2, (a.y + b.y) / 2))
        points[1] = replace(points[1], source=RouteWaypointSource.USER, pinned=True)
        routes[route.id] = replace(route, waypoints=tuple(points))
    elif reason == "node_locked":
        extensions = dict(node.extensions)
        extensions["position_pinned"] = True
        reps[node.id] = replace(node, extensions=extensions)
    elif reason == "bus":
        reps[node.id] = replace(node, symbol_key="busbar_horizontal")
    elif reason == "extra_route":
        from rza_calc.domain.diagram import DiagramRouteId
        copy = replace(pair_routes[0], id=DiagramRouteId.new())
        routes[copy.id] = copy
    else:
        incident = {anchor.representation_id for route in pair_routes
                    for anchor in (route.start_anchor, route.end_anchor)}
        moved = {next(identifier for identifier in reps if identifier not in incident)}
    actual_reps, actual_routes = _run(project, reps, routes, moved)
    assert actual_reps[node.id] == reps[node.id]
    assert all(actual_routes[route.id] == routes[route.id] for route in pair_routes)


def test_repeated_preview_is_geometrically_identical_and_keeps_node_identity(pair):
    project, node, _, breaker = pair
    reps = dict(project.diagram.representations)
    reps[breaker.id] = replace(breaker, y=breaker.y - 180)
    first_reps, first_routes = _run(project, reps, project.diagram.routes, {breaker.id})
    second_reps, second_routes = _run(project, reps, project.diagram.routes, {breaker.id})
    assert first_reps == second_reps
    assert first_reps[node.id].electrical_node_id == node.electrical_node_id
    assert {key: [(point.x, point.y) for point in route.waypoints] for key, route in first_routes.items()} == {
        key: [(point.x, point.y) for point in route.waypoints] for key, route in second_routes.items()}


def test_a_third_electrical_connection_not_drawn_on_this_page_prevents_reflow(pair):
    from types import SimpleNamespace
    project, node, pair_routes, breaker = pair
    reps = dict(project.diagram.representations)
    reps[breaker.id] = replace(breaker, y=breaker.y - 180)
    # A read-only model protocol fixture: the extra connection exists outside
    # this page. The helper must not infer degree from just the visible routes.
    connections = dict(project.electrical_model.connections)
    connections["additional-connection-on-another-page"] = SimpleNamespace(electrical_node_id=node.electrical_node_id)
    model = SimpleNamespace(connections=connections,
                            connection_for_port=project.electrical_model.connection_for_port)
    actual_reps, actual_routes = _run(project, reps, project.diagram.routes, {breaker.id}, model=model)
    assert actual_reps[node.id] == node
    assert all(actual_routes[route.id] == route for route in pair_routes)


def test_reflow_uses_obstacle_callback_for_the_whole_connection(pair):
    project, node, pair_routes, breaker = pair
    reps = dict(project.diagram.representations)
    reps[breaker.id] = replace(breaker, y=breaker.y - 180)
    base_reps, base_routes = _run(project, reps, project.diagram.routes, {breaker.id})
    midpoint = base_reps[node.id]
    obstacle = RoutingObstacle(midpoint.x - 8, midpoint.y - 8, midpoint.x + 8, midpoint.y + 8)
    actual_reps, actual_routes = _run(project, reps, project.diagram.routes, {breaker.id}, obstacles=(obstacle,))
    points = _one_wire(actual_reps[node.id], [actual_routes[route.id] for route in pair_routes])
    for a, b in zip(points, points[1:]):
        if a[0] == b[0] and obstacle.left < a[0] < obstacle.right:
            assert max(a[1], b[1]) <= obstacle.top or min(a[1], b[1]) >= obstacle.bottom
        if a[1] == b[1] and obstacle.top < a[1] < obstacle.bottom:
            assert max(a[0], b[0]) <= obstacle.left or min(a[0], b[0]) >= obstacle.right


def test_reflow_save_reload_preserves_all_domain_and_graphical_ids(pair, tmp_path):
    project, node, pair_routes, breaker = pair
    original_bytes = EXAMPLE.read_bytes()
    fingerprint = electrical_model_fingerprint(project.electrical_model)
    reps = dict(project.diagram.representations)
    reps[breaker.id] = replace(breaker, y=breaker.y - 180)
    updated_reps, updated_routes = _run(project, reps, project.diagram.routes, {breaker.id})
    project.diagram = replace(project.diagram, representations=updated_reps, routes=updated_routes)
    target = tmp_path / "reflow.json"
    save_project(target, project)
    reopened = load_project(target)
    assert electrical_model_fingerprint(reopened.electrical_model) == fingerprint
    assert reopened.diagram.representations == project.diagram.representations
    assert reopened.diagram.routes == project.diagram.routes
    assert EXAMPLE.read_bytes() == original_bytes
