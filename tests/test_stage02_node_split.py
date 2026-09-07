"""Insert an impedance by isolating one port, preserving every external branch."""
from __future__ import annotations

from dataclasses import replace

import pytest

from rza_calc.core.methodology import Methodology
from rza_calc.core.model import Network
from rza_calc.domain import ProjectStructure
from rza_calc.domain.diagram import (
    DiagramPage, DiagramRouteId, DiagramRouteKind, GraphicalRepresentationId,
    PageId, RouteWaypoint, RouteWaypointId, RouteWaypointSource,
)
from rza_calc.domain.electrical import DataConfirmation, LineKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import (
    EditorCommandError, NodeTarget, PhysicalLineInput, PortTarget, ProjectEditorController,
)
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.project import FORMAT_VERSION, ProjectData, load_project, save_project
from test_bus_connection_spacing import PAGE, U10, _connect, _load, _setup


PHYSICAL = PhysicalLineInput(None, DataConfirmation.UNCONFIRMED)


def _state(controller):
    return (electrical_model_fingerprint(controller.model),
            dict(controller.model.connections), dict(controller.model.electrical_nodes),
            dict(controller.model.ports), diagram_to_dict(controller.diagram),
            len(controller.journal))


def _insert(controller, port, target):
    return controller.create_physical_line(
        "Inserted", LineKind.CABLE, PortTarget(port), target,
        physical=PHYSICAL, page_id=PAGE, split_shared_node=True,
    )


def _fanout(*, physical=False):
    controller, bus = _setup(width=320)
    selected = _load(controller, 140)
    wire_to_bus = _connect(controller, bus, selected, .25)
    peer = _load(controller, 700)
    external = controller.connect_ports(selected.port_ids[0], peer.port_ids[0], page_id=PAGE)
    unaffected = _connect(controller, bus, _load(controller, 940), .85)
    old_line = None
    if physical:
        far = controller.add_electrical_node("Far", x=1100, y=440, voltage_class_id=U10)
        old_line = controller.create_physical_line(
            "External physical line", LineKind.OVERHEAD,
            PortTarget(selected.port_ids[0]), NodeTarget(far.node_id), physical=PHYSICAL,
            route_waypoints=(
                RouteWaypoint(RouteWaypointId.new(), 140, 220),
                RouteWaypoint(RouteWaypointId.new(), 140, 100, RouteWaypointSource.USER, True),
                RouteWaypoint(RouteWaypointId.new(), 1100, 100, RouteWaypointSource.USER, True),
                RouteWaypoint(RouteWaypointId.new(), 1100, 440),
            ),
        )
    return controller, bus, selected, peer, wire_to_bus, external, unaffected, old_line


def test_detaching_fanout_centre_preserves_other_visible_wires_and_connections():
    controller, bus, selected, peer, removed, external, unaffected, _ = _fanout()
    selected_port = selected.port_ids[0]
    peer_connection = controller.model.connection_for_port(peer.port_ids[0])
    untouched_route = controller.diagram.routes[unaffected.route_id]
    before_journal = len(controller.journal)
    result = _insert(controller, selected_port, NodeTarget(bus.node_id, bus.representation_id, ".25"))
    assert controller.model.connection_for_port(selected_port).electrical_node_id == result.start_node_id
    assert result.start_node_id != bus.node_id
    assert controller.model.connection_for_port(peer.port_ids[0]) == peer_connection
    assert external.route_id in controller.diagram.routes, "the peer is still electrically connected and must remain visible"
    assert removed.route_id not in controller.diagram.routes
    retained = controller.diagram.routes[external.route_id]
    assert selected_port not in (retained.start_anchor.target_port_id, retained.end_anchor.target_port_id)
    assert retained.electrical_node_id == bus.node_id
    assert controller.diagram.routes[unaffected.route_id] == untouched_route
    assert not controller.diagram.validate_targets(controller.model)
    assert len(controller.journal) == before_journal + 1


def test_physical_external_branch_stays_on_old_node_and_keeps_manual_bends():
    controller, bus, selected, _, _, external, _, old_line = _fanout(physical=True)
    before = controller.diagram.routes[old_line.route_id]
    line_ports = controller.model.equipment[old_line.section_id].port_ids
    old_connections = {p: controller.model.connection_for_port(p) for p in line_ports}
    _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, bus.representation_id, ".25"))
    after = controller.diagram.routes[old_line.route_id]
    assert after.id == before.id and after.equipment_id == before.equipment_id
    assert after.start_anchor.electrical_node_id == bus.node_id
    assert after.start_anchor.branch_port_id == before.start_anchor.branch_port_id
    assert {p: controller.model.connection_for_port(p) for p in line_ports} == old_connections
    assert {p for p in before.waypoints if p.pinned} <= set(after.waypoints)
    assert before.waypoints[-1] == after.waypoints[-1]
    assert external.route_id in controller.diagram.routes
    assert not controller.diagram.validate_targets(controller.model)


