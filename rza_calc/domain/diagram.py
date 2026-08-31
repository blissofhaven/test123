# -*- coding: utf-8 -*-
"""Данные графического представления, не определяющие электрическую связность.

Один ``ElectricalNodeId`` может иметь любое число представлений на
разных страницах. Межсхемный порт — только такое представление.
Координаты и точки маршрута никогда не передаются в ``ElectricalModel``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .electrical import (
    Connection,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNodeId,
    EquipmentId,
    PortId,
    StableId,
)


class DiagramDocumentId(StableId):
    prefix = "diagram"


class PageId(StableId):
    prefix = "page"


class GraphicalRepresentationId(StableId):
    prefix = "representation"


class DiagramRouteId(StableId):
    prefix = "route"


class RouteWaypointId(StableId):
    prefix = "waypoint"


class RepresentationTargetKind(StrEnum):
    EQUIPMENT = "equipment"
    ELECTRICAL_NODE = "electrical_node"


class DiagramRouteKind(StrEnum):
    """Electrical meaning of one persisted graphical route."""

    NODE_CONNECTION = "node_connection"
    EQUIPMENT_BRANCH = "equipment_branch"


class RouteAnchorKind(StrEnum):
    EQUIPMENT_PORT = "equipment_port"
    ELECTRICAL_NODE = "electrical_node"
    BUS = "bus"
    CROSS_DIAGRAM_PORT = "cross_diagram_port"


class RouteWaypointSource(StrEnum):
    AUTOMATIC = "automatic"
    USER = "user"


def _finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainInvariantError(f"{field_name} должно быть числом.")
    result = float(value)
    if not math.isfinite(result):
        raise DomainInvariantError(f"{field_name}: NaN и Infinity недопустимы.")
    return result


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainInvariantError("NaN и Infinity нельзя хранить в Diagram Model.")
        return value
    if isinstance(value, StableId):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DomainInvariantError("Ключ JSON-объекта Diagram Model должен быть непустой строкой.")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise DomainInvariantError(f"Тип {type(value).__name__} нельзя хранить в Diagram Model.")


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """Deprecated Stage-3 polyline point kept for lossless compatibility.

    New electrical connections use :class:`DiagramRoute` and
    :class:`RouteWaypoint`.  This value remains on a graphical representation
    so an old project is never silently stripped of geometry when its
    electrical meaning cannot be established during migration.
    """

    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "RoutePoint.x"))
        object.__setattr__(self, "y", _finite(self.y, "RoutePoint.y"))


@dataclass(frozen=True, slots=True)
class RouteEndpointAnchor:
    """Stable binding of one route end to an existing diagram/domain object."""

    kind: RouteAnchorKind
    representation_id: GraphicalRepresentationId
    electrical_node_id: ElectricalNodeId
    branch_port_id: PortId | None = None
    target_port_id: PortId | None = None
    anchor_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RouteAnchorKind):
            try:
                object.__setattr__(self, "kind", RouteAnchorKind(self.kind))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError("Неизвестный вид привязки конца трассы.") from exc
        if not isinstance(self.representation_id, GraphicalRepresentationId):
            raise DomainInvariantError(
                "RouteEndpointAnchor.representation_id должен быть "
                "GraphicalRepresentationId."
            )
        if not isinstance(self.electrical_node_id, ElectricalNodeId):
            raise DomainInvariantError(
                "RouteEndpointAnchor.electrical_node_id должен быть ElectricalNodeId."
            )
        if self.branch_port_id is not None and not isinstance(
            self.branch_port_id, PortId
        ):
            raise DomainInvariantError(
                "RouteEndpointAnchor.branch_port_id должен быть PortId или null."
            )
        if self.target_port_id is not None and not isinstance(
            self.target_port_id, PortId
        ):
            raise DomainInvariantError(
                "RouteEndpointAnchor.target_port_id должен быть PortId или null."
            )
        if (
            self.kind is RouteAnchorKind.EQUIPMENT_PORT
            and self.target_port_id is None
        ):
            raise DomainInvariantError(
                "Привязка к электрическому порту должна содержать target_port_id."
            )
        if (
            self.kind is not RouteAnchorKind.EQUIPMENT_PORT
            and self.target_port_id is not None
        ):
            raise DomainInvariantError(
                "target_port_id допускается только для привязки к порту оборудования."
            )
        if not isinstance(self.anchor_key, str):
            raise DomainInvariantError("RouteEndpointAnchor.anchor_key должен быть строкой.")


@dataclass(frozen=True, slots=True)
class RouteWaypoint:
    id: RouteWaypointId
    x: float
    y: float
    source: RouteWaypointSource = RouteWaypointSource.AUTOMATIC
    pinned: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, RouteWaypointId):
            raise DomainInvariantError("RouteWaypoint.id должен быть RouteWaypointId.")
        object.__setattr__(self, "x", _finite(self.x, "RouteWaypoint.x"))
        object.__setattr__(self, "y", _finite(self.y, "RouteWaypoint.y"))
        if not isinstance(self.source, RouteWaypointSource):
            try:
                object.__setattr__(
                    self, "source", RouteWaypointSource(self.source)
                )
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError("Неизвестный источник точки маршрута.") from exc
        if not isinstance(self.pinned, bool):
            raise DomainInvariantError("RouteWaypoint.pinned должен быть bool.")
        if self.pinned and self.source is not RouteWaypointSource.USER:
            raise DomainInvariantError(
                "Закрепить можно только пользовательскую точку маршрута."
            )


@dataclass(frozen=True, slots=True)
class DiagramRoute:
    """Persisted orthogonal geometry separated from electrical connectivity."""

    id: DiagramRouteId
    page_id: PageId
    kind: DiagramRouteKind
    start_anchor: RouteEndpointAnchor
    end_anchor: RouteEndpointAnchor
    equipment_id: EquipmentId | None = None
    electrical_node_id: ElectricalNodeId | None = None
    waypoints: tuple[RouteWaypoint, ...] = ()
    routing_algorithm_version: int = 1
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, DiagramRouteId):
            raise DomainInvariantError("DiagramRoute.id должен быть DiagramRouteId.")
        if not isinstance(self.page_id, PageId):
            raise DomainInvariantError("DiagramRoute.page_id должен быть PageId.")
        if not isinstance(self.kind, DiagramRouteKind):
            try:
                object.__setattr__(self, "kind", DiagramRouteKind(self.kind))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError("Неизвестный вид графической трассы.") from exc
        if not isinstance(self.start_anchor, RouteEndpointAnchor) or not isinstance(
            self.end_anchor, RouteEndpointAnchor
        ):
            raise DomainInvariantError(
                "Концы DiagramRoute должны содержать RouteEndpointAnchor."
            )
        if self.kind is DiagramRouteKind.NODE_CONNECTION:
            if not isinstance(self.electrical_node_id, ElectricalNodeId):
                raise DomainInvariantError(
                    "Трасса соединения узла должна ссылаться на ElectricalNodeId."
                )
            if self.equipment_id is not None:
                raise DomainInvariantError(
                    "Трасса соединения узла не может ссылаться на оборудование."
                )
            if (
                self.start_anchor.electrical_node_id != self.electrical_node_id
                or self.end_anchor.electrical_node_id != self.electrical_node_id
            ):
                raise DomainInvariantError(
                    "Оба конца графического соединения должны принадлежать одному узлу."
                )
            if (
                self.start_anchor.branch_port_id is not None
                or self.end_anchor.branch_port_id is not None
            ):
                raise DomainInvariantError(
                    "Графическое соединение узла не имеет портов физической ветви."
                )
        else:
            if not isinstance(self.equipment_id, EquipmentId):
                raise DomainInvariantError(
                    "Трасса физической ветви должна ссылаться на EquipmentId."
                )
            if self.electrical_node_id is not None:
                raise DomainInvariantError(
                    "Трасса физической ветви не может иметь electrical_node_id."
                )
            if (
                self.start_anchor.electrical_node_id
                == self.end_anchor.electrical_node_id
            ):
                raise DomainInvariantError(
                    "Физическая ветвь должна соединять два разных электрических узла."
                )
            if (
                self.start_anchor.branch_port_id is None
                or self.end_anchor.branch_port_id is None
            ):
                raise DomainInvariantError(
                    "Оба конца физической ветви должны хранить её собственный PortId."
                )
            if (
                self.start_anchor.branch_port_id
                == self.end_anchor.branch_port_id
            ):
                raise DomainInvariantError(
                    "Начало и конец физической ветви должны использовать разные порты."
                )
        points = tuple(self.waypoints)
        if len(points) < 2:
            raise DomainInvariantError(
                "Сохранённая трасса должна содержать не менее двух точек."
            )
        if any(not isinstance(item, RouteWaypoint) for item in points):
            raise DomainInvariantError(
                "DiagramRoute.waypoints должны содержать RouteWaypoint."
            )
        waypoint_ids = [item.id for item in points]
        if len(waypoint_ids) != len(set(waypoint_ids)):
            raise DomainInvariantError("ID точек маршрута не должны повторяться.")
        for first, second in zip(points, points[1:]):
            if first.x == second.x and first.y == second.y:
                raise DomainInvariantError(
                    "Трасса не должна содержать сегменты нулевой длины."
                )
            if first.x != second.x and first.y != second.y:
                raise DomainInvariantError(
                    "Трасса должна состоять только из ортогональных сегментов."
                )
        if (
            isinstance(self.routing_algorithm_version, bool)
            or not isinstance(self.routing_algorithm_version, int)
            or self.routing_algorithm_version < 1
        ):
            raise DomainInvariantError(
                "routing_algorithm_version должен быть положительным целым."
            )
        object.__setattr__(self, "waypoints", points)
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))

    @property
    def target_id(self) -> StableId:
        return self.equipment_id or self.electrical_node_id  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class DiagramPage:
    id: PageId
    name: str
    parent_id: PageId | None = None
    order: int = 0
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, PageId):
            raise DomainInvariantError("DiagramPage.id должен быть PageId.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise DomainInvariantError("Страница должна иметь название.")
        if self.parent_id is not None and not isinstance(self.parent_id, PageId):
            raise DomainInvariantError("DiagramPage.parent_id должен быть PageId или null.")
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise DomainInvariantError("DiagramPage.order должен быть целым числом.")
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class GraphicalRepresentation:
    id: GraphicalRepresentationId
    page_id: PageId
    target_kind: RepresentationTargetKind
    equipment_id: EquipmentId | None = None
    electrical_node_id: ElectricalNodeId | None = None
    x: float = 0.0
    y: float = 0.0
    rotation_deg: float = 0.0
    z_index: int = 0
    symbol_key: str = ""
    label: str = ""
    route_points: tuple[RoutePoint, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, GraphicalRepresentationId):
            raise DomainInvariantError("GraphicalRepresentation.id должен быть GraphicalRepresentationId.")
        if not isinstance(self.page_id, PageId):
            raise DomainInvariantError("GraphicalRepresentation.page_id должен быть PageId.")
        if not isinstance(self.target_kind, RepresentationTargetKind):
            try:
                object.__setattr__(self, "target_kind", RepresentationTargetKind(self.target_kind))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError("Неизвестный вид цели графического представления.") from exc
        if self.target_kind is RepresentationTargetKind.EQUIPMENT:
            if not isinstance(self.equipment_id, EquipmentId) or self.electrical_node_id is not None:
                raise DomainInvariantError("Представление equipment должно ссылаться ровно на EquipmentId.")
        else:
            if not isinstance(self.electrical_node_id, ElectricalNodeId) or self.equipment_id is not None:
                raise DomainInvariantError("Представление electrical_node должно ссылаться ровно на ElectricalNodeId.")
        object.__setattr__(self, "x", _finite(self.x, "GraphicalRepresentation.x"))
        object.__setattr__(self, "y", _finite(self.y, "GraphicalRepresentation.y"))
        object.__setattr__(self, "rotation_deg", _finite(self.rotation_deg, "GraphicalRepresentation.rotation_deg"))
        if isinstance(self.z_index, bool) or not isinstance(self.z_index, int):
            raise DomainInvariantError("GraphicalRepresentation.z_index должен быть целым.")
        if not isinstance(self.symbol_key, str) or not isinstance(self.label, str):
            raise DomainInvariantError("symbol_key и label должны быть строками.")
        points = tuple(self.route_points)
        if any(not isinstance(item, RoutePoint) for item in points):
            raise DomainInvariantError("route_points должны содержать RoutePoint.")
        object.__setattr__(self, "route_points", points)
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))

    @property
    def target_id(self) -> StableId:
        return self.equipment_id or self.electrical_node_id  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class DiagramDocument:
    id: DiagramDocumentId
    name: str
    pages: Mapping[PageId, DiagramPage] = field(default_factory=dict)
    representations: Mapping[GraphicalRepresentationId, GraphicalRepresentation] = field(default_factory=dict)
    routes: Mapping[DiagramRouteId, DiagramRoute] = field(default_factory=dict)
    revision: int = 0
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, DiagramDocumentId):
            raise DomainInvariantError("DiagramDocument.id должен быть DiagramDocumentId.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise DomainInvariantError("DiagramDocument.name должно быть непустой строкой.")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise DomainInvariantError("DiagramDocument.revision должна быть целой и неотрицательной.")
        pages: dict[PageId, DiagramPage] = {}
        for page_id, page in self.pages.items():
            if not isinstance(page_id, PageId) or not isinstance(page, DiagramPage) or page.id != page_id:
                raise DomainInvariantError("DiagramDocument.pages имеет неверную запись.")
            pages[page_id] = page
        for page in pages.values():
            if page.parent_id is not None and page.parent_id not in pages:
                raise DomainInvariantError(f"Страница '{page.id}' ссылается на отсутствующего родителя.")
            seen: set[PageId] = set()
            current: PageId | None = page.id
            while current is not None:
                if current in seen:
                    raise DomainInvariantError(f"Иерархия страниц содержит цикл с '{current}'.")
                seen.add(current)
                current = pages[current].parent_id

        representations: dict[GraphicalRepresentationId, GraphicalRepresentation] = {}
        for representation_id, representation in self.representations.items():
            if (
                not isinstance(representation_id, GraphicalRepresentationId)
                or not isinstance(representation, GraphicalRepresentation)
                or representation.id != representation_id
            ):
                raise DomainInvariantError("DiagramDocument.representations имеет неверную запись.")
            if representation.page_id not in pages:
                raise DomainInvariantError(f"Представление '{representation.id}' ссылается на отсутствующую страницу.")
            representations[representation_id] = representation

        routes: dict[DiagramRouteId, DiagramRoute] = {}
        for route_id, route in self.routes.items():
            if (
                not isinstance(route_id, DiagramRouteId)
                or not isinstance(route, DiagramRoute)
                or route.id != route_id
            ):
                raise DomainInvariantError(
                    "DiagramDocument.routes имеет неверную запись."
                )
            if route.page_id not in pages:
                raise DomainInvariantError(
                    f"Трасса '{route.id}' ссылается на отсутствующую страницу."
                )
            for anchor in (route.start_anchor, route.end_anchor):
                if anchor.representation_id not in representations:
                    raise DomainInvariantError(
                        f"Трасса '{route.id}' ссылается на отсутствующее "
                        f"представление '{anchor.representation_id}'."
                    )
                if representations[anchor.representation_id].page_id != route.page_id:
                    raise DomainInvariantError(
                        f"Привязка трассы '{route.id}' находится на другой странице."
                    )
            routes[route_id] = route

        all_values = [
            self.id.value,
            *(item.value for item in pages),
            *(item.value for item in representations),
            *(item.value for item in routes),
            *(
                point.id.value
                for route in routes.values()
                for point in route.waypoints
            ),
        ]
        if len(all_values) != len(set(all_values)):
            raise DomainInvariantError(
                "ID документа, страниц, представлений, трасс и точек маршрута "
                "должны быть уникальны."
            )
        object.__setattr__(self, "pages", MappingProxyType(dict(sorted(pages.items(), key=lambda item: item[0].value))))
        object.__setattr__(self, "representations", MappingProxyType(dict(sorted(representations.items(), key=lambda item: item[0].value))))
        object.__setattr__(self, "routes", MappingProxyType(dict(sorted(routes.items(), key=lambda item: item[0].value))))
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))

    @classmethod
    def create(
        cls,
        name: str,
        pages: Iterable[DiagramPage] = (),
        representations: Iterable[GraphicalRepresentation] = (),
        *,
        routes: Iterable[DiagramRoute] = (),
        document_id: DiagramDocumentId | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> "DiagramDocument":
        page_map: dict[PageId, DiagramPage] = {}
        for page in pages:
            if page.id in page_map:
                raise DomainInvariantError(f"Страница '{page.id}' указана дважды.")
            page_map[page.id] = page
        representation_map: dict[GraphicalRepresentationId, GraphicalRepresentation] = {}
        for representation in representations:
            if representation.id in representation_map:
                raise DomainInvariantError(f"Представление '{representation.id}' указано дважды.")
            representation_map[representation.id] = representation
        route_map: dict[DiagramRouteId, DiagramRoute] = {}
        for route in routes:
            if route.id in route_map:
                raise DomainInvariantError(f"Трасса '{route.id}' указана дважды.")
            route_map[route.id] = route
        return cls(
            document_id or DiagramDocumentId.new(),
            name,
            page_map,
            representation_map,
            route_map,
            0,
            extensions or {},
        )

    def representations_for_node(
        self, node_id: ElectricalNodeId
    ) -> tuple[GraphicalRepresentation, ...]:
        return tuple(
            item for item in self.representations.values()
            if item.electrical_node_id == node_id
        )

    def representations_for_equipment(
        self, equipment_id: EquipmentId
    ) -> tuple[GraphicalRepresentation, ...]:
        return tuple(
            item for item in self.representations.values()
            if item.equipment_id == equipment_id
        )

    def routes_for_node(
        self, node_id: ElectricalNodeId
    ) -> tuple[DiagramRoute, ...]:
        return tuple(
            item for item in self.routes.values()
            if item.electrical_node_id == node_id
        )

    def routes_for_equipment(
        self, equipment_id: EquipmentId
    ) -> tuple[DiagramRoute, ...]:
        return tuple(
            item for item in self.routes.values()
            if item.equipment_id == equipment_id
        )

    def validate_targets(self, model: ElectricalModel) -> tuple[str, ...]:
        if not isinstance(model, ElectricalModel):
            raise TypeError("validate_targets ожидает ElectricalModel.")
        problems: list[str] = []
        for item in self.representations.values():
            if item.equipment_id is not None and item.equipment_id not in model.equipment:
                problems.append(f"Представление '{item.id}' ссылается на удалённое оборудование '{item.equipment_id}'.")
            if item.electrical_node_id is not None and item.electrical_node_id not in model.electrical_nodes:
                problems.append(f"Представление '{item.id}' ссылается на удалённый электрический узел '{item.electrical_node_id}'.")
        connection_by_port = {
            item.port_id: item for item in model.connections.values()
        }
        # Индекс повторяет прежнюю семантику буквально: принадлежность порта
        # берётся из EquipmentInstance.port_ids, а не из model.ports. Поэтому
        # даже для диагностируемой повреждённой модели (запись PortInstance
        # потеряна, но Connection и ссылка оборудования ещё существуют)
        # порядок и состав сообщений остаются прежними.
        equipment_ids_by_port: dict[PortId, list[EquipmentId]] = {}
        for equipment in model.equipment.values():
            for port_id in equipment.port_ids:
                equipment_ids_by_port.setdefault(port_id, []).append(
                    equipment.id
                )
        endpoint_nodes_by_equipment: dict[
            EquipmentId, set[ElectricalNodeId]
        ] = {}
        for connection in model.connections.values():
            for equipment_id in equipment_ids_by_port.get(
                connection.port_id, ()
            ):
                endpoint_nodes_by_equipment.setdefault(
                    equipment_id, set()
                ).add(connection.electrical_node_id)
        for route in self.routes.values():
            if route.equipment_id is not None and route.equipment_id not in model.equipment:
                problems.append(
                    f"Трасса '{route.id}' ссылается на удалённое оборудование "
                    f"'{route.equipment_id}'."
                )
            if (
                route.electrical_node_id is not None
                and route.electrical_node_id not in model.electrical_nodes
            ):
                problems.append(
                    f"Трасса '{route.id}' ссылается на удалённый электрический "
                    f"узел '{route.electrical_node_id}'."
                )
            for label, anchor in (
                ("начала", route.start_anchor),
                ("конца", route.end_anchor),
            ):
                if anchor.electrical_node_id not in model.electrical_nodes:
                    problems.append(
                        f"Привязка {label} трассы '{route.id}' ссылается на "
                        f"удалённый узел '{anchor.electrical_node_id}'."
                    )
                representation = self.representations[anchor.representation_id]
                if anchor.target_port_id is not None:
                    port = model.ports.get(anchor.target_port_id)
                    if port is None:
                        problems.append(
                            f"Привязка {label} трассы '{route.id}' ссылается на "
                            f"удалённый целевой порт '{anchor.target_port_id}'."
                        )
                    else:
                        connection = connection_by_port.get(anchor.target_port_id)
                        if (
                            connection is None
                            or connection.electrical_node_id
                            != anchor.electrical_node_id
                        ):
                            problems.append(
                                f"Целевой порт '{anchor.target_port_id}' привязки "
                                f"{label} трассы '{route.id}' не подключён к "
                                f"указанному узлу '{anchor.electrical_node_id}'."
                            )
                        if representation.equipment_id != port.equipment_id:
                            problems.append(
                                f"Привязка {label} трассы '{route.id}' указывает "
                                "целевой порт другого оборудования, чем символ."
                            )
                elif anchor.kind in {
                    RouteAnchorKind.ELECTRICAL_NODE,
                    RouteAnchorKind.BUS,
                    RouteAnchorKind.CROSS_DIAGRAM_PORT,
                } and representation.electrical_node_id != anchor.electrical_node_id:
                    problems.append(
                        f"Привязка {label} трассы '{route.id}' указывает "
                        "графическое представление другого электрического узла."
                    )
                if anchor.branch_port_id is not None:
                    branch_port = model.ports.get(anchor.branch_port_id)
                    if branch_port is None:
                        problems.append(
                            f"Привязка {label} трассы '{route.id}' ссылается на "
                            f"удалённый порт ветви '{anchor.branch_port_id}'."
                        )
                    else:
                        if branch_port.equipment_id != route.equipment_id:
                            problems.append(
                                f"Порт ветви '{anchor.branch_port_id}' трассы "
                                f"'{route.id}' принадлежит другому оборудованию."
                            )
                        connection = connection_by_port.get(anchor.branch_port_id)
                        if (
                            connection is None
                            or connection.electrical_node_id
                            != anchor.electrical_node_id
                        ):
                            problems.append(
                                f"Порт ветви '{anchor.branch_port_id}' привязки "
                                f"{label} трассы '{route.id}' не подключён к "
                                f"указанному узлу '{anchor.electrical_node_id}'."
                            )
            if route.kind is DiagramRouteKind.EQUIPMENT_BRANCH:
                equipment = model.equipment.get(route.equipment_id)  # type: ignore[arg-type]
                if equipment is not None:
                    endpoint_nodes = endpoint_nodes_by_equipment.get(
                        equipment.id, set()
                    )
                    route_nodes = {
                        route.start_anchor.electrical_node_id,
                        route.end_anchor.electrical_node_id,
                    }
                    if endpoint_nodes != route_nodes:
                        problems.append(
                            f"Трасса физической ветви '{route.id}' не совпадает "
                            "с электрическими узлами оборудования."
                        )
        return tuple(problems)

    def require_valid_targets(self, model: ElectricalModel) -> None:
        problems = self.validate_targets(model)
        if problems:
            raise DomainInvariantError("Графическая модель содержит повреждённые ссылки:\n- " + "\n- ".join(problems))

    def moved_representation(
        self,
        representation_id: GraphicalRepresentationId,
        x: float,
        y: float,
    ) -> "DiagramDocument":
        try:
            current = self.representations[representation_id]
        except KeyError as exc:
            raise DomainInvariantError(f"Представление '{representation_id}' не найдено.") from exc
        values = dict(self.representations)
        values[representation_id] = replace(current, x=x, y=y)
        return replace(self, representations=values, revision=self.revision + 1)

    def add_route(self, route: DiagramRoute) -> "DiagramDocument":
        if not isinstance(route, DiagramRoute):
            raise TypeError("add_route ожидает DiagramRoute.")
        if route.id in self.routes:
            raise DomainInvariantError(f"Трасса '{route.id}' уже существует.")
        values = dict(self.routes)
        values[route.id] = route
        return replace(self, routes=values, revision=self.revision + 1)

    def remove_route(self, route_id: DiagramRouteId) -> "DiagramDocument":
        if route_id not in self.routes:
            raise DomainInvariantError(f"Трасса '{route_id}' не найдена.")
        values = dict(self.routes)
        del values[route_id]
        return replace(self, routes=values, revision=self.revision + 1)

    def remove_representation(
        self,
        representation_id: GraphicalRepresentationId,
        *,
        cascade_routes: bool = False,
    ) -> "DiagramDocument":
        if representation_id not in self.representations:
            raise DomainInvariantError(
                f"Представление '{representation_id}' не найдено."
            )
        dependent = tuple(
            route.id for route in self.routes.values()
            if representation_id in {
                route.start_anchor.representation_id,
                route.end_anchor.representation_id,
            }
        )
        if dependent and not cascade_routes:
            raise DomainInvariantError(
                f"Представление '{representation_id}' используется трассами: "
                + ", ".join(item.value for item in dependent)
            )
        representations = dict(self.representations)
        del representations[representation_id]
        routes = {
            route_id: route for route_id, route in self.routes.items()
            if route_id not in dependent
        }
        return replace(
            self,
            representations=representations,
            routes=routes,
            revision=self.revision + 1,
        )

    def rerouted_representation(
        self,
        representation_id: GraphicalRepresentationId,
        route_points: Iterable[RoutePoint],
    ) -> "DiagramDocument":
        try:
            current = self.representations[representation_id]
        except KeyError as exc:
            raise DomainInvariantError(f"Представление '{representation_id}' не найдено.") from exc
        values = dict(self.representations)
        values[representation_id] = replace(current, route_points=tuple(route_points))
        return replace(self, representations=values, revision=self.revision + 1)

    def rerouted_route(
        self,
        route_id: DiagramRouteId,
        waypoints: Iterable[RouteWaypoint],
    ) -> "DiagramDocument":
        try:
            current = self.routes[route_id]
        except KeyError as exc:
            raise DomainInvariantError(f"Трасса '{route_id}' не найдена.") from exc
        values = dict(self.routes)
        values[route_id] = replace(current, waypoints=tuple(waypoints))
        return replace(self, routes=values, revision=self.revision + 1)

    def reanchor_route(
        self,
        route_id: DiagramRouteId,
        start_anchor: RouteEndpointAnchor,
        end_anchor: RouteEndpointAnchor,
    ) -> "DiagramDocument":
        try:
            current = self.routes[route_id]
        except KeyError as exc:
            raise DomainInvariantError(f"Трасса '{route_id}' не найдена.") from exc
        values = dict(self.routes)
        values[route_id] = replace(
            current,
            start_anchor=start_anchor,
            end_anchor=end_anchor,
        )
        return replace(self, routes=values, revision=self.revision + 1)


__all__ = [
    "DiagramDocument",
    "DiagramDocumentId",
    "DiagramPage",
    "DiagramRoute",
    "DiagramRouteId",
    "DiagramRouteKind",
    "GraphicalRepresentation",
    "GraphicalRepresentationId",
    "PageId",
    "RepresentationTargetKind",
    "RouteAnchorKind",
    "RouteEndpointAnchor",
    "RoutePoint",
    "RouteWaypoint",
    "RouteWaypointId",
    "RouteWaypointSource",
]
