# -*- coding: utf-8 -*-
"""Строгий JSON codec для data-only Diagram Model v2."""
from __future__ import annotations

import math
from typing import Any

from ..domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
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
    RoutePoint,
    RouteWaypoint,
    RouteWaypointId,
    RouteWaypointSource,
)
from ..domain.electrical import (
    ElectricalModel,
    ElectricalNodeId,
    EquipmentId,
    PortId,
    thaw_json,
)


DIAGRAM_FORMAT_VERSION = 2


class DiagramFormatError(ValueError):
    """JSON графической модели не соответствует схеме v2."""


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiagramFormatError(f"{context}: ожидался объект JSON.")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise DiagramFormatError(f"{context}: ожидался массив JSON.")
    return value


def _strict(value: Any, fields: set[str], context: str) -> dict[str, Any]:
    row = _object(value, context)
    unknown = set(row) - fields
    missing = fields - set(row)
    if unknown:
        raise DiagramFormatError(f"{context}: неизвестные поля {sorted(unknown)}.")
    if missing:
        raise DiagramFormatError(f"{context}: отсутствуют поля {sorted(missing)}.")
    return row


def _string(value: Any, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise DiagramFormatError(f"{context}: ожидалась {'строка' if empty else 'непустая строка'}.")
    return value


def _integer(value: Any, context: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
        raise DiagramFormatError(f"{context}: ожидалось целое число.")
    return value


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagramFormatError(f"{context}: ожидалось число.")
    result = float(value)
    if not math.isfinite(result):
        raise DiagramFormatError(f"{context}: NaN и Infinity недопустимы.")
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise DiagramFormatError(f"{context}: ожидалось логическое значение.")
    return value


def _nullable_id(value: Any, cls, context: str):
    return None if value is None else cls(_string(value, context))


def _anchor_to_dict(anchor: RouteEndpointAnchor) -> dict[str, Any]:
    return {
        "kind": anchor.kind.value,
        "representation_id": anchor.representation_id.value,
        "electrical_node_id": anchor.electrical_node_id.value,
        "branch_port_id": (
            anchor.branch_port_id.value
            if anchor.branch_port_id is not None
            else None
        ),
        "target_port_id": (
            anchor.target_port_id.value
            if anchor.target_port_id is not None
            else None
        ),
        "anchor_key": anchor.anchor_key,
    }


def _anchor_from_dict(value: Any, context: str) -> RouteEndpointAnchor:
    row = _strict(
        value,
        {
            "kind",
            "representation_id",
            "electrical_node_id",
            "branch_port_id",
            "target_port_id",
            "anchor_key",
        },
        context,
    )
    try:
        kind = RouteAnchorKind(_string(row["kind"], context + ".kind"))
    except ValueError as exc:
        raise DiagramFormatError(f"{context}.kind: неизвестное значение.") from exc
    try:
        return RouteEndpointAnchor(
            kind,
            GraphicalRepresentationId(
                _string(row["representation_id"], context + ".representation_id")
            ),
            ElectricalNodeId(
                _string(row["electrical_node_id"], context + ".electrical_node_id")
            ),
            _nullable_id(
                row["branch_port_id"], PortId, context + ".branch_port_id"
            ),
            _nullable_id(
                row["target_port_id"], PortId, context + ".target_port_id"
            ),
            _string(row["anchor_key"], context + ".anchor_key", empty=True),
        )
    except Exception as exc:
        if isinstance(exc, DiagramFormatError):
            raise
        raise DiagramFormatError(f"{context}: {exc}") from exc


def diagram_to_dict(document: DiagramDocument) -> dict[str, Any]:
    if not isinstance(document, DiagramDocument):
        raise TypeError("diagram_to_dict ожидает DiagramDocument.")
    return {
        "format_version": DIAGRAM_FORMAT_VERSION,
        "id": document.id.value,
        "name": document.name,
        "revision": document.revision,
        "pages": [
            {
                "id": item.id.value,
                "name": item.name,
                "parent_id": item.parent_id.value if item.parent_id else None,
                "order": item.order,
                "extensions": thaw_json(item.extensions),
            }
            for item in document.pages.values()
        ],
        "representations": [
            {
                "id": item.id.value,
                "page_id": item.page_id.value,
                "target_kind": item.target_kind.value,
                "equipment_id": item.equipment_id.value if item.equipment_id else None,
                "electrical_node_id": item.electrical_node_id.value if item.electrical_node_id else None,
                "x": item.x,
                "y": item.y,
                "rotation_deg": item.rotation_deg,
                "z_index": item.z_index,
                "symbol_key": item.symbol_key,
                "label": item.label,
                "route_points": [
                    {"x": point.x, "y": point.y} for point in item.route_points
                ],
                "extensions": thaw_json(item.extensions),
            }
            for item in document.representations.values()
        ],
        "routes": [
            {
                "id": item.id.value,
                "page_id": item.page_id.value,
                "kind": item.kind.value,
                "equipment_id": (
                    item.equipment_id.value if item.equipment_id is not None else None
                ),
                "electrical_node_id": (
                    item.electrical_node_id.value
                    if item.electrical_node_id is not None
                    else None
                ),
                "start_anchor": _anchor_to_dict(item.start_anchor),
                "end_anchor": _anchor_to_dict(item.end_anchor),
                "waypoints": [
                    {
                        "id": point.id.value,
                        "x": point.x,
                        "y": point.y,
                        "source": point.source.value,
                        "pinned": point.pinned,
                    }
                    for point in item.waypoints
                ],
                "routing_algorithm_version": item.routing_algorithm_version,
                "extensions": thaw_json(item.extensions),
            }
            for item in document.routes.values()
        ],
        "extensions": thaw_json(document.extensions),
    }


def diagram_from_dict(
    value: Any, electrical_model: ElectricalModel | None = None
) -> DiagramDocument:
    root = _strict(
        value,
        {
            "format_version",
            "id",
            "name",
            "revision",
            "pages",
            "representations",
            "routes",
            "extensions",
        },
        "diagram",
    )
    version = _integer(root["format_version"], "diagram.format_version", minimum=1)
    if version != DIAGRAM_FORMAT_VERSION:
        raise DiagramFormatError(f"Формат Diagram Model v{version} не поддерживается.")

    pages: dict[PageId, DiagramPage] = {}
    for index, raw in enumerate(_array(root["pages"], "diagram.pages")):
        context = f"diagram.pages[{index}]"
        row = _strict(raw, {"id", "name", "parent_id", "order", "extensions"}, context)
        page_id = PageId(_string(row["id"], context + ".id"))
        if page_id in pages:
            raise DiagramFormatError(f"{context}: ID '{page_id}' повторяется.")
        parent_id = row["parent_id"]
        pages[page_id] = DiagramPage(
            page_id,
            _string(row["name"], context + ".name"),
            None if parent_id is None else PageId(_string(parent_id, context + ".parent_id")),
            _integer(row["order"], context + ".order"),
            _object(row["extensions"], context + ".extensions"),
        )

    representations: dict[GraphicalRepresentationId, GraphicalRepresentation] = {}
    for index, raw in enumerate(_array(root["representations"], "diagram.representations")):
        context = f"diagram.representations[{index}]"
        row = _strict(
            raw,
            {
                "id", "page_id", "target_kind", "equipment_id", "electrical_node_id",
                "x", "y", "rotation_deg", "z_index", "symbol_key", "label",
                "route_points", "extensions",
            },
            context,
        )
        representation_id = GraphicalRepresentationId(_string(row["id"], context + ".id"))
        if representation_id in representations:
            raise DiagramFormatError(f"{context}: ID '{representation_id}' повторяется.")
        try:
            target_kind = RepresentationTargetKind(_string(row["target_kind"], context + ".target_kind"))
        except ValueError as exc:
            raise DiagramFormatError(f"{context}.target_kind: неизвестное значение.") from exc
        equipment_id = row["equipment_id"]
        node_id = row["electrical_node_id"]
        points: list[RoutePoint] = []
        for point_index, raw_point in enumerate(_array(row["route_points"], context + ".route_points")):
            point_context = f"{context}.route_points[{point_index}]"
            point = _strict(raw_point, {"x", "y"}, point_context)
            points.append(RoutePoint(_number(point["x"], point_context + ".x"), _number(point["y"], point_context + ".y")))
        representations[representation_id] = GraphicalRepresentation(
            representation_id,
            PageId(_string(row["page_id"], context + ".page_id")),
            target_kind,
            None if equipment_id is None else EquipmentId(_string(equipment_id, context + ".equipment_id")),
            None if node_id is None else ElectricalNodeId(_string(node_id, context + ".electrical_node_id")),
            _number(row["x"], context + ".x"),
            _number(row["y"], context + ".y"),
            _number(row["rotation_deg"], context + ".rotation_deg"),
            _integer(row["z_index"], context + ".z_index"),
            _string(row["symbol_key"], context + ".symbol_key", empty=True),
            _string(row["label"], context + ".label", empty=True),
            tuple(points),
            _object(row["extensions"], context + ".extensions"),
        )

    routes: dict[DiagramRouteId, DiagramRoute] = {}
    for index, raw in enumerate(_array(root["routes"], "diagram.routes")):
        context = f"diagram.routes[{index}]"
        row = _strict(
            raw,
            {
                "id",
                "page_id",
                "kind",
                "equipment_id",
                "electrical_node_id",
                "start_anchor",
                "end_anchor",
                "waypoints",
                "routing_algorithm_version",
                "extensions",
            },
            context,
        )
        route_id = DiagramRouteId(_string(row["id"], context + ".id"))
        if route_id in routes:
            raise DiagramFormatError(f"{context}: ID '{route_id}' повторяется.")
        try:
            kind = DiagramRouteKind(_string(row["kind"], context + ".kind"))
        except ValueError as exc:
            raise DiagramFormatError(f"{context}.kind: неизвестное значение.") from exc
        points: list[RouteWaypoint] = []
        for point_index, raw_point in enumerate(
            _array(row["waypoints"], context + ".waypoints")
        ):
            point_context = f"{context}.waypoints[{point_index}]"
            point = _strict(
                raw_point,
                {"id", "x", "y", "source", "pinned"},
                point_context,
            )
            try:
                source = RouteWaypointSource(
                    _string(point["source"], point_context + ".source")
                )
            except ValueError as exc:
                raise DiagramFormatError(
                    f"{point_context}.source: неизвестное значение."
                ) from exc
            points.append(RouteWaypoint(
                RouteWaypointId(_string(point["id"], point_context + ".id")),
                _number(point["x"], point_context + ".x"),
                _number(point["y"], point_context + ".y"),
                source,
                _boolean(point["pinned"], point_context + ".pinned"),
            ))
        try:
            routes[route_id] = DiagramRoute(
                route_id,
                PageId(_string(row["page_id"], context + ".page_id")),
                kind,
                _anchor_from_dict(row["start_anchor"], context + ".start_anchor"),
                _anchor_from_dict(row["end_anchor"], context + ".end_anchor"),
                _nullable_id(row["equipment_id"], EquipmentId, context + ".equipment_id"),
                _nullable_id(
                    row["electrical_node_id"],
                    ElectricalNodeId,
                    context + ".electrical_node_id",
                ),
                tuple(points),
                _integer(
                    row["routing_algorithm_version"],
                    context + ".routing_algorithm_version",
                    minimum=1,
                ),
                _object(row["extensions"], context + ".extensions"),
            )
        except Exception as exc:
            if isinstance(exc, DiagramFormatError):
                raise
            raise DiagramFormatError(f"{context}: {exc}") from exc

    try:
        document = DiagramDocument(
            DiagramDocumentId(_string(root["id"], "diagram.id")),
            _string(root["name"], "diagram.name"),
            pages,
            representations,
            routes,
            _integer(root["revision"], "diagram.revision", minimum=0),
            _object(root["extensions"], "diagram.extensions"),
        )
        if electrical_model is not None:
            document.require_valid_targets(electrical_model)
        return document
    except Exception as exc:
        if isinstance(exc, DiagramFormatError):
            raise
        raise DiagramFormatError(f"diagram: {exc}") from exc


__all__ = [
    "DIAGRAM_FORMAT_VERSION",
    "DiagramFormatError",
    "diagram_from_dict",
    "diagram_to_dict",
]
