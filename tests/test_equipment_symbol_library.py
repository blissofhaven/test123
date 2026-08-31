# -*- coding: utf-8 -*-
"""Проверки библиотеки условных графических обозначений.

Тесты закрепляют инварианты, из-за нарушения которых символ выглядит
правильно, но ведёт себя неправильно: вывод не на конце проводника, разъезд
рисунка и точки подключения при повороте, несоразмерные аппараты.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.editor.orthogonal_routing import RouteDirection
from rza_calc.editor.symbol_svg import rotate_direction, rotate_point, symbol_svg
from rza_calc.editor.symbols import (
    DEFAULT_SIZES,
    build_symbol,
    canonical_key,
    default_size,
    symbol_for,
)

ALL_KEYS = tuple(DEFAULT_SIZES)
ANGLES = (0, 90, 180, 270)


def test_every_key_builds_and_has_drawing_and_terminals() -> None:
    for key in ALL_KEYS:
        geometry = build_symbol(key)
        assert geometry.primitives, f"у символа «{key}» нет ни одного примитива"
        assert geometry.terminals, f"у символа «{key}» нет выводов"
        assert geometry.width > 0 and geometry.height > 0


def test_terminals_lie_on_the_declared_outline() -> None:
    """Точка подключения обязана лежать на границе габарита или в его центре.

    Иначе соединительная линия либо не дойдёт до аппарата, либо войдёт
    внутрь него.
    """
    for key in ALL_KEYS:
        geometry = build_symbol(key)
        half_w, half_h = geometry.width / 2.0, geometry.height / 2.0
        for terminal in geometry.terminals:
            on_edge = (
                math.isclose(abs(terminal.x), half_w, abs_tol=1e-9)
                or math.isclose(abs(terminal.y), half_h, abs_tol=1e-9)
            )
            centred = math.isclose(terminal.x, 0.0, abs_tol=1e-9) and math.isclose(
                terminal.y, 0.0, abs_tol=1e-9
            )
            assert on_edge or centred, (
                f"вывод «{terminal.role}» символа «{key}» не лежит ни на границе "
                "габарита, ни в его центре"
            )
            assert abs(terminal.x) <= half_w + 1e-9
            assert abs(terminal.y) <= half_h + 1e-9


def test_terminal_meets_the_drawn_conductor() -> None:
    """К выводу обязан подходить нарисованный проводник.

    Проверяется буквально: среди примитивов есть отрезок, конец которого
    совпадает с точкой вывода. Это то самое требование «точка подключения
    находится непосредственно на выводе аппарата».
    """
    for key in ALL_KEYS:
        geometry = build_symbol(key)
        if key == "connection_point":
            continue  # узел сам является точкой, проводника у него нет
        for terminal in geometry.terminals:
            touching = any(
                any(
                    math.isclose(point[0], terminal.x, abs_tol=1e-6)
                    and math.isclose(point[1], terminal.y, abs_tol=1e-6)
                    for point in item.points
                )
                for item in geometry.primitives
                if item.kind in {"line", "polyline"}
            )
            centre_terminal = math.isclose(terminal.x, 0.0, abs_tol=1e-9) and math.isclose(
                terminal.y, 0.0, abs_tol=1e-9
            )
            assert touching or centre_terminal, (
                f"к выводу «{terminal.role}» символа «{key}» не подходит проводник"
            )


def test_rotation_moves_terminals_together_with_the_drawing() -> None:
    """Рисунок и выводы поворачиваются одним и тем же преобразованием."""
    for key in ALL_KEYS:
        geometry = build_symbol(key)
        for angle in ANGLES:
            for terminal in geometry.terminals:
                x, y = rotate_point(terminal.x, terminal.y, angle)
                direction = rotate_direction(terminal.direction, angle)
                half_w, half_h = geometry.width / 2.0, geometry.height / 2.0
                if angle % 180 == 90:
                    half_w, half_h = half_h, half_w
                assert abs(x) <= half_w + 1e-6
                assert abs(y) <= half_h + 1e-6
                if direction is RouteDirection.LEFT:
                    assert x <= 1e-6
                elif direction is RouteDirection.RIGHT:
                    assert x >= -1e-6
                elif direction is RouteDirection.UP:
                    assert y <= 1e-6
                else:
                    assert y >= -1e-6


def test_rotation_by_four_quarters_returns_to_the_original() -> None:
    for key in ALL_KEYS:
        geometry = build_symbol(key)
        for terminal in geometry.terminals:
            x, y = terminal.x, terminal.y
            for _ in range(4):
                x, y = rotate_point(x, y, 90)
            assert (x, y) == pytest.approx((terminal.x, terminal.y))


def test_switch_states_differ_and_keep_the_same_terminals() -> None:
    """Положение аппарата меняет рисунок, но не электрические выводы."""
    for key in ("circuit_breaker", "disconnector", "recloser"):
        closed = build_symbol(key, opened=False)
        opened = build_symbol(key, opened=True)
        assert closed.primitives != opened.primitives, (
            f"состояние аппарата «{key}» не видно на рисунке"
        )
        assert closed.terminals == opened.terminals


def test_body_size_does_not_grow_with_the_bounding_box() -> None:
    """Тело аппарата постоянно: растягиваются только выводы.

    Именно это делает схему соразмерной — иначе растянутый по длине
    выключатель превращался бы в огромный квадрат.
    """
    narrow = build_symbol("circuit_breaker", width=64.0, height=32.0)
    wide = build_symbol("circuit_breaker", width=160.0, height=32.0)
    square_narrow = next(item for item in narrow.primitives if item.kind == "rect")
    square_wide = next(item for item in wide.primitives if item.kind == "rect")
    assert square_narrow.half_width == square_wide.half_width
    assert square_narrow.half_height == square_wide.half_height
    assert wide.terminal("b").x == pytest.approx(80.0)


def test_apparatus_bodies_are_dimensionally_consistent() -> None:
    """Тела разных аппаратов соразмерны друг другу."""
    breaker = next(
        item for item in build_symbol("circuit_breaker").primitives
        if item.kind == "rect"
    )
    coil = next(
        item for item in build_symbol("transformer_2w").primitives
        if item.kind == "circle"
    )
    machine = max(
        (item for item in build_symbol("generator").primitives if item.kind == "circle"),
        key=lambda item: item.radius,
    )
    assert breaker.half_width == pytest.approx(coil.radius)
    assert 1.0 < machine.radius / coil.radius <= 2.0


def test_line_widths_are_uniform_except_busbars() -> None:
    for key in ALL_KEYS:
        for item in build_symbol(key).primitives:
            if item.direction_marker:
                # Requested thin direction hints are decorations, not a
                # change in conductor/apparatus pen widths.
                assert key in {"line", "line_section"}
                assert item.stroke_scale == pytest.approx(0.8)
            elif key == "busbar":
                assert item.stroke_scale > 1.0
            else:
                assert item.stroke_scale == pytest.approx(1.0), (
                    f"символ «{key}» использует другую толщину линии"
                )


def test_canonical_key_maps_behaviour_and_legacy_keys() -> None:
    assert canonical_key("", "switch") == "circuit_breaker"
    assert canonical_key("disconnector", "switch") == "disconnector"
    assert canonical_key("line_section", "line_section") == "line_section"
    assert canonical_key("", "bus") == "busbar"
    assert canonical_key("", "electrical_node") == "connection_point"
    assert canonical_key("", "unknown-behaviour") == "generic"
    assert canonical_key("builtin.cable", "line") == "line_section"


def test_symbol_for_accepts_definition_style_arguments() -> None:
    geometry = symbol_for("recloser", "recloser", opened=True)
    assert geometry.key == "recloser"
    assert {item.role for item in geometry.terminals} == {"a", "b"}


def test_default_sizes_are_on_the_modular_grid() -> None:
    for key in ALL_KEYS:
        width, height = default_size(key)
        assert math.isclose(width % 8.0, 0.0, abs_tol=1e-9), key
        assert math.isclose(height % 8.0, 0.0, abs_tol=1e-9), key


def test_ink_bounds_stay_inside_the_declared_box() -> None:
    for key in ALL_KEYS:
        geometry = build_symbol(key)
        min_x, min_y, max_x, max_y = geometry.ink_bounds()
        assert min_x >= -geometry.width / 2.0 - 1e-6
        assert max_x <= geometry.width / 2.0 + 1e-6
        assert min_y >= -geometry.height / 2.0 - 1e-6
        assert max_y <= geometry.height / 2.0 + 1e-6


def test_svg_render_is_well_formed_for_every_symbol_and_angle() -> None:
    from xml.etree import ElementTree

    for key in ALL_KEYS:
        for angle in ANGLES:
            markup = symbol_svg(key, rotation=angle)
            root = ElementTree.fromstring(markup)
            assert root.tag.endswith("svg")
            assert len(list(root)) > 0


def test_invalid_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_symbol("circuit_breaker", width=0.0)
    with pytest.raises(ValueError):
        build_symbol("circuit_breaker", height=float("nan"))
