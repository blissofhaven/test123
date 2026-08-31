"""Direct terminal gestures are drafts until an explicit, atomic choice."""
from __future__ import annotations

from dataclasses import replace
from collections import defaultdict
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu

from rza_calc.domain.electrical import DataConfirmation, LineKind, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.connection_tool import ConnectionTargetFeedback
from rza_calc.editor.controller import PhysicalLineInput, PortTarget
from rza_calc.editor.connection_voltage import endpoint_voltage
from rza_calc.editor.orthogonal_routing import RoutingError
from rza_calc.editor.tool_state import EditorTool
from rza_calc.gui.editor_scene import CanvasMode, EditorCanvas, HitTestKind
from rza_calc.io.project import load_project
from test_stage4_editor_interaction import _controller

_APP = None
U10 = VoltageClassId("builtin.voltage.ac.10kv")
U110 = VoltageClassId("builtin.voltage.ac.110kv")


@pytest.fixture
def canvas_factory(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    errors, widgets = [], []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb:
                        errors.append("".join(traceback.format_exception(kind, value, tb))))

    def create(controller):
        canvas = EditorCanvas(controller)
        widgets.append(canvas)
        canvas.resize(1000, 740)
        canvas.show()
        canvas.view.actual_size()
        canvas.set_snap_enabled(False)
        canvas.view.centerOn(120, 0)
        canvas.view.setFocus()
        QApplication.processEvents()
        return canvas

    yield create
    for canvas in widgets:
        canvas.close()
    QApplication.processEvents()
    assert not errors, "\n".join(errors)


def _pair(canvas_factory, *, target_voltage=U10):
    controller = _controller()
    first = controller.add_equipment("builtin.circuit_breaker", "Начало", x=0, y=0,
                                     voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "Конец", x=240, y=0,
                                      voltage_class_by_group={"main": target_voltage})
    canvas = canvas_factory(controller)
    ports = (canvas.scene._items_by_id[first.representation_id].port_item(first.port_ids[-1]),
             canvas.scene._items_by_id[second.representation_id].port_item(second.port_ids[0]))
    return canvas, ports


def _state(canvas):
    return (electrical_model_fingerprint(canvas.controller.model),
            canvas.controller.model.connectivity_signature(),
            dict(canvas.controller.diagram.representations), dict(canvas.controller.diagram.routes))


def _mouse(canvas, event, point, button=Qt.MouseButton.LeftButton):
    position = canvas.view.mapFromScene(point)
    if event == "move":
        QTest.mouseMove(canvas.view.viewport(), position, delay=1)
    else:
        {"press": QTest.mousePress, "release": QTest.mouseRelease, "click": QTest.mouseClick}[event](
            canvas.view.viewport(), button, pos=position)
    QApplication.processEvents()


def _drag_start(canvas, ports):
    start, end = (port.scenePos() for port in ports)
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    assert canvas.scene._connection_press_position is not None
    _mouse(canvas, "move", (start + end) / 2)
    _mouse(canvas, "move", end)
    assert canvas.scene._connection_dragged
    return start, end


def test_terminal_drag_previews_then_commits_one_ordinary_connection(canvas_factory, monkeypatch):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    commits = []
    original_commit = canvas._commit_connection_draft

    def commit(draft):
        assert _state(canvas) == before
        assert not canvas.scene.connection_active
        assert canvas.scene._connection_press_position is None
        commits.append(draft)
        original_commit(draft)

    monkeypatch.setattr(canvas, "_commit_connection_draft", commit)
    monkeypatch.setattr(QMenu, "exec", lambda *_: pytest.fail("A terminal drag must not open a menu"))
    _, end = _drag_start(canvas, ports)
    assert _state(canvas) == before
    assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.COMPATIBLE
    _mouse(canvas, "release", end)
    assert len(commits) == 1
    assert len(canvas.controller.journal) == journal + 1
    assert len(canvas.controller.model.connections) == 2
    assert len(canvas.controller.diagram.routes) == 1
    assert not canvas.controller.model.logical_lines
    canvas.undo()
    assert _state(canvas) == before


