"""Explicit locks and guarded geometric commands preserve canonical data."""
from dataclasses import replace

import pytest

from rza_calc.domain.diagram import RouteWaypoint,RouteWaypointId,RouteWaypointSource
from rza_calc.editor.connected_geometry import shift_route_segment
from rza_calc.editor.controller import EditorCommandError
from rza_calc.editor.orthogonal_routing import _segment_hits_obstacle
from test_ordinary_wire_insertion import _wire
from test_equipment_attachment_commands import _snapshot


def test_pin_and_auto_route_noop_do_not_add_history_on_straight_route():
    c,bus,load,rid=_wire();before=_snapshot(c)
    c.set_route_bends_pinned(rid,True)
    c.auto_route_diagram_route(rid)
    assert _snapshot(c)==before


def test_manual_pin_command_preserves_ids_and_undo_then_auto_clears_detour():
    c,bus,load,rid=_wire()
    c.move_route_segment(rid,0,dx=100,dy=0)
    original=c.diagram.routes[rid]
    assert len(original.waypoints)>2
    before=_snapshot(c)
    c.set_route_bends_pinned(rid,True)
    pinned=c.diagram.routes[rid]
    assert tuple(p.id for p in pinned.waypoints)==tuple(p.id for p in original.waypoints)
    assert all(p.pinned and p.source is RouteWaypointSource.USER for p in pinned.waypoints[1:-1])
    c.undo();assert _snapshot(c)[:2]==before[:2]
    c.redo()
    c.auto_route_diagram_route(rid)
    assert len(c.diagram.routes[rid].waypoints)==2
    assert _snapshot(c)[0]==before[0]


def test_segment_drag_preview_and_commit_share_body_guard_and_exact_lane():
    c,bus,load,rid=_wire()
    other=c.add_equipment("builtin.load","Foreign body",x=100,y=200)
    before=_snapshot(c);original=c.diagram.routes[rid]
    occupied,bodies=c.preview_route_segment_constraints(rid)
    # This requested x=100 passes through the foreign symbol; nearby lane is
    # allowed only when the same guarded preview can display it first.
    try:
        points=shift_route_segment(original,0,dx=100,dy=0,occupied_segments=occupied,obstacles=bodies)
    except ValueError:
        with pytest.raises(EditorCommandError):c.move_route_segment(rid,0,dx=100,dy=0)
        assert _snapshot(c)==before
    else:
        assert not any(_segment_hits_obstacle((a.x,a.y),(b.x,b.y),body)
            for a,b in zip(points,points[1:]) for body in bodies)
        c.move_route_segment(rid,0,dx=100,dy=0)
        assert [(p.x,p.y) for p in c.diagram.routes[rid].waypoints]==[(p.x,p.y) for p in points]
        assert _snapshot(c)[0]==before[0]


def test_direct_route_update_cannot_bypass_apparatus_body_guard():
    c,bus,load,rid=_wire()
    c.add_equipment("builtin.load","Foreign body",x=100,y=200)
    old=c.diagram.routes[rid];before=_snapshot(c)
    points=(old.waypoints[0],RouteWaypoint(RouteWaypointId.new(),100,old.waypoints[0].y),
        RouteWaypoint(RouteWaypointId.new(),100,0),old.waypoints[-1])
    with pytest.raises(EditorCommandError,match="тело аппарата"):
        c.reroute_diagram_route(rid,points)
    assert _snapshot(c)==before


def test_simultaneously_reflowed_routes_ignore_old_locations_but_all_final_routes_are_separate():
    from pathlib import Path
    from rza_calc.io.project import load_project
    from rza_calc.editor.controller import ProjectEditorController
    from rza_calc.editor.orthogonal_routing import segments_overlap
    from test_ui_direct_connections import _actual_series_pairs
    c=ProjectEditorController(load_project(Path(__file__).parent/'fixtures/legacy_projects/energoraion.json'))
    node,breaker=_actual_series_pairs(c._project)[0]
    before=_snapshot(c)
    planned=c.preview_move_representations((breaker.id,),0,-180,bypass_snap=True)
    changed=[r for r in planned.routes.values() if r!=c.diagram.routes[r.id]]
    assert len(changed)>=3
    assert planned.representations[node.id]!=c.diagram.representations[node.id]
    assert _snapshot(c)==before
    for route in changed:
        for other in planned.routes.values():
            if route.id==other.id or route.page_id!=other.page_id:
                continue
            assert not any(segments_overlap((a.x,a.y),(b.x,b.y),(x.x,x.y,y.x,y.y))
                for a,b in zip(route.waypoints,route.waypoints[1:])
                for x,y in zip(other.waypoints,other.waypoints[1:]))
    c.move_representations((breaker.id,),0,-180,bypass_snap=True)
    assert {k:tuple((p.x,p.y) for p in r.waypoints) for k,r in c.diagram.routes.items()}=={
        k:tuple((p.x,p.y) for p in r.waypoints) for k,r in planned.routes.items()}
    assert _snapshot(c)[0]==before[0]


def test_own_line_terminal_and_attached_line_have_opposite_graphical_leads():
    from test_stage01_result_freshness import opened
    from rza_calc.domain.electrical import DataConfirmation,LineKind
    from rza_calc.editor import NodeTarget,PortTarget,PhysicalLineInput
    from rza_calc.editor.orthogonal_routing import segments_overlap
    vm,c=opened()
    equipment=next(e for e in c.model.equipment.values() if e.properties.get("legacy_payload",{}).get("kind")=="line")
    port=c.model.port_by_role(equipment.id,"from")
    node=c.model.node_for_port(port.id).id
    original_parameters=equipment.properties
    inserted=c.create_physical_line("Draft cable",LineKind.CABLE,PortTarget(port.id),NodeTarget(node),
        physical=PhysicalLineInput(None,DataConfirmation.UNCONFIRMED),split_shared_node=True)
    own=next(r for r in c.diagram.routes.values() if r.equipment_id==equipment.id)
    attached=c.diagram.routes[inserted.route_id]
    assert own.start_anchor.branch_port_id==own.start_anchor.target_port_id==port.id
    assert attached.start_anchor.target_port_id==port.id
    assert not any(segments_overlap((a.x,a.y),(b.x,b.y),(x.x,x.y,y.x,y.y))
        for a,b in zip(own.waypoints,own.waypoints[1:])
        for x,y in zip(attached.waypoints,attached.waypoints[1:]))
    assert c.model.equipment[equipment.id].properties==original_parameters
    assert not c.diagram.validate_targets(c.model)
