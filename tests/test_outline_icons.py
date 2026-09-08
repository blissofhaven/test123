"""The neutral glyph sheet stays vector, legible and separate from state."""
import os
from xml.etree import ElementTree as ET

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from rza_calc.gui.outline_icons import OUTLINE_GLYPHS, glyph_svg, outline_icon


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize('canvas', [False, True])
@pytest.mark.parametrize('dpr', [1.0, 2.0])
def test_complete_glyph_sheet_has_margin_no_fill_unique_shapes_at_both_dpis(canvas, dpr):
    assert len(OUTLINE_GLYPHS) == 24
    images = set()
    for key, spec in OUTLINE_GLYPHS.items():
        svg = ET.fromstring(glyph_svg(key, canvas=canvas))
        assert svg.attrib['viewBox'] == '0 0 16 16'
        group = next(item for item in svg if item.tag.endswith('g'))
        assert group.attrib['fill'] == 'none'
        assert group.attrib['stroke-width'] == ('1.5' if canvas else '1.25')
        assert spec.title
        assert all(element.attrib.get('fill', 'none') == 'none' for element in group.iter())
        icon = outline_icon(key, canvas)
        pixmap = icon.pixmap(QSize(16, 16), dpr)
        assert not pixmap.isNull()
        assert pixmap.devicePixelRatio() == dpr
        image = pixmap.toImage()
        assert image.width() == image.height() == round(16 * dpr)
        ink = [(x, y) for x in range(image.width()) for y in range(image.height())
               if image.pixelColor(x, y).alpha() > 0]
        assert ink
        margin = round(dpr)
        assert min(x for x, y in ink) >= margin
        assert min(y for x, y in ink) >= margin
        assert max(x for x, y in ink) < image.width() - margin
        assert max(y for x, y in ink) < image.height() - margin
        images.add(bytes(image.constBits()))
    assert len(images) == 24


def test_disabled_and_selected_remain_neutral_and_keep_geometry():
    icon = outline_icon('breaker')
    normal = icon.pixmap(QSize(32, 32), QIcon.Mode.Normal).toImage()
    selected = icon.pixmap(QSize(32, 32), QIcon.Mode.Selected).toImage()
    disabled = icon.pixmap(QSize(32, 32), QIcon.Mode.Disabled).toImage()
    assert normal == selected
    assert normal != disabled
    assert [[normal.pixelColor(x, y).alpha() for x in range(32)] for y in range(32)] == [
        [disabled.pixelColor(x, y).alpha() for x in range(32)] for y in range(32)]


def test_explicit_aliases_do_not_fabricate_unknown_model_support():
    for alias, key in [('gtes', 'power_station'), ('ru', 'switchyard'), ('busbar', 'bus_system'),
                       ('ps', 'substation'), ('ct', 'current_transformer'), ('dgr', 'arc_suppression_coil')]:
        assert glyph_svg(alias) == glyph_svg(key)
    assert outline_icon('unsupported_custom_equipment').isNull()
    with pytest.raises(KeyError):
        glyph_svg('unsupported_custom_equipment')
    with pytest.raises(ValueError):
        glyph_svg('load', color='not-a-color')


def test_navigation_glyphs_do_not_change_engineering_symbol_fill_and_terminals():
    from rza_calc.editor.symbols import build_symbol
    before = build_symbol('circuit_breaker', opened=False)
    glyph_svg('circuit_breaker', canvas=True)
    after = build_symbol('circuit_breaker', opened=False)
    assert after == before
    assert any(primitive.state_fill == 'closed' for primitive in after.primitives)
