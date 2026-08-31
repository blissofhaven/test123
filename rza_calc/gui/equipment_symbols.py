# -*- coding: utf-8 -*-
"""Small, stateless SVG symbols for the future general diagram editor.

The functions in this module are presentation-only.  They do not own switch
state and never determine electrical connectivity; callers pass the state
already resolved from the Domain Model.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from types import MappingProxyType
from typing import Mapping

from ..domain.electrical import SwitchPosition


@dataclass(frozen=True, slots=True)
class EquipmentSymbolSpec:
    key: str
    display_name: str
    terminal_a: tuple[int, int]
    terminal_b: tuple[int, int]
    view_box: tuple[int, int, int, int]


RECLOSER_SYMBOL = EquipmentSymbolSpec(
    "recloser",
    "Реклоузер",
    (0, 20),
    (80, 20),
    (0, 0, 80, 40),
)

EQUIPMENT_SYMBOLS: Mapping[str, EquipmentSymbolSpec] = MappingProxyType(
    {RECLOSER_SYMBOL.key: RECLOSER_SYMBOL}
)


def render_recloser_symbol(
    position: SwitchPosition,
    *,
    equipment_id: str = "",
    stroke: str = "currentColor",
) -> str:
    """Render a distinct two-terminal recloser symbol in OPEN/CLOSED state."""
    if not isinstance(position, SwitchPosition):
        position = SwitchPosition(position)
    blade = (
        '<path class="recloser-blade" d="M 30 20 L 50 20" />'
        if position is SwitchPosition.CLOSED
        else '<path class="recloser-blade" d="M 30 20 L 49 8" />'
    )
    identifier = escape(equipment_id, quote=True)
    colour = escape(stroke, quote=True)
    position_name = (
        "Включён" if position is SwitchPosition.CLOSED else "Отключён"
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 40" '
        f'role="img" aria-label="Реклоузер: {position_name}" '
        f'data-symbol-key="recloser" data-equipment-id="{identifier}" '
        f'data-position="{position.value}">'
        f'<g fill="none" stroke="{colour}" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path class="terminal terminal-a" d="M 0 20 H 24" />'
        '<path class="terminal terminal-b" d="M 56 20 H 80" />'
        '<circle class="contact contact-a" cx="28" cy="20" r="3" />'
        '<circle class="contact contact-b" cx="52" cy="20" r="3" />'
        f'{blade}'
        '<rect class="recloser-body" x="20" y="2" width="40" height="36" rx="6" />'
        '</g></svg>'
    )


__all__ = [
    "EQUIPMENT_SYMBOLS",
    "EquipmentSymbolSpec",
    "RECLOSER_SYMBOL",
    "render_recloser_symbol",
]
