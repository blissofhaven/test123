# -*- coding: utf-8 -*-
"""Регрессии командных обходов collision-модели UI-UX-1."""
from __future__ import annotations

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

from rza_calc.domain.diagram import (  # noqa: E402
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor import EditorMode  # noqa: E402
from rza_calc.editor.connection_tool import ConnectionToolMode  # noqa: E402
from rza_calc.gui import editor_scene as editor_scene_module  # noqa: E402
from rza_calc.gui.editor_panels import EditorWorkspaceWidget  # noqa: E402
from rza_calc.gui.editor_scene import (  # noqa: E402
    DiagramGraphicsScene,
    DiagramGraphicsView,
    HitTestKind,
)

from tests.test_ui_interaction_gui import (  # noqa: E402
    _app,
    _controller,
    _physical_line,
    _show_canvas,
)


def _snapshot(controller):
    return (
        len(controller.model.equipment),
        len(controller.diagram.representations),
        len(controller.journal),
        controller.diagram.revision,
        electrical_model_fingerprint(controller.model),
    )


@pytest.mark.parametrize("command", ("duplicate", "paste"))
@pytest.mark.parametrize("blocker_x", (None, 200.0, 290.0))
def test_gui_copy_commands_use_one_predictable_safe_place_atomically(
    command: str,
    blocker_x: float | None,
) -> None:
    _app()
    blocked = blocker_x is not None
    controller = _controller(f"copy-{command}-{blocker_x}")
    source = controller.add_equipment(
        "builtin.load",
        "Нагрузка 1",
        x=100,
        y=100,
        width=80,
        height=50,
    )
    if blocked:
        controller.add_equipment(
            "builtin.load",
            "Нагрузка 2",
            x=blocker_x,
            y=100,
            width=80,
            height=50,
        )
    canvas = _show_canvas(controller)
    errors: list[str] = []
    canvas.errorOccurred.connect(errors.append)
    try:
        canvas.scene.select_representations((source.representation_id,))
        if command == "paste":
            canvas.copy((source.representation_id,))
        before = _snapshot(controller)
        key = Qt.Key.Key_D if command == "duplicate" else Qt.Key.Key_V
        QTest.keyClick(
            canvas.view,
            key,
            Qt.KeyboardModifier.ControlModifier,
        )
        QApplication.processEvents()

        if blocked:
            assert _snapshot(controller) == before
            assert errors
            assert any(
                token in errors[-1].casefold()
                for token in ("пересечен", "зазор", "разместить")
            )
            assert canvas.scene.selected_representation_ids() == (
                source.representation_id,
            )
        else:
            assert len(controller.model.equipment) == before[0] + 1
            assert len(controller.diagram.representations) == before[1] + 1
            assert len(controller.journal) == before[2] + 1
            copied_id = canvas.scene.selected_representation_ids()[0]
            copied = controller.diagram.representations[copied_id]
            assert (copied.x, copied.y) == (200.0, 100.0)
            assert not errors
    finally:
        canvas.close()


def test_inspector_resize_runs_full_collision_preflight() -> None:
    _app()
    controller = _controller("resize-preflight")
    first = controller.add_equipment(
        "builtin.load", "Нагрузка 1", x=100, y=100, width=80, height=50
    )
    controller.add_equipment(
        "builtin.load", "Нагрузка 2", x=220, y=100, width=80, height=50
    )
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    messages: list[str] = []
    workspace.statusMessage.connect(messages.append)
    try:
        workspace.canvas.scene.select_representations((first.representation_id,))
        before = _snapshot(controller)
        before_representation = controller.diagram.representations[
            first.representation_id
        ]
        workspace._edit_property("graphics.width", 240.0)
        QApplication.processEvents()
        assert _snapshot(controller) == before
        assert (
            controller.diagram.representations[first.representation_id]
            == before_representation
        )
        assert messages and "нельзя" in messages[-1].casefold()
    finally:
        workspace.close()


def test_analysis_disables_and_blocks_gui_undo_redo_without_losing_selection() -> None:
    _app()
    controller = _controller("analysis-history")
    added = controller.add_equipment("builtin.load", "Нагрузка", x=100, y=100)
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    try:
        workspace.canvas.scene.select_representations((added.representation_id,))
        workspace.canvas.set_mode(EditorMode.ANALYSIS)
        workspace.command_bar.refresh(controller)
        before = _snapshot(controller)
        inspector_rows_before = workspace.inspector.tree.topLevelItemCount()
        assert inspector_rows_before > 0
        assert not workspace.command_bar.undo_action.isEnabled()
        QTest.keyClick(
            workspace.canvas.view,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier,
        )
        QApplication.processEvents()
        assert _snapshot(controller) == before
        assert workspace.canvas.scene.selected_representation_ids() == (
            added.representation_id,
        )
        assert workspace._selected_ids == (added.representation_id,)
        assert workspace.inspector.tree.topLevelItemCount() == inspector_rows_before

        # Тот же барьер действует, если действие панели команд вызовет метод
        # напрямую, минуя обработчик клавиатуры сцены.
        workspace.canvas.undo()
        QApplication.processEvents()
        assert _snapshot(controller) == before
        assert workspace._selected_ids == (added.representation_id,)
        assert workspace.inspector.tree.topLevelItemCount() == inspector_rows_before

        workspace.canvas.set_mode(EditorMode.EDIT)
        workspace.canvas.undo()
        assert added.equipment_id not in controller.model.equipment
        workspace.canvas.set_mode(EditorMode.ANALYSIS)
        workspace.command_bar.refresh(controller)
        before_redo = _snapshot(controller)
        assert not workspace.command_bar.redo_action.isEnabled()
        QTest.keyClick(
            workspace.canvas.view,
            Qt.Key.Key_Y,
            Qt.KeyboardModifier.ControlModifier,
        )
        QApplication.processEvents()
        assert _snapshot(controller) == before_redo
        assert added.equipment_id not in controller.model.equipment
        workspace.canvas.redo()
        QApplication.processEvents()
        assert _snapshot(controller) == before_redo
        assert added.equipment_id not in controller.model.equipment
    finally:
        workspace.close()


def test_context_recloser_preflight_uses_projected_snapped_point(
    monkeypatch,
) -> None:
    _app()
    controller = _controller("context-recloser-collision")
    line = _physical_line(controller, (0, 0), (400, 0))
    controller.add_equipment(
        "builtin.transformer_2w",
        "Т1",
        x=200,
        y=0,
        width=74,
        height=54,
    )
    canvas = _show_canvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 500_000),
    )
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *args, **kwargs: ("Р-1", True),
    )
    messages: list[str] = []
    canvas.statusMessage.connect(messages.append)
    try:
        route = controller.diagram.routes[line.route_id]
        before = _snapshot(controller)
        canvas.scene.routeContextActionRequested.emit(
            "insert_recloser",
            {
                "route_id": route.id,
                "equipment_id": line.section_id,
                "page_id": route.page_id,
                "scene_x": 203.0,
                "scene_y": 2.0,
                "route_fraction": 0.5,
            },
        )
        QApplication.processEvents()
        assert _snapshot(controller) == before
        assert line.section_id in controller.model.line_sections
        assert line.route_id in controller.diagram.routes
        assert not [
            item
            for item in controller.model.equipment.values()
            if item.type_id.value == "builtin.recloser"
        ]
        assert messages and "нельзя" in messages[-1].casefold()
    finally:
        canvas.close()


