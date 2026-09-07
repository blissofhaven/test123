"""Local wire gestures with fixed electrical endpoints (no Qt/model writes)."""
from __future__ import annotations

from dataclasses import replace
import math

from rza_calc.domain.diagram import (
    DiagramRoute, RouteWaypoint, RouteWaypointId, RouteWaypointSource,
)
from rza_calc.editor.route_geometry_rules import normalize_waypoints
from rza_calc.editor.orthogonal_routing import (
    RoutingError, RoutingObstacle, _segment_hits_obstacle, segments_overlap,
)


def _shift_route_segment(
    route: DiagramRoute, segment_index: int, *, dx: float, dy: float,
) -> tuple[RouteWaypoint, ...]:
    """Move one orthogonal segment perpendicularly, keeping both ends attached.

    End segments receive short, unchanged-direction terminal leads and two new
    bends. Existing waypoint identities survive wherever the point remains;
    only new bends receive new identities. Parallel motion is a true no-op.
    """
    if (isinstance(segment_index, bool) or not isinstance(segment_index, int)
            or not 0 <= segment_index < len(route.waypoints) - 1):
        raise ValueError("Участок графической трассы не найден.")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in (dx, dy)):
        raise ValueError("Смещение трассы должно содержать конечные числа.")
    points = route.waypoints
    first, second = points[segment_index:segment_index + 2]
    horizontal = first.y == second.y
    delta = float(dy if horizontal else dx)
    if delta == 0.0:
        return points

    def moved(point: RouteWaypoint) -> RouteWaypoint:
        return replace(point, x=point.x + (0.0 if horizontal else delta),
                       y=point.y + (delta if horizontal else 0.0),
                       source=point.source, pinned=point.pinned)

    def new_point(x: float, y: float) -> RouteWaypoint:
        return RouteWaypoint(RouteWaypointId.new(), x, y, RouteWaypointSource.AUTOMATIC, False)

    # A fixed terminal must still leave along the original segment, not turn
    # sideways through the apparatus when the first/last segment is dragged.
    length = abs(second.x - first.x) + abs(second.y - first.y)
    if length == 0.0:
        return normalize_waypoints(points)
    stub = min(12.0, length / 3.0)
    ux = (second.x - first.x) / length
    uy = (second.y - first.y) / length
    replacement: list[RouteWaypoint] = []
    if segment_index == 0:
        lead = new_point(first.x + ux * stub, first.y + uy * stub)
        replacement.extend((first, lead, moved(new_point(lead.x, lead.y))))
    else:
        replacement.append(moved(first))
    if segment_index == len(points) - 2:
        lead = new_point(second.x - ux * stub, second.y - uy * stub)
        replacement.extend((moved(new_point(lead.x, lead.y)), lead, second))
    else:
        replacement.append(moved(second))
    candidates = (*points[:segment_index], *replacement, *points[segment_index + 2:])
    result: list[RouteWaypoint] = []
    for point in candidates:
        if result and (result[-1].x, result[-1].y) == (point.x, point.y):
            # Endpoint identity wins if a moved bend lands exactly on it.
            if point.id == points[-1].id:
                if result[-1].pinned and result[-1].id != point.id:
                    raise RoutingError("Закреплённый изгиб нельзя совместить с концом линии. Сначала освободите изгибы.")
                result[-1] = point
            elif point.pinned and point.id != result[-1].id:
                raise RoutingError("Закреплённый изгиб нельзя совместить с другой точкой. Сначала освободите изгибы.")
            continue
        result.append(point)
    return normalize_waypoints(tuple(result))


def shift_route_segment(
    route: DiagramRoute, segment_index: int, *, dx: float, dy: float,
    occupied_segments: tuple[tuple[float, float, float, float], ...] = (),
    obstacles: tuple[RoutingObstacle, ...] = (),
) -> tuple[RouteWaypoint, ...]:
    """Move a segment, offering the nearest separate lane on wire coincidence.

    Point crossings remain legal. If every nearby position still overlaps a
    fixed terminal lead, the gesture fails instead of stacking conductors.
    The same pure function drives the preview and committed command.
    """
    candidate = _shift_route_segment(route, segment_index, dx=dx, dy=dy)
    if candidate == route.waypoints or (not occupied_segments and not obstacles):
        return candidate

    def free(points):
        return not any(
            any(segments_overlap((a.x, a.y), (b.x, b.y), wire) for wire in occupied_segments)
            or any(_segment_hits_obstacle((a.x, a.y), (b.x, b.y), body) for body in obstacles)
            for a, b in zip(points, points[1:]))

    if free(candidate):
        return candidate
    first, second = route.waypoints[segment_index:segment_index + 2]
    horizontal = first.y == second.y
    for distance in (10.0, 20.0, 30.0, 40.0):
        for offset in (-distance, distance):
            shifted = _shift_route_segment(route, segment_index,
                dx=dx if horizontal else dx + offset,
                dy=dy + offset if horizontal else dy)
            if free(shifted):
                return shifted
    raise RoutingError("Рядом нет свободной полосы для линии: трасса пересекает аппарат или совпадает с другим проводником.")


__all__ = ["shift_route_segment"]
