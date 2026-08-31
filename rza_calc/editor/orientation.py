# -*- coding: utf-8 -*-
"""Qt-независимая геометрия дискретной ориентации оборудования.

Модуль работает только с графическим представлением портов. Он не изменяет
электрические узлы, соединения, роли или идентификаторы портов.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Iterable

from rza_calc.domain.electrical import (
    EquipmentInstance,
    EquipmentTypeDefinition,
    PortId,
)

from .orthogonal_routing import RouteDirection
from .symbols import symbol_for


class OrientationMode(StrEnum):
    """Способ выбора угла одного графического представления."""

    AUTO = "auto"
    MANUAL = "manual"


class QuarterTurn(IntEnum):
    """Допустимые углы символа в экранной системе координат Qt."""

    DEG_0 = 0
    DEG_90 = 90
    DEG_180 = 180
    DEG_270 = 270

    def shifted(self, steps_clockwise: int) -> "QuarterTurn":
        values = tuple(type(self))
        index = values.index(self)
        return values[(index + int(steps_clockwise)) % len(values)]


# This is an interaction policy, not a change to saved diagram geometry.
# The four QuarterTurn values remain valid for older documents and routing.
EDITOR_ROTATIONS: tuple[int, int] = (90, 180)


@dataclass(frozen=True, slots=True)
class PortAnchorGeometry:
    """Один смысловой порт и его графическая точка/наружное направление."""

    port_id: PortId
    role: str
    display_name: str
    x: float
    y: float
    direction: RouteDirection


@dataclass(frozen=True, slots=True)
class DirectedSegment:
    """Ближайший направленный ортогональный сегмент графической трассы."""

    index: int
    x: float
    y: float
    direction: RouteDirection
    distance: float

    @property
    def quarter_turn(self) -> QuarterTurn:
        return quarter_turn_for_direction(self.direction)


def normalize_quarter_turn(value: int | float | QuarterTurn) -> QuarterTurn:
    """Нормализовать кратный 90° угол или отклонить произвольный угол."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Угол поворота должен быть числом, кратным 90°.")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("Угол поворота должен быть конечным числом.")
    normalized = numeric % 360.0
    nearest = round(normalized / 90.0) * 90 % 360
    if not math.isclose(normalized, float(nearest), abs_tol=1e-9):
        raise ValueError("Допустимы только углы 0°, 90°, 180° и 270°.")
    return QuarterTurn(nearest)


def editor_rotation(value: int | float | QuarterTurn) -> int:
    """Выбрать одну из двух ориентаций для нового действия редактора.

    Старые горизонтальные углы 0°/180° дают 180°, вертикальные 90°/270°
    дают 90°. Вызывать при действии пользователя, но не при загрузке
    документа: противоположные направления смысловых портов не тождественны.
    """

    turn = normalize_quarter_turn(value)
    return 90 if int(turn) % 180 else 180


def next_editor_rotation(value: int | float | QuarterTurn) -> int:
    """Переключить горизонтальное и вертикальное положения без третьего."""

    return 180 if editor_rotation(value) == 90 else 90