def test_preview_reuses_collision_snapshot_until_revision_changes(
    monkeypatch,
) -> None:
    _app()
    controller = _controller("preview-cache")
    controller.add_equipment("builtin.load", "Нагрузка", x=100, y=100)
    original = editor_scene_module.DiagramCollisionService
    constructions = 0

    class CountingCollisionService(original):
        def __init__(self, *args, **kwargs):
            nonlocal constructions
            constructions += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        editor_scene_module,
        "DiagramCollisionService",
        CountingCollisionService,
    )
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.circuit_breaker",
        "name": "Выключатель",
    }
    try:
        canvas.view.begin_placement(payload)
        for x in range(300, 1300, 10):
            canvas.view._update_equipment_drop_feedback(
                payload,
                QPointF(float(x), 300.0),
            )
        assert constructions == 1

        controller.add_equipment("builtin.load", "Нагрузка 2", x=600, y=600)
        canvas.refresh()
        canvas.view._update_equipment_drop_feedback(
            payload,
            QPointF(420.0, 300.0),
        )
        assert constructions == 2
    finally:
        canvas.close()


def test_ctrl_a_and_select_same_type_emit_one_final_selection() -> None:
    _app()
    controller = _controller("batch-selection")
    loads = tuple(
        controller.add_equipment(
            "builtin.load",
            f"Нагрузка {index + 1}",
            x=100 + index * 140,
            y=100,
        )
        for index in range(3)
    )
    breaker = controller.add_equipment(
        "builtin.circuit_breaker", "QF1", x=100, y=260
    )
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    emissions: list[tuple] = []
    workspace.canvas.selectionChanged.connect(
        lambda ids: emissions.append(tuple(ids))
    )
    try:
        workspace.canvas.view.setFocus()
        emissions.clear()
        QTest.keyClick(
            workspace.canvas.view,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.ControlModifier,
        )
        QApplication.processEvents()
        assert len(emissions) == 1
        assert set(emissions[0]) == {
            *(item.representation_id for item in loads),
            breaker.representation_id,
        }

        workspace.canvas.scene.select_representations(
            (loads[0].representation_id,)
        )
        emissions.clear()
        workspace.canvas.scene.select_same_type()
        QApplication.processEvents()
        assert len(emissions) == 1
        assert set(emissions[0]) == {
            item.representation_id for item in loads
        }
    finally:
        workspace.close()


