"""Visio-style presentation invariants against the actual editor Qt scene.

These checks do not replace protection/topology acceptance. They ensure the
new drawing remains a read-only consumer of the established model.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent / "work_verification"))

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from visio_style_evidence import bus_bridge_fixture, controller_for, crossing_fixture, model_signature
from rza_calc.domain.electrical import SwitchPosition, VoltageClassId
from rza_calc.domain.diagram import GraphicalRepresentation, GraphicalRepresentationId, RepresentationTargetKind
from rza_calc.editor.symbols import DIAGRAM_DEENERGIZED_STROKE, DiagramColorMode, build_symbol, voltage_stroke
from rza_calc.gui import editor_scene
from rza_calc.gui.editor_scene import CanvasMode, DiagramGraphicsScene
from rza_calc.io.project import load_project
from rza_calc.topology import Energization, TopologyEngine

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    for filename in ("segoeui.ttf", "arial.ttf", "arialbd.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    instance.setFont(QFont("Segoe UI", 10))
    yield instance


def _paint_scene(scene: DiagramGraphicsScene) -> QImage:
    image = QImage(1100, 700, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    try:
        scene.render(painter)
    finally:
        painter.end()
    return image


def _switch_fixture(symbol="circuit_breaker", rotation=0):
    controller = controller_for("Read-only render verification")
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    left = controller.add_electrical_node("A", x=30, y=100, voltage_class_id=voltage)
    right = controller.add_electrical_node("B", x=250, y=100, voltage_class_id=voltage)
    switch = controller.add_equipment("builtin." + symbol, "QF-test", x=140, y=100,
        voltage_class_by_group={"main": voltage}, normal_position=SwitchPosition.CLOSED,
        rotation_deg=rotation)
    controller.connect_port_to_node(switch.port_ids[0], left.node_id,
                                    source_representation_id=switch.representation_id)
    controller.connect_port_to_node(switch.port_ids[1], right.node_id,
                                    source_representation_id=switch.representation_id)
    return controller, switch


def _port_signature(item):
    return {port_id: (port.anchor.x, port.anchor.y, port.scenePos().x(), port.scenePos().y())
            for port_id, port in item._port_items.items()}


def test_render_mode_color_and_state_sync_preserve_entire_project(app):
    project = load_project(ROOT / "rza_calc" / "examples" / "energoraion.json")
    model, document = project.electrical_model, project.diagram
    before = model_signature(model, document)
    scene = DiagramGraphicsScene()
    scene.sync_document(document, model)
    for mode in (CanvasMode.EDIT, CanvasMode.ANALYSIS):
        scene.set_mode(mode)
        scene.sync_document(document, model)
        snapshot = scene.topology_snapshot
        ports = {identifier: _port_signature(item) for identifier, item in scene._items_by_id.items()}
        for color in (DiagramColorMode.COLOR, DiagramColorMode.MONOCHROME, DiagramColorMode.COLOR):
            scene.set_color_mode(color)
            _paint_scene(scene)
            assert scene.topology_snapshot is snapshot
            assert {identifier: _port_signature(item) for identifier, item in scene._items_by_id.items()} == ports
            assert model_signature(model, document) == before


@pytest.mark.parametrize("symbol", ("circuit_breaker", "recloser", "disconnector"))
@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_all_switch_ports_stay_fixed_when_opened_at_each_rotation(app, symbol, rotation):
    controller, switch = _switch_fixture(symbol, rotation)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[switch.representation_id]
    closed_geometry = item.symbol_geometry()
    closed_ports = _port_signature(item)
    model_port_ids = tuple(controller.model.ports)
    node_ids = tuple(controller.model.electrical_nodes)
    equipment_ids = tuple(controller.model.equipment)
    state_id = controller.switch_equipment(switch.equipment_id, SwitchPosition.OPEN, confirmed=True)
    # The command intentionally changes the operating state. Rendering after
    # that command must not add any further model/document changes.
    baseline = model_signature(controller.model, controller.diagram)
    scene.sync_document(controller.diagram, controller.model, operating_state_id=state_id)
    opened_item = scene._items_by_id[switch.representation_id]
    assert opened_item._switch_open
    assert opened_item.symbol_geometry().primitives != closed_geometry.primitives
    assert opened_item.symbol_geometry().terminals == closed_geometry.terminals
    assert _port_signature(opened_item) == closed_ports
    assert tuple(controller.model.ports) == model_port_ids
    assert tuple(controller.model.electrical_nodes) == node_ids
    assert tuple(controller.model.equipment) == equipment_ids
    _paint_scene(scene)
    assert model_signature(controller.model, controller.diagram) == baseline


@pytest.mark.parametrize("opened,expected_fill", ((False, "#E5B9B5"), (True, "#92D050")))
@pytest.mark.parametrize("color_mode", (DiagramColorMode.COLOR, DiagramColorMode.MONOCHROME))
def test_breaker_body_uses_nominal_voltage_and_position_fill_on_dead_network(
    app, monkeypatch, opened, expected_fill, color_mode,
):
    controller, switch = _switch_fixture()
    state_id = None
    if opened:
        state_id = controller.switch_equipment(switch.equipment_id, SwitchPosition.OPEN, confirmed=True)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model, operating_state_id=state_id)
    scene.set_color_mode(color_mode)
    item = scene._items_by_id[switch.representation_id]
    # This fixture has no source, so position is not confused with energization.
    assert item._energization() is Energization.DEENERGIZED
    before = model_signature(controller.model, controller.diagram)
    captured = []

    def capture(painter, primitive):
        captured.append((primitive, painter.pen().color().name().upper(),
                         painter.brush().color().name().upper(), painter.brush().style()))

    monkeypatch.setattr(editor_scene.DiagramObjectItem, "_paint_primitive", staticmethod(capture))
    image = QImage(256, 128, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setPen(QPen(QColor("#111827"), 2.0))
        item._paint_symbol(painter)
    finally:
        painter.end()
    bodies = [(primitive, stroke, fill) for primitive, stroke, fill, brush in captured
              if primitive.kind == "rect" and getattr(primitive, "state_fill", None)]
    assert len(bodies) == 1
    primitive, stroke, fill = bodies[0]
    assert primitive.voltage_role == "body"
    assert stroke == voltage_stroke(10_000, color_mode=color_mode)
    if color_mode is DiagramColorMode.MONOCHROME:
        expected_fill = "#FFFFFF" if opened else "#E5E7EB"
    assert fill == expected_fill
    assert model_signature(controller.model, controller.diagram) == before


def test_display_crossing_keeps_four_nodes_and_two_electrical_components(app):
    controller, nodes = crossing_fixture()
    before = model_signature(controller.model, controller.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    for mode in (CanvasMode.EDIT, CanvasMode.ANALYSIS):
        scene.set_mode(mode)
        _paint_scene(scene)
    snapshot = TopologyEngine().compile(controller.model)
    assert len(controller.model.electrical_nodes) == 4
    assert snapshot.has_path(nodes[0].node_id, nodes[1].node_id)
    assert snapshot.has_path(nodes[2].node_id, nodes[3].node_id)
    assert not snapshot.has_path(nodes[0].node_id, nodes[2].node_id)
    assert model_signature(controller.model, controller.diagram) == before


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_equipment_label_is_horizontal_and_saved_anchor_is_not_rewritten(app, rotation):
    controller, switch = _switch_fixture(rotation=rotation)
    controller.set_label(switch.representation_id, text="QF-user-label", label_x=23.0, label_y=-77.0)
    baseline = model_signature(controller.model, controller.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[switch.representation_id]
    assert item._label.pos().x() == 23.0
    assert item._label.pos().y() == -77.0
    assert (item.rotation() + item._label.rotation()) % 360 == 0
    _paint_scene(scene)
    scene.set_mode(CanvasMode.ANALYSIS)
    scene.set_color_mode(DiagramColorMode.MONOCHROME)
    assert model_signature(controller.model, controller.diagram) == baseline


def test_physical_route_names_and_parameters_are_horizontal_without_duplicate_object_labels(app):
    controller, _ = crossing_fixture()
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    horizontal = vertical = 0
    for item in scene._route_items_by_id.values():
        if item.route.equipment_id is None:
            continue
        bounds = item._path.boundingRect()
        if bounds.width() > bounds.height():
            horizontal += 1
        else:
            vertical += 1
        # B3 has one editable name+parameter block. Route orientation must
        # not introduce a second, rotated name-only label from the donor UI.
        assert item._label.rotation() == 0.0
        assert item._label.isVisible()
        assert controller.model.equipment[item.route.equipment_id].name in item._label.text()
        assert "км" in item._label.text()
        assert not hasattr(item, "_name_label") or item._name_label is item._label
    assert horizontal and vertical
    labels = [item._label.sceneBoundingRect() for item in scene._route_items_by_id.values()
              if item.route.equipment_id is not None and item._label.isVisible()]
    assert len(labels) == 2
    assert not labels[0].intersects(labels[1])
    assert not any(rect.intersects(QRectF(440, 290, 20, 20)) for rect in labels)
    project = load_project(ROOT / "rza_calc" / "examples" / "energoraion.json")
    scene.sync_document(project.diagram, project.electrical_model)
    object_names = {item.representation.equipment_id for item in scene._items_by_id.values()
                    if item.representation.equipment_id is not None and item._label.isVisible()}
    for item in scene._route_items_by_id.values():
        if item.route.equipment_id in object_names:
            assert not item._label.isVisible()


def test_bus_marks_only_explicit_anchors_and_branches_have_gaps_in_both_directions(app):
    controller, buses = bus_bridge_fixture()
    baseline = model_signature(controller.model, controller.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    for bus, independent_intersection in zip(buses, ((100.0, 0.0), (0.0, 120.0))):
        item = scene._items_by_id[bus.representation_id]
        actual = {(round(point.x(), 5), round(point.y(), 5)) for point in item._bus_junction_points}
        expected = set()
        for route_item in scene._route_items_by_id.values():
            route = route_item.route
            vertices = route_item._display_vertices
            for anchor, vertex in ((route.start_anchor, vertices[0]), (route.end_anchor, vertices[-1])):
                if anchor.representation_id == bus.representation_id:
                    point = item.mapFromScene(vertex.x, vertex.y)
                    expected.add((round(point.x(), 5), round(point.y(), 5)))
        assert len(expected) == 1
        assert actual == expected
        assert independent_intersection not in actual, "independent crossing must not create a junction"
    jumped_directions = set()
    for item in scene._route_items_by_id.values():
        if item.route.equipment_id is None:
            continue
        path = item._display_path
        moves = [index for index in range(path.elementCount()) if path.elementAt(index).isMoveTo()]
        assert len(moves) == 2, "only the crossing branch must have one full-band gap"
        assert not any(path.elementAt(index).isCurveTo() for index in range(path.elementCount()))
        first, second = path.elementAt(moves[1] - 1), path.elementAt(moves[1])
        gap = abs(second.x - first.x) + abs(second.y - first.y)
        assert 8 <= gap < 25, "The gap follows restored thin bus ink, not the removed band"
        bounds = item._path.boundingRect()
        jumped_directions.add("horizontal" if bounds.width() > bounds.height() else "vertical")
    assert jumped_directions == {"horizontal", "vertical"}
    _paint_scene(scene)
    assert model_signature(controller.model, controller.diagram) == baseline


def test_unknown_state_never_claims_closed_or_open_and_preserves_ports(app, monkeypatch):
    controller, switch = _switch_fixture()
    baseline = model_signature(controller.model, controller.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    ports = _port_signature(scene._items_by_id[switch.representation_id])
    scene.sync_document(controller.diagram, controller.model, topology_state_available=False)
    item = scene._items_by_id[switch.representation_id]
    captured = []
    def capture(painter, primitive):
        captured.append((primitive, painter.brush().color().name().upper()))
    monkeypatch.setattr(editor_scene.DiagramObjectItem, "_paint_primitive", staticmethod(capture))
    image = QImage(256, 128, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        item._paint_symbol(painter)
    finally:
        painter.end()
    body = [(primitive, fill) for primitive, fill in captured if primitive.voltage_role == "body"]
    assert len(body) == 1
    assert body[0][0].kind == "rect"
    assert body[0][1] == "#FFFFFF"
    assert _port_signature(item) == ports
    assert model_signature(controller.model, controller.diagram) == baseline


def test_explicitly_hidden_equipment_label_is_not_reintroduced_by_branch_route(app):
    controller, _ = crossing_fixture()
    route = next(route for route in controller.diagram.routes.values() if route.equipment_id is not None)
    representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.evidence.hidden-branch-label"), route.page_id,
        RepresentationTargetKind.EQUIPMENT, equipment_id=route.equipment_id,
        x=100.0, y=650.0, symbol_key="line_section", label="User hidden name",
        extensions={"stage3_graphics": {"label_visible": False}},
    )
    document = replace(controller.diagram, representations={
        **controller.diagram.representations, representation.id: representation,
    })
    baseline = model_signature(controller.model, document)
    scene = DiagramGraphicsScene()
    scene.sync_document(document, controller.model)
    assert not scene._items_by_id[representation.id]._label.isVisible()
    assert not scene._route_items_by_id[route.id]._label.isVisible()
    _paint_scene(scene)
    assert model_signature(controller.model, document) == baseline


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_factory_label_anchor_is_drawn_outside_body_without_rewriting_saved_defaults(app, rotation):
    controller, switch = _switch_fixture(rotation=rotation)
    graphics = controller.diagram.representations[switch.representation_id].extensions["stage3_graphics"]
    assert graphics["label_x"] == 0 and graphics["label_y"] == -32
    baseline = model_signature(controller.model, controller.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[switch.representation_id]
    body_bounds = item.mapRectToScene(item.symbol_ink_rect())
    assert not item._label.sceneBoundingRect().intersects(body_bounds)
    assert (item.rotation() + item._label.rotation()) % 360 == 0
    _paint_scene(scene)
    assert model_signature(controller.model, controller.diagram) == baseline


@pytest.mark.parametrize("symbol", ("circuit_breaker", "recloser", "disconnector"))
def test_open_switch_keeps_live_incoming_lead_and_dead_outgoing_lead_solid(app, monkeypatch, symbol):
    controller, switch = _switch_fixture(symbol)
    left_node = controller.model.connection_for_port(switch.port_ids[0]).electrical_node_id
    source = controller.add_equipment("builtin.external_grid", "Supply", x=-100.0, y=100.0,
        voltage_class_by_group={"main": VoltageClassId("builtin.voltage.ac.10kv")})
    controller.connect_port_to_node(source.port_ids[0], left_node,
                                    source_representation_id=source.representation_id)
    state_id = controller.switch_equipment(switch.equipment_id, SwitchPosition.OPEN, confirmed=True)
    baseline = model_signature(controller.model, controller.diagram)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model, operating_state_id=state_id)
    item = scene._items_by_id[switch.representation_id]
    assert item._switch_open
    assert item._primitive_stroke("a") == voltage_stroke(10_000)
    assert item._primitive_stroke("b") == DIAGRAM_DEENERGIZED_STROKE
    captured = []
    def capture(painter, primitive):
        if primitive.voltage_role in {"a", "b"}:
            captured.append((primitive.voltage_role, painter.pen().color().name().upper(), painter.pen().style()))
    monkeypatch.setattr(editor_scene.DiagramObjectItem, "_paint_primitive", staticmethod(capture))
    image = QImage(256, 128, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        item.paint(painter, None)
    finally:
        painter.end()
    assert any(role == "a" and color == voltage_stroke(10_000) for role, color, _ in captured)
    assert any(role == "b" and color == DIAGRAM_DEENERGIZED_STROKE for role, color, _ in captured)
    assert all(style == Qt.PenStyle.SolidLine for _, _, style in captured)
    assert model_signature(controller.model, controller.diagram) == baseline
