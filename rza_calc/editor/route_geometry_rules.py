"""Small geometry-only rules shared by gesture previews and history commands."""
from __future__ import annotations

from dataclasses import replace
import math

from rza_calc.domain.diagram import DiagramRoute, RouteWaypoint, RouteWaypointId, RouteWaypointSource


def _same(a: RouteWaypoint, b: RouteWaypoint) -> bool:
    return math.isclose(a.x, b.x, abs_tol=1e-9) and math.isclose(a.y, b.y, abs_tol=1e-9)


def normalize_waypoints(points: tuple[RouteWaypoint, ...]) -> tuple[RouteWaypoint, ...]:
    """Remove redundant unlocked bends without moving endpoints or locked bends.

    A collinear reversal is redundant too: its travelled distance must not become
    a permanent loop after a segment is dragged back. Non-collinear detours are
    retained here; automatic routing uses apparatus obstacles and port directions.
    """
    result = list(points)
    changed = True
    while changed:
        changed = False
        # Trim self-crossings before collinear cleanup can hide their repeated
        # vertex. This only removes existing pieces, never cuts through a body.
        for left in range(len(result) - 3):
            a, b = result[left:left + 2]
            for right in range(left + 2, len(result) - 1):
                c, d = result[right:right + 2]
                if any(point.pinned for point in result[left + 1:right + 1]):
                    continue
                crossing = None
                if a.x == b.x and c.y == d.y:
                    if min(c.x, d.x) <= a.x <= max(c.x, d.x) and min(a.y, b.y) <= c.y <= max(a.y, b.y):
                        crossing = (a.x, c.y)
                elif a.y == b.y and c.x == d.x:
                    if min(a.x, b.x) <= c.x <= max(a.x, b.x) and min(c.y, d.y) <= a.y <= max(c.y, d.y):
                        crossing = (c.x, a.y)
                if crossing is None:
                    continue
                vertex = next((point for point in (b, c) if (point.x, point.y) == crossing), None)
                if vertex is None:
                    vertex = RouteWaypoint(RouteWaypointId.new(), *crossing)
                result = result[:left + 1] + [vertex] + result[right + 1:]
                changed = True
                break
            if changed:
                break
        if changed:
            continue
        for index in range(1, len(result) - 1):
            previous, current, following = result[index - 1:index + 2]
            if current.pinned:
                continue
            collinear = (
                math.isclose(previous.x, current.x, abs_tol=1e-9)
                and math.isclose(current.x, following.x, abs_tol=1e-9)
            ) or (
                math.isclose(previous.y, current.y, abs_tol=1e-9)
                and math.isclose(current.y, following.y, abs_tol=1e-9)
            )
            if _same(previous, current) or _same(current, following) or collinear:
                del result[index]
                changed = True
                break
        if changed:
            continue
        # Erase closed walks only when they contain no deliberate locks. Keep
        # the last endpoint's identity if the closed walk ends at that endpoint.
        for left in range(len(result) - 2):
            for right in range(left + 2, len(result)):
                if (_same(result[left], result[right])
                        and not any(point.pinned for point in result[left + 1:right])):
                    if right == len(result) - 1 and left != 0 and not result[left].pinned:
                        del result[left:right]
                    elif right != len(result) - 1 and not result[right].pinned:
                        del result[left + 1:right + 1]
                    else:
                        continue
                    changed = True
                    break
            if changed:
                break
    return tuple(result)


def set_route_bends_pinned(route: DiagramRoute, pinned: bool) -> DiagramRoute:
    """Explicitly lock/unlock interior vertices, retaining every ID and anchor."""
    if not isinstance(pinned, bool):
        raise TypeError("Признак фиксации изгибов должен быть логическим.")
    points = tuple(
        replace(point, pinned=pinned,
                source=RouteWaypointSource.USER if pinned else RouteWaypointSource.AUTOMATIC)
        if 0 < index < len(route.waypoints) - 1 else point
        for index, point in enumerate(route.waypoints)
    )
    return replace(route, waypoints=points)
