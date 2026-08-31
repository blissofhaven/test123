# -*- coding: utf-8 -*-
"""Presentation contract for the distinct recloser symbol."""
from __future__ import annotations

from rza_calc.domain.electrical import SwitchPosition
from rza_calc.gui.equipment_symbols import (
    EQUIPMENT_SYMBOLS,
    RECLOSER_SYMBOL,
    render_recloser_symbol,
)


def test_recloser_symbol_has_two_terminals_and_a_distinct_key():
    assert EQUIPMENT_SYMBOLS["recloser"] is RECLOSER_SYMBOL
    assert RECLOSER_SYMBOL.terminal_a != RECLOSER_SYMBOL.terminal_b
    assert RECLOSER_SYMBOL.key == "recloser"


def test_recloser_symbol_visually_distinguishes_open_and_closed_state():
    opened = render_recloser_symbol(
        SwitchPosition.OPEN, equipment_id="rec<&>"
    )
    closed = render_recloser_symbol(
        SwitchPosition.CLOSED, equipment_id="rec<&>"
    )
    assert opened != closed
    assert 'data-position="OPEN"' in opened
    assert 'data-position="CLOSED"' in closed
    assert 'd="M 30 20 L 49 8"' in opened
    assert 'd="M 30 20 L 50 20"' in closed
    assert 'data-equipment-id="rec&lt;&amp;&gt;"' in opened
