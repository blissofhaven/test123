"""A visible switch splits node incidence, across every picture, atomically."""
from dataclasses import replace

import pytest

from rza_calc.domain.diagram import DiagramPage, DiagramRouteId, GraphicalRepresentationId, PageId, RouteWaypointId
from rza_calc.domain.electrical import SwitchPosition
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor import NodeTarget, ProjectEditorController
from rza_calc.editor.controller import EditorCommandError
from rza_calc.editor.ordinary_wire_insertion import partition_wire
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict
from rza_calc.topology import TopologyEngine
from test_equipment_attachment_commands import _bus,_snapshot,U10
from test_ui_connected_commands import _controller


def _wire():
    c=_controller();bus=_bus(c)
    p=c.preview_equipment_attachment("builtin.load","Load",NodeTarget(bus.node_id,bus.representation_id,"0.5"),x=0,y=400)
    load=c.apply_equipment_placement(p)
    return c,bus,load,load.route_ids[0]


def test_inline_has_two_nodes_retains_old_connection_ids_and_rolls_back_whole_command():
    c,bus,load,route_id=_wire();before=_snapshot(c)
    old_connections=dict(c.model.connections)
    p=c.preview_inline_equipment(route_id,0,200)
    assert p.valid,p.reason
    assert p.wire_points and p.extra_wire_points
    assert _snapshot(c)==before
    r=c.apply_equipment_placement(p)
    assert len(set(r.node_ids))==2
    assert {c.model.node_for_port(port).id for port in r.port_ids}==set(r.node_ids)
    assert set(old_connections)<=set(c.model.connections)
    assert c.model.node_for_port(load.port_ids[0]).id!=bus.node_id
    assert c.diagram.validate_targets(c.model)==()
    assert len(c.journal)==len(before[2])+1
    after=_snapshot(c)
    c.undo();assert _snapshot(c)[:2]==before[:2]
    c.redo();assert _snapshot(c)[:2]==after[:2]
    model=electrical_model_from_dict(electrical_model_to_dict(c.model))
    diagram=diagram_from_dict(diagram_to_dict(c.diagram),model)
    assert diagram.validate_targets(model)==()
    assert model.connectivity_signature()==c.model.connectivity_signature()


def test_inline_closed_connects_sides_and_open_separates_them():
    c,bus,load,route_id=_wire()
    r=c.apply_equipment_placement(c.preview_inline_equipment(route_id,0,200))
    engine=TopologyEngine()
    closed=engine.compile(c.model)
    # Public canonical topology must change when this real series switch opens.
    equipment=c.model.equipment[r.equipment_id]
    c.model._equipment[equipment.id]=replace(equipment,normal_position=SwitchPosition.OPEN)
    opened=engine.compile(c.model)
    assert len(opened.components)==len(closed.components)+1


def test_duplicate_reversed_sheet_views_are_both_split_and_port_ids_shared():
    c,bus,load,route_id=_wire()
    page=DiagramPage(PageId("second"),"Second")
    old=c.diagram.routes[route_id]
    rep_map={}
    for rid in (old.start_anchor.representation_id,old.end_anchor.representation_id):
        row=c.diagram.representations[rid]
        rep_map[rid]=replace(row,id=GraphicalRepresentationId.new(),page_id=page.id,x=row.x+500)
    duplicate=replace(old,id=DiagramRouteId.new(),page_id=page.id,
        start_anchor=replace(old.end_anchor,representation_id=rep_map[old.end_anchor.representation_id].id),
        end_anchor=replace(old.start_anchor,representation_id=rep_map[old.start_anchor.representation_id].id),
        waypoints=tuple(replace(p,id=RouteWaypointId.new(),x=p.x+500) for p in reversed(old.waypoints)))
    c._project.diagram=replace(c.diagram,pages={**c.diagram.pages,page.id:page},
        representations={**c.diagram.representations,**{r.id:r for r in rep_map.values()}},
        routes={**c.diagram.routes,duplicate.id:duplicate})
    c=ProjectEditorController(c._project)
    before=_snapshot(c)
    p=c.preview_inline_equipment(route_id,0,200)
    assert p.valid,p.reason
    r=c.apply_equipment_placement(p)
    assert len(r.route_ids)==4
    assert route_id in r.route_ids and duplicate.id in r.route_ids
    views=c.diagram.representations_for_equipment(r.equipment_id)
    assert len(views)==2
    assert {(v.page_id,v.x,v.y) for v in views}=={(old.page_id,0,200),(page.id,500,200)}
    assert c.diagram.validate_targets(c.model)==()
    for route in c.diagram.routes.values():
        for anchor in (route.start_anchor,route.end_anchor):
            if anchor.target_port_id:
                assert c.model.node_for_port(anchor.target_port_id).id==anchor.electrical_node_id
    c.undo();assert _snapshot(c)[:2]==before[:2]


