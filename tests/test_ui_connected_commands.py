"""Connected wire editing changes drawing geometry, never the circuit."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument, DiagramPage, DiagramRouteKind, PageId, RouteAnchorKind,
    RouteWaypoint, RouteWaypointId, RouteWaypointSource,
)
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, LineKind, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import NodeTarget, PhysicalLineInput, ProjectEditorController
from rza_calc.editor.bus_connections import endpoint_fraction
from rza_calc.editor.connected_geometry import shift_route_segment
from rza_calc.editor.controller import EditorCommandError
from rza_calc.editor.history import ProjectDraft
from rza_calc.editor.orientation import bus_anchor_geometry
from rza_calc.editor.state import EditorMode
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.project import load_project, save_project


DEMO = Path(__file__).resolve().parent.parent / "tests/fixtures/legacy_projects/energoraion.json"


def _controller():
    return ProjectEditorController(SimpleNamespace(
        electrical_model=ElectricalModel.with_builtins("Connected editing"),
        diagram=DiagramDocument.create("Connected", (DiagramPage(PageId("page.connected"), "Connected"),)),
        catalog_snapshots=ProjectCatalogSnapshots(),
    ))


def _native_line(*, bus=False, vertical=False, rotation=0, fraction="0.25"):
    controller = _controller()
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first = controller.add_electrical_node(
        "Шина" if bus else "Начало", x=0, y=0, voltage_class_id=voltage,
        symbol_key=("busbar_vertical" if vertical else "busbar_horizontal") if bus else "electrical_node",
        width=20 if vertical else 240, height=240 if vertical else 20, rotation_deg=rotation,
    )
    last = controller.add_electrical_node("Конец", x=600, y=200, voltage_class_id=voltage)
    line = controller.create_physical_line(
        "КЛ-1", LineKind.CABLE,
        NodeTarget(first.node_id, first.representation_id, fraction if bus else ""),
        NodeTarget(last.node_id, last.representation_id),
        physical=PhysicalLineInput(4_200_000, DataConfirmation.CONFIRMED,
                                   {"r1_ohm_per_km": .4, "x1_ohm_per_km": .3},
                                   DataConfirmation.CONFIRMED),
    )
    return controller, first, last, line


def _signature(controller):
    model = controller.model
    return (electrical_model_fingerprint(model), model.revision, model.connectivity_signature(),
            dict(model.ports), dict(model.equipment), dict(model.connections),
            dict(model.electrical_nodes), dict(model.line_sections), dict(model.operating_states))


def _points(coordinates):
    return tuple(RouteWaypoint(RouteWaypointId.new(), x, y) for x, y in coordinates)


def _anchor_identity(route):
    return tuple(replace(anchor, anchor_key="") for anchor in (route.start_anchor, route.end_anchor))


def _old_coincident_bus_endpoint(controller, route, *, at_start, point):
    """Build an orthogonal saved overlap without calling the new allocator."""
    field = "start_anchor" if at_start else "end_anchor"
    anchor = getattr(route, field)
    row = controller.diagram.representations[anchor.representation_id]
    width, height = controller._equipment_symbol_size(row, None)
    fraction = endpoint_fraction(row, width, height, replace(anchor, anchor_key=""), point)
    # Change the target first, then reroute. Moving just the last waypoint
    # would create a transient diagonal rejected by the domain constructor.
    targeted = replace(route, **{field: replace(anchor, anchor_key=format(fraction, ".17g"))})
    changed = controller._reroute_to_current_port_anchors(
        ProjectDraft(controller.model, controller.diagram, controller._project.catalog_snapshots), targeted)
    return replace(changed, **{field: replace(anchor, anchor_key="")})


@pytest.mark.parametrize("coordinates,index", [
    ([(0, 0), (100, 0)], 0),
    ([(100, 0), (0, 0)], 0),
    ([(0, 0), (0, 100)], 0),
    ([(0, 100), (0, 0)], 0),
    ([(0, 0), (100, 0), (100, 80), (200, 80)], 0),
    ([(0, 0), (100, 0), (100, 80), (200, 80)], 1),
    ([(0, 0), (100, 0), (100, 80), (200, 80)], 2),
])
def test_segment_drag_keeps_terminal_coordinates_ids_direction_and_unselected_geometry(coordinates, index):
    controller, _, _, line = _native_line()
    original = replace(controller.diagram.routes[line.route_id], waypoints=_points(coordinates))
    changed = shift_route_segment(original, index, dx=30, dy=40)
    assert changed != original.waypoints
    assert changed[0] == original.waypoints[0]
    assert changed[-1] == original.waypoints[-1]
    assert all((a.x == b.x) != (a.y == b.y) for a, b in zip(changed, changed[1:]))
    assert len({point.id for point in changed}) == len(changed)
    assert (changed[1].x - changed[0].x) * (coordinates[1][1] - coordinates[0][1]) == pytest.approx(
        (changed[1].y - changed[0].y) * (coordinates[1][0] - coordinates[0][0]))
    assert (changed[-1].x - changed[-2].x) * (coordinates[-1][1] - coordinates[-2][1]) == pytest.approx(
        (changed[-1].y - changed[-2].y) * (coordinates[-1][0] - coordinates[-2][0]))
    before_ids = {point.id for point in original.waypoints}
    assert before_ids.issubset({point.id for point in changed})
    for point in changed:
        if point.id not in before_ids:
            assert point.source is RouteWaypointSource.USER and point.pinned


def test_native_segment_command_is_single_undoable_graphics_edit_with_same_model_and_labels():
    controller, _, _, line = _native_line()
    controller.set_route_label(line.route_id, label_x=71, label_y=-29)
    original = controller.diagram.routes[line.route_id]
    before, count = _signature(controller), len(controller.journal)
    controller.move_route_segment(line.route_id, 0, dx=40, dy=60)
    changed = controller.diagram.routes[line.route_id]
    assert changed != original
    assert changed.start_anchor == original.start_anchor and changed.end_anchor == original.end_anchor
    assert changed.extensions == original.extensions
    assert changed.waypoints[0] == original.waypoints[0] and changed.waypoints[-1] == original.waypoints[-1]
    assert _signature(controller) == before
    assert len(controller.journal) == count + 1
    controller.diagram.require_valid_targets(controller.model)
    controller.undo()
    assert controller.diagram.routes[line.route_id] == original
    controller.redo()
    assert controller.diagram.routes[line.route_id] == changed
    assert _signature(controller) == before


@pytest.mark.parametrize("index,dx,dy", [(-1, 1, 1), (99, 1, 1), (True, 1, 1), (0, float("nan"), 1),
                                         (0, 1, float("inf")), (0, True, 1)])
def test_segment_invalid_input_is_atomic(index, dx, dy):
    controller, _, _, line = _native_line()
    original, count = diagram_to_dict(controller.diagram), len(controller.journal)
    with pytest.raises(EditorCommandError):
        controller.move_route_segment(line.route_id, index, dx=dx, dy=dy)
    assert diagram_to_dict(controller.diagram) == original
    assert len(controller.journal) == count


def test_zero_or_parallel_segment_drag_does_not_create_history():
    controller, _, _, line = _native_line()
    route = controller.diagram.routes[line.route_id]
    first, second = route.waypoints[:2]
    count = len(controller.journal)
    controller.move_route_segment(route.id, 0, dx=0, dy=0)
    controller.move_route_segment(route.id, 0, dx=15 if first.y == second.y else 0,
                                  dy=15 if first.x == second.x else 0)
    assert controller.diagram.routes[route.id] == route and len(controller.journal) == count


@pytest.mark.parametrize("vertical", (False, True))
@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_bus_attachment_moves_in_local_axis_at_all_rotations_with_port_roles_unchanged(vertical, rotation):
    controller, bus, _, line = _native_line(bus=True, vertical=vertical, rotation=rotation)
    original = controller.diagram.routes[line.route_id]
    row = controller.diagram.representations[bus.representation_id]
    before, count = _signature(controller), len(controller.journal)
    preview = controller.preview_bus_attachment_move(line.route_id, at_start=True, fraction=.75)
    assert len(preview) == 1 and controller.diagram.routes[line.route_id] == original
    assert len(controller.journal) == count and _signature(controller) == before
    assert controller.move_bus_attachment(line.route_id, at_start=True, fraction=.75) == (line.route_id,)
    changed = controller.diagram.routes[line.route_id]
    expected = bus_anchor_geometry(width=20 if vertical else 240, height=240 if vertical else 20,
                                   rotation=rotation, center_x=row.x, center_y=row.y, fraction=.75)
    assert (changed.waypoints[0].x, changed.waypoints[0].y) == pytest.approx(expected[:2])
    assert changed.waypoints[0].id == original.waypoints[0].id
    assert changed.waypoints[-1] == original.waypoints[-1]
    assert _anchor_identity(changed) == _anchor_identity(original)
    assert changed.start_anchor.anchor_key == "0.75"
    assert controller.diagram.representations[bus.representation_id] == row
    assert _signature(controller) == before and len(controller.journal) == count + 1
    controller.undo()
    assert controller.diagram.routes[line.route_id] == original
    controller.redo()
    assert controller.diagram.routes[line.route_id] == changed
    assert _signature(controller) == before


@pytest.mark.parametrize("fraction,expected", [(-10, 0), (10, 1)])
def test_bus_slide_clamps_to_actual_bus_extent(fraction, expected):
    controller, _, _, line = _native_line(bus=True)
    controller.move_bus_attachment(line.route_id, at_start=True, fraction=fraction)
    moved = controller.diagram.routes[line.route_id]
    assert float(moved.start_anchor.anchor_key) == expected
    assert (moved.waypoints[0].x, moved.waypoints[0].y) == pytest.approx(((expected - .5) * 240, 0))


@pytest.mark.parametrize("fraction", [float("nan"), float("inf"), True, "0.5"])
def test_bus_invalid_input_is_atomic(fraction):
    controller, _, _, line = _native_line(bus=True)
    original, count = diagram_to_dict(controller.diagram), len(controller.journal)
    with pytest.raises(EditorCommandError):
        controller.move_bus_attachment(line.route_id, at_start=True, fraction=fraction)
    assert diagram_to_dict(controller.diagram) == original and len(controller.journal) == count


def test_noop_bus_slide_and_analysis_mode_do_not_mutate_or_add_history():
    controller, _, _, line = _native_line(bus=True)
    controller.move_bus_attachment(line.route_id, at_start=True, fraction=.75)
    original, count = diagram_to_dict(controller.diagram), len(controller.journal)
    assert controller.move_bus_attachment(line.route_id, at_start=True, fraction=.75) == ()
    assert diagram_to_dict(controller.diagram) == original and len(controller.journal) == count
    controller.set_mode(EditorMode.ANALYSIS)
    original, count = diagram_to_dict(controller.diagram), len(controller.journal)
    with pytest.raises(EditorCommandError):
        controller.move_bus_attachment(line.route_id, at_start=True, fraction=.5)
    with pytest.raises(EditorCommandError):
        controller.move_route_segment(line.route_id, 0, dx=40, dy=40)
    assert diagram_to_dict(controller.diagram) == original and len(controller.journal) == count


def test_native_non_bus_anchor_cannot_be_slid_as_bus():
    controller, _, _, line = _native_line()
    with pytest.raises(EditorCommandError, match="шины"):
        controller.move_bus_attachment(line.route_id, at_start=True, fraction=.5)


@pytest.mark.parametrize("selected_kind", ("native", "legacy"))
def test_mixed_native_and_legacy_coincident_bus_tap_moves_only_selected_endpoint(selected_kind):
    controller, bus, _, line = _native_line(bus=True)
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    extra_routes = []
    for name, x, fraction in (("Совпадает", -300, .25), ("Другая точка", 300, .9)):
        added = controller.add_equipment(
            "builtin.load", name, x=x, y=-200, voltage_class_by_group={"main": voltage},
        )
        row = controller.diagram.representations[added.representation_id]
        port = controller._port_anchor_geometry(controller.model, row, added.port_ids[0])
        tap_x = (fraction - .5) * 240
        linked = controller.connect_port_to_node(
            added.port_ids[0], bus.node_id, source_representation_id=added.representation_id,
            node_representation_id=bus.representation_id, target_anchor_key="",  # Legacy without fraction.
            route_waypoints=_points([(port.x, port.y), (tap_x, port.y), (tap_x, 0)]),
        )
        extra_routes.append(linked.route_id)
    native = controller.diagram.routes[line.route_id]
    coincident = controller.diagram.routes[extra_routes[0]]
    # New connections are deliberately spaced now. Emulate an OLD saved
    # drawing with a genuinely coincident legacy endpoint and no anchor key.
    saved = dict(controller.diagram.routes)
    saved[coincident.id] = _old_coincident_bus_endpoint(
        controller, coincident, at_start=False, point=native.waypoints[0])
    controller._project.diagram = replace(controller.diagram, routes=saved)
    controller = ProjectEditorController(controller._project)
    coincident = controller.diagram.routes[coincident.id]
    assert (native.waypoints[0].x, native.waypoints[0].y) == (coincident.waypoints[-1].x, coincident.waypoints[-1].y)
    untouched = controller.diagram.routes[extra_routes[1]]
    before, count = _signature(controller), len(controller.journal)
    original = dict(controller.diagram.routes)
    selected = native if selected_kind == "native" else coincident
    at_start = selected_kind == "native"
    updates = controller.preview_bus_attachment_move(selected.id, at_start=at_start, fraction=.6)
    assert len(updates) == 1 and updates[0].id == selected.id
    assert updates[0].kind is selected.kind
    assert dict(controller.diagram.routes) == original and len(controller.journal) == count
    changed_ids = controller.move_bus_attachment(selected.id, at_start=at_start, fraction=.6)
    assert changed_ids == (selected.id,)
    assert len(controller.journal) == count + 1
    assert controller.diagram.routes[untouched.id] == untouched
    for identifier, original_route in original.items():
        changed = controller.diagram.routes[identifier]
        if identifier == selected.id:
            anchor = changed.start_anchor if at_start else changed.end_anchor
            waypoint = changed.waypoints[0 if at_start else -1]
            assert float(anchor.anchor_key) == .6
            assert (waypoint.x, waypoint.y) == pytest.approx((24, 0))
            assert tuple((point.x, point.y) for point in changed.waypoints) == tuple(
                (point.x, point.y) for point in updates[0].waypoints)
        else:
            assert changed == original_route
        assert _anchor_identity(changed) == _anchor_identity(original[identifier])
    assert _signature(controller) == before
    changed_routes = dict(controller.diagram.routes)
    controller.undo()
    assert dict(controller.diagram.routes) == original and _signature(controller) == before
    controller.redo()
    assert dict(controller.diagram.routes) == changed_routes and _signature(controller) == before


def test_bus_move_preserves_manual_waypoint_id_position_and_pin():
    controller, _, _, line = _native_line(bus=True)
    route = controller.diagram.routes[line.route_id]
    first, last = route.waypoints[0], route.waypoints[-1]
    manual = RouteWaypoint(RouteWaypointId.new(), 300, -200, RouteWaypointSource.USER, True)
    waypoints = (first, RouteWaypoint(RouteWaypointId.new(), first.x, -200), manual,
                 RouteWaypoint(RouteWaypointId.new(), last.x, -200), last)
    controller.reroute_diagram_route(route.id, waypoints)
    controller.move_bus_attachment(route.id, at_start=True, fraction=.8)
    assert manual in controller.diagram.routes[route.id].waypoints


def test_legacy_connection_segment_command_keeps_same_electrical_targets_and_all_other_routes():
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    original = dict(controller.diagram.routes)
    route = next(item for item in original.values() if item.kind is DiagramRouteKind.NODE_CONNECTION)
    before = _signature(controller)
    controller.move_route_segment(route.id, 0, dx=37, dy=43)
    changed = controller.diagram.routes[route.id]
    assert changed.start_anchor == route.start_anchor and changed.end_anchor == route.end_anchor
    assert changed.waypoints[0] == route.waypoints[0] and changed.waypoints[-1] == route.waypoints[-1]
    assert changed != route
    assert all(value == original[key] for key, value in controller.diagram.routes.items() if key != route.id)
    assert _signature(controller) == before
    controller.undo()
    assert dict(controller.diagram.routes) == original


def test_actual_legacy_bus_moves_selected_coincident_tap_only_and_survives_save_undo(tmp_path):
    source_hash = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    project = load_project(DEMO)
    controller = ProjectEditorController(project)
    # The delivered demo is repaired separately. Build one old-style overlap
    # IN MEMORY so this regression never depends on keeping the live defect.
    bus_endpoints = [(route, at_start, anchor)
                     for route in controller.diagram.routes.values()
                     if route.kind is DiagramRouteKind.NODE_CONNECTION
                     for at_start, anchor in ((True, route.start_anchor), (False, route.end_anchor))
                     if anchor.kind is RouteAnchorKind.BUS]
    route, at_start, selected = next(
        item for item in bus_endpoints if any(
            peer.id != item[0].id and anchor.representation_id == item[2].representation_id
            and anchor.electrical_node_id == item[2].electrical_node_id
            for peer, _, anchor in bus_endpoints))
    peer, peer_start, _ = next(
        item for item in bus_endpoints
        if item[0].id != route.id and item[2].representation_id == selected.representation_id
        and item[2].electrical_node_id == selected.electrical_node_id)
    point = route.waypoints[0 if at_start else -1]
    peer = _old_coincident_bus_endpoint(controller, peer, at_start=peer_start, point=point)
    route = replace(route, **{
        "start_anchor" if at_start else "end_anchor": replace(selected, anchor_key=""),
    })
    project.diagram = replace(project.diagram, routes={**project.diagram.routes, route.id: route, peer.id: peer})
    controller = ProjectEditorController(project)
    selected = route.start_anchor if at_start else route.end_anchor
    peer_point = peer.waypoints[0 if peer_start else -1]
    assert (peer_point.x, peer_point.y) == pytest.approx((point.x, point.y))
    expected_ids = {route.id}
    original_routes = dict(controller.diagram.routes)
    original_reps = dict(controller.diagram.representations)
    before = _signature(controller)
    preview = controller.preview_bus_attachment_move(route.id, at_start=at_start, fraction=.73)
    assert len(preview) == 1 and preview[0].id == route.id
    assert dict(controller.diagram.routes) == original_routes
    changes = controller.move_bus_attachment(route.id, at_start=at_start, fraction=.73)
    assert set(changes) == expected_ids
    assert len(controller.journal) == 1
    assert frozenset(controller.diagram.routes) == frozenset(original_routes)
    assert dict(controller.diagram.representations) == original_reps
    for identifier, original in original_routes.items():
        changed = controller.diagram.routes[identifier]
        if identifier not in expected_ids:
            assert changed == original
        else:
            assert changed != original
            assert _anchor_identity(changed) == _anchor_identity(original)
            assert changed.waypoints[0].id == original.waypoints[0].id
            assert changed.waypoints[-1].id == original.waypoints[-1].id
    assert _signature(controller) == before
    assert controller.diagram.routes[peer.id] == peer
    assert tuple((point.x, point.y) for point in controller.diagram.routes[route.id].waypoints) == tuple(
        (point.x, point.y) for point in preview[0].waypoints)
    changed_routes = dict(controller.diagram.routes)
    save_project(tmp_path / "connected.json", project)
    restored = load_project(tmp_path / "connected.json")
    assert dict(restored.diagram.routes) == changed_routes
    assert _signature(ProjectEditorController(restored)) == before
    controller.undo()
    assert dict(controller.diagram.routes) == original_routes
    controller.redo()
    assert dict(controller.diagram.routes) == changed_routes
    assert _signature(controller) == before
    assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == source_hash
