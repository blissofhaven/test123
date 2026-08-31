"""Read-only display geometry for orthogonal wire crossings.

The input is deliberately separate from DiagramDocument.  A bridge never changes
waypoints, IDs, electrical nodes or hit-testing geometry. A junction requires
both shared node identity and a local endpoint/attachment, not a remote common
net ID or coincident coordinates alone.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass
import heapq
import math
from typing import Iterable

Point = tuple[float, float]
_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class BridgeWire:
    key: str
    points: tuple[Point, ...]
    node_id: str | None = None
    endpoint_node_ids: tuple[str | None, str | None] = (None, None)
    bridge_allowed: bool = True
    visual_half_width: float = 0.0
    # Viewers may supply cosmetic pen/cap clearance converted into scene units.
    # This affects display gaps only, never route or electrical geometry.
    visual_gap_padding: float = 2.5


@dataclass(frozen=True, slots=True)
class WireBridge:
    """One semicircular jump, expressed on an original segment's axis."""

    segment_index: int
    start: Point
    end: Point
    center: Point
    radius: float
    horizontal: bool


@dataclass(frozen=True, slots=True)
class WireDisplay:
    wire: BridgeWire
    bridges: tuple[WireBridge, ...] = ()
    junctions: tuple[Point, ...] = ()
    gaps: tuple[tuple[int, Point, Point], ...] = ()


@dataclass(frozen=True, slots=True)
class _Segment:
    wire_index: int
    index: int
    start: Point
    end: Point
    horizontal: bool

    @property
    def low(self) -> float:
        axis = 0 if self.horizontal else 1
        return min(self.start[axis], self.end[axis])

    @property
    def high(self) -> float:
        axis = 0 if self.horizontal else 1
        return max(self.start[axis], self.end[axis])


def _same(first: Point, second: Point) -> bool:
    return abs(first[0] - second[0]) <= _EPS and abs(first[1] - second[1]) <= _EPS


def _nodes_at(wire: BridgeWire, point: Point) -> set[str]:
    nodes = {wire.node_id} if wire.node_id else set()
    if wire.points and _same(point, wire.points[0]) and wire.endpoint_node_ids[0]:
        nodes.add(wire.endpoint_node_ids[0])
    if wire.points and _same(point, wire.points[-1]) and wire.endpoint_node_ids[1]:
        nodes.add(wire.endpoint_node_ids[1])
    return nodes


def _is_endpoint(wire: BridgeWire, point: Point) -> bool:
    return bool(wire.points) and (
        _same(point, wire.points[0]) or _same(point, wire.points[-1])
    )


def _crossings(segments: tuple[_Segment, ...]):
    """Sweep only orthogonal segments, avoiding an all-wire pair scan."""

    horizontal = sorted(
        (segment for segment in segments if segment.horizontal),
        key=lambda segment: segment.low,
    )
    vertical = sorted(
        (segment for segment in segments if not segment.horizontal),
        key=lambda segment: segment.start[0],
    )
    active: list[tuple[float, int]] = []
    expiry: list[tuple[float, int]] = []
    next_horizontal = 0
    for upright in vertical:
        x = upright.start[0]
        while next_horizontal < len(horizontal) and horizontal[next_horizontal].low <= x + _EPS:
            flat = horizontal[next_horizontal]
            insort(active, (flat.start[1], next_horizontal))
            heapq.heappush(expiry, (flat.high, next_horizontal))
            next_horizontal += 1
        while expiry and expiry[0][0] < x - _EPS:
            _, index = heapq.heappop(expiry)
            entry = (horizontal[index].start[1], index)
            active.pop(bisect_left(active, entry))
        first = bisect_left(active, (upright.low - _EPS, -1))
        last = bisect_right(active, (upright.high + _EPS, len(horizontal)))
        for y, index in active[first:last]:
            flat = horizontal[index]
            if flat.wire_index != upright.wire_index:
                yield flat, upright, (x, y)


