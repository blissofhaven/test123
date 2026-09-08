# -*- coding: utf-8 -*-
"""GUI-контракт выбора, one-shot вставки, поворота и коллизий UI-UX-1."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsSceneHoverEvent, QMessageBox  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentationId,
    PageId,
)
from rza_calc.domain.electrical import (  # noqa: E402
    DataConfirmation,
    ElectricalModel,
    LineKind,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor import EditorMode, EditorTool, ProjectEditorController  # noqa: E402
from rza_calc.editor.controller import NodeTarget, PhysicalLineInput  # noqa: E402
from rza_calc.editor.collision import DiagramCollisionService  # noqa: E402
from rza_calc.editor.orientation import OrientationMode  # noqa: E402
from rza_calc.editor.state import orientation_mode_for_representation  # noqa: E402
from rza_calc.gui.editor_panels import (  # noqa: E402
    EditorWorkspaceWidget,
    EquipmentLibraryTree,
)
from rza_calc.gui.editor_scene import DiagramGraphicsView, EditorCanvas  # noqa: E402
from rza_calc.gui.strings import ui_text  # noqa: E402


_APP: QApplication | None = None
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller(token: str = "main") -> ProjectEditorController:
    page = DiagramPage(PageId(f"page.ui.interaction.{token}"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins("UI-UX-1"),
        DiagramDocument.create(
            "Проверка UI-UX-1",
            (page,),
            document_id=DiagramDocumentId(f"diagram.ui.interaction.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def _physical_line(
    controller: ProjectEditorController,
    start: tuple[float, float],
    end: tuple[float, float],
):
    first = controller.add_electrical_node(
        "Начало", x=start[0], y=start[1], voltage_class_id=U10
    )
    second = controller.add_electrical_node(
        "Конец", x=end[0], y=end[1], voltage_class_id=U10
    )
    return controller.create_physical_line(
        "ВЛ-10 кВ",
        LineKind.OVERHEAD,
        NodeTarget(first.node_id, first.representation_id),
        NodeTarget(second.node_id, second.representation_id),
        physical=PhysicalLineInput(
            1_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )


def _show_canvas(controller: ProjectEditorController) -> EditorCanvas:
    canvas = EditorCanvas(controller)
    canvas.resize(920, 640)
    canvas.show()
    QApplication.processEvents()
    canvas.view.actual_size()
    canvas.view.centerOn(300.0, 260.0)
    canvas.view.setFocus()
    return canvas


def _click_scene(
    canvas: EditorCanvas,
    x: float,
    y: float,
    *,
    double: bool = False,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    point = canvas.view.mapFromScene(QPointF(x, y))
    action = QTest.mouseDClick if double else QTest.mouseClick
    action(
        canvas.view.viewport(),
        Qt.MouseButton.LeftButton,
        modifiers,
        pos=point,
    )
    QApplication.processEvents()


def _library_item(workspace: EditorWorkspaceWidget, title: str):
    tree = workspace.side_panel.library

    def visit(item):
        if item.text(0) == title and isinstance(
            item.data(0, EquipmentLibraryTree.PAYLOAD_ROLE), dict
        ):
            return item
        for index in range(item.childCount()):
            found = visit(item.child(index))
            if found is not None:
                return found
        return None

    for index in range(tree.topLevelItemCount()):
        found = visit(tree.topLevelItem(index))
        if found is not None:
            return found
    raise AssertionError(f"В библиотеке нет элемента «{title}».")


def _click_library(workspace: EditorWorkspaceWidget, title: str) -> None:
    tree = workspace.side_panel.library
    item = _library_item(workspace, title)
    tree.scrollToItem(item)
    QApplication.processEvents()
    QTest.mouseClick(
        tree.viewport(),
        Qt.MouseButton.LeftButton,
        pos=tree.visualItemRect(item).center(),
    )
    QApplication.processEvents()


def test_single_click_selects_repeated_click_keeps_and_empty_clears() -> None:
    _app()
    controller = _controller("selection")
    added = controller.add_equipment("builtin.load", "Нагрузка", x=100, y=100)
    canvas = _show_canvas(controller)
    try:
        _click_scene(canvas, 100, 100)
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)

        _click_scene(canvas, 100, 100)
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)

        _click_scene(canvas, 500, 400)
        assert canvas.scene.selected_representation_ids() == ()
    finally:
        canvas.close()


def test_double_click_requests_properties_once_and_keeps_selection() -> None:
    _app()
    controller = _controller("double")
    added = controller.add_equipment("builtin.transformer_2w", "Т1", x=100, y=100)
    canvas = _show_canvas(controller)
    requested: list[object] = []
    canvas.equipmentDetailsRequested.connect(requested.append)
    try:
        _click_scene(canvas, 100, 100, double=True)
        assert requested == [added.representation_id]
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)
        assert len(controller.journal) == 1  # Только исходное создание объекта.
    finally:
        canvas.close()


def test_shift_ctrl_and_ctrl_a_manage_multiple_selection() -> None:
    _app()
    controller = _controller("multiple-selection")
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=100, y=100)
    second = controller.add_equipment("builtin.load", "Нагрузка 2", x=260, y=100)
    canvas = _show_canvas(controller)
    try:
        _click_scene(canvas, 100, 100)
        _click_scene(
            canvas,
            260,
            100,
            modifiers=Qt.KeyboardModifier.ShiftModifier,
        )
        assert set(canvas.scene.selected_representation_ids()) == {
            first.representation_id,
            second.representation_id,
        }
        assert canvas.scene._group_selection_overlay.isVisible()

        _click_scene(
            canvas,
            100,
            100,
            modifiers=Qt.KeyboardModifier.ControlModifier,
        )
        assert canvas.scene.selected_representation_ids() == (
            second.representation_id,
        )

        QTest.keyClick(
            canvas.view,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.ControlModifier,
        )
        QApplication.processEvents()
        assert set(canvas.scene.selected_representation_ids()) == {
            first.representation_id,
            second.representation_id,
        }
    finally:
        canvas.close()


def test_library_single_click_places_once_and_following_clicks_do_not_copy() -> None:
    _app()
    controller = _controller("once")
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    workspace.canvas.view.actual_size()
    workspace.canvas.view.centerOn(300.0, 260.0)
    try:
        _click_library(workspace, ui_text("equipment.circuit_breaker"))
        assert workspace.canvas.view.tool_state.tool is EditorTool.PLACE_EQUIPMENT_ONCE

        _click_scene(workspace.canvas, 100, 100)
        assert len(controller.model.equipment) == 1
        assert workspace.canvas.view.tool_state.tool is EditorTool.SELECT
        assert len(workspace.canvas.scene.selected_representation_ids()) == 1

        for x, y in ((250, 80), (350, 120), (450, 160), (550, 200), (650, 240)):
            _click_scene(workspace.canvas, x, y)
        assert len(controller.model.equipment) == 1

        workspace.canvas.undo()
        assert controller.model.equipment == {}
    finally:
        workspace.close()


def test_repeat_placement_requires_explicit_pin_and_escape_finishes() -> None:
    _app()
    controller = _controller("repeat")
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    workspace.canvas.view.actual_size()
    workspace.canvas.view.centerOn(300.0, 260.0)
    try:
        workspace.command_bar.repeat_placement_action.setChecked(True)
        _click_library(workspace, ui_text("equipment.load"))
        assert workspace.canvas.view.tool_state.tool is EditorTool.PLACE_EQUIPMENT_REPEAT

        for x in (80.0, 220.0, 360.0):
            _click_scene(workspace.canvas, x, 100.0)
        assert len(controller.model.equipment) == 3
        assert workspace.canvas.view.tool_state.tool is EditorTool.PLACE_EQUIPMENT_REPEAT

        QTest.keyClick(workspace.canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert workspace.canvas.view.tool_state.tool is EditorTool.SELECT
        _click_scene(workspace.canvas, 500.0, 100.0)
        assert len(controller.model.equipment) == 3
    finally:
        workspace.close()


def test_collision_proposes_nearest_free_and_one_shot_commits_once() -> None:
    _app()
    controller = _controller("placement-collision")
    controller.add_equipment("builtin.transformer_2w", "Т1", x=100, y=100)
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.transformer_2w",
        "name": "Т2",
    }
    before = electrical_model_fingerprint(controller.model)
    before_journal = len(controller.journal)
    before_diagram = controller.diagram
    try:
        canvas.view.begin_placement(payload)
        canvas.view._update_equipment_drop_feedback(payload, QPointF(100, 100))
        proposal = canvas.view._placement_proposal
        assert proposal.valid and proposal.adjusted
        assert (proposal.x, proposal.y) != (100, 100)
        geometry = canvas.view._equipment_preview_geometry(payload, QPointF(proposal.x, proposal.y),
                                                           rotation_deg=proposal.rotation_deg)
        assert DiagramCollisionService(controller.diagram, controller.model).check_placement(geometry).allowed
        assert electrical_model_fingerprint(controller.model) == before
        assert len(controller.journal) == before_journal
        _click_scene(canvas, 100, 100)
        assert len(controller.model.equipment) == 2
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        created = next(row for rid, row in controller.diagram.representations.items()
                       if rid not in before_diagram.representations)
        assert (created.x, created.y, created.rotation_deg) == (proposal.x, proposal.y, proposal.rotation_deg)
        assert all(controller.diagram.representations[rid] == row
                   for rid, row in before_diagram.representations.items())
        assert len(controller.journal) == before_journal + 1
        _click_scene(canvas, 300, 100)
        assert len(controller.model.equipment) == 2
        assert len(controller.journal) == before_journal + 1
        canvas.undo()
        assert controller.diagram == before_diagram
        assert electrical_model_fingerprint(controller.model) == before
    finally:
        canvas.close()


def test_r_rotates_preview_without_creating_domain_object() -> None:
    _app()
    controller = _controller("preview-rotate")
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.circuit_breaker",
        "name": "QF preview",
    }
    try:
        canvas.view.begin_placement(payload)
        before_revision = controller.model.revision
        before_journal = tuple(controller.journal)
        QTest.keyClick(canvas.view, Qt.Key.Key_R)
        QApplication.processEvents()
        assert canvas.view._placement_rotation_deg == 180
        assert controller.model.equipment == {}
        assert controller.model.revision == before_revision
        assert tuple(controller.journal) == before_journal

        QTest.keyClick(canvas.view, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
        QApplication.processEvents()
        assert canvas.view._placement_rotation_deg == 90
    finally:
        canvas.close()


def test_rotated_preview_is_committed_as_manual_orientation() -> None:
    _app()
    controller = _controller("preview-manual-commit")
    canvas = _show_canvas(controller)
    try:
        canvas.view.begin_placement({
            "target_kind": "equipment",
            "type_id": "builtin.circuit_breaker",
            "name": "QF preview",
        })
        QTest.keyClick(canvas.view, Qt.Key.Key_R)
        _click_scene(canvas, 100.0, 100.0)
        representation_id = canvas.scene.selected_representation_ids()[0]
        representation = controller.diagram.representations[representation_id]
        assert representation.rotation_deg == 180.0
        assert (
            orientation_mode_for_representation(representation)
            is OrientationMode.MANUAL
        )
    finally:
        canvas.close()


@pytest.mark.parametrize(
    ("start", "end", "point", "expected"),
    (
        ((400.0, 100.0), (0.0, 100.0), (200.0, 100.0), 180),
        ((100.0, 400.0), (100.0, 0.0), (100.0, 200.0), 90),
    ),
)
def test_gui_preview_maps_directed_inline_axis_to_allowed_orientation(
    start: tuple[float, float],
    end: tuple[float, float],
    point: tuple[float, float],
    expected: int,
) -> None:
    _app()
    controller = _controller(f"directed-preview-{expected}")
    _physical_line(controller, start, end)
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.recloser",
        "name": "Реклоузер",
    }
    try:
        canvas.view.begin_placement(payload)
        canvas.view._update_equipment_drop_feedback(
            canvas.view._placement_payload,
            QPointF(*point),
        )
        assert canvas.view._drop_feedback_kind is not None
        assert canvas.view._placement_rotation_deg == expected
    finally:
        canvas.close()


def test_branch_preview_commit_position_and_generator_auto_orientation(
    monkeypatch,
) -> None:
    _app()
    controller = _controller("branch-preview-position")
    _physical_line(controller, (0.0, 0.0), (400.0, 0.0))
    canvas = _show_canvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 500_000),
    )
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.generator",
        "name": "Генератор на отпайке",
    }
    try:
        canvas.view.begin_placement(payload)
        canvas.view._update_equipment_drop_feedback(
            canvas.view._placement_payload,
            QPointF(200.0, 0.0),
        )
        preview_point = canvas.view._drop_equipment_point
        assert preview_point == QPointF(200.0, 140.0)
        assert canvas.view._placement_rotation_deg == 180
        assert canvas.view._placement_valid

        canvas._add_equipment(
            canvas.view._placement_payload,
            200.0,
            0.0,
        )
        generator = next(
            item
            for item in controller.model.equipment.values()
            if item.type_id.value == "builtin.generator"
        )
        representation = controller.diagram.representations_for_equipment(
            generator.id
        )[0]
        assert (representation.x, representation.y) == (
            preview_point.x(),
            preview_point.y(),
        )
        assert representation.rotation_deg == 180.0
        assert (
            orientation_mode_for_representation(representation)
            is OrientationMode.AUTO
        )
    finally:
        canvas.close()


def test_branch_collision_uses_real_future_equipment_position() -> None:
    _app()
    controller = _controller("branch-real-position-collision")
    _physical_line(controller, (0.0, 0.0), (400.0, 0.0))
    obstacle = controller.add_equipment(
        "builtin.load",
        "Препятствие",
        x=200.0,
        y=140.0,
    )
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.generator",
        "name": "Заблокированный генератор",
    }
    before_equipment = frozenset(controller.model.equipment)
    before_journal = len(controller.journal)
    try:
        canvas.view.begin_placement(payload)
        canvas.view._update_equipment_drop_feedback(
            canvas.view._placement_payload,
            QPointF(200.0, 0.0),
        )
        assert canvas.view._drop_equipment_point == QPointF(200.0, 140.0)
        assert canvas.view._placement_valid is False
        assert canvas.view._placement_conflict_name == "Препятствие"

        canvas._add_equipment(
            canvas.view._placement_payload,
            200.0,
            0.0,
        )
        assert frozenset(controller.model.equipment) == before_equipment
        assert len(controller.journal) == before_journal
        assert obstacle.equipment_id in controller.model.equipment
    finally:
        canvas.close()


def test_standalone_preview_and_commit_use_same_snapped_collision_point() -> None:
    _app()
    controller = _controller("standalone-snap-collision")
    controller.set_snap(False)
    controller.add_equipment(
        "builtin.load",
        "Препятствие на границе",
        x=25.0,
        y=100.0,
    )
    controller.set_snap(True)
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.load",
        "name": "Новая нагрузка",
        # Keep the original horizontal footprint: this test isolates snapping
        # at the clearance boundary, independently of the new vertical default.
        "graphics": {"rotation_deg": 180.0, "orientation_mode": "manual"},
    }
    before_equipment = frozenset(controller.model.equipment)
    before_journal = len(controller.journal)
    before_diagram = controller.diagram
    before_fingerprint = electrical_model_fingerprint(controller.model)
    try:
        allowed_at_raw, _ = canvas.view._check_placement_collision(
            payload,
            QPointF(125.0, 100.0),
            candidate_point=QPointF(125.0, 100.0),
            rotation_deg=180.0,
        )
        assert allowed_at_raw

        canvas.view.begin_placement(payload)
        canvas.view._update_equipment_drop_feedback(
            canvas.view._placement_payload,
            QPointF(125.0, 100.0),
        )
        assert canvas.view._drop_equipment_point == QPointF(140.0, 100.0)
        proposal = canvas.view._placement_proposal
        assert canvas.view._placement_valid and proposal.valid and proposal.adjusted
        collision = DiagramCollisionService(controller.diagram, controller.model)
        snapped = canvas.view._equipment_preview_geometry(payload, QPointF(120, 100), rotation_deg=180)
        accepted = canvas.view._equipment_preview_geometry(payload, QPointF(proposal.x, proposal.y), rotation_deg=180)
        assert not collision.check_placement(snapped).allowed
        assert collision.check_placement(accepted).allowed
        assert frozenset(controller.model.equipment) == before_equipment
        assert len(controller.journal) == before_journal

        _click_scene(canvas, 125.0, 100.0)
        assert len(controller.model.equipment) == len(before_equipment) + 1
        assert len(controller.journal) == before_journal + 1
        created = next(row for rid, row in controller.diagram.representations.items()
                       if rid not in before_diagram.representations)
        assert (created.x, created.y, created.rotation_deg) == (proposal.x, proposal.y, proposal.rotation_deg)
        assert all(controller.diagram.representations[rid] == row
                   for rid, row in before_diagram.representations.items())
        canvas.undo()
        assert controller.diagram == before_diagram
        assert electrical_model_fingerprint(controller.model) == before_fingerprint
    finally:
        canvas.close()


@pytest.mark.parametrize(
    ("rotation", "expected_hv"),
    (
        (0, (160.0, 200.0)),
        (90, (200.0, 160.0)),
        (180, (240.0, 200.0)),
        (270, (200.0, 240.0)),
    ),
)
def test_ghost_preview_uses_semantic_port_count_and_rotated_map(
    rotation: int,
    expected_hv: tuple[float, float],
) -> None:
    _app()
    controller = _controller(f"ghost-ports-{rotation}")
    canvas = _show_canvas(controller)
    try:
        transformer = canvas.view._equipment_preview_geometry(
            {
                "target_kind": "equipment",
                "type_id": "builtin.transformer_3w",
                "name": "Трёхобмоточный трансформатор",
            },
            QPointF(200.0, 200.0),
            rotation_deg=rotation,
        )
        assert transformer is not None
        assert {zone.role for zone in transformer.port_connection_zones} == {
            "hv",
            "mv",
            "lv",
        }
        hv = next(
            zone for zone in transformer.port_connection_zones
            if zone.role == "hv"
        )
        assert (hv.shape.center_x, hv.shape.center_y) == pytest.approx(
            expected_hv
        )

        generator = canvas.view._equipment_preview_geometry(
            {
                "target_kind": "equipment",
                "type_id": "builtin.generator",
                "name": "Генератор",
            },
            QPointF(200.0, 200.0),
            rotation_deg=rotation,
        )
        assert generator is not None
        assert tuple(
            zone.role for zone in generator.port_connection_zones
        ) == ("terminal",)
    finally:
        canvas.close()


def test_inline_split_marker_follows_target_segment_orientation() -> None:
    target = QPointF(100.0, 100.0)
    horizontal, horizontal_leads = (
        DiagramGraphicsView._inline_split_feedback_geometry(
            target,
            "horizontal",
            1.0,
        )
    )
    vertical, vertical_leads = (
        DiagramGraphicsView._inline_split_feedback_geometry(
            target,
            "vertical",
            1.0,
        )
    )

    assert horizontal.width() > horizontal.height()
    assert all(first.y() == second.y() for first, second in horizontal_leads)
    assert vertical.height() > vertical.width()
    assert all(first.x() == second.x() for first, second in vertical_leads)


def test_selected_auto_orientation_restores_reverse_line_direction() -> None:
    _app()
    controller = _controller("selected-auto-reverse")
    line = _physical_line(controller, (400.0, 100.0), (0.0, 100.0))
    inserted = controller.insert_recloser(
        line.section_id,
        500_000,
        "Реклоузер",
        x=200.0,
        y=100.0,
    )
    controller.rotate_representation(inserted.representation_id, 0)
    canvas = _show_canvas(controller)
    try:
        canvas.scene.select_representations((inserted.representation_id,))
        canvas._auto_orient_selected((inserted.representation_id,))
        QApplication.processEvents()
        representation = controller.diagram.representations[
            inserted.representation_id
        ]
        assert representation.rotation_deg == 180.0
        assert (
            orientation_mode_for_representation(representation)
            is OrientationMode.AUTO
        )
        assert canvas.scene.selected_representation_ids() == (
            inserted.representation_id,
        )
    finally:
        canvas.close()


def test_r_toggles_selected_two_positions_without_electrical_change() -> None:
    _app()
    controller = _controller("selected-rotate")
    added = controller.add_equipment("builtin.circuit_breaker", "QF1", x=100, y=100)
    canvas = _show_canvas(controller)
    before = electrical_model_fingerprint(controller.model)
    equipment = controller.model.equipment[added.equipment_id]
    port_ids = equipment.port_ids
    try:
        canvas.scene.select_representations((added.representation_id,))
        for expected in (90.0, 180.0, 90.0, 180.0):
            QTest.keyClick(canvas.view, Qt.Key.Key_R)
            QApplication.processEvents()
            assert controller.diagram.representations[
                added.representation_id
            ].rotation_deg == expected
            assert canvas.scene.selected_representation_ids() == (
                added.representation_id,
            )
        assert controller.model.equipment[added.equipment_id].port_ids == port_ids
        assert electrical_model_fingerprint(controller.model) == before
    finally:
        canvas.close()


def test_shift_r_toggles_selected_object_to_other_allowed_position() -> None:
    _app()
    controller = _controller("selected-shift-rotate")
    added = controller.add_equipment(
        "builtin.circuit_breaker", "QF1", x=100, y=100
    )
    canvas = _show_canvas(controller)
    try:
        canvas.scene.select_representations((added.representation_id,))
        QTest.keyClick(canvas.view, Qt.Key.Key_R)
        QTest.keyClick(
            canvas.view,
            Qt.Key.Key_R,
            Qt.KeyboardModifier.ShiftModifier,
        )
        QApplication.processEvents()
        representation = controller.diagram.representations[added.representation_id]
        assert representation.rotation_deg == 180.0
        assert orientation_mode_for_representation(representation) is OrientationMode.MANUAL
    finally:
        canvas.close()


def test_analysis_allows_selection_and_properties_but_blocks_rotation(monkeypatch) -> None:
    _app()
    controller = _controller("analysis")
    # Switch symbols now toggle on double-click; other equipment opens its card.
    added = controller.add_equipment("builtin.load", "Нагрузка", x=100, y=100)
    canvas = _show_canvas(controller)
    requested: list[object] = []
    questions: list[str] = []
    def decline_unexpected_question(*args):
        questions.append(args[2])
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", decline_unexpected_question)
    canvas.equipmentDetailsRequested.connect(requested.append)
    before_fingerprint = electrical_model_fingerprint(controller.model)
    before_journal = len(controller.journal)
    before_ports = controller.model.equipment[added.equipment_id].port_ids
    try:
        canvas.set_mode(EditorMode.ANALYSIS)
        _click_scene(canvas, 100, 100)
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)
        _click_scene(canvas, 100, 100, double=True)
        assert requested == [added.representation_id]
        QTest.keyClick(canvas.view, Qt.Key.Key_R)
        QApplication.processEvents()
        assert controller.diagram.representations[added.representation_id].rotation_deg == 0.0
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)
        assert questions == []
        assert controller.model.equipment[added.equipment_id].port_ids == before_ports
        assert electrical_model_fingerprint(controller.model) == before_fingerprint
        assert len(controller.journal) == before_journal
    finally:
        canvas.close()


def test_rotation_with_collision_is_rejected_without_undo_entry() -> None:
    _app()
    controller = _controller("rotation-collision")
    wide = controller.add_equipment(
        "builtin.transformer_2w", "Т широкий", x=100, y=100, width=160, height=40
    )
    controller.add_equipment(
        "builtin.circuit_breaker", "QF сверху", x=100, y=190, width=40, height=40
    )
    canvas = _show_canvas(controller)
    before_journal = len(controller.journal)
    before_fingerprint = electrical_model_fingerprint(controller.model)
    try:
        canvas.scene.select_representations((wide.representation_id,))
        QTest.keyClick(canvas.view, Qt.Key.Key_R)
        QApplication.processEvents()
        assert controller.diagram.representations[wide.representation_id].rotation_deg == 0.0
        assert len(controller.journal) == before_journal
        assert electrical_model_fingerprint(controller.model) == before_fingerprint
    finally:
        canvas.close()


def test_hover_and_selected_are_distinct_item_states() -> None:
    _app()
    controller = _controller("hover")
    added = controller.add_equipment("builtin.load", "Нагрузка", x=100, y=100)
    canvas = _show_canvas(controller)
    item = canvas.scene._items_by_id[added.representation_id]
    try:
        item.hoverEnterEvent(
            QGraphicsSceneHoverEvent(QEvent.Type.GraphicsSceneHoverEnter)
        )
        assert item._hovered is True
        assert item.isSelected() is False
        _click_scene(canvas, 100, 100)
        assert item.isSelected() is True
        assert item._hovered is True
    finally:
        canvas.close()


def test_first_escape_cancels_tool_second_escape_clears_selection() -> None:
    _app()
    controller = _controller("escape")
    added = controller.add_equipment("builtin.load", "Нагрузка", x=100, y=100)
    canvas = _show_canvas(controller)
    try:
        canvas.scene.select_representations((added.representation_id,))
        canvas.view.begin_placement({
            "target_kind": "equipment",
            "type_id": "builtin.load",
            "name": "Новая нагрузка",
        })
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)

        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert canvas.scene.selected_representation_ids() == ()
    finally:
        canvas.close()


def test_space_cancels_placement_before_temporary_pan() -> None:
    _app()
    controller = _controller("placement-pan")
    canvas = _show_canvas(controller)
    try:
        canvas.view.begin_placement({
            "target_kind": "equipment",
            "type_id": "builtin.load",
            "name": "Новая нагрузка",
        })
        QTest.keyPress(canvas.view, Qt.Key.Key_Space)
        QApplication.processEvents()
        assert canvas.view.tool_state.tool is EditorTool.PAN
        assert canvas.view._placement_payload is None

        QTest.keyRelease(canvas.view, Qt.Key.Key_Space)
        QApplication.processEvents()
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        _click_scene(canvas, 100.0, 100.0)
        assert controller.model.equipment == {}
    finally:
        canvas.close()


def test_connection_tool_updates_shared_tool_state() -> None:
    _app()
    controller = _controller("connection-state")
    added = controller.add_equipment(
        "builtin.circuit_breaker", "QF1", x=100, y=100
    )
    canvas = _show_canvas(controller)
    observed: list[EditorTool] = []
    canvas.toolStateChanged.connect(lambda state: observed.append(state.tool))
    try:
        equipment = controller.model.equipment[added.equipment_id]
        object_item = canvas.scene._items_by_id[added.representation_id]
        port_item = object_item.port_item(equipment.port_ids[0])
        assert port_item is not None

        canvas.scene.begin_connection(port_item)
        assert canvas.view.tool_state.tool is EditorTool.DRAW_CONNECTION
        canvas.scene.cancel_connection()
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        assert observed[-2:] == [EditorTool.DRAW_CONNECTION, EditorTool.SELECT]
    finally:
        canvas.close()


def test_right_click_cancels_active_placement() -> None:
    _app()
    controller = _controller("right-cancel")
    canvas = _show_canvas(controller)
    try:
        canvas.view.begin_placement({
            "target_kind": "equipment",
            "type_id": "builtin.load",
            "name": "Новая нагрузка",
        })
        QTest.mouseClick(
            canvas.view.viewport(),
            Qt.MouseButton.RightButton,
            pos=canvas.view.mapFromScene(QPointF(100.0, 100.0)),
        )
        QApplication.processEvents()
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        assert canvas.view._placement_payload is None
        assert controller.model.equipment == {}
    finally:
        canvas.close()


def test_keyboard_move_collision_is_rejected_without_history_entry() -> None:
    _app()
    controller = _controller("keyboard-collision")
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=100, y=100)
    controller.add_equipment("builtin.load", "Нагрузка 2", x=200, y=100)
    canvas = _show_canvas(controller)
    try:
        canvas.scene.select_representations((first.representation_id,))
        before_journal = len(controller.journal)
        QTest.keyClick(
            canvas.view,
            Qt.Key.Key_Right,
            Qt.KeyboardModifier.ShiftModifier,
        )
        QApplication.processEvents()
        representation = controller.diagram.representations[first.representation_id]
        assert (representation.x, representation.y) == (100.0, 100.0)
        assert len(controller.journal) == before_journal
    finally:
        canvas.close()


def test_drag_preview_marks_collision_before_commit() -> None:
    _app()
    controller = _controller("drag-preview-collision")
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=100, y=100)
    second = controller.add_equipment("builtin.load", "Нагрузка 2", x=220, y=100)
    canvas = _show_canvas(controller)
    try:
        first_item = canvas.scene._items_by_id[first.representation_id]
        second_item = canvas.scene._items_by_id[second.representation_id]
        canvas.scene._start_positions = {
            first.representation_id: QPointF(first_item.pos())
        }
        first_item.setPos(QPointF(180.0, 100.0))
        canvas.scene._update_drag_collision_preview()
        assert first.representation_id in canvas.scene._drag_collision_ids
        assert second.representation_id in canvas.scene._drag_collision_ids
        assert first_item._target_feedback.value == "incompatible"
        assert second_item._target_feedback.value == "incompatible"
    finally:
        canvas.close()


def test_toolbar_rotation_actions_follow_selection_and_edit_mode() -> None:
    _app()
    controller = _controller("toolbar-rotation")
    added = controller.add_equipment("builtin.circuit_breaker", "QF1", x=100, y=100)
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    try:
        workspace.canvas.scene.select_representations((added.representation_id,))
        QApplication.processEvents()
        assert workspace.command_bar.rotate_right_action.isEnabled()
        workspace.command_bar.rotate_right_action.trigger()
        QApplication.processEvents()
        assert controller.diagram.representations[added.representation_id].rotation_deg == 90.0

        workspace.canvas.set_mode(EditorMode.ANALYSIS)
        workspace.command_bar.refresh(controller)
        assert not workspace.command_bar.rotate_right_action.isEnabled()
        assert not workspace.command_bar.auto_orientation_action.isEnabled()
    finally:
        workspace.close()


def test_inspector_coordinate_edit_cannot_overlap_other_equipment() -> None:
    _app()
    controller = _controller("inspector-collision")
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=100, y=100)
    controller.add_equipment("builtin.load", "Нагрузка 2", x=220, y=100)
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    try:
        workspace.canvas.scene.select_representations((first.representation_id,))
        before_journal = len(controller.journal)
        workspace._edit_property("graphics.x", 220.0)
        representation = controller.diagram.representations[first.representation_id]
        assert representation.x == 100.0
        assert len(controller.journal) == before_journal
    finally:
        workspace.close()
