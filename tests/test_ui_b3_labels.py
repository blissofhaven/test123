# -*- coding: utf-8 -*-
"""B3 user contracts: labels are presentation, never electrical changes.

The tests intentionally drive the real controller, saved project and Qt canvas.
Old explicitly stored offsets are user-owned even when they look like defaults.
"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument, DiagramDocumentId, DiagramPage, GraphicalRepresentation,
    GraphicalRepresentationId, PageId, RepresentationTargetKind,
)
from rza_calc.domain.electrical import (  # noqa: E402
    DataConfirmation, ElectricalModel, LineConstructionSegment,
    LineConstructionSegmentId, LineKind, VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor import NodeTarget, PhysicalLineInput, ProjectEditorController  # noqa: E402
from rza_calc.editor.labels import present_label, present_route_label  # noqa: E402
from rza_calc.editor.state import RepresentationGraphics, label_is_manual  # noqa: E402
from rza_calc.gui.editor_scene import DiagramGraphicsScene, EditorCanvas  # noqa: E402
from rza_calc.gui.editor_panels import EditorWorkspaceWidget  # noqa: E402
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict  # noqa: E402
from rza_calc.io.project import load_project, save_project  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tests" / "fixtures" / "legacy_projects" / "energoraion.json"
_APP: QApplication | None = None


@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller() -> ProjectEditorController:
    return ProjectEditorController(_Project(
        ElectricalModel.with_builtins("B3 labels"),
        DiagramDocument.create(
            "B3", (DiagramPage(PageId("page.b3.labels"), "B3"),),
            document_id=DiagramDocumentId("diagram.b3.labels"),
        ),
        ProjectCatalogSnapshots(),
    ))


def _electrical_signature(controller):
    model = controller.model
    return (
        electrical_model_fingerprint(model),
        model.revision,
        model.connectivity_signature(),
        tuple(sorted((port.id.value, port.equipment_id.value, port.role)
                     for port in model.ports.values())),
    )


def _sync(controller):
    scene = DiagramGraphicsScene()
    scene.sync_document(controller.diagram, controller.model)
    scene.set_label_options(show_labels=True, show_parameters=True, show_results=False)
    return scene


@pytest.mark.parametrize("offsets", [
    {"label_x": 0.0, "label_y": -32.0},
    {"label_x": 46.0, "label_y": -24.0},
    {"label_x": -137.5, "label_y": 81.25},
    {"label_offset_x": 17.25, "label_offset_y": -80.5},
])
def test_legacy_explicit_offsets_are_pinned_not_inferred_automatic(offsets):
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка", x=400, y=300)
    original = controller.diagram.representations[added.representation_id]
    representation = replace(original, extensions={"stage3_graphics": offsets})
    assert label_is_manual(representation), "Legacy provenance is unknowable: preserve offsets."
    document = replace(controller.diagram, representations={representation.id: representation})
    before = diagram_to_dict(document)
    scene = DiagramGraphicsScene()
    scene.sync_document(document, controller.model)
    item = scene._items_by_id[representation.id]
    expected = (
        offsets.get("label_x", offsets.get("label_offset_x")),
        offsets.get("label_y", offsets.get("label_offset_y")),
    )
    for _ in range(3):
        scene.relayout_labels()
        assert (item._label.pos().x(), item._label.pos().y()) == pytest.approx(expected)
        assert diagram_to_dict(document) == before


def test_new_labels_are_auto_but_explicit_move_is_manual_and_undoable():
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка", x=400, y=300)
    representation_id = added.representation_id
    assert not label_is_manual(controller.diagram.representations[representation_id])
    signature = _electrical_signature(controller)
    controller.set_label(representation_id, label_x=123.25, label_y=-67.5)
    assert label_is_manual(controller.diagram.representations[representation_id])
    controller.undo()
    assert not label_is_manual(controller.diagram.representations[representation_id])
    controller.redo()
    graphics = RepresentationGraphics.from_representation(controller.diagram.representations[representation_id])
    assert (graphics.label_x, graphics.label_y) == (123.25, -67.5)
    assert label_is_manual(controller.diagram.representations[representation_id])
    assert _electrical_signature(controller) == signature


def test_changing_text_or_visibility_does_not_accidentally_pin_auto_position():
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка")
    controller.set_label(added.representation_id, text="Н1", visible=False)
    representation = controller.diagram.representations[added.representation_id]
    assert representation.label == "Н1"
    assert not label_is_manual(representation)
    assert not RepresentationGraphics.from_representation(representation).label_visible


def test_explicit_reset_is_one_undoable_command_and_keeps_hidden_labels_hidden():
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    ids = tuple(controller.diagram.representations)
    assert len(ids) == 157
    assert all(label_is_manual(row) for row in controller.diagram.representations.values())
    signature = _electrical_signature(controller)
    before = diagram_to_dict(controller.diagram)
    hidden_before = {row.id for row in controller.diagram.representations.values()
                     if not RepresentationGraphics.from_representation(row).label_visible}
    journal_count = len(controller.journal)
    result = controller.reset_label_positions(ids)
    assert set(result) == set(ids)
    assert len(controller.journal) == journal_count + 1
    assert all(not label_is_manual(row) for row in controller.diagram.representations.values())
    assert {row.id for row in controller.diagram.representations.values()
            if not RepresentationGraphics.from_representation(row).label_visible} == hidden_before
    controller.undo()
    # Document revisions may advance on undo; all stored representation data must return.
    assert diagram_to_dict(controller.diagram)["representations"] == before["representations"]
    controller.redo()
    assert all(not label_is_manual(row) for row in controller.diagram.representations.values())
    assert _electrical_signature(controller) == signature


def test_manual_offsets_and_display_options_survive_real_save_and_reopen(tmp_path):
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    representation_id = next(iter(controller.diagram.representations))
    controller.set_label(representation_id, label_x=173.25, label_y=-84.5, text="Ручная подпись")
    controller.set_label_display(show_labels=True, show_parameters=False, show_results=False)
    signature = _electrical_signature(controller)
    target = tmp_path / "manual-label-project.json"
    save_project(target, project)
    restored = load_project(target)
    restored_controller = ProjectEditorController(restored)
    row = restored.diagram.representations[representation_id]
    assert row.label == "Ручная подпись"
    assert label_is_manual(row)
    graphics = RepresentationGraphics.from_representation(row)
    assert (graphics.label_x, graphics.label_y) == (173.25, -84.5)
    assert restored_controller.workspace_state.show_labels is True
    assert restored_controller.workspace_state.show_parameters is False
    assert restored_controller.workspace_state.show_results is False
    assert _electrical_signature(restored_controller) == signature


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_rotated_label_text_is_horizontal_without_changing_permanent_port_roles(angle):
    controller = _controller()
    added = controller.add_equipment("builtin.transformer_3w", "Т1", x=400, y=300)
    controller.set_label(added.representation_id, label_x=103.25, label_y=-75.5)
    before = _electrical_signature(controller)
    controller.rotate_representation(added.representation_id, angle)
    scene = _sync(controller)
    item = scene._items_by_id[added.representation_id]
    transform = item._label.sceneTransform()
    assert (transform.m11(), transform.m12(), transform.m21(), transform.m22()) == pytest.approx((1, 0, 0, 1))
    assert item._label.parentItem() is item
    assert (item._label.pos().x(), item._label.pos().y()) == pytest.approx((103.25, -75.5))
    assert _electrical_signature(controller) == before


def test_moving_owner_keeps_manual_label_attached_and_does_not_change_topology():
    controller = _controller()
    added = controller.add_equipment("builtin.recloser", "РКЛ1", x=400, y=300)
    controller.set_label(added.representation_id, label_x=80, label_y=-70)
    scene = _sync(controller)
    label = scene._items_by_id[added.representation_id]._label
    origin = label.scenePos()
    before = _electrical_signature(controller)
    controller.move_representations((added.representation_id,), 100, 80)
    scene.sync_document(controller.diagram, controller.model)
    moved = scene._items_by_id[added.representation_id]._label.scenePos()
    assert (moved.x() - origin.x(), moved.y() - origin.y()) == pytest.approx((100, 80))
    controller.undo()
    scene.sync_document(controller.diagram, controller.model)
    restored = scene._items_by_id[added.representation_id]._label.scenePos()
    assert (restored.x(), restored.y()) == pytest.approx((origin.x(), origin.y()))
    assert _electrical_signature(controller) == before


@pytest.mark.parametrize("angle, local_delta", ((0, (40, 20)), (90, (20, -40)), (180, (-40, -20)), (270, (-20, 40))))
def test_dragging_label_uses_real_qt_events_and_persists_only_presentation(angle, local_delta):
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка", x=400, y=300)
    controller.set_label(added.representation_id, label_x=100, label_y=-80)
    controller.rotate_representation(added.representation_id, angle)
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 700)
    canvas.show()
    QApplication.processEvents()
    canvas.view.actual_size()
    canvas.view.centerOn(400, 300)
    QApplication.processEvents()
    before = _electrical_signature(controller)
    journal_before = len(controller.journal)
    owner = canvas.scene._items_by_id[added.representation_id]
    owner_position = QPointF(owner.pos())
    label = owner._label
    start = canvas.view.mapFromScene(label.mapToScene(label.boundingRect().center()))
    finish = start + canvas.view.mapFromScene(QPointF(40, 20)) - canvas.view.mapFromScene(QPointF(0, 0))
    try:
        QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas.view.viewport(), finish, delay=30)
        QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=finish)
        QApplication.processEvents()
        graphics = RepresentationGraphics.from_representation(controller.diagram.representations[added.representation_id])
        assert (graphics.label_x, graphics.label_y) == pytest.approx((100 + local_delta[0], -80 + local_delta[1]), abs=1)
        assert label_is_manual(controller.diagram.representations[added.representation_id])
        assert len(controller.journal) == journal_before + 1
        assert owner.pos() == owner_position, "Dragging a label must not move its apparatus."
        assert _electrical_signature(controller) == before
    finally:
        canvas.close()


def test_auto_placement_and_display_do_not_mutate_serialized_document():
    controller = _controller()
    controller.add_equipment("builtin.load", "Нагрузка", x=400, y=300)
    before = diagram_to_dict(controller.diagram)
    scene = _sync(controller)
    for parameters in (False, True, False, True):
        scene.set_label_options(show_labels=True, show_parameters=parameters, show_results=False)
        scene.relayout_labels()
        scene.sync_document(controller.diagram, controller.model)
    assert diagram_to_dict(controller.diagram) == before
    assert diagram_to_dict(diagram_from_dict(before)) == before


@pytest.mark.parametrize("type_id, properties, parameter", (
    ("builtin.transformer_2w", {"s_nom": 16000, "u_hv": 110, "u_lv": 10}, "16 МВ·А · 110/10 кВ"),
    ("builtin.transformer_3w", {"s_nom": 25000, "u_hv": 110, "u_mv": 35, "u_lv": 10}, "25 МВ·А · 110/35/10 кВ"),
    ("builtin.cable", {"brand": "АПвП", "section_mm2": 120, "length_km": 4.2}, "АПвП · 120 мм² · 4,2 км"),
    ("builtin.line", {"conductor_mark": "АС-70", "cross_section_mm2": 70, "length_km": 6.2}, "АС-70 · 70 мм² · 6,2 км"),
    ("builtin.circuit_breaker", {"rated_current_a": 1000}, "1000 А"),
    ("builtin.recloser", {"rated_current_a": 630}, "630 А"),
    ("builtin.load", {"p_kw": 250}, "250 кВт"),
    ("builtin.load", {"p_kw": 1500}, "1,5 МВт"),
    ("builtin.load", {"p_kw": 0}, "0 кВт"),
    ("builtin.generator", {"p_nom": 5}, "5 МВт"),
))
def test_native_main_parameters_have_honest_units_and_do_not_change_inputs(type_id, properties, parameter):
    controller = _controller()
    added = controller.add_equipment(type_id, "Имя", properties=properties)
    representation = controller.diagram.representations[added.representation_id]
    before = _electrical_signature(controller)
    content = present_label(controller.model, representation)
    assert content.name == "Имя"
    assert content.parameter == parameter
    assert content.text() == f"Имя · {parameter}"
    assert content.text(show_parameters=False) == "Имя"
    assert content.text(show_name=False) == parameter
    assert content.text(show_name=False, show_parameters=False) == ""
    assert _electrical_signature(controller) == before


@pytest.mark.parametrize("legacy_id, expected", (
    ("CT1", "25 МВ·А · 110/35/10 кВ"),
    ("CT2", "16 МВ·А · 110/10 кВ"),
    ("CF1", "АПвПу-10 · 150 мм² · 1,2 км"),
    ("NF3", "АС-70 · 70 мм² · 6,2 км"),
))
def test_saved_legacy_main_parameters_use_the_same_local_reader(legacy_id, expected):
    project = load_project(DEMO)
    representation = next(row for row in project.diagram.representations.values()
                          if row.equipment_id and project.electrical_model.equipment[row.equipment_id]
                          .extensions.get("legacy_calculation", {}).get("legacy_id") == legacy_id)
    fingerprint = electrical_model_fingerprint(project.electrical_model)
    assert present_label(project.electrical_model, representation).parameter == expected
    assert electrical_model_fingerprint(project.electrical_model) == fingerprint


@pytest.mark.parametrize("type_id, properties, expected", (
    ("builtin.transformer_2w", {}, "Sном не задана · Uном не задано"),
    ("builtin.transformer_3w", {"s_nom": 25000, "u_hv": 110, "u_lv": 10}, "25 МВ·А · Uном не задано"),
    ("builtin.circuit_breaker", {}, "Iном не задан"),
    ("builtin.circuit_breaker", {"rated_current_a": True}, "Iном не задан"),
    ("builtin.circuit_breaker", {"rated_current_a": "630"}, "Iном не задан"),
    ("builtin.circuit_breaker", {"rated_current_a": -630}, "Iном не задан"),
    ("builtin.load", {}, "P не задана"),
    ("builtin.generator", {"s_nom": 10, "cosphi": 0.8}, "Pном не задан"),
    ("builtin.cable", {"section_mm2": 120}, "марка не задана · 120 мм² · длина не задана"),
))
def test_missing_or_invalid_ratings_are_not_synthesized(type_id, properties, expected):
    controller = _controller()
    added = controller.add_equipment(type_id, "Имя", properties=properties)
    representation = controller.diagram.representations[added.representation_id]
    assert present_label(controller.model, representation).parameter == expected


def test_cable_cross_section_does_not_invent_core_or_parallel_count():
    controller = _controller()
    added = controller.add_equipment("builtin.cable", "КЛ", properties={
        "brand": "АПвП", "section_mm2": 120, "length_km": 4.2, "parallel_count": 3,
    })
    content = present_label(controller.model, controller.diagram.representations[added.representation_id])
    assert content.parameter == "АПвП · 120 мм² · 4,2 км"
    assert "3×" not in content.parameter and "3х" not in content.parameter


def test_conflicting_conductor_aliases_are_exposed_instead_of_arbitrarily_selected():
    controller = _controller()
    added = controller.add_equipment("builtin.cable", "КЛ", properties={
        "brand": "АПвП", "conductor_mark": "АС-70", "length_km": 4.2,
    })
    content = present_label(controller.model, controller.diagram.representations[added.representation_id])
    assert content.parameter == "параметры недоступны"
    assert "противоречат" in content.tooltip


def _native_physical_line():
    controller = _controller()
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first = controller.add_electrical_node("Начало", x=0, y=0, voltage_class_id=voltage)
    second = controller.add_electrical_node("Конец", x=600, y=0, voltage_class_id=voltage)
    line = controller.create_physical_line(
        "КЛ-1", LineKind.CABLE,
        NodeTarget(first.node_id, first.representation_id),
        NodeTarget(second.node_id, second.representation_id),
        physical=PhysicalLineInput(4_200_000, DataConfirmation.CONFIRMED,
                                   {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
                                   DataConfirmation.CONFIRMED),
        inherited_properties={"conductor_mark": "АПвП", "cross_section_mm2": 120},
    )
    # This temporary presentation input is NOT added to the real diagram;
    # route-owned labels are separately checked through the actual canvas.
    representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.b3.reader-only"), first.page_id,
        RepresentationTargetKind.EQUIPMENT, equipment_id=line.section_id, label="КЛ-1",
    )
    return controller, line, representation


def test_native_line_label_resolves_inherited_parameters_and_physical_length():
    controller, _, representation = _native_physical_line()
    before = _electrical_signature(controller)
    assert present_label(controller.model, representation).parameter == "АПвП · 120 мм² · 4,2 км"
    assert _electrical_signature(controller) == before


def test_mixed_construction_segments_do_not_masquerade_as_the_first_conductor():
    controller, line, representation = _native_physical_line()
    segments = (
        LineConstructionSegment(LineConstructionSegmentId("segment.b3.first"), LineKind.CABLE,
                                1_200_000, {"conductor_mark": "АПвП", "cross_section_mm2": 120}),
        LineConstructionSegment(LineConstructionSegmentId("segment.b3.second"), LineKind.OVERHEAD,
                                3_000_000, {"conductor_mark": "АС-70", "cross_section_mm2": 70}),
    )
    controller.model.replace_line_construction_segments(line.section_id, segments)
    before = _electrical_signature(controller)
    content = present_label(controller.model, representation)
    assert content.parameter == "составная линия: 2 уч. · 4,2 км"
    assert "АПвП · 120 мм² · 1,2 км" in content.tooltip
    assert "АС-70 · 70 мм² · 3 км" in content.tooltip
    assert _electrical_signature(controller) == before


@pytest.mark.parametrize("length, confirmation, expected", (
    (4_200_000, DataConfirmation.UNCONFIRMED, "4,2 км (не подтверждена)"),
    (None, DataConfirmation.UNCONFIRMED, "длина не задана"),
))
def test_native_line_unconfirmed_or_missing_length_is_not_presented_as_confirmed(length, confirmation, expected):
    controller, line, representation = _native_physical_line()
    segment = controller.model.line_sections[line.section_id].construction_segments[0]
    controller.model.replace_line_construction_segments(line.section_id, (
        replace(segment, length_mm=length, length_confirmation=confirmation),
    ))
    content = present_label(controller.model, representation)
    assert content.parameter == f"АПвП · 120 мм² · {expected}"


def _route_midpoint(route):
    segments = tuple(zip(route.waypoints, route.waypoints[1:]))
    lengths = [math.hypot(second.x - first.x, second.y - first.y) for first, second in segments]
    remaining = sum(lengths) / 2.0
    for (first, second), length in zip(segments, lengths):
        if remaining <= length:
            part = remaining / length if length else 0
            return QPointF(first.x + (second.x - first.x) * part,
                           first.y + (second.y - first.y) * part)
        remaining -= length
    raise AssertionError("A physical route must have nonzero segments.")


def test_real_native_line_route_gets_a_parameter_label_without_synthetic_representations():
    controller, line, _ = _native_physical_line()
    assert len(controller.diagram.representations) == 2  # Only endpoint nodes.
    assert all(row.equipment_id != line.section_id for row in controller.diagram.representations.values())
    before = diagram_to_dict(controller.diagram)
    electrical = _electrical_signature(controller)
    scene = _sync(controller)
    route = controller.diagram.routes[line.route_id]
    content = present_route_label(controller.model, route)
    assert content.name == "КЛ-1"
    assert content.parameter == "АПвП · 120 мм² · 4,2 км"
    label = scene._route_items_by_id[line.route_id]._label
    assert label.isVisible()
    assert "КЛ-1" in label.text()
    assert "4,2 км" in label.text()
    assert len(scene._items_by_id) == 2
    assert diagram_to_dict(controller.diagram) == before
    assert _electrical_signature(controller) == electrical


def test_native_manual_route_label_offsets_keep_waypoints_and_ids_unchanged_and_reset_undoable():
    controller, line, _ = _native_physical_line()
    before = controller.diagram.routes[line.route_id]
    electrical = _electrical_signature(controller)
    controller.set_route_label(line.route_id, label_x=81.25, label_y=-63.5)
    manual = controller.diagram.routes[line.route_id]
    assert label_is_manual(manual)
    assert replace(manual, extensions=before.extensions) == before
    scene = _sync(controller)
    label = scene._route_items_by_id[line.route_id]._label
    origin = _route_midpoint(manual)
    assert (label.scenePos().x() - origin.x(), label.scenePos().y() - origin.y()) == pytest.approx((81.25, -63.5))
    journal_count = len(controller.journal)
    changed = controller.reset_label_positions(route_ids=(line.route_id,))
    assert changed == (line.route_id,)
    assert len(controller.journal) == journal_count + 1
    assert not label_is_manual(controller.diagram.routes[line.route_id])
    controller.undo()
    assert controller.diagram.routes[line.route_id] == manual
    controller.redo()
    assert not label_is_manual(controller.diagram.routes[line.route_id])
    assert _electrical_signature(controller) == electrical


def test_native_manual_route_label_follows_changed_geometry_not_an_old_screen_point():
    controller, line, _ = _native_physical_line()
    controller.set_route_label(line.route_id, label_x=80, label_y=-64)
    before = _electrical_signature(controller)
    scene = _sync(controller)
    original_waypoints = controller.diagram.routes[line.route_id].waypoints
    controller.move_representations(tuple(controller.diagram.representations), 100, 100)
    moved = controller.diagram.routes[line.route_id]
    assert moved.waypoints != original_waypoints
    scene.sync_document(controller.diagram, controller.model)
    midpoint = _route_midpoint(moved)
    position = scene._route_items_by_id[line.route_id]._label.scenePos()
    assert (position.x() - midpoint.x(), position.y() - midpoint.y()) == pytest.approx((80, -64))
    controller.undo()
    scene.sync_document(controller.diagram, controller.model)
    assert controller.diagram.routes[line.route_id].waypoints == original_waypoints
    midpoint = _route_midpoint(controller.diagram.routes[line.route_id])
    position = scene._route_items_by_id[line.route_id]._label.scenePos()
    assert (position.x() - midpoint.x(), position.y() - midpoint.y()) == pytest.approx((80, -64))
    assert _electrical_signature(controller) == before


def test_native_manual_route_label_survives_real_project_save_and_reopen(tmp_path):
    from rza_calc.core.methodology import Methodology
    from rza_calc.domain import ProjectStructure
    from rza_calc.io.project import FORMAT_VERSION, ProjectData
    from rza_calc.core.model import Network

    controller, line, _ = _native_physical_line()
    controller.set_route_label(line.route_id, label_x=-42.25, label_y=73.5)
    project = ProjectData(Network("B3"), Methodology.load(), {"name": "B3"},
                          ProjectStructure(), FORMAT_VERSION, controller.model,
                          diagram=controller.diagram)
    electrical = _electrical_signature(controller)
    original_route = controller.diagram.routes[line.route_id]
    target = tmp_path / "native-physical-line-label.json"
    save_project(target, project)
    restored = load_project(target)
    restored_route = restored.diagram.routes[line.route_id]
    assert restored_route == original_route
    assert label_is_manual(restored_route)
    assert _electrical_signature(ProjectEditorController(restored)) == electrical
    scene = DiagramGraphicsScene()
    scene.sync_document(restored.diagram, restored.electrical_model)
    anchor = _route_midpoint(restored_route)
    position = scene._route_items_by_id[line.route_id]._label.scenePos()
    assert (position.x() - anchor.x(), position.y() - anchor.y()) == pytest.approx((-42.25, 73.5))


def test_real_qt_drag_of_native_route_label_does_not_edit_wire_or_electrical_model():
    controller, line, _ = _native_physical_line()
    controller.set_route_label(line.route_id, label_x=50, label_y=-80)
    before = controller.diagram.routes[line.route_id]
    electrical = _electrical_signature(controller)
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 700)
    canvas.show()
    canvas.view.actual_size()
    canvas.view.centerOn(300, 0)
    QApplication.processEvents()
    label = canvas.scene._route_items_by_id[line.route_id]._label
    first = canvas.view.mapFromScene(label.mapToScene(label.boundingRect().center()))
    last = first + QPointF(40, 20).toPoint()
    try:
        QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(canvas.view.viewport(), last, delay=30)
        QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=last)
        QApplication.processEvents()
        after = controller.diagram.routes[line.route_id]
        graphics = after.extensions["stage3_graphics"]
        assert (graphics["label_x"], graphics["label_y"]) == pytest.approx((90, -60), abs=1)
        assert after.waypoints == before.waypoints
        assert replace(after, extensions=before.extensions) == before
        assert _electrical_signature(controller) == electrical
    finally:
        canvas.close()


@pytest.mark.parametrize("container", ("stage3_graphics", "graphics", None))
def test_editing_legacy_label_does_not_materialize_symbol_size_or_move_semantic_ports(container):
    controller = _controller()
    added = controller.add_equipment("builtin.transformer_3w", "Т1", x=400, y=300)
    representation = controller.diagram.representations[added.representation_id]
    original_graphics = {"label_offset_x": 17.25, "label_offset_y": -84.5,
                         "line_width": 1.5, "custom_style": {"review_note": "keep me"}}
    extensions = original_graphics if container is None else {container: original_graphics}
    representation = replace(representation, extensions=extensions)
    project = controller._project
    project.diagram = replace(project.diagram, representations={representation.id: representation})
    controller = ProjectEditorController(project)
    scene = _sync(controller)
    item = scene._items_by_id[representation.id]
    ports_before = {port_id: (port.scenePos().x(), port.scenePos().y()) for port_id, port in item._port_items.items()}
    size_before = item._width, item._height
    electrical = _electrical_signature(controller)
    controller.set_label(representation.id, label_x=141.25, label_y=-62.5)
    updated = controller.diagram.representations[representation.id]
    values = updated.extensions if container is None else updated.extensions[container]
    assert "width" not in values and "height" not in values
    assert values["custom_style"] == original_graphics["custom_style"]
    assert values["label_offset_x"] == 17.25 and values["label_offset_y"] == -84.5
    if container != "stage3_graphics":
        assert "stage3_graphics" not in updated.extensions, "Do not shadow an existing legacy graphics container."
    scene.sync_document(controller.diagram, controller.model)
    item = scene._items_by_id[representation.id]
    assert (item._width, item._height) == size_before
    assert {port_id: (port.scenePos().x(), port.scenePos().y()) for port_id, port in item._port_items.items()} == ports_before
    assert _electrical_signature(controller) == electrical


@pytest.mark.parametrize("key, value, manual", (
    ("graphics.label", "Новое имя подписи", False),
    ("graphics.label_visible", False, False),
    ("graphics.label_x", 125.5, True),
    ("graphics.label_y", -81.25, True),
))
def test_inspector_only_pins_labels_when_the_user_actually_edits_coordinates(key, value, manual):
    controller = _controller()
    added = controller.add_equipment("builtin.load", "Нагрузка", x=400, y=300)
    workspace = EditorWorkspaceWidget(controller)
    before = _electrical_signature(controller)
    try:
        workspace.scene.select_representations((added.representation_id,))
        workspace._edit_property(key, value)
        representation = controller.diagram.representations[added.representation_id]
        assert label_is_manual(representation) is manual
        if key == "graphics.label":
            assert representation.label == value
        else:
            assert representation.extensions["stage3_graphics"][key.removeprefix("graphics.")] == value
        assert _electrical_signature(controller) == before
    finally:
        workspace.close()
