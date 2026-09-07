"""Native Qt corner rotation: preview, commit, cancellation and model safety."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsItem

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument, DiagramDocumentId, DiagramPage, PageId,
    RouteWaypoint, RouteWaypointId, RouteWaypointSource,
)
from rza_calc.domain.electrical import ElectricalModel, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import EditorMode, EditorTool, ProjectEditorController
from rza_calc.gui.editor_panels import EditorWorkspaceWidget, EquipmentLibraryTree
from rza_calc.gui.editor_scene import EditorCanvas


U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def controller_for(token="main"):
    page = DiagramPage(PageId(f"page.rotation-handle.{token}"), "Основная схема")
    return ProjectEditorController(_Project(
        ElectricalModel.with_builtins("Поворот за угол"),
        DiagramDocument.create(
            "Поворот за угол", (page,),
            document_id=DiagramDocumentId(f"diagram.rotation-handle.{token}"),
        ),
        ProjectCatalogSnapshots(),
    ))


def electrical_signature(controller):
    model = controller.model
    return (
        electrical_model_fingerprint(model), model.revision,
        model.connectivity_signature(), frozenset(model.electrical_nodes),
        frozenset(model.connections),
        {identifier: item.port_ids for identifier, item in model.equipment.items()},
        dict(model.ports),
    )


def document_signature(controller):
    return (
        controller.diagram.revision,
        dict(controller.diagram.representations),
        dict(controller.diagram.routes),
        tuple(controller.journal),
    )


def port_signature(item):
    return {
        identifier: (port.anchor.role, port.anchor.x, port.anchor.y,
                     port.scenePos().x(), port.scenePos().y())
        for identifier, port in item._port_items.items()
    }


def show_canvas(controller):
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 760)
    canvas.show()
    QApplication.processEvents()
    canvas.view.actual_size()
    canvas.view.centerOn(300.0, 260.0)
    canvas.view.setFocus()
    QApplication.processEvents()
    return canvas


def select_object(canvas, representation_id):
    canvas.scene.select_representations((representation_id,))
    QApplication.processEvents()
    return canvas.scene._items_by_id[representation_id]


def mouse_at(canvas, scene_point, kind="move"):
    pos = canvas.view.mapFromScene(scene_point)
    if kind == "move":
        QTest.mouseMove(canvas.view.viewport(), pos, delay=15)
    else:
        action = {"press": QTest.mousePress, "release": QTest.mouseRelease,
                  "click": QTest.mouseClick}[kind]
        action(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    QApplication.processEvents()


def quarter_turn_point(center, start, delta=90.0):
    angle = math.radians(delta)
    dx, dy = start.x() - center.x(), start.y() - center.y()
    return center + QPointF(dx * math.cos(angle) - dy * math.sin(angle),
                            dx * math.sin(angle) + dy * math.cos(angle))


def path_enters_rectangle(path, rectangle):
    """Test the wire segments, not QPainterPath's implicitly closed fill area."""
    points = [(path.elementAt(index).x, path.elementAt(index).y)
              for index in range(path.elementCount())]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 == x2:
            if rectangle.left() < x1 < rectangle.right() and (
                max(min(y1, y2), rectangle.top()) < min(max(y1, y2), rectangle.bottom())
            ):
                return True
        else:
            assert y1 == y2, "Rotation must keep incident routes orthogonal"
            if rectangle.top() < y1 < rectangle.bottom() and (
                max(min(x1, x2), rectangle.left()) < min(max(x1, x2), rectangle.right())
            ):
                return True
    return False


def begin_drag(canvas, representation_id, delta=90.0):
    item = select_object(canvas, representation_id)
    handle = canvas.scene._rotation_handle
    assert handle.isVisible()
    start = handle.scenePos()
    end = quarter_turn_point(item.scenePos(), start, delta)
    # Ensure a real hover transition even when the host cursor was already here.
    mouse_at(canvas, start + QPointF(24, 24))
    mouse_at(canvas, start)
    mouse_at(canvas, start, "press")
    mouse_at(canvas, end)
    return end


