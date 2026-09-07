"""The demo uses one conductor between apparatus, preserving its circuit."""
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from rza_calc.domain.diagram import DiagramRouteKind, RouteAnchorKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.labels import present_route_label, route_destination
from rza_calc.editor.orientation import rotated_port_layout
from rza_calc.io.project import load_project, save_project

DEMO=Path(__file__).resolve().parents[1]/'rza_calc/examples/oilfield_gtes.json'


@pytest.fixture(scope='module')
def project():
    return load_project(DEMO)


def _port_point(project, anchor):
    rep=project.diagram.representations[anchor.representation_id]
    equipment=project.electrical_model.equipment[rep.equipment_id]
    definition=project.electrical_model.equipment_types[(equipment.type_id,equipment.type_version)]
    graphics=rep.extensions['stage3_graphics']
    return next(p for p in rotated_port_layout(equipment,definition,
        width=graphics['width'],height=graphics['height'],rotation=rep.rotation_deg,
        center_x=rep.x,center_y=rep.y) if p.port_id==anchor.target_port_id)


def test_every_physical_line_goes_directly_between_apparatus_terminals(project):
    lines=[r for r in project.diagram.routes.values() if r.kind is DiagramRouteKind.EQUIPMENT_BRANCH]
    assert lines
    wire_ports={(r.page_id,a.target_port_id) for r in project.diagram.routes.values()
                if r.kind is DiagramRouteKind.NODE_CONNECTION
                for a in (r.start_anchor,r.end_anchor)}
    for route in lines:
        for anchor,point in ((route.start_anchor,route.waypoints[0]),(route.end_anchor,route.waypoints[-1])):
            assert anchor.kind is RouteAnchorKind.EQUIPMENT_PORT
            exact=_port_point(project,anchor)
            assert (point.x,point.y)==pytest.approx((exact.x,exact.y))
            assert (route.page_id,anchor.target_port_id) not in wire_ports
            assert project.electrical_model.ports[anchor.branch_port_id].equipment_id==route.equipment_id
    assert not project.diagram.validate_targets(project.electrical_model)


def test_plain_degree_two_apparatus_connection_has_one_wire_and_no_tail(project):
    by_node=defaultdict(list)
    for c in project.electrical_model.connections.values():by_node[c.electrical_node_id].append(c)
    wires=defaultdict(list)
    for route in project.diagram.routes.values():
        if route.kind is DiagramRouteKind.NODE_CONNECTION:
            wires[(route.page_id,route.electrical_node_id)].append(route)
    checked=0
    for rows in wires.values():
        node=rows[0].electrical_node_id
        peers=by_node[node]
        if len(peers)!=2:continue
        rep_equipment={r.equipment_id for r in project.diagram.representations.values()
                       if r.page_id==rows[0].page_id}
        if not all(project.electrical_model.ports[c.port_id].equipment_id in rep_equipment for c in peers):continue
        assert len(rows)==1
        assert all(a.kind is RouteAnchorKind.EQUIPMENT_PORT for a in (rows[0].start_anchor,rows[0].end_anchor))
        checked+=1
    assert checked>=60


def test_destination_uses_declared_to_role_even_when_saved_geometry_is_reversed(project):
    before=electrical_model_fingerprint(project.electrical_model)
    route=next(r for r in project.diagram.routes.values() if r.kind is DiagramRouteKind.EQUIPMENT_BRANCH)
    target=project.electrical_model.equipment[project.electrical_model.ports[route.end_anchor.target_port_id].equipment_id]
    content=present_route_label(project.electrical_model,route)
    assert content.destination==target.name
    assert content.name==project.electrical_model.equipment[route.equipment_id].name
    assert '→ '+target.name in content.display_name
    reversed_route=replace(route,start_anchor=route.end_anchor,end_anchor=route.start_anchor,
                           waypoints=tuple(reversed(route.waypoints)))
    assert route_destination(project.electrical_model,reversed_route)==target.name
    assert electrical_model_fingerprint(project.electrical_model)==before


def test_move_connected_breaker_keeps_one_line_and_undo_roundtrip(tmp_path):
    project=load_project(DEMO)
    controller=ProjectEditorController(project)
    route=next(r for r in project.diagram.routes.values() if r.kind is DiagramRouteKind.EQUIPMENT_BRANCH)
    controller.set_active_page(route.page_id)
    rep=project.diagram.representations[route.start_anchor.representation_id]
    before=electrical_model_fingerprint(project.electrical_model)
    original_routes=dict(project.diagram.routes)
    controller.move_representations((rep.id,),dx=40,dy=20,bypass_snap=True)
    moved=project.diagram.routes[route.id]
    assert moved.start_anchor==route.start_anchor and moved.end_anchor==route.end_anchor
    assert moved.waypoints[0].x==pytest.approx(route.waypoints[0].x+40)
    assert moved.waypoints[0].y==pytest.approx(route.waypoints[0].y+20)
    assert len(project.diagram.routes)==len(original_routes)
    assert electrical_model_fingerprint(project.electrical_model)==before
    controller.undo()
    assert dict(project.diagram.routes)==original_routes
    controller.redo()
    output=tmp_path/'direct-lines.json'
    save_project(output,project)
    loaded=load_project(output)
    assert electrical_model_fingerprint(loaded.electrical_model)==before
    assert loaded.diagram.routes[route.id].start_anchor==route.start_anchor
    assert not loaded.diagram.validate_targets(loaded.electrical_model)
