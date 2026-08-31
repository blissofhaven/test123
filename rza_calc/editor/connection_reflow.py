"""Reflow a two-leg graphical connection as one wire, without domain writes.

A hidden degree-two node is a drawing joint, not an independent waypoint the
user must drag after each apparatus move. Real branches, buses and explicitly
fixed geometry must not be relocated by this helper.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from typing import Callable, Iterable, Mapping

from rza_calc.domain.diagram import (
    DiagramRoute, DiagramRouteId, DiagramRouteKind, GraphicalRepresentation,
    GraphicalRepresentationId, RouteAnchorKind, RouteEndpointAnchor,
    RouteWaypoint, RouteWaypointId, RouteWaypointSource,
)
from rza_calc.domain.electrical import ElectricalModel
from rza_calc.editor.orthogonal_routing import (
    RouteDirection, RouteVertex, RoutingObstacle, RoutingRequest,
    build_orthogonal_route,
)

EndpointGeometry = Callable[[RouteEndpointAnchor, RouteWaypoint],
                            tuple[RouteVertex, RouteDirection | None]]
PairObstacles = Callable[[DiagramRoute, DiagramRoute], Iterable[RoutingObstacle]]


def _split_midway(vertices: tuple[RouteVertex, ...]):
    lengths = [abs(b.x - a.x) + abs(b.y - a.y)
               for a, b in zip(vertices, vertices[1:])]
    remaining = sum(lengths) / 2.0
    if remaining <= 1e-9:
        raise ValueError("Совпавшие выводы не образуют корректного провода.")
    for index, length in enumerate(lengths):
        if remaining <= length and length > 0:
            a, b = vertices[index:index + 2]
            ratio = remaining / length
            middle = RouteVertex(a.x + (b.x - a.x) * ratio,
                                 a.y + (b.y - a.y) * ratio)
            before = list(vertices[:index + 1])
            after = list(vertices[index + 1:])
            if before[-1].point != middle.point:
                before.append(middle)
            if after[0].point != middle.point:
                after.insert(0, middle)
            return middle, tuple(before), tuple(after)
        remaining -= length
    raise ValueError("Не найдена середина графического соединения.")


def _with_vertices(route: DiagramRoute, vertices: tuple[RouteVertex, ...]):
    if len(vertices) < 2:
        raise ValueError("Участок соединения должен иметь два разных конца.")
    old_at = {(point.x, point.y): point.id for point in route.waypoints[1:-1]}
    points = []
    for index, vertex in enumerate(vertices):
        if index == 0:
            points.append(replace(route.waypoints[0], x=vertex.x, y=vertex.y))
            continue
        elif index == len(vertices) - 1:
            points.append(replace(route.waypoints[-1], x=vertex.x, y=vertex.y))
            continue
        else:
            identifier = old_at.get(vertex.point) or RouteWaypointId.new()
        points.append(RouteWaypoint(identifier, vertex.x, vertex.y,
                                    RouteWaypointSource.AUTOMATIC, False))
    return replace(route, waypoints=tuple(points))


def reflow_degree_two_connections(
    model: ElectricalModel,
    representations: Mapping[GraphicalRepresentationId, GraphicalRepresentation],
    routes: Mapping[DiagramRouteId, DiagramRoute],
    moved_ids: Iterable[GraphicalRepresentationId],
    *,
    endpoint_geometry: EndpointGeometry,
    obstacles_for_pair: PairObstacles = lambda first, second: (),
) -> tuple[dict[GraphicalRepresentationId, GraphicalRepresentation],
           dict[DiagramRouteId, DiagramRoute]]:
    """Return new graphics maps; inputs and all electrical objects are untouched.

    ``representations`` already contains the requested apparatus positions.
    Callers must not call this for a no-op move. Geometry callbacks must resolve
    those same positions in preview and commit. Pinned bends and explicitly
    moved/pinned nodes retain their positions and are left to normal rerouting.
    """
    moved = set(moved_ids)
    result_reps, result_routes = dict(representations), dict(routes)
    if not moved:
        return result_reps, result_routes
    degree = Counter(connection.electrical_node_id for connection in model.connections.values())
    incident = defaultdict(list)
    for route in routes.values():
        for at_start, anchor in ((True, route.start_anchor), (False, route.end_anchor)):
            if anchor.target_port_id is None:
                incident[anchor.representation_id].append((route, at_start))

    for node_id in sorted(incident, key=lambda value: value.value):
        node = representations.get(node_id)
        if (node is None or node.id in moved or node.electrical_node_id is None
                or node.symbol_key.casefold() != "connection_point"
                or degree[node.electrical_node_id] != 2):
            continue
        # Optional extension flags must not pollute the strict graphics schema.
        if any(node.extensions.get(key) for key in
               ("position_pinned", "position_locked", "locked")):
            continue
        legs = sorted(incident[node.id], key=lambda value: value[0].id.value)
        if len(legs) != 2 or legs[0][0].id == legs[1][0].id:
            continue
        ends = []
        eligible = True
        for route, node_at_start in legs:
            anchor = route.start_anchor if node_at_start else route.end_anchor
            other = route.end_anchor if node_at_start else route.start_anchor
            other_row = representations.get(other.representation_id)
            connection = (model.connection_for_port(other.target_port_id)
                          if other.target_port_id is not None else None)
            if (route.kind is not DiagramRouteKind.NODE_CONNECTION
                    or route.electrical_node_id != node.electrical_node_id
                    or route.page_id != node.page_id
                    or anchor.kind is not RouteAnchorKind.ELECTRICAL_NODE
                    or other.kind is not RouteAnchorKind.EQUIPMENT_PORT
                    or other_row is None or other_row.equipment_id is None
                    or other_row.page_id != node.page_id
                    or connection is None
                    or connection.electrical_node_id != node.electrical_node_id
                    or any(point.pinned for point in route.waypoints[1:-1])):
                eligible = False
                break
            fallback = route.waypoints[-1 if node_at_start else 0]
            ends.append((other, fallback))
        if (not eligible or not any(anchor.representation_id in moved for anchor, _ in ends)
                or ends[0][0].target_port_id == ends[1][0].target_port_id):
            continue
        start, start_direction = endpoint_geometry(*ends[0])
        end, end_direction = endpoint_geometry(*ends[1])
        vertices = build_orthogonal_route(RoutingRequest(
            start, end, start_direction, end_direction,
            obstacles=tuple(obstacles_for_pair(legs[0][0], legs[1][0])), port_stub=12.0,
        ))
        middle, first_leg, second_leg = _split_midway(vertices)
        first, first_node_at_start = legs[0]
        second, second_node_at_start = legs[1]
        if first_node_at_start:
            first_leg = tuple(reversed(first_leg))
        if not second_node_at_start:
            second_leg = tuple(reversed(second_leg))
        result_reps[node.id] = replace(node, x=middle.x, y=middle.y)
        result_routes[first.id] = _with_vertices(first, first_leg)
        result_routes[second.id] = _with_vertices(second, second_leg)
    return result_reps, result_routes


__all__ = ["reflow_degree_two_connections"]
