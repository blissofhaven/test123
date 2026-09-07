"""Electrical connection gestures use the real menu and one atomic command."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from rza_calc.domain.diagram import DiagramRouteKind
from rza_calc.domain.electrical import LineKind
from rza_calc.editor.connection_tool import ConnectionTargetFeedback
from test_ui_direct_connections import (
    U10, U110, _controller, _drag_start, _mouse, _pair, _state, canvas_factory,
)


def answer_real_menu(canvas, choice, timers):
    """Drive QMenu.exec with Qt input; a watchdog makes regressions bounded."""
    calls = []
    poll = QTimer(canvas)
    watchdog = QTimer(canvas)
    watchdog.setSingleShot(True)
    timers.extend((poll, watchdog))

    def popup():
        return next((widget for widget in QApplication.topLevelWidgets()
                     if isinstance(widget, QMenu) and widget.isVisible()
                     and widget.title() == "Чем соединить"), None)

    def select():
        menu = popup()
        if menu is None:
            return
        poll.stop()
        call = {
            "titles": [action.text() for action in menu.actions()],
            "active_draft": canvas.scene.connection_active,
            "project": _state(canvas),
            "timed_out": False,
            "visible": menu.isVisible(),
        }
        calls.append(call)
        if choice is None:
            QTest.keyClick(menu, Qt.Key.Key_Escape)
        else:
            keys = [key for key, _ in canvas.DRAGGED_CONNECTION_CHOICES]
            menu.setActiveAction(menu.actions()[keys.index(choice)])
            QTest.keyClick(menu, Qt.Key.Key_Return)
        watchdog.stop()

    def timeout():
        poll.stop()
        menu = popup()
        if menu is not None:
            calls.append({"timed_out": True})
            menu.close()
    poll.timeout.connect(select)
    poll.start(1)
    watchdog.timeout.connect(timeout)
    watchdog.start(1500)
    return calls


@pytest.fixture
def menu_reply():
    timers = []
    yield lambda canvas, choice: answer_real_menu(canvas, choice, timers)
    for timer in timers:
        timer.stop()


def finish(canvas, ports, gesture):
    if gesture == "drag":
        _, end = _drag_start(canvas, ports)
        _mouse(canvas, "release", end)
    else:
        _mouse(canvas, "move", ports[0].scenePos())
        _mouse(canvas, "click", ports[0].scenePos())
        # QTest can suppress a move to the same global position left by a
        # preceding test; traverse the route as a real pointer would.
        _mouse(canvas, "move", (ports[0].scenePos() + ports[1].scenePos()) / 2)
        _mouse(canvas, "move", ports[1].scenePos())
        if gesture == "click":
            _mouse(canvas, "click", ports[1].scenePos())
        else:
            QTest.keyClick(canvas.view, Qt.Key.Key_Return)
            QApplication.processEvents()


def assert_one_menu(canvas, calls, before):
    assert len(calls) == 1
    assert calls[0]["titles"] == [title for _, title in canvas.DRAGGED_CONNECTION_CHOICES]
    assert calls[0]["visible"] and not calls[0]["timed_out"]
    assert not calls[0]["active_draft"]
    assert calls[0]["project"] == before
    assert not canvas.scene.connection_active


@pytest.mark.parametrize("gesture", ["drag", "click", "enter"])
@pytest.mark.parametrize("choice", ["wire", "overhead", "cable", None])
def test_menu_choice_and_escape_have_the_same_meaning_for_every_finish(
    canvas_factory, menu_reply, gesture, choice,
):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    calls = menu_reply(canvas, choice)
    finish(canvas, ports, gesture)
    assert_one_menu(canvas, calls, before)
    if choice is None:
        assert _state(canvas) == before
        assert len(canvas.controller.journal) == journal
        _mouse(canvas, "release", ports[1].scenePos())
        assert _state(canvas) == before  # no delayed release command
        return
    assert len(canvas.controller.journal) == journal + 1
    routes = list(canvas.controller.diagram.routes.values())
    assert len(routes) == 1
    if choice == "wire":
        assert routes[0].kind is DiagramRouteKind.NODE_CONNECTION
        assert not canvas.controller.model.line_sections
        nodes = {canvas.controller.model.connection_for_port(p.port_id).electrical_node_id for p in ports}
        assert len(nodes) == 1
    else:
        assert routes[0].kind is DiagramRouteKind.EQUIPMENT_BRANCH
        section, = canvas.controller.model.line_sections.values()
        assert section.length_mm is None
        logical = canvas.controller.model.logical_lines[section.logical_line_id]
        assert logical.line_kind is (LineKind.CABLE if choice == "cable" else LineKind.OVERHEAD)
    after = _state(canvas)
    canvas.undo()
    assert _state(canvas) == before
    canvas.redo()
    assert _state(canvas) == after


@pytest.mark.parametrize("gesture", ["drag", "click", "enter"])
@pytest.mark.parametrize("choice", ["wire", "overhead", "cable", None])
def test_same_node_allows_explicit_line_insertion_but_never_another_wire(
    canvas_factory, menu_reply, gesture, choice,
):
    canvas, ports = _pair(canvas_factory)
    source_id, target_id = (p.port_id for p in ports)
    canvas.controller.connect_ports(source_id, target_id)
    canvas.refresh()
    ports = tuple(canvas.scene._items_by_id[p.representation_id].port_item(p.port_id) for p in ports)
    old_node = canvas.controller.model.connection_for_port(source_id).electrical_node_id
    before, journal = _state(canvas), len(canvas.controller.journal)
    calls = menu_reply(canvas, choice)
    finish(canvas, ports, gesture)
    assert_one_menu(canvas, calls, before)
    if choice in (None, "wire"):
        assert _state(canvas) == before
        assert len(canvas.controller.journal) == journal
        return
    assert len(canvas.controller.journal) == journal + 1
    model = canvas.controller.model
    new_node = model.connection_for_port(source_id).electrical_node_id
    assert new_node != old_node
    assert model.connection_for_port(target_id).electrical_node_id == old_node
    section, = model.line_sections.values()
    line_nodes = {model.connection_for_port(model.port_by_role(section.equipment_id, role).id).electrical_node_id
                  for role in ("from", "to")}
    assert line_nodes == {new_node, old_node}
    assert not canvas.controller.diagram.validate_targets(model)
    after = _state(canvas)
    canvas.undo()
    assert _state(canvas) == before
    canvas.redo()
    assert _state(canvas) == after


@pytest.mark.parametrize("gesture", ["drag", "click", "enter"])
def test_incompatible_voltage_never_reaches_menu_or_history(canvas_factory, menu_reply, gesture):
    canvas, ports = _pair(canvas_factory, target_voltage=U110)
    before, journal = _state(canvas), len(canvas.controller.journal)
    calls = menu_reply(canvas, "wire")
    finish(canvas, ports, gesture)
    assert calls == []
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert not canvas.scene.connection_active


@pytest.mark.parametrize("held", [False, True])
def test_escape_before_finish_cancels_without_menu_or_late_commit(canvas_factory, menu_reply, held):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    calls = menu_reply(canvas, "wire")
    if held:
        _drag_start(canvas, ports)
    else:
        _mouse(canvas, "click", ports[0].scenePos())
        _mouse(canvas, "move", ports[1].scenePos())
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    _mouse(canvas, "release", ports[1].scenePos())
    assert calls == [] and not canvas.scene.connection_active
    assert _state(canvas) == before and len(canvas.controller.journal) == journal


@pytest.mark.parametrize("gesture", ["enter", "double_click"])
@pytest.mark.parametrize("choice", ["wire", "overhead", "cable", None])
def test_free_endpoint_completion_uses_real_menu(canvas_factory, menu_reply, gesture, choice):
    canvas, ports = _pair(canvas_factory)
    errors = []
    canvas.errorOccurred.connect(errors.append)
    before, journal = _state(canvas), len(canvas.controller.journal)
    calls = menu_reply(canvas, choice)
    target = QPointF(100, 180)
    _mouse(canvas, "move", ports[0].scenePos())
    _mouse(canvas, "click", ports[0].scenePos())
    _mouse(canvas, "move", QPointF(40, 100))
    _mouse(canvas, "move", target)
    assert canvas.scene._connection_target is None
    assert canvas.scene._connection_tool.cursor_vertex.point == (target.x(), target.y())
    if gesture == "enter":
        QTest.keyClick(canvas.view, Qt.Key.Key_Return)
    else:
        QTest.mouseDClick(canvas.view.viewport(), Qt.MouseButton.LeftButton,
                         pos=canvas.view.mapFromScene(target))
    QApplication.processEvents()
    assert_one_menu(canvas, calls, before)
    if choice is None:
        assert _state(canvas) == before and len(canvas.controller.journal) == journal
    else:
        assert len(canvas.controller.journal) == journal + 1, errors
        route, = canvas.controller.diagram.routes.values()
        assert route.kind is (DiagramRouteKind.NODE_CONNECTION if choice == "wire"
                              else DiagramRouteKind.EQUIPMENT_BRANCH)
        assert (route.waypoints[-1].x, route.waypoints[-1].y) == (target.x(), target.y())
        canvas.undo()
        assert _state(canvas) == before


@pytest.mark.parametrize("target_kind", ["node", "port"])
@pytest.mark.parametrize("gesture", ["drag", "click", "enter"])
@pytest.mark.parametrize("choice", ["wire", "overhead", "cable", None])
def test_unknown_port_adopts_known_target_only_after_menu_choice(
    canvas_factory, menu_reply, target_kind, gesture, choice,
):
    controller = _controller()
    blank = controller.add_equipment("builtin.circuit_breaker", "Новый аппарат", x=0, y=0)
    source_id = blank.port_ids[-1]
    assert controller.model.port_voltage_class(source_id) is None
    if target_kind == "port":
        target = controller.add_equipment("builtin.circuit_breaker", "Аппарат 10 кВ", x=240, y=0,
                                          voltage_class_by_group={"main": U10})
    else:
        target = controller.add_electrical_node("Шины 10 кВ", x=240, y=0,
                                                symbol_key="busbar_horizontal", width=180, height=20,
                                                voltage_class_id=U10)
    canvas = canvas_factory(controller)
    source_item = canvas.scene._items_by_id[blank.representation_id].port_item(source_id)
    target_item = canvas.scene._items_by_id[target.representation_id]
    if target_kind == "port":
        target_item = target_item.port_item(target.port_ids[0])
    before, journal = _state(canvas), len(controller.journal)
    calls = menu_reply(canvas, choice)
    finish(canvas, (source_item, target_item), gesture)
    assert_one_menu(canvas, calls, before)
    if choice is None:
        assert _state(canvas) == before and len(controller.journal) == journal
        assert controller.model.port_voltage_class(source_id) is None
    else:
        assert controller.model.port_voltage_class(source_id) == U10
        assert len(controller.journal) == journal + 1
        after = _state(canvas)
        canvas.undo()
        assert _state(canvas) == before
        assert controller.model.port_voltage_class(source_id) is None
        canvas.redo()
        assert _state(canvas) == after


@pytest.mark.parametrize("gesture", ["drag", "click", "enter", "escape"])
def test_real_route_endpoint_move_preserves_line_identity_without_type_menu(
    canvas_factory, menu_reply, gesture,
):
    canvas, ports = _pair(canvas_factory)
    # Build the route through the same gesture so its endpoints really sit
    # on terminals rather than default representation centres inside bodies.
    creation_menu = menu_reply(canvas, "overhead")
    finish(canvas, ports, "drag")
    assert len(creation_menu) == 1
    original_section, = canvas.controller.model.line_sections.values()
    section_id = original_section.equipment_id
    route_id = next(iter(canvas.controller.diagram.routes))
    target = canvas.controller.add_electrical_node("Новый конец", x=350, y=150,
                                                  voltage_class_id=U10)
    canvas.refresh()
    route_item = canvas.scene._route_items_by_id[route_id]
    route_item.setSelected(True)
    QApplication.processEvents()
    handle = route_item._endpoint_handles[False]
    before, journal = _state(canvas), len(canvas.controller.journal)
    calls = menu_reply(canvas, "cable")
    start = handle.scenePos()
    end = canvas.scene._items_by_id[target.representation_id].scenePos()
    _mouse(canvas, "move", start)
    _mouse(canvas, "press" if gesture == "drag" else "click", start)
    assert canvas.scene._connection_tool.from_route_endpoint
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.COMPATIBLE, canvas.scene._connection_target.message
    if gesture == "drag":
        _mouse(canvas, "release", end)
    elif gesture == "click":
        _mouse(canvas, "click", end)
    else:
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape if gesture == "escape" else Qt.Key.Key_Return)
        QApplication.processEvents()
    assert calls == []
    assert not canvas.scene.connection_active
    if gesture == "escape":
        assert _state(canvas) == before and len(canvas.controller.journal) == journal
        return
    assert len(canvas.controller.journal) == journal + 1
    assert canvas.controller.model.line_sections[section_id] == original_section
    updated = canvas.controller.diagram.routes[route_id]
    assert updated.end_anchor.electrical_node_id == target.node_id
    assert canvas.controller.model.logical_lines[original_section.logical_line_id].line_kind is LineKind.OVERHEAD
    canvas.undo()
    assert _state(canvas) == before
