# -*- coding: utf-8 -*-
"""Qt-независимая геометрия и проверка наложений редактора схем.

Электрическая модель не импортирует и не вызывает этот модуль.  Все фигуры
описывают только графическое представление, поэтому проверка preview,
перемещения или поворота не может изменить электрическую топологию, ревизию
модели или историю undo/redo.

Один прямоугольник намеренно не используется для всех задач.  Для каждого
представления строятся отдельные:

* ``visual_bounds`` — видимый габарит символа;
* ``body_collision_shape`` — твёрдое тело оборудования;
* ``routing_obstacle_shape`` — препятствие для трассировщика;
* ``selection_shape`` — удобная область выбора;
* ``port_connection_zones`` — разрешённые зоны электрического подключения.

Фигуры являются ориентированными прямоугольниками.  Этого достаточно для
текущего набора символов и, в отличие от привязки к ``QGraphicsItem``, даёт
одинаковый результат в GUI, диагностике старого проекта и headless-тестах.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..domain.diagram import (
    DiagramDocument,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
)
from ..domain.electrical import (
    ElectricalModel,
    EquipmentId,
    EquipmentTypeDefinition,
    PortId,
)


DEFAULT_SAFE_GAP = 20.0
DEFAULT_SPATIAL_CELL_SIZE = 160.0
_EPSILON = 1e-9


class CollisionModelError(ValueError):
    """Графическую проверку невозможно выполнить с переданными данными."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollisionModelError(f"{name} должно быть числом.")
    result = float(value)
    if not math.isfinite(result):
        raise CollisionModelError(f"{name} должно быть конечным числом.")
    return result


def _positive(value: Any, name: str) -> float:
    result = _finite(value, name)
    if result <= 0.0:
        raise CollisionModelError(f"{name} должно быть больше нуля.")
    return result


def _non_negative(value: Any, name: str) -> float:
    result = _finite(value, name)
    if result < 0.0:
        raise CollisionModelError(f"{name} не может быть отрицательным.")
    return result


@dataclass(frozen=True, slots=True)
class Point2D:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "Координата X"))
        object.__setattr__(self, "y", _finite(self.y, "Координата Y"))


@dataclass(frozen=True, slots=True)
class AxisAlignedBounds:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        left = _finite(self.left, "Левая граница")
        right = _finite(self.right, "Правая граница")
        top = _finite(self.top, "Верхняя граница")
        bottom = _finite(self.bottom, "Нижняя граница")
        object.__setattr__(self, "left", min(left, right))
        object.__setattr__(self, "right", max(left, right))
        object.__setattr__(self, "top", min(top, bottom))
        object.__setattr__(self, "bottom", max(top, bottom))

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def intersects(self, other: "AxisAlignedBounds") -> bool:
        return not (
            self.right < other.left
            or other.right < self.left
            or self.bottom < other.top
            or other.bottom < self.top
        )

    def inflated(self, margin: float) -> "AxisAlignedBounds":
        margin = _non_negative(margin, "Зазор")
        return AxisAlignedBounds(
            self.left - margin,
            self.top - margin,
            self.right + margin,
            self.bottom + margin,
        )


