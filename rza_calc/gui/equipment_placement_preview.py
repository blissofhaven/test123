"""Read-only scene targeting; electrical decisions belong to controller proposals."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from ..domain.diagram import DiagramRouteKind
from ..editor.controller import NodeTarget, PortTarget
from ..editor.placement import EquipmentPlacementKind


def ordinary_route_at(scene, point):
    tolerance = 12.0 / scene._view_scale()
    candidates = []
    for item in scene.items(QRectF(point.x() - tolerance, point.y() - tolerance,
                                   tolerance * 2, tolerance * 2)):
        route = getattr(item, "route", None)
        if route is None or route.kind is not DiagramRouteKind.NODE_CONNECTION:
            continue
        projected = scene._project_to_route(route, point)
        if projected is not None:
            distance = (projected[0] - point).manhattanLength()
            if distance <= tolerance:
                candidates.append((distance, route.id.value, route, projected[0]))
    return min(candidates, default=None, key=lambda row: row[:2])


def attachment_candidates(scene, geometry, excluded_representation=None):
    """Rank visible terminal/node anchors without inferring connections by touch."""
    tolerance = 12.0 / scene._view_scale()
    candidates = []
    for zone in geometry.port_connection_zones:
        point = QPointF(zone.shape.center_x, zone.shape.center_y)
        for item in scene._items_by_id.values():
            if item.representation_id == excluded_representation:
                continue
            if item.representation.electrical_node_id is not None:
                if item.shape().isEmpty():
                    continue
                projected, _, fraction = scene._project_to_node_item(item, point)
                distance = (point - projected).manhattanLength()
                if distance <= tolerance:
                    target = NodeTarget(item.representation.electrical_node_id,
                                        item.representation_id, fraction)
                    candidates.append((distance, zone.role, item.representation_id.value, target))
            elif item._canonical_key not in {"line", "line_section"}:
                for port in item._port_items.values():
                    if not port.interaction_enabled():
                        continue
                    distance = (point - port.scenePos()).manhattanLength()
                    if distance <= tolerance:
                        target = PortTarget(port.port_id, item.representation_id)
                        candidates.append((distance, zone.role, port.port_id.value, target))
    # An existing terminal can occupy the exact bus contact under the cursor.
    # Use the visible bus allocator for that same electrical node, so the next
    # attachment receives its own contact instead of stacking onto the terminal.
    node_targets = {(role, target.node_id) for _, role, _, target in candidates
                    if isinstance(target, NodeTarget)}
    def redundant_port(row):
        _, role, _, target = row
        if not isinstance(target, PortTarget):
            return False
        node = scene._model.node_for_port(target.port_id)
        return node is not None and (role, node.id) in node_targets
    return sorted((row for row in candidates if not redundant_port(row)), key=lambda row: row[:3])


def proposal_paths(proposal):
    extra = getattr(proposal, "extra_wire_points", ())
    if extra and hasattr(extra[0], "x"):
        extra = (extra,)
    rows = (getattr(proposal, "wire_points", ()), *extra)
    return tuple(tuple(row) for row in rows if len(row) > 1)


def paint_proposal_wires(painter, proposal, scale):
    if proposal is None or not proposal.valid or proposal.action not in {"attach", "inline"}:
        return
    painter.save()
    color = QColor("#16A34A")
    pen = QPen(color, 1.8)
    pen.setCosmetic(True)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    paths = proposal_paths(proposal)
    for points in paths:
        path = QPainterPath(QPointF(points[0].x, points[0].y))
        for point in points[1:]:
            path.lineTo(point.x, point.y)
        painter.drawPath(path)
    if proposal.action == "inline":
        dots = [(point.x, point.y) for points in paths for point in (points[0], points[-1])]
    else:
        dots = (proposal.target_point,) if proposal.target_point is not None else ()
    painter.setBrush(color)
    for x, y in dots:
        painter.drawEllipse(QPointF(x, y), 3.5 / scale, 3.5 / scale)
    painter.restore()


class PlacementWirePreview(QGraphicsObject):
    def __init__(self):
        super().__init__()
        self.proposal = None
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setZValue(60)
        self.hide()

    def set_proposal(self, proposal):
        self.prepareGeometryChange()
        self.proposal = proposal
        self.setVisible(proposal is not None and proposal.valid and bool(proposal_paths(proposal)))
        self.update()

    def boundingRect(self):
        points = [point for row in proposal_paths(self.proposal) for point in row] if self.proposal else []
        if not points:
            return QRectF()
        xs, ys = [p.x for p in points], [p.y for p in points]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)).adjusted(-20, -20, 20, 20)

    def shape(self):
        return QPainterPath()  # Feedback must never capture a target hit.

    def paint(self, painter, option, widget=None):
        del option, widget
        paint_proposal_wires(painter, self.proposal, self.scene()._view_scale())


def equipment_proposal(canvas, payload, point, *, representation_id=None):
    """Prepare one request; no project data or neighbouring items are changed."""
    view, scene, controller = canvas.view, canvas.scene, canvas.controller
    preview = getattr(controller, "preview_equipment_attachment", None)
    if not callable(preview):
        return None
    definition = view._preview_definition(payload)
    if definition is None:
        return None
    graphics = payload.get("graphics", {})
    width, height = view._placement_size(payload)
    rotation = graphics.get("rotation_deg", view._placement_rotation_deg)
    kwargs = dict(x=point.x(), y=point.y(), page_id=canvas.page_id,
                  representation_id=representation_id, rotation_deg=rotation,
                  width=width, height=height, type_version=definition.schema_version,
                  symbol_key=payload.get("symbol_key") or None,
                  orientation_mode=graphics.get("orientation_mode", "auto"))
    name = payload.get("name") or definition.display_name
    for field in ("properties", "voltage_class_by_group", "normal_position"):
        if field in payload:
            kwargs[field] = payload[field]
    placement = view._placement_for_payload(payload)
    route_hit = ordinary_route_at(scene, point)
    if placement is EquipmentPlacementKind.INLINE_SERIES and route_hit is not None:
        _, _, route, projected = route_hit
        inline = getattr(controller, "preview_inline_equipment", None)
        if callable(inline):
            return inline(route.id, projected.x(), projected.y(), type_id=definition.id,
                          name=name, **{key: value for key, value in kwargs.items() if key not in {"x", "y"}})
    geometry = view._equipment_preview_geometry(payload, point, rotation_deg=rotation)
    candidates = attachment_candidates(scene, geometry, representation_id) if geometry is not None else ()
    role, target = (candidates[0][1], candidates[0][3]) if candidates else (None, None)
    return preview(definition.id, name, target, port_role=role, **kwargs)
