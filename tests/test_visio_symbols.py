# -*- coding: utf-8 -*-
"""Reference-Visio symbol contract without changing electrical terminals."""
from __future__ import annotations

from dataclasses import replace
from xml.etree import ElementTree

import pytest

from rza_calc.editor.symbol_svg import primitive_svg, rotate_point, symbol_svg
from rza_calc.editor.symbols import (
    DIAGRAM_DEENERGIZED_STROKE,
    DIAGRAM_MONOCHROME_STROKE,
    DIAGRAM_NEUTRAL_STROKE,
    SWITCH_CLOSED_FILL,
    SWITCH_OPEN_FILL,
    DiagramColorMode,
    build_symbol,
    switch_state_fill,
    voltage_stroke,
)


def _svg_children(markup: str, tag: str) -> list[ElementTree.Element]:
    return [item for item in ElementTree.fromstring(markup) if item.tag.endswith(tag)]


@pytest.mark.parametrize("key", ("circuit_breaker", "recloser"))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
@pytest.mark.parametrize("opened", (False, True))
def test_breaker_bar_tracks_conductor_axis_and_preserves_ports(
    key: str, angle: int, opened: bool,
) -> None:
    geometry = build_symbol(key, opened=opened)
    other_position = build_symbol(key, opened=not opened)
    assert geometry.terminals == other_position.terminals
    assert [(p.role, p.x, p.y) for p in geometry.terminals] == [
        ("a", -32.0, 0.0), ("b", 32.0, 0.0),
    ]
    body = next(item for item in geometry.primitives if item.kind == "rect")
    other_body = next(item for item in other_position.primitives if item.kind == "rect")
    assert replace(body, state_fill=None) == replace(other_body, state_fill=None)
    assert body.voltage_role == "body"
    assert body.state_fill == ("open" if opened else "closed")
    bar = next(
        item for item in geometry.primitives
        if item.kind == "line" and item.voltage_role == "body"
    )
    start, end = [rotate_point(*point, angle) for point in bar.points]
    axis = rotate_point(1.0, 0.0, angle)
    vector = (end[0] - start[0], end[1] - start[1])
    dot = vector[0] * axis[0] + vector[1] * axis[1]
    cross = vector[0] * axis[1] - vector[1] * axis[0]
    assert dot == pytest.approx(0.0) if opened else abs(dot) > 0.0
    assert abs(cross) > 0.0 if opened else cross == pytest.approx(0.0)
    for point in bar.points:
        assert abs(point[0]) < body.half_width
        assert abs(point[1]) < body.half_height


@pytest.mark.parametrize("key", ("circuit_breaker", "recloser"))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
@pytest.mark.parametrize("opened", (False, True))
def test_svg_switch_fill_is_independent_from_nominal_outline_and_dead_leads(
    key: str, angle: int, opened: bool,
) -> None:
    markup = symbol_svg(
        key, rotation=angle, opened=opened, show_terminals=False,
        terminal_strokes={"a": DIAGRAM_DEENERGIZED_STROKE, "b": "#ABCDEF"},
        body_stroke=voltage_stroke(10_000),
    )
    rect = _svg_children(markup, "rect")[0]
    assert rect.attrib["fill"] == (SWITCH_OPEN_FILL if opened else SWITCH_CLOSED_FILL)
    assert rect.attrib["stroke"] == "#002060"
    lines = _svg_children(markup, "line")
    assert [item.attrib["stroke"] for item in lines[:2]] == [
        DIAGRAM_DEENERGIZED_STROKE, "#ABCDEF",
    ]
    assert lines[-1].attrib["stroke"] == "#002060"
    assert lines[-1].attrib["fill"] == "none"
    assert rect.attrib["width"] == rect.attrib["height"] == "24"


def test_unknown_nominal_body_does_not_guess_from_a_live_or_dead_terminal() -> None:
    markup = symbol_svg(
        "circuit_breaker", opened=True, show_terminals=False,
        terminal_strokes={"a": "#123456", "b": DIAGRAM_DEENERGIZED_STROKE},
    )
    assert _svg_children(markup, "rect")[0].attrib["stroke"] == DIAGRAM_NEUTRAL_STROKE


def test_svg_body_color_explicit_argument_then_body_role_then_nominal_fallback() -> None:
    common = {"voltage_stroke": "#100000", "terminal_strokes": {"body": "#200000"}}
    assert _svg_children(symbol_svg("circuit_breaker", **common), "rect")[0].attrib["stroke"] == "#200000"
    assert _svg_children(
        symbol_svg("circuit_breaker", body_stroke="#300000", **common), "rect",
    )[0].attrib["stroke"] == "#300000"
    assert _svg_children(
        symbol_svg("circuit_breaker", voltage_stroke="#100000"), "rect",
    )[0].attrib["stroke"] == "#100000"