@dataclass(frozen=True, slots=True)
class OrientedRectangle:
    """Ориентированный прямоугольник в координатах схемы."""

    center_x: float
    center_y: float
    width: float
    height: float
    rotation_deg: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "center_x", _finite(self.center_x, "Центр X"))
        object.__setattr__(self, "center_y", _finite(self.center_y, "Центр Y"))
        object.__setattr__(self, "width", _positive(self.width, "Ширина фигуры"))
        object.__setattr__(self, "height", _positive(self.height, "Высота фигуры"))
        angle = _finite(self.rotation_deg, "Угол фигуры") % 360.0
        if math.isclose(angle, 360.0, abs_tol=_EPSILON):
            angle = 0.0
        object.__setattr__(self, "rotation_deg", angle)

    @property
    def center(self) -> Point2D:
        return Point2D(self.center_x, self.center_y)

    @property
    def corners(self) -> tuple[Point2D, Point2D, Point2D, Point2D]:
        half_width = self.width / 2.0
        half_height = self.height / 2.0
        angle = math.radians(self.rotation_deg)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        result: list[Point2D] = []
        for x, y in (
            (-half_width, -half_height),
            (half_width, -half_height),
            (half_width, half_height),
            (-half_width, half_height),
        ):
            result.append(Point2D(
                self.center_x + x * cosine - y * sine,
                self.center_y + x * sine + y * cosine,
            ))
        return tuple(result)  # type: ignore[return-value]

    @property
    def bounds(self) -> AxisAlignedBounds:
        points = self.corners
        return AxisAlignedBounds(
            min(item.x for item in points),
            min(item.y for item in points),
            max(item.x for item in points),
            max(item.y for item in points),
        )

    @staticmethod
    def _axes(points: tuple[Point2D, ...]) -> tuple[tuple[float, float], ...]:
        result: list[tuple[float, float]] = []
        for first, second in zip(points, (*points[1:], points[0])):
            dx, dy = second.x - first.x, second.y - first.y
            length = math.hypot(dx, dy)
            if length <= _EPSILON:
                continue
            result.append((-dy / length, dx / length))
        return tuple(result[:2])

    @staticmethod
    def _projection(
        points: tuple[Point2D, ...], axis: tuple[float, float]
    ) -> tuple[float, float]:
        values = tuple(item.x * axis[0] + item.y * axis[1] for item in points)
        return min(values), max(values)

    def intersects(self, other: "OrientedRectangle") -> bool:
        """Вернуть True только для положительного перекрытия площадей.

        Касание границ не считается наложением. Это позволяет линии или шине
        подходить ровно к порту без ложной жёсткой коллизии.
        """

        if not self.bounds.intersects(other.bounds):
            return False
        first_points = self.corners
        second_points = other.corners
        for axis in (*self._axes(first_points), *self._axes(second_points)):
            first_min, first_max = self._projection(first_points, axis)
            second_min, second_max = self._projection(second_points, axis)
            if min(first_max, second_max) - max(first_min, second_min) <= _EPSILON:
                return False
        return True

    def overlap_depth(self, other: "OrientedRectangle") -> float:
        if not self.intersects(other):
            return 0.0
        first_points = self.corners
        second_points = other.corners
        depths = []
        for axis in (*self._axes(first_points), *self._axes(second_points)):
            first_min, first_max = self._projection(first_points, axis)
            second_min, second_max = self._projection(second_points, axis)
            depths.append(min(first_max, second_max) - max(first_min, second_min))
        return min(depths, default=0.0)

    def contains(self, point: Point2D) -> bool:
        angle = math.radians(-self.rotation_deg)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        dx, dy = point.x - self.center_x, point.y - self.center_y
        local_x = dx * cosine - dy * sine
        local_y = dx * sine + dy * cosine
        return (
            abs(local_x) <= self.width / 2.0 + _EPSILON
            and abs(local_y) <= self.height / 2.0 + _EPSILON
        )

    def inflated(self, margin: float) -> "OrientedRectangle":
        margin = _non_negative(margin, "Зазор")
        return OrientedRectangle(
            self.center_x,
            self.center_y,
            self.width + margin * 2.0,
            self.height + margin * 2.0,
            self.rotation_deg,
        )

    def translated(self, dx: float, dy: float) -> "OrientedRectangle":
        return OrientedRectangle(
            self.center_x + _finite(dx, "Смещение X"),
            self.center_y + _finite(dy, "Смещение Y"),
            self.width,
            self.height,
            self.rotation_deg,
        )

    def rotated_about(
        self, center: Point2D, delta_deg: float
    ) -> "OrientedRectangle":
        delta_deg = _finite(delta_deg, "Изменение угла")
        angle = math.radians(delta_deg)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        dx, dy = self.center_x - center.x, self.center_y - center.y
        return OrientedRectangle(
            center.x + dx * cosine - dy * sine,
            center.y + dx * sine + dy * cosine,
            self.width,
            self.height,
            self.rotation_deg + delta_deg,
        )


class CollisionCategory(StrEnum):
    EQUIPMENT = "equipment"
    BUS = "bus"
    ELECTRICAL_NODE = "electrical_node"
    LINE = "line"
    CONTAINER = "container"


@dataclass(frozen=True, slots=True)
class PortConnectionZone:
    port_id: PortId | None
    role: str
    display_name: str
    shape: OrientedRectangle