@pytest.mark.parametrize("case",["endpoint","off_wire","hidden_port","parallel"])
def test_ambiguous_or_invalid_cut_rejects_without_mutation(case):
    c,bus,load,route_id=_wire()
    x,y=0,200
    if case=="endpoint":y=0
    elif case=="off_wire":x=1
    elif case=="hidden_port":
        hidden=c.add_equipment("builtin.load","No drawing wire",x=700,y=500,voltage_class_by_group={"main":U10})
        c.model.connect_port(hidden.port_ids[0],bus.node_id)
    else:
        # Distinct junction detour is a real parallel path, not a second picture.
        joint=c.add_electrical_node("J",x=200,y=200,voltage_class_id=U10)
        c.connect_from_node(NodeTarget(bus.node_id,bus.representation_id),NodeTarget(joint.node_id,joint.representation_id))
        from rza_calc.domain.diagram import RouteEndpointAnchor,RouteAnchorKind
        old=c.diagram.routes[route_id]
        alternate=replace(old,id=DiagramRouteId.new(),end_anchor=RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE,joint.representation_id,bus.node_id),
            waypoints=tuple(replace(p,id=RouteWaypointId.new()) for p in old.waypoints))
        c._project.diagram=replace(c.diagram,routes={**c.diagram.routes,alternate.id:alternate})
    before=_snapshot(c)
    p=c.preview_inline_equipment(route_id,x,y)
    assert not p.valid,p
    with pytest.raises(EditorCommandError):c.apply_equipment_placement(p)
    assert _snapshot(c)==before


def test_existing_free_qf_moves_into_cut_in_one_history_command():
    c,bus,load,route_id=_wire()
    free=c.add_equipment("builtin.circuit_breaker","Own QF",x=500,y=500)
    before=_snapshot(c)
    p=c.preview_inline_equipment(route_id,0,200,representation_id=free.representation_id)
    assert p.valid,p.reason
    r=c.apply_equipment_placement(p)
    assert r.equipment_id==free.equipment_id and r.port_ids==free.port_ids
    assert len(c.journal)==len(before[2])+1
    c.undo();assert _snapshot(c)[:2]==before[:2]


def test_two_parallel_wires_on_same_sheet_are_not_mistaken_for_duplicate_views():
    c,bus,load,route_id=_wire()
    original=c.diagram.routes[route_id]
    duplicate=replace(original,id=DiagramRouteId.new(),
        waypoints=tuple(replace(p,id=RouteWaypointId.new()) for p in original.waypoints))
    c._project.diagram=replace(c.diagram,routes={**c.diagram.routes,duplicate.id:duplicate})
    before=_snapshot(c)
    p=c.preview_inline_equipment(route_id,0,200)
    assert not p.valid and "несколько проводов" in p.reason
    assert _snapshot(c)==before


def test_physical_branch_endpoint_follows_its_partition_without_parameter_change():
    from rza_calc.domain.electrical import DataConfirmation,LineKind
    from rza_calc.editor import PhysicalLineInput,PortTarget
    c,bus,load,route_id=_wire()
    end=c.add_electrical_node("End",x=500,y=500,voltage_class_id=U10)
    physical=c.create_physical_line("КЛ",LineKind.CABLE,
        PortTarget(load.port_ids[0],load.representation_id),NodeTarget(end.node_id,end.representation_id),
        physical=PhysicalLineInput(250000,DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km":.3,"x1_ohm_per_km":.1},DataConfirmation.CONFIRMED))
    equipment=c.model.equipment[physical.section_id]
    section=c.model.line_sections[physical.section_id]
    connections={p:c.model.connection_for_port(p) for p in equipment.port_ids}
    p=c.preview_inline_equipment(route_id,0,200)
    assert p.valid,p.reason
    result=c.apply_equipment_placement(p)
    assert c.model.equipment[equipment.id]==equipment
    assert c.model.line_sections[equipment.id]==section
    assert all(c.model.connection_for_port(port).id==old.id for port,old in connections.items())
    start=c.diagram.routes[physical.route_id].start_anchor
    assert start.target_port_id==load.port_ids[0]
    assert start.electrical_node_id==c.model.node_for_port(load.port_ids[0]).id!=bus.node_id
    assert c.diagram.validate_targets(c.model)==()


def test_inline_failed_diagram_validation_rolls_back_node_partition(monkeypatch):
    c,bus,load,route_id=_wire()
    p=c.preview_inline_equipment(route_id,0,200)
    before=_snapshot(c)
    def reject(*args):raise EditorCommandError("Injected validation failure")
    monkeypatch.setattr(c.history,"_validate",reject)
    with pytest.raises(EditorCommandError,match="Injected"):
        c.apply_equipment_placement(p)
    assert _snapshot(c)==before
