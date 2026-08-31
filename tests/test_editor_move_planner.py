"""The preview and commit share one topology-preserving wire geometry plan."""
from dataclasses import replace
import hashlib

import pytest

from rza_calc.domain.diagram import RouteWaypoint, RouteWaypointId
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor.controller import EditorCommandError, ProjectEditorController
from rza_calc.io.project import load_project, save_project
from test_connection_reflow import EXAMPLE, pair, _one_wire
from test_ui_connected_commands import _controller
from test_editor_voltage_guard import U10


def _geometry(diagram):
    return (dict(diagram.representations), {
        key: (route.start_anchor, route.end_anchor,
              [(point.x, point.y, point.source, point.pinned) for point in route.waypoints])
        for key, route in diagram.routes.items()})


@pytest.mark.parametrize("dx,dy", ((0, -180), (160, -240), (-200, -240), (0, 180)))
def test_actual_pair_preview_commit_and_undo_share_geometry_and_preserve_electrical_data(pair, dx, dy):
    project, node, pair_routes, apparatus = pair
    controller = ProjectEditorController(project)
    before_model = ElectricalModelMemento.capture(controller.model)
    before_diagram = controller.diagram
    before_journal = controller.journal
    digest = hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
    preview = controller.preview_move_representations((apparatus.id,), dx, dy, bypass_snap=True)
    again = controller.preview_move_representations((apparatus.id,), dx, dy, bypass_snap=True)
    assert _geometry(preview) == _geometry(again)
    assert controller.diagram is before_diagram and controller.journal == before_journal
    assert ElectricalModelMemento.capture(controller.model) == before_model
    assert preview.representations[node.id] != node
    combined = _one_wire(preview.representations[node.id], [preview.routes[route.id] for route in pair_routes])
    assert all(a != b and (a[0] == b[0] or a[1] == b[1]) for a, b in zip(combined, combined[1:]))
    controller.move_representations((apparatus.id,), dx, dy, bypass_snap=True)
    assert _geometry(controller.diagram) == _geometry(preview)
    assert len(controller.journal) == len(before_journal) + 1
    assert ElectricalModelMemento.capture(controller.model) == before_model
    assert set(controller.diagram.representations) == set(before_diagram.representations)
    assert set(controller.diagram.routes) == set(before_diagram.routes)
    for key, route in controller.diagram.routes.items():
        old = before_diagram.routes[key]
        assert (route.start_anchor, route.end_anchor) == (old.start_anchor, old.end_anchor)
        assert (route.waypoints[0].id, route.waypoints[-1].id) == (old.waypoints[0].id, old.waypoints[-1].id)
    committed = controller.diagram
    controller.undo()
    assert _geometry(controller.diagram) == _geometry(before_diagram)
    controller.redo()
    assert _geometry(controller.diagram) == _geometry(committed)
    assert hashlib.sha256(EXAMPLE.read_bytes()).hexdigest() == digest


def test_zero_move_never_reflows_existing_geometry_or_creates_history(pair):
    project, node, routes, apparatus = pair
    controller = ProjectEditorController(project)
    original, journal = controller.diagram, controller.journal
    assert controller.preview_move_representations((apparatus.id,), 0, 0, bypass_snap=True) is original
    controller.move_representations((apparatus.id,), 0, 0, bypass_snap=True)
    assert controller.diagram is original and controller.journal == journal


def test_locked_hidden_node_stays_fixed_in_the_shared_planner(pair):
    project, node, routes, apparatus = pair
    reps = dict(project.diagram.representations)
    reps[node.id] = replace(node, extensions={**node.extensions, "position_pinned": True})
    project.diagram = replace(project.diagram, representations=reps)
    controller = ProjectEditorController(project)
    preview = controller.preview_move_representations((apparatus.id,), 40, 0, bypass_snap=True)
    assert preview.representations[node.id] == reps[node.id]
    for route in routes:
        updated = preview.routes[route.id]
        point = updated.waypoints[0 if route.start_anchor.representation_id == node.id else -1]
        assert (point.x, point.y) == (node.x, node.y)


def test_committed_pair_reopens_with_same_hidden_node_and_route_identities(pair, tmp_path):
    project, node, routes, apparatus = pair
    controller = ProjectEditorController(project)
    before = ElectricalModelMemento.capture(controller.model)
    controller.move_representations((apparatus.id,), 160, -240, bypass_snap=True)
    output = tmp_path / "connected-move.json"
    save_project(output, project)
    reopened = load_project(output)
    assert reopened.diagram.representations == controller.diagram.representations
    assert reopened.diagram.routes == controller.diagram.routes
    assert ElectricalModelMemento.capture(reopened.electrical_model).equipment == before.equipment
    assert dict(reopened.electrical_model.connections) == dict(before.connections)


def _simple_connection():
    controller = _controller()
    first = controller.add_equipment("builtin.external_grid", "S", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "L", x=400, y=0, voltage_class_by_group={"main": U10})
    terminals = [controller._port_anchor_geometry(controller.model,
                 controller.diagram.representations[item.representation_id], item.port_ids[0])
                 for item in (first, second)]
    points = (terminals[0].x, terminals[0].y), (0, 100), (400, 100), (terminals[1].x, terminals[1].y)
    result = controller.connect_ports(first.port_ids[0], second.port_ids[0], route_waypoints=tuple(
        RouteWaypoint(RouteWaypointId.new(), x, y) for x, y in points))
    return controller, first, second, result.route_id


def test_move_avoids_nonincident_apparatus_body_in_both_preview_and_commit():
    controller, first, second, route_id = _simple_connection()
    blocker = controller.add_equipment("builtin.load", "Obstacle", x=200, y=-100, width=80, height=100)
    preview = controller.preview_move_representations((first.representation_id,), 40, -150, bypass_snap=True)
    route = preview.routes[route_id]
    # Independent axis-aligned segment/body test; no shared router predicate.
    for a, b in zip(route.waypoints, route.waypoints[1:]):
        if a.x == b.x and 160 < a.x < 240:
            assert max(a.y, b.y) <= -150 or min(a.y, b.y) >= -50
        if a.y == b.y and -150 < a.y < -50:
            assert max(a.x, b.x) <= 160 or min(a.x, b.x) >= 240
    controller.move_representations((first.representation_id,), 40, -150, bypass_snap=True)
    assert _geometry(controller.diagram) == _geometry(preview)
    assert controller.diagram.representations[blocker.representation_id] == preview.representations[blocker.representation_id]


def test_impossible_route_rejects_preview_and_commit_atomically():
    controller, first, second, route_id = _simple_connection()
    controller.add_equipment("builtin.load", "Foreign body", x=200, y=-100, width=100, height=100)
    before = (ElectricalModelMemento.capture(controller.model), controller.diagram, controller.journal)
    for command in (controller.preview_move_representations, controller.move_representations):
        with pytest.raises(EditorCommandError):
            command((first.representation_id,), 200, -100, bypass_snap=True)
        assert (ElectricalModelMemento.capture(controller.model), controller.diagram, controller.journal) == before