@dataclass(frozen=True, slots=True)
class RepresentationGeometry:
    representation_id: GraphicalRepresentationId
    page_id: PageId
    equipment_id: EquipmentId | None
    display_name: str
    category: CollisionCategory
    anchor: Point2D
    rotation_deg: float
    visual_bounds: OrientedRectangle
    body_collision_shape: OrientedRectangle | None
    routing_obstacle_shape: OrientedRectangle | None
    selection_shape: OrientedRectangle
    port_connection_zones: tuple[PortConnectionZone, ...] = ()
    safe_gap: float = DEFAULT_SAFE_GAP

    def __post_init__(self) -> None:
        if not isinstance(self.representation_id, GraphicalRepresentationId):
            raise CollisionModelError(
                "representation_id должен быть GraphicalRepresentationId."
            )
        if not isinstance(self.page_id, PageId):
            raise CollisionModelError("page_id должен быть PageId.")
        if not isinstance(self.category, CollisionCategory):
            object.__setattr__(self, "category", CollisionCategory(self.category))
        object.__setattr__(self, "rotation_deg", _finite(
            self.rotation_deg, "Угол представления"
        ) % 360.0)
        object.__setattr__(self, "port_connection_zones", tuple(
            self.port_connection_zones
        ))
        object.__setattr__(self, "safe_gap", _non_negative(
            self.safe_gap, "Безопасный зазор"
        ))

    @property
    def orientation_quarter_turns(self) -> int | None:
        turns = round(self.rotation_deg / 90.0)
        if math.isclose(
            self.rotation_deg % 360.0,
            (turns * 90.0) % 360.0,
            abs_tol=1e-7,
        ):
            return turns % 4
        return None

    def translated(self, dx: float, dy: float) -> "RepresentationGeometry":
        dx = _finite(dx, "Смещение X")
        dy = _finite(dy, "Смещение Y")
        return RepresentationGeometry(
            self.representation_id,
            self.page_id,
            self.equipment_id,
            self.display_name,
            self.category,
            Point2D(self.anchor.x + dx, self.anchor.y + dy),
            self.rotation_deg,
            self.visual_bounds.translated(dx, dy),
            (
                self.body_collision_shape.translated(dx, dy)
                if self.body_collision_shape is not None
                else None
            ),
            (
                self.routing_obstacle_shape.translated(dx, dy)
                if self.routing_obstacle_shape is not None
                else None
            ),
            self.selection_shape.translated(dx, dy),
            tuple(
                PortConnectionZone(
                    item.port_id,
                    item.role,
                    item.display_name,
                    item.shape.translated(dx, dy),
                )
                for item in self.port_connection_zones
            ),
            self.safe_gap,
        )

    def with_rotation(self, rotation_deg: float) -> "RepresentationGeometry":
        rotation_deg = _finite(rotation_deg, "Новый угол") % 360.0
        delta = rotation_deg - self.rotation_deg
        return RepresentationGeometry(
            self.representation_id,
            self.page_id,
            self.equipment_id,
            self.display_name,
            self.category,
            self.anchor,
            rotation_deg,
            self.visual_bounds.rotated_about(self.anchor, delta),
            (
                self.body_collision_shape.rotated_about(self.anchor, delta)
                if self.body_collision_shape is not None
                else None
            ),
            (
                self.routing_obstacle_shape.rotated_about(self.anchor, delta)
                if self.routing_obstacle_shape is not None
                else None
            ),
            self.selection_shape.rotated_about(self.anchor, delta),
            tuple(
                PortConnectionZone(
                    item.port_id,
                    item.role,
                    item.display_name,
                    item.shape.rotated_about(self.anchor, delta),
                )
                for item in self.port_connection_zones
            ),
            self.safe_gap,
        )


class CollisionKind(StrEnum):
    BODY_OVERLAP = "body_overlap"
    SAFE_CLEARANCE = "safe_clearance"


@dataclass(frozen=True, slots=True)
class CollisionConflict:
    first_id: GraphicalRepresentationId
    second_id: GraphicalRepresentationId
    kind: CollisionKind
    message: str
    overlap_depth: float

    @property
    def pair_key(self) -> tuple[str, str]:
        return tuple(sorted((self.first_id.value, self.second_id.value)))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class CollisionCheckResult:
    allowed: bool
    conflicts: tuple[CollisionConflict, ...] = ()
    proposed_geometries: tuple[RepresentationGeometry, ...] = ()

    @property
    def message(self) -> str:
        return self.conflicts[0].message if self.conflicts else "Размещение допустимо."

    @property
    def conflicting_ids(self) -> tuple[GraphicalRepresentationId, ...]:
        values = {
            item
            for conflict in self.conflicts
            for item in (conflict.first_id, conflict.second_id)
        }
        return tuple(sorted(values, key=lambda item: item.value))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _graphics(representation: GraphicalRepresentation) -> Mapping[str, Any]:
    nested = _mapping(representation.extensions.get("stage3_graphics"))
    if not nested:
        nested = _mapping(representation.extensions.get("graphics"))
    return nested or representation.extensions


