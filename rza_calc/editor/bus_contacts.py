"""One read-only allocation of an independent bus contact and its drawn point."""
from __future__ import annotations

from dataclasses import dataclass

from .bus_connections import (
    BUS_ATTACHMENT_GAP, BUS_ATTACHMENT_MERGE_TOLERANCE, available_bus_fraction,
)
from .orientation import bus_anchor_geometry
from .orthogonal_routing import RouteDirection


@dataclass(frozen=True, slots=True)
class BusContactPlacement:
    fraction: float
    x: float
    y: float
    direction: RouteDirection


def allocate_bus_contact(
    diagram, representation, *, width, height, requested, exclude_route_ids=(),
    minimum_gap=BUS_ATTACHMENT_GAP, merge_tolerance=BUS_ATTACHMENT_MERGE_TOLERANCE,
) -> BusContactPlacement:
    """Keep a free point, or choose the nearest distinct contact on this bus.

    Both sides occupy the same bus axis: approaching from above/below does not
    merge independent attachments. The fraction and endpoint coordinates are
    one result, so a caller cannot draw a wire to the old unallocated point.
    Only a route being moved/reconnected may exclude its own persisted ID.
    Normal gestures preserve the existing ten-unit collision contract; an
    explicit separation/repair may request the twenty-unit gap for clarity.
    """
    fraction = available_bus_fraction(
        diagram, representation, width=width, height=height, requested=requested,
        exclude_route_ids=exclude_route_ids, minimum_gap=minimum_gap,
        merge_tolerance=merge_tolerance,
    )
    x, y, direction = bus_anchor_geometry(
        width=width, height=height, rotation=representation.rotation_deg,
        center_x=representation.x, center_y=representation.y, fraction=fraction,
    )
    return BusContactPlacement(fraction, x, y, direction)


__all__ = ["BusContactPlacement", "allocate_bus_contact"]
