"""Acceptance of the large demo's saved drawings against its electrical model."""
from pathlib import Path

import pytest

from rza_calc.domain.diagram import DiagramRouteKind, RouteAnchorKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.orientation import rotated_port_layout
from rza_calc.io.project import ProjectData, load_project
from tools.autolayout import legacy_id
from tools.oilfield_layout import build_layout


@pytest.fixture(scope='module')
def project():
    return load_project(Path(__file__).resolve().parents[1]/'rza_calc/examples/oilfield_gtes.json')


def test_every_equipment_and_node_is_placed_and_every_route_target_is_valid(project):
    diagram=project.diagram
    model=project.electrical_model
    placed={r.equipment_id for r in diagram.representations.values() if r.equipment_id}
    placed.update(r.equipment_id for r in diagram.routes.values() if r.equipment_id)
    assert placed==set(model.equipment)
    assert {r.electrical_node_id for r in diagram.representations.values() if r.electrical_node_id} == set(model.electrical_nodes)
    assert len(diagram.pages)==34
    assert not diagram.validate_targets(model)


def test_layout_is_deterministic_and_geometry_only_with_one_network_capture(project,monkeypatch):
    before=electrical_model_fingerprint(project.electrical_model)
    getter=ProjectData.__getattribute__
    reads=[]
    def counted(self,name):
        if self is project and name=='network':reads.append(name)
        return getter(self,name)
    monkeypatch.setattr(ProjectData,'__getattribute__',counted)
    first=build_layout(project,project.metadata['oilfield_demo'])
    second=build_layout(project,project.metadata['oilfield_demo'])
    assert first==second
    assert reads==['network','network']
    assert electrical_model_fingerprint(project.electrical_model)==before


def test_incoming_line_repeated_views_refer_to_one_physical_equipment(project):
    manifest=project.metadata['oilfield_demo']
    model=project.electrical_model
    equipment={legacy_id(e):e for e in model.equipment.values()}
    for facility in manifest['facilities']:
        for incoming in facility['incoming']:
            eid=equipment[incoming['line']].id
            routes=[r for r in project.diagram.routes.values() if r.equipment_id==eid]
            assert len(routes)==2
            assert len({r.page_id for r in routes})==2
            assert len({(r.start_anchor.branch_port_id,r.end_anchor.branch_port_id) for r in routes})==1
            assert len({(r.start_anchor.electrical_node_id,r.end_anchor.electrical_node_id) for r in routes})==1


def test_saved_connection_endpoints_reach_the_correct_symbol_ports(project):
    model=project.electrical_model
    for route in project.diagram.routes.values():
        if route.kind is not DiagramRouteKind.NODE_CONNECTION:continue
        anchor=route.start_anchor
        assert anchor.kind is RouteAnchorKind.EQUIPMENT_PORT
        rep=project.diagram.representations[anchor.representation_id]
        equipment=model.equipment[rep.equipment_id]
        definition=model.equipment_types[(equipment.type_id,equipment.type_version)]
        graphics=rep.extensions['stage3_graphics']
        point=next(p for p in rotated_port_layout(equipment,definition,
            width=graphics['width'],height=graphics['height'],rotation=rep.rotation_deg,
            center_x=rep.x,center_y=rep.y) if p.port_id==anchor.target_port_id)
        assert (route.waypoints[0].x,route.waypoints[0].y)==pytest.approx((point.x,point.y))


def test_gtes_auxiliary_feeders_do_not_visually_join_110kv_buses(project):
    model=project.electrical_model
    nodes={legacy_id(n):n.id for n in model.electrical_nodes.values()}
    first=min(project.diagram.pages.values(),key=lambda p:p.order)
    high_buses=[r for r in project.diagram.representations.values()
                if r.page_id==first.id and r.electrical_node_id in {nodes['gtes_hv1'],nodes['gtes_hv2']}]
    equipment={legacy_id(e):e.id for e in model.equipment.values()}
    gtes=project.metadata['oilfield_demo']['facilities'][0]
    for load in gtes['loads']:
        route=next(r for r in project.diagram.routes.values() if r.page_id==first.id and r.equipment_id==equipment[load['line']])
        for a,b in zip(route.waypoints,route.waypoints[1:]):
            for bus in high_buses:
                half=bus.extensions['stage3_graphics']['width']/2
                assert not (a.x==b.x and min(a.y,b.y)<=bus.y<=max(a.y,b.y)
                            and bus.x-half<=a.x<=bus.x+half)


def test_all_node_endpoints_and_foreign_bus_crossings(project):
    diagram=project.diagram
    buses=[rep for rep in diagram.representations.values() if rep.symbol_key=='busbar']
    for route in diagram.routes.values():
        for anchor,point in ((route.start_anchor,route.waypoints[0]),
                             (route.end_anchor,route.waypoints[-1])):
            if anchor.kind is RouteAnchorKind.EQUIPMENT_PORT:continue
            rep=diagram.representations[anchor.representation_id]
            if anchor.kind is RouteAnchorKind.BUS:
                half=rep.extensions['stage3_graphics']['width']/2
                assert point.y==pytest.approx(rep.y)
                assert rep.x-half<=point.x<=rep.x+half
            else:
                assert (point.x,point.y)==pytest.approx((rep.x,rep.y))
        for bus in buses:
            if bus.page_id!=route.page_id or bus.electrical_node_id in {
                route.start_anchor.electrical_node_id,route.end_anchor.electrical_node_id}:
                continue
            half=bus.extensions['stage3_graphics']['width']/2
            for a,b in zip(route.waypoints,route.waypoints[1:]):
                if a.x==b.x:
                    assert not (bus.x-half<=a.x<=bus.x+half and min(a.y,b.y)<=bus.y<=max(a.y,b.y))
                elif a.y==bus.y:
                    assert max(min(a.x,b.x),bus.x-half)>min(max(a.x,b.x),bus.x+half)
