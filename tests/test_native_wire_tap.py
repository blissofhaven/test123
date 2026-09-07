"""A wire to a physical line adds a real tap, never another physical branch."""
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from rza_calc.domain.diagram import (DiagramPage, PageId, DiagramRouteId,
    GraphicalRepresentationId, RouteWaypointId, RouteWaypoint)
from rza_calc.domain.electrical import DataConfirmation, LineKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import NodeTarget, PortTarget, PhysicalLineInput, ProjectEditorController, EditorCommandError
from rza_calc.io.project import load_project, save_project
from test_ui_direct_connections import _controller, _mouse, _state, U10, canvas_factory
from test_stage02_connection_gestures import answer_real_menu, assert_one_menu


def fixture(known=True, real=False):
    controller = (ProjectEditorController(load_project(Path(__file__).parent / 'fixtures/legacy_projects/four_fault_types.json'))
                  if real else _controller())
    start = controller.add_electrical_node('Начало', x=0, y=0, voltage_class_id=U10)
    end = controller.add_electrical_node('Конец', x=400, y=0, voltage_class_id=U10)
    bus = controller.add_electrical_node('Присоединяемая шина', x=200, y=200,
        symbol_key='busbar', width=120, height=12, voltage_class_id=U10)
    line = controller.create_physical_line('Магистраль', LineKind.CABLE,
        NodeTarget(start.node_id, start.representation_id), NodeTarget(end.node_id, end.representation_id),
        physical=PhysicalLineInput(1_000_000 if known else None,
            DataConfirmation.CONFIRMED if known else DataConfirmation.UNCONFIRMED,
            {'r1_ohm_per_km': .4, 'x1_ohm_per_km': .2, 'parallel_count': 2}, DataConfirmation.CONFIRMED))
    return controller, line, bus


def assert_split(controller, line, known):
    sections = [controller.model.line_sections[key] for key in controller.model.logical_lines[line.logical_line_id].section_equipment_ids]
    assert len(sections) == 2
    if known:
        assert [section.length_mm for section in sections] == [250_000, 750_000]
    else:
        assert [section.length_mm for section in sections] == [None, None]
        assert all(segment.length_confirmation is DataConfirmation.UNCONFIRMED
                   for section in sections for segment in section.construction_segments)
    for section in sections:
        props = controller.model.effective_equipment_properties(section.equipment_id)
        assert props['r1_ohm_per_km'] == .4
        assert props['x1_ohm_per_km'] == .2
        assert props['parallel_count'] == 2
    if known:
        assert sum(section.length_mm / 1_000_000 * .4 / 2 for section in sections) == pytest.approx(.2)
    assert not controller.diagram.validate_targets(controller.model)


@pytest.mark.parametrize('known', [True, False])
def test_node_wire_tap_preserves_lengths_parameters_undo_and_full_reload(known, tmp_path):
    controller, line, bus = fixture(known, real=True)
    before = electrical_model_fingerprint(controller.model), controller.diagram
    old_connection_ids = set(controller.model.connections)
    journal = len(controller.journal)
    result = controller.connect_node_to_tap(NodeTarget(bus.node_id, bus.representation_id),
        line.section_id, 250_000 if known else None, tap_x=200, tap_y=0)
    assert result.tap_node_id == bus.node_id
    assert len(controller.journal) == journal + 1
    assert_split(controller, line, known)

    assert any(row.electrical_node_id == bus.node_id and (row.x, row.y) == (200, 0)
               for row in controller.diagram.representations.values())
    path = tmp_path / 'wire-tap.json'
    save_project(path, controller._project)
    restored = load_project(path)
    assert electrical_model_fingerprint(restored.electrical_model) == electrical_model_fingerprint(controller.model)
    assert restored.diagram == controller.diagram
    controller.undo()
    assert (electrical_model_fingerprint(controller.model), controller.diagram) == before
    assert set(controller.model.connections) == old_connection_ids
    controller.redo()
    assert_split(controller, line, known)


def second_view(controller, route_id, source_representation_id, same_page):
    original = controller.diagram.routes[route_id]
    result = {}
    def command(draft):
        page = draft.diagram.pages[original.page_id] if same_page else DiagramPage(PageId.new(), 'Второй лист')
        pages = {**draft.diagram.pages, page.id: page}
        representations = dict(draft.diagram.representations)
        anchors = []
        for anchor in (original.start_anchor, original.end_anchor):
            rep = replace(representations[anchor.representation_id], id=GraphicalRepresentationId.new(), page_id=page.id,
                y=representations[anchor.representation_id].y + 400)
            representations[rep.id] = rep
            anchors.append(replace(anchor, representation_id=rep.id))
        bus = replace(representations[source_representation_id], id=GraphicalRepresentationId.new(), page_id=page.id, y=600)
        representations[bus.id] = bus
        route = replace(original, id=DiagramRouteId.new(), page_id=page.id,
            start_anchor=anchors[1], end_anchor=anchors[0],
            waypoints=tuple(replace(point, id=RouteWaypointId.new(), y=point.y + 400)
                            for point in reversed(original.waypoints)))
        draft.diagram = replace(draft.diagram, pages=pages, representations=representations,
            routes={**draft.diagram.routes, route.id: route})
        result.update(route=route, source=bus, page=page.id)
    controller._execute('Подготовить второе изображение для проверки', command)
    return result