@pytest.mark.parametrize("opened", (False, True))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_monochrome_switch_preserves_indicator_and_removes_state_hues(
    opened: bool, angle: int,
) -> None:
    kwargs = dict(
        rotation=angle, opened=opened, show_terminals=False,
        voltage_stroke="#C00000", body_stroke="#002060",
    )
    colored = ElementTree.fromstring(symbol_svg("circuit_breaker", **kwargs))
    mono = ElementTree.fromstring(symbol_svg(
        "circuit_breaker", color_mode=DiagramColorMode.MONOCHROME, **kwargs,
    ))
    assert len(colored) == len(mono)
    for before, after in zip(colored, mono, strict=True):
        before_geometry = {k: v for k, v in before.attrib.items() if k not in {"stroke", "fill"}}
        after_geometry = {k: v for k, v in after.attrib.items() if k not in {"stroke", "fill"}}
        assert before_geometry == after_geometry
        assert after.attrib["stroke"] == DIAGRAM_MONOCHROME_STROKE
    rect = next(item for item in mono if item.tag.endswith("rect"))
    assert rect.attrib["fill"] == ("#FFFFFF" if opened else "#E5E7EB")


def test_state_fill_survives_nonrotating_primitive_copy() -> None:
    geometry = build_symbol("circuit_breaker", opened=True)
    body = next(item for item in geometry.primitives if item.kind == "rect")
    markup = primitive_svg(replace(body, rotates=False), rotation=90, body_stroke="#002060")
    assert ElementTree.fromstring(markup).attrib["fill"] == SWITCH_OPEN_FILL


def test_switch_fill_resolver_validates_state() -> None:
    assert switch_state_fill("open") == "#92D050"
    assert switch_state_fill("closed") == "#E5B9B5"
    assert switch_state_fill("open", color_mode="monochrome") == "#FFFFFF"
    with pytest.raises(ValueError):
        switch_state_fill("unknown")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "roles"),
    (("transformer_2w", ("hv", "lv")), ("transformer_3w", ("hv", "mv", "lv"))),
)
def test_transformer_winding_circles_use_their_semantic_voltage_roles(
    key: str, roles: tuple[str, ...],
) -> None:
    geometry = build_symbol(key)
    circles = [item for item in geometry.primitives if item.kind == "circle"]
    assert [item.voltage_role for item in circles] == list(roles)
    colors = {"hv": "#C00000", "mv": "#002060", "lv": "#BF9000"}
    markup = symbol_svg(key, terminal_strokes=colors, show_terminals=False)
    assert [item.attrib["stroke"] for item in _svg_children(markup, "circle")] == [
        colors[role] for role in roles
    ]


def test_recloser_retains_distinct_apv_arc_without_changing_breaker_body() -> None:
    for opened in (False, True):
        breaker = build_symbol("circuit_breaker", opened=opened)
        recloser = build_symbol("recloser", opened=opened)
        assert [p for p in breaker.primitives if p.voltage_role == "body"] == [
            p for p in recloser.primitives if p.voltage_role == "body"
        ]
        assert any(p.kind == "arc" for p in recloser.primitives)
        assert not any(p.kind == "arc" for p in breaker.primitives)


@pytest.mark.parametrize(("width", "height"), ((160.0, 16.0), (16.0, 160.0)))
def test_busbar_weight_matches_visio_ratio_in_both_orientations(width: float, height: float) -> None:
    geometry = build_symbol("busbar", width=width, height=height)
    assert geometry.primitives[0].stroke_scale == 2.25
    markup = symbol_svg("busbar", width=width, height=height, show_terminals=False)
    line = _svg_children(markup, "line")[0]
    assert line.attrib["stroke-width"] == "4.5"
    assert line.attrib["stroke-linecap"] == "round"


def test_only_reference_observed_voltage_classes_are_updated() -> None:
    assert voltage_stroke(35_000) == "#C00000"
    assert voltage_stroke(10_000) == "#002060"
    assert voltage_stroke(400) == "#BF9000"
    assert voltage_stroke(220_000) == "#7A1638"
    assert voltage_stroke(110_000) == "#DB4437"
    assert voltage_stroke(6_000) == "#009B83"