def connected_fixture(token="connected", equipment_type="circuit_breaker"):
    controller = controller_for(token)
    groups = {"main": U10}
    if equipment_type.startswith("transformer"):
        groups = {"hv": VoltageClassId("builtin.voltage.ac.110kv"), "lv": U10}
        if equipment_type == "transformer_3w":
            groups["mv"] = VoltageClassId("builtin.voltage.ac.35kv")
    added = controller.add_equipment(
        "builtin." + equipment_type, "Объект", x=300, y=260,
        width=100, height=60, rotation_deg=90,
        voltage_class_by_group=groups,
    )
    load = controller.add_equipment(
        "builtin.load", "Нагрузка", x=100, y=420, rotation_deg=180,
        voltage_class_by_group={"main": U10},
    )
    controller.connect_ports(added.port_ids[-1], load.port_ids[0])
    return controller, added


def test_handle_hover_is_discoverable_zoom_stable_and_requires_single_selection(app):
    controller = controller_for("visibility")
    first = controller.add_equipment("builtin.transformer_2w", "Т1", x=300, y=260,
                                     rotation_deg=90)
    second = controller.add_equipment("builtin.load", "Н1", x=600, y=260)
    canvas = show_canvas(controller)
    try:
        assert not canvas.scene._rotation_handle.isVisible()
        select_object(canvas, first.representation_id)
        handle = canvas.scene._rotation_handle
        assert handle.isVisible()
        assert handle.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        assert handle.acceptHoverEvents()
        assert handle.toolTip()
        for zoom in (0.4, 1.0, 2.5):
            canvas.view.set_zoom(zoom)
            QApplication.processEvents()
            point = handle.scenePos()
            mouse_at(canvas, point + QPointF(40 / zoom, 40 / zoom))
            mouse_at(canvas, point)
            assert canvas.view.itemAt(canvas.view.mapFromScene(point)) is handle
            assert handle.cursor().shape() != Qt.CursorShape.ArrowCursor
        canvas.scene.select_representations((first.representation_id, second.representation_id))
        assert not handle.isVisible()
        select_object(canvas, first.representation_id)
        canvas.view.begin_placement({"target_kind": "equipment", "type_id": "builtin.load",
                                     "name": "Новая нагрузка"})
        QApplication.processEvents()
        assert not handle.isVisible()
    finally:
        canvas.close()


@pytest.mark.parametrize("equipment_type", ("circuit_breaker", "transformer_2w", "transformer_3w"))
def test_mouse_drag_previews_then_commits_once_and_undo_redo_preserve_semantic_ports(app, equipment_type):
    controller, added = connected_fixture(equipment_type, equipment_type)
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        original_electrical = electrical_signature(controller)
        original_document = document_signature(controller)
        ports_before = port_signature(item)
        end = begin_drag(canvas, added.representation_id)
        assert canvas.view.tool_state.tool is EditorTool.ROTATE_OBJECT
        assert item.rotation() == 180.0
        assert document_signature(controller) == original_document
        assert electrical_signature(controller) == original_electrical
        assert port_signature(item) != ports_before
        mouse_at(canvas, end, "release")
        assert controller.diagram.representations[added.representation_id].rotation_deg == 180.0
        assert len(controller.journal) == len(original_document[-1]) + 1
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)
        assert electrical_signature(controller) == original_electrical
        ports_after = port_signature(canvas.scene._items_by_id[added.representation_id])
        assert set(ports_after) == set(ports_before)
        assert {key: value[:3] for key, value in ports_after.items()} == {
            key: value[:3] for key, value in ports_before.items()
        }
        canvas.undo()
        assert controller.diagram.representations[added.representation_id].rotation_deg == 90.0
        assert port_signature(canvas.scene._items_by_id[added.representation_id]) == ports_before
        assert electrical_signature(controller) == original_electrical
        canvas.redo()
        assert controller.diagram.representations[added.representation_id].rotation_deg == 180.0
        assert port_signature(canvas.scene._items_by_id[added.representation_id]) == ports_after
        assert electrical_signature(controller) == original_electrical
    finally:
        canvas.close()