def test_short_click_retains_click_click_without_type_popup(canvas_factory, monkeypatch):
    canvas, ports = _pair(canvas_factory)
    monkeypatch.setattr(QMenu, "exec", lambda *_: pytest.fail("Click-click must not show a popup"))
    _mouse(canvas, "click", ports[0].scenePos())
    assert canvas.scene.connection_active and canvas.scene._connection_press_position is None
    assert not canvas.controller.model.connections
    _mouse(canvas, "click", ports[1].scenePos())
    assert len(canvas.controller.model.connections) == 2
    assert not canvas.scene.connection_active


@pytest.mark.parametrize("kind", (LineKind.OVERHEAD, LineKind.CABLE))
def test_physical_palette_creates_one_unconfirmed_branch_without_modal_parameters(canvas_factory, monkeypatch, kind):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: pytest.fail("Physical creation must not open a modal"))
    monkeypatch.setattr(QInputDialog, "getDouble", lambda *_args, **_kwargs: pytest.fail("Length belongs in the inspector"))
    start, end = (port.scenePos() for port in ports)
    canvas.view.begin_placement({"target_kind": "physical_line", "type_id": "physical_line." + kind.value, "name": "Новая физическая линия"})
    _mouse(canvas, "click", start)
    assert canvas.scene.physical_line_active
    _mouse(canvas, "move", end)
    assert _state(canvas) == before and not canvas.controller.model.logical_lines
    _mouse(canvas, "click", end)
    assert len(canvas.controller.journal) == journal + 1
    line = next(iter(canvas.controller.model.logical_lines.values()))
    section = next(iter(canvas.controller.model.line_sections.values()))
    assert line.line_kind is kind and section.length_mm is None
    assert len(canvas.controller.diagram.routes) == 1
    assert canvas.scene._route_items_by_id[next(iter(canvas.controller.diagram.routes))].direction_marker() is not None
    canvas.undo()
    assert _state(canvas) == before


@pytest.mark.parametrize("where", ("escape", "analysis"))
def test_cancel_physical_draft_leaves_no_electrical_side_effect(canvas_factory, where):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    start, end = (port.scenePos() for port in ports)
    assert canvas.scene.begin_physical_line(scene_pos=start, line_kind=LineKind.CABLE.value, name="КЛ")
    _mouse(canvas, "move", end)
    if where == "escape":
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    else:
        canvas.set_mode(CanvasMode.ANALYSIS)
    QApplication.processEvents()
    _mouse(canvas, "release", end)
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
    assert not canvas.scene.connection_active


def test_mismatched_voltage_is_red_and_release_cannot_show_choice_or_commit(canvas_factory, monkeypatch):
    canvas, ports = _pair(canvas_factory, target_voltage=U110)
    before, journal = _state(canvas), len(canvas.controller.journal)
    monkeypatch.setattr(canvas, "_commit_connection_draft", lambda *_: pytest.fail("Incompatible endpoints cannot commit"))
    _, end = _drag_start(canvas, ports)
    target = canvas.scene._connection_target
    assert target.feedback is ConnectionTargetFeedback.INCOMPATIBLE
    assert "напряжения" in target.message and "110" in target.message and "10" in target.message
    _mouse(canvas, "release", end)
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
    assert not canvas.scene.connection_active


def test_actual_legacy_undeclared_ports_use_topology_voltage_in_preview(canvas_factory):
    path = Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json"
    raw = path.read_bytes()
    from rza_calc.editor.controller import ProjectEditorController
    canvas = canvas_factory(ProjectEditorController(load_project(path)))
    ports = [port for item in canvas.scene._items_by_id.values()
             if item._canonical_key not in {"line", "line_section"} for port in item._port_items.values()]
    source = next(port for port in ports if endpoint_voltage(canvas.controller.model, port.port_id).voltage_class_id == U10)
    target = next(port for port in ports if endpoint_voltage(canvas.controller.model, port.port_id).voltage_class_id == U110)
    assert canvas.controller.model.port_voltage_class(source.port_id) is None
    assert canvas.controller.model.port_voltage_class(target.port_id) is None
    before = _state(canvas)
    canvas.scene.begin_connection(source)
    feedback, message = canvas.scene._compatibility_for_port(source.port_id, target.port_id)
    assert feedback is ConnectionTargetFeedback.INCOMPATIBLE
    assert "110" in message and "10" in message
    canvas.scene.cancel_connection()
    assert _state(canvas) == before and path.read_bytes() == raw