def test_selection_updates_counter_without_revalidating_unchanged_project(
    monkeypatch,
) -> None:
    _app()
    controller = _controller("selection-no-revalidation")
    added = controller.add_equipment(
        "builtin.load", "Нагрузка", x=100, y=100
    )
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1300, 760)
    workspace.show()
    QApplication.processEvents()
    validation_calls = 0

    def unexpected_validation(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return ()

    monkeypatch.setattr(
        workspace._validation_service,
        "validate",
        unexpected_validation,
    )
    try:
        workspace.canvas.scene.select_representations(
            (added.representation_id,)
        )
        QApplication.processEvents()
        assert validation_calls == 0
        assert any(
            workspace.bottom_panel.diagnostics.item(index).text()
            == "Выбрано: 1"
            for index in range(workspace.bottom_panel.diagnostics.count())
        )
    finally:
        workspace.close()


def test_hit_test_prefers_handle_and_physical_line_over_body_and_plain_route() -> None:
    _app()
    controller = _controller("hit-priority")
    line = _physical_line(controller, (0, 0), (400, 0))
    breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF1",
        x=100,
        y=0,
    )
    physical = controller.diagram.routes[line.route_id]
    ordinary = DiagramRoute(
        DiagramRouteId("route.ui.hit.ordinary"),
        physical.page_id,
        DiagramRouteKind.NODE_CONNECTION,
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            physical.start_anchor.representation_id,
            physical.start_anchor.electrical_node_id,
        ),
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            physical.end_anchor.representation_id,
            physical.start_anchor.electrical_node_id,
        ),
        electrical_node_id=physical.start_anchor.electrical_node_id,
        waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.ui.hit.0"), 200, -120),
            RouteWaypoint(RouteWaypointId("waypoint.ui.hit.1"), 200, 120),
        ),
    )
    document = controller.diagram.add_route(ordinary)
    scene = DiagramGraphicsScene()
    scene.sync_document(
        document,
        controller.model,
        page_id=physical.page_id,
    )
    view = DiagramGraphicsView(scene)
    view.resize(920, 640)
    view.show()
    view.actual_size()
    view.centerOn(200, 0)
    QApplication.processEvents()
    try:
        body_point = QPointF(100, 0)
        target = scene.resolve_hit_target(body_point, view.transform())
        assert target.kind is HitTestKind.BODY
        assert target.item is scene._items_by_id[breaker.representation_id]

        breaker_item = scene._items_by_id[breaker.representation_id]
        port_item = next(iter(breaker_item._port_items.values()))
        port_point = port_item.mapToScene(QPointF())
        target = scene.resolve_hit_target(port_point, view.transform())
        assert target.kind is HitTestKind.PORT
        assert target.item is port_item

        crossing = QPointF(200, 0)
        target = scene.resolve_hit_target(crossing, view.transform())
        assert target.kind is HitTestKind.PHYSICAL_LINE
        assert target.item is scene._route_items_by_id[line.route_id]
        QTest.mouseClick(
            view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=view.mapFromScene(crossing),
        )
        QApplication.processEvents()
        assert scene._route_items_by_id[line.route_id].isSelected()
        assert not scene._route_items_by_id[ordinary.id].isSelected()

        route_item = scene._route_items_by_id[line.route_id]
        route_item.setSelected(True)
        QApplication.processEvents()
        handle = route_item._endpoint_handles[True]
        handle_point = handle.mapToScene(QPointF())
        target = scene.resolve_hit_target(handle_point, view.transform())
        assert target.kind is HitTestKind.HANDLE
        assert target.item is handle
        QTest.mouseClick(
            view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=view.mapFromScene(handle_point),
        )
        QApplication.processEvents()
        assert scene._connection_tool.active
        assert scene._connection_tool.mode is ConnectionToolMode.RECONNECT
    finally:
        view.close()
        scene.deleteLater()