def test_click_without_drag_does_not_create_undo_entry(app):
    controller, added = connected_fixture("no-drag")
    canvas = show_canvas(controller)
    try:
        select_object(canvas, added.representation_id)
        before = document_signature(controller), electrical_signature(controller)
        mouse_at(canvas, canvas.scene._rotation_handle.scenePos(), "click")
        assert (document_signature(controller), electrical_signature(controller)) == before
        assert canvas.view.tool_state.tool is EditorTool.SELECT
    finally:
        canvas.close()


def test_losing_mouse_grab_cancels_preview_without_creating_a_node_or_command(app):
    controller, added = connected_fixture("lost-grab")
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        before = document_signature(controller), electrical_signature(controller)
        original_ports = port_signature(item)
        end = begin_drag(canvas, added.representation_id)
        assert item.rotation() == 180.0
        assert canvas.scene.mouseGrabberItem() is canvas.scene._rotation_handle
        canvas.scene._rotation_handle.ungrabMouse()
        QApplication.processEvents()
        assert item.rotation() == 90.0
        assert port_signature(item) == original_ports
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        mouse_at(canvas, end, "release")
        assert (document_signature(controller), electrical_signature(controller)) == before
    finally:
        canvas.close()


def test_escape_rolls_back_visual_preview_and_release_does_not_commit(app):
    controller, added = connected_fixture("escape")
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        before = document_signature(controller), electrical_signature(controller)
        original_ports = port_signature(item)
        original_paths = {key: value._path for key, value in canvas.scene._route_items_by_id.items()}
        end = begin_drag(canvas, added.representation_id)
        assert item.rotation() == 180.0
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert item.rotation() == 90.0
        assert port_signature(item) == original_ports
        assert {key: value._path for key, value in canvas.scene._route_items_by_id.items()} == original_paths
        assert canvas.scene.selected_representation_ids() == (added.representation_id,)
        mouse_at(canvas, end, "release")
        assert (document_signature(controller), electrical_signature(controller)) == before
        assert canvas.view.tool_state.tool is EditorTool.SELECT
    finally:
        canvas.close()


def test_collision_rejects_whole_rotation_without_diagram_or_history_change(app):
    controller = controller_for("collision")
    transformer = controller.add_equipment(
        "builtin.transformer_2w", "Т1", x=300, y=260,
        width=160, height=40, rotation_deg=90,
    )
    controller.add_equipment("builtin.circuit_breaker", "QF препятствие", x=390, y=260,
                              width=40, height=40, rotation_deg=90)
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, transformer.representation_id)
        before = document_signature(controller), electrical_signature(controller)
        original_ports = port_signature(item)
        assert canvas.scene.rotation_collision(transformer.representation_id, 180) is not None
        end = begin_drag(canvas, transformer.representation_id)
        assert document_signature(controller) == before[0]
        mouse_at(canvas, end, "release")
        assert item.rotation() == 90.0
        assert port_signature(item) == original_ports
        assert (document_signature(controller), electrical_signature(controller)) == before
        assert canvas.view.tool_state.tool is EditorTool.SELECT
    finally:
        canvas.close()


def test_analysis_hides_handle_and_cancels_pending_preview(app):
    controller, added = connected_fixture("analysis")
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        before = document_signature(controller), electrical_signature(controller)
        end = begin_drag(canvas, added.representation_id)
        assert item.rotation() == 180.0
        canvas.set_mode(EditorMode.ANALYSIS)
        QApplication.processEvents()
        assert not canvas.scene._rotation_handle.isVisible()
        assert item.rotation() == 90.0
        # The existing controller persists editor mode as workspace metadata,
        # which increments the document revision but never geometry/history.
        after_mode = document_signature(controller), electrical_signature(controller)
        assert after_mode[0][1:] == before[0][1:]
        assert after_mode[1] == before[1]
        mouse_at(canvas, end, "release")
        QTest.keyClick(canvas.view, Qt.Key.Key_R)
        QApplication.processEvents()
        assert (document_signature(controller), electrical_signature(controller)) == after_mode
        canvas.scene.begin_object_rotation(added.representation_id, item.scenePos() + QPointF(80, -80))
        canvas.scene.preview_object_rotation(item.scenePos() + QPointF(80, 80))
        canvas.scene.finish_object_rotation()
        assert (document_signature(controller), electrical_signature(controller)) == after_mode
    finally:
        canvas.close()


