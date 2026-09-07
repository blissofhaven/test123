"""Pure graphical replacement of a split branch on every saved sheet."""
from dataclasses import replace
import math

from rza_calc.domain.diagram import (DiagramRouteId, GraphicalRepresentation,
    GraphicalRepresentationId, RepresentationTargetKind, RouteAnchorKind,
    RouteEndpointAnchor, RouteWaypoint, RouteWaypointId)
from rza_calc.domain.electrical import DomainInvariantError


def split_route_points(route, x, y):
    """Preserve old bends/IDs and introduce distinct endpoint IDs at the cut."""
    def same(point):
        return math.isclose(point.x, x, abs_tol=1e-8, rel_tol=0) and math.isclose(point.y, y, abs_tol=1e-8, rel_tol=0)
    if same(route.waypoints[0]) or same(route.waypoints[-1]):
        raise DomainInvariantError("Отпайка должна находиться внутри графической трассы, не на её конце.")
    index = next((index for index, (a, b) in enumerate(zip(route.waypoints, route.waypoints[1:]))
        if min(a.x, b.x) - 1e-8 <= x <= max(a.x, b.x) + 1e-8
        and min(a.y, b.y) - 1e-8 <= y <= max(a.y, b.y) + 1e-8
        and ((a.x == b.x and math.isclose(x, a.x, abs_tol=1e-8, rel_tol=0))
             or (a.y == b.y and math.isclose(y, a.y, abs_tol=1e-8, rel_tol=0)))), None)
    if index is None:
        raise DomainInvariantError("Точка отпайки больше не лежит на выбранной физической линии.")
    left, right = list(route.waypoints[:index + 1]), list(route.waypoints[index + 1:])
    if not same(left[-1]):
        left.append(RouteWaypoint(RouteWaypointId.new(), x, y))
    if not same(right[0]):
        right.insert(0, RouteWaypoint(RouteWaypointId.new(), x, y))
    return tuple(left), tuple(right)


def split_physical_route_views(diagram, source_routes, *, original_from_port,
        first_equipment_id, second_equipment_id, first_ports, second_ports,
        tap_node_id, selected_route_id, selected_point, midpoint, outer_anchor):
    """Return replacement graphics and the selected tap, without model writes.

    The selected sheet uses the exact hit. Other views receive a graphical
    midpoint; no drawing distance is ever interpreted as physical length.
    ``outer_anchor`` repairs a target-terminal attachment detached by a wire
    reconnect and may return an extra representation for that old node.
    """
    source_ids = {route.id for route in source_routes}
    routes = {key: row for key, row in diagram.routes.items() if key not in source_ids}
    representations = dict(diagram.representations)
    created, selected_tap = [], None
    for saved in source_routes:
        route = saved
        if route.start_anchor.branch_port_id != original_from_port:
            if route.end_anchor.branch_port_id != original_from_port:
                raise DomainInvariantError("Графическая ветвь не содержит исходный порт начала линии.")
            route = replace(route, start_anchor=route.end_anchor, end_anchor=route.start_anchor,
                            waypoints=tuple(reversed(route.waypoints)))
        x, y = selected_point if saved.id == selected_route_id else midpoint(route)
        left, right = split_route_points(route, x, y)
        tap = GraphicalRepresentation(GraphicalRepresentationId.new(), route.page_id,
            RepresentationTargetKind.ELECTRICAL_NODE, electrical_node_id=tap_node_id,
            x=x, y=y, symbol_key="connection_point",
            extensions={"graphics": {"width": 8.0, "height": 8.0, "label_visible": False}})
        representations[tap.id] = tap
        if saved.id == selected_route_id:
            selected_tap = tap
        anchors = []
        for anchor, point in ((route.start_anchor, left[0]), (route.end_anchor, right[-1])):
            anchor, extra = outer_anchor(anchor, point, route.page_id)
            if extra is not None:
                representations[extra.id] = extra
            anchors.append(anchor)
        middle = RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, tap.id, tap_node_id)
        pieces = (
            replace(route, equipment_id=first_equipment_id,
                start_anchor=replace(anchors[0], branch_port_id=first_ports[0]),
                end_anchor=replace(middle, branch_port_id=first_ports[1]), waypoints=left),
            replace(route, id=DiagramRouteId.new(), equipment_id=second_equipment_id,
                start_anchor=replace(middle, branch_port_id=second_ports[0]),
                end_anchor=replace(anchors[1], branch_port_id=second_ports[1]), waypoints=right),
        )
        for piece in pieces:
            routes[piece.id] = piece
            created.append(piece.id)
    if selected_tap is None:
        raise DomainInvariantError("Выбранная графическая трасса отпайки отсутствует.")
    return replace(diagram, representations=representations, routes=routes), selected_tap, tuple(created)