@pytest.mark.parametrize("physical", (False, True))
def test_unrouteable_preview_reports_failure_and_never_commits_stale_vertices(canvas_factory, monkeypatch, physical):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    messages = []
    canvas.statusMessage.connect(messages.append)
    if physical:
        assert canvas.scene.begin_physical_line(name="ВЛ", line_kind=LineKind.OVERHEAD,
                                               scene_pos=ports[0].scenePos())
        tool = canvas.scene._physical_line_tool
    else:
        canvas.scene.begin_connection(ports[0])
        tool = canvas.scene._connection_tool

    def blocked(*args, **kwargs):
        raise RoutingError("Нет безопасного ортогонального пути")

    monkeypatch.setattr(type(tool), "update", blocked)
    if physical:
        canvas.scene.update_physical_line_cursor(ports[1].scenePos())
        assert not canvas.scene.finish_physical_line(free_target=True)
    else:
        canvas.scene.update_connection_cursor(ports[1].scenePos())
        canvas.scene._emit_connection_draft(free_target=True)
    assert messages and "Нет безопасного" in messages[-1]
    assert not tool.preview_vertices
    assert _state(canvas) == before and len(canvas.controller.journal) == journal


@pytest.mark.parametrize("action", ("escape", "analysis", "focus", "pan", "ungrab", "sync"))
def test_terminal_drag_cancellation_has_no_late_release_commit(canvas_factory, monkeypatch, action):
    canvas, ports = _pair(canvas_factory)
    before, journal = _state(canvas), len(canvas.controller.journal)
    monkeypatch.setattr(canvas, "_commit_connection_draft", lambda *_: pytest.fail("A cancelled gesture cannot commit"))
    _, end = _drag_start(canvas, ports)
    if action == "escape":
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    elif action == "analysis":
        canvas.set_mode(CanvasMode.ANALYSIS)
    elif action == "focus":
        QApplication.sendEvent(canvas.view, QFocusEvent(QEvent.Type.FocusOut))
    elif action == "pan":
        canvas.view._tool_state.activate(EditorTool.PAN)
        canvas.view._emit_tool_state()
    elif action == "ungrab":
        QApplication.sendEvent(canvas.view.viewport(), QEvent(QEvent.Type.UngrabMouse))
    else:
        canvas.refresh()
    assert not canvas.scene.connection_active
    assert canvas.scene._connection_press_position is None
    _mouse(canvas, "release", end)
    assert _state(canvas) == before and len(canvas.controller.journal) == journal


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
@pytest.mark.parametrize("side", (-1, 1))
def test_drag_from_side_apparatus_projects_to_bus_end_without_moving_port(canvas_factory, monkeypatch, angle, side):
    controller = _controller()
    vertical = angle % 180 == 90
    apparatus = controller.add_equipment("builtin.circuit_breaker", "Сбоку от шины",
        x=0 if vertical else side * 260, y=side * 260 if vertical else 0,
        voltage_class_by_group={"main": U10})
    bus = controller.add_electrical_node("Шина", x=0, y=0, symbol_key="busbar_horizontal",
                                        width=180, height=20, voltage_class_id=U10)
    controller.rotate_representation(bus.representation_id, angle)
    canvas = canvas_factory(controller)
    canvas.view.centerOn(0, 0)
    source = canvas.scene._items_by_id[apparatus.representation_id].port_item(apparatus.port_ids[0])
    bus_item = canvas.scene._items_by_id[bus.representation_id]
    point = QPointF(source.scenePos())
    _mouse(canvas, "press", point)
    _mouse(canvas, "move", bus_item.scenePos())
    target = canvas.scene._connection_target
    local_source = bus_item.mapFromScene(point)
    expected_fraction = 1.0 if local_source.x() > 0 else 0.0
    expected = bus_item.mapToScene(QPointF((expected_fraction - .5) * 180, 0))
    assert float(target.anchor_key) == expected_fraction
    assert QPointF(target.x, target.y) == expected
    assert source.scenePos() == point
    _mouse(canvas, "release", bus_item.scenePos())
    route = next(iter(controller.diagram.routes.values()))
    assert (route.waypoints[-1].x, route.waypoints[-1].y) == (expected.x(), expected.y())
    assert float(route.end_anchor.anchor_key) == expected_fraction
    assert source.scenePos() == point


