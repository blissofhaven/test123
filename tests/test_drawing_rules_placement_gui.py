"""Real palette/drag gestures commit the exact ordinary-wire proposal once."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.diagram import DiagramRouteKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import NodeTarget
from rza_calc.gui.editor_scene import EditorCanvas, EQUIPMENT_MIME_TYPE
from test_stage4_editor_interaction import _controller, _U10


@pytest.fixture(scope="module", autouse=True)
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture
def canvas():
    controller = _controller()
    controller.add_electrical_node("Шина", x=0, y=0, voltage_class_id=_U10,
                                   symbol_key="busbar", width=240, height=12)
    widget = EditorCanvas(controller)
    widget.resize(1000, 700)
    widget.show()
    widget.view.actual_size()
    widget.view.centerOn(0, 60)
    widget.view.setFocus()
    widget.set_snap_enabled(False)
    widget._save_viewport(widget.view.viewport_state())
    QApplication.processEvents()
    yield widget
    widget.close()
    QApplication.processEvents()


def _payload(kind="builtin.load"):
    return {"type_id": kind, "name": "Новый аппарат",
            "graphics": {"rotation_deg": 90, "width": 80, "height": 50}}


def _near_bus(canvas, payload, x=0):
    geometry = canvas.view._equipment_preview_geometry(payload, QPointF(), rotation_deg=90)
    zone = min(geometry.port_connection_zones, key=lambda p: p.shape.center_y)
    return QPointF(x - zone.shape.center_x, 8 - zone.shape.center_y)


def _at(canvas, point):
    return canvas.view.mapFromScene(point)


def _assert_committed(canvas, proposal, count):
    controller = canvas.controller
    assert len(controller.journal) == count + 1
    representation = next(r for r in controller.diagram.representations.values()
                          if r.equipment_id is not None)
    assert (representation.x, representation.y) == (proposal.x, proposal.y)
    assert representation.rotation_deg == proposal.rotation_deg
    assert len(controller.diagram.routes) == 1
    route = next(iter(controller.diagram.routes.values()))
    assert route.kind is DiagramRouteKind.NODE_CONNECTION
    assert tuple((p.x, p.y) for p in route.waypoints) == tuple((p.x, p.y) for p in proposal.wire_points)
    assert (route.waypoints[-1].x, route.waypoints[-1].y) == proposal.target_point
    assert controller.model.node_for_port(controller.model.equipment[representation.equipment_id].port_ids[0]).id == route.electrical_node_id
    assert not controller.diagram.validate_targets(controller.model)
    return representation, route


@pytest.mark.parametrize("kind", ["builtin.load", "builtin.circuit_breaker"])
def test_palette_green_preview_release_one_command_and_undo(canvas, kind):
    payload = _payload(kind)
    point = _near_bus(canvas, payload)
    canvas.view.begin_placement(payload)
    count = len(canvas.controller.journal)
    before = electrical_model_fingerprint(canvas.controller.model)
    QTest.mouseMove(canvas.view.viewport(), _at(canvas, point))
    proposal = canvas.view._placement_proposal
    assert proposal is not None and proposal.valid, getattr(proposal, "reason", None)
    assert proposal.action == "attach" and proposal.wire_points
    assert electrical_model_fingerprint(canvas.controller.model) == before
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, point))
    assert len(canvas.controller.journal) == count
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, point))
    _assert_committed(canvas, proposal, count)
    representation = next(r for r in canvas.controller.diagram.representations.values() if r.equipment_id)
    geometry = canvas.view._equipment_preview_geometry(payload, QPointF(proposal.x, proposal.y),
                                                       rotation_deg=proposal.rotation_deg)
    actual_ports = canvas.scene._items_by_id[representation.id]._port_items.values()
    actual_points = sorted((p.scenePos().y(), p.scenePos().x()) for p in actual_ports)
    expected_points = sorted((z.shape.center_y, z.shape.center_x) for z in geometry.port_connection_zones)
    assert len(actual_points) == len(expected_points)
    for actual, expected in zip(actual_points, expected_points):
        assert actual == pytest.approx(expected, abs=1e-9)
    assert electrical_model_fingerprint(canvas.controller.model) != before
    canvas.undo()
    assert electrical_model_fingerprint(canvas.controller.model) == before
    assert not canvas.controller.diagram.routes
    canvas.redo()
    assert len(canvas.controller.diagram.routes) == 1


def test_palette_cancel_and_final_release_position(canvas):
    payload = _payload()
    first, last = _near_bus(canvas, payload, -60), _near_bus(canvas, payload, 60)
    before, count = canvas.controller.diagram, len(canvas.controller.journal)
    canvas.view.begin_placement(payload)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, first))
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, last))
    assert canvas.controller.diagram == before and len(canvas.controller.journal) == count
    canvas.view.begin_placement(payload)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, first))
    # Deliberately omit a move: release must reevaluate its own final position.
    expected = canvas._preview_equipment_placement(payload, canvas.view.mapToScene(_at(canvas, last)))
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, last))
    _assert_committed(canvas, expected, count)


def test_nearest_free_proposal_moves_only_new_apparatus(canvas):
    old = canvas.controller.add_equipment("builtin.load", "Существующий", x=0, y=180)
    canvas.refresh()
    original = canvas.controller.diagram.representations[old.representation_id]
    payload = _payload()
    canvas.view.begin_placement(payload)
    point = QPointF(original.x, original.y)
    QTest.mouseMove(canvas.view.viewport(), _at(canvas, point))
    proposal = canvas.view._placement_proposal
    assert proposal.valid and proposal.adjusted and proposal.action == "place"
    assert (proposal.x, proposal.y) != (original.x, original.y)
    count = len(canvas.controller.journal)
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, point))
    assert len(canvas.controller.journal) == count + 1
    assert canvas.controller.diagram.representations[original.id] == original
    added = next(r for r in canvas.controller.diagram.representations.values()
                 if r.equipment_id is not None and r.id != original.id)
    assert (added.x, added.y) == (proposal.x, proposal.y)


@pytest.mark.parametrize("cancel", [False, True])
def test_existing_free_apparatus_move_previews_and_commits_atomically(canvas, cancel):
    added = canvas.controller.add_equipment("builtin.load", "Свободный", x=0, y=180, rotation_deg=90)
    canvas.refresh()
    point = _near_bus(canvas, _payload())
    start = QPointF(0, 180)
    before, count = canvas.controller.diagram, len(canvas.controller.journal)
    fingerprint = electrical_model_fingerprint(canvas.controller.model)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, start))
    QTest.mouseMove(canvas.view.viewport(), _at(canvas, point))
    proposal = canvas.scene._body_attachment_proposal
    assert proposal is not None and proposal.valid, getattr(proposal, "reason", None)
    assert proposal.action == "attach" and canvas.scene._placement_wire_preview.isVisible()
    assert canvas.controller.diagram == before and electrical_model_fingerprint(canvas.controller.model) == fingerprint
    if cancel:
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, point))
    if cancel:
        assert canvas.controller.diagram == before and len(canvas.controller.journal) == count
        assert canvas.scene._items_by_id[added.representation_id].pos() == start
        assert not canvas.scene._placement_wire_preview.isVisible()
    else:
        _assert_committed(canvas, proposal, count)
        canvas.undo()
        assert canvas.controller.diagram == before


def test_connected_apparatus_move_never_uses_autoattachment(canvas, monkeypatch):
    added = canvas.controller.add_equipment("builtin.load", "Подключённый", x=0, y=180, rotation_deg=90)
    bus = next(r for r in canvas.controller.diagram.representations.values() if r.electrical_node_id)
    canvas.controller.connect_port_to_node(added.port_ids[0], bus.electrical_node_id,
        source_representation_id=added.representation_id, node_representation_id=bus.id)
    canvas.refresh()
    calls = []
    monkeypatch.setattr(canvas.scene, "_equipment_move_preview", lambda *args: calls.append(args))
    before = electrical_model_fingerprint(canvas.controller.model)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, QPointF(0, 180)))
    QTest.mouseMove(canvas.view.viewport(), _at(canvas, QPointF(100, 180)))
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, QPointF(100, 180)))
    assert not calls
    assert electrical_model_fingerprint(canvas.controller.model) == before


def test_qf_over_ordinary_wire_previews_both_terminals_and_releases_once(canvas):
    controller = canvas.controller
    load = controller.add_equipment("builtin.load", "Нагрузка", x=0, y=240, rotation_deg=90)
    bus = next(r for r in controller.diagram.representations.values() if r.electrical_node_id)
    controller.connect_port_to_node(load.port_ids[0], bus.electrical_node_id,
        source_representation_id=load.representation_id, node_representation_id=bus.id)
    canvas.refresh()
    original, count = controller.diagram, len(controller.journal)
    fingerprint = electrical_model_fingerprint(controller.model)
    point = QPointF(0, 100)
    canvas.view.begin_placement(_payload("builtin.circuit_breaker"))
    QTest.mouseMove(canvas.view.viewport(), _at(canvas, point))
    proposal = canvas.view._placement_proposal
    assert proposal is not None and proposal.valid, getattr(proposal, "reason", None)
    assert proposal.action == "inline" and proposal.wire_points and proposal.extra_wire_points
    assert controller.diagram == original and electrical_model_fingerprint(controller.model) == fingerprint
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, point))
    assert len(controller.journal) == count + 1
    qf = next(e for e in controller.model.equipment.values() if e.type_id.value == "builtin.circuit_breaker")
    nodes = {controller.model.node_for_port(p).id for p in qf.port_ids}
    assert len(nodes) == 2
    routes = tuple(controller.diagram.routes.values())
    assert len(routes) == 2 and all(r.kind is DiagramRouteKind.NODE_CONNECTION for r in routes)
    assert {tuple((p.x, p.y) for p in r.waypoints) for r in routes} == {
        tuple((p.x, p.y) for p in proposal.wire_points), tuple((p.x, p.y) for p in proposal.extra_wire_points)}
    assert not controller.diagram.validate_targets(controller.model)
    canvas.undo()
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert controller.diagram == original
    canvas.redo()
    assert len(controller.diagram.routes) == 2


def test_external_palette_drag_uses_same_green_proposal_on_drop(canvas):
    import json
    payload = _payload()
    point = _at(canvas, _near_bus(canvas, payload))
    mime = QMimeData()
    mime.setData(EQUIPMENT_MIME_TYPE, json.dumps(payload).encode("utf-8"))
    enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    move = QDragMoveEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.view.viewport(), enter)
    QApplication.sendEvent(canvas.view.viewport(), move)
    assert enter.isAccepted() and move.isAccepted()
    proposal = canvas.view._placement_proposal
    assert proposal is not None and proposal.valid and proposal.action == "attach"
    count = len(canvas.controller.journal)
    drop = QDropEvent(QPointF(point), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.view.viewport(), drop)
    assert drop.isAccepted()
    _assert_committed(canvas, proposal, count)
    assert canvas.view._external_drag_payload is None


def test_focus_loss_cancels_pressed_palette_without_late_commit(canvas):
    payload = _payload()
    point = _at(canvas, _near_bus(canvas, payload))
    canvas.view.begin_placement(payload)
    before, count = canvas.controller.diagram, len(canvas.controller.journal)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.sendEvent(canvas.view, QFocusEvent(QFocusEvent.Type.FocusOut))
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.controller.diagram == before and len(canvas.controller.journal) == count
    assert canvas.view._placement_proposal is None


def test_two_attachments_get_distinct_bus_contacts_without_moving_first(canvas):
    payload = _payload("builtin.circuit_breaker")
    point = _at(canvas, _near_bus(canvas, payload))
    canvas.view.begin_placement(payload)
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    first_rows = dict(canvas.controller.diagram.representations)
    first_route = next(iter(canvas.controller.diagram.routes.values()))
    # Approach the same electrical bus contact from the opposite side. Contact
    # allocation must also separate top and bottom connections visibly.
    payload["graphics"]["rotation_deg"] = 270
    geometry = canvas.view._equipment_preview_geometry(payload, QPointF(), rotation_deg=270)
    zone = max(geometry.port_connection_zones, key=lambda p: p.shape.center_y)
    point = _at(canvas, QPointF(-zone.shape.center_x, -8 - zone.shape.center_y))
    canvas.view.begin_placement(payload)
    QTest.mouseMove(canvas.view.viewport(), point)
    # Qt need not emit a new move for identical pixels; press evaluates again.
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    proposal = canvas.view._placement_proposal
    assert proposal is not None and proposal.valid and proposal.action == "attach"
    assert proposal.target_point != (first_route.waypoints[-1].x, first_route.waypoints[-1].y)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    for identifier, row in first_rows.items():
        assert canvas.controller.diagram.representations[identifier] == row
    routes = tuple(canvas.controller.diagram.routes.values())
    assert len(routes) == 2
    assert abs(routes[0].waypoints[-1].x - routes[1].waypoints[-1].x) >= 20


def test_free_body_release_uses_final_position_and_plain_click_does_nothing(canvas):
    added = canvas.controller.add_equipment("builtin.load", "Свободный", x=0, y=180, rotation_deg=90)
    canvas.refresh()
    start = _at(canvas, QPointF(0, 180))
    count, before = len(canvas.controller.journal), canvas.controller.diagram
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    assert len(canvas.controller.journal) == count and canvas.controller.diagram == before
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_at(canvas, _near_bus(canvas, _payload())))
    assert len(canvas.controller.journal) == count + 1
    assert canvas.controller.model.node_for_port(added.port_ids[0]) is not None


@pytest.mark.parametrize("outcome", ["cancel", "leave", "commit"])
def test_existing_inline_rotation_preview_matches_ports_and_restores_on_cancel(canvas, outcome):
    controller = canvas.controller
    load = controller.add_equipment("builtin.load", "Нагрузка", x=0, y=300, rotation_deg=90)
    bus = next(r for r in controller.diagram.representations.values() if r.electrical_node_id)
    controller.connect_port_to_node(load.port_ids[0], bus.electrical_node_id,
        source_representation_id=load.representation_id, node_representation_id=bus.id)
    qf = controller.add_equipment("builtin.circuit_breaker", "Свободный QF", x=-180, y=160, rotation_deg=180)
    canvas.refresh()
    original, count = controller.diagram, len(controller.journal)
    item = canvas.scene._items_by_id[qf.representation_id]
    angle = item.rotation()
    start, end = _at(canvas, QPointF(-180, 160)), _at(canvas, QPointF(0, 160))
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas.view.viewport(), end)
    proposal = canvas.scene._body_attachment_proposal
    assert proposal.valid and proposal.action == "inline"
    assert item.rotation() == proposal.rotation_deg != angle
    assert item._label.rotation() == -item.rotation()
    path_ends = [(p.x, p.y) for row in (proposal.wire_points, proposal.extra_wire_points)
                 for p in (row[0], row[-1])]
    for port in item._port_items.values():
        assert any((port.scenePos().x(), port.scenePos().y()) == pytest.approx(point, abs=1e-9)
                   for point in path_ends)
    assert controller.diagram == original and len(controller.journal) == count
    if outcome == "leave":
        QTest.mouseMove(canvas.view.viewport(), _at(canvas, QPointF(-160, 180)))
        assert item.rotation() == angle
        assert canvas.scene._body_attachment_proposal.action == "place"
    if outcome != "commit":
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    if outcome == "commit":
        assert controller.diagram.representations[qf.representation_id].rotation_deg == proposal.rotation_deg
        assert len(controller.journal) == count + 1
        canvas.undo()
    assert controller.diagram == original
    assert item.rotation() == angle and item._label.rotation() == -angle
    assert item.pos() == QPointF(-180, 160)


def test_rejected_stale_placement_clears_green_permission(canvas):
    payload = _payload("builtin.circuit_breaker")
    canvas.view.begin_placement(payload)
    QTest.mouseMove(canvas.view.viewport(), _at(canvas, _near_bus(canvas, payload)))
    proposal = canvas.view._placement_proposal
    assert proposal.valid and proposal.action == "attach"
    # A real controller edit invalidates the already-visible proposal.
    canvas.controller.add_electrical_node("Поздняя правка", x=400, y=300, voltage_class_id=_U10)
    count = len(canvas.controller.journal)
    canvas._apply_prepared_equipment(proposal)
    assert len(canvas.controller.journal) == count and not canvas.controller.model.equipment
    assert not canvas.view._placement_valid and canvas.view._placement_proposal is None
