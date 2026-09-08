"""Neutral navigation glyphs, independent of electrical symbols and state.

Logical size is 16 px. All ink stays inside a 1 px margin, with no fill.
Availability of a glyph does not imply support for that equipment in the solver.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from types import MappingProxyType

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


@dataclass(frozen=True, slots=True)
class OutlineGlyph:
    key: str
    title: str
    body: str


# Hand-drawn vectors matching the agreed icon sheet; no font glyphs/bitmaps.
_GLYPHS = (
    OutlineGlyph('power_station', 'ГТЭС / станция',
        '<circle cx="8" cy="5.8" r="3.5"/><path d="M6 5.8 Q7 3.8 8 5.8 T10 5.8 M8 9.3 V12.7 M1.8 12.7 H14.2"/>'),
    OutlineGlyph('generator', 'Генератор',
        '<circle cx="8" cy="9" r="4.3"/><path d="M8 1.8 V4.7 M5.7 9 Q6.85 6.7 8 9 T10.3 9"/>'),
    OutlineGlyph('switchyard', 'ОРУ / РУ',
        '<path d="M1.8 3.3 H14.2 M3.3 3.3 V5.8 M8 3.3 V5.8 M12.7 3.3 V5.8 M3.3 9 V13.5 M8 9 V13.5 M12.7 9 V13.5"/>'
        '<path d="M1.9 5.8 H4.7 V9 H1.9 Z M6.6 5.8 H9.4 V9 H6.6 Z M11.3 5.8 H14.1 V9 H11.3 Z"/>'),
    OutlineGlyph('bus_system', 'Системы шин',
        '<path d="M1.8 5 H14.2 M1.8 11 H14.2 M4.6 5 V11 M11.4 5 V11"/>'),
    OutlineGlyph('bus_section_breaker', 'Секционный выключатель',
        '<path d="M1.8 4 V12 M14.2 4 V12 M1.8 8 H5.5 M10.5 8 H14.2"/>'
        '<rect x="5.5" y="5.5" width="5" height="5"/>'),
    OutlineGlyph('transformer_2w', 'Трансформатор 2-обм.',
        '<path d="M8 1.8 V3.5"/><circle cx="8" cy="6.8" r="3.3"/><circle cx="8" cy="11" r="3.2"/>'),
    OutlineGlyph('transformer_3w', 'Трансформатор 3-обм.',
        '<circle cx="8" cy="4.9" r="2.9"/><circle cx="5.1" cy="10.6" r="2.9"/><circle cx="10.9" cy="10.6" r="2.9"/>'),
    OutlineGlyph('autotransformer', 'Автотрансформатор',
        '<path d="M8 1.8 V14.1"/><circle cx="8" cy="6.8" r="3.3"/><circle cx="8" cy="11" r="3.2"/>'),
    OutlineGlyph('substation', 'Подстанция',
        '<rect x="1.8" y="2.5" width="12.4" height="11" rx="1.7"/>'
        '<circle cx="6.3" cy="8" r="2.6"/><circle cx="9.7" cy="8" r="2.6"/>'),
    OutlineGlyph('ktp', 'КТП',
        '<rect x="2.6" y="1.9" width="10.8" height="8.1" rx="1"/>'
        '<circle cx="6.5" cy="5.9" r="2.1"/><circle cx="9.5" cy="5.9" r="2.1"/>'
        '<path d="M8 10 V14.1 M6.1 12.2 L8 14.1 L9.9 12.2"/>'),
    OutlineGlyph('feeder', 'Фидер',
        '<path d="M2.2 2 H13.8 M8 2 V6 M8 10 V14.1 M6.1 12.2 L8 14.1 L9.9 12.2"/>'
        '<rect x="6.2" y="6" width="3.6" height="4"/>'),
    OutlineGlyph('overhead_line', 'Воздушная линия',
        '<path d="M8 1.8 V14.2 M4.6 3.2 H11.4 M3.2 5.7 H12.8 M4.1 14.1 L8 10.2 L11.9 14.1"/>'),
    OutlineGlyph('cable_line', 'Кабельная линия',
        '<path d="M1.8 8 H14.2 M6 4.5 V11.5 M8 4.5 V11.5 M10 4.5 V11.5"/>'),
    OutlineGlyph('circuit_breaker', 'Выключатель',
        '<path d="M8 1.8 V5.6 M8 10.4 V14.2"/><rect x="5.7" y="5.6" width="4.6" height="4.8"/>'),
    OutlineGlyph('disconnector', 'Разъединитель',
        '<path d="M8 1.8 V5.4 L11.5 10.4 M5.7 10.4 H10.3 M8 10.4 V14.2"/>'),
    OutlineGlyph('earthing_switch', 'Заземляющий нож',
        '<path d="M8 1.8 V4.5 L11.5 8 M8 7.8 V10.5 M4.3 10.5 H11.7 M5.8 12.4 H10.2 M7 14.2 H9"/>'),
    OutlineGlyph('current_transformer', 'Трансформатор тока',
        '<path d="M8 1.8 V14.2 M11.2 8 H14.2"/><circle cx="8" cy="8" r="3.2"/>'),
    OutlineGlyph('voltage_transformer', 'Трансформатор напряжения',
        '<path d="M8 1.8 V3"/><circle cx="8" cy="6.3" r="3.3"/>'
        '<path d="M5.1 11 L8 14.2 L10.9 11 Z"/>'),
    OutlineGlyph('reactor', 'Реактор',
        '<path d="M8 1.8 V4.4 C13 4.4 13 11.6 8 11.6 V14.2"/>'),
    OutlineGlyph('relay_terminal', 'Терминал РЗА',
        '<rect x="2.3" y="3.5" width="11.4" height="10.7" rx="1.7"/>'
        '<path d="M4.5 1.8 V3.5 M11.5 1.8 V3.5 M4.8 6.9 H11.2 M4.8 10.1 H9.7"/>'),
    OutlineGlyph('load', 'Нагрузка',
        '<path d="M8 1.8 V14.2 M5.7 10.4 L8 14.2 L10.3 10.4"/>'),
    OutlineGlyph('motor', 'Двигатель',
        '<circle cx="8" cy="9" r="4.3"/><path d="M8 1.8 V4.7 M6 10.8 V7.2 L8 9.3 L10 7.2 V10.8"/>'),
    OutlineGlyph('arc_suppression_coil', 'Дугогасящий реактор',
        '<path d="M8 1.8 V4.2 C12.5 4.2 12.5 9.9 8 9.9 V10.6 M4.5 10.6 H11.5 M5.8 12.4 H10.2 M7 14.2 H9"/>'),
    OutlineGlyph('neutral_resistor', 'Резистор нейтрали',
        '<rect x="5.7" y="3.1" width="4.6" height="7.1"/>'
        '<path d="M8 1.8 V3.1 M8 10.2 V11.4 M4.5 11.4 H11.5 M5.8 12.8 H10.2 M7 14.2 H9"/>'),
)

OUTLINE_GLYPHS = MappingProxyType({glyph.key: glyph for glyph in _GLYPHS})
_ALIASES = {
    'station': 'power_station', 'gtes': 'power_station', 'power_plant': 'power_station',
    'ru': 'switchyard', 'oru': 'switchyard', 'switchgear': 'switchyard',
    'bus': 'bus_system', 'busbar': 'bus_system', 'bus_section': 'bus_system',
    'section_breaker': 'bus_section_breaker', 'bus_coupler': 'bus_section_breaker',
    'transformer': 'transformer_2w', 'transformer2w': 'transformer_2w',
    'transformer3w': 'transformer_3w', 'auto_transformer': 'autotransformer',
    'ps': 'substation', 'overhead': 'overhead_line', 'cable': 'cable_line',
    'breaker': 'circuit_breaker', 'grounding_switch': 'earthing_switch',
    'ct': 'current_transformer', 'vt': 'voltage_transformer',
    'relay': 'relay_terminal', 'dgr': 'arc_suppression_coil',
}


def glyph_svg(kind: str, *, canvas: bool = False, color: str = '#455468') -> str:
    """Standalone vector; unknown keys raise instead of inventing a symbol."""
    key = _ALIASES.get(kind, kind)
    glyph = OUTLINE_GLYPHS[key]
    colour = QColor(color)
    if not colour.isValid():
        raise ValueError('Некорректный цвет значка.')
    stroke = '1.5' if canvas else '1.25'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        f'<title>{escape(glyph.title)}</title>'
        f'<g fill="none" stroke="{colour.name()}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round">{glyph.body}</g></svg>'
    )


class _OutlineIconEngine(QIconEngine):
    def __init__(self, kind: str, canvas: bool):
        super().__init__()
        self.kind, self.canvas = kind, canvas
        self._normal = QSvgRenderer(QByteArray(glyph_svg(kind, canvas=canvas).encode('utf-8')))
        self._disabled = QSvgRenderer(QByteArray(glyph_svg(kind, canvas=canvas, color='#8B96A5').encode('utf-8')))

    def clone(self):
        return _OutlineIconEngine(self.kind, self.canvas)

    def paint(self, painter, rect, mode, state):
        del state
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = min(rect.width(), rect.height())
        target = QRectF(rect.x() + (rect.width() - size) / 2,
                        rect.y() + (rect.height() - size) / 2, size, size)
        renderer = self._disabled if mode == QIcon.Mode.Disabled else self._normal
        renderer.render(painter, target)
        painter.restore()

    def pixmap(self, size, mode, state):
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, pixmap.rect(), mode, state)
        painter.end()
        return pixmap

    def scaledPixmap(self, size, mode, state, scale):  # noqa: N802 - Qt virtual
        pixels = QSize(round(size.width() * scale), round(size.height() * scale))
        pixmap = self.pixmap(pixels, mode, state)
        pixmap.setDevicePixelRatio(scale)
        return pixmap


def outline_icon(kind: str, canvas: bool = False) -> QIcon:
    """Resolution-independent neutral icon. Unknown/unassigned types stay blank."""
    key = _ALIASES.get(kind, kind)
    return QIcon(_OutlineIconEngine(key, canvas)) if key in OUTLINE_GLYPHS else QIcon()


__all__ = ['OUTLINE_GLYPHS', 'OutlineGlyph', 'glyph_svg', 'outline_icon']