def test_port_to_port_fanout_keeps_remaining_peer_visible_without_a_bus_symbol():
    controller, _ = _setup(width=320)
    first, chosen_peer, other_peer = [_load(controller, x) for x in (140, 600, 960)]
    removed = controller.connect_ports(first.port_ids[0], chosen_peer.port_ids[0], page_id=PAGE)
    retained = controller.connect_ports(first.port_ids[0], other_peer.port_ids[0], page_id=PAGE)
    old_node = controller.model.connection_for_port(first.port_ids[0]).electrical_node_id
    _insert(controller, first.port_ids[0], PortTarget(chosen_peer.port_ids[0]))
    assert retained.route_id in controller.diagram.routes
    assert removed.route_id not in controller.diagram.routes
    assert controller.model.connection_for_port(other_peer.port_ids[0]).electrical_node_id == old_node
    assert controller.diagram.routes[retained.route_id].electrical_node_id == old_node
    assert not controller.diagram.validate_targets(controller.model)


def test_all_page_representations_of_external_line_anchors_are_repaired():
    controller, bus, selected, _, _, _, _, old_line = _fanout(physical=True)
    original = controller.diagram.routes[old_line.route_id]
    page = PageId("page.other")
    reps = dict(controller.diagram.representations)
    anchors = []
    for anchor in (original.start_anchor, original.end_anchor):
        clone = replace(reps[anchor.representation_id], id=GraphicalRepresentationId.new(), page_id=page)
        reps[clone.id] = clone
        anchors.append(replace(anchor, representation_id=clone.id))
    duplicate = replace(original, id=DiagramRouteId.new(), page_id=page,
                        start_anchor=anchors[0], end_anchor=anchors[1],
                        waypoints=tuple(replace(p, id=RouteWaypointId.new()) for p in original.waypoints))
    controller._project.diagram = replace(
        controller.diagram, pages={**controller.diagram.pages, page: DiagramPage(page, "Other")},
        representations=reps, routes={**controller.diagram.routes, duplicate.id: duplicate},
    )
    controller = ProjectEditorController(controller._project)
    _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, bus.representation_id, ".25"))
    for route_id in (original.id, duplicate.id):
        route = controller.diagram.routes[route_id]
        assert route.start_anchor.electrical_node_id == bus.node_id
        assert route.start_anchor.target_port_id != selected.port_ids[0]
        assert controller.diagram.representations[route.start_anchor.representation_id].page_id == route.page_id
    assert not controller.diagram.validate_targets(controller.model)


def test_undo_redo_and_save_reload_keep_the_complete_split(tmp_path):
    controller, bus, selected, _, _, external, _, old_line = _fanout(physical=True)
    before = _state(controller)
    result = _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, bus.representation_id, ".25"))
    after = _state(controller)
    controller.undo()
    assert _state(controller)[:-1] == before[:-1]
    controller.redo()
    assert _state(controller)[:-1] == after[:-1]
    project = ProjectData(Network("Split"), Methodology.load(), {"name": "Split"},
                          ProjectStructure(), FORMAT_VERSION, controller.model,
                          diagram=controller.diagram, catalog_snapshots=controller._project.catalog_snapshots)
    path = tmp_path / "split.json"
    save_project(path, project)
    loaded = load_project(path)
    assert electrical_model_fingerprint(loaded.electrical_model) == after[0]
    assert loaded.diagram.routes == controller.diagram.routes
    assert {external.route_id, old_line.route_id, result.route_id} <= set(loaded.diagram.routes)
    assert not loaded.diagram.validate_targets(loaded.electrical_model)


def test_late_failure_rolls_back_port_move_external_routes_and_history(monkeypatch):
    controller, bus, selected, _, _, _, _, _ = _fanout()
    before = _state(controller)

    def refused(*args, **kwargs):
        raise EditorCommandError("deliberate final route failure")

    monkeypatch.setattr(controller, "_add_route", refused)
    with pytest.raises(EditorCommandError, match="deliberate final route failure"):
        _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, bus.representation_id, ".25"))
    assert _state(controller) == before


