"""Atomic placement proves preview geometry, voltage and history agree."""
from dataclasses import replace

import pytest

from rza_calc.domain.diagram import DiagramRouteKind
from rza_calc.domain.electrical import ElectricalModel, VoltageClassId
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor import NodeTarget, PortTarget
from rza_calc.editor.controller import EditorCommandError
from rza_calc.io.diagram import diagram_to_dict
from test_ui_connected_commands import _controller

U10 = VoltageClassId("builtin.voltage.ac.10kv")
U110 = VoltageClassId("builtin.voltage.ac.110kv")


def _snapshot(c):
    return ElectricalModelMemento.capture(c.model), diagram_to_dict(c.diagram), c.journal


def _bus(c):
    return c.add_electrical_node("Bus", x=0,y=0,voltage_class_id=U10,
        symbol_key="busbar_horizontal",width=240,height=20)


def test_new_attach_uses_exact_allocated_bus_contact_one_command_and_undo():
    c=_controller();bus=_bus(c)
    target=NodeTarget(bus.node_id,bus.representation_id,"0.5")
    before=_snapshot(c)
    proposal=c.preview_equipment_attachment("builtin.load","L1",target,x=0,y=250.5)
    assert proposal.valid,proposal.reason
    assert _snapshot(c)==before
    result=c.apply_equipment_placement(proposal)
    route=c.diagram.routes[result.route_ids[0]]
    assert route.kind is DiagramRouteKind.NODE_CONNECTION
    assert [(p.x,p.y) for p in route.waypoints]==[(p.x,p.y) for p in proposal.wire_points]
    assert c.diagram.representations[result.representation_id].y==250.5
    assert c.model.port_voltage_class(result.port_ids[0])==U10
    assert len(c.journal)==len(before[2])+1
    second=c.preview_equipment_attachment("builtin.load","L2",target,x=80,y=250)
    assert second.valid,second.reason
    assert second.target_point!=proposal.target_point
    assert abs(second.target_point[0]-proposal.target_point[0])>=10
    c.undo()
    assert _snapshot(c)[:2]==before[:2]
    c.redo()
    assert result.route_ids[0] in c.diagram.routes


def test_repeated_hover_uses_one_semantic_copy_and_readonly_state(monkeypatch):
    c=_controller();bus=_bus(c);before=_snapshot(c)
    target=NodeTarget(bus.node_id,bus.representation_id,"0.5")
    calls=[];original=ElectricalModel._transaction_copy
    def counted(model):
        calls.append(model)
        return original(model)
    monkeypatch.setattr(ElectricalModel,"_transaction_copy",counted)
    for i in range(25):
        proposal=c.preview_equipment_attachment("builtin.load","L",target,x=i,y=240)
        assert proposal.valid,proposal.reason
    assert calls==[c.model]
    assert _snapshot(c)==before


@pytest.mark.parametrize("mutation",["geometry","same_revision_record"])
def test_changed_inputs_reject_previously_green_proposal(mutation):
    c=_controller();bus=_bus(c)
    p=c.preview_equipment_attachment("builtin.load","L",NodeTarget(bus.node_id,bus.representation_id),x=0,y=240)
    assert p.valid
    if mutation=="geometry":
        row=c.diagram.representations[bus.representation_id]
        c._project.diagram=replace(c.diagram,representations={**c.diagram.representations,row.id:replace(row,x=row.x+1)})
    else:
        node=c.model.electrical_nodes[bus.node_id]
        c.model._electrical_nodes[node.id]=replace(node,name="Changed outside history")
    before=_snapshot(c)
    with pytest.raises(EditorCommandError,match="изменилась"):
        c.apply_equipment_placement(p)
    assert _snapshot(c)==before


