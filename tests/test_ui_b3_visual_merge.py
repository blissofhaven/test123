"""Integration of B3 labels with the adopted Visio drawing/rotation UI.

All fixtures use the actual Qt scene and controller. The four-unit label
clearance is measured against painted bridge arcs, thick buses and junctions,
not only the unchanged electrical centreline.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.core.methodology import Methodology
from rza_calc.core.model import Network
from rza_calc.domain import ProjectStructure
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import ProjectEditorController
from rza_calc.editor.state import label_is_manual
from rza_calc.gui.editor_scene import DiagramGraphicsScene, DiagramGraphicsView
from rza_calc.io.project import FORMAT_VERSION, ProjectData, load_project, save_project
from test_rotation_handle import begin_drag, connected_fixture, mouse_at, select_object, show_canvas
from test_ui_b3_label_geometry import _visible_rectangles, measure_scene_conflicts
from test_ui_b3_labels import _route_midpoint
from test_visio_style_integration import bus_bridge_fixture, crossing_fixture, model_signature


_APP = None


@pytest.fixture(scope="module", autouse=True)
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


def _scene(controller):
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    return scene


def _has_curve(path):
    return any(path.elementAt(index).type == QPainterPath.ElementType.CurveToElement
               for index in range(path.elementCount()))


@pytest.mark.parametrize("zoom", (1.0, 0.6))
def test_auto_labels_clear_painted_bridges_thick_buses_and_junction_markers(zoom):
    controller, buses = bus_bridge_fixture()
    before = model_signature(controller.model, controller.diagram)
    scene = _scene(controller)
    view = DiagramGraphicsView(scene)
    view.set_zoom(zoom)
    QApplication.processEvents()
    try:
        assert len(_visible_rectangles(scene)) >= 8
        # A filled bus uses an orthogonal gap in the crossing branch: a small
        # arc would be hidden inside the new band. Thin-wire arcs remain
        # covered by the independent measurement test below.
        assert any(sum(item._display_path.elementAt(index).isMoveTo()
                       for index in range(item._display_path.elementCount())) > 1
                   for item in scene._route_items_by_id.values())
        for bus in buses:
            item = scene._items_by_id[bus.representation_id]
            assert item.symbol_geometry().primitives[0].stroke_scale == 2.25
            assert item._bus_junction_points
        assert measure_scene_conflicts(scene) == []
        assert scene.label_layout_conflicts() == ()
        assert model_signature(controller.model, controller.diagram) == before
    finally:
        view.close()


def test_independent_measurement_detects_bridge_ink_outside_the_saved_centerline():
    controller, _ = crossing_fixture()
    before = model_signature(controller.model, controller.diagram)
    scene = _scene(controller)
    route_item = next(item for item in scene._route_items_by_id.values() if _has_curve(item._display_path))
    owner_id, owner = next(iter(scene._items_by_id.items()))
    label = owner._label
    # Place a test glyph just beyond the centreline's four-unit clearance,
    # where the bridge still has real painted ink. No document data is edited.
    label.setText("X")
    center = route_item._path.boundingRect().center()
    size = label.boundingRect()
    if route_item._path.boundingRect().width() > route_item._path.boundingRect().height():
        position = QPointF(center.x() - size.width() / 2, center.y() - 5.5 - size.height())
    else:
        position = QPointF(center.x() + 5.5, center.y() - size.height() / 2)
    label.setPos(owner.mapFromScene(position))
    rectangle = label.sceneBoundingRect().adjusted(-4, -4, 4, 4)
    old_stroker = QPainterPathStroker()
    old_stroker.setWidth(2.1)
    old_stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    assert not old_stroker.createStroke(route_item._path).intersects(rectangle)
    assert ("wire", owner_id.value, route_item.route_id.value) in measure_scene_conflicts(scene)
    assert model_signature(controller.model, controller.diagram) == before


@pytest.mark.parametrize("manual", (False, True))
@pytest.mark.parametrize("commit", (False, True))
def test_corner_rotation_preview_keeps_b3_label_horizontal_and_restores_offsets(manual, commit):
    controller, added = connected_fixture("b3-visual-preview", "transformer_2w")
    if manual:
        # Default-looking coordinates are still an explicit user choice.
        controller.set_label(added.representation_id, label_x=0, label_y=-32)
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        initial_rectangles = _visible_rectangles(canvas.scene)
        initial_document = controller.diagram
        initial_model = electrical_model_fingerprint(controller.model)
        initial_offsets = dict(initial_document.representations[added.representation_id].extensions["stage3_graphics"])
        end = begin_drag(canvas, added.representation_id)
        assert item.rotation() == 180
        transform = item._label.sceneTransform()
        assert (transform.m11(), transform.m12(), transform.m21(), transform.m22()) == pytest.approx((1, 0, 0, 1))
        assert controller.diagram is initial_document
        assert dict(controller.diagram.representations[added.representation_id].extensions["stage3_graphics"]) == initial_offsets
        if manual:
            assert (item._label.pos().x(), item._label.pos().y()) == (0, -32)
        else:
            assert measure_scene_conflicts(canvas.scene) == []
        if commit:
            mouse_at(canvas, end, "release")
            assert controller.diagram.representations[added.representation_id].rotation_deg == 180
            assert label_is_manual(controller.diagram.representations[added.representation_id]) is manual
            canvas.undo()
        else:
            QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
            QApplication.processEvents()
            mouse_at(canvas, end, "release")
            assert controller.diagram is initial_document
        assert canvas.scene._items_by_id[added.representation_id].rotation() == 90
        assert _visible_rectangles(canvas.scene) == initial_rectangles
        assert electrical_model_fingerprint(controller.model) == initial_model
    finally:
        canvas.close()


def test_bridge_route_manual_offset_uses_electrical_midpoint_and_survives_save_undo(tmp_path):
    controller, _ = crossing_fixture()
    scene = _scene(controller)
    route_id = next(identifier for identifier, item in scene._route_items_by_id.items() if _has_curve(item._display_path))
    controller.set_route_label(route_id, label_x=-65.25, label_y=-95.5)
    original_route = controller.diagram.routes[route_id]
    original_model = electrical_model_fingerprint(controller.model)
    scene.sync_document(controller.diagram, controller.model)
    item = scene._route_items_by_id[route_id]
    midpoint = _route_midpoint(original_route)
    assert (item._label.scenePos().x() - midpoint.x(), item._label.scenePos().y() - midpoint.y()) == pytest.approx((-65.25, -95.5))
    assert not hasattr(item, "_name_label") or item._name_label is item._label
    for _ in range(2):
        scene._refresh_route_bridges()
        assert (item._label.scenePos().x() - midpoint.x(), item._label.scenePos().y() - midpoint.y()) == pytest.approx((-65.25, -95.5))
        assert controller.diagram.routes[route_id] == original_route
    controller.reset_label_positions(route_ids=(route_id,))
    assert not label_is_manual(controller.diagram.routes[route_id])
    controller.undo()
    assert controller.diagram.routes[route_id] == original_route
    project = ProjectData(Network("B3 visual merge"), Methodology.load(), {"name": "B3 visual merge"},
                          ProjectStructure(), FORMAT_VERSION, controller.model, diagram=controller.diagram)
    destination = tmp_path / "b3-bridge-label.json"
    save_project(destination, project)
    restored = load_project(destination)
    assert restored.diagram.routes[route_id] == original_route
    assert electrical_model_fingerprint(restored.electrical_model) == original_model
    scene.sync_document(restored.diagram, restored.electrical_model)
    label = scene._route_items_by_id[route_id]._label
    assert (label.scenePos().x() - midpoint.x(), label.scenePos().y() - midpoint.y()) == pytest.approx((-65.25, -95.5))