def build_wire_displays(
    wires: Iterable[BridgeWire], *, radius: float = 6.0, corner_clearance: float = 0.75
) -> dict[str, WireDisplay]:
    """Plan bridges without modifying input or deriving electrical connectivity.

    A stable wire key chooses the jumping wire, independent of iteration order
    and segment direction.  If it has no room near a corner, the other wire is
    used.  Close jumps on one segment merge into one semicircle, never overlapping
    or extending past the segment.  Non-orthogonal/degenerate segments are left
    unchanged, allowing legacy geometry to be displayed conservatively.
    """

    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Bridge radius must be finite and positive.")
    if not math.isfinite(corner_clearance) or corner_clearance < 0:
        raise ValueError("Corner clearance must be finite and non-negative.")
    rows = tuple(sorted(wires, key=lambda wire: wire.key))
    if any(not math.isfinite(wire.visual_half_width) or wire.visual_half_width < 0 for wire in rows):
        raise ValueError("Wire display half-width must be finite and non-negative.")
    if any(not math.isfinite(wire.visual_gap_padding) or wire.visual_gap_padding < 0 for wire in rows):
        raise ValueError("Wire gap padding must be finite and non-negative.")
    if len({wire.key for wire in rows}) != len(rows):
        raise ValueError("Wire keys must be unique.")
    segments: list[_Segment] = []
    for wire_index, wire in enumerate(rows):
        for index, (first, second) in enumerate(zip(wire.points, wire.points[1:])):
            if not all(math.isfinite(value) for value in (*first, *second)):
                continue
            if _same(first, second):
                continue
            horizontal = abs(first[1] - second[1]) <= _EPS
            vertical = abs(first[0] - second[0]) <= _EPS
            if horizontal or vertical:
                segments.append(_Segment(wire_index, index, first, second, horizontal))
    marks: dict[tuple[int, int], tuple[_Segment, list[tuple[float, float]]]] = {}
    joints: dict[int, set[Point]] = {}
    protected: dict[int, set[Point]] = {}
    candidates: list[tuple[_Segment, _Segment, Point]] = []
    jumped: list[tuple[_Segment, _Segment, Point]] = []
    wide_bus_gaps: dict[int, list[tuple[int, Point, Point]]] = {}
    seen: set[tuple[int, int, Point]] = set()
    for flat, upright, point in _crossings(tuple(segments)):
        indices = tuple(sorted((flat.wire_index, upright.wire_index)))
        marker = (*indices, point)
        if marker in seen:
            continue
        seen.add(marker)
        first_wire, second_wire = rows[flat.wire_index], rows[upright.wire_index]
        if (
            (_is_endpoint(first_wire, point) or _is_endpoint(second_wire, point))
            and _nodes_at(first_wire, point) & _nodes_at(second_wire, point)
        ):
            # A bound endpoint can make a true T/bus attachment. Two bypass
            # interiors still cross without a local joint, even if both lead
            # to the same bus elsewhere. Never invent a dot at their bends.
            joints.setdefault(indices[0], set()).add(point)
            for index in indices:
                protected.setdefault(index, set()).add(point)
            continue
        candidates.append((flat, upright, point))
    for flat, upright, point in candidates:
        # A small arc would disappear inside a filled bus and falsely resemble
        # a connection. Keep an orthogonal thin conductor visibly interrupted
        # across the whole band. True, explicitly bound T junctions were handled
        # above and never enter this branch. Pixels/ink do not create topology.
        wide = next((segment for segment in (flat, upright)
                     if not rows[segment.wire_index].bridge_allowed
                     and rows[segment.wire_index].visual_half_width > 0), None)
        if wide is not None:
            crossing = upright if wide is flat else flat
            if rows[crossing.wire_index].bridge_allowed:
                axis = 0 if crossing.horizontal else 1
                clearance = rows[wide.wire_index].visual_half_width + rows[wide.wire_index].visual_gap_padding
                low = max(crossing.low, point[axis] - clearance)
                high = min(crossing.high, point[axis] + clearance)
                if high > low:
                    fixed = crossing.start[1 - axis]
                    first = (low, fixed) if crossing.horizontal else (fixed, low)
                    second = (high, fixed) if crossing.horizontal else (fixed, high)
                    if crossing.end[axis] < crossing.start[axis]:
                        first, second = second, first
                    wide_bus_gaps.setdefault(crossing.wire_index, []).append((crossing.index, first, second))
                continue
        for owner in sorted((flat, upright), key=lambda segment: rows[segment.wire_index].key):
            if not rows[owner.wire_index].bridge_allowed:
                continue
            axis = 0 if owner.horizontal else 1
            position = point[axis]
            available = min(position - owner.low, owner.high - position) - corner_clearance
            for joint in protected.get(owner.wire_index, ()):
                if abs(joint[1 - axis] - owner.start[1 - axis]) <= _EPS and owner.low <= joint[axis] <= owner.high:
                    available = min(available, abs(joint[axis] - position) - corner_clearance)
            jump_radius = min(radius, available)
            if jump_radius < min(1.0, radius):
                continue
            key = (owner.wire_index, owner.index)
            marks.setdefault(key, (owner, []))[1].append((position - jump_radius, position + jump_radius))
            jumped.append((owner, upright if owner is flat else flat, point))
            break
    bridges: dict[int, list[WireBridge]] = {}
    for (wire_index, _), (segment, intervals) in marks.items():
        merged: list[list[float]] = []
        for low, high in sorted(set(intervals)):
            if merged and low <= merged[-1][1] + corner_clearance:
                merged[-1][1] = max(merged[-1][1], high)
            else:
                merged.append([low, high])
        axis = 0 if segment.horizontal else 1
        forward = segment.end[axis] > segment.start[axis]
        for low, high in merged:
            fixed = segment.start[1 - axis]
            first = (low, fixed) if segment.horizontal else (fixed, low)
            second = (high, fixed) if segment.horizontal else (fixed, high)
            center = ((low + high) / 2, fixed) if segment.horizontal else (fixed, (low + high) / 2)
            bridges.setdefault(wire_index, []).append(WireBridge(
                segment.index,
                first if forward else second,
                second if forward else first,
                center,
                (high - low) / 2,
                segment.horizontal,
            ))
    gaps: dict[int, list[tuple[int, Point, Point]]] = wide_bus_gaps
    for owner, under, point in jumped:
        axis = 0 if owner.horizontal else 1
        for bridge in bridges.get(owner.wire_index, ()):
            if bridge.segment_index != owner.index or abs(point[axis] - bridge.center[axis]) > bridge.radius + _EPS:
                continue
            height = math.sqrt(max(0.0, bridge.radius ** 2 - (point[axis] - bridge.center[axis]) ** 2))
            # Jumps bulge upward for a horizontal wire and rightward for a
            # vertical one, independent of waypoint order.
            cut = bridge.center[1 - axis] + (-height if owner.horizontal else height)
            low, high = max(under.low, cut - 1.8), min(under.high, cut + 1.8)
            if high > low and under.low <= cut <= under.high:
                fixed = under.start[axis]
                first = (fixed, low) if owner.horizontal else (low, fixed)
                second = (fixed, high) if owner.horizontal else (high, fixed)
                if under.end[1 - axis] < under.start[1 - axis]:
                    first, second = second, first
                gaps.setdefault(under.wire_index, []).append((under.index, first, second))
            break
    return {
        wire.key: WireDisplay(
            wire,
            tuple(sorted(bridges.get(index, ()), key=lambda jump: (jump.segment_index, math.dist(wire.points[jump.segment_index], jump.start)))),
            tuple(sorted(joints.get(index, ()))),
            tuple(gaps.get(index, ())),
        )
        for index, wire in enumerate(rows)
    }