def test_existing_free_move_and_attach_is_atomic_and_any_connected_port_blocks():
    c=_controller();bus=_bus(c)
    free=c.add_equipment("builtin.circuit_breaker","Q",x=400,y=400)
    before=_snapshot(c)
    role=c.model.ports[free.port_ids[0]].role
    p=c.preview_equipment_attachment("builtin.circuit_breaker","Q",NodeTarget(bus.node_id,bus.representation_id),
        x=0,y=200,rotation_deg=90,port_role=role,representation_id=free.representation_id)
    assert p.valid,p.reason
    r=c.apply_equipment_placement(p)
    assert r.equipment_id==free.equipment_id and r.port_ids==free.port_ids
    assert len(c.journal)==len(before[2])+1
    no=c.preview_equipment_attachment("builtin.circuit_breaker","Q",NodeTarget(bus.node_id,bus.representation_id),
        x=100,y=200,port_role=c.model.ports[free.port_ids[1]].role,representation_id=free.representation_id)
    assert not no.valid and "полностью свободному" in no.reason
    c.undo();assert _snapshot(c)[:2]==before[:2]
    c.redo();assert c.model.node_for_port(free.port_ids[0]).id==bus.node_id


def test_collision_proposes_nearest_free_position_without_moving_neighbour():
    c=_controller()
    fixed=c.add_equipment("builtin.load","Existing",x=0,y=0)
    old=c.diagram.representations[fixed.representation_id]
    p=c.preview_equipment_attachment("builtin.load","New",x=0,y=0)
    assert p.valid and p.adjusted
    r=c.apply_equipment_placement(p)
    assert c.diagram.representations[fixed.representation_id]==old
    placed=c.diagram.representations[r.representation_id]
    assert (placed.x,placed.y)==(p.x,p.y)


@pytest.mark.parametrize("bus_rotation,rotation,requested,expected",[
    (0,90,(0,40),(0,80)),
    (90,0,(40,0),(80,0)),
])
def test_bus_contact_tie_prefers_straight_lead_without_sliding_along_bus(
        bus_rotation,rotation,requested,expected):
    c=_controller()
    bus=c.add_electrical_node("Bus",x=0,y=0,voltage_class_id=U10,
        symbol_key="busbar_horizontal",width=240,height=20,rotation_deg=bus_rotation)
    before=_snapshot(c)
    p=c.preview_equipment_attachment("builtin.circuit_breaker","Q",
        NodeTarget(bus.node_id,bus.representation_id,"0.5"),
        x=requested[0],y=requested[1],rotation_deg=rotation,port_role="a",width=80,height=50)
    assert p.valid,p.reason
    assert (p.x,p.y)==expected
    assert len(p.wire_points)==2
    assert _snapshot(c)==before
    result=c.apply_equipment_placement(p)
    route=c.diagram.routes[result.route_ids[0]]
    assert [(w.x,w.y) for w in route.waypoints]==[(w.x,w.y) for w in p.wire_points]
    assert len(c.journal)==len(before[2])+1
    c.undo()
    assert _snapshot(c)[:2]==before[:2]


@pytest.mark.parametrize("case",["voltage","unknown","missing_role","invalid_property"])
def test_rejected_preview_does_not_create_equipment_or_history(case):
    c=_controller();bus=_bus(c);before=_snapshot(c)
    target=NodeTarget(bus.node_id,bus.representation_id)
    kwargs={"x":0,"y":200}
    type_id="builtin.load"
    if case=="voltage":kwargs["voltage_class_by_group"]={"main":U110}
    elif case=="unknown":
        node=c.model.electrical_nodes[bus.node_id]
        c.model._electrical_nodes[node.id]=replace(node,declared_voltage_class_id=None)
        before=_snapshot(c)
    elif case=="missing_role":type_id="builtin.circuit_breaker"
    else:kwargs["properties"]={"invalid_json":object()}
    p=c.preview_equipment_attachment(type_id,"L",target,**kwargs)
    assert not p.valid,p
    with pytest.raises(EditorCommandError):c.apply_equipment_placement(p)
    assert _snapshot(c)==before


def test_failed_commit_rolls_back_added_equipment_and_wire(monkeypatch):
    c=_controller();bus=_bus(c)
    p=c.preview_equipment_attachment("builtin.load","L",NodeTarget(bus.node_id,bus.representation_id),x=0,y=240)
    before=_snapshot(c)
    def fail(*args):raise EditorCommandError("Injected route failure")
    monkeypatch.setattr(c,"_add_route",fail)
    with pytest.raises(EditorCommandError,match="Injected"):
        c.apply_equipment_placement(p)
    assert _snapshot(c)==before