def test_line_shaped_non_line_equipment_does_not_acquire_a_physical_arrow(canvas_factory):
    controller = _controller()
    equipment = controller.add_equipment("builtin.load", "Не линия", x=0, y=0,
        symbol_key="line_section", voltage_class_by_group={"main": U10})
    canvas = canvas_factory(controller)
    item = canvas.scene._items_by_id[equipment.representation_id]
    assert any(row.direction_marker for row in item.symbol_geometry().primitives)
    assert item.direction_marker() is None


def test_native_busduct_is_not_given_a_vl_kl_direction_arrow(canvas_factory):
    canvas, ports = _pair(canvas_factory)
    line = canvas.controller.create_physical_line("Токопровод", LineKind.BUSDUCT,
        PortTarget(ports[0].port_id, ports[0].representation_id),
        PortTarget(ports[1].port_id, ports[1].representation_id), physical=PhysicalLineInput(1_000))
    canvas.refresh()
    assert canvas.scene._route_items_by_id[line.route_id].direction_marker() is None


def test_arrow_hit_selects_and_moves_the_same_line_owner_with_incident_wire(canvas_factory):
    controller = _controller()
    line = controller.add_equipment("builtin.line", "ВЛ", x=0, y=0, width=128, height=24,
                                    voltage_class_by_group={"main": U10})
    load = controller.add_equipment("builtin.load", "Нагрузка", x=240, y=0,
                                    voltage_class_by_group={"main": U10})
    controller.connect_ports(line.port_ids[-1], load.port_ids[0])
    canvas = canvas_factory(controller)
    item = canvas.scene._items_by_id[line.representation_id]
    marker = item.direction_marker()
    assert marker is not None
    tip = item.mapToScene(QPointF(*marker.points[1]))
    hit = canvas.scene.resolve_hit_target(tip, canvas.view.transform())
    assert hit.kind is HitTestKind.BODY and hit.item is item
    before = _state(canvas)
    _mouse(canvas, "press", tip)
    _mouse(canvas, "move", tip + QPointF(0, 60))
    _mouse(canvas, "release", tip + QPointF(0, 60))
    assert canvas.scene.selected_representation_ids() == (line.representation_id,)
    assert controller.diagram.representations[line.representation_id].y == 60
    assert electrical_model_fingerprint(controller.model) == before[0]
    assert controller.model.connectivity_signature() == before[1]
    assert set(controller.diagram.representations) == set(before[2])
    assert set(controller.diagram.routes) == set(before[3])
    canvas.undo()
    assert _state(canvas) == before


def _actual_series_pairs(project):
    by_node = defaultdict(list)
    for route in project.diagram.routes.values():
        for anchor in (route.start_anchor, route.end_anchor):
            if anchor.target_port_id is None:
                by_node[anchor.representation_id].append(route)
    pairs = []
    model = project.electrical_model
    for node in project.diagram.representations.values():
        routes = by_node[node.id]
        if node.symbol_key != "connection_point" or len(routes) != 2:
            continue
        ends = [route.end_anchor if route.start_anchor.representation_id == node.id
                else route.start_anchor for route in routes]
        if not all(end.target_port_id is not None for end in ends):
            continue
        equipment = [model.equipment[project.diagram.representations[end.representation_id].equipment_id]
                     for end in ends]
        behaviors = [model.equipment_type(item.type_id, item.type_version).behavior_key for item in equipment]
        if not any("transformer" in value for value in behaviors) or "legacy.tie" not in behaviors:
            continue
        breaker = project.diagram.representations[ends[behaviors.index("legacy.tie")].representation_id]
        pairs.append((node, breaker))
    return sorted(pairs, key=lambda pair: (pair[1].x, pair[1].y))


