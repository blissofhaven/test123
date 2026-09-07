"""Drag backtracking, explicit locks and separate conductor corridors."""
from dataclasses import replace

import pytest

from rza_calc.domain.diagram import (
    DiagramRoute, DiagramRouteId, DiagramRouteKind, ElectricalNodeId,
    GraphicalRepresentationId, PageId, RouteAnchorKind, RouteEndpointAnchor,
    RouteWaypoint, RouteWaypointId, RouteWaypointSource,
)
from rza_calc.editor.connected_geometry import shift_route_segment
from rza_calc.editor.route_geometry_rules import normalize_waypoints, set_route_bends_pinned
from rza_calc.editor.orthogonal_routing import (
    RouteDirection as D, RouteVertex as V, RouteVertexSource as S, RoutingError, RoutingObstacle,
    RoutingRequest, build_orthogonal_route, segments_overlap,
)


def route(coords):
    node = ElectricalNodeId("node.rules")
    anchors = [RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE,
                 GraphicalRepresentationId(f"rep.{i}"), node) for i in range(2)]
    return DiagramRoute(DiagramRouteId("route.rules"), PageId("page.rules"),
        DiagramRouteKind.NODE_CONNECTION, *anchors, electrical_node_id=node,
        waypoints=tuple(RouteWaypoint(RouteWaypointId(f"wp.{i}"), x, y)
                        for i, (x, y) in enumerate(coords)))


def test_drag_left_then_back_right_restores_a_single_straight_conductor():
    original = route([(0, 0), (0, 200)])
    left = replace(original, waypoints=shift_route_segment(original, 0, dx=-80, dy=0))
    index = next(i for i, point in enumerate(left.waypoints[:-1])
                 if point.x == left.waypoints[i + 1].x == -80)
    restored = shift_route_segment(left, index, dx=80, dy=0)
    assert restored == original.waypoints
    assert all(not point.pinned for point in left.waypoints)


def test_existing_locked_detour_is_preserved_until_explicitly_unlocked():
    original = route([(0, 0), (0, 20), (0, -20), (0, 200)])
    locked = set_route_bends_pinned(original, True)
    assert normalize_waypoints(locked.waypoints) == locked.waypoints
    unlocked = set_route_bends_pinned(locked, False)
    assert normalize_waypoints(unlocked.waypoints) == (original.waypoints[0], original.waypoints[-1])
    assert unlocked.start_anchor == original.start_anchor
    assert unlocked.end_anchor == original.end_anchor
    assert [point.id for point in unlocked.waypoints] == [point.id for point in original.waypoints]


def test_dragging_onto_another_wire_offers_a_nearby_separate_lane():
    original = route([(0, 0), (0, 200)])
    occupied = ((-80, 20, -80, 180),)
    moved = shift_route_segment(original, 0, dx=-80, dy=0, occupied_segments=occupied)
    assert moved[0] == original.waypoints[0] and moved[-1] == original.waypoints[-1]
    assert any(point.x == -90 for point in moved)
    assert all(not segments_overlap((a.x, a.y), (b.x, b.y), occupied[0])
               for a, b in zip(moved, moved[1:]))


def test_segment_drag_cannot_leave_a_fixed_lead_stacked_with_another_wire():
    original = route([(0, 0), (0, 200)])
    with pytest.raises(RoutingError, match="свободной полосы"):
        shift_route_segment(original, 0, dx=-80, dy=0, occupied_segments=((0, 0, 0, 10),))


def test_drag_must_not_silently_erase_a_lock_that_reaches_an_endpoint():
    original = route([(0, 0), (100, 0), (100, 100), (200, 100)])
    point = replace(original.waypoints[1], source=RouteWaypointSource.USER, pinned=True)
    locked = replace(original, waypoints=(original.waypoints[0], point, *original.waypoints[2:]))
    with pytest.raises(RoutingError, match="Закреплённый изгиб"):
        shift_route_segment(locked, 1, dx=-100, dy=0)
    assert locked.waypoints[1] == point


