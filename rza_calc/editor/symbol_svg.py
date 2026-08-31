# -*- coding: utf-8 -*-
"""SVG-отрисовщик библиотеки условных обозначений.

Служит двум целям: предпросмотр и документация вне Qt, а также независимая
проверка того, что редактор и векторный вывод рисуют ОДНУ И ТУ ЖЕ геометрию.
Оба отрисовщика получают одни и те же :class:`SymbolPrimitive`, поэтому
расхождение возможно только в самом отрисовщике, а не в описании символа.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from html import escape

from .orthogonal_routing import RouteDirection
from .symbols import (  # noqa: F401
    DIAGRAM_NEUTRAL_STROKE,
    DiagramColorMode,
    SymbolGeometry,
    SymbolPrimitive,
    build_symbol,
    switch_state_fill,
    voltage_stroke as resolve_voltage_stroke,
)

_TURN_MATRIX = {
    0: (1.0, 0.0, 0.0, 1.0),
    90: (0.0, -1.0, 1.0, 0.0),
    180: (-1.0, 0.0, 0.0, -1.0),
    270: (0.0, 1.0, -1.0, 0.0),
}


def _num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def rotate_point(x: float, y: float, rotation: int) -> tuple[float, float]:
    """Повернуть точку на угол, кратный 90°, вокруг центра символа."""
    a, b, c, d = _TURN_MATRIX[int(rotation) % 360]
    return a * x + b * y, c * x + d * y


def rotate_direction(direction: RouteDirection, rotation: int) -> RouteDirection:
    order = (
        RouteDirection.RIGHT,
        RouteDirection.DOWN,
        RouteDirection.LEFT,
        RouteDirection.UP,
    )
    steps = (int(rotation) % 360) // 90
    return order[(order.index(direction) + steps) % 4]


def _color_mode(value: DiagramColorMode | str) -> DiagramColorMode:
    return value if isinstance(value, DiagramColorMode) else DiagramColorMode(str(value))


def _conductive_stroke(
    role: str | None,
    *,
    neutral_stroke: str,
    voltage_stroke: str | None,
    terminal_strokes: Mapping[str, str] | None,
    color_mode: DiagramColorMode | str,
    body_stroke: str | None = None,
) -> str:
    """Разрешить цвет проводящей части через единый контракт палитры."""

    if role is None:
        return neutral_stroke
    mode = _color_mode(color_mode)
    if mode is DiagramColorMode.MONOCHROME:
        # Любой известный класс возвращает общий печатный цвет. Число здесь
        # не кодирует класс: оно только проводит запрос через общий resolver.
        return resolve_voltage_stroke(220_000, color_mode=mode)
    if role == "body" and body_stroke is not None:
        return body_stroke
    if terminal_strokes is not None:
        resolved = terminal_strokes.get(role)
        if resolved is not None:
            return resolved
    if voltage_stroke is not None:
        return voltage_stroke
    return resolve_voltage_stroke(None, color_mode=mode)


def primitive_svg(
    item: SymbolPrimitive,
    *,
    rotation: int = 0,
    stroke: str = DIAGRAM_NEUTRAL_STROKE,
    stroke_width: float = 2.0,
    voltage_stroke: str | None = None,
    terminal_strokes: Mapping[str, str] | None = None,
    color_mode: DiagramColorMode | str = DiagramColorMode.COLOR,
    body_stroke: str | None = None,
) -> str:
    """Перевести примитив в SVG, независимо разрешая перо и заливку.

    ``body_stroke`` — номинальный цвет корпуса/индикатора выключателя.
    Он не выводится из цветов ``a``/``b``: они могут быть приглушены
    расчётным электрическим состоянием каждого вывода.
    """
    if not item.rotates:
        # Пометка внутри символа остаётся горизонтальной; поворачивается
        # только её точка привязки, чтобы она не уехала из символа.
        anchor_x, anchor_y = rotate_point(*item.center, rotation)
        shift = (anchor_x - item.center[0], anchor_y - item.center[1])
        moved = SymbolPrimitive(
            item.kind,
            points=tuple((x + shift[0], y + shift[1]) for x, y in item.points),
            center=(anchor_x, anchor_y),
            radius=item.radius,
            radius_y=item.radius_y,
            start_angle=item.start_angle,
            span_angle=item.span_angle,
            half_width=item.half_width,
            half_height=item.half_height,
            corner_radius=item.corner_radius,
            filled=item.filled,
            stroke_scale=item.stroke_scale,
            text=item.text,
            font_size=item.font_size,
            voltage_role=item.voltage_role,
            state_fill=item.state_fill,
            direction_marker=item.direction_marker,
        )
        return primitive_svg(
            moved,
            rotation=0,
            stroke=stroke,
            stroke_width=stroke_width,
            voltage_stroke=voltage_stroke,
            terminal_strokes=terminal_strokes,
            color_mode=color_mode,
            body_stroke=body_stroke,
        )
    stroke = _conductive_stroke(
        item.voltage_role,
        neutral_stroke=stroke,
        voltage_stroke=voltage_stroke,
        terminal_strokes=terminal_strokes,
        color_mode=color_mode,
        body_stroke=body_stroke,
    )
    width = _num(stroke_width * item.stroke_scale)
    fill = (
        switch_state_fill(item.state_fill, color_mode=color_mode)
        if item.state_fill is not None
        else stroke if item.filled else "none"
    )
    common = (
        f'fill="{fill}" stroke="{stroke}" stroke-width="{width}" '
        'stroke-linecap="round" stroke-linejoin="round"'
    )

    if item.kind in {"line", "polyline", "polygon"}:
        points = [rotate_point(x, y, rotation) for x, y in item.points]
        data = " ".join(f"{_num(x)},{_num(y)}" for x, y in points)
        if item.kind == "polygon":
            return f'<polygon points="{data}" {common} />'
        stroke_only = (
            f'fill="none" stroke="{stroke}" stroke-width="{width}" '
            'stroke-linecap="round" stroke-linejoin="round"'
        )
        if item.kind == "polyline":
            return f'<polyline points="{data}" {stroke_only} />'
        return (f'<line x1="{_num(points[0][0])}" y1="{_num(points[0][1])}" '
                f'x2="{_num(points[1][0])}" y2="{_num(points[1][1])}" '
                f'{stroke_only} />')

    if item.kind == "circle":
        cx, cy = rotate_point(*item.center, rotation)
        return (f'<circle cx="{_num(cx)}" cy="{_num(cy)}" '
                f'r="{_num(item.radius)}" {common} />')

    if item.kind == "rect":
        cx, cy = rotate_point(*item.center, rotation)
        half_w, half_h = item.half_width, item.half_height
        if int(rotation) % 180 == 90:
            half_w, half_h = half_h, half_w
        corner = f' rx="{_num(item.corner_radius)}"' if item.corner_radius else ""
        return (f'<rect x="{_num(cx - half_w)}" y="{_num(cy - half_h)}" '
                f'width="{_num(half_w * 2)}" height="{_num(half_h * 2)}"'
                f'{corner} {common} />')

    if item.kind == "arc":
        cx, cy = rotate_point(*item.center, rotation)
        start = item.start_angle + int(rotation)
        span = item.span_angle
        radius = item.radius
        x1 = cx + radius * math.cos(math.radians(start))
        y1 = cy - radius * math.sin(math.radians(start))
        x2 = cx + radius * math.cos(math.radians(start + span))
        y2 = cy - radius * math.sin(math.radians(start + span))
        large = 1 if abs(span) > 180 else 0
        sweep = 0 if span > 0 else 1
        return (f'<path d="M {_num(x1)} {_num(y1)} A {_num(radius)} {_num(radius)} '
                f'0 {large} {sweep} {_num(x2)} {_num(y2)}" fill="none" '
                f'stroke="{stroke}" stroke-width="{width}" stroke-linecap="round" />')

    if item.kind == "text":
        cx, cy = rotate_point(*item.center, rotation)
        return (f'<text x="{_num(cx)}" y="{_num(cy)}" fill="{stroke}" '
                f'font-family="Arial, Helvetica, sans-serif" '
                f'font-size="{_num(item.font_size)}" text-anchor="middle" '
                f'dominant-baseline="central" stroke="none">'
                f'{escape(item.text)}</text>')
    return ""


def symbol_svg_body(
    geometry: SymbolGeometry,
    *,
    rotation: int = 0,
    stroke: str = DIAGRAM_NEUTRAL_STROKE,
    stroke_width: float = 2.0,
    show_terminals: bool = True,
    terminal_fill: str = "#FFFFFF",
    terminal_stroke: str = "#2563EB",
    voltage_stroke: str | None = None,
    terminal_strokes: Mapping[str, str] | None = None,
    color_mode: DiagramColorMode | str = DiagramColorMode.COLOR,
    body_stroke: str | None = None,
) -> str:
    """Собрать содержимое группы SVG для одного символа."""
    parts = [
        primitive_svg(
            item,
            rotation=rotation,
            stroke=stroke,
            stroke_width=stroke_width,
            voltage_stroke=voltage_stroke,
            terminal_strokes=terminal_strokes,
            color_mode=color_mode,
            body_stroke=body_stroke,
        )
        for item in geometry.primitives
    ]
    if show_terminals:
        for terminal in geometry.terminals:
            x, y = rotate_point(terminal.x, terminal.y, rotation)
            resolved_terminal_stroke = _conductive_stroke(
                terminal.role,
                neutral_stroke=terminal_stroke,
                voltage_stroke=voltage_stroke,
                terminal_strokes=terminal_strokes,
                color_mode=color_mode,
            )
            parts.append(
                f'<circle cx="{_num(x)}" cy="{_num(y)}" r="3.2" '
                f'fill="{terminal_fill}" stroke="{resolved_terminal_stroke}" '
                'stroke-width="1.3" />'
            )
    return "".join(parts)


def symbol_svg(
    key: str,
    *,
    rotation: int = 0,
    width: float | None = None,
    height: float | None = None,
    opened: bool = False,
    stroke: str = DIAGRAM_NEUTRAL_STROKE,
    stroke_width: float = 2.0,
    show_terminals: bool = True,
    padding: float = 10.0,
    voltage_stroke: str | None = None,
    terminal_strokes: Mapping[str, str] | None = None,
    color_mode: DiagramColorMode | str = DiagramColorMode.COLOR,
    body_stroke: str | None = None,
) -> str:
    """Отдельный самостоятельный SVG-документ одного символа."""
    geometry = build_symbol(key, width=width, height=height, opened=opened)
    half_w, half_h = geometry.width / 2.0, geometry.height / 2.0
    if int(rotation) % 180 == 90:
        half_w, half_h = half_h, half_w
    box_w = half_w * 2 + padding * 2
    box_h = half_h * 2 + padding * 2
    body = symbol_svg_body(
        geometry, rotation=rotation, stroke=stroke, stroke_width=stroke_width,
        show_terminals=show_terminals,
        voltage_stroke=voltage_stroke,
        terminal_strokes=terminal_strokes,
        color_mode=color_mode,
        body_stroke=body_stroke,
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_num(-box_w / 2)} '
        f'{_num(-box_h / 2)} {_num(box_w)} {_num(box_h)}" '
        f'width="{_num(box_w)}" height="{_num(box_h)}" role="img" '
        f'aria-label="{escape(geometry.title)}" data-symbol-key="{escape(key)}">'
        f"{body}</svg>"
    )


__all__ = [
    "primitive_svg",
    "rotate_direction",
    "rotate_point",
    "symbol_svg",
    "symbol_svg_body",
]
