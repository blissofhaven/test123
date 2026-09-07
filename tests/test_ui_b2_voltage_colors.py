# -*- coding: utf-8 -*-
"""Acceptance checks for B2 voltage-class colouring.

This module is intentionally owned by the B-TEST track.  It checks visual
semantics only; electrical topology and calculation data are treated as
read-only inputs.
"""
from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rza_calc.editor.symbols import (  # noqa: E402
    DIAGRAM_DEENERGIZED_STROKE,
    DIAGRAM_MONOCHROME_STROKE,
    DIAGRAM_NEUTRAL_STROKE,
    DIAGRAM_OUT_OF_SERVICE_STROKE,
    VOLTAGE_STROKES_BY_NOMINAL_V,
    DiagramColorMode,
    build_symbol,
    canonical_key,
    voltage_stroke,
)
from rza_calc.editor import symbol_svg as symbol_svg_module  # noqa: E402
from rza_calc.editor.symbol_svg import symbol_svg  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tests" / "fixtures" / "legacy_projects" / "energoraion.json"


EXPECTED_VOLTAGE_STROKES = {
    220_000: "#7A1638",
    110_000: "#DB4437",
    35_000: "#C00000",
    10_000: "#002060",
    6_000: "#009B83",
    400: "#BF9000",
}

# A difference of 32 in 8-bit Rec. 709 luma remains visible when the scheme
# is printed or viewed in grayscale; the two mandatory pairs have more margin.
MIN_GRAYSCALE_LUMA_DELTA = 32.0

LEGACY_SYMBOLS = {
    "legacy.source": "source",
    "legacy.generator": "generator",
    "legacy.line": "line_section",
    "legacy.branch": "line_section",
    "legacy.transformer_2w": "transformer_2w",
    "legacy.transformer_3w": "transformer_3w",
    "legacy.tie": "circuit_breaker",
    "legacy.load": "load",
}


