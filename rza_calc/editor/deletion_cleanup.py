"""Local cleanup after an explicit delete; never a whole-project sweep.

A loose electrical connection on a surviving apparatus is not garbage.  Its
optional graphical tail may disappear, but the port, node and Connection ID
remain.  Only a node with no connections can be removed from the model.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable

from rza_calc.domain.diagram import (
    DiagramDocument, DiagramRoute, DiagramRouteId, DiagramRouteKind,
    GraphicalRepresentationId, PageId, RouteAnchorKind,
)
from rza_calc.domain.electrical import ElectricalModel, ElectricalNodeId, thaw_json

from .state import UNPLACED_EXTENSION_KEY
from .symbols import canonical_key


@dataclass(frozen=True, slots=True)
class DeletionCleanupResult:
    diagram: DiagramDocument
    node_ids: tuple[ElectricalNodeId, ...] = ()
    representation_ids: tuple[GraphicalRepresentationId, ...] = ()
    route_ids: tuple[DiagramRouteId, ...] = ()


def _route_nodes(route: DiagramRoute) -> set[ElectricalNodeId]:
    return {
        node_id for node_id in (
            route.electrical_node_id,
            route.start_anchor.electrical_node_id,
            route.end_anchor.electrical_node_id,
        ) if node_id is not None
    }


def cleanup_after_deletion(
    before_model: ElectricalModel,
    before_diagram: DiagramDocument,
    model: ElectricalModel,
    diagram: DiagramDocument,
    *,
    affected_page_ids: Iterable[PageId],
    remove_electrical_nodes: bool = True,
) -> DeletionCleanupResult:
    """Clean only points made redundant by this command, on its pages.

    ``remove_electrical_nodes=False`` is the remove-from-page contract: not a
    single electrical object or connection is modified, including loose nodes.
    Busbars, cross-diagram anchors, physical lines and other pages are barriers.
    """
    pages = frozenset(affected_page_ids)
    candidates = {
        connection.electrical_node_id
        for connection_id, connection in before_model.connections.items()
        if model.connections.get(connection_id) != connection
    }
    for route_id, route in before_diagram.routes.items():
        if route_id not in diagram.routes:
            candidates.update(_route_nodes(route))
    for representation_id, representation in before_diagram.representations.items():
        if representation_id not in diagram.representations:
            if representation.electrical_node_id is not None:
                candidates.add(representation.electrical_node_id)

    connections_by_node = defaultdict(list)
    for connection in model.connections.values():
        connections_by_node[connection.electrical_node_id].append(connection)
    representations_by_node = defaultdict(list)
    for representation in diagram.representations.values():
        if representation.electrical_node_id is not None:
            representations_by_node[representation.electrical_node_id].append(representation)
    routes_by_node = defaultdict(list)
    for route in diagram.routes.values():
        for node_id in _route_nodes(route):
            routes_by_node[node_id].append(route)

    removed_nodes: set[ElectricalNodeId] = set()
    removed_representations: set[GraphicalRepresentationId] = set()
    removed_routes: set[DiagramRouteId] = set()
    for node_id in sorted(candidates, key=lambda value: value.value):
        if node_id not in model.electrical_nodes:
            continue
        representations = representations_by_node[node_id]
        routes = routes_by_node[node_id]
        # A representation on another page is intentional use, even if its
        # route is currently hidden.  Never remove it as a side effect here.
        if any(
            row.page_id not in pages
            or canonical_key(row.symbol_key, "electrical_node") != "connection_point"
            for row in representations
        ):
            continue
        point_ids = {row.id for row in representations}
        if any(
            route.page_id not in pages
            or route.kind is not DiagramRouteKind.NODE_CONNECTION
            or _route_nodes(route) != {node_id}
            or any(
                anchor.kind in {RouteAnchorKind.BUS, RouteAnchorKind.CROSS_DIAGRAM_PORT}
                or (
                    anchor.target_port_id is None
                    and anchor.representation_id not in point_ids
                )
                for anchor in (route.start_anchor, route.end_anchor)
            )
            for route in routes
        ):
            continue
        connections = connections_by_node[node_id]
        connected_ports = {row.port_id for row in connections}
        drawn_ports = {
            anchor.target_port_id
            for route in routes
            for anchor in (route.start_anchor, route.end_anchor)
            if anchor.target_port_id is not None
        }
        # A true connection between two visible apparatus must never be
        # mistaken for a tail.  A direct apparatus-to-apparatus wire is not a
        # point tail either, even in an incomplete/corrupt draft.
        if len(drawn_ports) > 1 or any(
            not any(anchor.representation_id in point_ids for anchor in (
                route.start_anchor, route.end_anchor,
            )) for route in routes
        ):
            continue
        if remove_electrical_nodes and (
            len(connected_ports) > 1 or not drawn_ports.issubset(connected_ports)
        ):
            continue
        removed_representations.update(point_ids)
        removed_routes.update(route.id for route in routes)
        if remove_electrical_nodes and not connections:
            # No cascade: this API is itself a final guard against accidentally
            # deleting a surviving neighbour's electrical connection.
            model.remove_node(node_id, cascade=False)
            removed_nodes.add(node_id)

    if not (removed_nodes or removed_representations or removed_routes):
        return DeletionCleanupResult(diagram)
    representations = {
        key: row for key, row in diagram.representations.items()
        if key not in removed_representations
    }
    routes = {
        key: row for key, row in diagram.routes.items()
        if key not in removed_routes
    }
    extensions = thaw_json(diagram.extensions)
    unplaced = set(extensions.get(UNPLACED_EXTENSION_KEY, ()))
    unplaced.difference_update(node_id.value for node_id in removed_nodes)
    represented_nodes = {
        row.electrical_node_id for row in representations.values()
        if row.electrical_node_id is not None
    }
    unplaced.update(
        diagram.representations[key].electrical_node_id.value
        for key in removed_representations
        if diagram.representations[key].electrical_node_id in model.electrical_nodes
        and diagram.representations[key].electrical_node_id not in represented_nodes
    )
    extensions[UNPLACED_EXTENSION_KEY] = sorted(unplaced)
    cleaned = replace(
        diagram, representations=representations, routes=routes,
        extensions=extensions,
    )
    return DeletionCleanupResult(
        cleaned,
        tuple(sorted(removed_nodes, key=lambda value: value.value)),
        tuple(key for key in diagram.representations if key in removed_representations),
        tuple(key for key in diagram.routes if key in removed_routes),
    )
