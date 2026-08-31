"""Direction glyphs are presentation-only and follow stable endpoint roles.

The arrow denotes the declared from/to orientation, never measured current.
Native route storage may legally be reversed; do not infer roles from points.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import math
import os
from pathlib import Path
from xml.etree import ElementTree

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPainterPathStroker
from PySide6.QtWidgets import QApplication

from rza_calc.domain.diagram import DiagramRouteKind
from rza_calc.domain.electrical import VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.symbol_svg import symbol_svg
from rza_calc.editor.symbols import SymbolPrimitive, build_symbol, line_direction_marker
from rza_calc.gui.editor_scene import DiagramGraphicsScene, DiagramGraphicsView
from rza_calc.gui.label_layout import route_label_obstacle_path
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.project import load_project
from test_ui_b3_label_geometry import _native_line_scene


DEMO = Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json"
_APP = None


@pytest.fixture(scope="module", autouse=True)
def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


def _axis(marker):
    first, tip, last = marker.points
    return tip[0] - (first[0] + last[0]) / 2, tip[1] - (first[1] + last[1]) / 2


def _center(marker):
    first, tip, last = marker.points
    return ((first[0] + last[0]) / 2 + tip[0]) / 2, ((first[1] + last[1]) / 2 + tip[1]) / 2


def _signature(model):
    return (electrical_model_fingerprint(model), model.revision, model.connectivity_signature(),
            dict(model.ports), {key: eq.port_ids for key, eq in model.equipment.items()},
            frozenset(model.electrical_nodes), frozenset(model.connections))


@pytest.mark.parametrize("end", ((128, 0), (0, 128), (-128, 0), (0, -128), (128, 128)))
@pytest.mark.parametrize("reverse", (False, True))
def test_shared_marker_has_fixed_thin_geometry_and_declared_direction(end, reverse):
    marker = line_direction_marker((0, 0), end, reverse=reverse)
    assert isinstance(marker, SymbolPrimitive)
    assert marker.kind == "polyline" and len(marker.points) == 3
    assert marker.direction_marker is True
    assert marker.filled is False and marker.stroke_scale == .8
    assert marker.voltage_role == "from"
    assert _center(marker) == pytest.approx((end[0] / 2, end[1] / 2))
    axis = _axis(marker)
    assert math.hypot(*axis) == pytest.approx(8)
    assert math.dist(marker.points[0], marker.points[2]) == pytest.approx(5.6)
    assert axis[0] * end[1] - axis[1] * end[0] == pytest.approx(0)
    assert (axis[0] * end[0] + axis[1] * end[1] > 0) is (not reverse)


@pytest.mark.parametrize("length", (0, 4, 11.99))
def test_short_or_degenerate_segment_cannot_fit_an_arrow_with_endpoint_margins(length):
    assert line_direction_marker((0, 0), (length, 0)) is None


@pytest.mark.parametrize("key", ("line", "line_section"))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_line_qt_and_svg_arrow_points_rotate_with_identical_unchanged_semantic_ports(key, angle):
    controller, line, scene = _native_line_scene()
    if key == "line_section":
        identifier = controller.place_existing_equipment(line.section_id, x=300, y=160)
        controller.resize_representation(identifier, 128, 24)
    else:
        identifier = controller.add_equipment("builtin.line", "ВЛ", x=300, y=160,
                                             width=128, height=24).representation_id
    before = _signature(controller.model)
    controller.rotate_representation(identifier, angle)
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[identifier]
    geometry = item.symbol_geometry()
    assert geometry.key == key
    assert len(geometry.primitives) == 2, "One shaft and one chevron; no legacy double ticks"
    shaft, marker = geometry.primitives
    assert shaft.kind == "line" and not shaft.direction_marker
    assert marker.kind == "polyline" and marker.direction_marker
    assert [(port.role, port.x, port.y) for port in geometry.terminals] == [("from", -64, 0), ("to", 64, 0)]
    svg = ElementTree.fromstring(symbol_svg(key, width=128, height=24, rotation=angle,
                                          show_terminals=False, terminal_strokes={"from": "#123456"}))
    assert len(svg) == 2
    chevron = next(node for node in svg if node.tag.endswith("polyline"))
    assert chevron.attrib["fill"] == "none"
    assert chevron.attrib["stroke"] == "#123456"
    assert float(chevron.attrib["stroke-width"]) == pytest.approx(1.6)
    svg_points = [tuple(float(value) for value in point.split(","))
                  for point in chevron.attrib["points"].split()]
    qt_points = [(item.mapToScene(QPointF(*point)).x() - item.x(),
                  item.mapToScene(QPointF(*point)).y() - item.y()) for point in marker.points]
    for qt_point, svg_point in zip(qt_points, svg_points, strict=True):
        assert qt_point == pytest.approx(svg_point)
    ports = {port.anchor.role: port for port in item._port_items.values()}
    start, end = ports["from"].scenePos(), ports["to"].scenePos()
    arrow = (svg_points[1][0] - (svg_points[0][0] + svg_points[2][0]) / 2,
             svg_points[1][1] - (svg_points[0][1] + svg_points[2][1]) / 2)
    assert arrow[0] * (end.x() - start.x()) + arrow[1] * (end.y() - start.y()) > 0
    assert _signature(controller.model) == before


@pytest.mark.parametrize("opened", (False, True))
def test_real_disconnector_keeps_contact_ticks_and_is_not_repainted_as_a_breaker(opened):
    geometry = build_symbol("disconnector", opened=opened)
    assert len(geometry.primitives) == 5
    assert all(not primitive.direction_marker for primitive in geometry.primitives)
    assert not any(primitive.kind == "rect" for primitive in geometry.primitives)
    ticks = [primitive for primitive in geometry.primitives if primitive.kind == "line"
             and primitive.points[0][0] == primitive.points[1][0]]
    assert len(ticks) == 2
    assert sorted(tick.points[0][0] for tick in ticks) == [-12, 12]
    assert [port.role for port in geometry.terminals] == ["a", "b"]


def test_native_reverse_anchor_order_is_valid_but_cannot_reverse_declared_arrow():
    controller, line, scene = _native_line_scene()
    route = controller.diagram.routes[line.route_id]
    before = _signature(controller.model)
    original_item = scene._route_items_by_id[route.id]
    marker = original_item.direction_marker()
    assert marker is not None and _axis(marker)[0] > 0
    reverse = replace(route, start_anchor=route.end_anchor, end_anchor=route.start_anchor,
                      waypoints=tuple(reversed(route.waypoints)))
    reversed_document = replace(controller.diagram, routes={route.id: reverse})
    assert reversed_document.validate_targets(controller.model) == ()
    reverse_scene = DiagramGraphicsScene()
    reverse_scene.sync_document(reversed_document, controller.model)
    reverse_item = reverse_scene._route_items_by_id[route.id]
    assert controller.model.port_definition(reverse.start_anchor.branch_port_id).role == "to"
    before_paths = (QPainterPath(reverse_item._path), QPainterPath(reverse_item._display_path), QPainterPath(reverse_item.shape()))
    reverse_marker = reverse_item.direction_marker()
    assert reverse_marker is not None
    for point, expected in zip(reverse_marker.points, marker.points, strict=True):
        assert point == pytest.approx(expected)
    assert (reverse_item._path, reverse_item._display_path, reverse_item.shape()) == before_paths
    assert _signature(controller.model) == before


def _visible_path_item():
    controller, line, scene = _native_line_scene()
    return controller, scene, scene._route_items_by_id[line.route_id]


def test_native_marker_uses_longest_visible_straight_segment_not_arc_or_gap():
    controller, scene, item = _visible_path_item()
    before = diagram_to_dict(controller.diagram)
    path = QPainterPath(QPointF(0, 0))
    path.lineTo(40, 0)
    path.cubicTo(44, -8, 56, -8, 60, 0)
    path.lineTo(200, 0)
    path.moveTo(400, 0)  # A real displayed gap; never connect it for the arrow.
    path.lineTo(450, 0)
    item.set_bridge_display(path, ())
    marker = item.direction_marker()
    assert marker is not None
    assert _center(marker) == pytest.approx((130, 0))
    assert all(60 < point[0] < 200 for point in marker.points)
    assert item._display_path == path
    assert diagram_to_dict(controller.diagram) == before


def test_native_arrow_tie_break_uses_midpoint_not_path_traversal_order():
    controller, scene, item = _visible_path_item()
    forward = QPainterPath(QPointF(0, 100))
    forward.lineTo(100, 100)
    forward.moveTo(0, 0)
    forward.lineTo(100, 0)
    item.set_bridge_display(forward, ())
    first = item.direction_marker()
    other = QPainterPath(QPointF(0, 0))
    other.lineTo(100, 0)
    other.moveTo(0, 100)
    other.lineTo(100, 100)
    item.set_bridge_display(other, ())
    assert first is not None
    assert _center(first) == pytest.approx((50, 0))
    assert item.direction_marker() == first


@pytest.mark.parametrize("kind", ("curve_only", "short", "empty"))
def test_native_arrow_omits_paths_without_a_suitable_visible_straight_segment(kind):
    controller, scene, item = _visible_path_item()
    path = QPainterPath(QPointF(0, 0))
    if kind == "curve_only":
        path.cubicTo(100, -100, 200, 100, 300, 0)
    elif kind == "short":
        path.lineTo(8, 0)
    item.set_bridge_display(path, ())
    assert item.direction_marker() is None


def test_native_arrow_does_not_cover_a_junction_dot_at_segment_midpoint():
    controller, scene, item = _visible_path_item()
    path = QPainterPath(QPointF(0, 0))
    path.lineTo(300, 0)
    path.moveTo(0, 100)
    path.lineTo(100, 100)
    item.set_bridge_display(path, (QPointF(150, 0),))
    marker = item.direction_marker()
    assert marker is not None
    assert _center(marker) == pytest.approx((50, 100))


def test_unknown_endpoint_roles_and_plain_connections_do_not_fabricate_direction():
    controller, line, _ = _native_line_scene()
    original = controller.diagram.routes[line.route_id]
    breaker = controller.add_equipment("builtin.circuit_breaker", "Unknown from/to roles", x=300, y=160,
                                       voltage_class_by_group={"main": VoltageClassId("builtin.voltage.ac.10kv")})
    for port, anchor in zip(breaker.port_ids, (original.start_anchor, original.end_anchor), strict=True):
        controller.connect_port_to_node(port, anchor.electrical_node_id,
                                       node_representation_id=anchor.representation_id,
                                       source_representation_id=breaker.representation_id)
    route = replace(original, equipment_id=breaker.equipment_id,
                    start_anchor=replace(original.start_anchor, branch_port_id=breaker.port_ids[0]),
                    end_anchor=replace(original.end_anchor, branch_port_id=breaker.port_ids[1]))
    routes = dict(controller.diagram.routes)
    routes[route.id] = route
    document = replace(controller.diagram, routes=routes)
    assert document.validate_targets(controller.model) == ()
    scene = DiagramGraphicsScene()
    scene.sync_document(document, controller.model)
    assert scene._route_items_by_id[route.id].direction_marker() is None
    plain = [item for item in scene._route_items_by_id.values() if item.route.kind is DiagramRouteKind.NODE_CONNECTION]
    assert plain
    assert all(item.direction_marker() is None for item in plain)


def test_native_arrow_wing_ink_is_a_label_layout_obstacle_beyond_the_shaft():
    controller, scene, item = _visible_path_item()
    marker = item.direction_marker()
    assert marker is not None
    wing = item.mapToScene(QPointF(*marker.points[0]))
    assert abs(wing.y()) == pytest.approx(2.8), "Fixture shaft lies on y=0"
    assert not QRectF(-10, -2.5, 620, 5).contains(wing), "Probe must extend beyond the old thickened shaft"
    obstacle = route_label_obstacle_path(item)
    assert obstacle.contains(wing)


@pytest.mark.parametrize("zoom", (.6, 1.0))
def test_native_auto_labels_keep_four_units_from_independently_measured_arrow_ink(zoom):
    controller, scene, item = _visible_path_item()
    before = diagram_to_dict(controller.diagram)
    view = DiagramGraphicsView(scene)
    view.set_zoom(zoom)
    try:
        marker = item.direction_marker()
        assert marker is not None
        # Measure the two actual painted wings, not label-layout obstacles or
        # the underlying electrical route (which deliberately omits arrows).
        painted = QPainterPath(item.mapToScene(QPointF(*marker.points[0])))
        for point in marker.points[1:]:
            painted.lineTo(item.mapToScene(QPointF(*point)))
        stroker = QPainterPathStroker()
        stroker.setWidth(2.1 * marker.stroke_scale / zoom)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        ink = stroker.createStroke(painted)
        labels = [owner._label for owner in (*scene._items_by_id.values(), *scene._route_items_by_id.values())
                  if owner._label.isVisible() and owner._label.text().strip()]
        assert len(labels) == 3
        for label in labels:
            margin = 4 - 1e-6
            padded = label.sceneBoundingRect().adjusted(-margin, -margin, margin, margin)
            assert not ink.intersects(padded), "An auto label enters the arrow's four-unit clearance"
        assert diagram_to_dict(controller.diagram) == before
    finally:
        view.close()


def test_current_demo_has_one_arrow_per_line_without_rewriting_ids_ports_or_source():
    original_hash = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    project = load_project(DEMO)
    before = _signature(project.electrical_model)
    document = diagram_to_dict(project.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(project.diagram, project.electrical_model)
    rows = [item for item in scene._items_by_id.values() if item._canonical_key in {"line", "line_section"}]
    assert rows, "Read the current user project; do not assume old stored coordinates"
    for item in rows:
        arrows = [primitive for primitive in item.symbol_geometry().primitives if primitive.direction_marker]
        assert len(arrows) == 1
        ports = {port.anchor.role: port for port in item._port_items.values()}
        start, end = ports["from"].scenePos(), ports["to"].scenePos()
        local_axis = _axis(arrows[0])
        origin = item.mapToScene(QPointF(0, 0))
        target = item.mapToScene(QPointF(*local_axis))
        assert (target.x() - origin.x()) * (end.x() - start.x()) + (target.y() - origin.y()) * (end.y() - start.y()) > 0
    assert _signature(project.electrical_model) == before
    assert diagram_to_dict(project.diagram) == document
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == original_hash


def _straight_spans(path):
    last = None
    spans = []
    for index in range(path.elementCount()):
        element = path.elementAt(index)
        point = (element.x, element.y)
        if element.type == QPainterPath.ElementType.LineToElement and last is not None:
            spans.append((last, point))
        # CurveToDataElements update the cursor through both the control and
        # endpoint, but only a following LineTo creates a usable straight span.
        last = point
    return spans


@pytest.mark.parametrize("angle", (0, 90))
def test_crossed_line_objects_paint_the_arrow_on_visible_ink_not_the_template_gap(angle, monkeypatch):
    controller, line, _initial_scene = _native_line_scene()
    first = controller.add_equipment("builtin.line", "Crossing A", x=300, y=300, width=128, height=24)
    second = controller.add_equipment("builtin.line", "Crossing B", x=600, y=300, width=128, height=24)
    ordered = sorted((first.representation_id, second.representation_id), key=lambda identifier: identifier.value)
    rows = dict(controller.diagram.representations)
    rows[ordered[0]] = replace(rows[ordered[0]], x=300, y=300, rotation_deg=angle)
    rows[ordered[1]] = replace(rows[ordered[1]], x=300, y=300, rotation_deg=(angle + 90) % 180)
    document = replace(controller.diagram, representations=rows)
    before = _signature(controller.model)
    before_document = diagram_to_dict(document)
    assert document.validate_targets(controller.model) == ()
    scene = DiagramGraphicsScene()
    scene.sync_document(document, controller.model)
    jumping = scene._items_by_id[ordered[0]]
    assert jumping.rotation() == angle
    assert 0 in jumping._bridge_paths
    assert any(jumping._bridge_paths[0].elementAt(i).type == QPainterPath.ElementType.CurveToElement
               for i in range(jumping._bridge_paths[0].elementCount()))
    for identifier in ordered:
        item = scene._items_by_id[identifier]
        template = next(primitive for primitive in item.symbol_geometry().primitives if primitive.direction_marker)
        marker = item.direction_marker()
        assert marker is not None
        center = _center(marker)
        visible = item._bridge_paths[0]
        candidates = []
        for start, end in _straight_spans(visible):
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy)
            if length < 12:
                continue
            ux, uy = dx / length, dy / length
            cross = (center[0] - start[0]) * uy - (center[1] - start[1]) * ux
            projected = [(point[0] - start[0]) * ux + (point[1] - start[1]) * uy for point in marker.points]
            if abs(cross) < 1e-6 and min(projected) >= 2 - 1e-6 and max(projected) <= length - 2 + 1e-6:
                candidates.append((start, end))
        assert candidates, "Actual marker must fit a straight visible span, outside both bridge curves and pen gaps"
        assert marker.points != template.points
        recorded = []
        original_paint = item._paint_primitive
        def record(painter, primitive):
            recorded.append(primitive)
            return original_paint(painter, primitive)
        monkeypatch.setattr(item, "_paint_primitive", record)
        image = QImage(256, 256, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(128, 128)
        try:
            item._paint_symbol(painter)
        finally:
            painter.end()
        painted_markers = [primitive for primitive in recorded if primitive.direction_marker]
        assert len(painted_markers) == 1
        assert painted_markers[0].points == marker.points
    assert _signature(controller.model) == before
    assert diagram_to_dict(document) == before_document


class _SelectionPaintRecorder(QPainter):
    """Record actual paint calls while still executing them on a Qt image."""
    def __init__(self, image):
        super().__init__(image)
        self.ellipses = []
        self.frames = []

    def drawEllipse(self, *args):  # noqa: N802 - Qt API
        self.ellipses.append((args, self.brush().color().name(), self.brush().style()))
        return super().drawEllipse(*args)

    def drawRoundedRect(self, *args):  # noqa: N802 - Qt API
        self.frames.append((QRectF(args[0]), self.pen().color().name(), self.pen().style()))
        return super().drawRoundedRect(*args)


@pytest.mark.parametrize("key, expected_dot", (("line", False), ("line_section", False),
                                               ("busbar", False), ("circuit_breaker", True)))
def test_selected_linear_objects_keep_frame_and_handle_without_a_false_blue_junction(key, expected_dot):
    controller, line, scene = _native_line_scene()
    if key == "line_section":
        identifier = controller.place_existing_equipment(line.section_id, x=300, y=160)
    elif key == "busbar":
        identifier = controller.add_electrical_node("Шина", x=300, y=160, symbol_key="busbar_horizontal",
                                                   width=128, height=20).representation_id
    else:
        identifier = controller.add_equipment("builtin." + key, "Выбранный объект", x=300, y=160).representation_id
    scene.sync_document(controller.diagram, controller.model)
    before = _signature(controller.model)
    document = diagram_to_dict(controller.diagram)
    scene.select_representations((identifier,))
    item = scene._items_by_id[identifier]
    assert item._canonical_key == key
    assert item.isSelected()
    assert scene._rotation_handle.isVisible()
    assert scene._rotation_handle.representation_id == identifier
    image = QImage(256, 256, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = _SelectionPaintRecorder(image)
    painter.translate(128, 128)
    try:
        item.paint(painter, None)
    finally:
        painter.end()
    frames = [rect for rect, color, style in painter.frames
              if color == "#2563eb" and style is Qt.PenStyle.DashLine]
    frame = item.symbol_ink_rect().adjusted(-5, -5, 5, 5)
    assert frames == [frame], "Restored thin buses and apparatus keep their selection margin"
    dots = [args for args, color, brush in painter.ellipses
            if color == "#2563eb" and brush is Qt.BrushStyle.SolidPattern
            and len(args) == 3 and args[0] == QPointF(0, 0)
            and args[1:] == (2.8, 2.8)]
    assert len(dots) == int(expected_dot), "The selection dot must not mimic a conductor junction or mask an arrow"
    assert _signature(controller.model) == before
    assert diagram_to_dict(controller.diagram) == document
