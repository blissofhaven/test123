"""Pure graphical routing: hard body clearance, explicit pins and real bus ends."""
from __future__ import annotations

import math

import pytest

from rza_calc.editor import orthogonal_routing as routing
from rza_calc.editor.orientation import bus_anchor_geometry, bus_anchor_toward_point
from rza_calc.editor.orthogonal_routing import (
    RouteDirection as D, RouteVertex as V, RouteVertexSource as S,
    RoutingError, RoutingObstacle as O, RoutingRequest, build_orthogonal_route,
)


def _assert_geometry(vertices, obstacles=(), clearance=0.0):
    points = [(v.x, v.y) for v in vertices]
    assert len(points) >= 2 and len(set(points)) == len(points)
    for a, b in zip(points, points[1:]):
        assert (a[0] == b[0]) != (a[1] == b[1])
        for obstacle in obstacles:
            left, right = obstacle.left - clearance, obstacle.right + clearance
            top, bottom = obstacle.top - clearance, obstacle.bottom + clearance
            if a[0] == b[0]:
                assert not (left < a[0] < right and max(min(a[1], b[1]), top) < min(max(a[1], b[1]), bottom))
            else:
                assert not (top < a[1] < bottom and max(min(a[0], b[0]), left) < min(max(a[0], b[0]), right))


def _assert_directions(vertices, start, end):
    deltas = {D.LEFT: (-1, 0), D.RIGHT: (1, 0), D.UP: (0, -1), D.DOWN: (0, 1)}
    for first, second, direction in ((vertices[0], vertices[1], start), (vertices[-1], vertices[-2], end)):
        ux, uy = deltas[direction]
        dx, dy = second.x - first.x, second.y - first.y
        assert dx * uy == dy * ux and dx * ux + dy * uy > 0


def test_unpinned_old_user_bends_do_not_force_a_detour_after_move():
    vertices = build_orthogonal_route(RoutingRequest(
        V(0, 0), V(0, 100), D.DOWN, D.UP,
        manual_vertices=(V(300, 18, S.USER), V(300, 80, S.USER)),
    ))
    assert [v.point for v in vertices] == [(0.0, 0.0), (0.0, 100.0)]
    _assert_directions(vertices, D.DOWN, D.UP)


@pytest.mark.parametrize("source", (S.USER, S.AUTOMATIC))
def test_explicitly_pinned_manual_goal_survives_reflow(source):
    pinned = V(80, 60, source, True)
    vertices = build_orthogonal_route(RoutingRequest(V(0, 0), V(0, 120), manual_vertices=(pinned,)))
    assert pinned in vertices
    _assert_geometry(vertices)


def test_bounded_search_handles_alternating_obstacles_beyond_two_bends():
    obstacles = (O(20, -200, 30, 20), O(50, -20, 60, 200), O(80, -200, 90, 20))
    request = RoutingRequest(V(0, 0), V(110, 0), D.RIGHT, D.LEFT,
                             obstacles=obstacles, clearance=2, port_stub=5)
    vertices = build_orthogonal_route(request)
    _assert_geometry(vertices, obstacles, 2)
    _assert_directions(vertices, D.RIGHT, D.LEFT)
    assert len(vertices) > 4
    assert max(abs(v.y) for v in vertices) < 100
    assert vertices == build_orthogonal_route(request)
    assert vertices == build_orthogonal_route(RoutingRequest(
        request.start, request.end, request.start_direction, request.end_direction,
        obstacles=tuple(reversed(obstacles)), clearance=2, port_stub=5))


def test_no_safe_path_raises_instead_of_silently_crossing_a_body():
    ring = (O(-20, -20, -10, 20), O(10, -20, 20, 20),
            O(-20, -20, 20, -10), O(-20, 10, 20, 20))
    with pytest.raises(RoutingError):
        build_orthogonal_route(RoutingRequest(V(0, 0), V(100, 0), obstacles=ring, clearance=0))


def test_a_foreign_body_cannot_be_skipped_by_the_terminal_stub():
    body = O(5, -2, 10, 2)
    vertices = build_orthogonal_route(RoutingRequest(
        V(0, 0), V(100, 0), D.RIGHT, D.LEFT, obstacles=(body,), clearance=1, port_stub=18))
    _assert_geometry(vertices, (body,), 1)
    _assert_directions(vertices, D.RIGHT, D.LEFT)


@pytest.mark.parametrize("reverse", (False, True))
@pytest.mark.parametrize("directed", (False, True))
def test_endpoint_inside_a_foreign_body_raises_instead_of_discarding_it(reverse, directed):
    start, end = (V(100, 0), V(0, 0)) if reverse else (V(0, 0), V(100, 0))
    start_direction = (D.LEFT if reverse else D.RIGHT) if directed else None
    end_direction = (D.RIGHT if reverse else D.LEFT) if directed else None
    with pytest.raises(RoutingError, match="внутри тела"):
        build_orthogonal_route(RoutingRequest(
            start, end, start_direction, end_direction,
            obstacles=(O(-5, -5, 50, 5),), port_stub=12))


def test_port_on_body_boundary_can_leave_outward():
    body = O(-40, -20, 0, 20)
    vertices = build_orthogonal_route(RoutingRequest(
        V(0, 0), V(100, 0), D.RIGHT, D.LEFT, obstacles=(body,), port_stub=12))
    assert [v.point for v in vertices] == [(0.0, 0.0), (100.0, 0.0)]
    _assert_geometry(vertices, (body,))
    _assert_directions(vertices, D.RIGHT, D.LEFT)