def _number(values: Mapping[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        return default
    return float(value)


def _category_and_behavior(
    representation: GraphicalRepresentation,
    model: ElectricalModel,
) -> tuple[CollisionCategory, str, str]:
    graphics = _graphics(representation)
    collision_values = _mapping(
        representation.extensions.get("collision_shapes")
    )
    explicit = collision_values.get("category", graphics.get("collision_category"))
    if explicit is not None:
        try:
            category = CollisionCategory(str(explicit))
        except ValueError as exc:
            raise CollisionModelError(
                f"Неизвестная категория коллизии «{explicit}»."
            ) from exc
    else:
        category = None

    if representation.equipment_id is not None:
        equipment = model.equipment.get(representation.equipment_id)
        definition = (
            model.equipment_types.get((equipment.type_id, equipment.type_version))
            if equipment is not None
            else None
        )
        behavior = str(getattr(definition, "behavior_key", "equipment"))
        name = (
            representation.label
            or (equipment.name if equipment is not None else "Оборудование")
        )
        if category is None:
            if behavior == "bus":
                category = CollisionCategory.BUS
            elif behavior in {"line", "line_section"}:
                category = CollisionCategory.LINE
            else:
                category = CollisionCategory.EQUIPMENT
        return category, behavior, name

    symbol = representation.symbol_key.casefold()
    name = representation.label or "Электрический узел"
    if category is None:
        if "busbar" in symbol or symbol == "busbar" or "шина" in name.casefold():
            category = CollisionCategory.BUS
        elif any(token in symbol for token in ("container", "subscheme", "substation")):
            category = CollisionCategory.CONTAINER
        else:
            category = CollisionCategory.ELECTRICAL_NODE
    return category, "bus" if category is CollisionCategory.BUS else "electrical_node", name


def _rotate_local(
    anchor: Point2D, x: float, y: float, rotation_deg: float
) -> Point2D:
    angle = math.radians(rotation_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return Point2D(
        anchor.x + x * cosine - y * sine,
        anchor.y + x * sine + y * cosine,
    )


def _port_zones(
    representation: GraphicalRepresentation,
    model: ElectricalModel,
    *,
    behavior: str,
    width: float,
    height: float,
    radius: float,
) -> tuple[PortConnectionZone, ...]:
    anchor = Point2D(representation.x, representation.y)
    if representation.equipment_id is None:
        if behavior == "bus":
            return (
                PortConnectionZone(
                    None,
                    "bus",
                    "Шина",
                    OrientedRectangle(
                        anchor.x,
                        anchor.y,
                        width + radius * 2.0,
                        height + radius * 2.0,
                        representation.rotation_deg,
                    ),
                ),
            )
        return (
            PortConnectionZone(
                None,
                "node",
                "Электрический узел",
                OrientedRectangle(anchor.x, anchor.y, radius * 2.0, radius * 2.0),
            ),
        )

    equipment = model.equipment.get(representation.equipment_id)
    if equipment is None:
        return ()
    definition = model.equipment_types.get((equipment.type_id, equipment.type_version))
    definitions = tuple(getattr(definition, "port_definitions", ()))
    half_width, half_height = width / 2.0, height / 2.0
    local: list[tuple[PortId, str, str, float, float]] = []
    ports = tuple(equipment.port_ids)
    if len(ports) == 1:
        row = definitions[0] if definitions else None
        role = str(getattr(row, "role", "terminal"))
        if behavior in {"source", "generator"}:
            x, y = 0.0, half_height
        elif behavior in {"load", "shunt"}:
            x, y = 0.0, -half_height
        else:
            x, y = half_width, 0.0
        local.append((
            ports[0], role, str(getattr(row, "display_name", "Электрический порт")), x, y
        ))
    elif len(ports) == 2:
        for index, port_id in enumerate(ports):
            row = definitions[index] if index < len(definitions) else None
            local.append((
                port_id,
                str(getattr(row, "role", index + 1)),
                str(getattr(row, "display_name", "Электрический порт")),
                -half_width if index == 0 else half_width,
                0.0,
            ))
    else:
        for index, port_id in enumerate(ports):
            row = definitions[index] if index < len(definitions) else None
            if index == 0:
                x, y = -half_width, 0.0
            else:
                x = half_width
                y = -half_height + height / max(2, len(ports)) * index
            local.append((
                port_id,
                str(getattr(row, "role", index + 1)),
                str(getattr(row, "display_name", "Электрический порт")),
                x,
                y,
            ))
    result = []
    for port_id, role, display_name, x, y in local:
        point = _rotate_local(anchor, x, y, representation.rotation_deg)
        result.append(PortConnectionZone(
            port_id,
            role,
            display_name,
            OrientedRectangle(
                point.x,
                point.y,
                radius * 2.0,
                radius * 2.0,
                representation.rotation_deg,
            ),
        ))
    return tuple(result)


def geometry_for_representation(
    representation: GraphicalRepresentation,
    model: ElectricalModel,
    *,
    safe_gap: float = DEFAULT_SAFE_GAP,
) -> RepresentationGeometry:
    """Построить все независимые графические фигуры представления."""

    if not isinstance(representation, GraphicalRepresentation):
        raise TypeError("Ожидается GraphicalRepresentation.")
    if not isinstance(model, ElectricalModel):
        raise TypeError("Ожидается ElectricalModel.")
    safe_gap = _non_negative(safe_gap, "Безопасный зазор")
    category, behavior, name = _category_and_behavior(representation, model)
    graphics = _graphics(representation)
    collision_values = _mapping(
        representation.extensions.get("collision_shapes")
    )
    orientation = str(graphics.get("orientation", "horizontal")).casefold()
    if behavior == "bus":
        default_width, default_height = (
            (22.0, 150.0) if orientation == "vertical" else (150.0, 22.0)
        )
    elif behavior in {"line", "line_section"}:
        default_width, default_height = 120.0, 24.0
    elif behavior == "electrical_node":
        default_width, default_height = 24.0, 24.0
    else:
        default_width, default_height = 74.0, 54.0
    width = _number(graphics, "width", default_width)
    height = _number(graphics, "height", default_height)
    body_width = _number(collision_values, "body_width", width)
    body_height = _number(collision_values, "body_height", height)
    selection_margin = _number(collision_values, "selection_margin", 9.0)
    routing_margin = _number(
        collision_values, "routing_margin", max(12.0, safe_gap)
    )
    port_zone_radius = _number(
        collision_values, "port_zone_radius", max(8.0, safe_gap / 2.0)
    )
    visual = OrientedRectangle(
        representation.x,
        representation.y,
        width,
        height,
        representation.rotation_deg,
    )
    has_hard_body = category in {
        CollisionCategory.EQUIPMENT,
        CollisionCategory.BUS,
    }
    body = (
        OrientedRectangle(
            representation.x,
            representation.y,
            body_width,
            body_height,
            representation.rotation_deg,
        )
        if has_hard_body
        else None
    )
    routing = body.inflated(routing_margin) if body is not None else None
    selection = visual.inflated(selection_margin)
    return RepresentationGeometry(
        representation.id,
        representation.page_id,
        representation.equipment_id,
        name,
        category,
        Point2D(representation.x, representation.y),
        representation.rotation_deg,
        visual,
        body,
        routing,
        selection,
        _port_zones(
            representation,
            model,
            behavior=behavior,
            width=width,
            height=height,
            radius=port_zone_radius,
        ),
        safe_gap,
    )


def geometry_for_equipment_preview(
    definition: EquipmentTypeDefinition,
    *,
    page_id: PageId | str,
    x: float,
    y: float,
    rotation_deg: float = 0.0,
    width: float | None = None,
    height: float | None = None,
    display_name: str = "",
    representation_id: GraphicalRepresentationId | str = "representation.preview.equipment",
    safe_gap: float = DEFAULT_SAFE_GAP,
) -> RepresentationGeometry:
    """Построить collision/preview-фигуры без создания Domain-объекта.

    Функция принимает неизменяемое определение типа из справочника. В модель
    не добавляются EquipmentInstance, Port или Connection; будущие порты
    представлены только их смысловыми ролями.
    """

    if not isinstance(definition, EquipmentTypeDefinition):
        raise TypeError("Ожидается EquipmentTypeDefinition.")
    if isinstance(page_id, str):
        page_id = PageId(page_id)
    if isinstance(representation_id, str):
        representation_id = GraphicalRepresentationId(representation_id)
    x = _finite(x, "Координата X предварительного объекта")
    y = _finite(y, "Координата Y предварительного объекта")
    rotation_deg = _finite(rotation_deg, "Угол предварительного объекта") % 360.0
    safe_gap = _non_negative(safe_gap, "Безопасный зазор")
    behavior = definition.behavior_key
    if behavior == "bus":
        default_width, default_height = 150.0, 22.0
        category = CollisionCategory.BUS
    elif behavior in {"line", "line_section"}:
        default_width, default_height = 120.0, 24.0
        category = CollisionCategory.LINE
    else:
        default_width, default_height = 74.0, 54.0
        category = CollisionCategory.EQUIPMENT
    width = (
        default_width
        if width is None
        else _positive(width, "Ширина предварительного объекта")
    )
    height = (
        default_height
        if height is None
        else _positive(height, "Высота предварительного объекта")
    )
    anchor = Point2D(x, y)
    visual = OrientedRectangle(x, y, width, height, rotation_deg)
    body = (
        visual
        if category in {CollisionCategory.EQUIPMENT, CollisionCategory.BUS}
        else None
    )
    routing = body.inflated(max(12.0, safe_gap)) if body is not None else None
    selection = visual.inflated(9.0)
    radius = max(8.0, safe_gap / 2.0)
    definitions = tuple(definition.port_definitions)
    half_width, half_height = width / 2.0, height / 2.0
    local: list[tuple[str, str, float, float]] = []
    if len(definitions) == 1:
        row = definitions[0]
        if behavior in {"source", "generator"}:
            local_x, local_y = 0.0, half_height
        elif behavior in {"load", "shunt"}:
            local_x, local_y = 0.0, -half_height
        else:
            local_x, local_y = half_width, 0.0
        local.append((row.role, row.display_name, local_x, local_y))
    elif len(definitions) == 2:
        for index, row in enumerate(definitions):
            local.append((
                row.role,
                row.display_name,
                -half_width if index == 0 else half_width,
                0.0,
            ))
    else:
        for index, row in enumerate(definitions):
            if index == 0:
                local_x, local_y = -half_width, 0.0
            else:
                local_x = half_width
                local_y = (
                    -half_height
                    + height / max(2, len(definitions)) * index
                )
            local.append((row.role, row.display_name, local_x, local_y))
    zones = []
    for role, port_name, local_x, local_y in local:
        point = _rotate_local(anchor, local_x, local_y, rotation_deg)
        zones.append(PortConnectionZone(
            None,
            role,
            port_name,
            OrientedRectangle(
                point.x,
                point.y,
                radius * 2.0,
                radius * 2.0,
                rotation_deg,
            ),
        ))
    return RepresentationGeometry(
        representation_id,
        page_id,
        None,
        display_name or definition.display_name,
        category,
        anchor,
        rotation_deg,
        visual,
        body,
        routing,
        selection,
        tuple(zones),
        safe_gap,
    )


class DiagramSpatialIndex:
    """Детерминированный uniform-grid индекс графических представлений."""

    def __init__(
        self,
        geometries: Iterable[RepresentationGeometry] = (),
        *,
        cell_size: float = DEFAULT_SPATIAL_CELL_SIZE,
    ):
        self.cell_size = _positive(cell_size, "Размер ячейки пространственного индекса")
        self._geometries: dict[GraphicalRepresentationId, RepresentationGeometry] = {}
        self._cells: dict[tuple[str, int, int], set[GraphicalRepresentationId]] = defaultdict(set)
        self._keys_by_id: dict[
            GraphicalRepresentationId, tuple[tuple[str, int, int], ...]
        ] = {}
        for geometry in geometries:
            self.insert(geometry)

    def __len__(self) -> int:
        return len(self._geometries)

    @staticmethod
    def _index_shape(geometry: RepresentationGeometry) -> OrientedRectangle:
        return (
            geometry.routing_obstacle_shape
            or geometry.selection_shape
            or geometry.visual_bounds
        )

    def _keys(
        self, page_id: PageId, bounds: AxisAlignedBounds
    ) -> tuple[tuple[str, int, int], ...]:
        left = math.floor(bounds.left / self.cell_size)
        right = math.floor(bounds.right / self.cell_size)
        top = math.floor(bounds.top / self.cell_size)
        bottom = math.floor(bounds.bottom / self.cell_size)
        return tuple(
            (page_id.value, x, y)
            for x in range(left, right + 1)
            for y in range(top, bottom + 1)
        )

    def insert(self, geometry: RepresentationGeometry) -> None:
        if not isinstance(geometry, RepresentationGeometry):
            raise TypeError("Индекс принимает RepresentationGeometry.")
        self.remove(geometry.representation_id)
        keys = self._keys(
            geometry.page_id, self._index_shape(geometry).bounds
        )
        self._geometries[geometry.representation_id] = geometry
        self._keys_by_id[geometry.representation_id] = keys
        for key in keys:
            self._cells[key].add(geometry.representation_id)

    def remove(self, representation_id: GraphicalRepresentationId) -> None:
        for key in self._keys_by_id.pop(representation_id, ()):
            values = self._cells.get(key)
            if values is None:
                continue
            values.discard(representation_id)
            if not values:
                self._cells.pop(key, None)
        self._geometries.pop(representation_id, None)

    def query(
        self,
        page_id: PageId,
        shape: OrientedRectangle | AxisAlignedBounds,
    ) -> tuple[RepresentationGeometry, ...]:
        bounds = shape.bounds if isinstance(shape, OrientedRectangle) else shape
        ids: set[GraphicalRepresentationId] = set()
        for key in self._keys(page_id, bounds):
            ids.update(self._cells.get(key, ()))
        return tuple(
            self._geometries[item]
            for item in sorted(ids, key=lambda value: value.value)
            if self._index_shape(self._geometries[item]).bounds.intersects(bounds)
        )

    @property
    def geometries(self) -> Mapping[GraphicalRepresentationId, RepresentationGeometry]:
        return MappingProxyType(self._geometries)


class DiagramCollisionService:
    """Неизменяемый снимок для preview и атомарной проверки команды."""

    def __init__(
        self,
        diagram: DiagramDocument,
        model: ElectricalModel,
        *,
        safe_gap: float = DEFAULT_SAFE_GAP,
        cell_size: float = DEFAULT_SPATIAL_CELL_SIZE,
    ):
        if not isinstance(diagram, DiagramDocument):
            raise TypeError("Ожидается DiagramDocument.")
        if not isinstance(model, ElectricalModel):
            raise TypeError("Ожидается ElectricalModel.")
        self._diagram = diagram
        self._model = model
        self.safe_gap = _non_negative(safe_gap, "Безопасный зазор")
        values = {
            representation.id: geometry_for_representation(
                representation, model, safe_gap=self.safe_gap
            )
            for representation in diagram.representations.values()
        }
        self._geometries = MappingProxyType(values)
        self.index = DiagramSpatialIndex(values.values(), cell_size=cell_size)

    @property
    def geometries(self) -> Mapping[GraphicalRepresentationId, RepresentationGeometry]:
        return self._geometries

    @staticmethod
    def _bus_port_contact_allowed(
        first: RepresentationGeometry,
        second: RepresentationGeometry,
    ) -> bool:
        categories = {first.category, second.category}
        if categories != {CollisionCategory.EQUIPMENT, CollisionCategory.BUS}:
            return False
        equipment = first if first.category is CollisionCategory.EQUIPMENT else second
        bus = second if equipment is first else first
        if bus.body_collision_shape is None:
            return False
        if bus.body_collision_shape.contains(equipment.anchor):
            return False
        return any(
            zone.shape.intersects(bus.body_collision_shape)
            for zone in equipment.port_connection_zones
        )

    @classmethod
    def _conflict(
        cls,
        first: RepresentationGeometry,
        second: RepresentationGeometry,
        *,
        include_clearance: bool,
    ) -> CollisionConflict | None:
        if first.page_id != second.page_id:
            return None
        if first.body_collision_shape is None or second.body_collision_shape is None:
            return None
        if cls._bus_port_contact_allowed(first, second):
            return None
        first_shape = first.body_collision_shape
        second_shape = second.body_collision_shape
        if first_shape.intersects(second_shape):
            kind = CollisionKind.BODY_OVERLAP
            depth = first_shape.overlap_depth(second_shape)
            message = (
                f"Нельзя разместить объект: пересечение «{first.display_name}» "
                f"с «{second.display_name}»."
            )
        elif include_clearance:
            margin = min(first.safe_gap, second.safe_gap) / 2.0
            first_clearance = first_shape.inflated(margin)
            second_clearance = second_shape.inflated(margin)
            if not first_clearance.intersects(second_clearance):
                return None
            kind = CollisionKind.SAFE_CLEARANCE
            depth = first_clearance.overlap_depth(second_clearance)
            message = (
                f"Нельзя разместить объект: недостаточный зазор между "
                f"«{first.display_name}» и «{second.display_name}»."
            )
        else:
            return None
        first_id, second_id = sorted(
            (first.representation_id, second.representation_id),
            key=lambda item: item.value,
        )
        return CollisionConflict(first_id, second_id, kind, message, depth)

    def _conflicts_for(
        self,
        candidates: Iterable[RepresentationGeometry],
        *,
        ignored_ids: frozenset[GraphicalRepresentationId] = frozenset(),
        include_clearance: bool = True,
        check_internal: bool = True,
    ) -> tuple[CollisionConflict, ...]:
        result: dict[tuple[str, str, str], CollisionConflict] = {}
        rows = tuple(candidates)
        candidate_ids = {item.representation_id for item in rows}
        for candidate in rows:
            query_shape = (
                candidate.routing_obstacle_shape
                or candidate.selection_shape
            )
            for existing in self.index.query(candidate.page_id, query_shape):
                if (
                    existing.representation_id in ignored_ids
                    or existing.representation_id in candidate_ids
                ):
                    continue
                conflict = self._conflict(
                    candidate, existing, include_clearance=include_clearance
                )
                if conflict is not None:
                    result[(*conflict.pair_key, conflict.kind.value)] = conflict
        # Несколько новых объектов проверяются также между собой. При
        # групповом перемещении внутреннее расположение, напротив, должно
        # сохраниться и проверяются только объекты вне группы.
        if check_internal:
            for index, first in enumerate(rows):
                for second in rows[index + 1:]:
                    conflict = self._conflict(
                        first, second, include_clearance=include_clearance
                    )
                    if conflict is not None:
                        result[(*conflict.pair_key, conflict.kind.value)] = conflict
        return tuple(
            result[key]
            for key in sorted(result)
        )

    def check_placement(
        self,
        candidate: RepresentationGeometry,
        *,
        ignored_representation_ids: Iterable[GraphicalRepresentationId] = (),
    ) -> CollisionCheckResult:
        conflicts = self._conflicts_for(
            (candidate,),
            ignored_ids=frozenset(ignored_representation_ids),
        )
        return CollisionCheckResult(not conflicts, conflicts, (candidate,))

    def check_placements(
        self,
        candidates: Iterable[RepresentationGeometry],
        *,
        ignored_representation_ids: Iterable[GraphicalRepresentationId] = (),
    ) -> CollisionCheckResult:
        """Атомарно проверить группу новых представлений.

        Проверяются как конфликты с текущим документом, так и
        конфликты внутри вставляемой группы. Метод не меняет модель
        и подходит для preflight внутри транзакции paste/duplicate.
        """

        rows = tuple(candidates)
        conflicts = self._conflicts_for(
            rows,
            ignored_ids=frozenset(ignored_representation_ids),
            check_internal=True,
        )
        return CollisionCheckResult(not conflicts, conflicts, rows)

    def check_resize(
        self,
        representation_id: GraphicalRepresentationId,
        width: float,
        height: float,
    ) -> CollisionCheckResult:
        """Проверить изменение габарита как одну графическую транзакцию."""

        width = _positive(width, "Ширина представления")
        height = _positive(height, "Высота представления")
        try:
            representation = self._diagram.representations[representation_id]
            original = self._geometries[representation_id]
        except KeyError as exc:
            raise CollisionModelError(
                "Графическое представление для изменения размера не найдено."
            ) from exc

        # Локальный импорт сохраняет collision.py независимым от GUI
        # и не создаёт цикл модулей при импорте.
        from .state import RepresentationGraphics, representation_with_graphics

        graphics = RepresentationGraphics.from_representation(representation)
        proposed_representation = representation_with_graphics(
            representation,
            replace(
                graphics,
                width=width,
                height=height,
            ),
        )
        proposed = geometry_for_representation(
            proposed_representation,
            self._model,
            safe_gap=self.safe_gap,
        )
        return self._check_transform((original,), (proposed,))

    def _check_transform(
        self,
        originals: tuple[RepresentationGeometry, ...],
        proposed: tuple[RepresentationGeometry, ...],
    ) -> CollisionCheckResult:
        moving_ids = frozenset(item.representation_id for item in originals)
        before = {
            (*item.pair_key, item.kind.value): item
            for item in self._conflicts_for(
                originals,
                ignored_ids=moving_ids,
                check_internal=False,
            )
        }
        after = self._conflicts_for(
            proposed,
            ignored_ids=moving_ids,
            check_internal=False,
        )
        blocking: list[CollisionConflict] = []
        for conflict in after:
            previous = before.get((*conflict.pair_key, conflict.kind.value))
            # Старое наложение разрешено исправлять. Сохранение или увеличение
            # глубины конфликта новой операцией блокируется.
            if (
                previous is not None
                and conflict.overlap_depth < previous.overlap_depth - _EPSILON
            ):
                continue
            blocking.append(conflict)
        return CollisionCheckResult(
            not blocking,
            tuple(blocking),
            proposed,
        )

    def check_move(
        self,
        representation_ids: Iterable[GraphicalRepresentationId],
        dx: float,
        dy: float,
    ) -> CollisionCheckResult:
        ids = tuple(dict.fromkeys(representation_ids))
        originals = tuple(self._geometries[item] for item in ids)
        proposed = tuple(item.translated(dx, dy) for item in originals)
        return self._check_transform(originals, proposed)

    def check_rotation(
        self,
        representation_id: GraphicalRepresentationId,
        rotation_deg: float,
    ) -> CollisionCheckResult:
        original = self._geometries[representation_id]
        proposed = original.with_rotation(rotation_deg)
        return self._check_transform((original,), (proposed,))

    def legacy_overlaps(self) -> tuple[CollisionConflict, ...]:
        """Найти старые перекрытия тел без изменения координат проекта."""

        result: dict[tuple[str, str], CollisionConflict] = {}
        for geometry in self._geometries.values():
            if geometry.body_collision_shape is None:
                continue
            for candidate in self.index.query(
                geometry.page_id, geometry.body_collision_shape
            ):
                if candidate.representation_id == geometry.representation_id:
                    continue
                conflict = self._conflict(
                    geometry, candidate, include_clearance=False
                )
                if conflict is not None:
                    result[conflict.pair_key] = conflict
        return tuple(result[key] for key in sorted(result))


__all__ = [
    "AxisAlignedBounds",
    "CollisionCategory",
    "CollisionCheckResult",
    "CollisionConflict",
    "CollisionKind",
    "CollisionModelError",
    "DEFAULT_SAFE_GAP",
    "DEFAULT_SPATIAL_CELL_SIZE",
    "DiagramCollisionService",
    "DiagramSpatialIndex",
    "OrientedRectangle",
    "Point2D",
    "PortConnectionZone",
    "RepresentationGeometry",
    "geometry_for_representation",
    "geometry_for_equipment_preview",
]