def nearest_editor_rotation(
    value: int | float,
    *,
    current: int | float | None = None,
) -> int:
    """Привязать угол указателя к ближайшей вертикальной/горизонтальной оси.

    Расстояние измеряется до оси, а не до направленного луча, поэтому полный
    оборот мышью не создаёт положений 0°/270°. На точной диагонали сохраняется
    текущее положение; без него при равенстве выбирается вертикальное.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Угол поворота должен быть числом.")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("Угол поворота должен быть конечным числом.")
    axis_angle = numeric % 180.0
    vertical_distance = abs(axis_angle - 90.0)
    horizontal_distance = min(axis_angle, 180.0 - axis_angle)
    if math.isclose(vertical_distance, horizontal_distance, abs_tol=1e-9):
        return nearest_editor_rotation(current) if current is not None else 90
    return 90 if vertical_distance < horizontal_distance else 180


def normalize_orientation_mode(value: OrientationMode | str) -> OrientationMode:
    if isinstance(value, OrientationMode):
        return value
    try:
        return OrientationMode(str(value).lower())
    except ValueError as exc:
        raise ValueError(
            "Режим ориентации должен быть автоматическим или ручным."
        ) from exc


def quarter_turn_for_direction(direction: RouteDirection | str) -> QuarterTurn:
    direction = (
        direction
        if isinstance(direction, RouteDirection)
        else RouteDirection(str(direction))
    )
    return {
        RouteDirection.RIGHT: QuarterTurn.DEG_0,
        RouteDirection.DOWN: QuarterTurn.DEG_90,
        RouteDirection.LEFT: QuarterTurn.DEG_180,
        RouteDirection.UP: QuarterTurn.DEG_270,
    }[direction]


def rotate_direction(
    direction: RouteDirection | str,
    rotation: int | float | QuarterTurn,
) -> RouteDirection:
    direction = (
        direction
        if isinstance(direction, RouteDirection)
        else RouteDirection(str(direction))
    )
    steps = int(normalize_quarter_turn(rotation)) // 90
    clockwise = (
        RouteDirection.RIGHT,
        RouteDirection.DOWN,
        RouteDirection.LEFT,
        RouteDirection.UP,
    )
    return clockwise[(clockwise.index(direction) + steps) % 4]


def quarter_turn_between_directions(
    source: RouteDirection | str,
    target: RouteDirection | str,
) -> QuarterTurn:
    source = source if isinstance(source, RouteDirection) else RouteDirection(source)
    target = target if isinstance(target, RouteDirection) else RouteDirection(target)
    clockwise = (
        RouteDirection.RIGHT,
        RouteDirection.DOWN,
        RouteDirection.LEFT,
        RouteDirection.UP,
    )
    steps = (clockwise.index(target) - clockwise.index(source)) % 4
    return QuarterTurn(steps * 90)


def _symbol_geometry(
    definition: EquipmentTypeDefinition,
    *,
    width: float,
    height: float,
):
    """Геометрия условного обозначения типа оборудования, если она известна.

    Ключ символа берётся из расширений типа, поэтому пользовательский тип
    может указать своё обозначение, не меняя код. Если ключ неизвестен
    библиотеке, возвращается ``None`` и работает прежняя раскладка по
    габаритному прямоугольнику.
    """
    try:
        symbol_key = str(definition.extensions.get("diagram_symbol_key", "") or "")
    except AttributeError:
        symbol_key = ""
    try:
        geometry = symbol_for(
            symbol_key,
            definition.behavior_key,
            width=width,
            height=height,
        )
    except (ValueError, KeyError):
        return None
    return None if geometry.key == "generic" else geometry


def base_port_direction(
    definition: EquipmentTypeDefinition,
    role: str,
) -> RouteDirection:
    """Вернуть наружное направление смыслового порта при угле 0°.

    Правило совпадает с :func:`base_port_layout`, но не требует уже созданного
    экземпляра оборудования. Поэтому его можно безопасно применять к ghost-
    preview и к атомарной команде создания оборудования.
    """

    rows = tuple(definition.port_definitions)
    index = next(
        (item_index for item_index, item in enumerate(rows) if item.role == role),
        None,
    )
    if index is None:
        raise KeyError(
            f"У типа «{definition.display_name}» нет порта с указанным назначением."
        )
    symbol = _symbol_geometry(definition, width=64.0, height=64.0)
    if symbol is not None:
        terminal = symbol.terminal(role)
        if terminal is not None:
            return terminal.direction
    if len(rows) == 1:
        if definition.behavior_key in {"source", "generator"}:
            return RouteDirection.DOWN
        if definition.behavior_key in {"load", "shunt"}:
            return RouteDirection.UP
        return RouteDirection.RIGHT
    if len(rows) == 2:
        return RouteDirection.LEFT if index == 0 else RouteDirection.RIGHT
    return RouteDirection.LEFT if index == 0 else RouteDirection.RIGHT


def direction_toward_point(
    source_x: float,
    source_y: float,
    target_x: float,
    target_y: float,
) -> RouteDirection:
    """Выбрать ортогональное направление от объекта к точке подключения."""

    values = tuple(float(value) for value in (
        source_x,
        source_y,
        target_x,
        target_y,
    ))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Координаты ориентации должны быть конечными числами.")
    dx = values[2] - values[0]
    dy = values[3] - values[1]
    if math.isclose(dx, 0.0, abs_tol=1e-9) and math.isclose(
        dy, 0.0, abs_tol=1e-9
    ):
        raise ValueError(
            "Нельзя определить автоматическую ориентацию: "
            "объект совпадает с точкой подключения."
        )
    if abs(dx) > abs(dy):
        return RouteDirection.RIGHT if dx > 0.0 else RouteDirection.LEFT
    return RouteDirection.DOWN if dy > 0.0 else RouteDirection.UP


def quarter_turn_for_port_toward_point(
    definition: EquipmentTypeDefinition,
    role: str,
    *,
    equipment_x: float,
    equipment_y: float,
    target_x: float,
    target_y: float,
) -> QuarterTurn:
    """Повернуть выбранный смысловой порт к точке будущей ветви."""

    return quarter_turn_between_directions(
        base_port_direction(definition, role),
        direction_toward_point(equipment_x, equipment_y, target_x, target_y),
    )


def _rotated_point(
    x: float,
    y: float,
    rotation: int | float | QuarterTurn,
) -> tuple[float, float]:
    turn = normalize_quarter_turn(rotation)
    if turn is QuarterTurn.DEG_0:
        return x, y
    if turn is QuarterTurn.DEG_90:
        return -y, x
    if turn is QuarterTurn.DEG_180:
        return -x, -y
    return y, -x


def bus_anchor_geometry(
    *,
    width: float,
    height: float,
    rotation: int | float | QuarterTurn,
    center_x: float,
    center_y: float,
    fraction: float,
) -> tuple[float, float, RouteDirection]:
    """Вернуть точку и наружное направление долевого присоединения к шине.

    Ось определяется в локальных координатах символа, а затем вместе с
    направлением поворачивается вокруг центра. Так preview и сохранённая
    трасса используют одну геометрию, в том числе для старых углов 0°/270°.
    Никаких узлов, соединений или смысловых портов функция не создаёт.
    """

    values = (width, height, center_x, center_y, fraction)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("Параметры присоединения к шине должны быть числами.")
    width, height, center_x, center_y, fraction = map(float, values)
    if not all(math.isfinite(value) for value in (width, height, center_x, center_y, fraction)):
        raise ValueError("Параметры присоединения к шине должны быть конечными числами.")
    if width <= 0.0 or height <= 0.0:
        raise ValueError("Габариты шины должны быть положительными.")

    turn = normalize_quarter_turn(rotation)
    bounded_fraction = min(1.0, max(0.0, fraction))
    if height > width:
        x, y = 0.0, (bounded_fraction - 0.5) * height
        direction = RouteDirection.LEFT
    else:
        x, y = (bounded_fraction - 0.5) * width, 0.0
        direction = RouteDirection.UP
    x, y = _rotated_point(x, y, turn)
    return center_x + x, center_y + y, rotate_direction(direction, turn)


def bus_anchor_toward_point(
    *,
    width: float,
    height: float,
    rotation: int | float | QuarterTurn,
    center_x: float,
    center_y: float,
    target_x: float,
    target_y: float,
) -> tuple[float, float, float, RouteDirection]:
    """Project an adjacent port onto the bus, including its real end points.

    This is the automatic-attachment policy, not a replacement for an explicit
    fractional attachment. A longitudinal approach beyond an end leaves along
    the bus axis, so its wire never doubles back through the bus. Equipment
    ports and electrical node identities are not moved by this calculation.
    """
    # Reuse the established validation and local-axis convention.
    bus_anchor_geometry(width=width, height=height, rotation=rotation,
                        center_x=center_x, center_y=center_y, fraction=0.5)
    for value in (target_x, target_y):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Координаты соседнего вывода должны быть конечными числами.")
    turn = normalize_quarter_turn(rotation)
    local_x, local_y = _rotated_point(
        float(target_x) - center_x, float(target_y) - center_y,
        (360 - int(turn)) % 360,
    )
    vertical = height > width
    along, across = (local_y, local_x) if vertical else (local_x, local_y)
    length = float(height if vertical else width)
    fraction = min(1.0, max(0.0, along / length + 0.5))
    x, y, _ = bus_anchor_geometry(
        width=width, height=height, rotation=turn,
        center_x=center_x, center_y=center_y, fraction=fraction,
    )
    beyond = along - (fraction - 0.5) * length
    if abs(beyond) > 1e-9 and abs(beyond) >= abs(across):
        direction = (RouteDirection.DOWN if beyond > 0 else RouteDirection.UP) if vertical else (
            RouteDirection.RIGHT if beyond > 0 else RouteDirection.LEFT)
    else:
        direction = (RouteDirection.RIGHT if across > 0 else RouteDirection.LEFT) if vertical else (
            RouteDirection.DOWN if across > 0 else RouteDirection.UP)
    return fraction, x, y, rotate_direction(direction, turn)


def base_port_layout(
    equipment: EquipmentInstance,
    definition: EquipmentTypeDefinition,
    *,
    width: float,
    height: float,
) -> tuple[PortAnchorGeometry, ...]:
    """Построить карту портов при 0°, не связывая роль с экранной стороной."""

    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("Габариты символа должны быть конечными и положительными.")
    rows = tuple(definition.port_definitions)
    ports = tuple(equipment.port_ids)
    half_w, half_h = float(width) / 2.0, float(height) / 2.0

    def metadata(index: int) -> tuple[str, str]:
        row = rows[index] if index < len(rows) else None
        return (
            getattr(row, "role", str(index + 1)),
            getattr(row, "display_name", "Электрический порт"),
        )

    # Основной путь: точка вывода берётся из библиотеки условных обозначений,
    # то есть находится на конце нарисованного проводника аппарата. Так линия
    # приходит именно туда, куда ведёт рисунок, а не в угол габарита.
    symbol = _symbol_geometry(definition, width=float(width), height=float(height))
    if symbol is not None:
        mapped: list[PortAnchorGeometry] = []
        for index, port_id in enumerate(ports):
            role, display_name = metadata(index)
            terminal = symbol.terminal(role)
            if terminal is None:
                mapped = []
                break
            mapped.append(
                PortAnchorGeometry(
                    port_id, role, display_name,
                    terminal.x, terminal.y, terminal.direction,
                )
            )
        if mapped:
            return tuple(mapped)

    if len(ports) == 1:
        role, display_name = metadata(0)
        if definition.behavior_key in {"source", "generator"}:
            x, y, direction = 0.0, half_h, RouteDirection.DOWN
        elif definition.behavior_key in {"load", "shunt"}:
            x, y, direction = 0.0, -half_h, RouteDirection.UP
        else:
            x, y, direction = half_w, 0.0, RouteDirection.RIGHT
        return (PortAnchorGeometry(ports[0], role, display_name, x, y, direction),)

    if len(ports) == 2:
        result: list[PortAnchorGeometry] = []
        for index, port_id in enumerate(ports):
            role, display_name = metadata(index)
            result.append(
                PortAnchorGeometry(
                    port_id,
                    role,
                    display_name,
                    -half_w if index == 0 else half_w,
                    0.0,
                    RouteDirection.LEFT if index == 0 else RouteDirection.RIGHT,
                )
            )
        return tuple(result)

    result = []
    for index, port_id in enumerate(ports):
        role, display_name = metadata(index)
        if index == 0:
            x, y, direction = -half_w, 0.0, RouteDirection.LEFT
        else:
            step = float(height) / max(2, len(ports))
            x, y, direction = half_w, -half_h + step * index, RouteDirection.RIGHT
        result.append(
            PortAnchorGeometry(port_id, role, display_name, x, y, direction)
        )
    return tuple(result)


def rotated_port_layout(
    equipment: EquipmentInstance,
    definition: EquipmentTypeDefinition,
    *,
    width: float,
    height: float,
    rotation: int | float | QuarterTurn,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> tuple[PortAnchorGeometry, ...]:
    """Вернуть экранные координаты портов после дискретного поворота."""

    turn = normalize_quarter_turn(rotation)
    result: list[PortAnchorGeometry] = []
    for anchor in base_port_layout(
        equipment, definition, width=width, height=height
    ):
        x, y = _rotated_point(anchor.x, anchor.y, turn)
        result.append(
            PortAnchorGeometry(
                anchor.port_id,
                anchor.role,
                anchor.display_name,
                float(center_x) + x,
                float(center_y) + y,
                rotate_direction(anchor.direction, turn),
            )
        )
    return tuple(result)


def port_anchor_by_id(
    equipment: EquipmentInstance,
    definition: EquipmentTypeDefinition,
    port_id: PortId,
    *,
    width: float,
    height: float,
    rotation: int | float | QuarterTurn,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> PortAnchorGeometry:
    for anchor in rotated_port_layout(
        equipment,
        definition,
        width=width,
        height=height,
        rotation=rotation,
        center_x=center_x,
        center_y=center_y,
    ):
        if anchor.port_id == port_id:
            return anchor
    raise KeyError(f"Порт '{port_id}' отсутствует в графической карте оборудования.")


def closest_directed_segment(
    points: Iterable[tuple[float, float]],
    x: float,
    y: float,
) -> DirectedSegment | None:
    """Найти ближайший сегмент и сохранить направление порядка трассы."""

    rows = tuple((float(px), float(py)) for px, py in points)
    best: tuple[float, int, DirectedSegment] | None = None
    for index, (first, second) in enumerate(zip(rows, rows[1:])):
        x1, y1 = first
        x2, y2 = second
        if math.isclose(x1, x2, abs_tol=1e-9) and not math.isclose(
            y1, y2, abs_tol=1e-9
        ):
            projected_y = min(max(float(y), min(y1, y2)), max(y1, y2))
            distance = math.hypot(float(x) - x1, float(y) - projected_y)
            direction = RouteDirection.DOWN if y2 > y1 else RouteDirection.UP
            projected_x = x1
        elif math.isclose(y1, y2, abs_tol=1e-9) and not math.isclose(
            x1, x2, abs_tol=1e-9
        ):
            projected_x = min(max(float(x), min(x1, x2)), max(x1, x2))
            distance = math.hypot(float(x) - projected_x, float(y) - y1)
            direction = RouteDirection.RIGHT if x2 > x1 else RouteDirection.LEFT
            projected_y = y1
        else:
            continue
        segment = DirectedSegment(
            index, projected_x, projected_y, direction, distance
        )
        candidate = (distance, index, segment)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best is not None else None


__all__ = [
    "DirectedSegment",
    "EDITOR_ROTATIONS",
    "OrientationMode",
    "PortAnchorGeometry",
    "QuarterTurn",
    "base_port_direction",
    "base_port_layout",
    "bus_anchor_geometry",
    "bus_anchor_toward_point",
    "closest_directed_segment",
    "direction_toward_point",
    "editor_rotation",
    "nearest_editor_rotation",
    "next_editor_rotation",
    "normalize_orientation_mode",
    "normalize_quarter_turn",
    "port_anchor_by_id",
    "quarter_turn_between_directions",
    "quarter_turn_for_direction",
    "quarter_turn_for_port_toward_point",
    "rotate_direction",
    "rotated_port_layout",
]