def _geometry(document):
    return ({key: (row.x, row.y) for key, row in document.representations.items()},
            {key: tuple((point.x, point.y) for point in row.waypoints) for key, row in document.routes.items()})


def _scene_geometry(canvas):
    return ({key: (item.x(), item.y()) for key, item in canvas.scene._items_by_id.items()},
            {key: tuple((point.x, point.y) for point in item._display_vertices)
             for key, item in canvas.scene._route_items_by_id.items()})


@pytest.mark.parametrize("index", range(7))
def test_each_actual_transformer_breaker_pair_preview_commit_and_cancel_share_geometry(canvas_factory, index):
    from rza_calc.editor.controller import ProjectEditorController
    path = Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json"
    raw = path.read_bytes()
    project = load_project(path)
    pairs = _actual_series_pairs(project)
    assert len(pairs) == 7
    node, breaker = pairs[index]
    canvas = canvas_factory(ProjectEditorController(project))
    item = canvas.scene._items_by_id[breaker.id]
    before = _state(canvas)
    original = canvas.controller.diagram
    journal = len(canvas.controller.journal)
    canvas.view.centerOn(item.pos() + QPointF(0, -90))
    point = item.body_scene_rect().center()
    _mouse(canvas, "press", point)
    _mouse(canvas, "move", point + QPointF(0, -90))
    _mouse(canvas, "move", point + QPointF(0, -180))
    assert canvas.scene._start_positions, "An allowed move must not be silently cancelled"
    planned = canvas.controller.preview_move_representations((breaker.id,), 0, -180, bypass_snap=True)
    assert _scene_geometry(canvas) == _geometry(planned)
    assert canvas.controller.diagram is original
    assert (canvas.scene._items_by_id[node.id].x(), canvas.scene._items_by_id[node.id].y()) != (node.x, node.y)
    assert node.id in canvas.scene._body_drag_auxiliary_positions
    _mouse(canvas, "release", point + QPointF(0, -180))
    assert _geometry(canvas.controller.diagram) == _geometry(planned)
    assert _scene_geometry(canvas) == _geometry(canvas.controller.diagram)
    assert _state(canvas)[:2] == before[:2]
    assert len(canvas.controller.journal) == journal + 1
    assert set(canvas.controller.diagram.representations) == set(original.representations)
    assert set(canvas.controller.diagram.routes) == set(original.routes)
    canvas.undo()
    assert _state(canvas) == before

    journal = len(canvas.controller.journal)
    canvas.view.centerOn(item.pos() + QPointF(0, -90))
    point = item.body_scene_rect().center()
    _mouse(canvas, "press", point)
    for delta in (-60, -120, -40, -180, -80):
        _mouse(canvas, "move", point + QPointF(0, delta))
        assert canvas.scene._start_positions
    assert node.id in canvas.scene._body_drag_auxiliary_positions
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    _mouse(canvas, "release", point + QPointF(0, -80))
    assert _scene_geometry(canvas) == _geometry(original)
    assert not canvas.scene._body_drag_auxiliary_positions
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
    assert path.read_bytes() == raw