def separate_detached_terminals(diagram, detached, *, obstacles_for_page, reroute):
    """Withdraw each old common endpoint locally after a terminal reconnect.

    A shared fallback representation keeps every old lead together. Candidate
    offsets are drawing units only and never modify electrical source data.
    """
    def on_segment(point, a, b):
        return (min(a.x, b.x) - 1e-7 <= point[0] <= max(a.x, b.x) + 1e-7
            and min(a.y, b.y) - 1e-7 <= point[1] <= max(a.y, b.y) + 1e-7
            and abs((point[0] - a.x) * (b.y - a.y) - (point[1] - a.y) * (b.x - a.x)) < 1e-7)

    def overlaps(first, second):
        for a, b in zip(first.waypoints, first.waypoints[1:]):
            for c, d in zip(second.waypoints, second.waypoints[1:]):
                if a.x == b.x == c.x == d.x:
                    if min(max(a.y, b.y), max(c.y, d.y)) - max(min(a.y, b.y), min(c.y, d.y)) > 1e-7:
                        return True
                if a.y == b.y == c.y == d.y:
                    if min(max(a.x, b.x), max(c.x, d.x)) - max(min(a.x, b.x), min(c.x, d.x)) > 1e-7:
                        return True
        return False

    routes, representations = dict(diagram.routes), dict(diagram.representations)
    for row in detached:
        attached = [route for route in routes.values()
            if row.id in (route.start_anchor.representation_id, route.end_anchor.representation_id)]
        attached_ids = {route.id for route in attached}
        others = [route for route in routes.values() if route.page_id == row.page_id and route.id not in attached_ids]
        obstacles = obstacles_for_page(row.page_id)
        for dx, dy in ((20, 0), (-20, 0), (0, 20), (0, -20), (20, 20), (-20, 20), (20, -20), (-20, -20)):
            point = row.x + dx, row.y + dy
            if any(o.left - 2 <= point[0] <= o.right + 2 and o.top - 2 <= point[1] <= o.bottom + 2 for o in obstacles):
                continue
            if any(on_segment(point, a, b) for route in others for a, b in zip(route.waypoints, route.waypoints[1:])):
                continue
            chosen = None
            # Prefer the original exit at a fixed junction. A new wire may
            # occupy precisely that ray, so also try an unconstrained exit.
            # Both alternatives must satisfy the same separation checks.
            for preserve_direction in (True, False):
                candidates = []
                try:
                    for route in attached:
                        waypoints = list(route.waypoints)
                        if route.start_anchor.representation_id == row.id:
                            old, peer = waypoints[0], waypoints[1]
                            changed = replace(old, x=point[0], y=point[1])
                            corner = (old.x, point[1]) if old.x == peer.x else (point[0], old.y)
                            waypoints[0] = changed
                            if corner not in ((changed.x, changed.y), (peer.x, peer.y)):
                                waypoints.insert(1, RouteWaypoint(RouteWaypointId.new(), *corner))
                        if route.end_anchor.representation_id == row.id:
                            old, peer = waypoints[-1], waypoints[-2]
                            changed = replace(old, x=point[0], y=point[1])
                            corner = (old.x, point[1]) if old.x == peer.x else (point[0], old.y)
                            waypoints[-1] = changed
                            if corner not in ((changed.x, changed.y), (peer.x, peer.y)):
                                waypoints.insert(-1, RouteWaypoint(RouteWaypointId.new(), *corner))
                        candidates.append(reroute(replace(route, waypoints=tuple(waypoints)), obstacles, preserve_direction))
                except ValueError:
                    continue
                if any(overlaps(candidate, other) for candidate in candidates for other in others):
                    continue
                chosen = candidates
                break
            if chosen is None:
                continue
            routes.update((route.id, route) for route in chosen)
            representations[row.id] = replace(row, x=point[0], y=point[1])
            break
        else:
            raise DomainInvariantError("Не удалось безопасно отвести отключённый конец линии от вывода. Освободите место рядом с аппаратом и повторите соединение.")
    return replace(diagram, representations=representations, routes=routes)