def _rec709_luma(hex_color: str) -> float:
    red, green, blue = (
        int(hex_color[index : index + 2], 16) for index in (1, 3, 5)
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def test_exact_voltage_palette_is_read_only_and_has_six_classes() -> None:
    assert dict(VOLTAGE_STROKES_BY_NOMINAL_V) == EXPECTED_VOLTAGE_STROKES
    assert len(VOLTAGE_STROKES_BY_NOMINAL_V) == 6
    with pytest.raises(TypeError):
        VOLTAGE_STROKES_BY_NOMINAL_V[20_000] = "#FFFFFF"  # type: ignore[index]


@pytest.mark.parametrize(
    ("first_voltage", "second_voltage"),
    ((220_000, 110_000), (10_000, 6_000)),
)
def test_required_voltage_pairs_remain_distinct_in_grayscale(
    first_voltage: int,
    second_voltage: int,
) -> None:
    delta = abs(
        _rec709_luma(VOLTAGE_STROKES_BY_NOMINAL_V[first_voltage])
        - _rec709_luma(VOLTAGE_STROKES_BY_NOMINAL_V[second_voltage])
    )
    assert delta >= MIN_GRAYSCALE_LUMA_DELTA


def test_voltage_resolver_uses_exact_class_and_safe_fallbacks() -> None:
    for nominal_voltage_v, expected in EXPECTED_VOLTAGE_STROKES.items():
        assert voltage_stroke(nominal_voltage_v) == expected
        assert voltage_stroke(
            nominal_voltage_v,
            color_mode=DiagramColorMode.MONOCHROME,
        ) == DIAGRAM_MONOCHROME_STROKE

    assert voltage_stroke(None) == DIAGRAM_NEUTRAL_STROKE
    assert voltage_stroke(20_000) == DIAGRAM_NEUTRAL_STROKE
    assert (
        voltage_stroke(20_000, color_mode="monochrome")
        == DIAGRAM_NEUTRAL_STROKE
    )


def test_svg_uses_the_exact_same_voltage_resolver_object() -> None:
    assert symbol_svg_module.resolve_voltage_stroke is voltage_stroke


@pytest.mark.parametrize(
    "symbol_key",
    (
        "busbar",
        "connection_point",
        "line",
        "line_section",
        "circuit_breaker",
        "disconnector",
        "recloser",
        "transformer_2w",
        "transformer_3w",
        "generator",
        "source",
        "load",
    ),
)
def test_every_electrical_symbol_marks_its_conductive_primitives(
    symbol_key: str,
) -> None:
    geometry = build_symbol(symbol_key)
    assert any(item.voltage_role is not None for item in geometry.primitives)


@pytest.mark.parametrize(
    "symbol_key",
    (
        "disconnector",
        "recloser",
        "generator",
        "source",
        "load",
    ),
)
def test_nonconductive_decorations_remain_neutral(symbol_key: str) -> None:
    geometry = build_symbol(symbol_key)
    assert any(item.voltage_role is None for item in geometry.primitives)


def test_visio_body_and_windings_have_explicit_semantic_color_roles() -> None:
    for key in ("circuit_breaker", "recloser"):
        geometry = build_symbol(key)
        body = next(item for item in geometry.primitives if item.kind == "rect")
        assert body.voltage_role == "body"
        assert body.state_fill == "closed"
        assert any(item.kind == "line" and item.voltage_role == "body" for item in geometry.primitives)
    for key, roles in (("transformer_2w", {"hv", "lv"}), ("transformer_3w", {"hv", "mv", "lv"})):
        geometry = build_symbol(key)
        assert {item.voltage_role for item in geometry.primitives if item.kind == "circle"} == roles


def test_svg_colors_transformer_leads_and_windings_by_terminal_role() -> None:
    hv_stroke = EXPECTED_VOLTAGE_STROKES[110_000]
    lv_stroke = EXPECTED_VOLTAGE_STROKES[10_000]
    markup = symbol_svg(
        "transformer_2w",
        terminal_strokes={"hv": hv_stroke, "lv": lv_stroke},
    )
    root = ElementTree.fromstring(markup)
    elements = list(root)
    lead_strokes = [
        item.attrib["stroke"] for item in elements if item.tag.endswith("line")
    ]
    body_strokes = [
        item.attrib["stroke"]
        for item in elements
        if item.tag.endswith("circle") and item.attrib.get("fill") == "none"
    ]

    assert lead_strokes == [hv_stroke, lv_stroke]
    assert body_strokes == [hv_stroke, lv_stroke]
    assert sum(item.attrib.get("stroke") == hv_stroke for item in elements) >= 2
    assert sum(item.attrib.get("stroke") == lv_stroke for item in elements) >= 2


def test_svg_monochrome_mode_removes_voltage_hues_without_changing_geometry() -> None:
    terminal_strokes = {
        "hv": EXPECTED_VOLTAGE_STROKES[220_000],
        "mv": EXPECTED_VOLTAGE_STROKES[110_000],
        "lv": EXPECTED_VOLTAGE_STROKES[10_000],
    }
    color_root = ElementTree.fromstring(
        symbol_svg("transformer_3w", terminal_strokes=terminal_strokes)
    )
    monochrome_root = ElementTree.fromstring(
        symbol_svg(
            "transformer_3w",
            terminal_strokes=terminal_strokes,
            color_mode=DiagramColorMode.MONOCHROME,
        )
    )

    def geometry_signature(root: ElementTree.Element) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
        ignored = {"stroke", "fill"}
        return [
            (
                item.tag.rsplit("}", 1)[-1],
                tuple(sorted((key, value) for key, value in item.attrib.items() if key not in ignored)),
            )
            for item in root
        ]

    assert geometry_signature(monochrome_root) == geometry_signature(color_root)
    assert all(
        item.attrib.get("stroke") == DIAGRAM_MONOCHROME_STROKE
        for item in monochrome_root
    )


def test_editor_analysis_and_svg_share_one_resolver_and_one_scene_type() -> None:
    from rza_calc.gui import analysis_scheme, editor_scene, main_window, theme

    assert theme.VOLTAGE_STROKES_BY_NOMINAL_V is VOLTAGE_STROKES_BY_NOMINAL_V
    assert theme.voltage_stroke is voltage_stroke
    assert editor_scene.voltage_stroke is voltage_stroke
    assert main_window.voltage_stroke is voltage_stroke
    assert symbol_svg_module.resolve_voltage_stroke is voltage_stroke
    assert analysis_scheme.DiagramGraphicsScene is editor_scene.DiagramGraphicsScene


def test_render_state_priority_and_monochrome_are_explicit() -> None:
    from rza_calc.domain.electrical import EquipmentAvailability
    from rza_calc.gui.editor_scene import DiagramRenderContext, _state_stroke
    from rza_calc.topology import Energization

    color = DiagramRenderContext(color_mode=DiagramColorMode.COLOR)
    monochrome = DiagramRenderContext(color_mode=DiagramColorMode.MONOCHROME)

    assert _state_stroke(10_000, color) == EXPECTED_VOLTAGE_STROKES[10_000]
    assert _state_stroke(10_000, monochrome) == DIAGRAM_MONOCHROME_STROKE
    assert (
        _state_stroke(
            10_000,
            color,
            energization=Energization.DEENERGIZED,
        )
        == DIAGRAM_DEENERGIZED_STROKE
    )
    assert (
        _state_stroke(
            10_000,
            color,
            energization=Energization.DEENERGIZED,
            availability=EquipmentAvailability.OUT_OF_SERVICE,
        )
        == DIAGRAM_OUT_OF_SERVICE_STROKE
    )


def test_scene_applies_topology_color_to_bus_route_and_terminal() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from rza_calc.gui import editor_scene
    from rza_calc.io.project import load_project

    app = QApplication.instance() or QApplication([])
    assert app is not None
    project = load_project(DEMO)
    scene = editor_scene.DiagramGraphicsScene()
    scene.sync_document(project.diagram, project.electrical_model)

    bus_checked = False
    for item in scene._items_by_id.values():
        if item._canonical_key != "busbar":
            continue
        stroke = item._primitive_stroke("terminal")
        if stroke in EXPECTED_VOLTAGE_STROKES.values():
            bus_checked = True
            break
    assert bus_checked, "шина должна получить цвет разрешённой зоны напряжения"

    def painted_pen(item) -> tuple[str, Qt.PenStyle]:
        image = QImage(512, 256, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            item.paint(painter, None)
            return painter.pen().color().name().upper(), painter.pen().style()
        finally:
            painter.end()

    route_checked = False
    for item in scene._route_items_by_id.values():
        expected = editor_scene._state_stroke(
            item._nominal_voltage_v(),
            item._render_context,
            energization=item._energization(),
            availability=item._availability(),
        )
        if expected not in EXPECTED_VOLTAGE_STROKES.values():
            continue
        actual, _ = painted_pen(item)
        assert actual == expected
        route_checked = True
        break
    assert route_checked, "трасса должна получить цвет разрешённой зоны напряжения"

    terminal_checked = False
    for object_item in scene._items_by_id.values():
        for port_item in object_item._port_items.values():
            connection = project.electrical_model.connection_for_port(port_item.port_id)
            if connection is None:
                continue
            nominal = editor_scene._resolved_nominal_voltage(
                project.electrical_model,
                port_item._render_context,
                port_item.port_id,
            )
            expected = editor_scene._state_stroke(
                nominal,
                port_item._render_context,
                energization=editor_scene._node_energization(
                    port_item._render_context,
                    connection.electrical_node_id,
                ),
                availability=editor_scene._equipment_availability(
                    port_item._render_context,
                    object_item.representation.equipment_id,
                ),
            )
            if expected not in EXPECTED_VOLTAGE_STROKES.values():
                continue
            actual, _ = painted_pen(port_item)
            assert actual == expected
            terminal_checked = True
            break
        if terminal_checked:
            break
    assert terminal_checked, "вывод должен получить цвет разрешённой зоны напряжения"


def test_200_plus_element_scene_compiles_topology_once_and_color_toggle_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtWidgets import QApplication

    from rza_calc.domain.fingerprint import electrical_model_fingerprint
    from rza_calc.gui.editor_scene import DiagramGraphicsScene
    from rza_calc.io.project import load_project
    from rza_calc.topology import TopologyEngine

    app = QApplication.instance() or QApplication([])
    assert app is not None
    project = load_project(DEMO)
    model = project.electrical_model
    document = project.diagram
    before_fingerprint = electrical_model_fingerprint(model)
    before_equipment_ids = tuple(model.equipment)
    before_port_ids = tuple(model.ports)
    before_node_ids = tuple(model.electrical_nodes)
    before_connection_ids = tuple(model.connections)
    before_representations = dict(document.representations)
    before_routes = dict(document.routes)

    compile_calls = 0
    original_compile = TopologyEngine.compile

    def counted_compile(self, *args, **kwargs):
        nonlocal compile_calls
        compile_calls += 1
        return original_compile(self, *args, **kwargs)

    monkeypatch.setattr(TopologyEngine, "compile", counted_compile)
    scene = DiagramGraphicsScene()
    scene.sync_document(document, model)

    assert len(scene._items_by_id) + len(scene._route_items_by_id) >= 200
    assert compile_calls == 1
    snapshot = scene.topology_snapshot

    scene.set_color_mode(DiagramColorMode.MONOCHROME)
    assert scene.color_mode is DiagramColorMode.MONOCHROME
    scene.set_color_mode(DiagramColorMode.COLOR)
    assert scene.color_mode is DiagramColorMode.COLOR

    assert compile_calls == 1, "переключение цвета не должно компилировать топологию"
    assert scene.topology_snapshot is snapshot
    assert electrical_model_fingerprint(model) == before_fingerprint
    assert tuple(model.equipment) == before_equipment_ids
    assert tuple(model.ports) == before_port_ids
    assert tuple(model.electrical_nodes) == before_node_ids
    assert tuple(model.connections) == before_connection_ids
    assert dict(document.representations) == before_representations
    assert dict(document.routes) == before_routes


def test_editor_colors_transformer_leads_and_coils_by_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    from PySide6.QtWidgets import QApplication

    from rza_calc.gui import editor_scene
    from rza_calc.io.project import load_project

    app = QApplication.instance() or QApplication([])
    assert app is not None
    project = load_project(DEMO)
    scene = editor_scene.DiagramGraphicsScene()
    scene.sync_document(project.diagram, project.electrical_model)

    selected = None
    expected_by_role: dict[str, str] = {}
    for item in scene._items_by_id.values():
        if item._canonical_key != "transformer_2w":
            continue
        geometry = item.symbol_geometry()
        if not item._port_items:
            continue
        candidate: dict[str, int | None] = {}
        for semantic_role in ("hv", "lv"):
            terminal = geometry.terminal(semantic_role)
            if terminal is None:
                continue
            port_id, port_item = min(
                item._port_items.items(),
                key=lambda pair: (
                    (pair[1].anchor.x - terminal.x) ** 2
                    + (pair[1].anchor.y - terminal.y) ** 2
                ),
            )
            distance_sq = (
                (port_item.anchor.x - terminal.x) ** 2
                + (port_item.anchor.y - terminal.y) ** 2
            )
            if distance_sq > 1e-12:
                continue
            candidate[semantic_role] = editor_scene._resolved_nominal_voltage(
                project.electrical_model,
                item._render_context,
                port_id,
            )
        if candidate.get("hv") and candidate.get("lv"):
            expected_by_role = {
                role: voltage_stroke(nominal_voltage_v)
                for role, nominal_voltage_v in candidate.items()
                if nominal_voltage_v is not None
            }
            if expected_by_role.get("hv") != expected_by_role.get("lv"):
                selected = item
                break
    assert selected is not None, "в демо должен быть трансформатор с разными ВН/НН"

    captured: list[tuple[str, str | None, str]] = []

    def record_primitive(painter, primitive) -> None:
        captured.append(
            (primitive.kind, primitive.voltage_role, painter.pen().color().name().upper())
        )

    monkeypatch.setattr(
        editor_scene.DiagramObjectItem,
        "_paint_primitive",
        staticmethod(record_primitive),
    )

    def render_strokes() -> list[tuple[str, str | None, str]]:
        captured.clear()
        image = QImage(256, 128, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setPen(
                QPen(QColor(selected._symbol_stroke()), selected._line_width)
            )
            selected._paint_symbol(painter)
        finally:
            painter.end()
        return list(captured)

    geometry_before = selected.symbol_geometry()
    assert selected._primitive_stroke("hv") == expected_by_role["hv"]
    assert selected._primitive_stroke("lv") == expected_by_role["lv"]
    colored = render_strokes()
    for kind in ("line", "circle"):
        assert any(actual_kind == kind and role == "hv" and stroke == expected_by_role["hv"] for actual_kind, role, stroke in colored)
        assert any(actual_kind == kind and role == "lv" and stroke == expected_by_role["lv"] for actual_kind, role, stroke in colored)

    scene.set_color_mode(DiagramColorMode.MONOCHROME)
    monochrome = render_strokes()
    assert selected.symbol_geometry() == geometry_before
    assert all(stroke == DIAGRAM_MONOCHROME_STROKE for _, _, stroke in monochrome)


def test_out_of_service_equipment_is_muted_and_dashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from rza_calc.domain.electrical import EquipmentAvailability
    from rza_calc.gui import editor_scene
    from rza_calc.io.project import load_project

    app = QApplication.instance() or QApplication([])
    assert app is not None
    project = load_project(DEMO)
    scene = editor_scene.DiagramGraphicsScene()
    scene.sync_document(project.diagram, project.electrical_model)
    item = next(
        value
        for value in scene._items_by_id.values()
        if value.representation.equipment_id is not None
    )
    captured: list[tuple[Qt.PenStyle, str]] = []

    monkeypatch.setattr(
        editor_scene.DiagramObjectItem,
        "_availability",
        lambda self: EquipmentAvailability.OUT_OF_SERVICE,
    )

    def record_symbol(self, painter) -> None:
        captured.append(
            (painter.pen().style(), painter.pen().color().name().upper())
        )

    monkeypatch.setattr(editor_scene.DiagramObjectItem, "_paint_symbol", record_symbol)
    image = QImage(256, 128, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        item.paint(painter, None)
    finally:
        painter.end()

    assert captured == [
        (Qt.PenStyle.DashLine, DIAGRAM_OUT_OF_SERVICE_STROKE)
    ]


@pytest.mark.parametrize(("behavior_key", "symbol_key"), LEGACY_SYMBOLS.items())
def test_all_eight_legacy_behaviors_resolve_to_real_symbols(
    behavior_key: str,
    symbol_key: str,
) -> None:
    assert canonical_key("", behavior_key) == symbol_key
    assert canonical_key("", behavior_key) != "generic"


@pytest.mark.parametrize("symbol_key", ("circuit_breaker", "disconnector", "recloser"))
def test_open_and_closed_switches_differ_by_geometry_not_color(
    symbol_key: str,
) -> None:
    closed = build_symbol(symbol_key, opened=False)
    opened = build_symbol(symbol_key, opened=True)

    assert closed.primitives != opened.primitives
    assert closed.terminals == opened.terminals


def test_open_demo_line_is_muted_and_dashed_even_when_both_buses_are_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL110_3 must not look closed merely because both endpoint buses are live."""

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from rza_calc.gui import editor_scene
    from rza_calc.io.project import load_project

    app = QApplication.instance() or QApplication([])
    assert app is not None
    project = load_project(DEMO)
    model = project.electrical_model
    scene = editor_scene.DiagramGraphicsScene()
    scene.sync_document(project.diagram, model)

    equipment_by_legacy_id = {
        str((equipment.extensions.get("legacy_calculation") or {}).get("legacy_id")): equipment
        for equipment in model.equipment.values()
    }
    closed_equipment = equipment_by_legacy_id["VL110_1"]
    opened_equipment = equipment_by_legacy_id["VL110_3"]
    items_by_equipment = {
        item.representation.equipment_id: item
        for item in scene._items_by_id.values()
        if item.representation.equipment_id is not None
    }
    closed_item = items_by_equipment[closed_equipment.id]
    opened_item = items_by_equipment[opened_equipment.id]
    assert not closed_item._disconnected()
    assert opened_item._disconnected()
    assert closed_item.symbol_geometry().primitives == opened_item.symbol_geometry().primitives

    captured: list[tuple[str | None, Qt.PenStyle, str]] = []

    def record_primitive(painter, primitive) -> None:
        captured.append(
            (
                primitive.voltage_role,
                painter.pen().style(),
                painter.pen().color().name().upper(),
            )
        )

    monkeypatch.setattr(
        editor_scene.DiagramObjectItem,
        "_paint_primitive",
        staticmethod(record_primitive),
    )

    def object_conductor_style(item) -> tuple[Qt.PenStyle, str]:
        captured.clear()
        image = QImage(256, 128, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            item.paint(painter, None)
        finally:
            painter.end()
        return next((style, color) for role, style, color in captured if role == "from")

    assert object_conductor_style(closed_item)[0] is Qt.PenStyle.SolidLine
    assert object_conductor_style(opened_item) == (
        Qt.PenStyle.DashLine,
        DIAGRAM_DEENERGIZED_STROKE,
    )
    scene.set_color_mode(DiagramColorMode.MONOCHROME)
    assert object_conductor_style(opened_item) == (
        Qt.PenStyle.DashLine,
        DIAGRAM_DEENERGIZED_STROKE,
    )