@pytest.mark.parametrize('same_page', [True, False])
@pytest.mark.parametrize('operation', ['node', 'port', 'physical'])
def test_tap_preserves_every_view_and_uses_exact_reversed_view_hit(operation, same_page, tmp_path):
    controller, line, bus = fixture(real=True)
    view = second_view(controller, line.route_id, bus.representation_id, same_page)
    load = controller.add_equipment('builtin.load', 'Нагрузка', x=500, y=600, page_id=view['page'],
                                    voltage_class_by_group={'main': U10})
    before = electrical_model_fingerprint(controller.model), controller.diagram
    journal = len(controller.journal)
    args = dict(page_id=view['page'], tap_route_id=view['route'].id, tap_x=123, tap_y=400)
    if operation == 'node':
        result = controller.connect_node_to_tap(NodeTarget(bus.node_id, view['source'].id), line.section_id, 250_000, **args)
    elif operation == 'port':
        result = controller.reconnect_port_to_tap(load.port_ids[0], line.section_id, 250_000,
            source_representation_id=load.representation_id, **args)
    else:
        result = controller.create_tap(line.section_id, 250_000, 'Отходящая', LineKind.CABLE,
            NodeTarget(bus.node_id, view['source'].id), physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED), **args)
    assert len(controller.journal) == journal + 1
    assert_split(controller, line, True)
    main_routes = [route for route in controller.diagram.routes.values()
        if route.equipment_id in {result.first_section_id, result.second_section_id}]
    assert len(main_routes) == 4
    for old_id in (line.route_id, view['route'].id):
        first = controller.diagram.routes[old_id]
        old = before[1].routes[old_id]
        expected = (123, 400) if old_id == view['route'].id else (200, 0)
        assert (first.waypoints[-1].x, first.waypoints[-1].y) == expected
        assert first.equipment_id == result.first_section_id
        assert first.page_id == old.page_id
        assert first.extensions == old.extensions
    # Existing outer endpoints and unrelated representations survive exactly.
    for key, rep in before[1].representations.items():
        assert controller.diagram.representations[key] == rep
    path = tmp_path / 'multiview.json'
    save_project(path, controller._project)
    loaded = load_project(path)
    assert loaded.diagram == controller.diagram
    assert electrical_model_fingerprint(loaded.electrical_model) == electrical_model_fingerprint(controller.model)
    controller.undo()
    assert (electrical_model_fingerprint(controller.model), controller.diagram) == before
    controller.redo()
    assert len([route for route in controller.diagram.routes.values()
        if route.equipment_id in {result.first_section_id, result.second_section_id}]) == 4
    topology = electrical_model_fingerprint(controller.model)
    split_diagram = controller.diagram
    tap_id = controller.diagram.routes[view['route'].id].end_anchor.representation_id
    controller.move_representations((tap_id,), 20, 20, bypass_snap=True)
    assert electrical_model_fingerprint(controller.model) == topology
    assert controller.diagram.routes[line.route_id] == split_diagram.routes[line.route_id]
    moved = controller.diagram.routes[view['route'].id].waypoints[-1]
    assert (moved.x, moved.y) == (143, 420)
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert controller.diagram == split_diagram


@pytest.mark.parametrize('bad', ['off-route', 'offset', 'same-end-node', 'wrong-route'])
def test_rejected_wire_tap_rolls_back_model_graphics_and_history(bad):
    controller, line, bus = fixture()
    node = NodeTarget(bus.node_id, bus.representation_id)
    args = dict(tap_x=200, tap_y=0)
    offset = 250_000
    if bad == 'off-route':
        args['tap_y'] = 3
    elif bad == 'offset':
        offset = 0
    elif bad == 'same-end-node':
        route = controller.diagram.routes[line.route_id]
        node = NodeTarget(route.start_anchor.electrical_node_id, route.start_anchor.representation_id)
    else:
        args['tap_route_id'] = DiagramRouteId.new()
    before = electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)
    with pytest.raises(EditorCommandError):
        controller.connect_node_to_tap(node, line.section_id, offset, **args)
    assert (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)) == before


