# -*- coding: utf-8 -*-
"""Qt-независимая локальная ортогональная маршрутизация схемы.

Модуль работает только с графическими координатами. Он не импортирует
``ElectricalModel`` и не может изменить электрическую связность. Полный
глобальный трассировщик здесь намеренно не строится: выбирается лучший из
небольшого набора локальных манхэттенских маршрутов, а пользовательские точки
остаются обязательными промежуточными целями.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import StrEnum
from heapq import heappop, heappush
from typing import Iterable, Sequence


class RouteDirection(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


class RouteVertexSource(StrEnum):
    AUTOMATIC = "automatic"
    USER = "user"


class RoutingError(ValueError):
    """The bounded graphical router cannot find a safe orthogonal route."""


@dataclass(frozen=True, slots=True)
class RouteVertex:
    x: float
    y: float
    source: RouteVertexSource = RouteVertexSource.AUTOMATIC
    pinned: bool = False

    def __post_init__(self) -> None:
        for name, value in (("x", self.x), ("y", self.y)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"Координата {name} должна быть числом.")
            if not math.isfinite(float(value)):
                raise ValueError(f"Координата {name} должна быть конечной.")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.source, RouteVertexSource):
            object.__setattr__(self, "source", RouteVertexSource(self.source))
        if not isinstance(self.pinned, bool):
            raise TypeError("Признак фиксации точки должен быть логическим.")

    @property
    def point(self) -> tuple[float, float]:
        return self.x, self.y


@dataclass(frozen=True, slots=True)
class RoutingObstacle:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ):
            raise ValueError("Границы препятствия должны быть конечными числами.")
        left, right = sorted((float(self.left), float(self.right)))
        top, bottom = sorted((float(self.top), float(self.bottom)))
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "top", top)
        object.__setattr__(self, "bottom", bottom)

    def inflated(self, margin: float) -> "RoutingObstacle":
        return RoutingObstacle(
            self.left - margin,
            self.top - margin,
            self.right + margin,
            self.bottom + margin,
        )

    def contains(self, point: tuple[float, float]) -> bool:
        x, y = point
        return self.left < x < self.right and self.top < y < self.bottom


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    start: RouteVertex
    end: RouteVertex
    start_direction: RouteDirection | None = None
    end_direction: RouteDirection | None = None
    manual_vertices: tuple[RouteVertex, ...] = ()
    obstacles: tuple[RoutingObstacle, ...] = ()
    clearance: float = 12.0
    port_stub: float = 18.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "manual_vertices", tuple(self.manual_vertices))
        object.__setattr__(self, "obstacles", tuple(self.obstacles))
        for name in ("clearance", "port_stub"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValueError(f"{name} должен быть конечным неотрицательным числом.")
            object.__setattr__(self, name, float(value))


def _same(first: RouteVertex, second: RouteVertex) -> bool:
    return math.isclose(first.x, second.x, abs_tol=1e-9) and math.isclose(
        first.y, second.y, abs_tol=1e-9
    )


def _collinear(first: RouteVertex, middle: RouteVertex, last: RouteVertex) -> bool:
    return (
        math.isclose(first.x, middle.x, abs_tol=1e-9)
        and math.isclose(middle.x, last.x, abs_tol=1e-9)
    ) or (
        math.isclose(first.y, middle.y, abs_tol=1e-9)
        and math.isclose(middle.y, last.y, abs_tol=1e-9)
    )


def _merge_duplicate(first: RouteVertex, second: RouteVertex) -> RouteVertex:
    source = (
        RouteVertexSource.USER
        if RouteVertexSource.USER in {first.source, second.source}
        else RouteVertexSource.AUTOMATIC
    )
    return RouteVertex(first.x, first.y, source, first.pinned or second.pinned)


def normalize_route(vertices: Iterable[RouteVertex]) -> tuple[RouteVertex, ...]:
    """Убрать нулевые и лишние автоматические коллинеарные сегменты.

    Закреплённая точка сохраняется даже на прямом участке. Само происхождение
    USER без pinned не делает старый промежуточный изгиб обязательным.
    """

    compact: list[RouteVertex] = []
    for vertex in vertices:
        if not isinstance(vertex, RouteVertex):
            raise TypeError("Маршрут должен содержать RouteVertex.")
        if compact and _same(compact[-1], vertex):
            compact[-1] = _merge_duplicate(compact[-1], vertex)
        else:
            compact.append(vertex)

    orthogonal: list[RouteVertex] = []
    for vertex in compact:
        if orthogonal:
            previous = orthogonal[-1]
            diagonal = not math.isclose(previous.x, vertex.x, abs_tol=1e-9) and not math.isclose(
                previous.y, vertex.y, abs_tol=1e-9
            )
            if diagonal:
                orthogonal.append(
                    RouteVertex(vertex.x, previous.y, RouteVertexSource.AUTOMATIC)
                )
        orthogonal.append(vertex)

    changed = True
    while changed and len(orthogonal) >= 3:
        changed = False
        result = [orthogonal[0]]
        for index in range(1, len(orthogonal) - 1):
            current = orthogonal[index]
            if (
                not current.pinned
                and _collinear(result[-1], current, orthogonal[index + 1])
            ):
                changed = True
                continue
            result.append(current)
        result.append(orthogonal[-1])
        orthogonal = result
    return tuple(orthogonal)


def _direction_delta(
    direction: RouteDirection | None, distance: float
) -> tuple[float, float]:
    if direction is RouteDirection.LEFT:
        return -distance, 0.0
    if direction is RouteDirection.RIGHT:
        return distance, 0.0
    if direction is RouteDirection.UP:
        return 0.0, -distance
    if direction is RouteDirection.DOWN:
        return 0.0, distance
    return 0.0, 0.0


def _segment_hits_obstacle(
    first: tuple[float, float],
    second: tuple[float, float],
    obstacle: RoutingObstacle,
) -> bool:
    x1, y1 = first
    x2, y2 = second
    if math.isclose(x1, x2, abs_tol=1e-9):
        low, high = sorted((y1, y2))
        return obstacle.left < x1 < obstacle.right and max(low, obstacle.top) < min(
            high, obstacle.bottom
        )
    if math.isclose(y1, y2, abs_tol=1e-9):
        low, high = sorted((x1, x2))
        return obstacle.top < y1 < obstacle.bottom and max(low, obstacle.left) < min(
            high, obstacle.right
        )
    return True


def _route_score(
    points: Sequence[tuple[float, float]], obstacles: Sequence[RoutingObstacle]
) -> tuple[int, float, int]:
    hits = 0
    length = 0.0
    for first, second in zip(points, points[1:]):
        length += abs(first[0] - second[0]) + abs(first[1] - second[1])
        hits += sum(
            _segment_hits_obstacle(first, second, obstacle) for obstacle in obstacles
        )
    bends = max(0, len(points) - 2)
    return hits, length + bends * 24.0, bends


def _coordinate_candidates(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: Sequence[RoutingObstacle],
) -> tuple[list[float], list[float]]:
    xs = {start[0], end[0], (start[0] + end[0]) / 2.0,
          start[0] - 24.0, start[0] + 24.0, end[0] - 24.0, end[0] + 24.0}
    ys = {start[1], end[1], (start[1] + end[1]) / 2.0,
          start[1] - 24.0, start[1] + 24.0, end[1] - 24.0, end[1] + 24.0}
    for obstacle in obstacles:
        xs.update((obstacle.left, obstacle.right))
        ys.update((obstacle.top, obstacle.bottom))
    # Bound the fallback graph even in a large diagram. All obstacles still
    # participate in collision checks; only optional search coordinates are
    # limited. Failure is explicit rather than a wire through an apparatus.
    def bounded(values: set[float], first: float, last: float) -> list[float]:
        if len(values) <= 64:
            return sorted(values)
        mandatory = {first, last, min(values), max(values)}
        low, high = sorted((first, last))
        ordered = sorted(values - mandatory,
                         key=lambda value: (max(low - value, 0.0, value - high),
                                            min(abs(value - first), abs(value - last)), value))
        return sorted(mandatory | set(ordered[:64 - len(mandatory)]))
    return bounded(xs, start[0], end[0]), bounded(ys, start[1], end[1])


def _leg_directions_valid(
    points: Sequence[tuple[float, float]],
    start_direction: RouteDirection | None,
    end_direction: RouteDirection | None,
) -> bool:
    if len(points) < 2:
        return True
    start_dx, start_dy = _direction_delta(start_direction, 1.0)
    end_dx, end_dy = _direction_delta(end_direction, 1.0)
    first_dx, first_dy = points[1][0] - points[0][0], points[1][1] - points[0][1]
    last_dx, last_dy = points[-1][0] - points[-2][0], points[-1][1] - points[-2][1]
    return (first_dx * start_dx + first_dy * start_dy >= 0.0
            and last_dx * end_dx + last_dy * end_dy <= 0.0)


def _grid_leg(
    start: tuple[float, float], end: tuple[float, float],
    obstacles: Sequence[RoutingObstacle], xs: list[float], ys: list[float],
    start_direction: RouteDirection | None, end_direction: RouteDirection | None,
) -> tuple[tuple[float, float], ...] | None:
    """Dijkstra on at most 64x64 coordinates, with precomputed blocked edges."""
    horizontal: set[tuple[int, int]] = set()
    vertical: set[tuple[int, int]] = set()
    for obstacle in obstacles:
        # Strict interiors agree exactly with _segment_hits_obstacle: touching
        # the boundary of an already inflated rectangle retains clearance.
        h_columns = range(max(0, bisect_right(xs, obstacle.left) - 1),
                          min(len(xs) - 1, bisect_left(xs, obstacle.right)))
        h_rows = range(bisect_right(ys, obstacle.top), bisect_left(ys, obstacle.bottom))
        horizontal.update((ix, iy) for iy in h_rows for ix in h_columns)
        v_columns = range(bisect_right(xs, obstacle.left), bisect_left(xs, obstacle.right))
        v_rows = range(max(0, bisect_right(ys, obstacle.top) - 1),
                       min(len(ys) - 1, bisect_left(ys, obstacle.bottom)))
        vertical.update((ix, iy) for ix in v_columns for iy in v_rows)
    first = (xs.index(start[0]), ys.index(start[1]), -1)
    goal = (xs.index(end[0]), ys.index(end[1]))
    queue = [(0.0, 0, first)]
    costs = {first: (0.0, 0)}
    previous: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    while queue:
        cost, bends, state = heappop(queue)
        if costs.get(state) != (cost, bends):
            continue
        ix, iy, old_axis = state
        if (ix, iy) == goal:
            path = [(xs[ix], ys[iy])]
            while state != first:
                state = previous[state]
                path.append((xs[state[0]], ys[state[1]]))
            return tuple(reversed(path))
        for nx, ny, axis in ((ix - 1, iy, 0), (ix, iy - 1, 1),
                             (ix, iy + 1, 1), (ix + 1, iy, 0)):
            if not (0 <= nx < len(xs) and 0 <= ny < len(ys)):
                continue
            edge = (min(ix, nx), iy) if axis == 0 else (ix, min(iy, ny))
            if edge in (horizontal if axis == 0 else vertical):
                continue
            points = ((xs[ix], ys[iy]), (xs[nx], ys[ny]))
            if not _leg_directions_valid(points, start_direction if state == first else None,
                                         end_direction if (nx, ny) == goal else None):
                continue
            turning = old_axis not in (-1, axis)
            distance = abs(xs[nx] - xs[ix]) + abs(ys[ny] - ys[iy])
            score = (cost + distance + 24.0 * turning, bends + turning)
            next_state = (nx, ny, axis)
            if score < costs.get(next_state, (math.inf, math.inf)):
                costs[next_state] = score
                previous[next_state] = state
                heappush(queue, (*score, next_state))
    return None


def _local_leg(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: Sequence[RoutingObstacle],
    start_direction: RouteDirection | None = None,
    end_direction: RouteDirection | None = None,
) -> tuple[tuple[float, float], ...]:
    if start == end:
        if any(obstacle.contains(start) for obstacle in obstacles):
            raise RoutingError("Точка трассы находится внутри препятствия.")
        return (start,)
    candidates: list[tuple[tuple[float, float], ...]] = []
    if math.isclose(start[0], end[0], abs_tol=1e-9) or math.isclose(
        start[1], end[1], abs_tol=1e-9
    ):
        candidates.append((start, end))
    candidates.extend(
        (
            (start, (end[0], start[1]), end),
            (start, (start[0], end[1]), end),
        )
    )
    def safe_candidates(rows):
        result = []
        for candidate in rows:
            vertices = normalize_route(RouteVertex(x, y) for x, y in candidate)
            points = tuple(item.point for item in vertices)
            if _leg_directions_valid(points, start_direction, end_direction) and not _route_score(points, obstacles)[0]:
                result.append(points)
        return result

    # Most edited wires are direct or L-shaped. Do not build a graph for them.
    direct = safe_candidates(candidates)
    if direct:
        return min(direct, key=lambda points: (_route_score(points, obstacles), points))
    xs, ys = _coordinate_candidates(start, end, obstacles)
    candidates = []
    candidates.extend(
        (start, (x, start[1]), (x, end[1]), end) for x in xs
    )
    candidates.extend(
        (start, (start[0], y), (end[0], y), end) for y in ys
    )

    safe = safe_candidates(candidates)
    grid = _grid_leg(start, end, obstacles, xs, ys, start_direction, end_direction)
    if grid:
        normalized = normalize_route(RouteVertex(x, y) for x, y in grid)
        safe.extend(safe_candidates((tuple(point.point for point in normalized),)))
    if not safe:
        raise RoutingError("Не найден свободный ортогональный путь: измените размещение или ручные изгибы.")
    return min(safe, key=lambda points: (_route_score(points, obstacles), points))


def _terminal_stub_length(
    endpoint: RouteVertex, direction: RouteDirection | None, distance: float,
    obstacles: Sequence[RoutingObstacle], clearance: float,
) -> float:
    """Do not let the short terminal lead jump through a nearby foreign body."""
    if direction is None or distance == 0.0:
        return 0.0
    ux, uy = _direction_delta(direction, 1.0)
    x, y = endpoint.point
    for body in obstacles:
        if body.contains(endpoint.point):
            raise RoutingError("Вывод находится внутри тела аппарата: проверьте размещение.")
        own_boundary = body.left <= x <= body.right and body.top <= y <= body.bottom
        if own_boundary:
            if _segment_hits_obstacle((x, y), (x + ux * distance, y + uy * distance), body):
                raise RoutingError("Направление вывода ведёт внутрь собственного аппарата.")
            continue
        obstacle = body.inflated(clearance)
        if obstacle.contains(endpoint.point):
            raise RoutingError("Для вывода недостаточно свободного места рядом с аппаратом.")
        frontier = None
        if ux and obstacle.top < y < obstacle.bottom:
            if ux > 0 and obstacle.left >= x:
                frontier = obstacle.left - x
            elif ux < 0 and obstacle.right <= x:
                frontier = x - obstacle.right
        elif uy and obstacle.left < x < obstacle.right:
            if uy > 0 and obstacle.top >= y:
                frontier = obstacle.top - y
            elif uy < 0 and obstacle.bottom <= y:
                frontier = y - obstacle.bottom
        if frontier is not None:
            distance = min(distance, frontier)
    if distance <= 0.0:
        raise RoutingError("Перед выводом нет свободного места для трассы.")
    return distance


def _direct_facing_port_join(request: RoutingRequest) -> bool:
    """Permit a short real port-to-port lead within its own body's padding.

    Two facing terminals may be closer than the preferred routing clearance
    (for example, a bus end two units from a breaker). The raw body remains
    solid. A foreign body's padding is never waived by this exception.
    """
    if (request.start_direction is None or request.end_direction is None
            or any(vertex.pinned for vertex in request.manual_vertices)):
        return False
    start = request.start.point
    end = request.end.point
    dx, dy = end[0] - start[0], end[1] - start[1]
    ux, uy = _direction_delta(request.start_direction, 1.0)
    vx, vy = _direction_delta(request.end_direction, 1.0)
    if ((ux, uy) != (-vx, -vy) or dx * uy != dy * ux
            or dx * ux + dy * uy <= 0.0):
        return False
    for body in request.obstacles:
        if _segment_hits_obstacle(start, end, body):
            return False
        if not _segment_hits_obstacle(start, end, body.inflated(request.clearance)):
            continue
        if not any(body.left <= x <= body.right and body.top <= y <= body.bottom
                   for x, y in (start, end)):
            return False
    return True


def build_orthogonal_route(request: RoutingRequest) -> tuple[RouteVertex, ...]:
    """Построить локальный маршрут с сохранением пользовательских точек."""

    # No rectangle is silently discarded merely because it contains a port.
    # A port on the boundary may leave outward; an interior endpoint needs
    # its geometry corrected (or explicit ownership-aware treatment upstream).
    if any(obstacle.contains(request.start.point) or obstacle.contains(request.end.point)
           for obstacle in request.obstacles):
        raise RoutingError("Вывод находится внутри тела аппарата: проверьте размещение.")
    if _direct_facing_port_join(request):
        return request.start, request.end
    obstacles = tuple(obstacle.inflated(request.clearance) for obstacle in request.obstacles)
    stub_length = request.port_stub
    if request.start_direction is not None and request.end_direction is not None:
        unit_start = _direction_delta(request.start_direction, 1.0)
        unit_end = _direction_delta(request.end_direction, 1.0)
        dx, dy = request.end.x - request.start.x, request.end.y - request.start.y
        if (unit_start == (-unit_end[0], -unit_end[1])
                and dx * unit_start[1] == dy * unit_start[0]
                and dx * unit_start[0] + dy * unit_start[1] > 0.0):
            stub_length = min(stub_length, (abs(dx) + abs(dy)) / 2.0)
    start_length = _terminal_stub_length(request.start, request.start_direction, stub_length,
                                         request.obstacles, request.clearance)
    end_length = _terminal_stub_length(request.end, request.end_direction, stub_length,
                                       request.obstacles, request.clearance)
    start_dx, start_dy = _direction_delta(request.start_direction, start_length)
    end_dx, end_dy = _direction_delta(request.end_direction, end_length)
    start_stub = RouteVertex(
        request.start.x + start_dx,
        request.start.y + start_dy,
        RouteVertexSource.AUTOMATIC,
    )
    end_stub = RouteVertex(
        request.end.x + end_dx,
        request.end.y + end_dy,
        RouteVertexSource.AUTOMATIC,
    )

    anchors: list[RouteVertex] = [start_stub]
    anchors.extend(vertex for vertex in request.manual_vertices if vertex.pinned)
    anchors.append(end_stub)
    result: list[RouteVertex] = [request.start]
    if not _same(request.start, start_stub):
        result.append(start_stub)

    for leg_index, (first, second) in enumerate(zip(anchors, anchors[1:])):
        departure = request.start_direction if leg_index == 0 else None
        if leg_index and len(result) >= 2:
            dx, dy = result[-1].x - result[-2].x, result[-1].y - result[-2].y
            if dx:
                departure = RouteDirection.RIGHT if dx > 0 else RouteDirection.LEFT
            elif dy:
                departure = RouteDirection.DOWN if dy > 0 else RouteDirection.UP
        leg = _local_leg(first.point, second.point, obstacles,
                         departure,
                         request.end_direction if leg_index == len(anchors) - 2 else None)
        for index, (x, y) in enumerate(leg[1:], start=1):
            if index == len(leg) - 1:
                result.append(second)
            else:
                result.append(RouteVertex(x, y))

    if not _same(end_stub, request.end):
        result.append(request.end)
    return normalize_route(result)


class LocalRouteCache:
    """Небольшой кэш, инвалидируемый по ID затронутого маршрута."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, RoutingRequest], tuple[RouteVertex, ...]] = {}

    def get_or_build(
        self, route_id: str, request: RoutingRequest
    ) -> tuple[RouteVertex, ...]:
        key = (str(route_id), request)
        try:
            return self._values[key]
        except KeyError:
            value = build_orthogonal_route(request)
            self._values[key] = value
            return value

    def invalidate(self, route_id: str | None = None) -> None:
        if route_id is None:
            self._values.clear()
            return
        token = str(route_id)
        self._values = {
            key: value for key, value in self._values.items() if key[0] != token
        }

    def __len__(self) -> int:
        return len(self._values)


__all__ = [
    "LocalRouteCache",
    "RouteDirection",
    "RouteVertex",
    "RouteVertexSource",
    "RoutingObstacle",
    "RoutingError",
    "RoutingRequest",
    "build_orthogonal_route",
    "normalize_route",
]