def test_manual_node_is_absent_but_port_connection_still_creates_one_node(app, monkeypatch):
    # Enter now offers the same electrical choice as the mouse. This case
    # verifies automatic nodes; the real menu is covered by Stage 2 gestures.
    monkeypatch.setattr(EditorCanvas, "_ask_dragged_connection_kind", lambda *_: "wire")
    controller = controller_for("automatic-node")
    first = controller.add_equipment("builtin.load", "Н1", x=100, y=260,
                                     rotation_deg=90, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Н2", x=500, y=260,
                                      rotation_deg=90, voltage_class_by_group={"main": U10})
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    workspace.resize(1400, 800)
    workspace.show()
    QApplication.processEvents()
    try:
        tree = workspace.side_panel.library
        payloads = []
        for root_index in range(tree.topLevelItemCount()):
            category = tree.topLevelItem(root_index)
            for item_index in range(category.childCount()):
                payload = category.child(item_index).data(0, EquipmentLibraryTree.PAYLOAD_ROLE)
                if isinstance(payload, dict):
                    payloads.append(payload)
        assert not any(payload.get("symbol_key") in {"connection_point", "electrical_node"}
                       or payload.get("symbol") in {"connection_point", "electrical_node"}
                       for payload in payloads)
        assert any(payload.get("symbol_key", payload.get("symbol")) == "busbar"
                   for payload in payloads)
        canvas = workspace.canvas
        canvas.view.actual_size()
        canvas.view.centerOn(300, 260)
        first_port = canvas.scene._items_by_id[first.representation_id].port_item(first.port_ids[0])
        second_port = canvas.scene._items_by_id[second.representation_id].port_item(second.port_ids[0])
        nodes_before = set(controller.model.electrical_nodes)
        journal_before = len(controller.journal)
        canvas.scene.begin_connection(first_port)
        canvas.scene.update_connection_cursor(second_port.scenePos())
        assert canvas.scene._connection_target is not None
        QTest.keyClick(canvas.view, Qt.Key.Key_Return)
        QApplication.processEvents()
        new_nodes = set(controller.model.electrical_nodes) - nodes_before
        assert len(new_nodes) == 1
        node_id = next(iter(new_nodes))
        assert controller.model.connection_for_port(first.port_ids[0]).electrical_node_id == node_id
        assert controller.model.connection_for_port(second.port_ids[0]).electrical_node_id == node_id
        assert len(controller.journal) == journal_before + 1
        assert len(controller.model.equipment) == 2
    finally:
        workspace.close()


