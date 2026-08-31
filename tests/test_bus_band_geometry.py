"""Restored thin bus ink and independent display gaps never move connections."""
from xml.etree import ElementTree as ET

import pytest

from rza_calc.editor.orientation import bus_anchor_geometry
from rza_calc.editor.symbol_svg import primitive_svg
from rza_calc.editor.symbols import build_symbol, DiagramColorMode, DIAGRAM_MONOCHROME_STROKE
from rza_calc.editor.line_bridges import BridgeWire, build_wire_displays


@pytest.mark.parametrize("width,height", [(240., 12.), (12., 240.), (150., 22.), (22., 150.)])
def test_bus_restores_original_thin_stroke_and_keeps_length(width, height):
    symbol = build_symbol("busbar", width=width, height=height)
    axis, = symbol.primitives
    assert axis.kind == "line" and not axis.filled
    assert axis.voltage_role == "terminal"
    assert (symbol.width, symbol.height) == (width, height)
    assert axis.stroke_scale == 2.25
    left, top, right, bottom = symbol.ink_bounds()
    assert min(right-left, bottom-top) == 0, "No filled bus or duplicate conductor"
    assert axis.points == (((0, -height/2), (0, height/2)) if height > width
                           else ((-width/2, 0), (width/2, 0)))
    assert len(symbol.terminals) == 1
    assert symbol.terminals[0].x == symbol.terminals[0].y == 0


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("fraction", [0., .25, .5, .75, 1.])
def test_thicker_bus_does_not_move_any_fractional_attachment(rotation, fraction):
    before = bus_anchor_geometry(width=240, height=12, rotation=rotation,
                                 center_x=150, center_y=200, fraction=fraction)
    after = bus_anchor_geometry(width=240, height=22, rotation=rotation,
                                center_x=150, center_y=200, fraction=fraction)
    assert before == after


@pytest.mark.parametrize("mode", [DiagramColorMode.COLOR, DiagramColorMode.MONOCHROME])
def test_restored_bus_svg_is_one_thin_voltage_colored_stroke(mode):
    symbol = build_symbol("busbar", width=240, height=12)
    axis, = symbol.primitives
    stroke = ET.fromstring(primitive_svg(axis, voltage_stroke="#ff0000", color_mode=mode))
    assert stroke.tag == "line"
    assert float(stroke.attrib["stroke-width"]) == 4.5
    assert stroke.attrib["stroke"] == ("#ff0000" if mode is DiagramColorMode.COLOR else DIAGRAM_MONOCHROME_STROKE)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("transpose", [False, True])
def test_thin_wire_crossing_filled_bus_has_full_band_gap_without_joint(reverse, transpose):
    def points(values):
        values = tuple((y, x) if transpose else (x, y) for x, y in values)
        return values[::-1] if reverse else values
    bus = BridgeWire("bus", points([(-80, 0), (80, 0)]), "bus-node",
                     bridge_allowed=False, visual_half_width=11)
    wire = BridgeWire("wire", points([(0, -80), (0, 80)]), "other-node")
    display = build_wire_displays([bus, wire])
    assert display == build_wire_displays([wire, bus])
    assert not any(item.junctions or item.bridges for item in display.values())
    assert not display["bus"].gaps
    _, first, second = display["wire"].gaps[0]
    assert abs(second[0] - first[0]) + abs(second[1] - first[1]) == 27


def test_real_endpoint_t_on_filled_bus_still_connects_without_display_gap():
    result = build_wire_displays([
        BridgeWire("bus", ((-80, 0), (80, 0)), "node", bridge_allowed=False, visual_half_width=11),
        BridgeWire("wire", ((0, 0), (0, 80)), "node"),
    ])
    assert result["bus"].junctions == ((0, 0),)
    assert not any(item.gaps or item.bridges for item in result.values())


@pytest.mark.parametrize("padding", [0., 2.5, 9.])
def test_viewer_gap_padding_changes_only_the_independent_display_cut(padding):
    bus = BridgeWire("bus", ((-80, 0), (80, 0)), "bus", bridge_allowed=False,
                     visual_half_width=11, visual_gap_padding=padding)
    wire = BridgeWire("wire", ((0, -80), (0, 80)), "other")
    display = build_wire_displays([bus, wire])
    assert display["wire"].wire is wire
    assert display["bus"].wire is bus
    assert not any(value.junctions or value.bridges for value in display.values())
    assert display["wire"].gaps == ((0, (0, -11-padding), (0, 11+padding)),)


@pytest.mark.parametrize("padding", [-1., float("nan"), float("inf")])
def test_invalid_display_gap_padding_is_rejected(padding):
    with pytest.raises(ValueError, match="gap padding"):
        build_wire_displays([BridgeWire("bus", ((-80, 0), (80, 0)), visual_gap_padding=padding)])