def test_selected_physical_branch_port_moves_with_its_own_route_anchor():
    controller, bus = _setup(width=320)
    far = controller.add_electrical_node("Far", x=1000, y=400, voltage_class_id=U10)
    existing = controller.create_physical_line(
        "Existing", LineKind.OVERHEAD, NodeTarget(bus.node_id, bus.representation_id),
        NodeTarget(far.node_id), physical=PHYSICAL,
    )
    port = controller.model.port_by_role(existing.section_id, "from").id
    result = _insert(controller, port, NodeTarget(bus.node_id, bus.representation_id))
    old_route = controller.diagram.routes[existing.route_id]
    assert old_route.start_anchor.branch_port_id == port
    assert old_route.start_anchor.electrical_node_id == result.start_node_id
    assert result.start_node_id != bus.node_id
    assert not controller.diagram.validate_targets(controller.model)


def test_selected_node_representation_is_used_without_losing_other_representations():
    controller, bus, selected, _, original_wire, external, untouched, _ = _fanout()
    old_representation = controller.diagram.representations[bus.representation_id]
    second = replace(old_representation, id=GraphicalRepresentationId.new(), x=1100, y=-120)
    controller._project.diagram = replace(controller.diagram,
        representations={**controller.diagram.representations, second.id: second})
    controller = ProjectEditorController(controller._project)
    old_untouched = controller.diagram.routes[untouched.route_id]
    result = _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, second.id, ".4"))
    assert controller.diagram.routes[result.route_id].end_anchor.representation_id == second.id
    assert original_wire.route_id in controller.diagram.routes
    old_wire = controller.diagram.routes[original_wire.route_id]
    assert {old_wire.start_anchor.representation_id, old_wire.end_anchor.representation_id} == {
        second.id, bus.representation_id,
    }
    assert external.route_id in controller.diagram.routes
    assert controller.diagram.routes[untouched.route_id] == old_untouched
    assert controller.diagram.representations[bus.representation_id] == old_representation
    assert controller.diagram.representations[second.id] == second
    assert not controller.diagram.validate_targets(controller.model)


def test_external_physical_end_anchor_and_wire_end_anchor_are_both_repaired():
    controller, bus, selected, _, _, external, _, old_line = _fanout(physical=True)
    routes = dict(controller.diagram.routes)
    for route_id in (external.route_id, old_line.route_id):
        route = routes[route_id]
        routes[route_id] = replace(route, start_anchor=route.end_anchor, end_anchor=route.start_anchor,
                                   waypoints=tuple(reversed(route.waypoints)))
    controller._project.diagram = replace(controller.diagram, routes=routes)
    controller = ProjectEditorController(controller._project)
    _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, bus.representation_id, ".25"))
    for route_id in (external.route_id, old_line.route_id):
        route = controller.diagram.routes[route_id]
        assert route.end_anchor.electrical_node_id == bus.node_id
        assert route.end_anchor.target_port_id != selected.port_ids[0]
        assert route.start_anchor == routes[route_id].start_anchor
    assert not controller.diagram.validate_targets(controller.model)


def test_reconnect_commit_honours_preview_adoption_of_an_empty_target_group():
    controller, bus = _setup(width=320)
    source = _load(controller, 140)
    _connect(controller, bus, source)
    target = controller.add_equipment("builtin.load", "New empty target", x=960, y=240, page_id=PAGE)
    source_port, target_port = source.port_ids[0], target.port_ids[0]
    before = _state(controller)
    assert controller.preview_connection_voltage(source_port, target_port).valid
    assert _state(controller) == before
    controller.reconnect_port(source_port, PortTarget(target_port), page_id=PAGE)
    assert controller.model.port_voltage_class(target_port) == U10
    assert controller.model.connection_for_port(source_port).electrical_node_id == (
        controller.model.connection_for_port(target_port).electrical_node_id)
    assert len(controller.journal) == before[-1] + 1
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert _state(controller)[:-1] == before[:-1]


def test_tap_commit_honours_preview_adoption_of_an_empty_source_group():
    controller, bus = _setup(width=320)
    far = controller.add_electrical_node("Far", x=1000, y=440, voltage_class_id=U10)
    line = controller.create_physical_line(
        "Existing", LineKind.OVERHEAD, NodeTarget(bus.node_id, bus.representation_id),
        NodeTarget(far.node_id), physical=PHYSICAL,
    )
    source = controller.add_equipment("builtin.load", "New empty source", x=700, y=700, page_id=PAGE)
    port = source.port_ids[0]
    before = _state(controller)
    assert controller.preview_connection_voltage(port, bus.node_id).valid
    assert _state(controller) == before
    result = controller.reconnect_port_to_tap(port, line.section_id, None, page_id=PAGE)
    assert controller.model.port_voltage_class(port) == U10
    assert controller.model.connection_for_port(port).electrical_node_id == result.tap_node_id
    assert len(controller.journal) == before[-1] + 1
    assert not controller.diagram.validate_targets(controller.model)
    controller.undo()
    assert _state(controller)[:-1] == before[:-1]