def test_bus_rotation_keeps_fractional_connection_point_in_preview_commit_and_undo(app, monkeypatch):
    monkeypatch.setattr(EditorCanvas, "_ask_dragged_connection_kind", lambda *_: "wire")
    controller = controller_for("bus-fraction")
    bus = controller.add_electrical_node(
        "Шины 10 кВ", x=300, y=260, symbol_key="busbar", width=240, height=12,
        rotation_deg=90, voltage_class_id=U10,
    )
    load = controller.add_equipment(
        "builtin.load", "Нагрузка", x=100, y=420, rotation_deg=180,
        voltage_class_by_group={"main": U10},
    )
    canvas = show_canvas(controller)
    try:
        source = canvas.scene._items_by_id[load.representation_id].port_item(load.port_ids[0])
        canvas.scene.begin_connection(source)
        canvas.scene.update_connection_cursor(QPointF(300, 320))
        preview_fraction = float(canvas.scene._connection_target.anchor_key)
        assert preview_fraction == pytest.approx(0.75, abs=1e-12, rel=0)
        QTest.keyClick(canvas.view, Qt.Key.Key_Return)
        QApplication.processEvents()
        route = next(iter(controller.diagram.routes.values()))
        assert route.end_anchor.representation_id == bus.representation_id
        assert float(route.end_anchor.anchor_key) == preview_fraction
        assert (route.waypoints[-1].x, route.waypoints[-1].y) == (300.0, 320.0)
        original_document = document_signature(controller)
        original_electrical = electrical_signature(controller)
        end = begin_drag(canvas, bus.representation_id)
        item = canvas.scene._items_by_id[bus.representation_id]
        assert item.rotation() == 180.0
        route_item = canvas.scene._route_items_by_id[route.id]
        assert route_item._path.currentPosition() == QPointF(240, 260)
        assert document_signature(controller) == original_document
        assert electrical_signature(controller) == original_electrical
        mouse_at(canvas, end, "release")
        committed = controller.diagram.routes[route.id]
        assert (committed.waypoints[-1].x, committed.waypoints[-1].y) == (240.0, 260.0)
        assert committed.end_anchor == route.end_anchor
        assert electrical_signature(controller) == original_electrical
        assert len(controller.journal) == len(original_document[-1]) + 1
        canvas.undo()
        restored = controller.diagram.routes[route.id]
        assert restored == route
        assert controller.diagram.representations[bus.representation_id].rotation_deg == 90.0
        assert electrical_signature(controller) == original_electrical
    finally:
        canvas.close()


def test_handle_hides_during_object_move_and_connection_and_returns_at_new_corner(app):
    controller, added = connected_fixture("move-handle")
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        handle = canvas.scene._rotation_handle
        original_grip = handle.scenePos()
        original_electrical = electrical_signature(controller)
        start, finish = item.scenePos(), item.scenePos() + QPointF(120, 40)
        mouse_at(canvas, start, "press")
        mouse_at(canvas, finish)
        assert canvas.view.tool_state.tool is EditorTool.DRAG_OBJECT
        assert not handle.isVisible()
        mouse_at(canvas, finish, "release")
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        assert handle.isVisible()
        assert handle.scenePos() == original_grip + QPointF(120, 40)
        assert electrical_signature(controller) == original_electrical
        source = item.port_item(added.port_ids[0])
        canvas.scene.begin_connection(source)
        assert canvas.view.tool_state.tool is EditorTool.DRAW_CONNECTION
        assert not handle.isVisible()
        canvas.scene.cancel_connection()
        assert canvas.view.tool_state.tool is EditorTool.SELECT
        assert handle.isVisible()
        assert electrical_signature(controller) == original_electrical
    finally:
        canvas.close()