@pytest.mark.parametrize(
    ("modifiers", "expected_rotation"),
    (
        (Qt.KeyboardModifier.NoModifier, 90),
        (Qt.KeyboardModifier.ShiftModifier, 90),
    ),
)
def test_ghost_rotation_rechecks_collision_immediately_without_mouse_move(
    modifiers: Qt.KeyboardModifier,
    expected_rotation: int,
) -> None:
    _app()
    controller = _controller("ghost-rotation-collision")
    controller.add_equipment(
        "builtin.circuit_breaker",
        "QF1",
        x=100,
        y=190,
        width=40,
        height=40,
    )
    canvas = _show_canvas(controller)
    payload = {
        "target_kind": "equipment",
        "type_id": "builtin.transformer_2w",
        "name": "Т1",
        "graphics": {
            "width": 160.0,
            "height": 40.0,
            "rotation_deg": 0.0,
            "orientation_mode": "manual",
        },
    }
    messages: list[str] = []
    canvas.statusMessage.connect(messages.append)
    before = _snapshot(controller)
    try:
        canvas.view.begin_placement(payload)
        point = QPointF(100, 100)
        # QTest не посылает событие, если глобальный курсор уже стоит в той же
        # экранной точке после предыдущего параметризованного сценария.
        QTest.mouseMove(
            canvas.view.viewport(),
            canvas.view.mapFromScene(QPointF(120, 100)),
        )
        QTest.mouseMove(canvas.view.viewport(), canvas.view.mapFromScene(point))
        QApplication.processEvents()
        assert canvas.view._placement_valid
        messages.clear()

        QTest.keyClick(canvas.view, Qt.Key.Key_R, modifiers)
        QApplication.processEvents()
        assert canvas.view._placement_rotation_deg == expected_rotation
        assert not canvas.view._placement_valid
        assert canvas.view._placement_conflict_name
        assert messages and "нельзя" in messages[-1].casefold()
        assert canvas.view._placement_payload is not None
        assert _snapshot(controller) == before
    finally:
        canvas.close()
