"""Shared, read-only spacing of independent electrical attachments on a bus."""
from __future__ import annotations

import math

from rza_calc.domain.diagram import RouteAnchorKind

# Two nominal 10-unit grid steps keep distinct white junctions and their
# perpendicular leads readable. This is drawing space, never an electrical
# distance, and is deliberately independent of zoom or calculated values.
BUS_ATTACHMENT_GAP = 20.0

#  Насколько две точки должны сойтись, чтобы считаться попавшими в одно место.
#  Половина клетки сетки: ближе этого человек уже не различает две точки на
#  шине, и рисунок начинает врать, будто присоединение одно.
BUS_ATTACHMENT_MERGE_TOLERANCE = 10.0


def endpoint_fraction(representation, width, height, anchor, point):
    try:
        fraction = float(anchor.anchor_key)
        if math.isfinite(fraction):
            return max(0.0, min(1.0, fraction))
    except (TypeError, ValueError):
        pass
    radians = math.radians(-representation.rotation_deg)
    dx, dy = point.x - representation.x, point.y - representation.y
    local_x = dx * math.cos(radians) - dy * math.sin(radians)
    local_y = dx * math.sin(radians) + dy * math.cos(radians)
    return max(0.0, min(1.0, (local_y / height if height > width else local_x / width) + 0.5))


def available_bus_fraction(
    diagram, representation, *, width, height, requested,
    exclude_route_ids=(), minimum_gap=BUS_ATTACHMENT_GAP,
    merge_tolerance=BUS_ATTACHMENT_MERGE_TOLERANCE,
):
    """Keep the attachment exactly where it was drawn; separate only collisions.

    Решение заказчика 31.08.2026. Прежнее правило раздвигало КАЖДОЕ новое
    присоединение на ``minimum_gap`` от соседнего, поэтому точка на шине
    уезжала вбок от провода, а провод дорисовывал боковой крючок к своей
    точке. Теперь точка ставится ровно там, где её привели, и сдвигается,
    только если она попала в уже занятое место — ближе ``merge_tolerance``.
    Тогда две разные точки нарисовались бы одна поверх другой, и рисунок
    сказал бы, что присоединение одно, хотя электрически их два.

    A route being reconnected/moved must be explicitly excluded by its own ID.
    Mere overlap of saved endpoints never means they form one attachment.
    """
    values = (width, height, requested, minimum_gap, merge_tolerance)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("Положение и размеры присоединения должны быть конечными числами.")
    if min(width, height, minimum_gap) <= 0:
        raise ValueError("Шина и расстояние между присоединениями должны быть положительными.")
    if merge_tolerance < 0:
        raise ValueError("Порог слияния присоединений не может быть отрицательным.")
    requested = max(0.0, min(1.0, requested))
    excluded = set(exclude_route_ids)
    occupied = []
    for route in diagram.routes.values():
        if route.id in excluded or route.page_id != representation.page_id:
            continue
        for anchor, point in ((route.start_anchor, route.waypoints[0]),
                              (route.end_anchor, route.waypoints[-1])):
            if anchor.kind is RouteAnchorKind.BUS and anchor.representation_id == representation.id:
                occupied.append(endpoint_fraction(representation, width, height, anchor, point))
    span = max(width, height)
    separation = minimum_gap / span
    merge = merge_tolerance / span
    # 1e-10 здесь только гасит представление дробных координат.
    if all(abs(requested - other) + 1e-10 >= merge for other in occupied):
        return requested          # Место свободно — точка остаётся под проводом.
    #  При настоящем столкновении точка отодвигается на наименьшее расстояние,
    #  при котором она перестаёт сливаться с соседней (merge), а не сразу на
    #  полную ширину раздвижки: прыжок на 20 единиц там, где хватает 10, — это
    #  тот же увод точки от провода, только реже.
    candidates = {0.0, 1.0}
    for value in occupied:
        candidates.update((value - merge, value + merge,
                           value - separation, value + separation))
    valid = [value for value in candidates if 0.0 <= value <= 1.0
             and all(abs(value - other) + 1e-10 >= merge for other in occupied)]
    if not valid:
        raise ValueError("На шине нет свободного места для отдельного присоединения. Удлините шину.")
    return min(valid, key=lambda value: (round(abs(value - requested), 12), value))
