"""Qt conversion for cached, display-only wire jumps (no background masks)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPainterPath

from ..editor.line_bridges import WireDisplay
from ..editor.symbols import SymbolPrimitive, line_direction_marker


def visible_line_direction_marker(
    path: QPainterPath, *, reverse: bool = False, nodes: tuple[QPointF, ...] = (),
) -> SymbolPrimitive | None:
    """One arrow on real straight ink; skip curves, gaps and junction dots.

    The longest straight span is stable under storage-order reversal; equal
    lengths are ordered by geometric midpoint, not iteration direction.
    """
    candidates = []
    last = None
    index = 0
    while index < path.elementCount():
        element = path.elementAt(index)
        point = (element.x, element.y)
        if element.type == QPainterPath.ElementType.MoveToElement:
            last = point
        elif element.type == QPainterPath.ElementType.LineToElement:
            if last is not None:
                length = math.dist(last, point)
                midpoint = ((last[0] + point[0]) / 2.0, (last[1] + point[1]) / 2.0)
                marker = line_direction_marker(last, point, reverse=reverse)
                if marker is not None:
                    left, top, right, bottom = marker.bounds()
                    area = QRectF(left, top, right - left, bottom - top).adjusted(-2, -2, 2, 2)
                    if not any(area.contains(node) for node in nodes):
                        candidates.append(((-length, *midpoint), marker))
            last = point
        elif element.type == QPainterPath.ElementType.CurveToElement:
            endpoint = path.elementAt(index + 2)
            last = (endpoint.x, endpoint.y)
            index += 2
        index += 1
    return min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def wire_display_path(display: WireDisplay) -> QPainterPath:
    path = QPainterPath()
    points = display.wire.points
    if not points:
        return path
    path.moveTo(*points[0])
    for index, (first, second) in enumerate(zip(points, points[1:])):
        operations = []
        for bridge in display.bridges:
            if bridge.segment_index == index:
                operations.append((math.dist(first, bridge.start), "bridge", bridge))
        # Coalesce gap intervals before painting: two crossing wires must not
        # make the current pen run backwards along the edited polyline.
        gaps = []
        for segment_index, start, end in display.gaps:
            if segment_index == index:
                gaps.append((math.dist(first, start), math.dist(first, end), start, end))
        merged = []
        for low, high, start, end in sorted(gaps):
            if merged and low <= merged[-1][1]:
                if high > merged[-1][1]:
                    merged[-1] = (merged[-1][0], high, merged[-1][2], end)
            else:
                merged.append((low, high, start, end))
        operations.extend((low, "gap", (start, end)) for low, _, start, end in merged)
        consumed = 0.0
        for distance, kind, operation in sorted(operations, key=lambda item: (item[0], item[1])):
            if distance < consumed - 1e-6:
                continue
            if kind == "gap":
                start, end = operation
                path.lineTo(*start)
                path.moveTo(*end)
                consumed = math.dist(first, end)
                continue
            jump = operation
            path.lineTo(*jump.start)
            x, y = jump.center
            radius = jump.radius
            if jump.horizontal:
                forward = jump.end[0] > jump.start[0]
                start_angle, span = (180.0, -180.0) if forward else (0.0, 180.0)
            else:
                forward = jump.end[1] > jump.start[1]
                start_angle, span = (90.0, -180.0) if forward else (270.0, 180.0)
            path.arcTo(QRectF(x - radius, y - radius, radius * 2, radius * 2), start_angle, span)
            consumed = math.dist(first, jump.end)
        path.lineTo(*second)
    return path