def test_mouse_wheel_keeps_rotation_grip_at_fixed_screen_distance(app):
    controller, added = connected_fixture("wheel-gap")
    canvas = show_canvas(controller)
    try:
        item = select_object(canvas, added.representation_id)
        handle = canvas.scene._rotation_handle

        def screen_offset():
            corner = item.mapRectToScene(item.symbol_ink_rect()).topRight()
            delta = handle.scenePos() - corner
            return delta.x() * canvas.view.zoom_factor, delta.y() * canvas.view.zoom_factor

        assert screen_offset() == pytest.approx((22, -22))
        point = canvas.view.mapFromScene(item.scenePos())
        global_point = canvas.view.viewport().mapToGlobal(point)
        wheel = QWheelEvent(QPointF(point), QPointF(global_point), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(canvas.view.viewport(), wheel)
        QApplication.processEvents()
        assert canvas.view.zoom_factor > 1.0
        assert screen_offset() == pytest.approx((22, -22))
    finally:
        canvas.close()


def test_rotated_wire_exits_correct_port_side_and_stays_outside_equipment_body(app):
    controller = controller_for("outside-body")
    transformer = controller.add_equipment(
        "builtin.transformer_2w", "Т1", x=300, y=90,
        rotation_deg=90, width=100, height=60,
        voltage_class_by_group={"hv": VoltageClassId("builtin.voltage.ac.110kv"), "lv": U10},
    )
    breaker = controller.add_equipment(
        "builtin.circuit_breaker", "QF1", x=300, y=260,
        rotation_deg=90, width=100, height=60, voltage_class_by_group={"main": U10},
    )
    connected = controller.connect_ports(transformer.port_ids[-1], breaker.port_ids[0])
    canvas = show_canvas(controller)
    try:
        original_electrical = electrical_signature(controller)
        original_document = document_signature(controller)
        end = begin_drag(canvas, breaker.representation_id)
        preview_path = QPainterPath(canvas.scene._route_items_by_id[connected.route_id]._path)
        body_interior = QRectF(250, 230, 100, 60).adjusted(1e-6, 1e-6, -1e-6, -1e-6)
        assert preview_path.currentPosition() == QPointF(350, 260)
        before_last = preview_path.elementAt(preview_path.elementCount() - 2)
        assert before_last.x > 350.0
        assert before_last.y == 260.0
        assert not path_enters_rectangle(preview_path, body_interior)
        assert document_signature(controller) == original_document
        mouse_at(canvas, end, "release")
        committed_path = canvas.scene._route_items_by_id[connected.route_id]._path
        assert committed_path == preview_path
        assert not path_enters_rectangle(committed_path, body_interior)
        assert electrical_signature(controller) == original_electrical
    finally:
        canvas.close()


def test_legacy_vertical_bus_without_fraction_keeps_its_original_local_attachment(app):
    controller = controller_for("legacy-bus")
    bus = controller.add_electrical_node(
        "Старая вертикальная шина", x=300, y=260, symbol_key="busbar_vertical",
        width=22, height=420, rotation_deg=0, voltage_class_id=U10,
    )
    load = controller.add_equipment(
        "builtin.load", "Нагрузка", x=100, y=500, rotation_deg=180,
        voltage_class_by_group={"main": U10},
    )
    points = tuple(RouteWaypoint(RouteWaypointId.new(), x, y, RouteWaypointSource.AUTOMATIC)
                   for x, y in ((100, 525), (300, 525), (300, 365)))
    connected = controller.connect_port_to_node(
        load.port_ids[0], bus.node_id,
        source_representation_id=load.representation_id,
        node_representation_id=bus.representation_id,
        route_waypoints=points,
    )
    route = controller.diagram.routes[connected.route_id]
    # Emulate a saved legacy drawing: current connection commands already
    # materialize the fraction, but old routes stored only the endpoint.
    original_route = replace(route, end_anchor=replace(route.end_anchor, anchor_key=""))
    controller._project.diagram = replace(
        controller.diagram,
        routes={**controller.diagram.routes, connected.route_id: original_route},
    )
    controller = ProjectEditorController(controller._project)
    assert original_route.end_anchor.anchor_key == ""
    canvas = show_canvas(controller)
    try:
        original_electrical = electrical_signature(controller)
        original_document = document_signature(controller)
        end = begin_drag(canvas, bus.representation_id)
        assert canvas.scene._items_by_id[bus.representation_id].rotation() == 90.0
        path = canvas.scene._route_items_by_id[connected.route_id]._path
        assert path.currentPosition() == QPointF(195, 260)
        assert document_signature(controller) == original_document
        mouse_at(canvas, end, "release")
        rotated_route = controller.diagram.routes[connected.route_id]
        assert (rotated_route.waypoints[-1].x, rotated_route.waypoints[-1].y) == (195.0, 260.0)
        assert rotated_route.end_anchor == original_route.end_anchor
        assert electrical_signature(controller) == original_electrical
        canvas.undo()
        assert controller.diagram.routes[connected.route_id] == original_route
        assert electrical_signature(controller) == original_electrical
    finally:
        canvas.close()
