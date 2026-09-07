# -*- coding: utf-8 -*-
"""Independent B3 scene measurements, not layout-engine self-validation.

The acceptance clearance is fixed BEFORE measuring: four scene units between
the full text rectangle and another text block, apparatus ink, or drawn wire.
The 1e-6 tolerance handles floating-point boundary equality, not actual overlap.
The original demo offsets are conservatively pinned; the automatic-layout
acceptance explicitly invokes the user's undoable reset command first.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QPainterPath, QPainterPathStroker  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsItem  # noqa: E402

from rza_calc.domain.diagram import DiagramDocument, DiagramPage, DiagramRouteKind, PageId  # noqa: E402
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, LineKind, VoltageClassId  # noqa: E402
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor import NodeTarget, PhysicalLineInput, ProjectEditorController  # noqa: E402
from rza_calc.editor.state import label_is_manual  # noqa: E402
from rza_calc.gui.editor_panels import EditorWorkspaceWidget  # noqa: E402
from rza_calc.gui.editor_scene import DiagramGraphicsScene, DiagramGraphicsView  # noqa: E402
from rza_calc.io.diagram import diagram_to_dict  # noqa: E402
from rza_calc.io.project import load_project  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tests" / "fixtures" / "legacy_projects" / "energoraion.json"
MIN_CLEARANCE = 4.0
COORDINATE_EPSILON = 1e-6
DEMO_REPRESENTATIONS = 157
DEMO_VISIBLE_LABELS = 120
_APP: QApplication | None = None


@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


def _demo(*, reset=True):
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    if reset:
        controller.reset_label_positions(tuple(project.diagram.representations))
    scene = DiagramGraphicsScene()
    scene.sync_document(project.diagram, project.electrical_model)
    scene.set_label_options(show_labels=True, show_parameters=True, show_results=False)
    return project, controller, scene


def _visible_rectangles(scene):
    owners = (*scene._items_by_id.items(), *scene._route_items_by_id.items())
    return {representation_id: item._label.sceneBoundingRect()
            for representation_id, item in owners
            if item._label.isVisible() and item._label.text().strip()}


def _independent_ink_rect(item):
    # Vector primitives are the renderer's source data, not the label solver's
    # clearance geometry or the QGraphicsItem selection/hit-test halo.
    zoom = _visible_zoom(item)
    boxes = []
    for index, primitive in enumerate(item.symbol_geometry().primitives):
        left, top, right, bottom = primitive.bounds()
        rect = QRectF(left, top, right - left, bottom - top)
        if index in item._bridge_paths:
            rect = rect.united(item._bridge_paths[index].boundingRect())
        # Preserve the original conservative ink envelope, and strengthen it
        # where actual cosmetic stroke scaling extends beyond that envelope.
        margin = max(item._line_width, item._line_width * primitive.stroke_scale / (2 * zoom))
        boxes.append(rect.adjusted(-margin, -margin, margin, margin))
    assert boxes
    if item._canonical_key != "busbar":
        boxes.extend(QRectF(point.x() - 3, point.y() - 3, 6, 6) for point in item._bridge_nodes)
    radius = 3.3 + 1.25 / (2 * zoom)
    boxes.extend(QRectF(point.x() - radius, point.y() - radius, 2 * radius, 2 * radius)
                 for point in item._bus_junction_points)
    rect = boxes[0]
    for box in boxes[1:]:
        rect = rect.united(box)
    return item.mapRectToScene(rect)


def _visible_zoom(item):
    views = item.scene().views()
    return abs(views[0].transform().m11()) if views else 1.0


def _independent_wire_path(item):
    # Measure the actual painted route, including Visio bridge arcs and gaps;
    # never use label-solver obstacle helpers or the 14-unit mouse hit shape.
    path = item.mapToScene(item._display_path)
    stroker = QPainterPathStroker()
    width = 2.1 if item.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH else 1.7
    if item.isSelected():
        width += 0.9
    stroker.setWidth(width / _visible_zoom(item))
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painted = stroker.createStroke(path)
    for point in item._bridge_nodes:
        painted.addEllipse(item.mapToScene(point), 2.6, 2.6)
    return painted


def measure_scene_conflicts(scene):
    """Return independent diagnostics; reusable by evidence capture scripts."""
    labels = _visible_rectangles(scene)
    bodies = {identifier: _independent_ink_rect(item)
              for identifier, item in scene._items_by_id.items()}
    wires = {identifier: _independent_wire_path(item)
             for identifier, item in scene._route_items_by_id.items()}
    conflicts = []
    for identifier, rect in labels.items():
        margin = MIN_CLEARANCE - COORDINATE_EPSILON
        expanded = rect.adjusted(-margin, -margin, margin, margin)
        for other_id, other_rect in labels.items():
            if identifier.value < other_id.value and expanded.intersects(other_rect):
                conflicts.append(("label", identifier.value, other_id.value))
        for other_id, other_rect in bodies.items():
            if expanded.intersects(other_rect):
                conflicts.append(("body", identifier.value, other_id.value))
        for other_id, path in wires.items():
            if path.intersects(expanded) or path.contains(expanded):
                conflicts.append(("wire", identifier.value, other_id.value))
    return conflicts


def test_all_157_demo_objects_have_measured_clear_labels_after_explicit_auto_reset():
    project, controller, scene = _demo()
    assert len(scene._items_by_id) == DEMO_REPRESENTATIONS
    assert len(_visible_rectangles(scene)) == DEMO_VISIBLE_LABELS
    assert len(scene._route_items_by_id) == len(project.diagram.routes) > 0
    before = diagram_to_dict(project.diagram)
    fingerprint = electrical_model_fingerprint(project.electrical_model)
    conflicts = measure_scene_conflicts(scene)
    assert conflicts == [], f"Four-unit clearance violations: {conflicts[:30]} ({len(conflicts)} total)"
    assert scene.label_layout_conflicts() == ()
    assert diagram_to_dict(controller.diagram) == before
    assert electrical_model_fingerprint(project.electrical_model) == fingerprint


def test_auto_layout_is_repeatable_and_independent_of_mapping_insertion_order():
    project, _, scene = _demo()
    first = _visible_rectangles(scene)
    scene.relayout_labels()
    assert _visible_rectangles(scene) == first
    reversed_document = replace(project.diagram, representations=dict(reversed(tuple(project.diagram.representations.items()))))
    second = DiagramGraphicsScene()
    second.sync_document(reversed_document, project.electrical_model)
    second.set_label_options(show_labels=True, show_parameters=True, show_results=False)
    assert _visible_rectangles(second) == first
    assert measure_scene_conflicts(second) == []


def test_pinned_overlap_is_reported_without_silently_moving_the_label():
    project, controller, _ = _demo()
    identifier = next(iter(project.diagram.representations))
    controller.set_label(identifier, label_x=0, label_y=0, visible=True)
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[identifier]
    assert (item._label.pos().x(), item._label.pos().y()) == (0, 0)
    assert any(row[1] == identifier.value for row in measure_scene_conflicts(scene))
    assert scene.label_layout_conflicts(), "Manual obstruction must remain explicit, not disappear from diagnostics."
    before = diagram_to_dict(controller.diagram)
    scene.relayout_labels()
    assert (item._label.pos().x(), item._label.pos().y()) == (0, 0)
    assert diagram_to_dict(controller.diagram) == before


@pytest.mark.parametrize("zoom, expected_visible", ((0.59, False), (0.6, True), (0.61, True), (1.0, True), (0.25, False)))
def test_zoom_threshold_hides_all_labels_and_restores_them(zoom, expected_visible):
    _, _, scene = _demo()
    view = DiagramGraphicsView(scene)
    view.set_zoom(zoom)
    QApplication.processEvents()
    assert len(_visible_rectangles(scene)) == (DEMO_VISIBLE_LABELS if expected_visible else 0)
    view.actual_size()
    QApplication.processEvents()
    assert len(_visible_rectangles(scene)) == DEMO_VISIBLE_LABELS
    view.close()


def test_hidden_label_preference_is_not_overridden_by_zoom_or_master_toggle():
    project, _, scene = _demo()
    hidden_ids = {row.id for row in project.diagram.representations.values()
                  if not row.extensions["stage3_graphics"]["label_visible"]}
    assert len(hidden_ids) == 37
    view = DiagramGraphicsView(scene)
    for zoom in (0.25, 1.0, 0.6, 2.0):
        view.set_zoom(zoom)
        scene.set_label_options(show_labels=False, show_parameters=True, show_results=False)
        assert not _visible_rectangles(scene)
        scene.set_label_options(show_labels=True, show_parameters=True, show_results=False)
        assert all(not scene._items_by_id[identifier]._label.isVisible() for identifier in hidden_ids)
    view.close()


def test_toolbar_toggles_labels_and_parameters_but_cannot_fabricate_b4_results():
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    workspace = EditorWorkspaceWidget(controller)
    workspace.resize(1200, 800)
    workspace.show()
    workspace.view.actual_size()
    QApplication.processEvents()
    bar = workspace.command_bar
    before = electrical_model_fingerprint(controller.model)
    try:
        assert bar.labels_action.isCheckable() and bar.labels_action.isChecked()
        assert bar.parameters_action.isCheckable() and bar.parameters_action.isChecked()
        assert not bar.results_action.isEnabled()
        assert "B4" in bar.results_action.toolTip() or "В4" in bar.results_action.toolTip()
        full_text = {key: item._label.text() for key, item in workspace.scene._items_by_id.items()}
        bar.parameters_action.trigger()
        QApplication.processEvents()
        short_text = {key: item._label.text() for key, item in workspace.scene._items_by_id.items()}
        assert short_text != full_text, "Parameters switch must affect real labels."
        assert controller.workspace_state.show_parameters is False
        bar.labels_action.trigger()
        QApplication.processEvents()
        assert not _visible_rectangles(workspace.scene)
        assert controller.workspace_state.show_labels is False
        bar.labels_action.trigger()
        bar.parameters_action.trigger()
        QApplication.processEvents()
        assert len(_visible_rectangles(workspace.scene)) == DEMO_VISIBLE_LABELS
        assert {key: item._label.text() for key, item in workspace.scene._items_by_id.items()} == full_text
        assert electrical_model_fingerprint(controller.model) == before
    finally:
        workspace.close()


def _native_line_scene():
    project = SimpleNamespace(
        electrical_model=ElectricalModel.with_builtins("B3 native line geometry"),
        diagram=DiagramDocument.create("B3", (DiagramPage(PageId("page.b3.geometry"), "B3"),)),
        catalog_snapshots=ProjectCatalogSnapshots(),
    )
    controller = ProjectEditorController(project)
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first = controller.add_electrical_node("Начало", x=0, y=0, voltage_class_id=voltage)
    last = controller.add_electrical_node("Конец", x=600, y=0, voltage_class_id=voltage)
    line = controller.create_physical_line(
        "КЛ-1", LineKind.CABLE,
        NodeTarget(first.node_id, first.representation_id),
        NodeTarget(last.node_id, last.representation_id),
        physical=PhysicalLineInput(4_200_000, DataConfirmation.CONFIRMED,
                                   {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3,
                                    "conductor_mark": "АПвП", "cross_section_mm2": 120},
                                   DataConfirmation.CONFIRMED),
    )
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    return controller, line, scene


def test_native_physical_route_label_is_measured_against_wire_and_node_labels():
    controller, line, scene = _native_line_scene()
    assert len(_visible_rectangles(scene)) == 3  # Two nodes plus the real physical line.
    assert line.route_id in _visible_rectangles(scene)
    before = diagram_to_dict(controller.diagram)
    assert measure_scene_conflicts(scene) == []
    assert scene.label_layout_conflicts() == ()
    view = DiagramGraphicsView(scene)
    view.set_zoom(0.59)
    assert not _visible_rectangles(scene)
    view.set_zoom(0.6)
    assert len(_visible_rectangles(scene)) == 3
    assert measure_scene_conflicts(scene) == []
    assert diagram_to_dict(controller.diagram) == before
    view.close()


def test_physical_route_does_not_duplicate_an_existing_equipment_representation_label():
    controller, line, scene = _native_line_scene()
    controller.place_existing_equipment(line.section_id, x=300, y=160)
    scene.sync_document(controller.diagram, controller.model)
    assert len(scene._items_by_id) == 3
    assert not scene._route_items_by_id[line.route_id]._label.isVisible()
    assert len(_visible_rectangles(scene)) == 3


def test_toolbar_explicit_page_reset_includes_native_routes_and_is_one_undoable_command():
    controller, line, _ = _native_line_scene()
    for identifier in tuple(controller.diagram.representations):
        controller.set_label(identifier, label_x=0, label_y=-80)
    controller.set_route_label(line.route_id, label_x=90, label_y=-80)
    before = diagram_to_dict(controller.diagram)
    journal_count = len(controller.journal)
    workspace = EditorWorkspaceWidget(controller)
    try:
        workspace.command_bar.auto_labels_action.trigger()
        QApplication.processEvents()
        assert len(controller.journal) == journal_count + 1
        assert not label_is_manual(controller.diagram.routes[line.route_id])
        assert all(not label_is_manual(row) for row in controller.diagram.representations.values())
        controller.undo()
        restored = diagram_to_dict(controller.diagram)
        assert restored["representations"] == before["representations"]
        assert restored["routes"] == before["routes"]
    finally:
        workspace.close()


def test_document_resync_restores_persisted_visibility_after_a_transient_scene_override():
    project, _, scene = _demo()
    initial = {key: item._label.text() for key, item in scene._items_by_id.items()}
    before = diagram_to_dict(project.diagram)
    scene.set_label_options(show_labels=False, show_parameters=False, show_results=False)
    assert not _visible_rectangles(scene)
    scene.sync_document(project.diagram, project.electrical_model)
    assert len(_visible_rectangles(scene)) == DEMO_VISIBLE_LABELS
    assert {key: item._label.text() for key, item in scene._items_by_id.items()} == initial
    assert diagram_to_dict(project.diagram) == before


def test_editor_analysis_mode_switch_keeps_layout_but_disables_label_dragging():
    controller, _, scene = _native_line_scene()
    before = _visible_rectangles(scene)
    fingerprint = electrical_model_fingerprint(controller.model)
    for mode in ("analysis", "edit", "analysis", "edit"):
        scene.set_mode(mode)
        assert _visible_rectangles(scene) == before
        assert measure_scene_conflicts(scene) == []
        for item in (*scene._items_by_id.values(), *scene._route_items_by_id.values()):
            assert bool(item._label.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable) is (mode == "edit")
    assert electrical_model_fingerprint(controller.model) == fingerprint


def test_analysis_widget_uses_same_labels_and_persisted_toggles_without_recalculation(monkeypatch):
    from rza_calc.core import engine
    from rza_calc.gui.analysis_scheme import AnalysisSchemeView

    def forbidden_calculation(*args, **kwargs):
        raise AssertionError("Changing presentation must not rerun electrical calculations.")

    project, controller, editor_scene = _demo()
    monkeypatch.setattr(engine, "run", forbidden_calculation)
    view_model = SimpleNamespace(project=project, mode_id="")
    widget = AnalysisSchemeView(view_model)
    widget.actual_size()
    try:
        assert _visible_rectangles(widget.scene) == _visible_rectangles(editor_scene)
        assert measure_scene_conflicts(widget.scene) == []
        controller.set_label_display(show_parameters=False)
        editor_scene.sync_document(controller.diagram, controller.model)
        widget.refresh(view_model)
        assert _visible_rectangles(widget.scene) == _visible_rectangles(editor_scene)
        assert {key: item._label.text() for key, item in widget.scene._items_by_id.items()} == {
            key: item._label.text() for key, item in editor_scene._items_by_id.items()
        }
        controller.set_label_display(show_labels=False)
        widget.refresh(view_model)
        assert not _visible_rectangles(widget.scene)
    finally:
        widget.close()


def test_fast_obstacle_boolean_matches_full_diagnostics_at_boundaries_and_across_buckets():
    from rza_calc.gui.label_layout import _Obstacles

    bodies = _Obstacles()
    bodies.add("body", "body", QRectF(0, 0, 10, 10))
    body_cases = (
        (QRectF(2, 2, 1, 1), True),
        (QRectF(14, 0, 3, 3), False),  # Exactly four-unit clearance: touching is not overlap.
        (QRectF(13.999, 0, 3, 3), True),
        (QRectF(-14, 0, 10, 3), False),
        (QRectF(160, 160, 20, 20), False),
    )
    diagonal = QPainterPath()
    diagonal.moveTo(-320, -320)
    diagonal.lineTo(320, 320)
    stroker = QPainterPathStroker()
    stroker.setWidth(2)
    stroke = stroker.createStroke(diagonal)
    paths = _Obstacles()
    paths.add("diagonal", "wire", stroke.boundingRect(), stroke)
    path_cases = (
        (QRectF(250, -250, 5, 5), False),  # Inside bounds, outside the actual wire.
        (QRectF(159, 159, 3, 3), True),
        (QRectF(-162, -162, 3, 3), True),
        (QRectF(-170, -170, 340, 340), True),  # Repeated index in many buckets.
        (QRectF(500, 500, 10, 10), False),
    )
    for obstacles, cases in ((bodies, body_cases), (paths, path_cases)):
        for candidate, expected in cases:
            diagnostics = obstacles.collisions(candidate)
            assert bool(diagnostics) is expected
            assert obstacles.has_collision(candidate) is expected
            assert len(diagnostics) == len(set(diagnostics)), "Bucket duplicates must not duplicate obstacles."


def test_fast_obstacle_check_stops_after_the_first_blocking_rectangle():
    from rza_calc.gui.label_layout import _Obstacles

    visited = []

    class CountedBounds:
        def __init__(self, index, bounds):
            self.index, self.bounds = index, bounds

        def intersects(self, candidate):
            visited.append(self.index)
            return self.bounds.intersects(candidate)

    obstacles = _Obstacles()
    for index in range(64):
        obstacles.add(str(index), "body", QRectF(0, 0, 32, 32))
        key, kind, bounds, path = obstacles.rows[-1]
        obstacles.rows[-1] = (key, kind, CountedBounds(index, bounds), path)
    candidate = QRectF(5, 5, 2, 2)
    assert obstacles.has_collision(candidate)
    assert visited == [0], "A boolean rejection must not scan all 64 blocked bounds."
    visited.clear()
    assert len(obstacles.collisions(candidate)) == 64
    assert visited == list(range(64)), "The independent diagnostic path still enumerates every obstacle."


def test_optimized_layout_is_identical_to_full_diagnostic_candidate_checks(monkeypatch):
    from rza_calc.gui import label_layout

    project, _, optimized = _demo()
    expected_rectangles = _visible_rectangles(optimized)
    expected_conflicts = optimized.label_layout_conflicts()
    calls = []

    def full_check(self, rectangle):
        calls.append(1)
        return bool(self.collisions(rectangle))

    def uncached_offsets(radius):
        offsets = [(0, 0)] if radius == 0 else [
            (dx * 24.0, dy * 24.0)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if max(abs(dx), abs(dy)) == radius
        ]
        return tuple(sorted(offsets, key=lambda point: (point[0] ** 2 + point[1] ** 2, point[1], point[0])))

    monkeypatch.setattr(label_layout._Obstacles, "has_collision", full_check)
    monkeypatch.setattr(label_layout, "_ring_offsets", uncached_offsets)
    reference = DiagramGraphicsScene()
    reference.sync_document(project.diagram, project.electrical_model)
    assert calls, "Reference layout must actually exercise the full diagnostic path."
    assert _visible_rectangles(reference) == expected_rectangles
    assert reference.label_layout_conflicts() == expected_conflicts == ()
    assert measure_scene_conflicts(reference) == []
