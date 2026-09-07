"""Read-only placement proposals and one-command ordinary-wire attachments.

The semantic preview copy is cached only while all immutable model stores
match. Cursor coordinates never enter that cache; committed geometry does.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from rza_calc.domain.diagram import (
    DiagramRoute, DiagramRouteId, DiagramRouteKind, GraphicalRepresentation,
    GraphicalRepresentationId, PageId, RepresentationTargetKind, RouteAnchorKind,
    RouteEndpointAnchor, RouteWaypoint, RouteWaypointId,
)
from rza_calc.domain.electrical import (
    EquipmentId, EquipmentInstance, EquipmentTypeId, PortId, SwitchPosition,
)
from rza_calc.domain.history import ElectricalModelMemento
from .bus_contacts import allocate_bus_contact
from .collision import DiagramCollisionService, geometry_for_equipment_preview
from .history import ProjectDraft
from .orientation import (
    OrientationMode, direction_toward_point, normalize_orientation_mode,
    normalize_quarter_turn, rotated_port_layout,
)
from .orthogonal_routing import RouteDirection, RouteVertex, RoutingError, RoutingObstacle, RoutingRequest, build_orthogonal_route
from .state import RepresentationGraphics, representation_with_graphics


@dataclass(frozen=True, slots=True)
class EquipmentPlacementProposal:
    valid: bool
    reason: str
    action: str
    x: float
    y: float
    rotation_deg: float = 0
    width: float = 80
    height: float = 50
    source_role: str | None = None
    source_port_id: PortId | None = None
    target: object = None
    target_point: tuple[float, float] | None = None
    wire_points: tuple[RouteVertex, ...] = ()
    extra_wire_points: tuple[RouteVertex, ...] = ()
    adjusted: bool = False
    _payload: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class EquipmentPlacementEditResult:
    equipment_id: EquipmentId
    representation_id: GraphicalRepresentationId
    port_ids: tuple[PortId, ...]
    route_ids: tuple[DiagramRouteId, ...] = ()
    node_ids: tuple = ()


@dataclass(frozen=True, slots=True)
class PlacementPayload:
    controller: object
    inputs: ElectricalModelMemento
    diagram: object
    equipment: EquipmentInstance
    ports: tuple
    representation: GraphicalRepresentation
    existing: bool
    inline: object = None


def inputs_match(inputs,model):
    """Compare live immutable records without copying every store per motion.

    Revision alone is insufficient: imported/replaced records can change at
    the same revision. Mapping equality also detects those external changes.
    """
    return (inputs.revision==model.revision and inputs.name==model.name and
        all(getattr(inputs,name)==getattr(model,name) for name in (
            "neutral","extensions","voltage_classes","equipment_types","equipment",
            "ports","electrical_nodes","connections","operating_states","logical_lines","line_sections")))


def current_inputs(controller):
    old=getattr(controller,"_attachment_inputs",None)
    if old is None or not inputs_match(old,controller.model):
        old=ElectricalModelMemento.capture(controller.model)
        controller._attachment_inputs=old
    return old


def page_routing_snapshot(controller,page):
    inputs=current_inputs(controller)
    memo=getattr(controller,"_attachment_routing",None)
    if memo is None or memo[:3]!=(inputs,controller.diagram,page):
        draft=ProjectDraft(controller.model,controller.diagram,controller._project.catalog_snapshots)
        memo=(inputs,controller.diagram,page,controller._page_routing_obstacles(draft,page),
              occupied_segments(controller.diagram,page))
        controller._attachment_routing=memo
    return memo[3:]


def occupied_segments(diagram, page_id, exclude=()):
    excluded = frozenset(exclude)
    return tuple((a.x, a.y, b.x, b.y)
                 for route in diagram.routes.values()
                 if route.page_id == page_id and route.id not in excluded
                 for a, b in zip(route.waypoints, route.waypoints[1:])
                 if (a.x, a.y) != (b.x, b.y))


def _page(controller, page_id):
    selected = PageId(page_id) if isinstance(page_id, str) else page_id
    selected = selected or controller.workspace_state.active_page_id
    if isinstance(selected, str):
        selected = PageId(selected)
    if selected is None:
        selected = next(iter(controller.diagram.pages), None)
    if selected not in controller.diagram.pages:
        raise ValueError("Страница для установки аппарата не найдена.")
    return selected


def _free(model, equipment):
    if any(model.node_for_port(port) is not None for port in equipment.port_ids):
        raise ValueError("Автоподключение доступно только полностью свободному аппарату.")


def _semantic(controller, *, type_id, type_version, name, properties,
              voltage_class_by_group, normal_position, representation_id,
              port_role, target, inputs):
    from .controller import NodeTarget, PortTarget
    model = controller.model
    definition = model.equipment_type(EquipmentTypeId(str(type_id)), type_version)
    existing = representation_id is not None
    if existing:
        row = controller.diagram.representations.get(representation_id)
        if row is None or row.equipment_id is None:
            raise ValueError("Представление аппарата не найдено.")
        spec = model.equipment[row.equipment_id]
        definition = model.equipment_type(spec.type_id, spec.type_version)
        _free(model, spec)
    else:
        if normal_position is None and "switch.position" in definition.capabilities:
            normal_position = SwitchPosition(definition.extensions.get("default_normal_position", SwitchPosition.CLOSED.value))
        roles = tuple(p.role for p in definition.port_definitions if p.required)
        spec = EquipmentInstance(EquipmentId("editor.preview.equipment"), definition.id,
            type_version, name, tuple(PortId("editor.preview." + role) for role in roles),
            properties or {}, voltage_class_by_group or {}, normal_position,
            extensions={"stage3_editor": {"draft": True}})
    endpoint = ("port", target.port_id) if isinstance(target, PortTarget) else (
        ("node", target.node_id) if isinstance(target, NodeTarget) else None)
    key = (spec, existing, port_role, endpoint)
    memo = getattr(controller, "_attachment_semantic", None)
    if memo is not None and memo[0] == inputs and memo[1] == key:
        return memo[2:]
    copy = model._transaction_copy()
    if existing:
        equipment = copy.equipment[spec.id]
    else:
        equipment, _ = copy.create_equipment(spec.type_id, spec.name,
            type_version=spec.type_version, properties=spec.properties,
            voltage_class_by_group=spec.voltage_class_by_group,
            normal_position=spec.normal_position, extensions=spec.extensions)
    ports = tuple(copy.ports[p] for p in equipment.port_ids)
    source = next((p for p in ports if p.role == port_role), None)
    if target is not None:
        if source is None:
            if len(ports) != 1:
                raise ValueError("Выберите смысловой вывод подключаемого аппарата.")
            source = ports[0]
        if isinstance(target, PortTarget) and target.port_id in equipment.port_ids:
            raise ValueError("Нельзя соединить аппарат с собственным выводом.")
        controller._guard_connection_voltage(copy, source.id, target, adopt=True)
        # Also exercise canonical kind/capacity checks, once per semantic input.
        if isinstance(target, PortTarget):
            copy.connect_ports(source.id, target.port_id)
        else:
            copy.connect_port(source.id, target.node_id)
    result = (equipment, ports, definition, source)
    controller._attachment_semantic = (inputs, key, *result)
    return result


def _target_geometry(controller, target, page_id, source_xy, *, obstacles=()):
    from .controller import NodeTarget, PortTarget
    diagram, model = controller.diagram, controller.model
    row = diagram.representations.get(target.representation_id)
    if row is None or row.page_id != page_id:
        raise ValueError("Для предварительного подключения нужна видимая цель на этом листе.")
    if isinstance(target, PortTarget):
        port = model.ports.get(target.port_id)
        if port is None or row.equipment_id != port.equipment_id:
            raise ValueError("Графическая цель не соответствует выводу аппарата.")
        point = controller._port_anchor_geometry(model, row, port.id)
        return target, (point.x, point.y), point.direction
    if row.electrical_node_id != target.node_id:
        raise ValueError("Графическая цель не соответствует электрическому узлу.")
    if target.route_id is not None:
        # Point validation/splitting happens on a diagram-only temporary draft.
        draft = ProjectDraft(model, diagram, controller._project.catalog_snapshots)
        controller._resolve_conductor_target(draft, target, page_id)
        xy = (target.x, target.y)
    elif "busbar" in row.symbol_key:
        graphics = RepresentationGraphics.from_representation(row)
        requested=float(target.anchor_key or .5)
        length=max(graphics.width,graphics.height)
        fractions=[requested]
        # Contact separation alone is insufficient when a nearby apparatus's
        # body padding occupies that bus point. Look for the nearest visible
        # contact outside those same routing obstacles; never move the body.
        for distance in range(10,int(math.ceil(length))+10,10):
            fractions.extend((requested-distance/length,requested+distance/length))
        seen=set()
        for fraction in fractions:
            if not 0<=fraction<=1:
                continue
            contact=allocate_bus_contact(diagram,row,width=graphics.width,height=graphics.height,
                requested=fraction,merge_tolerance=20)
            if contact.fraction in seen:
                continue
            seen.add(contact.fraction)
            if not any(body.inflated(12).contains((contact.x,contact.y)) for body in obstacles):
                break
        else:
            raise ValueError("На шине нет свободной точки с допустимым подводом. Удлините шину.")
        target = replace(target, anchor_key=str(contact.fraction), x=contact.x, y=contact.y)
        xy = (contact.x, contact.y)
        # A conductor leaves a bus perpendicularly on the approaching side.
        horizontal = graphics.width >= graphics.height
        if int(row.rotation_deg) % 180:
            horizontal = not horizontal
        direction = (RouteDirection.UP if source_xy[1] < xy[1] else RouteDirection.DOWN) if horizontal else (
            RouteDirection.LEFT if source_xy[0] < xy[0] else RouteDirection.RIGHT)
        return target, xy, direction
    else:
        xy = (row.x, row.y)
    return target, xy, direction_toward_point(*xy, *source_xy)


def preview_attachment(controller, type_id, name, target=None, *, x, y,
        port_role=None, page_id=None, representation_id=None, rotation_deg=0,
        width=80, height=50, properties=None, voltage_class_by_group=None,
        normal_position=None, type_version=1, symbol_key=None,
        orientation_mode=OrientationMode.AUTO):
    """Compute geometry without writing the live model, diagram or history."""
    action = "attach" if target is not None else "place"
    try:
        controller._require_edit()
        if not all(math.isfinite(v) for v in (x, y, width, height)) or min(width, height) <= 0:
            raise ValueError("Размеры и координаты должны быть конечными и положительными.")
        # The caller already applied its snap/Alt policy; commit uses this point.
        requested = (x, y)
        x, y = requested
        page = _page(controller, page_id)
        rotation = normalize_quarter_turn(rotation_deg)
        mode = normalize_orientation_mode(orientation_mode)
        inputs = current_inputs(controller)
        equipment, ports, definition, source = _semantic(controller,
            type_id=type_id, type_version=type_version, name=name, properties=properties,
            voltage_class_by_group=voltage_class_by_group, normal_position=normal_position,
            representation_id=representation_id, port_role=port_role, target=target, inputs=inputs)
        existing = representation_id is not None
        if existing:
            old = controller.diagram.representations[representation_id]
            if old.page_id != page:
                raise ValueError("Аппарат находится на другой странице.")
            width, height = controller._equipment_symbol_size(old, definition)
        memo = getattr(controller, "_attachment_collision", None)
        if memo is None or memo[0] != inputs or memo[1] is not controller.diagram:
            memo = (inputs, controller.diagram, DiagramCollisionService(controller.diagram, controller.model))
            controller._attachment_collision = memo
        ignored = (representation_id,) if existing else ()
        # At equal distance prefer moving away from a bus over sliding along
        # it: that keeps the selected contact aligned with the source terminal.
        along_axis = 1  # Preserve the general placement tie-break otherwise.
        if target is not None:
            target_row = controller.diagram.representations.get(target.representation_id)
            if target_row is not None and "busbar" in target_row.symbol_key:
                graphics = RepresentationGraphics.from_representation(target_row)
                horizontal = graphics.width >= graphics.height
                if int(target_row.rotation_deg) % 180:
                    horizontal = not horizontal
                along_axis = 0 if horizontal else 1
        offsets = sorted(((dx, dy) for dx in range(-100, 101, 20) for dy in range(-100, 101, 20)),
                         key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[along_axis]), p[1], p[0]))
        obstacles,occupied = page_routing_snapshot(controller,page) if target is not None else ((),())
        if existing and target is not None:
            old_obstacle = controller._page_routing_obstacles(ProjectDraft(controller.model,
                replace(controller.diagram, representations={old.id: old}, routes={}), controller._project.catalog_snapshots), page)
            obstacles = tuple(o for o in obstacles if o not in old_obstacle)
        original_target = target
        for dx, dy in offsets:
            x,y=requested[0]+dx,requested[1]+dy
            candidate = geometry_for_equipment_preview(definition, page_id=page,
                x=x, y=y, rotation_deg=rotation, width=width, height=height,
                display_name=equipment.name)
            if not memo[2].check_placement(candidate, ignored_representation_ids=ignored).allowed:
                continue
            points,target_xy=(),None
            if original_target is not None:
                geometry=next(p for p in rotated_port_layout(equipment,definition,width=width,
                    height=height,rotation=rotation,center_x=x,center_y=y) if p.port_id==source.id)
                target,target_xy,direction=_target_geometry(controller,original_target,page,(geometry.x,geometry.y),obstacles=obstacles)
                rw,rh=(height,width) if int(rotation)%180 else (width,height)
                local_obstacles=(*obstacles,RoutingObstacle(x-rw/2,y-rh/2,x+rw/2,y+rh/2))
                try:
                    points=build_orthogonal_route(RoutingRequest(RouteVertex(geometry.x,geometry.y),
                        RouteVertex(*target_xy),geometry.direction,direction,obstacles=local_obstacles,
                        port_stub=12,occupied_segments=occupied))
                except RoutingError:
                    # A free body rectangle is insufficient if its terminal
                    # would force a wire through that body. Propose the nearest
                    # position satisfying BOTH tests, under the same guard.
                    continue
            break
        else:
            raise ValueError("Рядом нет свободного положения с допустимым подводом провода.")
        row = replace(old, x=x, y=y, rotation_deg=float(rotation)) if existing else representation_with_graphics(
            GraphicalRepresentation(GraphicalRepresentationId.new(), page,
                RepresentationTargetKind.EQUIPMENT, equipment_id=equipment.id, x=x, y=y,
                rotation_deg=float(rotation), label=equipment.name,
                symbol_key=symbol_key if symbol_key is not None else str(definition.extensions.get("diagram_symbol_key", ""))),
            RepresentationGraphics(width=width, height=height, orientation_mode=mode, label_manual=False))
        payload = PlacementPayload(controller, inputs, controller.diagram, equipment, ports, row, existing)
        return EquipmentPlacementProposal(True, "Размещение допустимо.", action, x, y,
            float(rotation), width, height, source.role if source else None,
            source.id if source and existing else None, target, target_xy, points,
            adjusted=(x, y) != requested, _payload=payload)
    except (ValueError, TypeError, KeyError, RuntimeError) as exc:
        return EquipmentPlacementProposal(False, str(exc), action, x, y, rotation_deg, width, height)


def place_on_draft(controller, draft, proposal):
    payload = proposal._payload
    equipment, row = payload.equipment, payload.representation
    if payload.existing:
        _free(draft.electrical_model, draft.electrical_model.equipment[equipment.id])
    else:
        draft.electrical_model.add_equipment_instance(equipment, payload.ports)
    draft.diagram = replace(draft.diagram, representations={**draft.diagram.representations, row.id: row})
    return equipment, row


def apply_placement(controller, proposal):
    from .controller import EditorCommandError
    controller._require_edit()
    payload = proposal._payload
    if not proposal.valid or payload is None or payload.controller is not controller:
        raise EditorCommandError(proposal.reason or "Нет допустимого предложения установки.")
    if not inputs_match(payload.inputs,controller.model) or payload.diagram != controller.diagram:
        raise EditorCommandError("Схема изменилась после предварительного просмотра. Повторите установку.")
    if payload.inline is not None:
        from .ordinary_wire_insertion import apply_inline
        return apply_inline(controller, proposal)
    def command(draft):
        equipment, row = place_on_draft(controller, draft, proposal)
        if proposal.target is None:
            return EquipmentPlacementEditResult(equipment.id, row.id, equipment.port_ids)
        source = next(p for p in payload.ports if p.role == proposal.source_role)
        controller._guard_connection_voltage(draft.electrical_model, source.id, proposal.target, adopt=True)
        resolved = controller._resolve_connection_target(draft, proposal.target, row.page_id)
        draft.electrical_model.connect_port(source.id, resolved.node_id)
        route = DiagramRoute(DiagramRouteId.new(), row.page_id, DiagramRouteKind.NODE_CONNECTION,
            RouteEndpointAnchor(RouteAnchorKind.EQUIPMENT_PORT, row.id, resolved.node_id,
                                target_port_id=source.id, anchor_key=source.role),
            RouteEndpointAnchor(resolved.anchor_kind, resolved.representation_id, resolved.node_id,
                                target_port_id=resolved.target_port_id, anchor_key=resolved.anchor_key),
            electrical_node_id=resolved.node_id,
            waypoints=tuple(RouteWaypoint(RouteWaypointId.new(), p.x, p.y) for p in proposal.wire_points))
        controller._add_route(draft, route)
        return EquipmentPlacementEditResult(equipment.id, row.id, equipment.port_ids, (route.id,), (resolved.node_id,))
    return controller._execute("Установить аппарат и подключить провод", command)