def test_cancel_then_drag_another_apparatus_in_same_scene_uses_its_own_original_position(canvas_factory):
    from rza_calc.editor.controller import ProjectEditorController
    project = load_project(Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json")
    canvas = canvas_factory(ProjectEditorController(project))
    before = _state(canvas)
    for node, breaker in _actual_series_pairs(project)[:2]:
        item = canvas.scene._items_by_id[breaker.id]
        canvas.view.centerOn(item.pos() + QPointF(0, -90))
        canvas.view.setFocus()
        QApplication.processEvents()
        point = item.body_scene_rect().center()
        _mouse(canvas, "move", point)
        _mouse(canvas, "press", point)
        _mouse(canvas, "move", point + QPointF(0, -90))
        _mouse(canvas, "move", point + QPointF(0, -180))
        assert item.pos() == QPointF(breaker.x, breaker.y - 180)
        _mouse(canvas, "release", point + QPointF(0, -180))
        canvas.undo()
        assert _state(canvas) == before
        _mouse(canvas, "move", point)
        _mouse(canvas, "press", point)
        for delta in (-60, -120, -40, -180, -80):
            _mouse(canvas, "move", point + QPointF(0, delta))
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        QApplication.processEvents()
        _mouse(canvas, "release", point + QPointF(0, -80))
        assert _state(canvas) == before
        assert _scene_geometry(canvas) == _geometry(canvas.controller.diagram)


@pytest.mark.parametrize("finish", ("return_valid", "release_invalid"))
def test_routing_failure_keeps_gesture_recoverable_and_restores_auxiliary_nodes(canvas_factory, monkeypatch, finish):
    from rza_calc.editor.controller import EditorCommandError, ProjectEditorController
    project = load_project(Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json")
    node, breaker = _actual_series_pairs(project)[0]
    canvas = canvas_factory(ProjectEditorController(project))
    original_planner = canvas.scene._move_preview

    def constrained(ids, dx, dy, **kwargs):
        if dy == -120:
            raise EditorCommandError("Нет безопасного пути в промежуточном положении")
        return original_planner(ids, dx, dy, **kwargs)

    monkeypatch.setattr(canvas.scene, "_move_preview", constrained)
    item = canvas.scene._items_by_id[breaker.id]
    canvas.view.centerOn(item.pos() + QPointF(0, -80))
    point = item.body_scene_rect().center()
    before, journal = _state(canvas), len(canvas.controller.journal)
    _mouse(canvas, "move", point)
    _mouse(canvas, "press", point)
    _mouse(canvas, "move", point + QPointF(0, -90))
    safe = _scene_geometry(canvas)
    assert node.id in canvas.scene._body_drag_auxiliary_positions
    _mouse(canvas, "move", point + QPointF(0, -120))
    assert canvas.scene._start_positions and canvas.scene._body_routing_error
    assert item._target_feedback is ConnectionTargetFeedback.INCOMPATIBLE
    assert _scene_geometry(canvas)[1] == safe[1]
    assert _scene_geometry(canvas)[0][node.id] == safe[0][node.id]
    assert _state(canvas) == before
    if finish == "return_valid":
        _mouse(canvas, "move", point + QPointF(0, -80))
        assert not canvas.scene._body_routing_error
        assert item._target_feedback is ConnectionTargetFeedback.NEUTRAL
        expected = _scene_geometry(canvas)
        _mouse(canvas, "release", point + QPointF(0, -80))
        assert _geometry(canvas.controller.diagram) == expected
        assert len(canvas.controller.journal) == journal + 1
        canvas.undo()
    else:
        _mouse(canvas, "release", point + QPointF(0, -120))
        _mouse(canvas, "release", point + QPointF(0, -120))
        assert len(canvas.controller.journal) == journal
    assert _state(canvas) == before
    assert _scene_geometry(canvas) == _geometry(canvas.controller.diagram)
    assert not canvas.scene._start_positions
    assert not canvas.scene._body_drag_auxiliary_positions
    assert not canvas.scene._body_routing_error


def _begin_actual_group(canvas, ids):
    canvas.scene.select_representations(ids)
    item = canvas.scene._items_by_id[ids[0]]
    canvas.view.centerOn(item.pos() + QPointF(0, -80))
    canvas.view.setFocus()
    QApplication.processEvents()
    point = item.body_scene_rect().center()
    _mouse(canvas, "move", point)
    _mouse(canvas, "press", point)
    assert set(canvas.scene._start_positions) == set(ids)
    return point


@pytest.mark.parametrize("invalid_frame", (False, True))
def test_group_drag_back_to_origin_keeps_selection_without_a_command(canvas_factory, monkeypatch, invalid_frame):
    from rza_calc.editor.controller import EditorCommandError, ProjectEditorController
    project = load_project(Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json")
    canvas = canvas_factory(ProjectEditorController(project))
    pairs = _actual_series_pairs(project)[:2]
    ids = tuple(breaker.id for node, breaker in pairs)
    planner = canvas.scene._move_preview

    def constrained(identifiers, dx, dy, **kwargs):
        if dy == -120:
            raise EditorCommandError("Промежуточное положение без безопасного пути")
        return planner(identifiers, dx, dy, **kwargs)

    monkeypatch.setattr(canvas.scene, "_move_preview", constrained)
    before, journal = _state(canvas), len(canvas.controller.journal)
    geometry = _geometry(canvas.controller.diagram)
    point = _begin_actual_group(canvas, ids)
    _mouse(canvas, "move", point + QPointF(0, -80))
    safe = _scene_geometry(canvas)
    assert {node.id for node, breaker in pairs} <= set(canvas.scene._body_drag_auxiliary_positions)
    if invalid_frame:
        _mouse(canvas, "move", point + QPointF(0, -120))
        assert canvas.scene._body_routing_error
        assert _scene_geometry(canvas)[1] == safe[1]
        for node, breaker in pairs:
            assert _scene_geometry(canvas)[0][node.id] == safe[0][node.id]
    _mouse(canvas, "move", point)
    assert not canvas.scene._body_routing_error
    assert _scene_geometry(canvas) == geometry
    _mouse(canvas, "release", point)
    assert set(canvas.scene.selected_representation_ids()) == set(ids)
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
    assert not canvas.scene._start_positions and not canvas.scene._body_drag_auxiliary_positions


def test_group_repeated_frames_commit_once_then_escape_preserves_group(canvas_factory):
    from rza_calc.editor.controller import ProjectEditorController
    project = load_project(Path(__file__).resolve().parent.parent / "rza_calc/examples/energoraion.json")
    canvas = canvas_factory(ProjectEditorController(project))
    pairs = _actual_series_pairs(project)[:2]
    ids = tuple(breaker.id for node, breaker in pairs)
    before, journal = _state(canvas), len(canvas.controller.journal)
    point = _begin_actual_group(canvas, ids)
    for dy in (-60, -120, -40, -180):
        _mouse(canvas, "move", point + QPointF(0, dy))
        planned = canvas.controller.preview_move_representations(ids, 0, dy, bypass_snap=True)
        assert _scene_geometry(canvas) == _geometry(planned)
    _mouse(canvas, "release", point + QPointF(0, -180))
    assert _geometry(canvas.controller.diagram) == _geometry(planned)
    assert len(canvas.controller.journal) == journal + 1
    assert set(canvas.scene.selected_representation_ids()) == set(ids)
    assert _state(canvas)[:2] == before[:2]
    canvas.undo()
    assert _state(canvas) == before
    journal = len(canvas.controller.journal)
    point = _begin_actual_group(canvas, ids)
    _mouse(canvas, "move", point + QPointF(0, -80))
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    _mouse(canvas, "release", point + QPointF(0, -80))
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
    assert _scene_geometry(canvas) == _geometry(canvas.controller.diagram)
    assert set(canvas.scene.selected_representation_ids()) == set(ids)


@pytest.mark.parametrize("modifier", (Qt.KeyboardModifier.NoModifier,
    Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier))
def test_body_click_selection_is_not_overridden_by_drag_group_preservation(canvas_factory, modifier):
    canvas, ports = _pair(canvas_factory)
    ids = tuple(port.representation_id for port in ports)
    canvas.scene.select_representations(ids if modifier != Qt.KeyboardModifier.ShiftModifier else ids[1:])
    before, journal = _state(canvas), len(canvas.controller.journal)
    item = canvas.scene._items_by_id[ids[0]]
    point = item.body_scene_rect().center()
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, modifier,
                     canvas.view.mapFromScene(point))
    QApplication.processEvents()
    expected = ({ids[0]} if modifier == Qt.KeyboardModifier.NoModifier else
                {ids[1]} if modifier == Qt.KeyboardModifier.ControlModifier else set(ids))
    assert set(canvas.scene.selected_representation_ids()) == expected
    assert _state(canvas) == before and len(canvas.controller.journal) == journal