@pytest.mark.parametrize("reverse", (False, True))
def test_two_unit_bus_to_breaker_gap_keeps_a_direct_wire_outside_raw_body(reverse):
    bus, port = V(2516, -410), V(2518, -410)
    start, end = (port, bus) if reverse else (bus, port)
    start_direction, end_direction = (D.LEFT, D.RIGHT) if reverse else (D.RIGHT, D.LEFT)
    body = O(2518, -426, 2582, -394)
    vertices = build_orthogonal_route(RoutingRequest(
        start, end, start_direction, end_direction, obstacles=(body,), port_stub=12))
    assert vertices == (start, end)
    _assert_geometry(vertices, (body,))
    _assert_directions(vertices, start_direction, end_direction)


def test_short_facing_join_does_not_waive_a_foreign_bodys_clearance():
    own = O(2518, -426, 2582, -394)
    foreign = O(2500, -409, 2510, -390)
    with pytest.raises(RoutingError):
        build_orthogonal_route(RoutingRequest(
            V(2516, -410), V(2518, -410), D.RIGHT, D.LEFT,
            obstacles=(own, foreign), port_stub=12))


def test_short_facing_join_does_not_cross_a_foreign_raw_body():
    own = O(2518, -426, 2582, -394)
    foreign = O(2516.5, -420, 2517.5, -400)
    with pytest.raises(RoutingError):
        build_orthogonal_route(RoutingRequest(
            V(2516, -410), V(2518, -410), D.RIGHT, D.LEFT,
            obstacles=(own, foreign), port_stub=12))


@pytest.mark.parametrize("distance", (1.0, 10.0, 35.0))
def test_close_facing_ports_use_a_straight_connection_without_returning_stubs(distance):
    vertices = build_orthogonal_route(RoutingRequest(V(0, 0), V(distance, 0), D.RIGHT, D.LEFT))
    assert [v.point for v in vertices] == [(0.0, 0.0), (distance, 0.0)]
    _assert_directions(vertices, D.RIGHT, D.LEFT)


def test_ports_facing_away_still_keep_their_semantic_exit_directions():
    vertices = build_orthogonal_route(RoutingRequest(V(0, 0), V(-100, 0), D.RIGHT, D.RIGHT))
    _assert_geometry(vertices)
    _assert_directions(vertices, D.RIGHT, D.RIGHT)


def test_direct_route_in_a_thousand_object_diagram_never_builds_the_grid(monkeypatch):
    monkeypatch.setattr(routing, "_grid_leg", lambda *args: pytest.fail("unnecessary fallback graph"))
    bodies = tuple(O(1000 + i * 20, 1000, 1010 + i * 20, 1010) for i in range(1000))
    result = build_orthogonal_route(RoutingRequest(V(0, 0), V(100, 0), obstacles=bodies))
    assert [v.point for v in result] == [(0.0, 0.0), (100.0, 0.0)]


def test_fallback_grid_coordinates_are_bounded_without_dropping_obstacles():
    bodies = tuple(O(i * 4, i * 3, i * 4 + 1, i * 3 + 1) for i in range(1000))
    xs, ys = routing._coordinate_candidates((-10, -10), (4001, 3001), bodies)
    assert len(xs) <= 64 and len(ys) <= 64
    assert {-10, 4001} <= set(xs) and {-10, 3001} <= set(ys)


def _rotate(x, y, angle):
    return ((x, y), (-y, x), (-x, -y), (y, -x))[angle // 90]


@pytest.mark.parametrize("vertical", (False, True))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
@pytest.mark.parametrize("sign", (-1, 1))
def test_side_port_projects_to_actual_bus_end_and_leaves_outward(vertical, angle, sign):
    local_target = (0, sign * 180) if vertical else (sign * 180, 0)
    tx, ty = _rotate(*local_target, angle)
    fraction, x, y, direction = bus_anchor_toward_point(
        width=12 if vertical else 240, height=240 if vertical else 12,
        rotation=angle, center_x=300, center_y=260, target_x=300 + tx, target_y=260 + ty,
    )
    local_end = (0, sign * 120) if vertical else (sign * 120, 0)
    ex, ey = _rotate(*local_end, angle)
    assert fraction == (0 if sign < 0 else 1)
    assert (x, y) == (300 + ex, 260 + ey)
    unit = {D.LEFT: (-1, 0), D.RIGHT: (1, 0), D.UP: (0, -1), D.DOWN: (0, 1)}[direction]
    assert unit[0] * (tx - ex) + unit[1] * (ty - ey) > 0
    assert unit[0] * (ty - ey) == unit[1] * (tx - ex)


@pytest.mark.parametrize("y,direction", ((80, D.DOWN), (-80, D.UP)))
def test_inner_bus_attachment_uses_perpendicular_side_of_adjacent_port(y, direction):
    assert bus_anchor_toward_point(width=240, height=12, rotation=0, center_x=0, center_y=0,
                                  target_x=60, target_y=y) == (.75, 60.0, 0.0, direction)
    # The explicit/manual fractional API retains its previous convention.
    assert bus_anchor_geometry(width=240, height=12, rotation=0, center_x=0, center_y=0,
                               fraction=.75) == (60.0, 0.0, D.UP)


@pytest.mark.parametrize("value", (math.nan, math.inf, True, "180"))
def test_automatic_bus_projection_rejects_invalid_target(value):
    with pytest.raises(ValueError):
        bus_anchor_toward_point(width=240, height=12, rotation=0, center_x=0, center_y=0,
                               target_x=value, target_y=0)