def test_segment_drag_chooses_a_nearby_lane_outside_a_foreign_apparatus():
    original = route([(0, 0), (0, 200)])
    body = RoutingObstacle(80, 90, 120, 110)
    result = shift_route_segment(original, 0, dx=100, dy=0, obstacles=(body,))
    assert result[0] == original.waypoints[0] and result[-1] == original.waypoints[-1]
    assert any(point.x == 80 for point in result)
    for first, last in zip(result, result[1:]):
        if first.x == last.x:
            assert not (80 < first.x < 120 and max(min(first.y, last.y), 90) < min(max(first.y, last.y), 110))


def test_closed_unlocked_walk_is_removed_without_changing_terminal_ids():
    original = route([(0, 0), (0, 20), (30, 20), (30, 50), (0, 50), (0, 20), (0, 100)])
    result = normalize_waypoints(original.waypoints)
    assert result == (original.waypoints[0], original.waypoints[-1])


@pytest.mark.parametrize("occupied,expected", [
    ((20, 0, 80, 0), True), ((100, 0, 140, 0), False),
    ((50, -80, 50, 80), False), ((0, 1, 100, 1), False),
    ((80, 0, 20, 0), True), ((50, 0, 50, 0), False),
])
def test_coincident_length_is_distinct_from_crossing_or_touching(occupied, expected):
    assert segments_overlap((0, 0), (100, 0), occupied) is expected


@pytest.mark.parametrize("occupied", [((20, 0, 80, 0),), ((80, 0, 20, 0),)])
def test_router_chooses_a_separate_lane_for_a_previously_occupied_middle(occupied):
    request = RoutingRequest(V(0, 0), V(100, 0), D.RIGHT, D.LEFT,
                             port_stub=10, occupied_segments=occupied)
    vertices = build_orthogonal_route(request)
    assert vertices[0] == request.start and vertices[-1] == request.end
    assert len(vertices) >= 6
    assert all(not segments_overlap(a.point, b.point, wire)
               for a, b in zip(vertices, vertices[1:]) for wire in occupied)
    assert all((a.x == b.x) != (a.y == b.y) for a, b in zip(vertices, vertices[1:]))


def test_crossing_stays_short_and_does_not_force_a_detour():
    vertices = build_orthogonal_route(RoutingRequest(V(0, 0), V(100, 0),
        occupied_segments=((50, -100, 50, 100),)))
    assert [v.point for v in vertices] == [(0, 0), (100, 0)]


def test_manual_lock_on_the_automatic_port_stub_survives_routing():
    pinned = V(12, 0, S.USER, True)
    vertices = build_orthogonal_route(RoutingRequest(V(0, 0), V(100, 0),
        D.RIGHT, D.LEFT, port_stub=12, manual_vertices=(pinned,)))
    assert pinned in vertices


def test_occupied_terminal_lead_is_rejected_instead_of_drawn_on_top():
    with pytest.raises(RoutingError, match="Подвод"):
        build_orthogonal_route(RoutingRequest(V(0, 0), V(100, 0), D.RIGHT, D.LEFT,
                                occupied_segments=((0, 0, 50, 0),)))


def test_body_clearance_remains_required_when_avoiding_another_wire():
    body = RoutingObstacle(30, -30, 70, 10)
    vertices = build_orthogonal_route(RoutingRequest(V(0, 0), V(100, 0),
        obstacles=(body,), clearance=2, occupied_segments=((0, 12, 100, 12),)))
    for first, last in zip(vertices, vertices[1:]):
        assert not segments_overlap(first.point, last.point, (0, 12, 100, 12))
        if first.y == last.y:
            assert not (-32 < first.y < 12 and max(min(first.x, last.x), 28) < min(max(first.x, last.x), 72))


@pytest.mark.parametrize("segment", [(0, 0, 20, 30), (0, 0, float("nan"), 0), (0, 0)])
def test_invalid_wire_constraint_is_rejected(segment):
    with pytest.raises(ValueError):
        RoutingRequest(V(0, 0), V(100, 0), occupied_segments=(segment,))
