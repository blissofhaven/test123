# -*- coding: utf-8 -*-
"""Библиотека условных графических обозначений оборудования.

Модуль намеренно не зависит от Qt и вообще от способа вывода: он описывает
ГЕОМЕТРИЮ символа в векторных примитивах, а рисование выполняют внешние
отрисовщики — Qt в редакторе схем и SVG в предпросмотре и документации. Один
источник истины исключает расхождение между тем, что видит пользователь, и
тем, где программа считает выводы аппарата.

Принципы построения, общие для всей библиотеки:

* **Единая модульная сетка.** Все размеры кратны :data:`UNIT` (4 единицы).
  Тело аппарата имеет ПОСТОЯННЫЙ размер и не растягивается вместе с рамкой
  габарита: меняется только длина выводов. Поэтому выключатель, разъединитель
  и трансформатор на одной схеме выглядят соразмерно, как на нормальной
  однолинейной схеме, а не «кто во что горазд».
* **Выводы — часть символа.** Каждый вывод описан точкой и наружным
  направлением. Точка подключения находится на конце вывода, а не на углу
  габаритного прямоугольника, поэтому соединительная линия приходит именно
  туда, куда ведёт нарисованный проводник.
* **Одинаковая толщина линий.** Толщина задаётся множителем к базовой
  толщине пера, а не абсолютным числом, поэтому масштаб схемы не ломает
  соотношение толщин. Жирными остаются только шины; условные стрелки
  направления тоньше проводника и не являются электрическими аппаратами.
* **Поворот кратен 90°.** Геометрия строится для 0°; повороты выполняет
  вызывающий код одним и тем же преобразованием и для рисунка, и для выводов,
  поэтому они не могут разъехаться.

Модуль ничего не знает об электрической модели: состояние аппарата (включён
или отключён) передаётся ему уже разрешённым.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Literal, Mapping

from .orthogonal_routing import RouteDirection

#: Базовый модуль сетки. Все характерные размеры кратны ему.
UNIT = 4.0

#: Половина стандартного тела двухполюсного аппарата.
BODY = 3.0 * UNIT          # 12 — половина квадрата выключателя 24×24
COIL_RADIUS = 3.0 * UNIT   # 12 — радиус обмотки трансформатора
ROUND_RADIUS = 5.0 * UNIT  # 20 — радиус машины (генератор, система)


class DiagramColorMode(StrEnum):
    """Режим цветового кодирования электрической схемы."""

    COLOR = "color"
    MONOCHROME = "monochrome"


# Цвета хранятся здесь, в Qt-независимом модуле обозначений: редактор, вкладка
# анализа и SVG используют один источник и не импортируют друг друга.
VOLTAGE_STROKES_BY_NOMINAL_V: Mapping[int, str] = MappingProxyType(
    {
        220_000: "#7A1638",
        110_000: "#DB4437",
        35_000: "#C00000",
        10_000: "#002060",
        6_000: "#009B83",
        400: "#BF9000",
    }
)

DIAGRAM_NEUTRAL_STROKE = "#172033"
DIAGRAM_DEENERGIZED_STROKE = "#A8B1BF"
DIAGRAM_OUT_OF_SERVICE_STROKE = "#7C8799"
DIAGRAM_MONOCHROME_STROKE = "#172033"

# Заливка показывает только механическое положение аппарата, а не наличие
# напряжения. Цвета взяты из согласованного образца Visio; остальные классы
# напряжения выше сохранены без изменений, так как в образце их нет.
SWITCH_OPEN_FILL = "#92D050"
SWITCH_CLOSED_FILL = "#E5B9B5"
SwitchFillState = Literal["open", "closed"]


def switch_state_fill(
    state: SwitchFillState,
    *,
    color_mode: DiagramColorMode | str = DiagramColorMode.COLOR,
) -> str:
    """Независимая от напряжения заливка положения выключателя.

    В монохроме положение по-прежнему читается по ориентации внутренней
    полоски: белая/светло-серая заливка не заменяет этот геометрический знак.
    """
    if state not in {"open", "closed"}:
        raise ValueError(f"Неизвестное положение выключателя: {state!r}")
    mode = (
        color_mode
        if isinstance(color_mode, DiagramColorMode)
        else DiagramColorMode(str(color_mode))
    )
    if mode is DiagramColorMode.MONOCHROME:
        return "#FFFFFF" if state == "open" else "#E5E7EB"
    return SWITCH_OPEN_FILL if state == "open" else SWITCH_CLOSED_FILL


def voltage_stroke(
    nominal_voltage_v: int | None,
    *,
    color_mode: DiagramColorMode | str = DiagramColorMode.COLOR,
) -> str:
    """Вернуть цвет точного класса напряжения без догадок по диапазону.

    Неизвестный класс остаётся нейтральным. В монохромном режиме известные
    классы получают единый печатный цвет; состояние и положение оборудования
    должны оставаться различимы геометрией и стилем линии, а не цветом.
    """

    mode = (
        color_mode
        if isinstance(color_mode, DiagramColorMode)
        else DiagramColorMode(str(color_mode))
    )
    resolved = VOLTAGE_STROKES_BY_NOMINAL_V.get(nominal_voltage_v)
    if resolved is None:
        return DIAGRAM_NEUTRAL_STROKE
    if mode is DiagramColorMode.MONOCHROME:
        return DIAGRAM_MONOCHROME_STROKE
    return resolved

PrimitiveKind = Literal[
    "line", "polyline", "polygon", "circle", "arc", "rect", "text"
]


@dataclass(frozen=True, slots=True)
class SymbolPrimitive:
    """Один векторный примитив в системе координат символа.

    Начало координат — центр символа, ось X вправо, ось Y вниз (экранная
    система Qt). Углы дуг задаются в градусах против часовой стрелки от оси
    +X, как принято в математике; отрисовщики переводят их в своё соглашение.
    """

    kind: PrimitiveKind
    points: tuple[tuple[float, float], ...] = ()
    center: tuple[float, float] = (0.0, 0.0)
    radius: float = 0.0
    radius_y: float = 0.0
    start_angle: float = 0.0
    span_angle: float = 0.0
    half_width: float = 0.0
    half_height: float = 0.0
    corner_radius: float = 0.0
    filled: bool = False
    stroke_scale: float = 1.0
    text: str = ""
    font_size: float = 12.0
    #: Пометка внутри символа (знак рода тока, буква) не поворачивается вместе
    #: с аппаратом: на схемах её всегда читают горизонтально, как подпись.
    #: Точка привязки при этом поворачивается, поэтому пометка остаётся на
    #: своём месте внутри повёрнутого символа.
    rotates: bool = True
    #: Смысловой вывод, напряжением которого окрашивается только этот
    #: проводящий примитив. ``None`` оставляет контур аппарата нейтральным.
    #: ``body`` — контур/полоска выключателя: номинальный цвет сети без
    #: переокраски по наличию напряжения на его выводах.
    voltage_role: str | None = None
    #: Явная заливка положения имеет приоритет над старым ``filled``;
    #: никогда не должна наследовать цвет пера или электрического состояния.
    state_fill: SwitchFillState | None = None
    #: Read-only beginning-to-end marker, not a conductor, contact or measured
    #: current. Renderers exclude this decoration from electrical crossings.
    direction_marker: bool = False

    def bounds(self) -> tuple[float, float, float, float]:
        """Габарит примитива (min_x, min_y, max_x, max_y)."""
        if self.kind in {"line", "polyline", "polygon"}:
            xs = [point[0] for point in self.points]
            ys = [point[1] for point in self.points]
            return min(xs), min(ys), max(xs), max(ys)
        if self.kind == "circle":
            cx, cy = self.center
            r = self.radius
            return cx - r, cy - r, cx + r, cy + r
        if self.kind == "arc":
            cx, cy = self.center
            r = self.radius
            ry = self.radius_y or self.radius
            return cx - r, cy - ry, cx + r, cy + ry
        if self.kind == "rect":
            cx, cy = self.center
            return (
                cx - self.half_width,
                cy - self.half_height,
                cx + self.half_width,
                cy + self.half_height,
            )
        cx, cy = self.center
        half = self.font_size * 0.62
        return cx - half, cy - half, cx + half, cy + half


@dataclass(frozen=True, slots=True)
class SymbolTerminal:
    """Электрический вывод символа и его наружное направление."""

    role: str
    x: float
    y: float
    direction: RouteDirection


@dataclass(frozen=True, slots=True)
class SymbolGeometry:
    """Полное описание одного условного обозначения при угле 0°."""

    key: str
    title: str
    width: float
    height: float
    primitives: tuple[SymbolPrimitive, ...] = ()
    terminals: tuple[SymbolTerminal, ...] = ()

    def terminal(self, role: str) -> SymbolTerminal | None:
        for item in self.terminals:
            if item.role == role:
                return item
        return None

    def ink_bounds(self) -> tuple[float, float, float, float]:
        """Габарит фактически нарисованного, а не объявленной рамки.

        Используется для рамки выделения и области попадания курсора: рамка
        должна облегать сам аппарат, а не пустое место вокруг него.
        """
        if not self.primitives:
            half_w, half_h = self.width / 2.0, self.height / 2.0
            return -half_w, -half_h, half_w, half_h
        boxes = [item.bounds() for item in self.primitives]
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )


# ──────────────────────────────────────────────────────────────────────────
#  Канонические ключи и размеры
# ──────────────────────────────────────────────────────────────────────────
#: Ключ символа → (ширина, высота) габаритной рамки по умолчанию.
DEFAULT_SIZES: Mapping[str, tuple[float, float]] = {
    "busbar": (160.0, 16.0),
    "connection_point": (16.0, 16.0),
    "line": (128.0, 24.0),
    "line_section": (128.0, 24.0),
    "circuit_breaker": (64.0, 32.0),
    "disconnector": (64.0, 32.0),
    "recloser": (64.0, 48.0),
    "transformer_2w": (72.0, 48.0),
    "transformer_3w": (72.0, 72.0),
    "generator": (56.0, 56.0),
    "source": (56.0, 56.0),
    #  Треугольник нагрузки уменьшен 31.08.2026 по замечанию заказчика: при
    #  40×48 он был самым крупным объектом схемы и перетягивал внимание вниз.
    #  Просили «примерно 26×30»; взято 24×32 — ближайший размер, лежащий на
    #  модульной сетке 8 единиц, которую проверяет
    #  test_default_sizes_are_on_the_modular_grid.
    "load": (24.0, 32.0),
}

_BEHAVIOR_TO_KEY: Mapping[str, str] = {
    "bus": "busbar",
    "line": "line",
    "line_section": "line_section",
    "switch": "circuit_breaker",
    "recloser": "recloser",
    "transformer_2w": "transformer_2w",
    "transformer_3w": "transformer_3w",
    "generator": "generator",
    "source": "source",
    "load": "load",
    "electrical_node": "connection_point",
    "legacy.source": "source",
    "legacy.generator": "generator",
    "legacy.line": "line_section",
    "legacy.branch": "line_section",
    "legacy.transformer_2w": "transformer_2w",
    "legacy.transformer_3w": "transformer_3w",
    "legacy.tie": "circuit_breaker",
    "legacy.load": "load",
}


def canonical_key(symbol_key: str | None, behavior_key: str | None) -> str:
    """Свести ключ символа и поведение типа к одному ключу библиотеки."""
    raw = (symbol_key or "").strip().casefold()
    behavior = (behavior_key or "").strip().casefold()
    if raw in DEFAULT_SIZES:
        return raw
    for candidate in (
        "connection_point",
        "busbar",
        "line_section",
        "transformer_3w",
        "transformer_2w",
        "circuit_breaker",
        "disconnector",
        "recloser",
        "generator",
        "source",
        "load",
    ):
        if candidate in raw:
            return candidate
    if "breaker" in raw or "switch" in raw:
        return "circuit_breaker"
    if "cable" in raw or "overhead" in raw or "busduct" in raw or "line" in raw:
        return "line_section"
    mapped = _BEHAVIOR_TO_KEY.get(behavior)
    if mapped is not None:
        return mapped
    return "generic"


def default_size(key: str) -> tuple[float, float]:
    return DEFAULT_SIZES.get(key, (64.0, 40.0))


# ──────────────────────────────────────────────────────────────────────────
#  Вспомогательные построители
# ──────────────────────────────────────────────────────────────────────────
def _line(x1: float, y1: float, x2: float, y2: float,
          *, stroke_scale: float = 1.0,
          voltage_role: str | None = None) -> SymbolPrimitive:
    return SymbolPrimitive(
        "line", points=((x1, y1), (x2, y2)), stroke_scale=stroke_scale,
        voltage_role=voltage_role,
    )


def _polyline(points: Iterable[tuple[float, float]],
              *, stroke_scale: float = 1.0) -> SymbolPrimitive:
    return SymbolPrimitive("polyline", points=tuple(points), stroke_scale=stroke_scale)


def _polygon(points: Iterable[tuple[float, float]], *, filled: bool = False
             ) -> SymbolPrimitive:
    return SymbolPrimitive("polygon", points=tuple(points), filled=filled)


def _circle(cx: float, cy: float, radius: float, *, filled: bool = False,
            voltage_role: str | None = None
            ) -> SymbolPrimitive:
    return SymbolPrimitive(
        "circle", center=(cx, cy), radius=radius, filled=filled,
        voltage_role=voltage_role,
    )


def _arc(cx: float, cy: float, radius: float, start: float, span: float
         ) -> SymbolPrimitive:
    return SymbolPrimitive(
        "arc", center=(cx, cy), radius=radius, radius_y=radius,
        start_angle=start, span_angle=span,
    )


def _rect(cx: float, cy: float, half_w: float, half_h: float,
          *, filled: bool = False, corner: float = 0.0,
          stroke_scale: float = 1.0,
          voltage_role: str | None = None,
          state_fill: SwitchFillState | None = None) -> SymbolPrimitive:
    return SymbolPrimitive(
        "rect", center=(cx, cy), half_width=half_w, half_height=half_h,
        filled=filled, corner_radius=corner, stroke_scale=stroke_scale, voltage_role=voltage_role,
        state_fill=state_fill,
    )


def _text(cx: float, cy: float, value: str, size: float = 15.0) -> SymbolPrimitive:
    return SymbolPrimitive("text", center=(cx, cy), text=value, font_size=size)


def _horizontal_leads(
    half_w: float,
    body_half: float,
    roles: tuple[str, str],
) -> list[SymbolPrimitive]:
    """Выводы влево и вправо от тела аппарата до границы габарита."""
    leads: list[SymbolPrimitive] = []
    if half_w > body_half:
        leads.append(
            _line(-half_w, 0.0, -body_half, 0.0, voltage_role=roles[0])
        )
        leads.append(
            _line(body_half, 0.0, half_w, 0.0, voltage_role=roles[1])
        )
    return leads


def _two_pole_terminals(half_w: float, roles: tuple[str, str]) -> tuple[SymbolTerminal, ...]:
    return (
        SymbolTerminal(roles[0], -half_w, 0.0, RouteDirection.LEFT),
        SymbolTerminal(roles[1], half_w, 0.0, RouteDirection.RIGHT),
    )


# ──────────────────────────────────────────────────────────────────────────
#  Символы аппаратов
# ──────────────────────────────────────────────────────────────────────────
def _busbar(width: float, height: float) -> SymbolGeometry:
    """Original thin bus stroke; selection size is not electrical ink."""
    vertical = height > width
    if vertical:
        half = height / 2.0
        body = _line(
            0.0, -half, 0.0, half,
            stroke_scale=2.25, voltage_role="terminal",
        )
        terminal = SymbolTerminal("terminal", 0.0, 0.0, RouteDirection.RIGHT)
    else:
        half = width / 2.0
        body = _line(
            -half, 0.0, half, 0.0,
            stroke_scale=2.25, voltage_role="terminal",
        )
        terminal = SymbolTerminal("terminal", 0.0, 0.0, RouteDirection.DOWN)
    return SymbolGeometry("busbar", "Шина", width, height, (body,), (terminal,))


def _connection_point(width: float, height: float) -> SymbolGeometry:
    radius = max(3.0, min(width, height) * 0.28)
    return SymbolGeometry(
        "connection_point", "Электрический узел", width, height,
        (_circle(
            0.0, 0.0, radius, filled=True, voltage_role="terminal",
        ),),
        (SymbolTerminal("terminal", 0.0, 0.0, RouteDirection.RIGHT),),
    )


def line_direction_marker(
    first: tuple[float, float], last: tuple[float, float], *, reverse: bool = False,
) -> SymbolPrimitive | None:
    """Thin central chevron for a visible straight segment, never a current.

    The caller resolves from/to roles; storage order alone is not authoritative.
    Twelve scene units leave a two-unit margin around the eight-unit arrow.
    This presentation helper does not change endpoints or infer connections.
    """
    if not all(math.isfinite(value) for value in (*first, *last)):
        return None
    dx, dy = last[0] - first[0], last[1] - first[1]
    length = math.hypot(dx, dy)
    if length < 12.0:
        return None
    sign = -1.0 if reverse else 1.0
    tx, ty = sign * dx / length, sign * dy / length
    cx, cy = (first[0] + last[0]) / 2.0, (first[1] + last[1]) / 2.0
    bx, by = cx - 4.0 * tx, cy - 4.0 * ty
    return SymbolPrimitive(
        "polyline",
        points=((bx - 2.8 * ty, by + 2.8 * tx),
                (cx + 4.0 * tx, cy + 4.0 * ty),
                (bx + 2.8 * ty, by - 2.8 * tx)),
        stroke_scale=0.8, voltage_role="from", direction_marker=True,
    )


def _line_symbol(key: str, title: str, width: float, height: float) -> SymbolGeometry:
    """Линия с направлением начала → конца; состав и выводы не меняются."""
    half_w = width / 2.0
    parts: list[SymbolPrimitive] = [
        _line(-half_w, 0.0, half_w, 0.0, voltage_role="from")
    ]
    marker = line_direction_marker((-half_w, 0.0), (half_w, 0.0))
    if marker is not None:
        parts.append(marker)
    return SymbolGeometry(
        key, title, width, height, tuple(parts),
        _two_pole_terminals(half_w, ("from", "to")),
    )


def _breaker_body(body: float, *, opened: bool) -> list[SymbolPrimitive]:
    """Рамка, независимая заливка и полоска положения по образцу Visio.

    Базовая ось выводов горизонтальна. При повороте вместе с символом
    полоска остаётся вдоль проводника во включённом положении и поперёк —
    в отключённом. Она не соединяет выводы геометрически или электрически.
    """
    half_bar = body * 0.5
    bar = (
        _line(0.0, -half_bar, 0.0, half_bar, voltage_role="body")
        if opened
        else _line(-half_bar, 0.0, half_bar, 0.0, voltage_role="body")
    )
    return [
        _rect(
            0.0, 0.0, body, body, voltage_role="body",
            state_fill="open" if opened else "closed",
        ),
        bar,
    ]


def _circuit_breaker(width: float, height: float, *, opened: bool) -> SymbolGeometry:
    """Выключатель — квадрат с полоской вдоль/поперёк проводника."""
    half_w = width / 2.0
    body = min(BODY, height / 2.0)
    parts = _horizontal_leads(half_w, body, ("a", "b"))
    parts.extend(_breaker_body(body, opened=opened))
    return SymbolGeometry(
        "circuit_breaker", "Выключатель", width, height, tuple(parts),
        _two_pole_terminals(half_w, ("a", "b")),
    )


def _disconnector(width: float, height: float, *, opened: bool) -> SymbolGeometry:
    """Разъединитель — контакты и нож с поперечной чертой на конце.

    Поперечная черта у неподвижного контакта — признак разъединителя по
    ГОСТ 2.755; именно она отличает его от выключателя нагрузки и от
    обычного контакта.
    """
    half_w = width / 2.0
    gap = 3.0 * UNIT
    tick = min(2.25 * UNIT, height / 2.0 - UNIT * 0.5)
    parts = _horizontal_leads(half_w, gap, ("a", "b"))
    # Обе контактные площадки показаны поперечными чертами: именно они, а не
    # наклон ножа, отличают разъединитель от простого проводника, когда он
    # включён и нож лежит на оси.
    parts.append(_line(-gap, -tick, -gap, tick))
    parts.append(_line(gap, -tick, gap, tick))
    if opened:
        parts.append(_line(-gap, 0.0, gap * 0.55, -tick * 1.55))
    else:
        parts.append(_line(-gap, 0.0, gap, 0.0))
    return SymbolGeometry(
        "disconnector", "Разъединитель", width, height, tuple(parts),
        _two_pole_terminals(half_w, ("a", "b")),
    )


def _recloser(width: float, height: float, *, opened: bool) -> SymbolGeometry:
    """Реклоузер — выключатель с дугой автоматического повторного включения."""
    half_w = width / 2.0
    body = min(BODY, height / 2.0 - 2.0 * UNIT)
    parts = _horizontal_leads(half_w, body, ("a", "b"))
    parts.extend(_breaker_body(body, opened=opened))
    # Дуга АПВ вынесена НАД корпусом с зазором, иначе она читается как часть
    # контура выключателя, а не как отдельный признак повторного включения.
    # Радиус подобран так, чтобы дуга оставалась внутри объявленного габарита.
    arc_center_y = -body - UNIT * 0.5
    arc_radius = min(body * 0.8, height / 2.0 + arc_center_y)
    if arc_radius > 0.0:
        parts.append(_arc(0.0, arc_center_y, arc_radius, 25.0, 130.0))
    return SymbolGeometry(
        "recloser", "Реклоузер", width, height, tuple(parts),
        _two_pole_terminals(half_w, ("a", "b")),
    )


def _transformer_2w(width: float, height: float) -> SymbolGeometry:
    """Двухобмоточный трансформатор — две пересекающиеся обмотки."""
    half_w = width / 2.0
    radius = min(COIL_RADIUS, height / 2.0 - UNIT * 0.5)
    offset = radius * 0.62
    body_half = offset + radius
    parts = _horizontal_leads(half_w, body_half, ("hv", "lv"))
    parts.append(_circle(-offset, 0.0, radius, voltage_role="hv"))
    parts.append(_circle(offset, 0.0, radius, voltage_role="lv"))
    return SymbolGeometry(
        "transformer_2w", "Двухобмоточный трансформатор", width, height,
        tuple(parts),
        (
            SymbolTerminal("hv", -half_w, 0.0, RouteDirection.LEFT),
            SymbolTerminal("lv", half_w, 0.0, RouteDirection.RIGHT),
        ),
    )


def _transformer_3w(width: float, height: float) -> SymbolGeometry:
    """Трёхобмоточный трансформатор — три обмотки треугольником.

    ВН слева, СН справа, НН снизу: такое расположение читается одинаково и на
    схеме подстанции, и после поворота на 90°, а вывод НН не пересекает две
    верхние обмотки.
    """
    half_w, half_h = width / 2.0, height / 2.0
    radius = min(COIL_RADIUS, half_h * 0.42, half_w * 0.42)
    offset = radius * 0.62
    top_y = -radius * 0.52
    bottom_y = radius * 0.52 + radius * 0.30
    parts = [
        _circle(-offset, top_y, radius, voltage_role="hv"),
        _circle(offset, top_y, radius, voltage_role="mv"),
        _circle(0.0, bottom_y, radius, voltage_role="lv"),
    ]
    body_half = offset + radius
    if half_w > body_half:
        parts.append(_line(
            -half_w, top_y, -body_half, top_y, voltage_role="hv",
        ))
        parts.append(_line(
            body_half, top_y, half_w, top_y, voltage_role="mv",
        ))
    if half_h > bottom_y + radius:
        parts.append(_line(
            0.0, bottom_y + radius, 0.0, half_h, voltage_role="lv",
        ))
    return SymbolGeometry(
        "transformer_3w", "Трёхобмоточный трансформатор", width, height,
        tuple(parts),
        (
            SymbolTerminal("hv", -half_w, top_y, RouteDirection.LEFT),
            SymbolTerminal("mv", half_w, top_y, RouteDirection.RIGHT),
            SymbolTerminal("lv", 0.0, half_h, RouteDirection.DOWN),
        ),
    )


def _sine(radius: float, *, at_y: float = 0.0) -> SymbolPrimitive:
    """Знак переменного тока внутри окружности машины."""
    amplitude = radius * 0.30
    span = radius * 0.58
    steps = 16
    points = []
    for index in range(steps + 1):
        t = index / steps
        x = -span + 2.0 * span * t
        y = at_y - amplitude * math.sin(2.0 * math.pi * t)
        points.append((x, y))
    return SymbolPrimitive("polyline", points=tuple(points), rotates=False)


def _machine(key: str, title: str, *, external: bool,
             width: float, height: float) -> SymbolGeometry:
    """Круг машины с выводом вниз.

    Генератор — одиночная окружность со знаком переменного тока. Внешняя
    энергосистема получает вторую, наружную окружность: это принятый способ
    показать, что источник находится за границей рассматриваемой схемы, и он
    читается даже при сильном уменьшении, когда буквенная пометка сливается.
    Шрифт не используется намеренно — обозначение должно выглядеть одинаково
    в редакторе, в SVG и в печати, независимо от установленных шрифтов.
    """
    half_h = height / 2.0
    radius = min(ROUND_RADIUS, width / 2.0, half_h - UNIT * 0.5)
    inner = radius * 0.74 if external else radius
    parts: list[SymbolPrimitive] = [_circle(0.0, 0.0, radius)]
    if external:
        parts.append(_circle(0.0, 0.0, inner))
    parts.append(_sine(inner))
    if half_h > radius:
        parts.append(_line(
            0.0, radius, 0.0, half_h, voltage_role="terminal",
        ))
    return SymbolGeometry(
        key, title, width, height, tuple(parts),
        (SymbolTerminal("terminal", 0.0, half_h, RouteDirection.DOWN),),
    )


def _load(width: float, height: float) -> SymbolGeometry:
    """Нагрузка — залитая стрелка отбора мощности, вывод сверху."""
    half_w, half_h = width / 2.0, height / 2.0
    span = min(half_w, 5.0 * UNIT)
    tip = half_h
    base = half_h - span * 1.35
    parts = [
        _polygon(((-span, base), (span, base), (0.0, tip)), filled=True),
    ]
    if base > -half_h:
        parts.append(_line(
            0.0, -half_h, 0.0, base, voltage_role="terminal",
        ))
    return SymbolGeometry(
        "load", "Нагрузка", width, height, tuple(parts),
        (SymbolTerminal("terminal", 0.0, -half_h, RouteDirection.UP),),
    )


def _generic(width: float, height: float) -> SymbolGeometry:
    half_w, half_h = width / 2.0, height / 2.0
    return SymbolGeometry(
        "generic", "Оборудование", width, height,
        (_rect(0.0, 0.0, half_w, half_h, corner=UNIT),),
        _two_pole_terminals(half_w, ("a", "b")),
    )


def build_symbol(
    key: str,
    *,
    width: float | None = None,
    height: float | None = None,
    opened: bool = False,
) -> SymbolGeometry:
    """Построить геометрию символа по каноническому ключу.

    ``opened`` учитывается только коммутационными аппаратами; остальные
    символы его игнорируют, поэтому вызывающему коду не нужно знать, у кого
    есть положение, а у кого нет.
    """
    normalized = key if key in DEFAULT_SIZES or key == "generic" else canonical_key(key, None)
    size_w, size_h = default_size(normalized)
    # Ноль и отрицательные габариты должны отклоняться, а не подменяться
    # значением по умолчанию: иначе повреждённые данные проекта дали бы
    # правдоподобный символ вместо явной ошибки.
    w = size_w if width is None else float(width)
    h = size_h if height is None else float(height)
    if not (math.isfinite(w) and math.isfinite(h)) or w <= 0 or h <= 0:
        raise ValueError("Габариты символа должны быть конечными и положительными.")

    if normalized == "busbar":
        return _busbar(w, h)
    if normalized == "connection_point":
        return _connection_point(w, h)
    if normalized == "line":
        return _line_symbol("line", "Линия", w, h)
    if normalized == "line_section":
        return _line_symbol("line_section", "Участок линии", w, h)
    if normalized == "circuit_breaker":
        return _circuit_breaker(w, h, opened=opened)
    if normalized == "disconnector":
        return _disconnector(w, h, opened=opened)
    if normalized == "recloser":
        return _recloser(w, h, opened=opened)
    if normalized == "transformer_2w":
        return _transformer_2w(w, h)
    if normalized == "transformer_3w":
        return _transformer_3w(w, h)
    if normalized == "generator":
        return _machine("generator", "Генератор", external=False, width=w, height=h)
    if normalized == "source":
        return _machine("source", "Энергосистема", external=True, width=w, height=h)
    if normalized == "load":
        return _load(w, h)
    return _generic(w, h)


def symbol_for(
    symbol_key: str | None,
    behavior_key: str | None,
    *,
    width: float | None = None,
    height: float | None = None,
    opened: bool = False,
) -> SymbolGeometry:
    """Построить символ по ключу представления и поведению типа."""
    return build_symbol(
        canonical_key(symbol_key, behavior_key),
        width=width,
        height=height,
        opened=opened,
    )


__all__ = [
    "BODY",
    "COIL_RADIUS",
    "DEFAULT_SIZES",
    "DIAGRAM_DEENERGIZED_STROKE",
    "DIAGRAM_MONOCHROME_STROKE",
    "DIAGRAM_NEUTRAL_STROKE",
    "DIAGRAM_OUT_OF_SERVICE_STROKE",
    "DiagramColorMode",
    "ROUND_RADIUS",
    "SWITCH_CLOSED_FILL",
    "SWITCH_OPEN_FILL",
    "SwitchFillState",
    "UNIT",
    "VOLTAGE_STROKES_BY_NOMINAL_V",
    "SymbolGeometry",
    "SymbolPrimitive",
    "SymbolTerminal",
    "build_symbol",
    "canonical_key",
    "default_size",
    "line_direction_marker",
    "symbol_for",
    "switch_state_fill",
    "voltage_stroke",
]