@pytest.mark.parametrize('blocked', [False, True])
def test_detached_terminal_keeps_all_old_branch_ends_together_or_rolls_back(monkeypatch, blocked):
    from rza_calc.editor import tap_routes
    from rza_calc.editor.orthogonal_routing import RoutingObstacle
    controller = _controller()
    qf = controller.add_equipment('builtin.circuit_breaker', 'Общий QF', x=400, y=120,
        voltage_class_by_group={'main': U10})
    roots = [controller.add_electrical_node(str(y), x=0, y=y, voltage_class_id=U10) for y in (0, 240)]
    port = controller._port_anchor_geometry(controller.model,
        controller.diagram.representations[qf.representation_id], qf.port_ids[0])
    lines = [controller.create_physical_line('КЛ', LineKind.CABLE,
        NodeTarget(node.node_id, node.representation_id), PortTarget(qf.port_ids[0], qf.representation_id),
        physical=PhysicalLineInput(100_000), route_waypoints=tuple(
            RouteWaypoint(RouteWaypointId.new(), x, y) for x, y in (
                (0, controller.diagram.representations[node.representation_id].y),
                (port.x, controller.diagram.representations[node.representation_id].y), (port.x, port.y)))) for node in roots]
    old = controller.diagram.routes[lines[0].route_id]
    point = controller._route_midpoint(old)
    before = electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)
    if blocked:
        original = tap_routes.separate_detached_terminals
        def no_space(diagram, detached, **kwargs):
            return original(diagram, detached, **{**kwargs,
                'obstacles_for_page': lambda page: (RoutingObstacle(-10000, -10000, 10000, 10000),)})
        monkeypatch.setattr(tap_routes, 'separate_detached_terminals', no_space)
        with pytest.raises(EditorCommandError, match='Освободите место'):
            controller.reconnect_port_to_tap(qf.port_ids[0], lines[0].section_id, 50_000,
                tap_x=point[0], tap_y=point[1])
        assert (electrical_model_fingerprint(controller.model), controller.diagram, len(controller.journal)) == before
        return
    result = controller.reconnect_port_to_tap(qf.port_ids[0], lines[0].section_id, 50_000,
        tap_x=point[0], tap_y=point[1])
    tail = next(row for row in controller.diagram.routes.values() if row.equipment_id == result.second_section_id)
    peer = controller.diagram.routes[lines[1].route_id]
    assert tail.end_anchor.representation_id == peer.end_anchor.representation_id
    assert tail.end_anchor.electrical_node_id == peer.end_anchor.electrical_node_id
    assert tail.end_anchor.target_port_id is None and peer.end_anchor.target_port_id is None
    assert (tail.waypoints[-1].x, tail.waypoints[-1].y) == (peer.waypoints[-1].x, peer.waypoints[-1].y)
    assert (tail.waypoints[-1].x, tail.waypoints[-1].y) != (old.waypoints[-1].x, old.waypoints[-1].y)
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert (electrical_model_fingerprint(controller.model), controller.diagram) == before[:2]


@pytest.mark.parametrize('source_kind', ['node', 'port'])
@pytest.mark.parametrize('known', [True, False])
@pytest.mark.parametrize('cancel', [False, True])
def test_real_wire_menu_and_tap_position_dialog(canvas_factory, source_kind, known, cancel):
    controller, line, bus = fixture(known)
    if source_kind == 'port':
        load = controller.add_equipment('builtin.load', 'Нагрузка', x=400, y=240, voltage_class_by_group={'main': U10})
    canvas = canvas_factory(controller)
    before = _state(canvas)
    journal = len(controller.journal)
    if source_kind == 'node':
        canvas.activate_connection_tool()
        start = QPointF(200, 200)
    else:
        start = canvas.scene._items_by_id[load.representation_id].port_item(load.port_ids[0]).scenePos()
    timers, answered = [], []
    calls = answer_real_menu(canvas, 'wire', timers)
    poll, watchdog = QTimer(canvas), QTimer(canvas)
    watchdog.setSingleShot(True)
    timers += [poll, watchdog]
    def answer_position():
        dialog = next((widget for widget in QApplication.topLevelWidgets()
            if isinstance(widget, (QInputDialog, QMessageBox)) and widget.isVisible()
            and widget.parent() is canvas), None)
        if dialog is None:
            return
        if answered:
            answered.append('unexpected extra dialog')
            dialog.reject()
            poll.stop()
            watchdog.stop()
            return
        answered.append('position')
        if cancel:
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        elif isinstance(dialog, QInputDialog):
            assert dialog.inputMode() == QInputDialog.InputMode.DoubleInput
            dialog.setDoubleValue(250)
            QTest.keyClick(dialog, Qt.Key.Key_Return)
        else:
            QTest.mouseClick(dialog.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
    def timeout():
        poll.stop()
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, (QInputDialog, QMessageBox)) and widget.isVisible():
                widget.reject()
    poll.timeout.connect(answer_position)
    watchdog.timeout.connect(timeout)
    poll.start(1)
    watchdog.start(2000)
    try:
        end = QPointF(200, 0)
        _mouse(canvas, 'move', start)
        _mouse(canvas, 'press', start)
        _mouse(canvas, 'move', (start + end) / 2)
        _mouse(canvas, 'move', end)
        _mouse(canvas, 'release', end)
        assert_one_menu(canvas, calls, before)
        assert answered == ['position']
        if cancel:
            assert _state(canvas) == before
            assert len(controller.journal) == journal
        else:
            assert len(controller.journal) == journal + 1
            assert len(controller.model.logical_lines) == 1
            assert len(controller.model.line_sections) == 2
            assert_split(controller, line, known)
            controller.undo()
            assert _state(canvas) == before
    finally:
        for timer in timers:
            timer.stop()
        canvas.scene.clearSelection()
