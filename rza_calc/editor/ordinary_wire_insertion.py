"""Insert a series switch only across an unambiguous drawn node bridge.

Ordinary wires are zero-impedance incidence, not electrical branches. Cutting
one requires partitioning the node's *ports*, including all other sheet views.
No equipment impedance or protection setting is synthesized here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from rza_calc.domain.diagram import (
    DiagramRouteId, DiagramRouteKind, GraphicalRepresentationId,
    RouteAnchorKind, RouteEndpointAnchor, RouteWaypoint, RouteWaypointId,
)
from rza_calc.domain.electrical import DomainInvariantError, ElectricalNodeId, thaw_json
from .equipment_attachment import (
    EquipmentPlacementProposal, EquipmentPlacementEditResult, _free,
    occupied_segments, place_on_draft, preview_attachment, current_inputs,
)
from .history import ProjectDraft
from .orientation import direction_toward_point, quarter_turn_for_port_toward_point, rotated_port_layout
from .orthogonal_routing import RouteVertex, RouteVertexSource, RoutingRequest, RoutingObstacle, build_orthogonal_route
from .placement import EquipmentPlacementKind, EquipmentPlacementRegistry


@dataclass(frozen=True, slots=True)
class WirePartition:
    node_id: ElectricalNodeId
    first_vertices: frozenset
    second_vertices: frozenset
    cut_routes: tuple
    vertex_by_representation: tuple
    keep_first: bool


@dataclass(frozen=True, slots=True)
class InlinePlan:
    partition: WirePartition
    views: tuple  # (old route, apparatus representation, first lead, second lead)


def partition_wire(model, diagram, route_id):
    route = diagram.routes.get(route_id)
    if route is None or route.kind is not DiagramRouteKind.NODE_CONNECTION:
        raise ValueError("Вставка аппарата возможна только в обычный провод.")
    node_id = route.electrical_node_id
    reps = {r.id: r for r in diagram.representations.values() if r.electrical_node_id == node_id}
    has_bus = any("busbar" in r.symbol_key for r in reps.values())
    def joint(row):
        if "busbar" in row.symbol_key:
            return ("bus", node_id)
        if row.extensions.get("linked_page_id"):
            return ("bus" if has_bus else "linked", node_id)
        return ("joint", row.id)
    rep_vertices = {r.id: joint(r) for r in reps.values()}
    def vertex(anchor):
        if anchor.target_port_id is not None:
            return ("port", anchor.target_port_id)
        if anchor.representation_id not in rep_vertices:
            raise ValueError("У провода отсутствует однозначная графическая привязка узла.")
        return rep_vertices[anchor.representation_id]
    first, second = vertex(route.start_anchor), vertex(route.end_anchor)
    if first == second:
        raise ValueError("Обе стороны провода представляют одну неразделимую точку.")
    pair = frozenset((first, second))
    cut = tuple(r for r in diagram.routes.values() if r.kind is DiagramRouteKind.NODE_CONNECTION
                and r.electrical_node_id == node_id
                and frozenset((vertex(r.start_anchor), vertex(r.end_anchor))) == pair)
    if len({r.page_id for r in cut}) != len(cut):
        raise ValueError("На листе несколько проводов между этими выводами; последовательная вставка неоднозначна.")
    # Multiple pictures of the very same terminals are cut together. Different
    # edges creating a parallel bypass are deliberately not guessed away.
    cut_ids = {r.id for r in cut}
    graph = {}
    def touch(v):
        graph.setdefault(v, set())
    def edge(a, b):
        touch(a); touch(b)
        graph[a].add(b); graph[b].add(a)
    touch(first); touch(second)
    for connection in model.connections.values():
        if connection.electrical_node_id == node_id:
            touch(("port", connection.port_id))
    for other in diagram.routes.values():
        if other.kind is DiagramRouteKind.NODE_CONNECTION and other.electrical_node_id == node_id:
            if other.id not in cut_ids:
                edge(vertex(other.start_anchor), vertex(other.end_anchor))
        elif other.kind is DiagramRouteKind.EQUIPMENT_BRANCH:
            for anchor in (other.start_anchor, other.end_anchor):
                if anchor.electrical_node_id == node_id:
                    edge(("port", anchor.branch_port_id), vertex(anchor))
    def component(start):
        found, pending = set(), [start]
        while pending:
            item = pending.pop()
            if item not in found:
                found.add(item); pending.extend(graph[item] - found)
        return frozenset(found)
    left, right = component(first), component(second)
    if left & right:
        raise ValueError("У провода есть обходная связь: стороны выключателя нельзя разделить однозначно.")
    if left | right != set(graph):
        raise ValueError("Не все выводы узла имеют однозначные видимые связи; вставка отменена.")
    if any(v not in left | right for v in rep_vertices.values()):
        raise ValueError("Есть дополнительные изображения узла без определённой стороны вставки.")
    keep_first = ("bus", node_id) not in right
    return WirePartition(node_id, left, right, cut, tuple(rep_vertices.items()), keep_first)


def _point(route, x, y):
    lengths = [abs(b.x-a.x)+abs(b.y-a.y) for a,b in zip(route.waypoints, route.waypoints[1:])]
    total = sum(lengths)
    traversed = 0
    for index, (a,b) in enumerate(zip(route.waypoints, route.waypoints[1:])):
        if ((a.x == b.x and abs(x-a.x)<1e-8 and min(a.y,b.y)<=y<=max(a.y,b.y)) or
            (a.y == b.y and abs(y-a.y)<1e-8 and min(a.x,b.x)<=x<=max(a.x,b.x))):
            distance = traversed+abs(x-a.x)+abs(y-a.y)
            if not 0 < distance < total:
                raise ValueError("Выберите внутреннюю точку обычного провода.")
            return distance/total, index
        traversed += lengths[index]
    raise ValueError("Точка установки не лежит на выбранном проводе.")


def _fraction_point(route, fraction):
    lengths = [abs(b.x-a.x)+abs(b.y-a.y) for a,b in zip(route.waypoints, route.waypoints[1:])]
    remaining = sum(lengths)*fraction
    for index, (a,b) in enumerate(zip(route.waypoints, route.waypoints[1:])):
        if remaining <= lengths[index] and lengths[index]:
            f = remaining/lengths[index]
            return a.x+(b.x-a.x)*f, a.y+(b.y-a.y)*f, index
        remaining -= lengths[index]
    raise ValueError("Маршрут не имеет участка для вставки.")


def _split_electrical(controller, model, ports, partition):
    """Used by the cached semantic preview and the isolated command alike."""
    original=model.electrical_nodes[partition.node_id]
    new_id=ElectricalNodeId.new()
    extensions=thaw_json(original.extensions)
    legacy=extensions.get("legacy_calculation")
    if isinstance(legacy,dict):
        legacy.pop("legacy_id",None)
        legacy.get("payload",{}).update(kind="point",section=None)
    extensions["creation_origin"]="explicit_ordinary_wire_series_insertion"
    model.add_node(replace(original,id=new_id,name=original.name+" — после аппарата",extensions=extensions))
    first_node,second_node=(original.id,new_id) if partition.keep_first else (new_id,original.id)
    moved=partition.second_vertices if partition.keep_first else partition.first_vertices
    for cid,connection in tuple(model.connections.items()):
        if connection.electrical_node_id==original.id and ("port",connection.port_id) in moved:
            model._connections[cid]=replace(connection,electrical_node_id=new_id)
    for port,node in zip(ports,(first_node,second_node)):
        controller._guard_connection_voltage(model,port.id,node,adopt=True)
        model.connect_port(port.id,node)
    errors=[i.message for i in model.validate_integrity() if i.severity=="error"]
    if errors:
        raise DomainInvariantError("Вставка нарушает целостность модели: "+"; ".join(errors))
    return original,new_id,first_node,second_node,moved


def preview_inline(controller, route_id, x, y, *, type_id="builtin.circuit_breaker", name="QF",
                   representation_id=None, page_id=None, **kwargs):
    try:
        inputs=current_inputs(controller)
        memo=getattr(controller,"_inline_partition",None)
        if memo is None or memo[:3]!=(inputs,controller.diagram,route_id):
            memo=(inputs,controller.diagram,route_id,partition_wire(controller.model,controller.diagram,route_id))
            controller._inline_partition=memo
        partition=memo[3]
        selected = controller.diagram.routes[route_id]
        if page_id is not None and str(page_id) != str(selected.page_id):
            raise ValueError("Провод находится на другой странице.")
        fraction, index = _point(selected, x, y)
        # Resolve the type once before choosing a real semantic terminal angle.
        base = preview_attachment(controller, type_id, name, x=x, y=y,
            representation_id=representation_id, page_id=selected.page_id, **kwargs)
        if not base.valid:
            return replace(base, action="inline")
        payload = base._payload
        definition = controller.model.equipment_type(payload.equipment.type_id, payload.equipment.type_version)
        registry = EquipmentPlacementRegistry.for_model((definition,))
        if (registry.resolve(definition.id, definition.schema_version) is not EquipmentPlacementKind.INLINE_SERIES
                or "switch.position" not in definition.capabilities or len(payload.ports) != 2):
            raise ValueError("Тип аппарата не поддерживает последовательную вставку двух выводов.")
        first_port, second_port = payload.ports
        direction_peer = selected.waypoints[index]
        angle = quarter_turn_for_port_toward_point(definition, first_port.role,
            equipment_x=x, equipment_y=y, target_x=direction_peer.x, target_y=direction_peer.y)
        options = dict(kwargs); options["rotation_deg"] = angle
        base = preview_attachment(controller, type_id, name, x=x, y=y,
            representation_id=representation_id, page_id=selected.page_id, **options)
        if not base.valid or base.adjusted:
            raise ValueError("В выбранной точке провода недостаточно свободного места для аппарата.")
        payload = base._payload
        # Use the same voltage adoption rules on a detached copy. Cache by
        # immutable model/spec/node so ordinary cursor movement does not clone.
        key = (payload.inputs, payload.equipment, partition)
        if getattr(controller, "_inline_voltage", None) != key:
            copy = controller.model._transaction_copy()
            if not payload.existing:
                copy.add_equipment_instance(payload.equipment, payload.ports)
            _split_electrical(controller,copy,payload.ports,partition)
            controller._inline_voltage = key
        cut_ids = tuple(r.id for r in partition.cut_routes)
        reps_by_id = dict(partition.vertex_by_representation)
        def vertex(anchor):
            return ("port", anchor.target_port_id) if anchor.target_port_id else reps_by_id[anchor.representation_id]
        views = []
        for old in partition.cut_routes:
            # Normalise duplicate pictures to the selected semantic direction.
            if vertex(old.start_anchor) not in partition.first_vertices:
                old = replace(old, start_anchor=old.end_anchor, end_anchor=old.start_anchor,
                              waypoints=tuple(reversed(old.waypoints)))
            px, py, segment = (x, y, index) if old.id == selected.id else _fraction_point(old, fraction)
            turn = quarter_turn_for_port_toward_point(definition, first_port.role,
                equipment_x=px, equipment_y=py,
                target_x=old.waypoints[segment].x, target_y=old.waypoints[segment].y)
            row = replace(payload.representation, x=px, y=py, rotation_deg=float(turn), page_id=old.page_id,
                id=payload.representation.id if old.id==selected.id else GraphicalRepresentationId.new())
            # Every additional sheet must have room too; never move its neighbours.
            from .collision import geometry_for_equipment_preview
            candidate = geometry_for_equipment_preview(definition, page_id=old.page_id,
                x=px,y=py,rotation_deg=turn,width=base.width,height=base.height)
            collision = controller._attachment_collision[2].check_placement(candidate,
                ignored_representation_ids=(representation_id,) if representation_id else ())
            if not collision.allowed:
                raise ValueError("На одном из изображений провода нет места для аппарата: " + collision.message)
            ports = rotated_port_layout(payload.equipment, definition, width=base.width,
                height=base.height, rotation=turn, center_x=px, center_y=py)
            first_geom = next(p for p in ports if p.port_id == first_port.id)
            second_geom = next(p for p in ports if p.port_id == second_port.id)
            if first_geom.direction == second_geom.direction:
                raise ValueError("Графические выводы аппарата не образуют две противоположные стороны.")
            draft = ProjectDraft(controller.model, controller.diagram, controller._project.catalog_snapshots)
            obstacles = controller._page_routing_obstacles(draft, old.page_id)
            if representation_id is not None:
                existing_row = controller.diagram.representations[representation_id]
                old_obstacles = controller._page_routing_obstacles(ProjectDraft(controller.model,
                    replace(controller.diagram,representations={existing_row.id:existing_row},routes={}),controller._project.catalog_snapshots),old.page_id)
                obstacles = tuple(o for o in obstacles if o not in old_obstacles)
            w,h = (base.height,base.width) if int(turn)%180 else (base.width,base.height)
            obstacles += (RoutingObstacle(px-w/2,py-h/2,px+w/2,py+h/2),)
            occupied = occupied_segments(controller.diagram,old.page_id,cut_ids)
            start, sd = controller._route_endpoint_geometry(draft,old.start_anchor,old.waypoints[0])
            end, ed = controller._route_endpoint_geometry(draft,old.end_anchor,old.waypoints[-1])
            if old.start_anchor.kind is RouteAnchorKind.BUS:
                sd=direction_toward_point(start.x,start.y,px,py)
            if old.end_anchor.kind is RouteAnchorKind.BUS:
                ed=direction_toward_point(end.x,end.y,px,py)
            pinned_left = tuple(RouteVertex(p.x,p.y,RouteVertexSource.USER,p.pinned)
                for p in old.waypoints[1:segment+1] if p.pinned)
            pinned_right = tuple(RouteVertex(p.x,p.y,RouteVertexSource.USER,p.pinned)
                for p in old.waypoints[segment+1:-1] if p.pinned)
            if any(px-w/2 < p.x < px+w/2 and py-h/2 < p.y < py+h/2 for p in (*pinned_left,*pinned_right)):
                raise ValueError("Закреплённый изгиб попадает в тело аппарата. Выберите другую точку.")
            left = build_orthogonal_route(RoutingRequest(start,RouteVertex(first_geom.x,first_geom.y),
                sd,first_geom.direction,manual_vertices=pinned_left,obstacles=obstacles,port_stub=12,occupied_segments=occupied))
            occupied += tuple((a.x,a.y,b.x,b.y) for a,b in zip(left,left[1:]))
            right = build_orthogonal_route(RoutingRequest(RouteVertex(second_geom.x,second_geom.y),end,
                second_geom.direction,ed,manual_vertices=pinned_right,obstacles=obstacles,port_stub=12,occupied_segments=occupied))
            views.append((old,row,left,right))
        shown = next(view for view in views if view[0].id == selected.id)
        plan = InlinePlan(partition,tuple(views))
        return replace(base,action="inline",source_role=first_port.role,
            source_port_id=first_port.id if payload.existing else None,
            target_point=(x,y),wire_points=shown[2],extra_wire_points=shown[3],
            _payload=replace(payload,representation=shown[1],inline=plan))
    except (ValueError,TypeError,KeyError,RuntimeError) as exc:
        return EquipmentPlacementProposal(False,str(exc),"inline",x,y)


def apply_inline(controller, proposal):
    payload, plan = proposal._payload, proposal._payload.inline
    def command(draft):
        model = draft.electrical_model
        equipment,row = place_on_draft(controller,draft,proposal)
        partition = plan.partition
        original,new_id,first_node,second_node,moved=_split_electrical(controller,model,payload.ports,partition)
        reps = dict(draft.diagram.representations)
        rep_vertices = dict(partition.vertex_by_representation)
        for rid,vertex in rep_vertices.items():
            if vertex in moved:
                reps[rid]=replace(reps[rid],electrical_node_id=new_id)
        def remap(anchor):
            if anchor.electrical_node_id != original.id:
                return anchor
            port = anchor.branch_port_id or anchor.target_port_id
            vertex=("port",port) if port is not None else rep_vertices[anchor.representation_id]
            return replace(anchor,electrical_node_id=new_id) if vertex in moved else anchor
        routes={}
        cut_ids={r.id for r in partition.cut_routes}
        for rid,old in draft.diagram.routes.items():
            if rid in cut_ids:
                continue
            start,end=remap(old.start_anchor),remap(old.end_anchor)
            routes[rid]=replace(old,start_anchor=start,end_anchor=end,
                electrical_node_id=start.electrical_node_id if old.kind is DiagramRouteKind.NODE_CONNECTION else old.electrical_node_id)
        route_ids=[]
        for old,representation,left,right in plan.views:
            reps[representation.id]=representation
            anchors=tuple(RouteEndpointAnchor(RouteAnchorKind.EQUIPMENT_PORT,representation.id,node,
                target_port_id=port.id,anchor_key=port.role) for port,node in zip(payload.ports,(first_node,second_node)))
            def points(vertices,first_id,last_id):
                by_xy={(p.x,p.y):p for p in old.waypoints[1:-1]}
                return tuple(replace(by_xy[(p.x,p.y)], x=p.x,y=p.y) if 0<i<len(vertices)-1 and (p.x,p.y) in by_xy
                             else RouteWaypoint(first_id if i==0 else last_id if i==len(vertices)-1 else RouteWaypointId.new(),p.x,p.y)
                             for i,p in enumerate(vertices))
            a=replace(old,start_anchor=remap(old.start_anchor),end_anchor=anchors[0],electrical_node_id=first_node,
                waypoints=points(left,old.waypoints[0].id,RouteWaypointId.new()))
            b=replace(old,id=DiagramRouteId.new(),start_anchor=anchors[1],end_anchor=remap(old.end_anchor),electrical_node_id=second_node,
                waypoints=points(right,RouteWaypointId.new(),old.waypoints[-1].id))
            routes[a.id]=a;routes[b.id]=b;route_ids.extend((a.id,b.id))
        draft.diagram=replace(draft.diagram,representations=reps,routes=routes)
        return EquipmentPlacementEditResult(equipment.id,row.id,equipment.port_ids,tuple(route_ids),(first_node,second_node))
    return controller._execute("Вставить аппарат в разрыв обычного провода",command)
