# -*- coding: utf-8 -*-
"""QGraphicsScene-холст редактора электрических соединений.

Модуль отображает :class:`~rza_calc.domain.diagram.DiagramDocument`, но не
определяет электрическую связность.  Изменения передаются единому
``ProjectEditorController`` только после завершения пользовательского жеста.
Таким образом перемещение не пересобирает топологию на каждом пикселе.

Электрическая мутация выполняется только контроллером после завершения жеста;
предварительные трассы и подсветка целей являются временными объектами сцены.
"""
from __future__ import annotations

import json
import inspect
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
    QShortcut,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneContextMenuEvent,
    QGraphicsSceneMouseEvent,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..domain.diagram import (
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    DiagramDocument,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RouteAnchorKind,
    RouteWaypoint,
    RouteWaypointId,
    RouteWaypointSource,
)
from ..domain.electrical import (
    DataConfirmation,
    DomainInvariantError,
    ElectricalNodeId,
    ElectricalModel,
    EquipmentAvailability,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeId,
    LineKind,
    OperatingStateId,
    PortId,
    SwitchPosition,
)
from ..editor.connection_tool import (
    ConnectionDraft,
    ConnectionTarget,
    ConnectionTargetFeedback,
    ConnectionTargetKind,
    ConnectionToolMode,
    ConnectionToolState,
    PhysicalLineDraft,
    PhysicalLineToolState,
)
from ..editor.connection_voltage import check_connection_voltage, endpoint_voltage
from ..editor.bus_connections import available_bus_fraction
from ..editor.collision import (
    DEFAULT_SAFE_GAP,
    DiagramCollisionService,
    geometry_for_equipment_preview,
)
from ..editor.labels import LabelContent, present_label, present_route_label
from .label_layout import MIN_LABEL_ZOOM, layout_labels, wrap_text, wrapped_label_text
from ..editor.orthogonal_routing import (
    build_orthogonal_route,
    normalize_route,
    RouteDirection,
    RouteVertex,
    RouteVertexSource,
    RoutingObstacle,
    RoutingError,
    RoutingRequest,
)
from ..editor.placement import (
    EquipmentPlacementKind,
    EquipmentPlacementRegistry,
)
from ..editor.orientation import (
    OrientationMode,
    base_port_layout,
    bus_anchor_geometry,
    bus_anchor_toward_point,
    closest_directed_segment,
    normalize_orientation_mode,
    quarter_turn_for_port_toward_point,
    editor_rotation,
    nearest_editor_rotation,
    next_editor_rotation,
)
from ..editor.line_bridges import BridgeWire, build_wire_displays
from ..editor.state import EditorMode
from ..editor.symbols import (
    DIAGRAM_DEENERGIZED_STROKE,
    DIAGRAM_NEUTRAL_STROKE,
    DIAGRAM_OUT_OF_SERVICE_STROKE,
    DiagramColorMode,
    SymbolGeometry,
    SymbolPrimitive,
    canonical_key,
    default_size as symbol_default_size,
    symbol_for,
    switch_state_fill,
    voltage_stroke,
)
from ..editor.tool_state import EditorTool, EditorToolStateMachine
from ..topology import (
    Energization,
    TopologyEngine,
    TopologyError,
    TopologyQueryError,
    TopologySnapshot,
    VoltageStatus,
)
from .strings import ui_text
from .route_bridges import visible_line_direction_marker, wire_display_path
from .rotation_handle import RotationHandleItem
from .theme import COLORS


EQUIPMENT_MIME_TYPE = "application/x-rza-equipment-type"
SCENE_LIMIT = 1_000_000.0
MIN_ZOOM = 0.05
MAX_ZOOM = 20.0
PORT_HIT_TOLERANCE_PX = 12.0
# Bus picking includes the visible band plus this screen-space margin. Ports
# and hidden nodes retain the existing 12-pixel radial acceptance test.
BUS_HIT_MARGIN_PX = 12.0
ROUTE_HIT_TOLERANCE_PX = 9.0


@dataclass(frozen=True, slots=True)
class DiagramRenderContext:
    """Immutable, read-only electrical state used by scene paint items."""

    snapshot: TopologySnapshot | None = None
    color_mode: DiagramColorMode = DiagramColorMode.COLOR
    state_available: bool = True


def _resolved_nominal_voltage(
    model: ElectricalModel,
    context: DiagramRenderContext,
    object_id: ElectricalNodeId | PortId,
) -> int | None:
    """Resolve one exact nominal voltage; unknown/conflicting zones stay neutral."""

    snapshot = context.snapshot
    if snapshot is None:
        return None
    try:
        resolution = snapshot.voltage_zone_of(object_id).resolution
    except TopologyQueryError:
        return None
    if (
        resolution.status is not VoltageStatus.RESOLVED
        or resolution.voltage_class_id is None
    ):
        return None
    voltage_class = model.voltage_classes.get(resolution.voltage_class_id)
    return (
        int(voltage_class.nominal_voltage_v)
        if voltage_class is not None
        else None
    )


def _common_nominal_voltage(
    model: ElectricalModel,
    context: DiagramRenderContext,
    object_ids: Iterable[ElectricalNodeId | PortId],
) -> int | None:
    """Return a voltage only when every supplied terminal resolves identically."""

    keys = tuple(object_ids)
    values = tuple(
        _resolved_nominal_voltage(model, context, object_id)
        for object_id in keys
    )
    if not values or any(value is None for value in values):
        return None
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 else None


def _node_energization(
    context: DiagramRenderContext,
    node_id: ElectricalNodeId,
) -> Energization:
    if not context.state_available or context.snapshot is None:
        return Energization.UNKNOWN
    try:
        return context.snapshot.energization_of(node_id)
    except TopologyQueryError:
        return Energization.UNKNOWN


def _equipment_availability(
    context: DiagramRenderContext,
    equipment_id: EquipmentId | None,
) -> EquipmentAvailability | None:
    if (
        equipment_id is None
        or not context.state_available
        or context.snapshot is None
    ):
        return None
    return context.snapshot.operating_state.availability.get(equipment_id)


def _equipment_disconnected(
    context: DiagramRenderContext,
    equipment_id: EquipmentId | None,
) -> bool:
    """Return the resolved OPEN state without inferring it from paint geometry."""

    if (
        equipment_id is None
        or not context.state_available
        or context.snapshot is None
    ):
        return False
    resolved = context.snapshot.operating_state.positions.get(equipment_id)
    return resolved is not None and resolved.position is SwitchPosition.OPEN


def _aggregate_energization(values: Iterable[Energization]) -> Energization:
    states = tuple(values)
    if Energization.ENERGIZED in states:
        return Energization.ENERGIZED
    if states and all(item is Energization.DEENERGIZED for item in states):
        return Energization.DEENERGIZED
    return Energization.UNKNOWN


def _state_stroke(
    nominal_voltage_v: int | None,
    context: DiagramRenderContext,
    *,
    energization: Energization = Energization.UNKNOWN,
    availability: EquipmentAvailability | None = None,
    disconnected: bool = False,
) -> str:
    if availability is EquipmentAvailability.OUT_OF_SERVICE:
        return DIAGRAM_OUT_OF_SERVICE_STROKE
    if disconnected or energization is Energization.DEENERGIZED:
        return DIAGRAM_DEENERGIZED_STROKE
    return voltage_stroke(nominal_voltage_v, color_mode=context.color_mode)


class CanvasMode(StrEnum):
    EDIT = "edit"
    ANALYSIS = "analysis"


class HitTestKind(StrEnum):
    HANDLE = "handle"
    PORT = "port"
    BODY = "body"
    LABEL = "label"
    PHYSICAL_LINE = "physical_line"
    GRAPHICAL_CONNECTION = "graphical_connection"
    CANVAS = "canvas"


@dataclass(frozen=True, slots=True)
class SceneHitTarget:
    kind: HitTestKind
    item: QGraphicsItem | None = None
    via_route: QGraphicsItem | None = None


@dataclass(frozen=True, slots=True)
class CanvasViewportState:
    zoom: float
    center_x: float
    center_y: float


@dataclass(frozen=True, slots=True)
class PendingTapBranch:
    section_id: EquipmentId
    offset_mm: int | None
    physical: object
    tap_x: float
    tap_y: float


@dataclass
class ObjectRotationGesture:
    representation_id: GraphicalRepresentationId
    original_angle: float
    current_angle: float
    center: QPointF
    start_point: QPointF
    start_pointer_angle: float
    dragged: bool = False
    blocked: bool = False


@dataclass
class ConnectedDragGesture:
    kind: str
    route_id: DiagramRouteId
    start: QPointF
    grabber: QGraphicsItem
    segment_index: int = 0
    at_start: bool = False
    dx: float = 0.0
    dy: float = 0.0
    fraction: float | None = None
    preview_routes: tuple[DiagramRoute, ...] = ()
    preview_input: tuple[object, ...] | None = None
    constraints_key: tuple[object, ...] | None = None
    constraints: tuple = ((), ())


class DiagramLabelItem(QGraphicsSimpleTextItem):
    """Перемещаемая подпись, остающаяся частью представления объекта."""

    def __init__(self, parent: "DiagramObjectItem"):
        super().__init__(parent)
        self._start_position = QPointF()
        self._start_scene_position = QPointF()
        self.setFont(QFont("Segoe UI", 9))
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def set_editable(self, enabled: bool) -> None:
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, enabled)
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton if enabled else Qt.MouseButton.NoButton
        )

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        parent = self.parentItem()
        if isinstance(parent, DiagramObjectItem):
            if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                parent.scene().clearSelection()
            parent.setSelected(True)
        self._start_position = QPointF(self.pos())
        self._start_scene_position = QPointF(event.scenePos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        parent = self.parentItem()
        if parent is not None and self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable:
            # Qt's default drag moves the selected ancestor instead of its
            # text child. A label drag must never move the apparatus or wires.
            delta = parent.mapFromScene(event.scenePos()) - parent.mapFromScene(self._start_scene_position)
            self.setPos(self._start_position + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if self.pos() == self._start_position:
            return
        parent = self.parentItem()
        scene = self.scene()
        if isinstance(parent, DiagramObjectItem) and isinstance(scene, DiagramGraphicsScene):
            scene.labelMoveRequested.emit(
                parent.representation_id, self.pos().x(), self.pos().y()
            )


class DiagramRouteLabelItem(DiagramLabelItem):
    """Physical-line annotation: offsets are relative to the route midpoint."""

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        parent = self.parentItem()
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            parent.scene().clearSelection()
        parent.setSelected(True)
        self._start_position = QPointF(self.pos())
        self._start_scene_position = QPointF(event.scenePos())
        QGraphicsSimpleTextItem.mousePressEvent(self, event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        QGraphicsSimpleTextItem.mouseReleaseEvent(self, event)
        if self.pos() != self._start_position:
            parent = self.parentItem()
            offset = self.pos() - parent._label_anchor
            self.scene().routeLabelMoveRequested.emit(parent.route_id, offset.x(), offset.y())


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_directional_line(model: ElectricalModel, equipment_id: EquipmentId | None) -> bool:
    """A line-shaped symbol alone is not proof of a physical VL/KL branch."""
    if equipment_id is None or equipment_id not in model.equipment:
        return False
    section = model.line_sections.get(equipment_id)
    if section is not None:
        line = model.logical_lines.get(section.logical_line_id)
        return line is not None and line.line_kind in {LineKind.OVERHEAD, LineKind.CABLE}
    equipment = model.equipment[equipment_id]
    definition = model.equipment_type(equipment.type_id, equipment.type_version)
    if definition.behavior_key == "line":
        # The existing builtin line type explicitly denotes an overhead line;
        # its physical data may still be incomplete, which is not a UI guess.
        return True
    if definition.behavior_key != "legacy.line":
        return False
    legacy = _mapping(equipment.extensions.get("legacy_calculation"))
    properties = model.effective_equipment_properties(equipment_id)
    payload = _mapping(properties.get("legacy_payload"))
    return (legacy.get("legacy_class") == "LineBranch"
            and payload.get("line_type") in {"overhead", "cable"})


def _graphics_extensions(representation: GraphicalRepresentation) -> Mapping[str, Any]:
    nested = _mapping(representation.extensions.get("stage3_graphics"))
    if not nested:
        nested = _mapping(representation.extensions.get("graphics"))
    return nested or representation.extensions


def _number(values: Mapping[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if math.isfinite(value) else default


def _boolean(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    return value if isinstance(value, bool) else default


def _mode_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    return CanvasMode.ANALYSIS.value if "analy" in text else CanvasMode.EDIT.value


def _target_signature(
    representation: GraphicalRepresentation,
    model: ElectricalModel,
) -> tuple[Any, ...]:
    """Минимальный срез domain target для локального обновления item."""

    if representation.equipment_id is not None:
        equipment = model.equipment.get(representation.equipment_id)
        if equipment is None:
            return (None,)
        definition = model.equipment_types.get((equipment.type_id, equipment.type_version))
        ports = frozenset(equipment.port_ids)
        connections = tuple(
            row for row in model.connections.values() if row.port_id in ports
        )
        return equipment, definition, connections
    node = model.electrical_nodes.get(representation.electrical_node_id)
    connections = tuple(
        row for row in model.connections.values()
        if row.electrical_node_id == representation.electrical_node_id
    )
    return node, connections


def _effective_equipment_position(
    equipment: EquipmentInstance | None,
    model: ElectricalModel,
    operating_state_id: OperatingStateId | None,
) -> SwitchPosition | None:
    """Положение аппарата с учётом выбранного sparse-режима сети."""

    if equipment is None:
        return None
    position = equipment.normal_position
    legacy_position = equipment.properties.get("position")
    if legacy_position is not None:
        try:
            position = SwitchPosition(str(legacy_position).upper())
        except ValueError:
            pass
    if operating_state_id is not None:
        operating_state = model.operating_states.get(operating_state_id)
        if operating_state is not None:
            position = operating_state.positions.get(equipment.id, position)
    return position


class PortVisualState(StrEnum):
    NORMAL = "normal"
    SOURCE = "source"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class SymbolPortAnchor:
    port_id: PortId
    role: str
    display_name: str
    x: float
    y: float
    direction: RouteDirection


def _symbol_port_anchors(
    equipment: EquipmentInstance,
    definition: Any,
    *,
    behavior_key: str,
    width: float,
    height: float,
) -> tuple[SymbolPortAnchor, ...]:
    """Определить графические выводы по роли, не меняя семантику порта."""
    del behavior_key  # Поведение берётся из EquipmentTypeDefinition.
    return tuple(
        SymbolPortAnchor(
            item.port_id,
            item.role,
            item.display_name,
            item.x,
            item.y,
            item.direction,
        )
        for item in base_port_layout(
            equipment,
            definition,
            width=width,
            height=height,
        )
    )


class ElectricalPortItem(QGraphicsObject):
    """Видимая привязка настоящего ``PortId`` на символе оборудования."""

    def __init__(self, anchor: SymbolPortAnchor, parent: "DiagramObjectItem"):
        super().__init__(parent)
        self.anchor = anchor
        self._visual_state = PortVisualState.NORMAL
        self._hovered = False
        self._render_context = DiagramRenderContext()
        self.setPos(anchor.x, anchor.y)
        self.setZValue(30.0)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(
            f"Электрический порт: {anchor.display_name}\n"
            f"Идентификатор: {anchor.port_id.value}"
        )

    @property
    def port_id(self) -> PortId:
        return self.anchor.port_id

    @property
    def representation_id(self) -> GraphicalRepresentationId:
        parent = self.parentItem()
        assert isinstance(parent, DiagramObjectItem)
        return parent.representation_id

    def update_anchor(self, anchor: SymbolPortAnchor) -> None:
        self.anchor = anchor
        self.setPos(anchor.x, anchor.y)
        self.setToolTip(
            f"Электрический порт: {anchor.display_name}\n"
            f"Идентификатор: {anchor.port_id.value}"
        )
        self.update()

    def update_domain_tooltip(self, model: ElectricalModel) -> None:
        connection = model.connection_for_port(self.port_id)
        voltage_id = model.port_voltage_class(self.port_id)
        voltage = model.voltage_classes.get(voltage_id) if voltage_id is not None else None
        rows = [
            f"Электрический порт: {self.anchor.display_name}",
            f"Идентификатор порта: {self.port_id.value}",
            "Электрический узел: "
            + (
                connection.electrical_node_id.value
                if connection is not None
                else "не подключён"
            ),
            "Класс напряжения: "
            + (voltage.display_name if voltage is not None else "не определён"),
        ]
        self.setToolTip("\n".join(rows))

    def set_visual_state(self, state: PortVisualState | str) -> None:
        normalized = state if isinstance(state, PortVisualState) else PortVisualState(state)
        if normalized != self._visual_state:
            self._visual_state = normalized
            self.update()

    def set_render_context(self, context: DiagramRenderContext) -> None:
        if context != self._render_context:
            self._render_context = context
            self.update()

    def scene_direction(self) -> RouteDirection:
        vector = {
            RouteDirection.LEFT: QPointF(-1.0, 0.0),
            RouteDirection.RIGHT: QPointF(1.0, 0.0),
            RouteDirection.UP: QPointF(0.0, -1.0),
            RouteDirection.DOWN: QPointF(0.0, 1.0),
        }[self.anchor.direction]
        origin = self.mapToScene(QPointF())
        target = self.mapToScene(vector)
        dx, dy = target.x() - origin.x(), target.y() - origin.y()
        if abs(dx) >= abs(dy):
            return RouteDirection.RIGHT if dx >= 0.0 else RouteDirection.LEFT
        return RouteDirection.DOWN if dy >= 0.0 else RouteDirection.UP

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-6.0, -6.0, 12.0, 12.0)

    def interaction_enabled(self) -> bool:
        parent, scene = self.parentItem(), self.scene()
        if not isinstance(parent, DiagramObjectItem) or parent._canonical_key not in {"line", "line_section"}:
            return True
        if self._visual_state is not PortVisualState.NORMAL:
            return True
        if not isinstance(scene, DiagramGraphicsScene):
            return False
        return scene._developer_overlay or scene.endpoint_editing_enabled()

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        if not self.interaction_enabled():
            return path
        path.addEllipse(QRectF(-7.0, -7.0, 14.0, 14.0))
        return path

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        parent = self.parentItem()
        if not self.interaction_enabled():
            return
        model = parent._model if isinstance(parent, DiagramObjectItem) else None
        nominal_voltage_v = (
            _resolved_nominal_voltage(model, self._render_context, self.port_id)
            if model is not None
            else None
        )
        connection = model.connection_for_port(self.port_id) if model is not None else None
        energization = (
            _node_energization(
                self._render_context,
                connection.electrical_node_id,
            )
            if connection is not None
            else Energization.UNKNOWN
        )
        equipment_id = (
            parent.representation.equipment_id
            if isinstance(parent, DiagramObjectItem)
            else None
        )
        normal_stroke = _state_stroke(
            nominal_voltage_v,
            self._render_context,
            energization=energization,
            availability=_equipment_availability(
                self._render_context,
                equipment_id,
            ),
        )
        colors = {
            PortVisualState.NORMAL: QColor(normal_stroke),
            PortVisualState.SOURCE: QColor(COLORS["blue"]),
            PortVisualState.COMPATIBLE: QColor("#22C55E"),
            PortVisualState.INCOMPATIBLE: QColor("#EF4444"),
        }
        outline = QColor(
            COLORS["blue"]
            if self._visual_state is not PortVisualState.NORMAL
            else normal_stroke
        )
        pen = QPen(outline, 1.35)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(colors[self._visual_state])
        if (
            self._visual_state is PortVisualState.NORMAL
            and connection is not None
            and isinstance(parent, DiagramObjectItem)
            and parent._canonical_key == "busbar"
        ):
            pen.setColor(QColor(DIAGRAM_NEUTRAL_STROKE))
            painter.setPen(pen)
            painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QPointF(), 4.0, 4.0)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.setScale(1.2)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.setScale(1.0)
        self.update()
        super().hoverLeaveEvent(event)


class DiagramObjectItem(QGraphicsObject):
    """Лёгкий символ одного GraphicalRepresentation.

    Объект кэшируется в координатах устройства и обновляется на месте, если
    изменилось только соответствующее представление.
    """

    def __init__(
        self,
        representation: GraphicalRepresentation,
        model: ElectricalModel,
        *,
        editable: bool,
        developer_overlay: bool,
        operating_state_id: OperatingStateId | None = None,
    ):
        super().__init__()
        self.representation = representation
        self._model = model
        self._target_state = _target_signature(representation, model)
        self._symbol_key = ""
        self._behavior_key = ""
        self._type_id = "electrical_node"
        self._name = ""
        self._width = 80.0
        self._height = 50.0
        self._line_width = 2.0
        self._switch_open = False
        self._symbol_cache: SymbolGeometry | None = None
        self._bridge_paths: dict[int, QPainterPath] = {}
        self._bridge_nodes: tuple[QPointF, ...] = ()
        self._bus_junction_points: tuple[QPointF, ...] = ()
        self._canonical_key = "generic"
        self._effective_position: SwitchPosition | None = None
        self._port_items: dict[PortId, ElectricalPortItem] = {}
        self._target_feedback = ConnectionTargetFeedback.NEUTRAL
        self._hovered = False
        self._operating_state_id = operating_state_id
        self._render_context = DiagramRenderContext()
        self._adapter_status = "данные адаптера не переданы"
        self._label = DiagramLabelItem(self)
        self._label.setBrush(QColor(COLORS["text"]))
        self._debug = QGraphicsSimpleTextItem(self)
        self._debug.setBrush(QColor(COLORS["muted"]))
        self._debug.setScale(0.82)
        self._diagnostic_badge = QGraphicsSimpleTextItem("!", self)
        self._diagnostic_badge.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )
        self._diagnostic_badge.setZValue(90.0)
        self._diagnostic_badge.setVisible(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.update_from(
            representation,
            model,
            editable=editable,
            developer_overlay=developer_overlay,
            operating_state_id=operating_state_id,
        )

    @property
    def representation_id(self) -> GraphicalRepresentationId:
        return self.representation.id

    @property
    def type_key(self) -> str:
        return self._type_id

    def set_editable(self, value: bool) -> None:
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, bool(value))
        self._label.set_editable(bool(value))

    def set_render_context(self, context: DiagramRenderContext) -> None:
        if context == self._render_context:
            return
        self._render_context = context
        for item in self._port_items.values():
            item.set_render_context(context)
        self._label.setBrush(QColor(self._label_stroke()))
        self.update()

    def _connected_node_ids(self) -> tuple[ElectricalNodeId, ...]:
        equipment_id = self.representation.equipment_id
        equipment = (
            self._model.equipment.get(equipment_id)
            if equipment_id is not None
            else None
        )
        if equipment is None:
            node_id = self.representation.electrical_node_id
            return (node_id,) if node_id is not None else ()
        return tuple(dict.fromkeys(
            connection.electrical_node_id
            for port_id in equipment.port_ids
            for connection in (self._model.connection_for_port(port_id),)
            if connection is not None
        ))

    def _nominal_voltage_v(self) -> int | None:
        node_id = self.representation.electrical_node_id
        if node_id is not None:
            return _resolved_nominal_voltage(
                self._model,
                self._render_context,
                node_id,
            )
        equipment_id = self.representation.equipment_id
        equipment = (
            self._model.equipment.get(equipment_id)
            if equipment_id is not None
            else None
        )
        if equipment is None:
            return None
        return _common_nominal_voltage(
            self._model,
            self._render_context,
            equipment.port_ids,
        )

    def _energization(self) -> Energization:
        return _aggregate_energization(
            _node_energization(self._render_context, node_id)
            for node_id in self._connected_node_ids()
        )

    def _availability(self) -> EquipmentAvailability | None:
        return _equipment_availability(
            self._render_context,
            self.representation.equipment_id,
        )

    def _disconnected(self) -> bool:
        return _equipment_disconnected(
            self._render_context,
            self.representation.equipment_id,
        )

    def _symbol_stroke(self) -> str:
        return _state_stroke(
            None,
            self._render_context,
            energization=self._energization(),
            availability=self._availability(),
            disconnected=self._disconnected(),
        )

    def _port_id_for_role(self, role: str) -> PortId | None:
        for port_id, item in self._port_items.items():
            if item.anchor.role == role:
                return port_id
        # Legacy definitions may call transformer terminals ``from``/``to``
        # while the shared symbol library calls the same drawn ends ``hv``/``lv``.
        # Match the terminal by its immutable local coordinate, keeping both the
        # real PortId and the domain role untouched.
        terminal = self.symbol_geometry().terminal(role)
        if terminal is not None:
            for port_id, item in self._port_items.items():
                if math.isclose(item.anchor.x, terminal.x, abs_tol=1e-9) and math.isclose(
                    item.anchor.y,
                    terminal.y,
                    abs_tol=1e-9,
                ):
                    return port_id
        return None

    def _primitive_stroke(self, voltage_role: str | None) -> str:
        """Color only the conductor bound to one semantic terminal role."""

        if voltage_role == "body":
            # Apparatus position, nominal class and live/dead conductors are
            # separate signals. An open/dead breaker still has its class frame.
            return voltage_stroke(
                self._nominal_voltage_v(), color_mode=self._render_context.color_mode
            )
        if voltage_role is None:
            return self._symbol_stroke()
        node_id = self.representation.electrical_node_id
        if node_id is not None:
            return _state_stroke(
                _resolved_nominal_voltage(
                    self._model,
                    self._render_context,
                    node_id,
                ),
                self._render_context,
                energization=_node_energization(self._render_context, node_id),
            )
        port_id = self._port_id_for_role(voltage_role)
        if port_id is None:
            return self._symbol_stroke()
        connection = self._model.connection_for_port(port_id)
        return _state_stroke(
            _resolved_nominal_voltage(
                self._model,
                self._render_context,
                port_id,
            ),
            self._render_context,
            energization=(
                _node_energization(
                    self._render_context,
                    connection.electrical_node_id,
                )
                if connection is not None
                else Energization.UNKNOWN
            ),
            availability=self._availability(),
            disconnected=(
                self._disconnected()
                if self._canonical_key not in {"circuit_breaker", "recloser", "disconnector"}
                else False
            ),
        )

    def _label_stroke(self) -> str:
        # Identification remains legible even when its circuit is deenergized.
        return DIAGRAM_NEUTRAL_STROKE

    def update_from(
        self,
        representation: GraphicalRepresentation,
        model: ElectricalModel,
        *,
        editable: bool,
        developer_overlay: bool,
        operating_state_id: OperatingStateId | None = None,
    ) -> None:
        self.prepareGeometryChange()
        self.representation = representation
        self._model = model
        self._target_state = _target_signature(representation, model)
        self._operating_state_id = operating_state_id
        graphics = _graphics_extensions(representation)
        equipment: EquipmentInstance | None = None
        definition = None
        if representation.equipment_id is not None:
            equipment = model.equipment.get(representation.equipment_id)
            if equipment is not None:
                definition = model.equipment_types.get((equipment.type_id, equipment.type_version))

        if equipment is None:
            self._type_id = "electrical_node"
            self._behavior_key = "electrical_node"
            self._symbol_key = representation.symbol_key or "connection_point"
            self._name = representation.label or "Электрический узел"
        else:
            self._type_id = equipment.type_id.value
            self._behavior_key = definition.behavior_key if definition is not None else "equipment"
            definition_symbol = ""
            if definition is not None:
                definition_symbol = str(definition.extensions.get("diagram_symbol_key", ""))
            self._symbol_key = representation.symbol_key or definition_symbol or self._behavior_key
            self._name = representation.label or equipment.name

        orientation = str(graphics.get("orientation", "horizontal")).lower()
        # Размеры по умолчанию берутся из библиотеки условных обозначений,
        # поэтому все аппараты на схеме соразмерны друг другу. Явно заданные в
        # проекте ширина и высота, как и раньше, имеют приоритет.
        self._canonical_key = canonical_key(self._symbol_key, self._behavior_key)
        default_width, default_height = symbol_default_size(self._canonical_key)
        if self._canonical_key == "busbar" and orientation == "vertical":
            default_width, default_height = default_height, default_width
        self._width = max(12.0, _number(graphics, "width", default_width))
        self._height = max(12.0, _number(graphics, "height", default_height))
        self._line_width = max(0.5, min(8.0, _number(graphics, "line_width", 2.0)))

        position = _effective_equipment_position(
            equipment,
            model,
            operating_state_id,
        )
        self._effective_position = position
        self._switch_open = position is SwitchPosition.OPEN
        # Геометрия символа строится один раз на обновление представления:
        # boundingRect() и shape() вызываются Qt очень часто, и пересборка
        # символа на каждый вызов была бы заметна на больших схемах.
        self._symbol_cache = symbol_for(
            self._symbol_key,
            self._behavior_key,
            width=self._width,
            height=self._height,
            opened=self._switch_open,
        )

        self.setPos(representation.x, representation.y)
        self.setRotation(representation.rotation_deg)
        self.setZValue(float(representation.z_index))
        self.set_editable(editable)
        self._sync_port_items(equipment, definition)
        for port_item in self._port_items.values():
            port_item.set_render_context(self._render_context)

        label_x = _number(graphics, "label_x", _number(graphics, "label_offset_x", -self._width / 2.0))
        label_y = _number(graphics, "label_y", _number(graphics, "label_offset_y", self._height / 2.0 + 7.0))
        self._label.setText(self._name)
        self._label_content = present_label(model, representation)
        self._label.setText(wrap_text(self._label_content.text(), self._label.font()))
        self._label.setBrush(QColor(self._label_stroke()))
        # Keep the persisted parent-local anchor, undo the apparatus rotation
        # for glyphs only. Ports and symbol geometry are untouched (AUD-SYM-002).
        self._label.setRotation(-representation.rotation_deg)
        self._label.setPos(label_x, label_y)
        self._label_preferred_position = QPointF(label_x, label_y)
        self._label_requested_visible = _boolean(graphics, "label_visible", True)
        self._label.setVisible(self._label_requested_visible)

        self._debug.setText(self._debug_text(equipment, model))
        self._debug.setPos(-self._width / 2.0, -self._height / 2.0 - 32.0)
        self._debug.setVisible(developer_overlay)
        self._diagnostic_badge.setPos(
            self._width / 2.0 + 4.0,
            -self._height / 2.0 - 10.0,
        )
        self.setToolTip(self._tooltip(equipment, model))
        self.update()

    def set_diagnostics(self, diagnostics: Iterable[object]) -> None:
        rows = tuple(diagnostics)
        self._has_diagnostics = bool(rows)
        if not rows:
            self._diagnostic_badge.setVisible(False)
            self._diagnostic_badge.setToolTip("")
            return
        severities = {
            str(getattr(getattr(row, "severity", "warning"), "value", getattr(row, "severity", "warning")))
            for row in rows
        }
        self._diagnostic_badge.setBrush(
            QColor(COLORS["red"] if "error" in severities else COLORS["amber"])
        )
        self._diagnostic_badge.setText("!" if len(rows) == 1 else f"! {len(rows)}")
        self._diagnostic_badge.setToolTip("\n\n".join(
            str(getattr(row, "message", row))
            + (
                "\nЧто сделать: " + str(getattr(row, "action", ""))
                if str(getattr(row, "action", "")).strip()
                else ""
            )
            for row in rows
        ))
        self._diagnostic_badge.setVisible(not getattr(self, "_service_node_hidden", False))

    def set_service_node_visibility(self, developer_overlay: bool) -> None:
        """Suppress bookkeeping dots, not their persisted electrical objects."""
        service = self.representation.electrical_node_id is not None and self._canonical_key == "connection_point"
        degree = sum(row.electrical_node_id == self.representation.electrical_node_id
                     for row in self._model.connections.values()) if service else 0
        self._service_node_hidden = service and degree < 3 and not developer_overlay
        if self._service_node_hidden:
            self._debug.setVisible(False)
        self._diagnostic_badge.setVisible(bool(getattr(self, "_has_diagnostics", False)) and not self._service_node_hidden)
        self.update()

    def service_node_is_hidden(self) -> bool:
        scene = self.scene()
        return bool(getattr(self, "_service_node_hidden", False)) and not (
            isinstance(scene, DiagramGraphicsScene) and scene.endpoint_editing_enabled())

    def set_adapter_diagnostics(
        self,
        diagnostics: Iterable[object],
        *,
        available: bool,
    ) -> None:
        if not available:
            status = "данные адаптера не переданы"
        else:
            object_ids = {
                self.representation.id.value,
                self.representation.target_id.value,
            }
            equipment = (
                self._model.equipment.get(self.representation.equipment_id)
                if self.representation.equipment_id is not None
                else None
            )
            if equipment is not None:
                object_ids.update(port_id.value for port_id in equipment.port_ids)
            matching = tuple(
                row
                for row in diagnostics
                if str(getattr(row, "object_id", "")) in object_ids
            )
            errors = tuple(
                row
                for row in matching
                if str(getattr(row, "severity", "")).lower() == "error"
            )
            if errors:
                status = "заблокирован — " + "; ".join(
                    str(getattr(row, "message", getattr(row, "code", "ошибка")))
                    for row in errors
                )
            elif matching:
                status = "есть предупреждения — " + "; ".join(
                    str(getattr(row, "message", getattr(row, "code", "предупреждение")))
                    for row in matching
                )
            else:
                status = "ошибок адаптации для объекта нет"
        if status != self._adapter_status:
            self._adapter_status = status
            equipment = (
                self._model.equipment.get(self.representation.equipment_id)
                if self.representation.equipment_id is not None
                else None
            )
            self._debug.setText(self._debug_text(equipment, self._model))

    def _sync_port_items(self, equipment: EquipmentInstance | None, definition: Any) -> None:
        if equipment is None or definition is None:
            for item in self._port_items.values():
                item.setParentItem(None)
                if item.scene() is not None:
                    item.scene().removeItem(item)
            self._port_items.clear()
            return
        anchors = {
            item.port_id: item
            for item in _symbol_port_anchors(
                equipment,
                definition,
                behavior_key=self._behavior_key,
                width=self._width,
                height=self._height,
            )
        }
        for port_id in tuple(self._port_items):
            if port_id not in anchors:
                item = self._port_items.pop(port_id)
                item.setParentItem(None)
                if item.scene() is not None:
                    item.scene().removeItem(item)
        for port_id, anchor in anchors.items():
            item = self._port_items.get(port_id)
            if item is None:
                item = ElectricalPortItem(anchor, self)
                self._port_items[port_id] = item
            else:
                item.update_anchor(anchor)
            item.update_domain_tooltip(self._model)
            item.set_render_context(self._render_context)

    def port_item(self, port_id: PortId) -> ElectricalPortItem | None:
        return self._port_items.get(port_id)

    def set_target_feedback(self, feedback: ConnectionTargetFeedback | str) -> None:
        normalized = (
            feedback
            if isinstance(feedback, ConnectionTargetFeedback)
            else ConnectionTargetFeedback(feedback)
        )
        if normalized != self._target_feedback:
            self._target_feedback = normalized
            self.update()

    def _debug_text(self, equipment: EquipmentInstance | None, model: ElectricalModel) -> str:
        rows = [f"Представление: {self.representation.id.value}"]
        if equipment is not None:
            rows.append(f"Оборудование: {equipment.id.value}")
            for port_id in equipment.port_ids:
                connection = model.connection_for_port(port_id)
                voltage_id = model.port_voltage_class(port_id)
                voltage = model.voltage_classes.get(voltage_id) if voltage_id is not None else None
                rows.append(
                    f"Порт {port_id.value} → "
                    + (
                        connection.electrical_node_id.value
                        if connection is not None
                        else "не подключён"
                    )
                    + f"; напряжение: {voltage.display_name if voltage else 'не определено'}"
                )
            rows.append(f"Соединений: {sum(1 for row in model.connections.values() if row.port_id in equipment.port_ids)}")
            if equipment.normal_position is not None:
                rows.append(
                    "Состояние аппарата: "
                    + ("Отключён" if self._switch_open else "Включён")
                )
                if self._operating_state_id is not None:
                    state = model.operating_states.get(self._operating_state_id)
                    rows.append(
                        "Активный режим: "
                        + (state.name if state is not None else self._operating_state_id.value)
                    )
            insertion = equipment.extensions.get("line_insertion")
            if isinstance(insertion, Mapping):
                terminal_names: dict[str, str] = {}
                for port_id in equipment.port_ids:
                    definition = model.port_definition(port_id)
                    terminal_names[definition.role] = definition.display_name
                rows.append(
                    "Терминалы вставки: "
                    + ", ".join(
                        terminal_names.get(
                            str(item),
                            f"неизвестный терминал (внутренний код: {item})",
                        )
                        for item in insertion.get("terminal_roles", ())
                    )
                )
                physical_offset = insertion.get("physical_offset_mm")
                rows.append(
                    "Физическое расстояние: "
                    + (
                        f"{physical_offset / 1_000_000.0:.6g} км"
                        if isinstance(physical_offset, int)
                        else "не подтверждено"
                    )
                )
            section = model.line_sections.get(equipment.id)
            if section is not None:
                line = model.logical_lines.get(section.logical_line_id)
                rows.append(f"Логическая линия: {section.logical_line_id.value}")
                if line is not None and line.feeder_id is not None:
                    rows.append(f"Фидер: {line.feeder_id.value}")
        elif self.representation.electrical_node_id is not None:
            node_id = self.representation.electrical_node_id
            rows.append(f"Электрический узел: {node_id.value}")
            rows.append(f"Соединений: {sum(1 for row in model.connections.values() if row.electrical_node_id == node_id)}")
            node = model.electrical_nodes.get(node_id)
            voltage = (
                model.voltage_classes.get(node.declared_voltage_class_id)
                if node is not None and node.declared_voltage_class_id is not None
                else None
            )
            rows.append(
                "Класс напряжения: "
                + (voltage.display_name if voltage is not None else "не определён")
            )
            if node is not None:
                origin = node.extensions.get("creation_origin")
                if origin:
                    origin_labels = {
                        "automatic_free_endpoint": "автоматический свободный конец",
                        "automatic_port_endpoint": "автоматический узел порта",
                        "automatic_port_to_port": "автоматическое соединение портов",
                        "automatic_line_split": "автоматическое разбиение линии",
                        "automatic_inline_insertion": "автоматическая вставка аппарата",
                    }
                    rows.append(
                        "Происхождение узла: "
                        + origin_labels.get(str(origin), "служебное")
                    )
                physical_offset = node.extensions.get("physical_offset_mm")
                rows.append(
                    "Физическое расстояние: "
                    + (
                        f"{physical_offset / 1_000_000.0:.6g} км"
                        if isinstance(physical_offset, int)
                        else "не подтверждено"
                    )
                )
        rows.append(f"Страница: {self.representation.page_id.value}")
        rows.append(f"Статус расчётного адаптера: {self._adapter_status}")
        return "\n".join(rows)

    def _tooltip(self, equipment: EquipmentInstance | None, model: ElectricalModel) -> str:
        if equipment is None:
            node_id = self.representation.electrical_node_id
            count = sum(1 for row in model.connections.values() if row.electrical_node_id == node_id)
            return f"{self._name}\nПодключений: {count}"
        definition = model.equipment_types.get((equipment.type_id, equipment.type_version))
        type_name = definition.display_name if definition is not None else "Оборудование"
        hint = (
            "\nСтрелка: начало → конец линии, не расчётное направление тока"
            if _is_directional_line(model, equipment.id) else ""
        )
        return f"{equipment.name}\n{type_name}{hint}"

    def symbol_ink_rect(self) -> QRectF:
        """Габарит фактически нарисованного символа при угле 0°.

        Рамка выделения и область попадания курсора строятся по нему, а не по
        объявленному прямоугольнику: у тонких аппаратов объявленная рамка
        заметно выше рисунка, и выделение выглядело бы как пустая коробка
        вокруг тонкой линии.
        """
        try:
            min_x, min_y, max_x, max_y = self.symbol_geometry().ink_bounds()
        except (ValueError, KeyError):
            return QRectF(
                -self._width / 2.0, -self._height / 2.0, self._width, self._height
            )
        # Ширина пера уходит за осевую линию примитива на половину толщины.
        pen = self._line_width
        return QRectF(
            min_x - pen, min_y - pen,
            (max_x - min_x) + pen * 2.0, (max_y - min_y) + pen * 2.0,
        )

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt API
        margin = 9.0
        bounds = self.symbol_ink_rect()
        for path in self._bridge_paths.values():
            bounds = bounds.united(path.boundingRect())
        return bounds.adjusted(-margin, -margin, margin, margin)

    def body_scene_rect(self, *, clearance: float = 0.0) -> QRectF:
        """Габарит тела без подписи и selection-overlay в координатах сцены."""

        body = QRectF(
            -self._width / 2.0,
            -self._height / 2.0,
            self._width,
            self._height,
        )
        mapped = self.mapRectToScene(body)
        result = mapped.boundingRect() if hasattr(mapped, "boundingRect") else QRectF(mapped)
        return result.adjusted(-clearance, -clearance, clearance, clearance)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def sceneEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.UngrabMouse:
            scene = self.scene()
            if isinstance(scene, DiagramGraphicsScene) and not scene._body_drag_releasing:
                # Losing the grab is cancellation, not an implicit commit. The
                # scene clears its gesture before any further Qt callbacks.
                scene.cancel_object_drag(release_mouse=False)
        return super().sceneEvent(event)

    def shape(self) -> QPainterPath:
        # Область попадания облегает рисунок с запасом на удобство мыши, но не
        # захватывает пустое место до углов объявленного габарита.
        path = QPainterPath()
        if self.service_node_is_hidden():
            return path
        path.addRoundedRect(self.symbol_ink_rect().adjusted(-5.0, -5.0, 5.0, 5.0), 4.0, 4.0)
        return path

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        if self.service_node_is_hidden():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(self._symbol_stroke())
        pen = QPen(color, self._line_width)
        if (
            self._availability() is EquipmentAvailability.OUT_OF_SERVICE
            or self._disconnected()
        ):
            pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        self._paint_symbol(painter)
        if self._bridge_nodes and self._canonical_key != "busbar":
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(DIAGRAM_NEUTRAL_STROKE))
            for point in self._bridge_nodes:
                painter.drawEllipse(point, 3.0, 3.0)
            painter.restore()
        # Bus attachment glyphs are real child handles, not paint-only dots.
        if self._hovered and not self.isSelected():
            hover_pen = QPen(QColor("#60A5FA"), 1.2, Qt.PenStyle.DotLine)
            hover_pen.setCosmetic(True)
            painter.setPen(hover_pen)
            painter.setBrush(QColor(96, 165, 250, 12))
            painter.drawRoundedRect(
                self.symbol_ink_rect().adjusted(-4, -4, 4, 4), 5, 5
            )
        if self.isSelected():
            selected_rect = self.symbol_ink_rect().adjusted(-5, -5, 5, 5)
            halo_pen = QPen(QColor(255, 255, 255, 225), 5.0)
            halo_pen.setCosmetic(True)
            painter.setPen(halo_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(selected_rect, 6, 6)
            selected_pen = QPen(QColor(COLORS["blue"]), 2.1, Qt.PenStyle.DashLine)
            selected_pen.setCosmetic(True)
            painter.setPen(selected_pen)
            painter.setBrush(QColor(37, 99, 235, 18))
            painter.drawRoundedRect(selected_rect, 5, 5)
            # A center dot on a conductor looks like a junction and masks its
            # direction arrow. The selection frame and rotation handle suffice.
            if self._canonical_key not in {"line", "line_section", "busbar"}:
                painter.setBrush(QColor(COLORS["blue"]))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(0.0, 0.0), 2.8, 2.8)
        if self._target_feedback is not ConnectionTargetFeedback.NEUTRAL:
            color = QColor(
                "#22C55E"
                if self._target_feedback is ConnectionTargetFeedback.COMPATIBLE
                else "#EF4444"
            )
            target_pen = QPen(color, 2.2, Qt.PenStyle.DashLine)
            target_pen.setCosmetic(True)
            painter.setPen(target_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.symbol_ink_rect().adjusted(-6, -6, 6, 6), 6, 6)

    def direction_marker(self) -> SymbolPrimitive | None:
        """Keep the line arrow off a display-only bridge or crossing gap."""
        if not _is_directional_line(self._model, self.representation.equipment_id):
            return None
        template = next((primitive for primitive in self.symbol_geometry().primitives
                         if primitive.direction_marker), None)
        if template is None:
            return None
        display = self._bridge_paths.get(0)
        if display is None:
            return template
        return visible_line_direction_marker(display, nodes=self._bridge_nodes)

    def symbol_geometry(self) -> SymbolGeometry:
        """Геометрия условного обозначения при угле 0° для текущих габаритов."""
        cached = getattr(self, "_symbol_cache", None)
        if cached is None:
            cached = symbol_for(
                self._symbol_key,
                self._behavior_key,
                width=self._width,
                height=self._height,
                opened=self._switch_open,
            )
            self._symbol_cache = cached
        return cached

    def _paint_symbol(self, painter: QPainter) -> None:
        """Нарисовать аппарат примитивами библиотеки условных обозначений.

        Отдельного «рисунка на каждый случай» здесь больше нет: и редактор, и
        векторный вывод получают одни и те же примитивы, поэтому нарисованный
        проводник не может разойтись с точкой подключения.
        """
        geometry = self.symbol_geometry()
        base_pen = painter.pen()
        # Элемент повёрнут через QGraphicsItem.setRotation(), поэтому пометки,
        # которые обязаны оставаться горизонтальными, разворачиваются обратно
        # вокруг своей точки привязки.
        item_rotation = float(self.rotation())
        position_known = (
            self._render_context.state_available and self._effective_position is not None
        )
        for index, item in enumerate(geometry.primitives):
            if item.direction_marker:
                item = self.direction_marker()
                if item is None:
                    continue
            if item.voltage_role == "body" and item.kind == "line" and not position_known:
                continue
            pen = QPen(base_pen)
            pen.setWidthF(self._line_width * item.stroke_scale)
            primitive_color = QColor(self._primitive_stroke(item.voltage_role))
            pen.setColor(primitive_color)
            if item.direction_marker:
                pen.setStyle(Qt.PenStyle.SolidLine)
            if item.voltage_role == "body":
                pen.setStyle(Qt.PenStyle.SolidLine)
            elif (
                item.voltage_role is not None
                and self._canonical_key in {"circuit_breaker", "recloser", "disconnector"}
                and self._availability() is not EquipmentAvailability.OUT_OF_SERVICE
            ):
                # An open contact does not deenergize its incoming lead.
                # Each lead gets its own node's colour up to the apparatus.
                pen.setStyle(Qt.PenStyle.SolidLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            if item.state_fill is not None:
                painter.setBrush(QColor(switch_state_fill(
                    item.state_fill, color_mode=self._render_context.color_mode
                ) if position_known else "#FFFFFF"))
            else:
                painter.setBrush(
                    primitive_color if item.filled else Qt.BrushStyle.NoBrush
                )
            if index in self._bridge_paths:
                painter.drawPath(self._bridge_paths[index])
                continue
            if not item.rotates and item_rotation:
                painter.save()
                anchor = QPointF(*item.center)
                painter.translate(anchor)
                painter.rotate(-item_rotation)
                painter.translate(-anchor)
                self._paint_primitive(painter, item)
                painter.restore()
                continue
            self._paint_primitive(painter, item)
            if item.state_fill is not None and not position_known:
                # No fallback red/green claim when the selected mode cannot be
                # resolved. The nominal frame stays; '?' is position unknown.
                painter.save()
                painter.rotate(-item_rotation)
                painter.setFont(QFont("Arial", 9))
                painter.drawText(QRectF(-10.0, -10.0, 20.0, 20.0), Qt.AlignmentFlag.AlignCenter, "?")
                painter.restore()
        painter.setPen(base_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    @staticmethod
    def _paint_primitive(painter: QPainter, item) -> None:
        kind = item.kind
        if kind == "line":
            (x1, y1), (x2, y2) = item.points
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        elif kind == "polyline":
            path = QPainterPath(QPointF(*item.points[0]))
            for point in item.points[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)
        elif kind == "polygon":
            painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in item.points]))
        elif kind == "circle":
            painter.drawEllipse(QPointF(*item.center), item.radius, item.radius)
        elif kind == "rect":
            cx, cy = item.center
            rect = QRectF(
                cx - item.half_width,
                cy - item.half_height,
                item.half_width * 2.0,
                item.half_height * 2.0,
            )
            if item.corner_radius:
                painter.drawRoundedRect(rect, item.corner_radius, item.corner_radius)
            else:
                painter.drawRect(rect)
        elif kind == "arc":
            cx, cy = item.center
            radius = item.radius
            rect = QRectF(cx - radius, cy - radius, radius * 2.0, radius * 2.0)
            # Qt считает углы в 1/16 градуса против часовой стрелки — как и
            # библиотека, поэтому дополнительного пересчёта знака не требуется.
            painter.drawArc(
                rect,
                int(round(item.start_angle * 16)),
                int(round(item.span_angle * 16)),
            )
        elif kind == "text" and item.text:
            font = painter.font()
            font.setPointSizeF(max(4.0, item.font_size * 0.72))
            painter.save()
            painter.setFont(font)
            painter.setPen(QPen(painter.pen().color()))
            cx, cy = item.center
            box = QRectF(cx - 40.0, cy - 20.0, 80.0, 40.0)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, item.text)
            painter.restore()


def _path_from_vertices(vertices: tuple[RouteVertex, ...]) -> QPainterPath:
    path = QPainterPath()
    if not vertices:
        return path
    path.moveTo(vertices[0].x, vertices[0].y)
    for point in vertices[1:]:
        path.lineTo(point.x, point.y)
    return path


def _route_vertices(route: DiagramRoute) -> tuple[RouteVertex, ...]:
    return tuple(
        RouteVertex(
            item.x,
            item.y,
            RouteVertexSource.USER
            if item.source is RouteWaypointSource.USER
            else RouteVertexSource.AUTOMATIC,
            item.pinned,
        )
        for item in route.waypoints
    )


class BusAttachmentHandle(QGraphicsObject):
    """A graphical endpoint on a bus; dragging never moves the bus or a port."""

    def __init__(self, route_id: DiagramRouteId, at_start: bool, parent: DiagramObjectItem):
        super().__init__(parent)
        self.route_id = route_id
        self.at_start = at_start
        self.setZValue(45.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Точка присоединения: перетащите вдоль шины без изменения электрических связей")

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-7.0, -7.0, 14.0, 14.0)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(self.boundingRect())
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        del option, widget
        pen = QPen(QColor(DIAGRAM_NEUTRAL_STROKE), 1.25)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QPointF(), 3.3, 3.3)

    def sceneEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.UngrabMouse:
            scene = self.scene()
            if isinstance(scene, DiagramGraphicsScene):
                scene.cancel_connected_drag(release_mouse=False)
        return super().sceneEvent(event)


class RouteEndpointHandle(QGraphicsObject):
    """Экранный маркер реального порта физической ветви."""

    def __init__(self, at_start: bool, parent: "DiagramRouteItem"):
        super().__init__(parent)
        self.at_start = bool(at_start)
        self.setZValue(41.0)
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(
            "Переподключить начало физической ветви"
            if self.at_start
            else "Переподключить конец физической ветви"
        )

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-6.0, -6.0, 12.0, 12.0)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        pen = QPen(QColor(COLORS["blue"]), 1.4)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QPointF(), 4.5, 4.5)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        parent = self.parentItem()
        scene = self.scene()
        if isinstance(parent, DiagramRouteItem) and isinstance(
            scene, DiagramGraphicsScene
        ):
            parent.setSelected(True)
            scene.begin_route_endpoint_reconnect(
                parent.route.id, at_start=self.at_start, press_position=event.scenePos()
            )
            event.accept()
            return
        super().mousePressEvent(event)


class RouteWaypointHandle(QGraphicsObject):
    """Экранный маркер закреплённой пользовательской точки маршрута."""

    def __init__(self, waypoint: RouteWaypoint, parent: "DiagramRouteItem"):
        super().__init__(parent)
        self.waypoint = waypoint
        self._start = QPointF(waypoint.x, waypoint.y)
        self.setPos(waypoint.x, waypoint.y)
        self.setZValue(40.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip(
            f"Закреплённая точка маршрута\n"
            f"Идентификатор: {waypoint.id.value}\n"
            "Перетащите для изменения только графической трассы"
        )

    @property
    def waypoint_id(self) -> RouteWaypointId:
        return self.waypoint.id

    def update_waypoint(self, waypoint: RouteWaypoint) -> None:
        self.waypoint = waypoint
        self.setPos(waypoint.x, waypoint.y)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-5.0, -5.0, 10.0, 10.0)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        pen = QPen(QColor(COLORS["blue"]), 1.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRect(QRectF(-3.5, -3.5, 7.0, 7.0))

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        self._start = QPointF(self.pos())
        parent = self.parentItem()
        if isinstance(parent, DiagramRouteItem):
            parent.setSelected(True)
        scene = self.scene()
        if (
            isinstance(scene, DiagramGraphicsScene)
            and scene._tool_state_machine is not None
        ):
            transition = scene._tool_state_machine.activate(
                EditorTool.DRAG_ROUTE_SEGMENT,
                tool_name="Перемещение точки маршрута",
            )
            if transition.accepted:
                scene.toolStateChanged.emit(scene._tool_state_machine.state)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        super().mouseMoveEvent(event)
        parent = self.parentItem()
        if isinstance(parent, DiagramRouteItem):
            parent.preview_user_waypoint(self.waypoint_id, self.pos())

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        parent = self.parentItem()
        scene = self.scene()
        if not isinstance(parent, DiagramRouteItem):
            return
        if self.pos() == self._start:
            parent.clear_temporary_route()
        else:
            parent.commit_user_waypoint(self.waypoint_id, self.pos())
        if (
            isinstance(scene, DiagramGraphicsScene)
            and scene._tool_state_machine is not None
            and scene._tool_state_machine.tool is EditorTool.DRAG_ROUTE_SEGMENT
        ):
            scene._tool_state_machine.select_tool()
            scene.toolStateChanged.emit(scene._tool_state_machine.state)


class RouteSegmentHandle(QGraphicsObject):
    """Маркер для поперечного перемещения внутреннего сегмента трассы."""

    def __init__(
        self,
        segment_index: int,
        horizontal: bool,
        midpoint: QPointF,
        parent: "DiagramRouteItem",
    ):
        super().__init__(parent)
        self.segment_index = int(segment_index)
        self.horizontal = bool(horizontal)
        self._start = QPointF(midpoint)
        self._dragging = False
        self.setPos(midpoint)
        self.setZValue(39.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(
            Qt.CursorShape.SizeVerCursor
            if horizontal
            else Qt.CursorShape.SizeHorCursor
        )
        self.setToolTip(
            "Переместить горизонтальный сегмент"
            if horizontal
            else "Переместить вертикальный сегмент"
        )

    def update_segment(
        self, horizontal: bool, midpoint: QPointF
    ) -> None:
        self.horizontal = bool(horizontal)
        self.setPos(midpoint)
        self.setCursor(
            Qt.CursorShape.SizeVerCursor
            if horizontal
            else Qt.CursorShape.SizeHorCursor
        )

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-7.0, -4.0, 14.0, 8.0)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        pen = QPen(QColor(COLORS["blue"]), 1.1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QColor("#FFFFFF"))
        if self.horizontal:
            painter.drawRoundedRect(QRectF(-6.0, -2.0, 12.0, 4.0), 1.5, 1.5)
        else:
            painter.drawRoundedRect(QRectF(-2.0, -6.0, 4.0, 12.0), 1.5, 1.5)

    def itemChange(self, change, value):  # noqa: N802
        if (
            change == QGraphicsItem.GraphicsItemChange.ItemPositionChange
            and self._dragging
            and isinstance(value, QPointF)
        ):
            if self.horizontal:
                return QPointF(self._start.x(), value.y())
            return QPointF(value.x(), self._start.y())
        return super().itemChange(change, value)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        self._start = QPointF(self.pos())
        self._dragging = True
        parent = self.parentItem()
        if isinstance(parent, DiagramRouteItem):
            parent.setSelected(True)
        scene = self.scene()
        if (
            isinstance(scene, DiagramGraphicsScene)
            and scene._tool_state_machine is not None
        ):
            transition = scene._tool_state_machine.activate(
                EditorTool.DRAG_ROUTE_SEGMENT,
                tool_name="Перемещение сегмента линии",
            )
            if transition.accepted:
                scene.toolStateChanged.emit(scene._tool_state_machine.state)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        super().mouseMoveEvent(event)
        parent = self.parentItem()
        if isinstance(parent, DiagramRouteItem):
            parent.preview_segment_move(self.segment_index, self.pos())

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        self._dragging = False
        parent = self.parentItem()
        scene = self.scene()
        if not isinstance(parent, DiagramRouteItem):
            return
        if self.pos() == self._start:
            parent.clear_temporary_route()
        else:
            parent.commit_segment_move(self.segment_index, self.pos())
        if (
            isinstance(scene, DiagramGraphicsScene)
            and scene._tool_state_machine is not None
            and scene._tool_state_machine.tool is EditorTool.DRAG_ROUTE_SEGMENT
        ):
            scene._tool_state_machine.select_tool()
            scene.toolStateChanged.emit(scene._tool_state_machine.state)


class DiagramRouteItem(QGraphicsObject):
    """Отдельная графическая трасса без собственной электрической семантики."""

    def __init__(
        self,
        route: DiagramRoute,
        model: ElectricalModel,
        *,
        editable: bool,
        developer_overlay: bool,
    ):
        super().__init__()
        self.route = route
        self._model = model
        self._path = QPainterPath()
        self._label_route_owner = True
        self._label = DiagramRouteLabelItem(self)
        self._label_anchor = QPointF()
        self._label_preferred_position = QPointF()
        self._label_requested_visible = False
        self._label_content = LabelContent("")
        self._label.setVisible(False)
        self._display_path = QPainterPath()
        self._display_vertices: tuple[RouteVertex, ...] = ()
        self._bridge_nodes: tuple[QPointF, ...] = ()
        self._render_context = DiagramRenderContext()
        self._target_feedback = ConnectionTargetFeedback.NEUTRAL
        self._editable = editable
        self._waypoint_handles: dict[RouteWaypointId, RouteWaypointHandle] = {}
        self._segment_handles: dict[int, RouteSegmentHandle] = {}
        self._endpoint_handles: dict[bool, RouteEndpointHandle] = {}
        self._adapter_status = "данные адаптера не переданы"
        self._debug = QGraphicsSimpleTextItem(self)
        self._debug.setBrush(QColor(COLORS["muted"]))
        self._debug.setScale(0.78)
        self._diagnostic_badge = QGraphicsSimpleTextItem("!", self)
        self._diagnostic_badge.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )
        self._diagnostic_badge.setZValue(90.0)
        self._diagnostic_badge.setVisible(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(-10.0 if route.kind is DiagramRouteKind.EQUIPMENT_BRANCH else -5.0)
        self.update_from(
            route,
            model,
            editable=editable,
            developer_overlay=developer_overlay,
        )

    @property
    def route_id(self) -> DiagramRouteId:
        return self.route.id

    def sceneEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.UngrabMouse:
            scene = self.scene()
            if isinstance(scene, DiagramGraphicsScene):
                scene.cancel_connected_drag(release_mouse=False)
        return super().sceneEvent(event)

    @property
    def representation_id(self):
        """Label-layout owner ID; remains an actual DiagramRouteId."""
        return self.route.id

    @property
    def representation(self):
        return self.route

    def body_scene_rect(self, *, clearance=0.0):
        center = self.mapToScene(self._label_anchor)
        return QRectF(center.x() - 4, center.y() - 4, 8, 8).adjusted(-clearance, -clearance, clearance, clearance)

    def symbol_ink_rect(self):
        return QRectF(self._label_anchor.x() - 4, self._label_anchor.y() - 4, 8, 8)

    def sync_label(self):
        self._label_content = present_route_label(self._model, self.route)
        self._label_anchor = self._path.pointAtPercent(0.5)
        graphics = _graphics_extensions(self.route)
        self._label_preferred_position = self._label_anchor + QPointF(
            _number(graphics, "label_x", 0.0), _number(graphics, "label_y", -24.0)
        )
        self._label.set_editable(self._editable)
        self._label.setBrush(QColor(COLORS["text"]))

    def set_target_feedback(self, feedback: ConnectionTargetFeedback | str) -> None:
        normalized = (
            feedback
            if isinstance(feedback, ConnectionTargetFeedback)
            else ConnectionTargetFeedback(feedback)
        )
        if normalized != self._target_feedback:
            self._target_feedback = normalized
            self.update()

    def set_render_context(self, context: DiagramRenderContext) -> None:
        if context != self._render_context:
            self._render_context = context
            self.update()

    def _node_ids(self) -> tuple[ElectricalNodeId, ...]:
        if self.route.electrical_node_id is not None:
            return (self.route.electrical_node_id,)
        return tuple(dict.fromkeys((
            self.route.start_anchor.electrical_node_id,
            self.route.end_anchor.electrical_node_id,
        )))

    def _nominal_voltage_v(self) -> int | None:
        if self.route.electrical_node_id is not None:
            return _resolved_nominal_voltage(
                self._model,
                self._render_context,
                self.route.electrical_node_id,
            )
        port_ids = tuple(
            port_id
            for port_id in (
                self.route.start_anchor.branch_port_id,
                self.route.end_anchor.branch_port_id,
            )
            if port_id is not None
        )
        return _common_nominal_voltage(
            self._model,
            self._render_context,
            port_ids,
        )

    def _energization(self) -> Energization:
        return _aggregate_energization(
            _node_energization(self._render_context, node_id)
            for node_id in self._node_ids()
        )

    def _availability(self) -> EquipmentAvailability | None:
        return _equipment_availability(
            self._render_context,
            self.route.equipment_id,
        )

    def _disconnected(self) -> bool:
        return _equipment_disconnected(
            self._render_context,
            self.route.equipment_id,
        )

    def update_from(
        self,
        route: DiagramRoute,
        model: ElectricalModel,
        *,
        editable: bool,
        developer_overlay: bool,
    ) -> None:
        self.prepareGeometryChange()
        self.route = route
        self._model = model
        self._editable = editable
        self._display_vertices = _route_vertices(route)
        self._path = _path_from_vertices(self._display_vertices)
        self._display_path = QPainterPath(self._path)
        self._bridge_nodes = ()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton
        )
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if editable
            else Qt.CursorShape.ArrowCursor
        )
        self._debug.setText(self._debug_text())
        bounds = self._path.boundingRect()
        self._debug.setPos(bounds.left(), bounds.top() - 42.0)
        self._debug.setVisible(developer_overlay)
        self._diagnostic_badge.setPos(bounds.center().x(), bounds.top() - 12.0)
        self.sync_label()
        self._sync_waypoint_handles()
        self._sync_segment_handles()
        self._sync_endpoint_handles()
        self.setToolTip(self._tooltip())
        self.update()

    def set_diagnostics(self, diagnostics: Iterable[object]) -> None:
        rows = tuple(diagnostics)
        if not rows:
            self._diagnostic_badge.setVisible(False)
            self._diagnostic_badge.setToolTip("")
            return
        severities = {
            str(getattr(getattr(row, "severity", "warning"), "value", getattr(row, "severity", "warning")))
            for row in rows
        }
        self._diagnostic_badge.setBrush(
            QColor(COLORS["red"] if "error" in severities else COLORS["amber"])
        )
        self._diagnostic_badge.setText("!" if len(rows) == 1 else f"! {len(rows)}")
        self._diagnostic_badge.setToolTip("\n\n".join(
            str(getattr(row, "message", row))
            + (
                "\nЧто сделать: " + str(getattr(row, "action", ""))
                if str(getattr(row, "action", "")).strip()
                else ""
            )
            for row in rows
        ))
        self._diagnostic_badge.setVisible(True)

    def set_adapter_diagnostics(
        self,
        diagnostics: Iterable[object],
        *,
        available: bool,
    ) -> None:
        if not available:
            status = "данные адаптера не переданы"
        else:
            object_ids = {
                self.route.equipment_id.value
                if self.route.equipment_id is not None
                else "",
                self.route.electrical_node_id.value
                if self.route.electrical_node_id is not None
                else "",
            }
            matching = tuple(
                row
                for row in diagnostics
                if str(getattr(row, "object_id", "")) in object_ids
            )
            errors = tuple(
                row
                for row in matching
                if str(getattr(row, "severity", "")).lower() == "error"
            )
            if errors:
                status = "заблокирован — " + "; ".join(
                    str(getattr(row, "message", getattr(row, "code", "ошибка")))
                    for row in errors
                )
            elif matching:
                status = "есть предупреждения — " + "; ".join(
                    str(getattr(row, "message", getattr(row, "code", "предупреждение")))
                    for row in matching
                )
            else:
                status = "ошибок адаптации для объекта нет"
        if status != self._adapter_status:
            self._adapter_status = status
            self._debug.setText(self._debug_text())

    def _sync_endpoint_handles(self) -> None:
        physical = (
            self.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
            and _is_directional_line(self._model, self.route.equipment_id)
            and len(self.route.waypoints) >= 2
        )
        wanted = {
            True: self.route.start_anchor.branch_port_id,
            False: self.route.end_anchor.branch_port_id,
        } if physical else {}
        for at_start in tuple(self._endpoint_handles):
            if at_start not in wanted or wanted[at_start] is None:
                handle = self._endpoint_handles.pop(at_start)
                handle.setParentItem(None)
                if handle.scene() is not None:
                    handle.scene().removeItem(handle)
        for at_start, port_id in wanted.items():
            if port_id is None:
                continue
            handle = self._endpoint_handles.get(at_start)
            if handle is None:
                handle = RouteEndpointHandle(at_start, self)
                self._endpoint_handles[at_start] = handle
            point = (
                self.route.waypoints[0]
                if at_start
                else self.route.waypoints[-1]
            )
            handle.setPos(point.x, point.y)
            handle.setVisible(self._editable and self.isSelected())

    def _sync_waypoint_handles(self) -> None:
        wanted = {
            item.id: item
            for item in self.route.waypoints
            if item.source is RouteWaypointSource.USER
        }
        for waypoint_id in tuple(self._waypoint_handles):
            if waypoint_id not in wanted:
                handle = self._waypoint_handles.pop(waypoint_id)
                handle.setParentItem(None)
                if handle.scene() is not None:
                    handle.scene().removeItem(handle)
        for waypoint_id, waypoint in wanted.items():
            handle = self._waypoint_handles.get(waypoint_id)
            if handle is None:
                handle = RouteWaypointHandle(waypoint, self)
                self._waypoint_handles[waypoint_id] = handle
            else:
                handle.update_waypoint(waypoint)
            handle.setVisible(self._editable and self.isSelected())

    def _sync_segment_handles(self) -> None:
        vertices = _route_vertices(self.route)
        wanted: dict[int, tuple[bool, QPointF]] = {}
        # Первый и последний сегменты привязаны к электрическим окончаниям.
        # Их не двигаем напрямую: для них используется переподключение порта.
        for index in range(1, max(1, len(vertices) - 2)):
            first, second = vertices[index], vertices[index + 1]
            horizontal = math.isclose(first.y, second.y, abs_tol=1e-9)
            vertical = math.isclose(first.x, second.x, abs_tol=1e-9)
            if not (horizontal ^ vertical):
                continue
            wanted[index] = (
                horizontal,
                QPointF((first.x + second.x) / 2.0, (first.y + second.y) / 2.0),
            )
        for index in tuple(self._segment_handles):
            if index not in wanted:
                handle = self._segment_handles.pop(index)
                handle.setParentItem(None)
                if handle.scene() is not None:
                    handle.scene().removeItem(handle)
        for index, (horizontal, midpoint) in wanted.items():
            handle = self._segment_handles.get(index)
            if handle is None:
                handle = RouteSegmentHandle(index, horizontal, midpoint, self)
                self._segment_handles[index] = handle
            else:
                handle.update_segment(horizontal, midpoint)
            handle.setVisible(self._editable and self.isSelected())

    def itemChange(self, change, value):  # noqa: N802
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            for handle in getattr(self, "_waypoint_handles", {}).values():
                handle.setVisible(self._editable and bool(value))
            for handle in getattr(self, "_segment_handles", {}).values():
                handle.setVisible(self._editable and bool(value))
            for handle in getattr(self, "_endpoint_handles", {}).values():
                handle.setVisible(self._editable and bool(value))
        return result

    def add_user_waypoint(self, position: QPointF) -> None:
        """Задать ручной изгиб; постоянная фиксация — отдельная команда."""
        base = _route_vertices(self.route)
        if len(base) < 2:
            return

        def distance_sq(index: int) -> float:
            first, second = base[index], base[index + 1]
            if math.isclose(first.y, second.y, abs_tol=1e-9):
                low, high = sorted((first.x, second.x))
                x = min(max(position.x(), low), high)
                return (position.x() - x) ** 2 + (position.y() - first.y) ** 2
            low, high = sorted((first.y, second.y))
            y = min(max(position.y(), low), high)
            return (position.x() - first.x) ** 2 + (position.y() - y) ** 2

        segment_index = min(range(len(base) - 1), key=distance_sq)
        manual: list[RouteVertex] = []
        insertion_index = 0
        for index, waypoint in enumerate(self.route.waypoints[1:-1], start=1):
            if waypoint.source is not RouteWaypointSource.USER:
                continue
            if index <= segment_index:
                insertion_index += 1
            manual.append(
                RouteVertex(
                    waypoint.x,
                    waypoint.y,
                    RouteVertexSource.USER,
                    pinned=True,
                )
            )
        added = RouteVertex(
            position.x(), position.y(), RouteVertexSource.USER, pinned=True
        )
        manual.insert(insertion_index, added)
        vertices = build_orthogonal_route(
            RoutingRequest(base[0], base[-1], manual_vertices=tuple(manual))
        )
        waypoint_id = RouteWaypointId.new()
        scene = self.scene()
        if isinstance(scene, DiagramGraphicsScene):
            scene.routeWaypointEditRequested.emit(
                self.route.id,
                self._persist_vertices(
                    vertices,
                    preferred_user_ids={(added.x, added.y): waypoint_id},
                ),
            )

    def _vertices_for_segment_move(
        self, segment_index: int, position: QPointF
    ) -> tuple[RouteVertex, ...]:
        vertices = list(_route_vertices(self.route))
        if not (0 < segment_index < len(vertices) - 2):
            return tuple(vertices)
        first, second = vertices[segment_index], vertices[segment_index + 1]
        if math.isclose(first.y, second.y, abs_tol=1e-9):
            first = RouteVertex(first.x, position.y(), RouteVertexSource.USER, True)
            second = RouteVertex(second.x, position.y(), RouteVertexSource.USER, True)
        elif math.isclose(first.x, second.x, abs_tol=1e-9):
            first = RouteVertex(position.x(), first.y, RouteVertexSource.USER, True)
            second = RouteVertex(position.x(), second.y, RouteVertexSource.USER, True)
        else:
            return tuple(vertices)
        vertices[segment_index] = first
        vertices[segment_index + 1] = second
        return normalize_route(vertices)

    def preview_segment_move(self, segment_index: int, position: QPointF) -> None:
        self.set_temporary_vertices(self._vertices_for_segment_move(segment_index, position))

    def commit_segment_move(self, segment_index: int, position: QPointF) -> None:
        vertices = self._vertices_for_segment_move(segment_index, position)
        preferred: dict[tuple[float, float], RouteWaypointId] = {}
        if 0 < segment_index < len(self.route.waypoints) - 2:
            for vertex, waypoint in zip(
                vertices[segment_index:segment_index + 2],
                self.route.waypoints[segment_index:segment_index + 2],
            ):
                preferred[(vertex.x, vertex.y)] = waypoint.id
        scene = self.scene()
        if isinstance(scene, DiagramGraphicsScene):
            scene.routeWaypointEditRequested.emit(
                self.route.id,
                self._persist_vertices(vertices, preferred_user_ids=preferred),
            )

    def _vertices_for_user_waypoint(
        self,
        waypoint_id: RouteWaypointId | None,
        position: QPointF | None,
        *,
        remove: bool = False,
    ) -> tuple[RouteVertex, ...]:
        base = _route_vertices(self.route)
        if len(base) < 2:
            return base
        manual: list[RouteVertex] = []
        for waypoint in self.route.waypoints[1:-1]:
            if waypoint.source is not RouteWaypointSource.USER:
                continue
            if waypoint.id == waypoint_id and remove:
                continue
            x = position.x() if waypoint.id == waypoint_id and position is not None else waypoint.x
            y = position.y() if waypoint.id == waypoint_id and position is not None else waypoint.y
            manual.append(RouteVertex(x, y, RouteVertexSource.USER, pinned=True))
        return build_orthogonal_route(
            RoutingRequest(base[0], base[-1], manual_vertices=tuple(manual))
        )

    def preview_user_waypoint(
        self, waypoint_id: RouteWaypointId, position: QPointF
    ) -> None:
        self.set_temporary_vertices(
            self._vertices_for_user_waypoint(waypoint_id, position)
        )

    def clear_temporary_route(self, *, refresh_bridges: bool = True) -> None:
        self.set_temporary_vertices(_route_vertices(self.route), refresh_bridges=refresh_bridges)

    def set_temporary_vertices(
        self, vertices: tuple[RouteVertex, ...], *, refresh_bridges: bool = True
    ) -> None:
        self.prepareGeometryChange()
        self._display_vertices = vertices
        self._path = _path_from_vertices(vertices)
        self._display_path = QPainterPath(self._path)
        self._bridge_nodes = ()
        self.sync_label()
        self.update()
        scene = self.scene()
        if refresh_bridges and isinstance(scene, DiagramGraphicsScene) and not scene._syncing:
            scene._refresh_route_bridges()

    def set_bridge_display(self, path: QPainterPath, nodes: tuple[QPointF, ...]) -> None:
        if self._display_path == path and self._bridge_nodes == nodes:
            return
        self.prepareGeometryChange()
        self._display_path = path
        self._bridge_nodes = nodes
        self.update()

    def _persist_vertices(
        self,
        vertices: tuple[RouteVertex, ...],
        *,
        preferred_user_ids: Mapping[tuple[float, float], RouteWaypointId] | None = None,
    ) -> tuple[RouteWaypoint, ...]:
        source_user_by_point = {
            (item.x, item.y): item
            for item in self.route.waypoints
            if item.source is RouteWaypointSource.USER
        }
        preferred_user_ids = preferred_user_ids or {}
        original_by_id = {item.id: item for item in self.route.waypoints}
        result: list[RouteWaypoint] = []
        for vertex in vertices:
            source = source_user_by_point.get((vertex.x, vertex.y))
            identifier = preferred_user_ids.get((vertex.x, vertex.y),
                source.id if source is not None else RouteWaypointId.new())
            original = original_by_id.get(identifier)
            result.append(
                RouteWaypoint(
                    identifier,
                    vertex.x,
                    vertex.y,
                    RouteWaypointSource.USER
                    if vertex.source is RouteVertexSource.USER
                    else RouteWaypointSource.AUTOMATIC,
                    original.pinned if original is not None else False,
                )
            )
        return tuple(result)

    def commit_user_waypoint(
        self, waypoint_id: RouteWaypointId, position: QPointF
    ) -> None:
        vertices = self._vertices_for_user_waypoint(waypoint_id, position)
        scene = self.scene()
        if isinstance(scene, DiagramGraphicsScene):
            scene.routeWaypointEditRequested.emit(
                self.route.id,
                self._persist_vertices(
                    vertices,
                    preferred_user_ids={(position.x(), position.y()): waypoint_id},
                ),
            )

    def remove_user_waypoint(self, waypoint_id: RouteWaypointId) -> None:
        vertices = self._vertices_for_user_waypoint(
            waypoint_id, None, remove=True
        )
        scene = self.scene()
        if isinstance(scene, DiagramGraphicsScene):
            scene.routeWaypointEditRequested.emit(
                self.route.id, self._persist_vertices(vertices)
            )

    def _debug_text(self) -> str:
        route = self.route
        rows = [
            f"Идентификатор графической трассы: {route.id.value}"
        ]
        rows.append(
            "Вид: "
            + (
                "физическая ветвь"
                if route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
                else "соединение одного узла"
            )
        )
        rows.append(f"Узел начала: {route.start_anchor.electrical_node_id.value}")
        rows.append(f"Узел конца: {route.end_anchor.electrical_node_id.value}")
        if route.equipment_id is not None:
            rows.append(
                f"Идентификатор физической ветви: {route.equipment_id.value}"
            )
            section = self._model.line_sections.get(route.equipment_id)
            if section is not None:
                rows.append(
                    f"Идентификатор логической линии: "
                    f"{section.logical_line_id.value}"
                )
                rows.append(
                    "Физическая длина: "
                    + (
                        f"{section.length_mm / 1_000_000.0:.6g} км"
                        if section.length_mm is not None
                        else "не подтверждена"
                    )
                )
                rows.append(
                    "Конструктивные участки: "
                    + ", ".join(item.id.value for item in section.construction_segments)
                )
                confirmation_labels = {
                    DataConfirmation.CONFIRMED: "подтверждено",
                    DataConfirmation.UNCONFIRMED: "не подтверждено",
                }
                length_states = ", ".join(
                    confirmation_labels[item.length_confirmation]
                    for item in section.construction_segments
                )
                impedance_states = ", ".join(
                    confirmation_labels[item.impedance_confirmation]
                    for item in section.construction_segments
                )
                rows.append(f"Подтверждённость длины: {length_states}")
                rows.append(f"Подтверждённость сопротивлений: {impedance_states}")
                line = self._model.logical_lines.get(section.logical_line_id)
                if line is not None and line.feeder_id is not None:
                    rows.append(f"Фидер: {line.feeder_id.value}")
        elif route.electrical_node_id is not None:
            count = sum(
                item.electrical_node_id == route.electrical_node_id
                for item in self._model.connections.values()
            )
            rows.append(
                f"Идентификатор электрического узла: "
                f"{route.electrical_node_id.value}"
            )
            rows.append(f"Число присоединений узла: {count}")
        rows.append(f"Статус расчётного адаптера: {self._adapter_status}")
        return "\n".join(rows)

    def _tooltip(self) -> str:
        if self.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH:
            equipment = self._model.equipment.get(self.route.equipment_id)
            direction = ("Стрелка: начало → конец линии, не расчётное направление тока\n"
                         if _is_directional_line(self._model, self.route.equipment_id) else "")
            return (
                f"Физическая линия: {equipment.name if equipment else self.route.id.value}\n"
                f"{direction}"
                "ПКМ — действия с линией"
            )
        return "Графическое соединение одного электрического узла"

    def direction_marker(self) -> SymbolPrimitive | None:
        """Place one role-aware arrow on straight visible ink, not a bridge/gap."""
        if (self.route.kind is not DiagramRouteKind.EQUIPMENT_BRANCH
                or not _is_directional_line(self._model, self.route.equipment_id)):
            return None
        port_ids = (self.route.start_anchor.branch_port_id, self.route.end_anchor.branch_port_id)
        if any(port_id not in self._model.ports for port_id in port_ids):
            return None
        roles = tuple(self._model.port_definition(port_id).role for port_id in port_ids)
        if roles not in (("from", "to"), ("to", "from")):
            return None
        return visible_line_direction_marker(
            self._display_path, reverse=roles[0] == "to", nodes=self._bridge_nodes,
        )

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._path.boundingRect().united(self._display_path.boundingRect()).adjusted(-8.0, -8.0, 8.0, 8.0)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(14.0)
        return stroker.createStroke(self._path)

    def _conductor_base_width(self) -> float:
        width = 2.1 if self.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH else 1.7
        if self._target_feedback in {ConnectionTargetFeedback.COMPATIBLE, ConnectionTargetFeedback.INCOMPATIBLE}:
            width = 3.0
        return width

    def conductor_ink_half_width(self) -> float:
        """Actual cosmetic round-cap reach, including selection highlighting."""
        width = self._conductor_base_width()
        return max(width + (0.9 if self.isSelected() else 0.0),
                   width + (4.0 if getattr(self, "_physical_owner_selected", False) else 0.0)) / 2.0

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        width = self._conductor_base_width()
        color = QColor(_state_stroke(
            self._nominal_voltage_v(),
            self._render_context,
            energization=self._energization(),
            availability=self._availability(),
            disconnected=self._disconnected(),
        ))
        if self._target_feedback is ConnectionTargetFeedback.COMPATIBLE:
            color = QColor("#22C55E")
        elif self._target_feedback is ConnectionTargetFeedback.INCOMPATIBLE:
            color = QColor("#EF4444")
        pen = QPen(color, width)
        if (
            self._availability() is EquipmentAvailability.OUT_OF_SERVICE
            or self._disconnected()
        ):
            pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if self.isSelected():
            pen.setWidthF(width + 0.9)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if getattr(self, "_physical_owner_selected", False):
            painter.save()
            halo = QPen(QColor(37, 99, 235, 100), width + 4.0)
            halo.setCosmetic(True)
            painter.setPen(halo)
            painter.drawPath(self._display_path)
            painter.restore()
        painter.drawPath(self._display_path)
        marker = self.direction_marker()
        if marker is not None:
            painter.save()
            arrow_pen = QPen(pen)
            arrow_pen.setStyle(Qt.PenStyle.SolidLine)
            arrow_pen.setWidthF(width * marker.stroke_scale)
            painter.setPen(arrow_pen)
            painter.drawPolyline(QPolygonF([QPointF(*point) for point in marker.points]))
            painter.restore()
        if self._bridge_nodes:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#111111"))
            for point in self._bridge_nodes:
                painter.drawEllipse(point, 2.6, 2.6)


class ConnectionPreviewItem(QGraphicsObject):
    """Облегчённая временная трасса; никогда не входит в DiagramDocument."""

    def __init__(self):
        super().__init__()
        self._vertices: tuple[RouteVertex, ...] = ()
        self._path = QPainterPath()
        self._feedback = ConnectionTargetFeedback.NEUTRAL
        self._endpoint_kind: ConnectionTargetKind | None = None
        self._junction_orientation: str | None = None
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(1_000.0)
        self.setVisible(False)

    def set_preview(
        self,
        vertices: tuple[RouteVertex, ...],
        feedback: ConnectionTargetFeedback = ConnectionTargetFeedback.NEUTRAL,
        endpoint_kind: ConnectionTargetKind | None = None,
        junction_orientation: str | None = None,
    ) -> None:
        self.prepareGeometryChange()
        self._vertices = tuple(vertices)
        self._path = _path_from_vertices(self._vertices)
        self._feedback = feedback
        self._endpoint_kind = endpoint_kind
        self._junction_orientation = (
            junction_orientation
            if junction_orientation in {"horizontal", "vertical"}
            else None
        )
        self.setVisible(bool(self._vertices))
        self.update()

    def clear(self) -> None:
        self.set_preview(())

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._path.boundingRect().adjusted(-24.0, -24.0, 24.0, 24.0)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        del option, widget
        color = {
            ConnectionTargetFeedback.NEUTRAL: QColor(COLORS["blue"]),
            ConnectionTargetFeedback.COMPATIBLE: QColor("#22C55E"),
            ConnectionTargetFeedback.INCOMPATIBLE: QColor("#EF4444"),
        }[self._feedback]
        pen = QPen(color, 1.8, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._path)
        if self._vertices and self._endpoint_kind in {
            ConnectionTargetKind.FREE,
            ConnectionTargetKind.PHYSICAL_LINE,
            ConnectionTargetKind.BUS,
        }:
            last = self._vertices[-1]
            point = QPointF(last.x, last.y)
            if self._endpoint_kind is ConnectionTargetKind.PHYSICAL_LINE:
                # Явный временный знак будущего разбиения. Он существует
                # только в overlay и никогда не попадает в DiagramDocument.
                split_pen = QPen(color, 2.4)
                split_pen.setCosmetic(True)
                painter.setPen(split_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                gap, reach = 7.0, 20.0
                if self._junction_orientation == "vertical":
                    painter.drawLine(
                        QPointF(point.x(), point.y() - reach),
                        QPointF(point.x(), point.y() - gap),
                    )
                    painter.drawLine(
                        QPointF(point.x(), point.y() + gap),
                        QPointF(point.x(), point.y() + reach),
                    )
                else:
                    painter.drawLine(
                        QPointF(point.x() - reach, point.y()),
                        QPointF(point.x() - gap, point.y()),
                    )
                    painter.drawLine(
                        QPointF(point.x() + gap, point.y()),
                        QPointF(point.x() + reach, point.y()),
                    )
                painter.setPen(QPen(color, 1.4))
                painter.drawEllipse(point, 8.5, 8.5)
                painter.setBrush(color)
            else:
                painter.setPen(QPen(color, 1.4))
                painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(point, 4.5, 4.5)


class DiagramGraphicsScene(QGraphicsScene):
    """Scene с выбором, групповым перемещением и безопасными командами."""

    representationSelectionChanged = Signal(object)
    routeSelectionChanged = Signal(object)
    moveRequested = Signal(object, float, float, bool)
    equipmentPlacementRequested = Signal(object)
    labelMoveRequested = Signal(object, float, float)
    copyRequested = Signal(object)
    pasteRequested = Signal()
    duplicateRequested = Signal(object)
    deleteRequested = Signal(object)
    removeFromPageRequested = Signal(object)
    undoRequested = Signal()
    redoRequested = Signal()
    placementCancelled = Signal()
    connectionDraftRequested = Signal(object)
    connectionDragDraftRequested = Signal(object, object)
    physicalLineDraftRequested = Signal(object)
    connectionStatusMessage = Signal(str)
    routeContextActionRequested = Signal(str, object)
    routeDeleteRequested = Signal(object)
    equipmentContextActionRequested = Signal(str, object)
    routeWaypointEditRequested = Signal(object, object)
    routeSegmentMoveRequested = Signal(object, int, float, float)
    busAttachmentMoveRequested = Signal(object, bool, float)
    propertiesRequested = Signal(object)
    linkedPageRequested = Signal(object)
    rotateRequested = Signal(object, int)
    rotationPositionRequested = Signal(object, int)
    autoOrientationRequested = Signal(object)
    routeLabelMoveRequested = Signal(object, float, float)
    toolStateChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSceneRect(-SCENE_LIMIT, -SCENE_LIMIT, SCENE_LIMIT * 2.0, SCENE_LIMIT * 2.0)
        self.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self._document: DiagramDocument | None = None
        self._model: ElectricalModel | None = None
        self._page_id: PageId | None = None
        self._operating_state_id: OperatingStateId | None = None
        self._topology_engine = TopologyEngine()
        self._render_context = DiagramRenderContext()
        self._items_by_id: dict[GraphicalRepresentationId, DiagramObjectItem] = {}
        self._label_options = (True, True, False)
        self._label_zoom = 1.0
        self._bridge_gap_display_state = None
        self._label_conflicts = ()
        self._label_layout_key = None
        self._route_items_by_id: dict[DiagramRouteId, DiagramRouteItem] = {}
        self._bus_attachment_handles: dict[tuple[DiagramRouteId, bool], BusAttachmentHandle] = {}
        self._connected_drag: ConnectedDragGesture | None = None
        self._bus_attachment_preview = None
        self._move_preview = None
        self._route_segment_constraints = None
        self._equipment_move_preview = None
        self._body_attachment_proposal = None
        self._body_preview_angles = {}
        from .equipment_placement_preview import PlacementWirePreview
        self._placement_wire_preview = PlacementWirePreview()
        self.addItem(self._placement_wire_preview)
        self._connection_voltage_preview = None
        self._bus_exit_direction_preview = None
        self._mode = CanvasMode.EDIT
        self._grid_visible = True
        self._snap_enabled = True
        self._grid_size = 20.0
        self._developer_overlay = False
        self._diagnostics: tuple[object, ...] = ()
        self._adapter_diagnostics: tuple[object, ...] = ()
        self._adapter_diagnostics_available = False
        self._tool_state_machine: EditorToolStateMachine | None = None
        self._start_positions: dict[GraphicalRepresentationId, QPointF] = {}
        self._body_press_position: QPointF | None = None
        self._body_explicit_grabber: DiagramObjectItem | None = None
        self._body_start_selection: tuple[GraphicalRepresentationId | DiagramRouteId, ...] = ()
        self._body_drag_threshold_exceeded = False
        self._body_drag_releasing = False
        self._body_drag_previewed = False
        self._body_drag_auxiliary_positions: dict[GraphicalRepresentationId, QPointF] = {}
        self._body_routing_error = ""
        self._body_routing_ids: set[GraphicalRepresentationId] = set()
        self._object_rotation: ObjectRotationGesture | None = None
        self._drag_collision_ids: set[GraphicalRepresentationId] = set()
        self._drag_collision_snapshot: DiagramCollisionService | None = None
        self._syncing = False
        self._connection_tool = ConnectionToolState()
        self._connection_replaced_route_id = None
        self._new_route_occupied_cache = None
        self._connection_press_position: QPointF | None = None
        self._connection_dragged = False
        self._connection_routing_error = ""
        self._physical_routing_error = ""
        self._physical_line_tool = PhysicalLineToolState()
        self._connection_target: ConnectionTarget | None = None
        self._preview_item = ConnectionPreviewItem()
        self.addItem(self._preview_item)
        self._group_selection_overlay = QGraphicsRectItem()
        group_pen = QPen(QColor(COLORS["blue"]), 1.2, Qt.PenStyle.DashLine)
        group_pen.setCosmetic(True)
        self._group_selection_overlay.setPen(group_pen)
        self._group_selection_overlay.setBrush(QColor(37, 99, 235, 8))
        self._group_selection_overlay.setZValue(900.0)
        self._group_selection_overlay.setAcceptedMouseButtons(
            Qt.MouseButton.NoButton
        )
        self._group_selection_overlay.setVisible(False)
        self.addItem(self._group_selection_overlay)
        self._rotation_guide = QGraphicsPathItem()
        self._rotation_guide.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._rotation_guide.setZValue(1190.0)
        self._rotation_guide.setVisible(False)
        self.addItem(self._rotation_guide)
        self._rotation_handle = RotationHandleItem()
        self.addItem(self._rotation_handle)
        self.toolStateChanged.connect(self._rotation_tool_changed)
        self.selectionChanged.connect(self._selection_changed)

    @property
    def page_id(self) -> PageId | None:
        return self._page_id

    @property
    def grid_size(self) -> float:
        return self._grid_size

    @property
    def grid_visible(self) -> bool:
        return self._grid_visible

    @property
    def snap_enabled(self) -> bool:
        return self._snap_enabled

    @property
    def color_mode(self) -> DiagramColorMode:
        return self._render_context.color_mode

    @property
    def topology_snapshot(self) -> TopologySnapshot | None:
        return self._render_context.snapshot

    def selected_representation_ids(self) -> tuple[GraphicalRepresentationId, ...]:
        return tuple(sorted(
            (item.representation_id for item in self.selectedItems() if isinstance(item, DiagramObjectItem)),
            key=lambda item: item.value,
        ))

    def selected_route_ids(self) -> tuple[DiagramRouteId, ...]:
        return tuple(sorted(
            (
                item.route_id
                for item in self.selectedItems()
                if isinstance(item, DiagramRouteItem)
            ),
            key=lambda item: item.value,
        ))

    @property
    def connection_active(self) -> bool:
        return self._connection_tool.active or self._physical_line_tool.active

    @property
    def physical_line_active(self) -> bool:
        return self._physical_line_tool.active

    def endpoint_editing_enabled(self) -> bool:
        """Explicit connection tools reveal real endpoints, including free nodes."""
        explicit_wire_tool = (self._tool_state_machine is not None
                              and self._tool_state_machine.tool is EditorTool.DRAW_CONNECTION)
        explicit_line_tool = any(isinstance(getattr(view, "_placement_payload", None), Mapping)
                                 and view._placement_payload.get("target_kind") == "physical_line"
                                 for view in self.views())
        return self.connection_active or explicit_wire_tool or explicit_line_tool

    def set_mode(self, mode: CanvasMode | str) -> None:
        self.cancel_connection_drag()
        self.cancel_object_drag()
        self.finish_object_rotation(commit=False)
        self._mode = CanvasMode(_mode_value(mode))
        editable = self._mode is CanvasMode.EDIT
        for item in self._items_by_id.values():
            item.set_editable(editable)
        for item in self._route_items_by_id.values():
            item.setAcceptedMouseButtons(
                Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton
            )
            item._label.set_editable(editable)
        for handle in self._bus_attachment_handles.values():
            handle.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if editable else Qt.MouseButton.NoButton)
            handle.setCursor(Qt.CursorShape.SizeAllCursor if editable else Qt.CursorShape.ArrowCursor)
        self._update_rotation_handle()

    def set_grid(self, *, visible: bool | None = None, size: float | None = None) -> None:
        if visible is not None:
            self._grid_visible = bool(visible)
        if size is not None:
            if not math.isfinite(size) or size < 2.0:
                raise ValueError("Шаг сетки должен быть конечным числом не менее 2.")
            self._grid_size = float(size)
        self.invalidate(self.sceneRect(), QGraphicsScene.SceneLayer.BackgroundLayer)

    def set_snap_enabled(self, enabled: bool) -> None:
        self._snap_enabled = bool(enabled)

    def set_color_mode(self, mode: DiagramColorMode | str) -> None:
        normalized = (
            mode
            if isinstance(mode, DiagramColorMode)
            else DiagramColorMode(str(mode))
        )
        if normalized is self._render_context.color_mode:
            return
        self._render_context = DiagramRenderContext(
            snapshot=self._render_context.snapshot,
            color_mode=normalized,
            state_available=self._render_context.state_available,
        )
        for item in self._items_by_id.values():
            item.set_render_context(self._render_context)
        for item in self._route_items_by_id.values():
            item.set_render_context(self._render_context)
        self.update()

    def set_developer_overlay(self, enabled: bool) -> None:
        self._developer_overlay = bool(enabled)
        for item in self._items_by_id.values():
            item._debug.setVisible(self._developer_overlay)
            item.set_service_node_visibility(self._developer_overlay)
        for item in self._route_items_by_id.values():
            item._debug.setVisible(self._developer_overlay)
        self._label_layout_key = None
        self.relayout_labels()

    def set_label_options(self, *, show_labels=True, show_parameters=True, show_results=False) -> None:
        options = (bool(show_labels), bool(show_parameters), bool(show_results))
        if options != self._label_options:
            self._label_options = options
            self._label_layout_key = None
            self.relayout_labels()

    def set_label_zoom(self, zoom: float) -> None:
        changed = not math.isclose(self._label_zoom, float(zoom), rel_tol=1e-12, abs_tol=1e-12)
        if changed:
            self._label_zoom = float(zoom)
        for item in self._label_owners():
            item._label.setVisible(item._label_requested_visible and self._label_zoom >= MIN_LABEL_ZOOM)
        if changed:
            # Store the zoom before rebuilding: the label pass below calls this
            # method again with the same value and must not recurse.
            self._refresh_bus_gap_display_if_needed()

    def _label_owners(self):
        owners = list(self._items_by_id.values())
        represented_equipment = {item.representation.equipment_id for item in owners}
        for item in self._route_items_by_id.values():
            if item.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH and item.route.equipment_id not in represented_equipment:
                owners.append(item)
            else:
                item._label_requested_visible = False
                item._label.setVisible(False)
        return owners

    def label_layout_conflicts(self):
        """Unresolved layout diagnostics, including deliberately pinned overlaps."""
        return self._label_conflicts

    def relayout_labels(self) -> None:
        show_labels, show_parameters, _ = self._label_options
        owners = self._label_owners()
        for item in owners:
            label = item._label
            content = item._label_content
            individual_visible = _boolean(_graphics_extensions(item.representation), "label_visible", True)
            text = content.display_name
            if individual_visible and show_parameters and content.parameter:
                text += ("\n" if text else "") + content.parameter
            label.setText(wrapped_label_text(label, text) if individual_visible else text)
            label.setPos(item._label_preferred_position)
            label.setRotation(-item.rotation())
            item._label_requested_visible = bool(text) and show_labels and individual_visible
            label.setToolTip(content.tooltip or text)
        self._label_conflicts = layout_labels(owners, self._route_items_by_id.values())
        by_id = {row.representation_id: row.manual for row in self._label_conflicts}
        for item in owners:
            if item.representation_id in by_id:
                explanation = (
                    "Ручное положение сохранено; доступна команда «Автоподписи»."
                    if by_id[item.representation_id]
                    else "Не найдено свободное место рядом. Переместите подпись вручную."
                )
                item._label.setToolTip(item._label.toolTip() + "\nПодпись пересекает схему. " + explanation)
        self.set_label_zoom(self._label_zoom)
        self.update()

    def sync_document(
        self,
        document: DiagramDocument,
        model: ElectricalModel,
        *,
        page_id: PageId | None = None,
        operating_state_id: OperatingStateId | None = None,
        topology_state_available: bool = True,
    ) -> None:
        """Синхронизировать только добавленные/изменённые представления."""

        if not isinstance(document, DiagramDocument) or not isinstance(model, ElectricalModel):
            raise TypeError("Холст принимает DiagramDocument и ElectricalModel.")
        # A reload/page switch must never adopt uncommitted scene coordinates.
        # The synchronization below performs the one complete display rebuild.
        self.cancel_connection(announce=False)
        self.cancel_physical_line(announce=False)
        self.cancel_object_drag(refresh=False)
        self.finish_object_rotation(commit=False)
        if page_id is None:
            if self._page_id in document.pages:
                page_id = self._page_id
            else:
                ordered = sorted(document.pages.values(), key=lambda page: (page.order, page.id.value))
                page_id = ordered[0].id if ordered else None
        if page_id is not None and page_id not in document.pages:
            raise KeyError(f"Страница '{page_id}' отсутствует в документе схемы.")

        try:
            snapshot = self._topology_engine.compile(
                model,
                operating_state_id if topology_state_available else None,
            )
        except TopologyError:
            snapshot = None
        render_context = DiagramRenderContext(
            snapshot=snapshot,
            color_mode=self._render_context.color_mode,
            state_available=bool(topology_state_available),
        )

        selected = {item.value for item in self.selected_representation_ids()}
        wanted = {
            item.id: item
            for item in document.representations.values()
            if page_id is not None and item.page_id == page_id
        }
        self._syncing = True
        try:
            # Contact handles are Qt children of the bus representation. Drop
            # our registry entries while those parents are still alive; once
            # a removed parent wrapper is released, Qt deletes its children.
            removed = set(self._items_by_id) - set(wanted)
            for key, handle in tuple(self._bus_attachment_handles.items()):
                owner = handle.parentItem()
                if owner is not None and owner.representation_id in removed:
                    self._remove_bus_attachment_handle(key)
            for representation_id in tuple(self._items_by_id):
                if representation_id not in wanted:
                    item = self._items_by_id.pop(representation_id)
                    self.removeItem(item)
            editable = self._mode is CanvasMode.EDIT
            for representation_id, representation in wanted.items():
                item = self._items_by_id.get(representation_id)
                if item is None:
                    item = DiagramObjectItem(
                        representation,
                        model,
                        editable=editable,
                        developer_overlay=self._developer_overlay,
                        operating_state_id=operating_state_id,
                    )
                    self._items_by_id[representation_id] = item
                    self.addItem(item)
                elif (
                    item.representation != representation
                    or item._model is not model
                    or item._target_state != _target_signature(representation, model)
                    or item._operating_state_id != operating_state_id
                    or item._effective_position
                    != _effective_equipment_position(
                        model.equipment.get(representation.equipment_id)
                        if representation.equipment_id is not None
                        else None,
                        model,
                        operating_state_id,
                    )
                ):
                    item.update_from(
                        representation,
                        model,
                        editable=editable,
                        developer_overlay=self._developer_overlay,
                        operating_state_id=operating_state_id,
                    )
                    # update_from resets the text anchor. Even a mode-only
                    # refresh with unchanged label content must reapply layout.
                    self._label_layout_key = None
                else:
                    item.set_editable(editable)
                item.set_render_context(render_context)
                item.set_service_node_visibility(self._developer_overlay)
                item.setSelected(representation_id.value in selected)

            wanted_routes = {
                item.id: item
                for item in document.routes.values()
                if page_id is not None and item.page_id == page_id
            }
            for route_id in tuple(self._route_items_by_id):
                if route_id not in wanted_routes:
                    item = self._route_items_by_id.pop(route_id)
                    self.removeItem(item)
            for route_id, route in wanted_routes.items():
                route_item = self._route_items_by_id.get(route_id)
                if route_item is None:
                    route_item = DiagramRouteItem(
                        route,
                        model,
                        editable=editable,
                        developer_overlay=self._developer_overlay,
                    )
                    self._route_items_by_id[route_id] = route_item
                    self.addItem(route_item)
                elif route_item.route != route or route_item._model is not model:
                    route_item.update_from(
                        route,
                        model,
                        editable=editable,
                        developer_overlay=self._developer_overlay,
                    )
                route_item.set_render_context(render_context)
                route_item.sync_label()
                route_item._label.set_editable(editable)
            self._refresh_route_bridges()
        finally:
            self._syncing = False
        self._document = document
        self._model = model
        self._page_id = page_id
        self._operating_state_id = operating_state_id
        self._render_context = render_context
        workspace = _mapping(document.extensions.get("stage3_workspace"))
        self._label_options = (
            _boolean(workspace, "show_labels", True),
            _boolean(workspace, "show_parameters", True),
            _boolean(workspace, "show_results", False),
        )
        label_key = (
            tuple((item.representation, item._label_content) for item in self._items_by_id.values()),
            tuple((item.route, item._label_content) for item in self._route_items_by_id.values()),
            self._label_options,
        )
        if label_key != self._label_layout_key:
            self.relayout_labels()
            self._label_layout_key = label_key
        self._update_rotation_handle()
        self.set_diagnostics(self._diagnostics)
        self.set_adapter_diagnostics(
            self._adapter_diagnostics,
            available=self._adapter_diagnostics_available,
        )
        self.update()

    def _remove_bus_attachment_handle(self, key: tuple[DiagramRouteId, bool]) -> None:
        handle = self._bus_attachment_handles.pop(key)
        handle.setParentItem(None)
        if handle.scene() is not None:
            self.removeItem(handle)

    def _refresh_visual_labels_and_connections(self) -> None:
        """Refresh presentation from explicit route anchors, never from proximity."""
        bus_points: dict[GraphicalRepresentationId, list[QPointF]] = {}
        wanted_handles = {}
        for item in self._items_by_id.values():
            if item._canonical_key == "busbar":
                # A hollow bus marker belongs to a persisted endpoint anchor,
                # not every same-net bend that happens to touch the bus axis.
                bus_points[item.representation_id] = []
        for route_item in self._route_items_by_id.values():
            route = route_item.route
            vertices = route_item._display_vertices
            if not vertices:
                continue
            for at_start, anchor, vertex in ((True, route.start_anchor, vertices[0]), (False, route.end_anchor, vertices[-1])):
                item = self._items_by_id.get(anchor.representation_id)
                if item is None or item._canonical_key != "busbar":
                    continue
                if anchor.electrical_node_id not in item._connected_node_ids():
                    continue
                local = item.mapFromScene(QPointF(vertex.x, vertex.y))
                ink = item.symbol_ink_rect().adjusted(-1.0, -1.0, 1.0, 1.0)
                if ink.contains(local):
                    bus_points[item.representation_id].append(local)
                    if anchor.kind is RouteAnchorKind.BUS:
                        wanted_handles[(route.id, at_start)] = (item, local)
        for key in tuple(self._bus_attachment_handles):
            if key not in wanted_handles:
                self._remove_bus_attachment_handle(key)
        occupied = set()
        # Duplicate routes sharing one explicit contact get one visible handle.
        for key in sorted(wanted_handles, key=lambda value: (value[0].value, value[1])):
            item, point = wanted_handles[key]
            handle = self._bus_attachment_handles.get(key)
            if handle is None:
                handle = BusAttachmentHandle(key[0], key[1], item)
                self._bus_attachment_handles[key] = handle
            elif handle.parentItem() is not item:
                handle.setParentItem(item)
            handle.setPos(point)
            coordinate = (item.representation_id, round(point.x(), 5), round(point.y(), 5))
            active = self._connected_drag is not None and self._connected_drag.grabber is handle
            handle.setVisible(coordinate not in occupied or active)
            handle.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if self._mode is CanvasMode.EDIT else Qt.MouseButton.NoButton)
            occupied.add(coordinate)
        for representation_id, points in bus_points.items():
            item = self._items_by_id[representation_id]
            unique = {(round(point.x(), 5), round(point.y(), 5)) for point in points}
            item._bus_junction_points = tuple(QPointF(x, y) for x, y in sorted(unique))
            item.update()

    def _bus_gap_display_parameters(self):
        buses = [item for item in self._items_by_id.values() if item._canonical_key == "busbar"]
        if not buses:
            return None
        scale = max(self._label_zoom, 1e-6)
        cap = max((item.conductor_ink_half_width() for item in self._route_items_by_id.values()), default=0.0)
        for item in self._items_by_id.values():
            if item._canonical_key in {"line", "line_section"}:
                cap = max(cap, max((item._line_width * primitive.stroke_scale / 2.0
                    for primitive in item.symbol_geometry().primitives
                    if primitive.kind in {"line", "polyline"} and not primitive.direction_marker), default=0.0))
        padding = []
        for item in buses:
            geometry = item.symbol_geometry()
            left, top, right, bottom = geometry.ink_bounds()
            half_geometry = min(right - left, bottom - top) / 2.0
            half_stroke = max(item._line_width * primitive.stroke_scale / 2.0
                              for primitive in geometry.primitives)
            # Follow real ink, not the stored hit box or the removed filled
            # band. A cosmetic thin bus stays equally thin at every zoom.
            half_ink = half_geometry + half_stroke / scale
            outer_edge = half_ink * scale
            if item.isSelected():
                ink = item.symbol_ink_rect().adjusted(-5, -5, 5, 5)
                half_frame = min(ink.width(), ink.height()) / 2.0
                outer_edge = max(outer_edge, half_frame * scale + 2.1 / 2.0)
            # Three viewport pixels reserve two fully clear pixels plus the
            # antialiased edge. Cosmetic pen/cap widths do not shrink on zoom-out.
            extra = (outer_edge - half_ink * scale + cap + 3.0) / scale
            padding.append((item.representation_id, (half_ink, extra)))
        return scale, cap, tuple(padding)

    def _refresh_bus_gap_display_if_needed(self) -> None:
        if not self._syncing and self._bus_gap_display_parameters() != self._bridge_gap_display_state:
            self._refresh_route_bridges()

    def _refresh_route_bridges(self) -> None:
        """Cache display-only jumps; never persist them into saved routes."""
        gap_state = self._bus_gap_display_parameters()
        self._bridge_gap_display_state = gap_state
        bus_geometry = dict(gap_state[2]) if gap_state is not None else {}
        wires: list[BridgeWire] = []
        owners: dict[str, tuple[object, int | None]] = {}
        object_paths: dict[GraphicalRepresentationId, dict[int, QPainterPath]] = {
            item.representation_id: {} for item in self._items_by_id.values()
        }
        object_nodes: dict[GraphicalRepresentationId, list[QPointF]] = {
            item.representation_id: [] for item in self._items_by_id.values()
        }
        for route_item in self._route_items_by_id.values():
            route = route_item.route
            key = "route:" + route.id.value
            wires.append(BridgeWire(
                key,
                tuple((point.x, point.y) for point in route_item._display_vertices),
                route.electrical_node_id.value if route.electrical_node_id is not None else None,
                (route.start_anchor.electrical_node_id.value, route.end_anchor.electrical_node_id.value),
            ))
            owners[key] = (route_item, None)
        for item in self._items_by_id.values():
            if item._canonical_key not in {"line", "line_section", "busbar"}:
                continue
            geometry = item.symbol_geometry()
            for primitive_index, primitive in enumerate(geometry.primitives):
                if (primitive.kind not in {"line", "polyline"}
                        or not primitive.voltage_role or primitive.direction_marker):
                    continue
                local_points = primitive.points
                if len(local_points) < 2:
                    continue
                points = tuple(item.mapToScene(QPointF(*point)) for point in local_points)
                endpoint_ids: list[str | None] = []
                for local_point in (local_points[0], local_points[-1]):
                    node_id = None
                    for port_id, port_item in item._port_items.items():
                        if math.isclose(port_item.anchor.x, local_point[0], abs_tol=1e-6) and math.isclose(port_item.anchor.y, local_point[1], abs_tol=1e-6):
                            connection = item._model.connection_for_port(port_id)
                            if connection is not None:
                                node_id = connection.electrical_node_id.value
                            break
                    endpoint_ids.append(node_id)
                node = item.representation.electrical_node_id
                key = f"object:{item.representation_id.value}:{primitive_index}"
                wires.append(BridgeWire(
                    key, tuple((point.x(), point.y()) for point in points),
                    node.value if node is not None else None,
                    (endpoint_ids[0], endpoint_ids[1]),
                    bridge_allowed=item._canonical_key != "busbar",
                    visual_half_width=bus_geometry.get(item.representation_id, (0.0, 2.5))[0],
                    visual_gap_padding=bus_geometry.get(item.representation_id, (0.0, 2.5))[1],
                ))
                owners[key] = (item, primitive_index)
        displays = build_wire_displays(wires)
        # A two-way continuation is a conductor, not an extra schematic dot.
        # Keep true local T/branch joints; the planner has already established
        # electrical compatibility and excluded unconnected crossings.
        rays = {point: set() for display in displays.values() for point in display.junctions}
        for wire in wires:
            for first, second in zip(wire.points, wire.points[1:]):
                horizontal = math.isclose(first[1], second[1], abs_tol=1e-7)
                vertical = math.isclose(first[0], second[0], abs_tol=1e-7)
                for point, directions in rays.items():
                    if horizontal and math.isclose(point[1], first[1], abs_tol=1e-7):
                        low, high = sorted((first[0], second[0]))
                        if low - 1e-7 <= point[0] <= high + 1e-7:
                            if point[0] > low + 1e-7:
                                directions.add("left")
                            if point[0] < high - 1e-7:
                                directions.add("right")
                    elif vertical and math.isclose(point[0], first[0], abs_tol=1e-7):
                        low, high = sorted((first[1], second[1]))
                        if low - 1e-7 <= point[1] <= high + 1e-7:
                            if point[1] > low + 1e-7:
                                directions.add("up")
                            if point[1] < high - 1e-7:
                                directions.add("down")
        for key, display in displays.items():
            item, primitive_index = owners[key]
            path = wire_display_path(display)
            nodes = tuple(QPointF(*point) for point in display.junctions if len(rays[point]) >= 3)
            if primitive_index is None:
                item.set_bridge_display(path, nodes)
            else:
                if display.bridges or display.gaps:
                    object_paths[item.representation_id][primitive_index] = item.mapFromScene(path)
                object_nodes[item.representation_id].extend(item.mapFromScene(point) for point in nodes)
        for item in self._items_by_id.values():
            paths = object_paths[item.representation_id]
            nodes = tuple(object_nodes[item.representation_id])
            if item._bridge_paths != paths or item._bridge_nodes != nodes:
                item.prepareGeometryChange()
                item._bridge_paths = paths
                item._bridge_nodes = nodes
                item.update()
        self._refresh_visual_labels_and_connections()
        if not self._syncing:
            # One B3 presenter/solver owns all text, including live previews.
            # Display arcs are obstacles; electrical waypoints remain unchanged.
            self._label_layout_key = None
            self.relayout_labels()

    def set_diagnostics(self, diagnostics: Iterable[object]) -> None:
        self._diagnostics = tuple(diagnostics)
        by_representation: dict[GraphicalRepresentationId, list[object]] = {}
        by_route: dict[DiagramRouteId, list[object]] = {}
        for diagnostic in self._diagnostics:
            object_id = str(getattr(diagnostic, "object_id", ""))
            route_matched = False
            for route_id, route_item in self._route_items_by_id.items():
                route = route_item.route
                route_targets = {
                    route.id.value,
                    route.target_id.value,
                    route.equipment_id.value if route.equipment_id is not None else "",
                    route.electrical_node_id.value
                    if route.electrical_node_id is not None
                    else "",
                }
                if object_id and object_id in route_targets:
                    by_route.setdefault(route_id, []).append(diagnostic)
                    route_matched = True
                    break
            if route_matched:
                continue
            representation_matched = False
            for representation_id, item in self._items_by_id.items():
                representation = item.representation
                representation_targets = {
                    representation.id.value,
                    representation.target_id.value,
                }
                equipment = (
                    self._model.equipment.get(representation.equipment_id)
                    if self._model is not None
                    and representation.equipment_id is not None
                    else None
                )
                if equipment is not None:
                    representation_targets.update(
                        port_id.value for port_id in equipment.port_ids
                    )
                if object_id and object_id in representation_targets:
                    by_representation.setdefault(
                        representation_id, []
                    ).append(diagnostic)
                    representation_matched = True
            if representation_matched:
                continue
            representation_id = getattr(diagnostic, "representation_id", None)
            if representation_id in self._items_by_id:
                by_representation.setdefault(representation_id, []).append(diagnostic)
        for representation_id, item in self._items_by_id.items():
            item.set_diagnostics(by_representation.get(representation_id, ()))
        for route_id, item in self._route_items_by_id.items():
            item.set_diagnostics(by_route.get(route_id, ()))

    def set_adapter_diagnostics(
        self,
        diagnostics: Iterable[object],
        *,
        available: bool = True,
    ) -> None:
        self._adapter_diagnostics = tuple(diagnostics)
        self._adapter_diagnostics_available = bool(available)
        for item in self._items_by_id.values():
            item.set_adapter_diagnostics(
                self._adapter_diagnostics,
                available=self._adapter_diagnostics_available,
            )
        for item in self._route_items_by_id.values():
            item.set_adapter_diagnostics(
                self._adapter_diagnostics,
                available=self._adapter_diagnostics_available,
            )

    def select_representations(self, ids: object, *, ensure_visible: bool = False) -> None:
        del ensure_visible  # Прокрутку выполняет view после синхронизации дерева.
        wanted = {
            item.value if isinstance(item, GraphicalRepresentationId) else str(item)
            for item in (ids or ())
        }
        self._set_object_selection(
            lambda representation_id, _item: representation_id.value in wanted
        )

    def _set_object_selection(self, predicate: Any) -> None:
        """Изменить массовое выделение одним логическим событием."""

        self._syncing = True
        try:
            self.clearSelection()
            for representation_id, item in self._items_by_id.items():
                item.setSelected(bool(predicate(representation_id, item)))
        finally:
            self._syncing = False
        self._selection_changed()

    def select_same_type(self) -> None:
        selected = [item for item in self.selectedItems() if isinstance(item, DiagramObjectItem)]
        if not selected:
            return
        type_key = selected[0].type_key
        self._set_object_selection(
            lambda _representation_id, item: item.type_key == type_key
        )

    def _selection_changed(self) -> None:
        if self._syncing:
            return
        for route_item in self._route_items_by_id.values():
            owner = self._legacy_physical_line_owner(route_item)
            selected = owner is not None and owner.isSelected()
            if selected != getattr(route_item, "_physical_owner_selected", False):
                route_item._physical_owner_selected = selected
                route_item.update()
        self._refresh_bus_gap_display_if_needed()
        selected_items = [
            item
            for item in self.selectedItems()
            if isinstance(item, DiagramObjectItem)
        ]
        if len(selected_items) > 1:
            rect = selected_items[0].sceneBoundingRect()
            for item in selected_items[1:]:
                rect = rect.united(item.sceneBoundingRect())
            self._group_selection_overlay.setRect(
                rect.adjusted(-8.0, -8.0, 8.0, 8.0)
            )
            self._group_selection_overlay.setToolTip(
                f"Выбрано объектов: {len(selected_items)}"
            )
            self._group_selection_overlay.setVisible(True)
        else:
            self._group_selection_overlay.setVisible(False)
        if self._object_rotation is not None and self.selected_representation_ids() != (self._object_rotation.representation_id,):
            self.finish_object_rotation(commit=False)
        self._update_rotation_handle()
        self.representationSelectionChanged.emit(self.selected_representation_ids())
        self.routeSelectionChanged.emit(self.selected_route_ids())

    def _rotation_tool_changed(self, *_args) -> None:
        if (self._connection_press_position is not None and self._tool_state_machine is not None
                and self._tool_state_machine.tool is not EditorTool.DRAW_CONNECTION):
            self.cancel_connection_drag()
        for item in self._items_by_id.values():
            if getattr(item, "_service_node_hidden", False):
                item.update()
            for port in item._port_items.values():
                port.update()
        if (self._connected_drag is not None and self._tool_state_machine is not None
                and self._tool_state_machine.tool is not EditorTool.DRAG_ROUTE_SEGMENT):
            self.cancel_connected_drag()
        if (
            self._start_positions
            and self._tool_state_machine is not None
            and self._tool_state_machine.tool not in {EditorTool.SELECT, EditorTool.DRAG_OBJECT}
        ):
            self.cancel_object_drag()
        if (
            self._object_rotation is not None
            and self._tool_state_machine is not None
            and self._tool_state_machine.tool is not EditorTool.ROTATE_OBJECT
        ):
            self.finish_object_rotation(commit=False)
        self._update_rotation_handle()

    def _update_rotation_handle(self) -> None:
        ids = self.selected_representation_ids()
        tool = self._tool_state_machine.tool if self._tool_state_machine is not None else EditorTool.SELECT
        enabled = (
            self._mode is CanvasMode.EDIT and len(ids) == 1
            and not self.selected_route_ids() and not self.connection_active
            and tool in {EditorTool.SELECT, EditorTool.ROTATE_OBJECT}
        )
        item = self._items_by_id.get(ids[0]) if enabled else None
        enabled = item is not None and (item.representation.equipment_id is not None or item._canonical_key == "busbar")
        self._rotation_handle.setVisible(enabled)
        self._rotation_guide.setVisible(enabled)
        if not enabled:
            return
        bounds = item.mapRectToScene(item.symbol_ink_rect())
        gap = 22.0 / self._view_scale()
        corner = bounds.topRight()
        grip = corner + QPointF(gap, -gap)
        self._rotation_handle.representation_id = item.representation_id
        self._rotation_handle.setPos(grip)
        guide = QPainterPath(corner)
        guide.lineTo(grip)
        pen = QPen(QColor("#93C5FD"), 1.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self._rotation_guide.setPen(pen)
        self._rotation_guide.setPath(guide)

    def begin_object_rotation(self, representation_id, scene_pos: QPointF) -> bool:
        if self._object_rotation is not None:
            return False
        self._update_rotation_handle()
        if not self._rotation_handle.isVisible() or self._rotation_handle.representation_id != representation_id:
            return False
        item = self._items_by_id.get(representation_id)
        if item is None:
            return False
        if self._tool_state_machine is not None:
            transition = self._tool_state_machine.activate(EditorTool.ROTATE_OBJECT)
            if not transition.accepted:
                return False
        center = item.mapToScene(QPointF())
        offset = scene_pos - center
        self._object_rotation = ObjectRotationGesture(
            representation_id, float(item.rotation()), float(item.rotation()),
            center, QPointF(scene_pos), math.degrees(math.atan2(offset.y(), offset.x())),
        )
        self._start_positions.clear()
        self._drag_collision_snapshot = None
        self._rotation_handle.set_feedback(dragging=True, angle=item.rotation())
        if self._tool_state_machine is not None:
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self.connectionStatusMessage.emit("Тяните маркер: 90° или 180°. Отпустите — применить, Esc — отменить")
        return True

    def preview_object_rotation(self, scene_pos: QPointF) -> None:
        gesture = self._object_rotation
        if gesture is None:
            return
        if self._mode is not CanvasMode.EDIT:
            self.finish_object_rotation(commit=False)
            return
        item = self._items_by_id.get(gesture.representation_id)
        if item is None:
            self.finish_object_rotation(commit=False)
            return
        movement = scene_pos - gesture.start_point
        if not gesture.dragged and math.hypot(movement.x(), movement.y()) * self._view_scale() < 4.0:
            return
        offset = scene_pos - gesture.center
        if math.hypot(offset.x(), offset.y()) * self._view_scale() < 4.0:
            return
        gesture.dragged = True
        pointer_angle = math.degrees(math.atan2(offset.y(), offset.x()))
        delta = (pointer_angle - gesture.start_pointer_angle + 180.0) % 360.0 - 180.0
        target = nearest_editor_rotation(gesture.original_angle + delta, current=gesture.current_angle)
        if target == gesture.current_angle:
            return
        gesture.current_angle = target
        gesture.blocked = self.rotation_collision(gesture.representation_id, target) is not None
        item.setRotation(target)
        item.set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE if gesture.blocked else ConnectionTargetFeedback.NEUTRAL)
        try:
            previews = [(route_item, self._incident_preview_vertices(route_item.route))
                        for route_item in self._route_items_by_id.values()
                        if gesture.representation_id in {
                            route_item.route.start_anchor.representation_id,
                            route_item.route.end_anchor.representation_id}]
        except RoutingError as exc:
            self.finish_object_rotation(commit=False)
            self.connectionStatusMessage.emit(str(exc))
            return
        for route_item, vertices in previews:
            route_item.set_temporary_vertices(vertices, refresh_bridges=False)
        self._refresh_route_bridges()
        self._rotation_handle.set_feedback(dragging=True, angle=target, blocked=gesture.blocked)
        self._update_rotation_handle()
        self.update()

    def finish_object_rotation(self, *, commit: bool = True) -> bool:
        gesture = self._object_rotation
        if gesture is None:
            return False
        # Restore the display first. A single existing controller command will
        # then commit/route/revalidate atomically, or leave everything untouched.
        self._object_rotation = None
        item = self._items_by_id.get(gesture.representation_id)
        if item is not None:
            item.setRotation(gesture.original_angle)
            item.set_target_feedback(ConnectionTargetFeedback.NEUTRAL)
        for route_item in self._route_items_by_id.values():
            route_item.clear_temporary_route(refresh_bridges=False)
        self._refresh_route_bridges()
        if self._tool_state_machine is not None and self._tool_state_machine.tool is EditorTool.ROTATE_OBJECT:
            self._tool_state_machine.select_tool()
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self._rotation_handle.set_feedback()
        if self.mouseGrabberItem() is self._rotation_handle and not self._rotation_handle._losing_grab:
            self._rotation_handle.ungrabMouse()
        self._update_rotation_handle()
        apply = commit and gesture.dragged and not gesture.blocked and self._mode is CanvasMode.EDIT
        changed = not math.isclose(gesture.original_angle, gesture.current_angle)
        if apply and changed:
            self.rotationPositionRequested.emit((gesture.representation_id,), int(gesture.current_angle))
            return True
        if commit and gesture.blocked:
            self.connectionStatusMessage.emit(ui_text("status.rotation_blocked"))
        elif not commit:
            self.connectionStatusMessage.emit("Поворот отменён; положение и соединения сохранены")
        return False

    def _view_scale(self) -> float:
        if not self.views():
            return 1.0
        return max(abs(float(self.views()[0].transform().m11())), MIN_ZOOM)

    @staticmethod
    def _segment_distance(point: QPointF, first: QPointF, second: QPointF) -> float:
        if math.isclose(first.x(), second.x(), abs_tol=1e-9):
            low, high = sorted((first.y(), second.y()))
            nearest_y = min(max(point.y(), low), high)
            return math.hypot(point.x() - first.x(), point.y() - nearest_y)
        low, high = sorted((first.x(), second.x()))
        nearest_x = min(max(point.x(), low), high)
        return math.hypot(point.x() - nearest_x, point.y() - first.y())

    @classmethod
    def _project_to_route(
        cls,
        route: DiagramRoute,
        point: QPointF,
    ) -> tuple[QPointF, str] | None:
        """Спроецировать курсор на ближайший ортогональный сегмент трассы."""

        rows = tuple(QPointF(item.x, item.y) for item in route.waypoints)
        best: tuple[float, int, QPointF, str] | None = None
        for index, (first, second) in enumerate(zip(rows, rows[1:])):
            distance = cls._segment_distance(point, first, second)
            if math.isclose(first.x(), second.x(), abs_tol=1e-9):
                low, high = sorted((first.y(), second.y()))
                projected = QPointF(first.x(), min(max(point.y(), low), high))
                orientation = "vertical"
            else:
                low, high = sorted((first.x(), second.x()))
                projected = QPointF(min(max(point.x(), low), high), first.y())
                orientation = "horizontal"
            candidate = (distance, index, projected, orientation)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            return None
        return best[2], best[3]

    def _check_connection_voltage(self, first, second):
        if callable(self._connection_voltage_preview):
            return self._connection_voltage_preview(first, second)
        return check_connection_voltage(self._model, first, second)

    @classmethod
    def _tap_entry_direction(cls, route, point, approach) -> RouteDirection:
        """Enter a tapped segment perpendicularly, without overlapping it."""
        projected = cls._project_to_route(route, point)
        if projected is not None and projected[1] == "vertical":
            return RouteDirection.RIGHT if approach.x >= point.x() else RouteDirection.LEFT
        return RouteDirection.DOWN if approach.y >= point.y() else RouteDirection.UP

    def _compatibility_for_port(
        self, source_port_id: PortId, target_port_id: PortId,
        *, allow_line_insertion: bool = False,
    ) -> tuple[ConnectionTargetFeedback, str]:
        assert self._model is not None
        if isinstance(source_port_id, ElectricalNodeId):
            return self._compatibility_for_node(target_port_id, source_port_id,
                                                allow_line_insertion=allow_line_insertion)
        if source_port_id == target_port_id:
            return (
                ConnectionTargetFeedback.INCOMPATIBLE,
                "Порт нельзя соединить с самим собой",
            )
        try:
            source_definition = self._model.port_definition(source_port_id)
            target_definition = self._model.port_definition(target_port_id)
        except Exception as exc:
            return ConnectionTargetFeedback.INCOMPATIBLE, str(exc)
        if source_definition.kind_id != target_definition.kind_id:
            return (
                ConnectionTargetFeedback.INCOMPATIBLE,
                "Электрические типы портов несовместимы",
            )
        voltage = self._check_connection_voltage(source_port_id, target_port_id)
        if not voltage.valid:
            return ConnectionTargetFeedback.INCOMPATIBLE, voltage.message
        first = self._model.connection_for_port(source_port_id)
        second = self._model.connection_for_port(target_port_id)
        if first is not None and second is not None:
            if first.electrical_node_id == second.electrical_node_id:
                if allow_line_insertion:
                    return (
                        ConnectionTargetFeedback.COMPATIBLE,
                        "Уже соединено одним узлом; можно вставить ВЛ или КЛ",
                    )
                return (
                    ConnectionTargetFeedback.INCOMPATIBLE,
                    "Порты уже принадлежат одному электрическому узлу",
                )
            if self._connection_tool.mode is ConnectionToolMode.RECONNECT:
                return (
                    ConnectionTargetFeedback.COMPATIBLE,
                    "Переподключить конец ветви к электрическому узлу порта",
                )
            return (
                ConnectionTargetFeedback.INCOMPATIBLE,
                "Оба порта уже подключены к разным электрическим узлам",
            )
        return ConnectionTargetFeedback.COMPATIBLE, "Подключить к электрическому порту"

    def _compatibility_for_node(
        self, source_port_id: PortId, node_id: ElectricalNodeId,
        *, allow_line_insertion: bool = False,
    ) -> tuple[ConnectionTargetFeedback, str]:
        assert self._model is not None
        if isinstance(source_port_id, ElectricalNodeId):
            voltage = self._check_connection_voltage(source_port_id, node_id)
            if not voltage.valid:
                return ConnectionTargetFeedback.INCOMPATIBLE, voltage.message
            if self._model.electrical_nodes[source_port_id].kind_id != self._model.electrical_nodes[node_id].kind_id:
                return ConnectionTargetFeedback.INCOMPATIBLE, "Электрические типы узлов несовместимы"
            return ConnectionTargetFeedback.COMPATIBLE, "Соединить электрические узлы"
        try:
            definition = self._model.port_definition(source_port_id)
            node = self._model.electrical_nodes[node_id]
        except Exception as exc:
            return ConnectionTargetFeedback.INCOMPATIBLE, str(exc)
        if definition.kind_id != node.kind_id:
            return (
                ConnectionTargetFeedback.INCOMPATIBLE,
                "Электрический тип порта не соответствует узлу",
            )
        voltage = self._check_connection_voltage(source_port_id, node_id)
        if not voltage.valid:
            return ConnectionTargetFeedback.INCOMPATIBLE, voltage.message
        existing = self._model.connection_for_port(source_port_id)
        if existing is not None and existing.electrical_node_id == node_id:
            if allow_line_insertion:
                return (
                    ConnectionTargetFeedback.COMPATIBLE,
                    "Уже соединено одним узлом; можно вставить ВЛ или КЛ",
                )
            return (
                ConnectionTargetFeedback.INCOMPATIBLE,
                "Порт уже подключён к этому электрическому узлу",
            )
        return ConnectionTargetFeedback.COMPATIBLE, "Подключить к электрическому узлу"

    def _project_to_node_item(
        self, item: DiagramObjectItem, scene_pos: QPointF
    ) -> tuple[QPointF, ConnectionTargetKind, str]:
        local = item.mapFromScene(scene_pos)
        bus_like = item._behavior_key == "bus" or "busbar" in item._symbol_key
        if not bus_like:
            return item.mapToScene(QPointF()), ConnectionTargetKind.ELECTRICAL_NODE, ""
        if item._height > item._width:
            y = min(max(local.y(), -item._height / 2.0), item._height / 2.0)
            anchor = item.mapToScene(QPointF(0.0, y))
            fraction = (y + item._height / 2.0) / item._height
        else:
            x = min(max(local.x(), -item._width / 2.0), item._width / 2.0)
            anchor = item.mapToScene(QPointF(x, 0.0))
            fraction = (x + item._width / 2.0) / item._width
        return anchor, ConnectionTargetKind.BUS, f"{fraction:.8f}"

    def _target_at(self, scene_pos: QPointF) -> ConnectionTarget | None:
        if not self._connection_tool.active or self._model is None:
            return None
        source_id = (ElectricalNodeId(self._connection_tool.source_target.target_id)
                     if self._connection_tool.source_target is not None
                     else PortId(self._connection_tool.source_port_id))
        # Жест от вывода может вставить физическую линию в существующий
        # узел. Окончательный смысл выбирается в меню; сам порт и неверный
        # класс напряжения по-прежнему недопустимы до этого выбора.
        allow_line_insertion = not self._connection_tool.from_route_endpoint
        tolerance = (PORT_HIT_TOLERANCE_PX + BUS_HIT_MARGIN_PX) / self._view_scale()
        rect = QRectF(
            scene_pos.x() - tolerance,
            scene_pos.y() - tolerance,
            tolerance * 2.0,
            tolerance * 2.0,
        )
        nearby = self.items(rect)
        port_candidates: list[tuple[float, ElectricalPortItem]] = []
        for item in nearby:
            if isinstance(item, ElectricalPortItem):
                point = item.mapToScene(QPointF())
                distance_px = math.hypot(
                    point.x() - scene_pos.x(), point.y() - scene_pos.y()
                ) * self._view_scale()
                if distance_px <= PORT_HIT_TOLERANCE_PX:
                    port_candidates.append((distance_px, item))
        if port_candidates:
            _, item = min(port_candidates, key=lambda row: (row[0], row[1].port_id.value))
            feedback, message = self._compatibility_for_port(
                source_id, item.port_id, allow_line_insertion=allow_line_insertion,
            )
            point = item.mapToScene(QPointF())
            return ConnectionTarget(
                ConnectionTargetKind.EQUIPMENT_PORT,
                point.x(),
                point.y(),
                item.port_id.value,
                item.representation_id.value,
                item.scene_direction(),
                feedback,
                message,
                anchor_key=item.anchor.role,
            )

        object_candidates = {}
        for raw_item in nearby:
            item: QGraphicsItem | None = raw_item
            while item is not None and not isinstance(item, DiagramObjectItem):
                item = item.parentItem()
            if (
                isinstance(item, DiagramObjectItem)
                and item.representation.electrical_node_id is not None
                and item.representation_id not in object_candidates
            ):
                point, kind, anchor_key = self._project_to_node_item(item, scene_pos)
                distance_px = math.hypot(point.x() - scene_pos.x(), point.y() - scene_pos.y()) * self._view_scale()
                if kind is ConnectionTargetKind.BUS:
                    distance_px = self._bus_band_distance_px(item, scene_pos)
                limit = BUS_HIT_MARGIN_PX if kind is ConnectionTargetKind.BUS else PORT_HIT_TOLERANCE_PX
                if distance_px <= limit + 1e-9:
                    object_candidates[item.representation_id] = (distance_px, item, point, kind, anchor_key)
        if object_candidates:
            _, item, point, kind, anchor_key = min(object_candidates.values(),
                key=lambda row: (row[0], row[1].representation_id.value))
            node_id = item.representation.electrical_node_id
            assert node_id is not None
            feedback, message = self._compatibility_for_node(
                source_id, node_id, allow_line_insertion=allow_line_insertion,
            )
            return ConnectionTarget(
                kind,
                point.x(),
                point.y(),
                node_id.value,
                item.representation_id.value,
                feedback=feedback,
                message=message,
                anchor_key=anchor_key,
            )

        route_candidates: list[tuple[float, DiagramRouteItem, QPointF]] = []
        for raw_item in nearby:
            if not isinstance(raw_item, DiagramRouteItem):
                continue
            points = [QPointF(row.x, row.y) for row in raw_item.route.waypoints]
            if len(points) < 2:
                continue
            for first, second in zip(points, points[1:]):
                distance = self._segment_distance(scene_pos, first, second)
                if distance * self._view_scale() > ROUTE_HIT_TOLERANCE_PX:
                    continue
                if math.isclose(first.x(), second.x(), abs_tol=1e-9):
                    low, high = sorted((first.y(), second.y()))
                    point = QPointF(first.x(), min(max(scene_pos.y(), low), high))
                else:
                    low, high = sorted((first.x(), second.x()))
                    point = QPointF(min(max(scene_pos.x(), low), high), first.y())
                route_candidates.append((distance, raw_item, point))
        if route_candidates:
            _, item, point = min(
                route_candidates, key=lambda row: (row[0], row[1].route_id.value)
            )
            if (
                item.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
                and _is_directional_line(self._model, item.route.equipment_id)
            ):
                from ..editor.connection_tool import ConnectionToolMode

                reconnecting = (
                    self._connection_tool.mode is ConnectionToolMode.RECONNECT
                )
                voltage = self._check_connection_voltage(
                    source_id,
                    self._model.port_by_role(item.route.equipment_id, "from").id,
                )
                return ConnectionTarget(
                    ConnectionTargetKind.PHYSICAL_LINE,
                    point.x(),
                    point.y(),
                    item.route.equipment_id.value,  # type: ignore[union-attr]
                    direction=self._tap_entry_direction(item.route, point,
                        self._connection_tool.manual_vertices[-1] if self._connection_tool.manual_vertices
                        else self._connection_tool.source_vertex),
                    feedback=(ConnectionTargetFeedback.COMPATIBLE if voltage.valid
                              else ConnectionTargetFeedback.INCOMPATIBLE),
                    message=(voltage.message if not voltage.valid else (
                        "Переподключить порт к узлу отпайки"
                        if reconnecting
                        else "Отпустите кнопку, чтобы создать отпайку"
                    )),
                    route_id=item.route.id.value,
                    route_fraction=self._route_fraction(item.route, point),
                )
            if item.route.kind is DiagramRouteKind.NODE_CONNECTION:
                node_id = item.route.electrical_node_id
                assert node_id is not None
                feedback, message = self._compatibility_for_node(
                    source_id, node_id, allow_line_insertion=allow_line_insertion,
                )
                return ConnectionTarget(
                    ConnectionTargetKind.NODE_CONNECTION,
                    point.x(),
                    point.y(),
                    node_id.value,
                    feedback=feedback,
                    message=message,
                    route_id=item.route.id.value,
                    route_fraction=self._route_fraction(item.route, point),
                )
        return None

    def _physical_endpoint_at(self, scene_pos: QPointF) -> ConnectionTarget | None:
        """Найти только явный электрический конец будущей ВЛ/КЛ."""

        if self._model is None:
            return None
        tolerance = (PORT_HIT_TOLERANCE_PX + BUS_HIT_MARGIN_PX) / self._view_scale()
        rect = QRectF(
            scene_pos.x() - tolerance,
            scene_pos.y() - tolerance,
            tolerance * 2.0,
            tolerance * 2.0,
        )
        # This is an explicit physical-line operation, including its first
        # source click before the tool is active. Hidden bookkeeping glyphs
        # must not turn an existing node/port into a newly created FREE end.
        nearby = self.items(rect, Qt.ItemSelectionMode.IntersectsItemBoundingRect)
        candidates: list[tuple[float, ElectricalPortItem]] = []
        for item in nearby:
            if not isinstance(item, ElectricalPortItem):
                continue
            point = item.mapToScene(QPointF())
            distance_px = math.hypot(
                point.x() - scene_pos.x(), point.y() - scene_pos.y()
            ) * self._view_scale()
            if distance_px <= PORT_HIT_TOLERANCE_PX:
                candidates.append((distance_px, item))
        if candidates:
            _, item = min(candidates, key=lambda row: (row[0], row[1].port_id.value))
            point = item.mapToScene(QPointF())
            feedback = ConnectionTargetFeedback.COMPATIBLE
            message = "Подключить физическую линию к электрическому порту"
            source = self._physical_line_tool.source
            if source is not None and source.kind is ConnectionTargetKind.EQUIPMENT_PORT:
                if source.target_id == item.port_id.value:
                    feedback = ConnectionTargetFeedback.INCOMPATIBLE
                    message = "Физическая линия не может завершаться на исходном порту"
                else:
                    feedback, message = self._compatibility_for_port(
                        PortId(source.target_id), item.port_id
                    )
                    # Для новой физической ветви занятые порты допустимы: она
                    # подключается к их существующему электрическому узлу.
                    if "Оба порта уже подключены" in message:
                        first = self._model.connection_for_port(PortId(source.target_id))
                        second = self._model.connection_for_port(item.port_id)
                        if first is not None and second is not None and first.electrical_node_id != second.electrical_node_id:
                            feedback = ConnectionTargetFeedback.COMPATIBLE
                            message = "Соединить существующие электрические узлы физической линией"
            return ConnectionTarget(
                ConnectionTargetKind.EQUIPMENT_PORT,
                point.x(),
                point.y(),
                item.port_id.value,
                item.representation_id.value,
                item.scene_direction(),
                feedback,
                message,
                anchor_key=item.anchor.role,
            )

        object_candidates = {}
        for raw_item in nearby:
            item: QGraphicsItem | None = raw_item
            while item is not None and not isinstance(item, DiagramObjectItem):
                item = item.parentItem()
            if not isinstance(item, DiagramObjectItem) or item.representation.electrical_node_id is None:
                continue
            point, kind, anchor_key = self._project_to_node_item(item, scene_pos)
            distance_px = math.hypot(point.x() - scene_pos.x(), point.y() - scene_pos.y()) * self._view_scale()
            # BSP rectangles only supply candidates. Acceptance is measured
            # from the real endpoint (or the projected bus segment), never
            # from a label/selection box around an invisible bookkeeping node.
            if kind is ConnectionTargetKind.BUS:
                distance_px = self._bus_band_distance_px(item, scene_pos)
            limit = BUS_HIT_MARGIN_PX if kind is ConnectionTargetKind.BUS else PORT_HIT_TOLERANCE_PX
            if distance_px <= limit + 1e-9:
                object_candidates[item.representation_id] = (distance_px, item, point, kind, anchor_key)
        if object_candidates:
            _, item, point, kind, anchor_key = min(object_candidates.values(),
                key=lambda row: (row[0], row[1].representation_id.value))
            node_id = item.representation.electrical_node_id
            assert node_id is not None
            feedback = ConnectionTargetFeedback.COMPATIBLE
            message = "Подключить физическую линию к электрическому узлу"
            source = self._physical_line_tool.source
            if source is not None:
                source_node_id: ElectricalNodeId | None = None
                if source.kind in {
                    ConnectionTargetKind.ELECTRICAL_NODE,
                    ConnectionTargetKind.BUS,
                }:
                    source_node_id = ElectricalNodeId(source.target_id)
                elif source.kind is ConnectionTargetKind.EQUIPMENT_PORT:
                    connection = self._model.connection_for_port(
                        PortId(source.target_id)
                    )
                    source_node_id = (
                        connection.electrical_node_id
                        if connection is not None
                        else None
                    )
                if source_node_id == node_id:
                    feedback = ConnectionTargetFeedback.INCOMPATIBLE
                    message = "Физическая ветвь должна соединять два разных узла"
            return self._available_bus_target(ConnectionTarget(
                kind,
                point.x(),
                point.y(),
                node_id.value,
                item.representation_id.value,
                feedback=feedback,
                message=message,
                anchor_key=anchor_key,
            ))

        # Эквипотенциальная графическая трасса уже представляет один
        # существующий ElectricalNode. Завершение новой физической линии на
        # ней переиспользует этот ID и не разрезает трассу как ВЛ/КЛ.
        node_route_candidates: list[
            tuple[float, DiagramRoute, QPointF]
        ] = []
        for raw_item in nearby:
            if not isinstance(raw_item, DiagramRouteItem):
                continue
            route = raw_item.route
            if (
                route.kind is not DiagramRouteKind.NODE_CONNECTION
                or route.electrical_node_id is None
            ):
                continue
            projected = self._project_to_route(route, scene_pos)
            if projected is None:
                continue
            point, _ = projected
            distance_px = math.hypot(
                point.x() - scene_pos.x(), point.y() - scene_pos.y()
            ) * self._view_scale()
            if distance_px <= PORT_HIT_TOLERANCE_PX:
                node_route_candidates.append((distance_px, route, point))
        if node_route_candidates:
            _, route, point = min(
                node_route_candidates,
                key=lambda row: (row[0], row[1].id.value),
            )
            node_id = route.electrical_node_id
            assert node_id is not None
            feedback = ConnectionTargetFeedback.COMPATIBLE
            message = (
                "Подключить физическую линию к существующему "
                "электрическому соединению"
            )
            source = self._physical_line_tool.source
            source_node_id: ElectricalNodeId | None = None
            if source is not None and source.kind in {
                ConnectionTargetKind.ELECTRICAL_NODE,
                ConnectionTargetKind.BUS,
                ConnectionTargetKind.NODE_CONNECTION,
            }:
                source_node_id = ElectricalNodeId(source.target_id)
            elif source is not None and source.kind is ConnectionTargetKind.EQUIPMENT_PORT:
                connection = self._model.connection_for_port(
                    PortId(source.target_id)
                )
                source_node_id = (
                    connection.electrical_node_id
                    if connection is not None
                    else None
                )
            if source_node_id == node_id:
                feedback = ConnectionTargetFeedback.INCOMPATIBLE
                message = "Физическая ветвь должна соединять два разных узла"
            return ConnectionTarget(
                ConnectionTargetKind.NODE_CONNECTION,
                point.x(),
                point.y(),
                node_id.value,
                feedback=feedback,
                message=message,
                route_id=route.id.value,
            )

        # Физическая трасса является допустимой только конечной целью нового
        # ответвления. Контекстный сценарий, где трасса уже является source,
        # продолжает обслуживаться отдельным PendingTapBranch.
        source = self._physical_line_tool.source
        if source is None or source.kind is ConnectionTargetKind.PHYSICAL_LINE:
            return None
        physical_hit = self.physical_route_at(scene_pos, include_legacy=True)
        if physical_hit is None:
            return None
        route, route_fraction = physical_hit
        projected = self._project_to_route(route, scene_pos)
        if projected is None or route.equipment_id is None:
            return None
        point, _ = projected
        return ConnectionTarget(
            ConnectionTargetKind.PHYSICAL_LINE,
            point.x(),
            point.y(),
            route.equipment_id.value,
            direction=self._tap_entry_direction(route, point,
                self._physical_line_tool.manual_vertices[-1] if self._physical_line_tool.manual_vertices else source),
            feedback=ConnectionTargetFeedback.COMPATIBLE,
            message="Отпустите кнопку, чтобы создать отпайку",
            route_id=route.id.value,
            route_fraction=route_fraction,
        )

    @staticmethod
    def _route_fraction(route: DiagramRoute, point: QPointF) -> float:
        """Вернуть только графическую долю для привязки маркера/подсказки.

        Это значение никогда не используется как физическая длина линии.
        """

        rows = tuple(QPointF(item.x, item.y) for item in route.waypoints)
        lengths = [
            abs(first.x() - second.x()) + abs(first.y() - second.y())
            for first, second in zip(rows, rows[1:])
        ]
        total = sum(lengths)
        if total <= 0.0:
            return 0.0
        best: tuple[float, float] | None = None
        passed = 0.0
        for length, first, second in zip(lengths, rows, rows[1:]):
            distance = DiagramGraphicsScene._segment_distance(point, first, second)
            if math.isclose(first.x(), second.x(), abs_tol=1e-9):
                along = abs(min(max(point.y(), min(first.y(), second.y())), max(first.y(), second.y())) - first.y())
            else:
                along = abs(min(max(point.x(), min(first.x(), second.x())), max(first.x(), second.x())) - first.x())
            candidate = distance, (passed + along) / total
            if best is None or candidate < best:
                best = candidate
            passed += length
        return min(1.0, max(0.0, best[1] if best is not None else 0.0))

    def _preview_junction_orientation(
        self,
        target: ConnectionTarget | None,
    ) -> str | None:
        if (
            target is None
            or target.kind is not ConnectionTargetKind.PHYSICAL_LINE
            or not target.route_id
        ):
            return None
        try:
            route_item = self._route_items_by_id.get(
                DiagramRouteId(target.route_id)
            )
        except (TypeError, ValueError):
            return None
        if route_item is None:
            return None
        projected = self._project_to_route(
            route_item.route,
            QPointF(target.x, target.y),
        )
        return projected[1] if projected is not None else None

    def _preview_obstacles(self, cursor: QPointF) -> tuple[RoutingObstacle, ...]:
        del cursor
        return self._apparatus_routing_obstacles()

    def _new_route_occupied_segments(self, *, reconnect=False):
        if self._document is None or self._page_id is None:
            return ()
        excluded = self._connection_replaced_route_id if reconnect else None
        key = (id(self._document), self._page_id, excluded)
        if self._new_route_occupied_cache is None or self._new_route_occupied_cache[0] != key:
            from ..editor.equipment_attachment import occupied_segments
            rows = occupied_segments(self._document, self._page_id, (excluded,) if excluded else ())
            self._new_route_occupied_cache = (key, rows)
        return self._new_route_occupied_cache[1]

    def _apparatus_routing_obstacles(self) -> tuple[RoutingObstacle, ...]:
        """Match the command planner: solid bodies, not hit padding or wires.

        The target port lies on its actual body boundary. Selection margins
        must not make it appear buried inside its own apparatus. Conductors
        remain crossable; their existing bridge/junction rules are unchanged.
        """
        result: list[RoutingObstacle] = []
        for item in self._items_by_id.values():
            if (item.representation.equipment_id is None
                    or item._canonical_key in {"line", "line_section", "busbar"}):
                continue
            rect = item.body_scene_rect()
            result.append(RoutingObstacle(rect.left(), rect.top(), rect.right(), rect.bottom()))
        return tuple(result)

    def _route_obstacles(
        self, area: QRectF, excluded_route_id: str = ""
    ) -> tuple[RoutingObstacle, ...]:
        """Тонкие локальные препятствия уменьшают наложения/пересечения."""

        result: list[RoutingObstacle] = []
        seen: set[DiagramRouteId] = set()
        for raw_item in self.items(area):
            if not isinstance(raw_item, DiagramRouteItem):
                continue
            if raw_item.route_id in seen or raw_item.route_id.value == excluded_route_id:
                continue
            seen.add(raw_item.route_id)
            points = tuple(QPointF(item.x, item.y) for item in raw_item.route.waypoints)
            for first, second in zip(points, points[1:]):
                result.append(RoutingObstacle(
                    min(first.x(), second.x()) - 3.0,
                    min(first.y(), second.y()) - 3.0,
                    max(first.x(), second.x()) + 3.0,
                    max(first.y(), second.y()) + 3.0,
                ))
        return tuple(result)

    def physical_route_at(
        self, scene_pos: QPointF, *, include_legacy: bool = False
    ) -> tuple[DiagramRoute, float] | None:
        """Найти физическую трассу под курсором без электрической мутации."""
        tolerance = ROUTE_HIT_TOLERANCE_PX / self._view_scale()
        area = QRectF(
            scene_pos.x() - tolerance,
            scene_pos.y() - tolerance,
            tolerance * 2.0,
            tolerance * 2.0,
        )
        candidates: list[tuple[float, DiagramRoute]] = []
        for raw_item in self.items(area):
            if not isinstance(raw_item, DiagramRouteItem):
                continue
            route = raw_item.route
            if (
                self._model is None
                or route.kind is not DiagramRouteKind.EQUIPMENT_BRANCH
                or not (route.equipment_id in self._model.line_sections
                    or (include_legacy and _is_directional_line(self._model, route.equipment_id)))
            ):
                continue
            points = tuple(QPointF(row.x, row.y) for row in route.waypoints)
            distance = min(
                (
                    self._segment_distance(scene_pos, first, second)
                    for first, second in zip(points, points[1:])
                ),
                default=float("inf"),
            )
            if distance <= tolerance:
                candidates.append((distance, route))
        if not candidates:
            return None
        _, route = min(candidates, key=lambda row: (row[0], row[1].id.value))
        return route, self._route_fraction(route, scene_pos)

    def _reset_connection_highlights(self) -> None:
        for object_item in self._items_by_id.values():
            object_item.set_target_feedback(ConnectionTargetFeedback.NEUTRAL)
            for port_item in object_item._port_items.values():
                port_item.set_visual_state(PortVisualState.NORMAL)
        for route_item in self._route_items_by_id.values():
            route_item.set_target_feedback(ConnectionTargetFeedback.NEUTRAL)

    def _apply_connection_highlight(self, target: ConnectionTarget | None) -> None:
        self._reset_connection_highlights()
        if not self._connection_tool.active:
            return
        source_port = (PortId(self._connection_tool.source_port_id)
                       if self._connection_tool.source_port_id else None)
        for object_item in self._items_by_id.values():
            source_item = object_item.port_item(source_port)
            if source_item is not None:
                source_item.set_visual_state(PortVisualState.SOURCE)
                break
        if target is None:
            return
        state = (
            PortVisualState.COMPATIBLE
            if target.feedback is ConnectionTargetFeedback.COMPATIBLE
            else PortVisualState.INCOMPATIBLE
        )
        if target.kind is ConnectionTargetKind.EQUIPMENT_PORT:
            port_id = PortId(target.target_id)
            for object_item in self._items_by_id.values():
                port_item = object_item.port_item(port_id)
                if port_item is not None:
                    port_item.set_visual_state(state)
                    break
        elif target.representation_id:
            representation_id = GraphicalRepresentationId(target.representation_id)
            if representation_id in self._items_by_id:
                self._items_by_id[representation_id].set_target_feedback(target.feedback)
        elif target.route_id:
            try:
                route_id = DiagramRouteId(target.route_id)
            except Exception:
                route_id = None
            if route_id in self._route_items_by_id:
                self._route_items_by_id[route_id].set_target_feedback(target.feedback)

    def begin_connection_from_target(self, target: ConnectionTarget) -> None:
        if self._mode is not CanvasMode.EDIT:
            return
        self.cancel_physical_line(announce=False)
        self._connection_replaced_route_id = None
        if self._tool_state_machine is not None:
            self._tool_state_machine.activate(EditorTool.DRAW_CONNECTION, tool_name="Соединить")
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self._connection_tool.begin_from_target(target)
        self.connectionStatusMessage.emit("Ведите соединение к выводу, шине или проводнику; тип выберите после отпускания")

    def begin_connection(self, port_item: ElectricalPortItem) -> None:
        if self._mode is not CanvasMode.EDIT:
            self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            return
        if self._model is None:
            return
        if self._tool_state_machine is not None:
            transition = self._tool_state_machine.activate(
                EditorTool.DRAW_CONNECTION,
                tool_name="Провод",
            )
            if not transition.accepted:
                self.connectionStatusMessage.emit(transition.message)
                return
            self.toolStateChanged.emit(self._tool_state_machine.state)
        point = port_item.mapToScene(QPointF())
        reconnect = self._model.connection_for_port(port_item.port_id) is not None
        candidates = [route.id for route in self._document.routes.values()
                      if reconnect and route.page_id == self._page_id
                      and route.kind is DiagramRouteKind.NODE_CONNECTION
                      and any(anchor.target_port_id == port_item.port_id
                              and anchor.representation_id == port_item.representation_id
                              for anchor in (route.start_anchor, route.end_anchor))]
        self._connection_replaced_route_id = candidates[0] if len(candidates) == 1 else None
        self._connection_tool.begin(
            source_port_id=port_item.port_id.value,
            source_representation_id=port_item.representation_id.value,
            x=point.x(),
            y=point.y(),
            direction=port_item.scene_direction(),
            anchor_key=port_item.anchor.role,
            reconnect=reconnect,
        )
        self._apply_connection_highlight(None)
        self._preview_item.set_preview(self._connection_tool.preview_vertices)
        self.connectionStatusMessage.emit(ui_text("status.connection_started"))

    def begin_route_endpoint_reconnect(
        self,
        route_id: DiagramRouteId,
        *,
        at_start: bool,
        press_position: QPointF | None = None,
    ) -> bool:
        """Начать переподключение реального порта физической ветви."""
        if self._mode is not CanvasMode.EDIT:
            self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            return False
        route_item = self._route_items_by_id.get(route_id)
        if route_item is None or self._model is None:
            self.connectionStatusMessage.emit(
                "Графическая трасса физической ветви не найдена"
            )
            return False
        route = route_item.route
        if (
            route.kind is not DiagramRouteKind.EQUIPMENT_BRANCH
            or not _is_directional_line(self._model, route.equipment_id)
            or len(route.waypoints) < 2
        ):
            self.connectionStatusMessage.emit(
                "Переподключать конец можно только у физической ветви"
            )
            return False
        anchor = route.start_anchor if at_start else route.end_anchor
        point = route.waypoints[0] if at_start else route.waypoints[-1]
        source_point, source_direction = self._anchor_scene_geometry(
            anchor, RouteVertex(point.x, point.y)
        )
        if anchor.branch_port_id is None:
            self.connectionStatusMessage.emit(
                "У конца физической ветви отсутствует электрический порт"
            )
            return False
        self.cancel_physical_line(announce=False)
        if self._tool_state_machine is not None:
            transition = self._tool_state_machine.activate(
                EditorTool.DRAW_CONNECTION,
                tool_name="Переподключение",
            )
            if not transition.accepted:
                self.connectionStatusMessage.emit(transition.message)
                return False
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self._connection_tool.begin(
            source_port_id=anchor.branch_port_id.value,
            source_representation_id=anchor.representation_id.value,
            x=source_point.x,
            y=source_point.y,
            direction=source_direction,
            anchor_key=anchor.anchor_key,
            reconnect=True,
            from_route_endpoint=True,
        )
        self._connection_replaced_route_id = route_id
        self._connection_press_position = (
            QPointF(press_position) if press_position is not None else None
        )
        self._connection_dragged = False
        self._apply_connection_highlight(None)
        self._preview_item.set_preview(self._connection_tool.preview_vertices)
        self.connectionStatusMessage.emit(
            "Выберите новый узел, шину, порт, свободное место или физическую линию"
        )
        return True

    def update_connection_cursor(self, scene_pos: QPointF) -> None:
        if not self._connection_tool.active:
            return
        target = self._target_at(scene_pos)
        if (self._connection_dragged and target is not None
                and target.kind is ConnectionTargetKind.BUS
                and not self._connection_tool.manual_vertices):
            bus = self._items_by_id.get(GraphicalRepresentationId(target.representation_id))
            source = self._connection_tool.source_vertex
            if bus is not None and source is not None:
                fraction, x, y, direction = bus_anchor_toward_point(
                    width=bus._width, height=bus._height, rotation=bus.rotation(),
                    center_x=bus.x(), center_y=bus.y(), target_x=source.x, target_y=source.y,
                )
                target = replace(target, x=x, y=y, direction=direction, anchor_key=f"{fraction:.8f}")
        if target is not None:
            target = self._available_bus_target(target)
        peer = RouteVertex(target.x, target.y) if target is not None else RouteVertex(scene_pos.x(), scene_pos.y())
        source = self._connection_tool.source_target
        if source is not None and source.kind is ConnectionTargetKind.BUS and callable(self._bus_exit_direction_preview):
            direction = self._bus_exit_direction_preview(source, peer)
            self._connection_tool.source_target = replace(source, direction=direction)
            self._connection_tool.source_direction = direction
        if target is not None and target.kind is ConnectionTargetKind.BUS and callable(self._bus_exit_direction_preview):
            target = replace(target, direction=self._bus_exit_direction_preview(target, self._connection_tool.source_vertex))
        self._connection_target = target
        self._connection_routing_error = ""
        try:
            vertices = self._connection_tool.update(
                scene_pos.x(), scene_pos.y(), target=target,
                obstacles=self._preview_obstacles(scene_pos),
                occupied_segments=self._new_route_occupied_segments(reconnect=True),
            )
        except RoutingError as exc:
            self._connection_routing_error = str(exc)
            self._connection_tool.preview_vertices = ()
            self._preview_item.clear()
            if target is not None:
                target = replace(target, feedback=ConnectionTargetFeedback.INCOMPATIBLE, message=str(exc))
                self._connection_target = target
                self._connection_tool.target = target
            self._apply_connection_highlight(target)
            self.connectionStatusMessage.emit(str(exc))
            return
        feedback = target.feedback if target is not None else ConnectionTargetFeedback.NEUTRAL
        self._preview_item.set_preview(
            vertices,
            feedback,
            target.kind if target is not None else ConnectionTargetKind.FREE,
            self._preview_junction_orientation(target),
        )
        self._apply_connection_highlight(target)
        if target is not None and target.message:
            self.connectionStatusMessage.emit(target.message)

    def _bus_band_distance_px(self, item: DiagramObjectItem, scene_pos: QPointF) -> float:
        """Distance to the visible band, with no longitudinal size inflation."""
        ink = item.symbol_ink_rect()
        local = item.mapFromScene(scene_pos)
        dx = max(ink.left() - local.x(), 0.0, local.x() - ink.right())
        dy = max(ink.top() - local.y(), 0.0, local.y() - ink.bottom())
        return math.hypot(dx, dy) * self._view_scale()

    def _available_bus_target(self, target: ConnectionTarget) -> ConnectionTarget:
        """One allocator for the preview and the controller's saved anchor."""
        if target.kind is not ConnectionTargetKind.BUS or self._document is None:
            return target
        bus = self._items_by_id.get(GraphicalRepresentationId(target.representation_id))
        if bus is None:
            return target
        excluded = ()
        if self._connection_tool.active and self._connection_tool.mode is ConnectionToolMode.RECONNECT:
            source_id = PortId(self._connection_tool.source_port_id)
            excluded = tuple(route.id for route in self._document.routes.values()
                if any(source_id in (anchor.target_port_id, anchor.branch_port_id)
                       for anchor in (route.start_anchor, route.end_anchor)))
        try:
            fraction = available_bus_fraction(self._document, bus.representation,
                width=bus._width, height=bus._height, requested=float(target.anchor_key), exclude_route_ids=excluded)
            x, y, direction = bus_anchor_geometry(width=bus._width, height=bus._height,
                rotation=bus.rotation(), center_x=bus.x(), center_y=bus.y(), fraction=fraction)
        except ValueError as exc:
            return replace(target, feedback=ConnectionTargetFeedback.INCOMPATIBLE, message=str(exc))
        return replace(target, x=x, y=y, anchor_key=format(fraction, ".17g"), direction=target.direction or direction)

    def cancel_connection(self, *, announce: bool = True) -> None:
        self._connection_press_position = None
        self._connection_dragged = False
        self._connection_routing_error = ""
        was_active = self._connection_tool.active
        self._connection_tool.cancel()
        self._connection_target = None
        self._preview_item.clear()
        self._reset_connection_highlights()
        if not was_active:
            return
        if (
            self._tool_state_machine is not None
            and self._tool_state_machine.tool is EditorTool.DRAW_CONNECTION
        ):
            self._tool_state_machine.select_tool()
            self.toolStateChanged.emit(self._tool_state_machine.state)
        if announce:
            self.connectionStatusMessage.emit(ui_text("status.connection_cancelled"))

    def cancel_connection_drag(self) -> None:
        """Cancel only a held-port gesture; do not discard click-click on focus changes."""
        if self._connection_press_position is not None:
            self.cancel_connection(announce=False)

    def update_connection_drag(self, scene_pos: QPointF) -> None:
        if self._connection_press_position is not None:
            delta = scene_pos - self._connection_press_position
            if math.hypot(delta.x(), delta.y()) * self._view_scale() >= QApplication.startDragDistance():
                self._connection_dragged = True

    def finish_connection_drag(self, scene_pos: QPointF, screen_pos: QPoint) -> bool:
        """Release ownership before a popup, focus change, or synchronous commit."""
        if self._connection_press_position is None:
            return False
        self.update_connection_drag(scene_pos)
        dragged = self._connection_dragged
        self._connection_press_position = None
        if not dragged:
            self._connection_dragged = False
            return True  # A short first click keeps the existing two-click tool.
        self.update_connection_cursor(scene_pos)
        self._connection_dragged = False
        target = self._connection_target
        if target is None or target.feedback is not ConnectionTargetFeedback.COMPATIBLE:
            message = target.message if target is not None else "Соединение отменено: отпустите кнопку над совместимым выводом или шиной"
            self.cancel_connection(announce=False)
            self.connectionStatusMessage.emit(message)
            return True
        draft = self._connection_tool.draft()
        self.cancel_connection(announce=False)
        self.connectionDragDraftRequested.emit(draft, screen_pos)
        return True

    def begin_physical_line(
        self,
        *,
        name: str,
        line_kind: LineKind | str,
        scene_pos: QPointF,
    ) -> bool:
        if self._mode is not CanvasMode.EDIT:
            self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            return False
        target = self._physical_endpoint_at(scene_pos)
        if target is None:
            target = ConnectionTarget(
                ConnectionTargetKind.FREE,
                scene_pos.x(),
                scene_pos.y(),
                feedback=ConnectionTargetFeedback.COMPATIBLE,
                message="Создать начальный электрический узел",
            )
        target = self._physical_voltage_feedback(target)
        if target.feedback is ConnectionTargetFeedback.INCOMPATIBLE:
            self.connectionStatusMessage.emit(target.message)
            return False
        self.cancel_connection()
        if self._tool_state_machine is not None:
            transition = self._tool_state_machine.activate(
                EditorTool.DRAW_CONNECTION,
                tool_name="Физическая линия",
            )
            if not transition.accepted:
                self.connectionStatusMessage.emit(transition.message)
                return False
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self._physical_line_tool.begin(
            name=name,
            line_kind=str(getattr(line_kind, "value", line_kind)),
            source=target,
        )
        self._preview_item.set_preview(self._physical_line_tool.preview_vertices)
        self._apply_physical_highlight(None)
        self.connectionStatusMessage.emit(ui_text("status.physical_line_started"))
        return True

    def begin_physical_line_from_target(
        self,
        *,
        name: str,
        line_kind: LineKind | str,
        source: ConnectionTarget,
    ) -> bool:
        """Начать временную отходящую линию от выбранной физической ветви."""
        if self._mode is not CanvasMode.EDIT:
            self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            return False
        if (
            source.kind is not ConnectionTargetKind.PHYSICAL_LINE
            or source.feedback is ConnectionTargetFeedback.INCOMPATIBLE
        ):
            self.connectionStatusMessage.emit(
                source.message or "Выбранная физическая линия несовместима"
            )
            return False
        self.cancel_connection()
        if self._tool_state_machine is not None:
            transition = self._tool_state_machine.activate(
                EditorTool.DRAW_CONNECTION,
                tool_name="Физическая линия",
            )
            if not transition.accepted:
                self.connectionStatusMessage.emit(transition.message)
                return False
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self._physical_line_tool.begin(
            name=name,
            line_kind=str(getattr(line_kind, "value", line_kind)),
            source=source,
        )
        self._preview_item.set_preview(self._physical_line_tool.preview_vertices)
        self._apply_physical_highlight(None)
        self.connectionStatusMessage.emit(
            "Укажите точки маршрута и завершите отходящую линию на порту, "
            "узле или свободном месте"
        )
        return True

    def _physical_preview_obstacles(
        self,
        cursor: QPointF,
        excluded_route_id: str = "",
    ) -> tuple[RoutingObstacle, ...]:
        del cursor, excluded_route_id
        return self._apparatus_routing_obstacles()

    def update_physical_line_cursor(self, scene_pos: QPointF) -> None:
        if not self._physical_line_tool.active:
            return
        target = self._physical_endpoint_at(scene_pos)
        if target is not None:
            target = self._physical_voltage_feedback(target, self._physical_line_tool.source)
        source = self._physical_line_tool.source
        peer = RouteVertex(target.x, target.y) if target is not None else RouteVertex(scene_pos.x(), scene_pos.y())
        if source.kind is ConnectionTargetKind.BUS and callable(self._bus_exit_direction_preview):
            direction = self._bus_exit_direction_preview(source, peer)
            self._physical_line_tool.source = replace(source, direction=direction)
            self._physical_line_tool.source_direction = direction
        if target is not None and target.kind is ConnectionTargetKind.BUS and callable(self._bus_exit_direction_preview):
            target = replace(target, direction=self._bus_exit_direction_preview(target, source))
        self._physical_routing_error = ""
        try:
            vertices = self._physical_line_tool.update(
                scene_pos.x(), scene_pos.y(), target=target,
                obstacles=self._physical_preview_obstacles(
                    scene_pos, target.route_id if target is not None else "",
                ),
                occupied_segments=self._new_route_occupied_segments(),
            )
        except RoutingError as exc:
            self._physical_routing_error = str(exc)
            self._physical_line_tool.preview_vertices = ()
            self._preview_item.clear()
            if target is not None:
                target = replace(target, feedback=ConnectionTargetFeedback.INCOMPATIBLE, message=str(exc))
                self._physical_line_tool.target = target
            self._apply_physical_highlight(target)
            self.connectionStatusMessage.emit(str(exc))
            return
        feedback = target.feedback if target is not None else ConnectionTargetFeedback.NEUTRAL
        self._preview_item.set_preview(
            vertices,
            feedback,
            target.kind if target is not None else ConnectionTargetKind.FREE,
            self._preview_junction_orientation(target),
        )
        self._apply_physical_highlight(target)
        if target is not None and target.message:
            self.connectionStatusMessage.emit(target.message)

    def _physical_voltage_feedback(
        self, target: ConnectionTarget, source: ConnectionTarget | None = None,
    ) -> ConnectionTarget:
        if self._model is None or target.feedback is ConnectionTargetFeedback.INCOMPATIBLE:
            return target

        def endpoint(value):
            if value is None or value.kind is ConnectionTargetKind.FREE:
                return None
            if value.kind is ConnectionTargetKind.EQUIPMENT_PORT:
                return PortId(value.target_id)
            if value.kind is ConnectionTargetKind.PHYSICAL_LINE:
                return self._model.port_by_role(EquipmentId(value.target_id), "from").id
            return ElectricalNodeId(value.target_id)

        start, end = endpoint(source), endpoint(target)
        check = (self._check_connection_voltage(start, end) if start is not None and end is not None
                 else endpoint_voltage(self._model, end if end is not None else start)
                 if end is not None or start is not None else None)
        if check is not None and not check.valid:
            return replace(target, feedback=ConnectionTargetFeedback.INCOMPATIBLE, message=check.message)
        return target

    def _apply_physical_highlight(self, target: ConnectionTarget | None) -> None:
        self._reset_connection_highlights()
        source = self._physical_line_tool.source
        if source is not None:
            self._highlight_endpoint(source, PortVisualState.SOURCE)
            if source.kind is ConnectionTargetKind.PHYSICAL_LINE and source.route_id:
                route_item = self._route_items_by_id.get(
                    DiagramRouteId(source.route_id)
                )
                if route_item is not None:
                    route_item.set_target_feedback(
                        ConnectionTargetFeedback.COMPATIBLE
                    )
        if target is not None:
            state = (
                PortVisualState.COMPATIBLE
                if target.feedback is ConnectionTargetFeedback.COMPATIBLE
                else PortVisualState.INCOMPATIBLE
            )
            self._highlight_endpoint(target, state)
            if target.route_id:
                try:
                    route_item = self._route_items_by_id.get(
                        DiagramRouteId(target.route_id)
                    )
                except (TypeError, ValueError):
                    route_item = None
                if route_item is not None:
                    route_item.set_target_feedback(target.feedback)

    def _highlight_endpoint(
        self, target: ConnectionTarget, state: PortVisualState
    ) -> None:
        if target.kind is ConnectionTargetKind.EQUIPMENT_PORT:
            port_id = PortId(target.target_id)
            for object_item in self._items_by_id.values():
                port_item = object_item.port_item(port_id)
                if port_item is not None:
                    port_item.set_visual_state(state)
                    return
        elif target.representation_id:
            representation_id = GraphicalRepresentationId(target.representation_id)
            item = self._items_by_id.get(representation_id)
            if item is not None:
                item.set_target_feedback(
                    ConnectionTargetFeedback.COMPATIBLE
                    if state is not PortVisualState.INCOMPATIBLE
                    else ConnectionTargetFeedback.INCOMPATIBLE
                )

    def cancel_physical_line(self, *, announce: bool = True) -> None:
        self._physical_routing_error = ""
        was_active = self._physical_line_tool.active
        self._physical_line_tool.cancel()
        self._preview_item.clear()
        self._reset_connection_highlights()
        if not was_active:
            return
        if (
            self._tool_state_machine is not None
            and self._tool_state_machine.tool is EditorTool.DRAW_CONNECTION
        ):
            self._tool_state_machine.select_tool()
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self.placementCancelled.emit()
        if announce:
            self.connectionStatusMessage.emit(ui_text("status.connection_cancelled"))

    def finish_physical_line(self, *, free_target: bool = False) -> bool:
        if self._physical_routing_error:
            self.connectionStatusMessage.emit(self._physical_routing_error)
            return False
        try:
            draft = self._physical_line_tool.draft(free_target=free_target)
        except RuntimeError as exc:
            self.connectionStatusMessage.emit(str(exc))
            return False
        self.physicalLineDraftRequested.emit(draft)
        self.cancel_physical_line(announce=False)
        return True

    def handle_physical_line_click(self, scene_pos: QPointF) -> bool:
        if not self._physical_line_tool.active:
            return False
        self.update_physical_line_cursor(scene_pos)
        target = self._physical_line_tool.target
        if target is not None:
            if target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                return self.finish_physical_line()
            self.connectionStatusMessage.emit(target.message)
            return False
        self._physical_line_tool.add_manual_vertex(scene_pos.x(), scene_pos.y())
        self.update_physical_line_cursor(scene_pos)
        self.connectionStatusMessage.emit(ui_text("status.connection_waypoint_added"))
        return False

    def _emit_connection_draft(
        self,
        *,
        free_target: bool = False,
        screen_pos: object | None = None,
    ) -> None:
        if self._connection_routing_error:
            self.connectionStatusMessage.emit(self._connection_routing_error)
            return
        try:
            draft = self._connection_tool.draft(free_target=free_target)
        except RuntimeError as exc:
            self.connectionStatusMessage.emit(str(exc))
            return
        #  Решение заказчика 02.09.2026: завершение вторым щелчком спрашивает
        #  тип так же, как протяжка с зажатой кнопкой. Раньше выбор
        #  «Провод / ВЛ / КЛ» висел только на протяжке, и тот же самый жест,
        #  сделанный двумя щелчками, молча давал провод.
        #  Меню открывается только там, где известна точка экрана, то есть
        #  жест сделал человек. Программное завершение (тесты, сценарии,
        #  встраиваемый холст) остаётся немодальным: спрашивать там некого, а
        #  модальное меню повесило бы вызывающий код.
        # Меню запускает вложенный цикл Qt. Завершаем владение жестом до
        # него, как и при отпускании протяжки: Esc/фокус/поздний release
        # не должны продолжать уже подтверждаемый черновик.
        self.cancel_connection(announce=False)
        if screen_pos is None:
            self.connectionDraftRequested.emit(draft)
        else:
            self.connectionDragDraftRequested.emit(draft, screen_pos)

    def _anchor_scene_geometry(
        self,
        anchor: object,
        fallback: RouteVertex,
    ) -> tuple[RouteVertex, RouteDirection | None]:
        representation_id = getattr(anchor, "representation_id", None)
        item = self._items_by_id.get(representation_id)
        if item is None:
            return fallback, None
        target_port_id = getattr(anchor, "target_port_id", None)
        if target_port_id is not None:
            port_item = item.port_item(target_port_id)
            if port_item is not None:
                point = port_item.mapToScene(QPointF())
                return RouteVertex(point.x(), point.y()), port_item.scene_direction()
        kind = getattr(anchor, "kind", None)
        if kind is RouteAnchorKind.BUS:
            try:
                fraction = float(getattr(anchor, "anchor_key", ""))
                if not math.isfinite(fraction):
                    raise ValueError
            except (TypeError, ValueError):
                previous = item.representation
                radians = math.radians(-previous.rotation_deg)
                dx, dy = fallback.x - previous.x, fallback.y - previous.y
                local_x = dx * math.cos(radians) - dy * math.sin(radians)
                local_y = dx * math.sin(radians) + dy * math.cos(radians)
                fraction = (
                    local_y / item._height if item._height > item._width
                    else local_x / item._width
                ) + 0.5
            x, y, direction = bus_anchor_geometry(
                width=item._width, height=item._height, rotation=item.rotation(),
                center_x=item.pos().x(), center_y=item.pos().y(), fraction=fraction,
            )
            return RouteVertex(x, y), direction
        point = item.mapToScene(QPointF())
        return RouteVertex(point.x(), point.y()), None

    def _incident_preview_vertices(
        self, route: DiagramRoute
    ) -> tuple[RouteVertex, ...]:
        base = _route_vertices(route)
        if len(base) < 2:
            return base
        start, start_direction = self._anchor_scene_geometry(
            route.start_anchor, base[0]
        )
        end, end_direction = self._anchor_scene_geometry(
            route.end_anchor, base[-1]
        )
        manual = tuple(
            RouteVertex(
                item.x,
                item.y,
                RouteVertexSource.USER,
                item.pinned,
            )
            for item in route.waypoints[1:-1]
            if item.source is RouteWaypointSource.USER
        )
        obstacles = []
        for representation_id in dict.fromkeys((
            route.start_anchor.representation_id, route.end_anchor.representation_id,
        )):
            item = self._items_by_id.get(representation_id)
            if item is not None and item.representation.equipment_id is not None:
                rect = item.body_scene_rect()
                obstacles.append(RoutingObstacle(rect.left(), rect.top(), rect.right(), rect.bottom()))
        return build_orthogonal_route(
            RoutingRequest(
                start,
                end,
                start_direction,
                end_direction,
                manual,
                obstacles=tuple(obstacles),
                port_stub=12.0,
            )
        )

    def _preview_incident_routes(self) -> None:
        moved_ids = {
            representation_id
            for representation_id, start in self._start_positions.items()
            if representation_id in self._items_by_id
            and self._items_by_id[representation_id].pos() != start
        }
        if not moved_ids and not self._body_drag_previewed:
            return
        self._body_drag_previewed = True
        if callable(self._move_preview) and self._start_positions:
            from ..editor.controller import EditorCommandError
            lead_id = min(self._start_positions, key=lambda identifier: identifier.value)
            lead = self._items_by_id.get(lead_id)
            if lead is None:
                self.cancel_object_drag()
                return
            delta = lead.pos() - self._start_positions[lead_id]
            try:
                preview = self._move_preview(tuple(self._start_positions), delta.x(), delta.y(), bypass_snap=True)
            except EditorCommandError as exc:
                self._set_body_routing_error(str(exc))
                return
            for identifier, representation in preview.representations.items():
                item = self._items_by_id.get(identifier)
                point = QPointF(representation.x, representation.y)
                if item is not None and item.pos() != point:
                    if identifier not in self._start_positions:
                        self._body_drag_auxiliary_positions.setdefault(identifier, QPointF(item.pos()))
                    item.setPos(point)
            for identifier, route_item in self._route_items_by_id.items():
                route = preview.routes.get(identifier, route_item.route)
                route_item.set_temporary_vertices(_route_vertices(route), refresh_bridges=False)
            self._refresh_route_bridges()
            return
        try:
            planned = {route_item.route_id: self._incident_preview_vertices(route_item.route)
                       for route_item in self._route_items_by_id.values()
                       if {route_item.route.start_anchor.representation_id,
                           route_item.route.end_anchor.representation_id} & moved_ids}
        except RoutingError as exc:
            self._set_body_routing_error(str(exc))
            return
        for route_item in self._route_items_by_id.values():
            if route_item.route_id in planned:
                route_item.set_temporary_vertices(
                    planned[route_item.route_id], refresh_bridges=False
                )
            else:
                route_item.clear_temporary_route(refresh_bridges=False)
        self._refresh_route_bridges()

    def begin_connected_drag(self, item: QGraphicsItem, position: QPointF) -> bool:
        """Start a display-only conductor gesture with stable endpoint owners."""
        if self._mode is not CanvasMode.EDIT or self._document is None:
            return False
        if isinstance(item, BusAttachmentHandle):
            if not callable(self._bus_attachment_preview):
                return False
            gesture = ConnectedDragGesture("bus", item.route_id, QPointF(position), item, at_start=item.at_start)
        else:
            route_item = self._route_ancestor(item)
            if route_item is None or len(route_item.route.waypoints) < 2:
                return False
            vertices = route_item.route.waypoints
            index = item.segment_index if isinstance(item, RouteSegmentHandle) else min(
                range(len(vertices) - 1), key=lambda idx: self._segment_distance(
                    position, QPointF(vertices[idx].x, vertices[idx].y),
                    QPointF(vertices[idx + 1].x, vertices[idx + 1].y)))
            gesture = ConnectedDragGesture("segment", route_item.route.id, QPointF(position), route_item, segment_index=index)
        self.cancel_object_drag()
        self.finish_object_rotation(commit=False)
        if self._tool_state_machine is not None:
            transition = self._tool_state_machine.activate(EditorTool.DRAG_ROUTE_SEGMENT,
                tool_name="Точка присоединения к шине" if gesture.kind == "bus" else "Перемещение сегмента линии")
            if not transition.accepted:
                return False
        self._connected_drag = gesture
        gesture.grabber.grabMouse()
        if self._tool_state_machine is not None:
            self.toolStateChanged.emit(self._tool_state_machine.state)
        return True

    def update_connected_drag(self, position: QPointF, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        gesture = self._connected_drag
        if gesture is None or self._document is None:
            return
        route = self._document.routes.get(gesture.route_id)
        if route is None:
            self.cancel_connected_drag()
            return
        point = QPointF(position)
        bypass_snap = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        # The immutable document owns all endpoint/obstacle inputs for a
        # gesture. Repeated pixels within one snap cell need no new geometry,
        # crossings or label layout. Waypoint equality cannot be used here:
        # extending an end segment allocates fresh temporary waypoint IDs.
        source = (id(self._document), id(route))
        if point == gesture.start:
            if not gesture.preview_routes:
                return
            preview_input = (*source, "origin")
            if preview_input == gesture.preview_input:
                return
            gesture.dx = gesture.dy = 0.0
            gesture.fraction = None
            rows = (route,)
        elif gesture.kind == "bus":
            anchor = route.start_anchor if gesture.at_start else route.end_anchor
            bus = self._items_by_id.get(anchor.representation_id)
            if bus is None:
                self.cancel_connected_drag()
                return
            endpoint = route.waypoints[0] if gesture.at_start else route.waypoints[-1]
            point = QPointF(endpoint.x, endpoint.y) + point - gesture.start
            if self._snap_enabled and not bypass_snap:
                point = QPointF(round(point.x() / self._grid_size) * self._grid_size,
                                round(point.y() / self._grid_size) * self._grid_size)
            local = bus.mapFromScene(point)
            fraction = (local.y() / bus._height if bus._height > bus._width else local.x() / bus._width) + 0.5
            gesture.fraction = min(1.0, max(0.0, fraction))
            preview_input = (*source, "bus", gesture.fraction)
            if preview_input == gesture.preview_input:
                return
            from ..editor.controller import EditorCommandError
            try:
                rows = self._bus_attachment_preview(gesture.route_id, at_start=gesture.at_start, fraction=gesture.fraction)
            except (EditorCommandError, RoutingError) as exc:
                self.cancel_connected_drag()
                self.connectionStatusMessage.emit(str(exc))
                return
        else:
            from ..editor.connected_geometry import shift_route_segment
            delta = point - gesture.start
            first, second = route.waypoints[gesture.segment_index:gesture.segment_index + 2]
            horizontal = math.isclose(first.y, second.y, abs_tol=1e-9)
            offset = delta.y() if horizontal else delta.x()
            origin = first.y if horizontal else first.x
            if self._snap_enabled and not bypass_snap:
                offset = round((origin + offset) / self._grid_size) * self._grid_size - origin
            gesture.dx, gesture.dy = (0.0, offset) if horizontal else (offset, 0.0)
            preview_input = (*source, "segment", gesture.dx, gesture.dy)
            if preview_input == gesture.preview_input:
                return
            from ..editor.equipment_attachment import occupied_segments
            try:
                if gesture.constraints_key != source:
                    gesture.constraints = (self._route_segment_constraints(route.id)
                        if callable(self._route_segment_constraints) else
                        (occupied_segments(self._document, route.page_id, (route.id,)), ()))
                    gesture.constraints_key = source
                points = shift_route_segment(route, gesture.segment_index, dx=gesture.dx, dy=gesture.dy,
                    occupied_segments=gesture.constraints[0], obstacles=gesture.constraints[1])
            except RoutingError as exc:
                self.cancel_connected_drag()
                self.connectionStatusMessage.emit(str(exc))
                return
            rows = (replace(route, waypoints=points),)
        rows = tuple(rows)
        gesture.preview_input = preview_input
        wanted = {row.id for row in rows}
        for previous in gesture.preview_routes:
            if previous.id not in wanted:
                route_item = self._route_items_by_id.get(previous.id)
                if route_item is not None:
                    route_item.clear_temporary_route(refresh_bridges=False)
        gesture.preview_routes = rows
        for preview in gesture.preview_routes:
            route_item = self._route_items_by_id.get(preview.id)
            if route_item is not None:
                route_item.set_temporary_vertices(_route_vertices(preview), refresh_bridges=False)
        self._refresh_route_bridges()

    def cancel_connected_drag(self, *, release_mouse: bool = True, refresh: bool = True) -> bool:
        gesture = self._connected_drag
        if gesture is None:
            return False
        # Clear ownership before refresh/ungrab: Qt callbacks may re-enter here.
        self._connected_drag = None
        for preview in gesture.preview_routes:
            route_item = self._route_items_by_id.get(preview.id)
            if route_item is not None:
                route_item.clear_temporary_route(refresh_bridges=False)
        if gesture.preview_routes and refresh:
            self._refresh_route_bridges()
        if release_mouse and self.mouseGrabberItem() is gesture.grabber:
            gesture.grabber.ungrabMouse()
        if self._tool_state_machine is not None and self._tool_state_machine.tool is EditorTool.DRAG_ROUTE_SEGMENT:
            self._tool_state_machine.select_tool()
            self.toolStateChanged.emit(self._tool_state_machine.state)
        return True

    def finish_connected_drag(self) -> None:
        gesture = self._connected_drag
        if gesture is None:
            return
        changed = self._document is not None and any(
            row != self._document.routes.get(row.id) for row in gesture.preview_routes)
        self.cancel_connected_drag()
        if changed and self._mode is CanvasMode.EDIT:
            if gesture.kind == "bus" and gesture.fraction is not None:
                self.busAttachmentMoveRequested.emit(gesture.route_id, gesture.at_start, gesture.fraction)
            elif gesture.kind == "segment":
                self.routeSegmentMoveRequested.emit(gesture.route_id, gesture.segment_index, gesture.dx, gesture.dy)

    def cancel_object_drag(
        self, *, announce: bool = False, release_mouse: bool = True,
        refresh: bool = True,
    ) -> bool:
        """Restore a body-drag preview without writing the project or history.

        Label/waypoint drags and rotation have their own gesture owners. Clear
        this gesture before ungrabbing or emitting signals: both can re-enter
        the scene synchronously. All incident paths are reset as one batch.
        """
        self.cancel_connected_drag(release_mouse=release_mouse, refresh=refresh)
        self._body_attachment_proposal = None
        self._placement_wire_preview.set_proposal(None)
        angles = self._body_preview_angles
        self._body_preview_angles = {}
        self._drag_collision_snapshot = None
        self._clear_body_routing_preview()
        self._body_start_selection = ()
        self._body_drag_threshold_exceeded = False
        explicit_grabber = self._body_explicit_grabber
        self._body_explicit_grabber = None
        if not self._start_positions:
            self._body_press_position = None
            if explicit_grabber is not None and self.mouseGrabberItem() is explicit_grabber:
                explicit_grabber.ungrabMouse()
            return False
        starts = self._start_positions
        self._start_positions = {}
        self._body_press_position = None
        auxiliary = self._body_drag_auxiliary_positions
        self._body_drag_auxiliary_positions = {}
        previewed = self._body_drag_previewed
        self._body_drag_previewed = False
        changed = False
        for item_id, start in (*starts.items(), *auxiliary.items()):
            item = self._items_by_id.get(item_id)
            if item is not None:
                item.set_target_feedback(ConnectionTargetFeedback.NEUTRAL)
                if item_id in angles and item.rotation() != angles[item_id]:
                    item.setRotation(angles[item_id])
                    item._label.setRotation(-angles[item_id])
                    changed = True
            if item is not None and item.pos() != start:
                item.setPos(start)
                changed = True
        self._clear_drag_collision_preview()
        for route_item in self._route_items_by_id.values():
            if route_item._display_vertices != _route_vertices(route_item.route):
                route_item.clear_temporary_route(refresh_bridges=False)
                changed = True
        if changed or previewed:
            self._label_layout_key = None
            if refresh:
                self._refresh_route_bridges()
        grabber = self.mouseGrabberItem()
        if isinstance(grabber, DiagramObjectItem) and (release_mouse or grabber is explicit_grabber):
            grabber.ungrabMouse()
        if self._tool_state_machine is not None and self._tool_state_machine.tool is EditorTool.DRAG_OBJECT:
            self._tool_state_machine.select_tool()
            self.toolStateChanged.emit(self._tool_state_machine.state)
        self._selection_changed()
        if announce:
            self.connectionStatusMessage.emit("Перемещение отменено; положение и соединения сохранены")
        return True

    def _equipment_collision(
        self,
        moved: Iterable[DiagramObjectItem],
        *,
        clearance: float | None = None,
    ) -> tuple[DiagramObjectItem, DiagramObjectItem] | None:
        """Проверить тела выбранной группы через BSP-кандидатов сцены."""

        moving = {item.representation_id: item for item in moved}
        gap = DEFAULT_SAFE_GAP if clearance is None else clearance
        for item in moving.values():
            area = item.body_scene_rect(clearance=gap)
            seen: set[GraphicalRepresentationId] = set()
            for raw in self.items(area):
                other = self._object_ancestor(raw)
                if (
                    other is None
                    or other.representation_id in moving
                    or other.representation_id in seen
                ):
                    continue
                seen.add(other.representation_id)
                if (
                    other.representation.equipment_id is None
                    and "busbar" not in other._symbol_key.casefold()
                    and other._behavior_key != "bus"
                ):
                    continue
                if area.intersects(other.body_scene_rect()):
                    return item, other
        return None

    def _clear_drag_collision_preview(self) -> None:
        for representation_id in self._drag_collision_ids:
            item = self._items_by_id.get(representation_id)
            if item is not None:
                item.set_target_feedback(ConnectionTargetFeedback.NEUTRAL)
        self._drag_collision_ids.clear()

    def _clear_body_routing_preview(self) -> None:
        self._body_routing_error = ""
        for identifier in self._body_routing_ids:
            item = self._items_by_id.get(identifier)
            if item is not None and identifier not in self._drag_collision_ids:
                item.set_target_feedback(ConnectionTargetFeedback.NEUTRAL)
        self._body_routing_ids.clear()

    def _set_body_routing_error(self, message: str) -> None:
        """Keep the held gesture recoverable, but never commit an invalid path."""
        self._body_routing_error = message
        self._body_routing_ids = set(self._start_positions)
        for identifier in self._body_routing_ids:
            item = self._items_by_id.get(identifier)
            if item is not None:
                item.set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE)
        self.connectionStatusMessage.emit(message)

    def _update_drag_collision_preview(self) -> None:
        """Use the commit collision rules without rebuilding their index per pixel."""

        self._clear_drag_collision_preview()
        moved = [
            self._items_by_id[item_id]
            for item_id, start in self._start_positions.items()
            if item_id in self._items_by_id
            and self._items_by_id[item_id].pos() != start
        ]
        if not moved:
            return
        if self._document is not None and self._model is not None:
            if self._drag_collision_snapshot is None:
                # The canonical service already has a spatial index. Its
                # snapshot belongs to this gesture and contains only saved
                # geometry, never the last frame's temporary item positions.
                self._drag_collision_snapshot = DiagramCollisionService(
                    self._document, self._model
                )
            lead = min(moved, key=lambda item: item.representation_id.value)
            original = self._document.representations[lead.representation_id]
            result = self._drag_collision_snapshot.check_move(
                tuple(self._start_positions),
                lead.x() - original.x, lead.y() - original.y,
            )
            if result.allowed:
                return
            collisions = tuple(
                self._items_by_id[item_id] for item_id in result.conflicting_ids
                if item_id in self._items_by_id
            )
        else:
            # Standalone, document-less graphics scenes retain their cheap
            # BSP fallback; an editor document always uses canonical rules.
            collisions = self._equipment_collision(moved) or ()
        for item in collisions:
            item.set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE)
            self._drag_collision_ids.add(item.representation_id)

    def rotation_collision(
        self,
        representation_id: GraphicalRepresentationId,
        rotation_deg: int,
    ) -> DiagramObjectItem | None:
        item = self._items_by_id.get(representation_id)
        if item is None:
            return None
        if self._document is not None and self._model is not None:
            result = DiagramCollisionService(
                self._document, self._model
            ).check_rotation(representation_id, rotation_deg)
            if result.allowed:
                return None
            for conflict_id in result.conflicting_ids:
                if conflict_id != representation_id:
                    return self._items_by_id.get(conflict_id)
            # Даже повреждённая ссылка на отсутствующее представление не должна
            # превращать отрицательный ответ collision-сервиса в разрешение.
            return item
        width, height = item._width, item._height
        if int(rotation_deg) % 180:
            width, height = height, width
        gap = DEFAULT_SAFE_GAP
        area = QRectF(
            item.pos().x() - width / 2.0 - gap,
            item.pos().y() - height / 2.0 - gap,
            width + gap * 2.0,
            height + gap * 2.0,
        )
        for raw in self.items(area):
            other = self._object_ancestor(raw)
            if other is None or other is item:
                continue
            if (
                other.representation.equipment_id is None
                and "busbar" not in other._symbol_key.casefold()
                and other._behavior_key != "bus"
            ):
                continue
            if area.intersects(other.body_scene_rect()):
                return other
        return None

    def resolve_hit_target(
        self,
        scene_pos: QPointF,
        transform: QTransform | None = None,
    ) -> SceneHitTarget:
        """Определить цель по явному CAD-приоритету, а не только по z-order."""

        device_transform = transform or QTransform()
        raw_items = self.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemShape,
            Qt.SortOrder.DescendingOrder,
            device_transform,
        )
        handles: list[QGraphicsItem] = []
        ports: list[QGraphicsItem] = []
        bodies: list[QGraphicsItem] = []
        labels: list[QGraphicsItem] = []
        physical_routes: list[QGraphicsItem] = []
        ordinary_routes: list[QGraphicsItem] = []
        seen_bodies: set[int] = set()
        seen_labels: set[int] = set()
        seen_routes: set[int] = set()
        delegated_bodies: dict[int, QGraphicsItem] = {}

        for raw_item in raw_items:
            if isinstance(raw_item, DiagramRouteLabelItem):
                labels.append(raw_item)
                continue
            if isinstance(
                raw_item,
                (RotationHandleItem, BusAttachmentHandle, RouteEndpointHandle, RouteWaypointHandle, RouteSegmentHandle),
            ):
                handles.append(raw_item)
                continue
            if isinstance(raw_item, ElectricalPortItem):
                parent = raw_item.parentItem()
                if not raw_item.interaction_enabled() and isinstance(parent, DiagramObjectItem):
                    # An invisible terminal must not hijack a normal line drag.
                    # Explicit connection/diagnostic modes retain the real port.
                    if id(parent) not in seen_bodies:
                        seen_bodies.add(id(parent))
                        bodies.append(parent)
                    continue
                ports.append(raw_item)
                continue
            if isinstance(raw_item, DiagramObjectItem):
                marker = id(raw_item)
                if marker not in seen_bodies:
                    seen_bodies.add(marker)
                    bodies.append(raw_item)
                continue
            object_item = self._object_ancestor(raw_item)
            if (
                object_item is not None
                and isinstance(raw_item, QGraphicsSimpleTextItem)
            ):
                marker = id(raw_item)
                if marker not in seen_labels:
                    seen_labels.add(marker)
                    labels.append(raw_item)
                continue
            route_item = self._route_ancestor(raw_item)
            if route_item is None:
                continue
            marker = id(route_item)
            if marker in seen_routes:
                continue
            seen_routes.add(marker)
            if route_item.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH:
                physical_routes.append(route_item)
            else:
                owner = self._legacy_physical_line_owner(route_item) if not self.connection_active else None
                if owner is not None and id(owner) not in seen_bodies:
                    seen_bodies.add(id(owner))
                    bodies.append(owner)
                    delegated_bodies[id(owner)] = route_item
                ordinary_routes.append(route_item)

        priorities = (
            (HitTestKind.HANDLE, handles),
            (HitTestKind.PORT, ports),
            (HitTestKind.BODY, bodies),
            (HitTestKind.LABEL, labels),
            (HitTestKind.PHYSICAL_LINE, physical_routes),
            (HitTestKind.GRAPHICAL_CONNECTION, ordinary_routes),
        )
        for kind, values in priorities:
            if values:
                return SceneHitTarget(kind, values[0], delegated_bodies.get(id(values[0])))
        return SceneHitTarget(HitTestKind.CANVAS)

    def _legacy_physical_line_owner(self, route_item: DiagramRouteItem) -> DiagramObjectItem | None:
        """Resolve ownership from real anchors, not proximity or the arrow."""
        if route_item.route.kind is not DiagramRouteKind.NODE_CONNECTION:
            return None
        owners = {}
        for anchor in (route_item.route.start_anchor, route_item.route.end_anchor):
            item = self._items_by_id.get(anchor.representation_id)
            if (item is not None and item._behavior_key == "legacy.line"
                    and _is_directional_line(self._model, item.representation.equipment_id)
                    and anchor.target_port_id in item._port_items):
                owners[item.representation_id] = item
        return next(iter(owners.values())) if len(owners) == 1 else None

    def hit_item(
        self,
        scene_pos: QPointF,
        transform: QTransform | None = None,
    ) -> QGraphicsItem | None:
        return self.resolve_hit_target(scene_pos, transform).item

    @staticmethod
    def _object_ancestor(raw_item: QGraphicsItem | None) -> DiagramObjectItem | None:
        current = raw_item
        while current is not None:
            if isinstance(current, DiagramObjectItem):
                return current
            current = current.parentItem()
        return None

    @staticmethod
    def _route_ancestor(raw_item: QGraphicsItem | None) -> DiagramRouteItem | None:
        current = raw_item
        while current is not None:
            if isinstance(current, DiagramRouteItem):
                return current
            current = current.parentItem()
        return None

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            transform = self.views()[0].transform() if self.views() else QTransform()
            hit = self.resolve_hit_target(event.scenePos(), transform)
            raw_item = hit.item
            if (self._mode is CanvasMode.EDIT and not self.connection_active
                    and self._tool_state_machine is not None
                    and self._tool_state_machine.tool is EditorTool.DRAW_CONNECTION):
                source = self._physical_endpoint_at(event.scenePos())
                if source is not None and source.kind in {
                    ConnectionTargetKind.BUS, ConnectionTargetKind.ELECTRICAL_NODE,
                    ConnectionTargetKind.NODE_CONNECTION,
                }:
                    self.begin_connection_from_target(source)
                    self._connection_press_position = QPointF(event.scenePos())
                    self._connection_dragged = False
                    event.accept()
                    return
            if isinstance(raw_item, RotationHandleItem):
                super().mousePressEvent(event)
                return
            if isinstance(raw_item, RouteEndpointHandle):
                parent = raw_item.parentItem()
                if isinstance(parent, DiagramRouteItem):
                    parent.setSelected(True)
                    self.begin_route_endpoint_reconnect(
                        parent.route.id,
                        at_start=raw_item.at_start,
                        press_position=event.scenePos(),
                    )
                    event.accept()
                    return
            if self._mode is CanvasMode.EDIT and self._connection_tool.active:
                self.update_connection_cursor(event.scenePos())
                target = self._connection_target
                if target is not None:
                    if target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                        self._emit_connection_draft(screen_pos=event.screenPos())
                    else:
                        self.connectionStatusMessage.emit(
                            target.message or ui_text("status.connection_incompatible")
                        )
                else:
                    self._connection_tool.add_manual_vertex(
                        event.scenePos().x(), event.scenePos().y()
                    )
                    self.update_connection_cursor(event.scenePos())
                    self.connectionStatusMessage.emit(
                        ui_text("status.connection_waypoint_added")
                    )
                event.accept()
                return
            if self._mode is CanvasMode.EDIT and isinstance(raw_item, ElectricalPortItem):
                self.begin_connection(raw_item)
                if self._connection_tool.active:
                    self._connection_press_position = QPointF(event.scenePos())
                    self._connection_dragged = False
                event.accept()
                return
            if isinstance(raw_item, BusAttachmentHandle) and self.begin_connected_drag(raw_item, event.scenePos()):
                event.accept()
                return
            if isinstance(raw_item, RouteWaypointHandle):
                super().mousePressEvent(event)
                return
            object_item = self._object_ancestor(raw_item)
            if object_item is not None:
                modifiers = event.modifiers()
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    object_item.setSelected(not object_item.isSelected())
                    event.accept()
                    return
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    object_item.setSelected(True)
                    event.accept()
                    return
                if hit.via_route is not None:
                    if not object_item.isSelected():
                        self.clearSelection()
                        object_item.setSelected(True)
                    if self._mode is CanvasMode.EDIT:
                        self._body_explicit_grabber = object_item
                        object_item.grabMouse()
                        self._capture_object_drag(event.scenePos())
                    event.accept()
                    return
            route_item = self._route_ancestor(raw_item)
            if route_item is not None and not isinstance(raw_item, DiagramRouteLabelItem):
                modifiers = event.modifiers()
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    route_item.setSelected(not route_item.isSelected())
                else:
                    if not (modifiers & Qt.KeyboardModifier.ShiftModifier):
                        self.clearSelection()
                    route_item.setSelected(True)
                    if not (modifiers & Qt.KeyboardModifier.ShiftModifier):
                        self.begin_connected_drag(raw_item, event.scenePos())
                event.accept()
                return
            self._clear_drag_collision_preview()
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self._mode is CanvasMode.EDIT:
            # Only the body owns a body gesture. A selected parent's B3 label
            # or a rubber-band selection must not trigger route restoration.
            self._capture_object_drag(event.scenePos())

    def _capture_object_drag(self, scene_pos: QPointF) -> None:
        self._start_positions = {
            item.representation_id: QPointF(item.pos())
            for item in self.selectedItems()
            if isinstance(item, DiagramObjectItem)
        } if isinstance(self.mouseGrabberItem(), DiagramObjectItem) else {}
        self._body_press_position = QPointF(scene_pos) if self._start_positions else None
        self._body_start_selection = (
            self.selected_representation_ids() + self.selected_route_ids()
        ) if self._start_positions else ()
        self._body_drag_threshold_exceeded = False
        self._body_drag_previewed = False
        self._drag_collision_snapshot = None

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._mode is CanvasMode.EDIT
            and self._physical_line_tool.active
        ):
            self.update_physical_line_cursor(event.scenePos())
            target = self._physical_line_tool.target
            if target is None:
                self.finish_physical_line(free_target=True)
            elif target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                self.finish_physical_line()
            else:
                self.connectionStatusMessage.emit(target.message)
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._mode is CanvasMode.EDIT
            and self._connection_tool.active
        ):
            self.update_connection_cursor(event.scenePos())
            target = self._connection_target
            if target is None:
                self._emit_connection_draft(free_target=True, screen_pos=event.screenPos())
            elif target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                self._emit_connection_draft(screen_pos=event.screenPos())
            else:
                self.connectionStatusMessage.emit(
                    target.message or ui_text("status.connection_incompatible")
                )
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            transform = self.views()[0].transform() if self.views() else QTransform()
            raw_item = self.hit_item(event.scenePos(), transform)
            object_item = self._object_ancestor(raw_item)
            if object_item is not None:
                if not object_item.isSelected():
                    self.clearSelection()
                    object_item.setSelected(True)
                if object_item.representation.extensions.get("linked_page_id"):
                    self.linkedPageRequested.emit(object_item.representation_id)
                else:
                    self.propertiesRequested.emit(object_item.representation_id)
                event.accept()
                return
            route_item = self._route_ancestor(raw_item)
            if route_item is not None:
                self.clearSelection()
                route_item.setSelected(True)
                self.propertiesRequested.emit(route_item.route_id)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self.finish_connection_drag(event.scenePos(), event.screenPos())):
            event.accept()
            return
        if self._connected_drag is not None:
            if event.button() == Qt.MouseButton.LeftButton:
                # Qt may deliver release at a new position/modifier without
                # an intervening move. Commit that exact final input.
                self.update_connected_drag(event.scenePos(), event.modifiers())
                self.finish_connected_drag()
            event.accept()
            return
        if self._object_rotation is not None or event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        drag_selection = self._body_start_selection if self._body_drag_threshold_exceeded else ()
        self._body_drag_releasing = True
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._body_drag_releasing = False
        if drag_selection:
            # A scene-owned drag may return to its press point. Qt then sees a
            # click (its movable-item handler never ran) and collapses the group.
            # Restore only a real drag's original selection, never a plain click.
            self._syncing = True
            try:
                for identifier, item in (*self._items_by_id.items(), *self._route_items_by_id.items()):
                    item.setSelected(identifier in drag_selection)
            finally:
                self._syncing = False
            self._selection_changed()
        if self._mode is not CanvasMode.EDIT or not self._start_positions:
            self.cancel_object_drag(release_mouse=False)
            self._clear_drag_collision_preview()
            return
        release_delta = event.scenePos() - self._body_press_position if self._body_press_position is not None else QPointF()
        if (self._body_drag_threshold_exceeded or release_delta.manhattanLength() * self._view_scale() >= QApplication.startDragDistance()) and self._update_body_attachment(event.scenePos(), event.modifiers()):
            proposal = self._body_attachment_proposal
            self.cancel_object_drag(release_mouse=False)
            if proposal.valid:
                self.equipmentPlacementRequested.emit(proposal)
            else:
                self.connectionStatusMessage.emit(proposal.reason)
            return
        if self._body_routing_error:
            message = self._body_routing_error
            self.cancel_object_drag(release_mouse=False)
            self.connectionStatusMessage.emit(message)
            return
        moved = [
            self._items_by_id[item_id]
            for item_id, start in self._start_positions.items()
            if item_id in self._items_by_id and self._items_by_id[item_id].pos() != start
        ]
        if not moved:
            self.cancel_object_drag(release_mouse=False)
            return
        bypass_snap = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        lead = sorted(moved, key=lambda item: item.representation_id.value)[0]
        lead_start = self._start_positions[lead.representation_id]
        delta = lead.pos() - lead_start
        if self._snap_enabled and not bypass_snap:
            proposed = lead_start + delta
            snapped = QPointF(
                round(proposed.x() / self._grid_size) * self._grid_size,
                round(proposed.y() / self._grid_size) * self._grid_size,
            )
            delta = snapped - lead_start
            for item_id, start in self._start_positions.items():
                item = self._items_by_id.get(item_id)
                if item is not None:
                    item.setPos(start + delta)
        collision = None
        if self._document is not None and self._model is not None:
            check = DiagramCollisionService(
                self._document, self._model
            ).check_move(
                tuple(self._start_positions), delta.x(), delta.y()
            )
            if not check.allowed:
                first = moved[0]
                other = next(
                    (
                        self._items_by_id[item]
                        for item in check.conflicting_ids
                        if item not in self._start_positions
                        and item in self._items_by_id
                    ),
                    first,
                )
                collision = (first, other)
        else:
            collision = self._equipment_collision(moved)
        if collision is not None:
            self.cancel_object_drag(release_mouse=False)
            collision[0].set_target_feedback(
                ConnectionTargetFeedback.INCOMPATIBLE
            )
            collision[1].set_target_feedback(
                ConnectionTargetFeedback.INCOMPATIBLE
            )
            self.connectionStatusMessage.emit(ui_text("status.move_collision"))
            return
        ids = tuple(sorted(self._start_positions, key=lambda item: item.value))
        # The scene preview is never the committed state. Restore it first;
        # one controller command validates and commits the whole group/routes,
        # or its rejection leaves both the document and the display unchanged.
        self.cancel_object_drag(release_mouse=False)
        if not math.isclose(delta.x(), 0.0) or not math.isclose(delta.y(), 0.0):
            self.moveRequested.emit(ids, delta.x(), delta.y(), bypass_snap)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        if self._connected_drag is not None:
            self.update_connected_drag(event.scenePos(), event.modifiers())
            event.accept()
            return
        if self._object_rotation is not None:
            super().mouseMoveEvent(event)
            return
        if (
            self._mode is CanvasMode.EDIT
            and self._start_positions
            and self._body_press_position is not None
            and not isinstance(self.mouseGrabberItem(), DiagramLabelItem)
        ):
            # Qt's internal movable-item origins can survive an ungrabbed,
            # cancelled gesture and treat a different item as starting at (0,0).
            # The scene owns this gesture: derive every frame from its own
            # captured positions, never from the last preview or Qt's cache.
            delta = event.scenePos() - self._body_press_position
            if math.hypot(delta.x(), delta.y()) * self._view_scale() >= QApplication.startDragDistance():
                self._body_drag_threshold_exceeded = True
            for identifier, start in self._start_positions.items():
                item = self._items_by_id.get(identifier)
                if item is not None:
                    item.setPos(start + delta)
            if self._body_drag_threshold_exceeded and self._update_body_attachment(event.scenePos(), event.modifiers()):
                event.accept()
                return
            # A text-child drag is not an apparatus drag. Rebuilding incident
            # routes here would reset its uncommitted manual B3 position.
            self._clear_body_routing_preview()
            self._update_drag_collision_preview()
            if not self._drag_collision_ids:
                self._preview_incident_routes()
            if (
                any(
                    item_id in self._items_by_id
                    and self._items_by_id[item_id].pos() != start
                    for item_id, start in self._start_positions.items()
                )
                and self._tool_state_machine is not None
                and self._tool_state_machine.tool is EditorTool.SELECT
            ):
                transition = self._tool_state_machine.activate(
                    EditorTool.DRAG_OBJECT,
                    tool_name="Перемещение объекта",
                )
                if transition.accepted:
                    self.toolStateChanged.emit(self._tool_state_machine.state)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _update_body_attachment(self, point, modifiers) -> bool:
        """Preview only a single wholly-free apparatus; preserve connected drags."""
        if len(self._start_positions) != 1 or not callable(self._equipment_move_preview):
            return False
        identifier, start = next(iter(self._start_positions.items()))
        item = self._items_by_id.get(identifier)
        equipment = self._model.equipment.get(item.representation.equipment_id) if item is not None else None
        if equipment is None or self._body_press_position is None:
            return False
        if item._canonical_key in {"line", "line_section"}:
            return False
        if any(self._model.node_for_port(port_id) is not None for port_id in equipment.port_ids):
            return False
        proposed = start + point - self._body_press_position
        if self._snap_enabled and not modifiers & Qt.KeyboardModifier.AltModifier:
            proposed = QPointF(round(proposed.x() / self._grid_size) * self._grid_size,
                               round(proposed.y() / self._grid_size) * self._grid_size)
        proposal = self._equipment_move_preview(identifier, proposed)
        if proposal is None:
            return False
        self._body_attachment_proposal = proposal
        original_angle = self._body_preview_angles.setdefault(identifier, item.rotation())
        angle = proposal.rotation_deg if proposal.valid else original_angle
        item.setRotation(angle)
        item._label.setRotation(-angle)
        item.setPos(proposal.x, proposal.y)
        self._clear_drag_collision_preview()
        self._clear_body_routing_preview()
        self._placement_wire_preview.set_proposal(proposal)
        self.relayout_labels()
        item.set_target_feedback(ConnectionTargetFeedback.COMPATIBLE if proposal.valid
                                 else ConnectionTargetFeedback.INCOMPATIBLE)
        self.connectionStatusMessage.emit(proposal.reason if not proposal.valid else (
            "Отпустите кнопку: вставить аппарат в провод" if proposal.action == "inline" else
            "Отпустите кнопку: подключить обычным проводом" if proposal.action == "attach" else
            "Предложено ближайшее свободное положение" if proposal.adjusted else "Перемещение аппарата"))
        return True

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._connected_drag is not None and event.key() == Qt.Key.Key_Escape:
            self.cancel_connected_drag()
            event.accept()
            return
        if self._object_rotation is not None:
            if event.key() == Qt.Key.Key_Escape:
                self.finish_object_rotation(commit=False)
            event.accept()
            return
        if self._start_positions and event.key() == Qt.Key.Key_Escape:
            self.cancel_object_drag(announce=True)
            event.accept()
            return
        modifiers = event.modifiers()
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if self._physical_line_tool.active:
            if event.key() == Qt.Key.Key_Escape:
                self.cancel_physical_line()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Backspace:
                removed = self._physical_line_tool.remove_last_manual_vertex()
                if self._physical_line_tool.cursor_vertex is not None:
                    point = self._physical_line_tool.cursor_vertex
                    self.update_physical_line_cursor(QPointF(point.x, point.y))
                self.connectionStatusMessage.emit(
                    ui_text(
                        "status.connection_waypoint_removed"
                        if removed
                        else "status.connection_no_waypoint"
                    )
                )
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                target = self._physical_line_tool.target
                if target is None:
                    self.finish_physical_line(free_target=True)
                elif target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                    self.finish_physical_line()
                else:
                    self.connectionStatusMessage.emit(target.message)
                event.accept()
                return
        if self._connection_tool.active:
            if event.key() == Qt.Key.Key_Escape:
                self.cancel_connection()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Backspace:
                removed = self._connection_tool.remove_last_manual_vertex()
                if self._connection_tool.cursor_vertex is not None:
                    point = self._connection_tool.cursor_vertex
                    self.update_connection_cursor(QPointF(point.x, point.y))
                self.connectionStatusMessage.emit(
                    ui_text(
                        "status.connection_waypoint_removed"
                        if removed
                        else "status.connection_no_waypoint"
                    )
                )
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                target = self._connection_target
                views = self.views()
                cursor = self._connection_tool.cursor_vertex
                screen_pos = (
                    views[0].viewport().mapToGlobal(
                        views[0].mapFromScene(QPointF(cursor.x, cursor.y))
                    ) if views and cursor is not None else None
                )
                if target is None:
                    self._emit_connection_draft(free_target=True, screen_pos=screen_pos)
                elif target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                    self._emit_connection_draft(screen_pos=screen_pos)
                else:
                    self.connectionStatusMessage.emit(
                        target.message or ui_text("status.connection_incompatible")
                    )
                event.accept()
                return
                event.accept()
                return
        ids = self.selected_representation_ids()
        route_ids = tuple(
            sorted(
                (
                    item.route_id
                    for item in self.selectedItems()
                    if isinstance(item, DiagramRouteItem)
                ),
                key=lambda value: value.value,
            )
        )
        if control and event.key() == Qt.Key.Key_A:
            self._set_object_selection(
                lambda _representation_id, _item: True
            )
            event.accept()
            return
        if control and event.key() == Qt.Key.Key_C:
            if ids:
                self.copyRequested.emit(ids)
            event.accept()
            return
        if control and event.key() == Qt.Key.Key_V:
            if self._mode is CanvasMode.EDIT:
                self.pasteRequested.emit()
            event.accept()
            return
        if control and event.key() == Qt.Key.Key_D:
            if ids and self._mode is CanvasMode.EDIT:
                self.duplicateRequested.emit(ids)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_R,):
            if ids and self._mode is CanvasMode.EDIT:
                self.rotateRequested.emit(ids, -90 if shift else 90)
            elif self._mode is CanvasMode.ANALYSIS:
                self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if ids:
                self.propertiesRequested.emit(ids[0])
            elif route_ids:
                self.propertiesRequested.emit(route_ids[0])
            event.accept()
            return
        if control and event.key() == Qt.Key.Key_Z and not shift:
            if self._mode is CanvasMode.EDIT:
                self.undoRequested.emit()
            else:
                self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            event.accept()
            return
        if (control and event.key() == Qt.Key.Key_Y) or (control and shift and event.key() == Qt.Key.Key_Z):
            if self._mode is CanvasMode.EDIT:
                self.redoRequested.emit()
            else:
                self.connectionStatusMessage.emit(ui_text("status.analysis_locked"))
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if ids and self._mode is CanvasMode.EDIT:
                self.deleteRequested.emit(ids)
            elif route_ids and self._mode is CanvasMode.EDIT:
                self.routeDeleteRequested.emit(route_ids[0])
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            if self._tool_state_machine is not None:
                transition = self._tool_state_machine.escape(
                    has_selection=bool(self.selectedItems())
                )
                if transition.clear_selection:
                    self.clearSelection()
                self.toolStateChanged.emit(self._tool_state_machine.state)
                self.connectionStatusMessage.emit(transition.message)
            else:
                self.placementCancelled.emit()
                self.clearSelection()
            event.accept()
            return
        if self._mode is CanvasMode.EDIT and ids and event.key() in {
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        }:
            step = (
                self._grid_size
                if shift
                else 1.0
            )
            dx = (-step if event.key() == Qt.Key.Key_Left else step if event.key() == Qt.Key.Key_Right else 0.0)
            dy = (-step if event.key() == Qt.Key.Key_Up else step if event.key() == Qt.Key.Key_Down else 0.0)
            if self._document is not None and self._model is not None:
                check = DiagramCollisionService(
                    self._document, self._model
                ).check_move(ids, dx, dy)
                if not check.allowed:
                    for item_id in check.conflicting_ids:
                        item = self._items_by_id.get(item_id)
                        if item is not None:
                            item.set_target_feedback(
                                ConnectionTargetFeedback.INCOMPATIBLE
                            )
                    self.connectionStatusMessage.emit(
                        ui_text("status.move_collision")
                    )
                    event.accept()
                    return
            # Стрелки сами задают точный малый/крупный шаг и не должны повторно
            # округляться контроллером к сетке.
            self.moveRequested.emit(ids, dx, dy, True)
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:  # noqa: N802
        transform = self.views()[0].transform() if self.views() else QTransform()
        raw_item = self.hit_item(event.scenePos(), transform)

        port_item = raw_item if isinstance(raw_item, ElectricalPortItem) else None
        waypoint_item = raw_item if isinstance(raw_item, RouteWaypointHandle) else None
        route_item: DiagramRouteItem | None = None
        object_item: DiagramObjectItem | None = None
        current = raw_item
        while current is not None:
            if route_item is None and isinstance(current, DiagramRouteItem):
                route_item = current
            if object_item is None and isinstance(current, DiagramObjectItem):
                object_item = current
            current = current.parentItem()

        if port_item is not None:
            if self._mode is not CanvasMode.EDIT:
                return
            menu = QMenu(event.widget())
            connected = (
                self._model is not None
                and self._model.connection_for_port(port_item.port_id) is not None
            )
            action = menu.addAction(
                ui_text("action.reconnect" if connected else "action.start_connection")
            )
            if menu.exec(event.screenPos()) is action:
                self.begin_connection(port_item)
            event.accept()
            return

        if route_item is not None:
            self.clearSelection()
            route_item.setSelected(True)
            menu = QMenu(event.widget())
            if waypoint_item is not None and self._mode is CanvasMode.EDIT:
                remove_waypoint = menu.addAction(
                    ui_text("action.remove_route_waypoint")
                )
                if menu.exec(event.screenPos()) is remove_waypoint:
                    route_item.remove_user_waypoint(waypoint_item.waypoint_id)
                event.accept()
                return
            actions: dict[object, str] = {}
            if (self._mode is CanvasMode.EDIT
                    and route_item.route.kind is DiagramRouteKind.NODE_CONNECTION):
                actions[menu.addAction("Начать соединение")] = "start_connection"
            physical = (
                self._model is not None
                and route_item.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
                and route_item.route.equipment_id in self._model.line_sections
            )
            if physical and self._mode is CanvasMode.EDIT:
                actions[menu.addAction(ui_text("action.add_tap"))] = "add_tap"
                actions[menu.addAction(ui_text("action.remove_tap"))] = "remove_tap"
                actions[menu.addAction(ui_text("action.split_line"))] = "split_line"
                actions[menu.addAction(ui_text("action.insert_recloser"))] = "insert_recloser"
                menu.addSeparator()
                actions[menu.addAction(ui_text("action.confirm_length"))] = "confirm_length"
            if self._mode is CanvasMode.EDIT:
                if actions:
                    menu.addSeparator()
                actions[menu.addAction(ui_text("action.add_route_waypoint"))] = "add_route_waypoint"
                actions[menu.addAction("Закрепить изгибы")] = "pin_route_bends"
                actions[menu.addAction("Освободить изгибы")] = "unpin_route_bends"
                actions[menu.addAction("Построить короткий маршрут")] = "auto_route"
                actions[menu.addAction(ui_text("action.delete_connection"))] = "delete_route"
            if not actions:
                return
            chosen = menu.exec(event.screenPos())
            action_name = actions.get(chosen)
            if action_name:
                if action_name == "start_connection":
                    route = route_item.route
                    projected = self._project_to_route(route, event.scenePos())
                    if projected is not None:
                        point = projected[0]
                        self.begin_connection_from_target(ConnectionTarget(
                            ConnectionTargetKind.NODE_CONNECTION, point.x(), point.y(),
                            route.electrical_node_id.value, route_id=route.id.value))
                    event.accept()
                    return
                if action_name == "add_route_waypoint":
                    route_item.add_user_waypoint(event.scenePos())
                    event.accept()
                    return
                route = route_item.route
                payload = {
                    "route_id": route.id,
                    "equipment_id": route.equipment_id,
                    "page_id": route.page_id,
                    "scene_x": event.scenePos().x(),
                    "scene_y": event.scenePos().y(),
                    "route_fraction": self._route_fraction(route, event.scenePos()),
                }
                self.routeContextActionRequested.emit(action_name, payload)
            event.accept()
            return

        item = object_item
        if isinstance(item, DiagramObjectItem) and not item.isSelected():
            if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self.clearSelection()
            item.setSelected(True)
        ids = self.selected_representation_ids()
        if not ids:
            return
        menu = QMenu(event.widget())
        properties_action = menu.addAction(ui_text("action.properties"))
        linked_page_action = (menu.addAction("Перейти на связанный лист")
                              if isinstance(item, DiagramObjectItem)
                              and item.representation.extensions.get("linked_page_id") else None)
        start_connection = None
        if (self._mode is CanvasMode.EDIT and isinstance(item, DiagramObjectItem)
                and item.representation.electrical_node_id is not None):
            start_connection = menu.addAction("Начать соединение")
        rotate_right = None
        rotate_left = None
        auto_orientation = None
        if self._mode is CanvasMode.EDIT:
            menu.addSeparator()
            rotate_right = menu.addAction(ui_text("action.orientation_vertical"))
            rotate_left = menu.addAction(ui_text("action.orientation_horizontal"))
            auto_orientation = menu.addAction(ui_text("action.auto_orientation"))
            menu.addSeparator()
        same = menu.addAction(ui_text("action.select_same_type"))
        copy = menu.addAction(ui_text("action.copy"))
        find_in_tree = menu.addAction(ui_text("action.find_in_tree"))
        switch_action = None
        remove_series_action = None
        port_diagnostics = None
        equipment = None
        if (
            isinstance(item, DiagramObjectItem)
            and item.representation.equipment_id is not None
        ):
            equipment = item._model.equipment.get(
                item.representation.equipment_id
            )
            if item._behavior_key in {"switch", "recloser"}:
                menu.addSeparator()
                switch_action = menu.addAction(
                    ui_text(
                        "action.switch_on"
                        if item._switch_open
                        else "action.switch_off"
                    )
                )
            if (
                self._mode is CanvasMode.EDIT
                and equipment is not None
                and isinstance(equipment.extensions.get("line_insertion"), Mapping)
            ):
                if switch_action is None:
                    menu.addSeparator()
                remove_series_action = menu.addAction(
                    ui_text("action.remove_series_equipment_from_line")
                )
            if self._developer_overlay:
                port_diagnostics = menu.addAction(
                    ui_text("action.port_diagnostics")
                )
        duplicate = None
        delete = None
        remove = None
        if self._mode is CanvasMode.EDIT:
            duplicate = menu.addAction(ui_text("action.duplicate"))
            menu.addSeparator()
            delete = menu.addAction(ui_text("action.delete_project"))
            remove = menu.addAction(ui_text("action.remove_page"))
        chosen = menu.exec(event.screenPos())
        if chosen is properties_action:
            self.propertiesRequested.emit(ids[0])
        elif linked_page_action is not None and chosen is linked_page_action:
            self.linkedPageRequested.emit(item.representation_id)
        elif start_connection is not None and chosen is start_connection:
            point, kind, anchor_key = self._project_to_node_item(item, event.scenePos())
            self.begin_connection_from_target(self._available_bus_target(ConnectionTarget(
                kind, point.x(), point.y(), item.representation.electrical_node_id.value,
                item.representation_id.value, anchor_key=anchor_key)))
        elif chosen is rotate_right:
            self.rotationPositionRequested.emit(ids, 90)
        elif chosen is rotate_left:
            self.rotationPositionRequested.emit(ids, 180)
        elif chosen is auto_orientation:
            self.autoOrientationRequested.emit(ids)
        elif chosen is same:
            self.select_same_type()
        elif chosen is copy:
            self.copyRequested.emit(ids)
        elif chosen is find_in_tree:
            self.propertiesRequested.emit(ids[0])
            self.connectionStatusMessage.emit(
                "Объект выбран в дереве проекта и панели свойств"
            )
        elif chosen is port_diagnostics and equipment is not None:
            rows = []
            for port_id in equipment.port_ids:
                definition = self._model.port_definition(port_id)
                rows.append(
                    f"{definition.display_name} "
                    f"(внутренний код: {definition.role}): {port_id.value}"
                )
            self.propertiesRequested.emit(ids[0])
            self.connectionStatusMessage.emit(
                "Порты: " + ("; ".join(rows) if rows else "отсутствуют")
            )
        elif chosen is duplicate:
            self.duplicateRequested.emit(ids)
        elif chosen is delete:
            self.deleteRequested.emit(ids)
        elif chosen is remove:
            self.removeFromPageRequested.emit(ids)
        elif (
            switch_action is not None
            and chosen is switch_action
            and isinstance(item, DiagramObjectItem)
        ):
            self.equipmentContextActionRequested.emit(
                "switch",
                {
                    "representation_id": item.representation_id,
                    "equipment_id": item.representation.equipment_id,
                    "position": (
                        SwitchPosition.CLOSED
                        if item._switch_open
                        else SwitchPosition.OPEN
                    ),
                },
            )
        elif (
            remove_series_action is not None
            and chosen is remove_series_action
            and isinstance(item, DiagramObjectItem)
        ):
            self.equipmentContextActionRequested.emit(
                "remove_series_equipment_from_line",
                {
                    "representation_id": item.representation_id,
                    "equipment_id": item.representation.equipment_id,
                },
            )
        event.accept()


class DiagramGraphicsView(QGraphicsView):
    """Масштабируемое полотно с rubber-band selection и панорамированием."""

    addEquipmentRequested = Signal(object, float, float)
    equipmentPlacementRequested = Signal(object)
    viewportChanged = Signal(object)
    statusMessage = Signal(str)
    toolStateChanged = Signal(object)

    def __init__(self, scene: DiagramGraphicsScene | None = None, parent: QWidget | None = None):
        self.diagram_scene = scene or DiagramGraphicsScene()
        super().__init__(self.diagram_scene, parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        # Keep the identical rasterized grid between foreground-only updates.
        # Qt invalidates this cache on view changes; set_grid explicitly
        # invalidates BackgroundLayer for visibility and spacing changes.
        self.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.ItemSelectionMode.IntersectsItemShape)
        self.setBackgroundBrush(QColor("#FBFCFE"))
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._panning = False
        self._space_pressed = False
        self._pan_start = QPoint()
        self._placement_payload: object | None = None
        self._external_drag_payload = None
        self._placement_proposal = None
        self._placement_preview_provider = None
        self._equipment_press_position = None
        self._physical_press_position: QPointF | None = None
        self._cursor_scene_pos: QPointF | None = None
        self._drop_feedback_kind: EquipmentPlacementKind | None = None
        self._drop_feedback_point: QPointF | None = None
        self._drop_equipment_point: QPointF | None = None
        self._drop_feedback_route_id: DiagramRouteId | None = None
        self._drop_feedback_orientation: str | None = None
        self._placement_valid = True
        self._placement_conflict_name = ""
        self._placement_rotation_deg = 90
        self._collision_snapshot_key: tuple[int, int, int, int] | None = None
        self._collision_snapshot: DiagramCollisionService | None = None
        self._tool_state = EditorToolStateMachine(EditorMode.EDIT)
        self.diagram_scene._tool_state_machine = self._tool_state
        self.viewportChanged.connect(lambda state: self.diagram_scene.set_label_zoom(state.zoom))

    @property
    def zoom_factor(self) -> float:
        return float(self.transform().m11())

    def viewport_state(self) -> CanvasViewportState:
        center = self.mapToScene(self.viewport().rect().center())
        return CanvasViewportState(self.zoom_factor, center.x(), center.y())

    def restore_viewport(self, state: CanvasViewportState) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(state.zoom)))
        self.resetTransform()
        self.scale(zoom, zoom)
        self.centerOn(state.center_x, state.center_y)
        self.diagram_scene.set_label_zoom(zoom)
        self.diagram_scene._update_rotation_handle()

    def set_zoom(self, factor: float) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, float(factor)))
        current = self.zoom_factor
        if current <= 0:
            return
        self.scale(factor / current, factor / current)
        self.diagram_scene._update_rotation_handle()
        self.viewportChanged.emit(self.viewport_state())

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom_factor * 1.2)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom_factor / 1.2)

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def fit_all(self) -> None:
        items = list(self.diagram_scene._items_by_id.values())
        if not items:
            self.actual_size()
            self.centerOn(0.0, 0.0)
            return
        rect = self.diagram_scene.itemsBoundingRect().adjusted(-60, -60, 60, 60)
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.diagram_scene._update_rotation_handle()
        if self.zoom_factor > MAX_ZOOM:
            self.set_zoom(MAX_ZOOM)
        self.viewportChanged.emit(self.viewport_state())

    @property
    def tool_state(self):
        return self._tool_state.state

    def _emit_tool_state(self, message: str | None = None) -> None:
        self.diagram_scene._rotation_tool_changed()
        self.toolStateChanged.emit(self._tool_state.state)
        self.statusMessage.emit(message or self._tool_state.state.status_text)

    def begin_placement(self, payload: object) -> None:
        self.diagram_scene.cancel_object_drag()
        self.diagram_scene.finish_object_rotation(commit=False)
        options = dict(payload) if isinstance(payload, Mapping) else {"type_id": str(payload)}
        repeat = bool(options.pop("placement_repeat", False))
        tool_name = str(options.get("name") or options.get("type_id") or "Оборудование")
        graphics = _mapping(options.get("graphics"))
        rotation = editor_rotation(_number(graphics, "rotation_deg", 90.0))
        transition = self._tool_state.begin_placement(
            options,
            tool_name,
            repeat=repeat,
            preview_rotation_deg=rotation,
        )
        if not transition.accepted:
            self._emit_tool_state(transition.message)
            return
        self._placement_payload = options
        self._placement_rotation_deg = self._tool_state.state.preview_rotation_deg
        self._placement_valid = True
        self._placement_conflict_name = ""
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._emit_tool_state()
        self.viewport().update()

    def _clear_placement_visuals(self) -> None:
        self._placement_payload = None
        self._external_drag_payload = None
        self._placement_proposal = None
        self._equipment_press_position = None
        self._physical_press_position = None
        self._cursor_scene_pos = None
        self._drop_feedback_kind = None
        self._drop_feedback_point = None
        self._drop_equipment_point = None
        self._drop_feedback_route_id = None
        self._drop_feedback_orientation = None
        self._placement_valid = True
        self._placement_conflict_name = ""
        self._placement_rotation_deg = 90
        self.unsetCursor()
        self.diagram_scene._reset_connection_highlights()
        self.viewport().update()

    def cancel_placement(self, *, announce: bool = True) -> None:
        transition = self._tool_state.cancel_active_tool()
        self._clear_placement_visuals()
        self.diagram_scene.cancel_physical_line(announce=False)
        if announce:
            self._emit_tool_state(transition.message)

    def placement_succeeded(self) -> None:
        transition = self._tool_state.placement_succeeded()
        if not transition.accepted:
            return
        if not self._tool_state.state.is_repeat_placement:
            self._clear_placement_visuals()
        else:
            self._placement_rotation_deg = self._tool_state.state.preview_rotation_deg
            self.viewport().update()
        self._emit_tool_state(transition.message)

    def placement_failed(self, reason: str) -> None:
        self._placement_proposal = None
        transition = self._tool_state.placement_failed(reason)
        self._placement_valid = False
        self._emit_tool_state(transition.message)
        self.viewport().update()

    def _placement_for_payload(
        self, payload: object
    ) -> EquipmentPlacementKind | None:
        if not isinstance(payload, Mapping):
            return None
        if str(payload.get("target_kind", "equipment")) != "equipment":
            return None
        model = self.diagram_scene._model
        if model is None:
            return None
        try:
            type_id = EquipmentTypeId(str(payload["type_id"]))
            type_version = int(payload.get("type_version", 1))
            definition = model.equipment_type(type_id, type_version)
            return EquipmentPlacementRegistry.for_model(
                model.equipment_types.values()
            ).resolve_definition(definition)
        except (KeyError, TypeError, ValueError, DomainInvariantError):
            return None

    def _branch_attachment_equipment_point(self, tap_point: QPointF) -> QPointF:
        """Единая позиция создаваемого ответвлением оборудования."""

        return self._snap_scene_point(
            QPointF(tap_point.x(), tap_point.y() + 140.0)
        )

    def _snap_scene_point(self, point: QPointF) -> QPointF:
        """Вернуть ту же точку сетки, которую сохранит контроллер."""

        if not self.diagram_scene.snap_enabled:
            return QPointF(point)
        step = self.diagram_scene.grid_size
        return QPointF(
            round(point.x() / step) * step,
            round(point.y() / step) * step,
        )

    def _preview_definition(self, payload: object) -> Any | None:
        options = payload if isinstance(payload, Mapping) else {}
        model = self.diagram_scene._model
        if model is None:
            return None
        try:
            return model.equipment_type(
                EquipmentTypeId(str(options["type_id"])),
                int(options.get("type_version", 1)),
            )
        except (KeyError, TypeError, ValueError, DomainInvariantError):
            return None

    def collision_snapshot(self) -> DiagramCollisionService | None:
        """Вернуть снимок collision-индекса для текущих ревизий.

        Движение мыши не пересоздаёт геометрию всей схемы. Замена
        immutable-документа или изменение ревизии модели автоматически
        инвалидирует кэш.
        """

        document = self.diagram_scene._document
        model = self.diagram_scene._model
        if document is None or model is None:
            self._collision_snapshot_key = None
            self._collision_snapshot = None
            return None
        key = (
            id(document),
            int(document.revision),
            id(model),
            int(model.revision),
        )
        if key != self._collision_snapshot_key:
            self._collision_snapshot = DiagramCollisionService(document, model)
            self._collision_snapshot_key = key
        return self._collision_snapshot

    def _equipment_preview_geometry(
        self,
        payload: object,
        point: QPointF,
        *,
        rotation_deg: int | float | None = None,
    ) -> Any | None:
        options = payload if isinstance(payload, Mapping) else {}
        if (
            str(options.get("target_kind", "equipment")) != "equipment"
            or self.diagram_scene.page_id is None
        ):
            return None
        definition = self._preview_definition(payload)
        if definition is None:
            return None
        graphics = _mapping(options.get("graphics"))
        return geometry_for_equipment_preview(
            definition,
            page_id=self.diagram_scene.page_id,
            x=point.x(),
            y=point.y(),
            rotation_deg=(
                self._placement_rotation_deg
                if rotation_deg is None
                else rotation_deg
            ),
            width=max(12.0, _number(graphics, "width", 80.0)),
            height=max(12.0, _number(graphics, "height", 50.0)),
            display_name=str(options.get("name") or definition.display_name),
        )

    def _branch_auto_rotation(
        self,
        payload: object,
        equipment_point: QPointF,
        tap_point: QPointF,
        *,
        terminal_role: str | None = None,
    ) -> int | None:
        definition = self._preview_definition(payload)
        if definition is None or not definition.port_definitions:
            return None
        role = terminal_role or definition.port_definitions[0].role
        try:
            return editor_rotation(quarter_turn_for_port_toward_point(
                definition,
                role,
                equipment_x=equipment_point.x(),
                equipment_y=equipment_point.y(),
                target_x=tap_point.x(),
                target_y=tap_point.y(),
            ))
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _placement_size(options: Mapping) -> tuple[float, float]:
        """Габариты будущего объекта: явные из палитры или свои у символа.

        Прежние 80×50 были общим умолчанием для всего подряд, поэтому призрак
        размещения и свойства поставленного выключателя показывали не тот
        размер, с которым он на самом деле рисуется (64×32).
        """
        graphics = _mapping(options.get("graphics"))
        key = str(options.get("symbol_key") or options.get("symbol") or "")
        default_w, default_h = symbol_default_size(canonical_key(key, "")) if key else (80.0, 50.0)
        return (max(12.0, _number(graphics, "width", default_w)),
                max(12.0, _number(graphics, "height", default_h)))

    @staticmethod
    def _placement_symbol(options: Mapping, width: float, height: float):
        """Условное обозначение для призрака — то же, что у готового объекта.

        Возвращает ``None``, если ключ символа неизвестен: тогда рисуется
        нейтральный прямоугольник, а не похожая на что-нибудь картинка.
        """
        key = str(options.get("symbol_key") or options.get("symbol") or "")
        if not key:
            return None
        try:
            return symbol_for(key, "", width=width, height=height, opened=False)
        except (KeyError, TypeError, ValueError):
            return None

    def _update_equipment_drop_feedback(
        self, payload: object, point: QPointF
    ) -> None:
        self._placement_proposal = None
        if self._placement_payload is None:
            options = payload if isinstance(payload, Mapping) else {}
            graphics = _mapping(options.get("graphics"))
            self._placement_rotation_deg = editor_rotation(_number(graphics, "rotation_deg", 90.0))
        self.diagram_scene._reset_connection_highlights()
        self._drop_feedback_kind = None
        self._drop_feedback_point = None
        self._drop_equipment_point = self._snap_scene_point(point)
        self._drop_feedback_route_id = None
        self._drop_feedback_orientation = None
        self._placement_valid = True
        self._placement_conflict_name = ""
        options = payload if isinstance(payload, Mapping) else {}
        if options.get("target_kind") == "physical_line":
            # A line starts at a point, not inside an apparatus-sized box.
            target = self.diagram_scene._physical_endpoint_at(point)
            if target is not None:
                self._drop_equipment_point = QPointF(target.x, target.y)
                self._placement_valid = target.feedback is not ConnectionTargetFeedback.INCOMPATIBLE
                self.diagram_scene._highlight_endpoint(target, PortVisualState.COMPATIBLE
                    if self._placement_valid else PortVisualState.INCOMPATIBLE)
            self.statusMessage.emit("Укажите начальную точку линии" if not self.diagram_scene.physical_line_active
                                    else "Протяните линию к выводу, шине или свободной точке")
            return
        placement = self._placement_for_payload(payload)
        hit = self.diagram_scene.physical_route_at(point)
        if hit is None and callable(self._placement_preview_provider):
            proposal = self._placement_preview_provider(payload, self._snap_scene_point(point))
            if proposal is not None:
                self._placement_proposal = proposal
                self._drop_equipment_point = QPointF(proposal.x, proposal.y)
                self._placement_rotation_deg = proposal.rotation_deg
                self._placement_valid = proposal.valid
                self._placement_conflict_name = proposal.reason
                self.statusMessage.emit(proposal.reason if not proposal.valid else (
                    "Отпустите кнопку: вставить аппарат в провод с двумя подключёнными выводами"
                    if proposal.action == "inline" else
                    "Отпустите кнопку: подключить обычным проводом"
                    if proposal.action == "attach" else
                    "Предложено ближайшее свободное положение" if proposal.adjusted else
                    "Отпустите кнопку: установить аппарат"))
                return
        if (
            placement is not None
            and placement is not EquipmentPlacementKind.NODE_REPRESENTATION
            and hit is not None
        ):
            route, _ = hit
            route_item = self.diagram_scene._route_items_by_id.get(route.id)
            if route_item is not None:
                route_item.set_target_feedback(ConnectionTargetFeedback.COMPATIBLE)
            projected = self.diagram_scene._project_to_route(route, point)
            self._drop_feedback_kind = placement
            self._drop_feedback_point = self._snap_scene_point(
                projected[0] if projected is not None else point
            )
            self._drop_feedback_orientation = (
                projected[1] if projected is not None else None
            )
            self._drop_feedback_route_id = route.id
            if placement is EquipmentPlacementKind.BRANCH_ATTACHMENT:
                self._drop_equipment_point = (
                    self._branch_attachment_equipment_point(
                        self._drop_feedback_point
                    )
                )
            else:
                self._drop_equipment_point = self._drop_feedback_point
            options = payload if isinstance(payload, Mapping) else {}
            graphics = _mapping(options.get("graphics"))
            try:
                orientation_mode = normalize_orientation_mode(
                    str(graphics.get("orientation_mode", "auto"))
                )
            except ValueError:
                orientation_mode = OrientationMode.AUTO
            if orientation_mode is OrientationMode.AUTO:
                if placement is EquipmentPlacementKind.INLINE_SERIES:
                    segment = closest_directed_segment(
                        ((item.x, item.y) for item in route.waypoints),
                        point.x(),
                        point.y(),
                    )
                    if segment is not None:
                        self._placement_rotation_deg = editor_rotation(segment.quarter_turn)
                elif placement is EquipmentPlacementKind.BRANCH_ATTACHMENT:
                    rotation = self._branch_auto_rotation(
                        payload,
                        self._drop_equipment_point,
                        self._drop_feedback_point,
                    )
                    if rotation is not None:
                        self._placement_rotation_deg = rotation
            if placement is EquipmentPlacementKind.INLINE_SERIES:
                message = "Отпустите кнопку, чтобы вставить аппарат в линию"
            elif placement is EquipmentPlacementKind.BRANCH_ATTACHMENT:
                message = "Отпустите кнопку, чтобы создать отпайку к оборудованию"
            else:
                message = "Отпустите кнопку, чтобы выбрать способ подключения"
            self.statusMessage.emit(message)
        self._placement_valid, self._placement_conflict_name = (
            self._check_placement_collision(payload, point)
        )
        if not self._placement_valid:
            self.statusMessage.emit(
                ui_text(
                    "status.placement_collision",
                    name=self._placement_conflict_name or "другим оборудованием",
                )
            )

    def _check_placement_collision(
        self,
        payload: object,
        point: QPointF,
        *,
        candidate_point: QPointF | None = None,
        rotation_deg: int | float | None = None,
    ) -> tuple[bool, str]:
        options = payload if isinstance(payload, Mapping) else {}
        target_kind = str(options.get("target_kind", "equipment"))
        if target_kind == "physical_line":
            return True, ""
        width, height = self._placement_size(options)
        preview_point = (
            candidate_point
            or self._drop_equipment_point
            or self._drop_feedback_point
            or point
        )
        preview_rotation = (
            self._placement_rotation_deg
            if rotation_deg is None
            else rotation_deg
        )
        if (
            target_kind == "equipment"
            and self.diagram_scene._document is not None
            and self.diagram_scene._model is not None
            and self.diagram_scene.page_id is not None
        ):
            try:
                candidate = self._equipment_preview_geometry(
                    payload,
                    preview_point,
                    rotation_deg=preview_rotation,
                )
                if candidate is None:
                    raise KeyError(
                        "Не удалось построить предварительное изображение оборудования."
                    )
                snapshot = self.collision_snapshot()
                if snapshot is None:
                    raise KeyError("Нет актуального снимка схемы.")
                result = snapshot.check_placement(candidate)
                if result.allowed:
                    return True, ""
                conflict = result.conflicts[0]
                other_id = (
                    conflict.second_id
                    if conflict.first_id == candidate.representation_id
                    else conflict.first_id
                )
                other = self.diagram_scene._items_by_id.get(other_id)
                return False, other._name if other is not None else result.message
            except (KeyError, TypeError, ValueError, DomainInvariantError):
                pass
        if int(preview_rotation) % 180:
            width, height = height, width
        clearance = DEFAULT_SAFE_GAP
        area = QRectF(
            preview_point.x() - width / 2.0 - clearance,
            preview_point.y() - height / 2.0 - clearance,
            width + clearance * 2.0,
            height + clearance * 2.0,
        )
        seen: set[GraphicalRepresentationId] = set()
        for raw_item in self.diagram_scene.items(area):
            item = self.diagram_scene._object_ancestor(raw_item)
            if item is None or item.representation_id in seen:
                continue
            seen.add(item.representation_id)
            if not area.intersects(item.body_scene_rect(clearance=0.0)):
                continue
            # Точки соединения не являются телами оборудования. Шины, напротив,
            # остаются препятствиями, кроме явной электрической операции по порту.
            if (
                item.representation.equipment_id is None
                and "busbar" not in item._symbol_key.casefold()
                and item._behavior_key != "bus"
            ):
                continue
            return False, item._name
        return True, ""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        factor = math.pow(1.0015, event.angleDelta().y())
        target = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom_factor * factor))
        factor = target / self.zoom_factor
        self.scale(factor, factor)
        self.diagram_scene._update_rotation_handle()
        self.viewportChanged.emit(self.viewport_state())
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if (
            self.diagram_scene._object_rotation is not None
        ):
            if event.key() == Qt.Key.Key_Escape:
                self.diagram_scene.finish_object_rotation(commit=False)
            event.accept()
            return
        if self.diagram_scene._connected_drag is not None and event.key() == Qt.Key.Key_Escape:
            self.diagram_scene.cancel_connected_drag()
            event.accept()
            return
        if self.diagram_scene._start_positions and event.key() == Qt.Key.Key_Escape:
            self.diagram_scene.cancel_object_drag(announce=True)
            event.accept()
            return
        if (
            event.key() == Qt.Key.Key_R
            and self._placement_payload is not None
        ):
            transition = self._tool_state.begin_preview_rotation(
                clockwise=not bool(
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ),
                current_rotation_deg=self._placement_rotation_deg,
            )
            if transition.accepted:
                self._tool_state.finish_preview_rotation()
                self._placement_rotation_deg = (
                    self._tool_state.state.preview_rotation_deg
                )
                options = dict(self._placement_payload) if isinstance(
                    self._placement_payload, Mapping
                ) else {}
                graphics = dict(_mapping(options.get("graphics")))
                graphics["rotation_deg"] = self._placement_rotation_deg
                graphics["orientation_mode"] = "manual"
                options["graphics"] = graphics
                self._placement_payload = options
                self._emit_tool_state(transition.message)
                if self._cursor_scene_pos is not None:
                    self._update_equipment_drop_feedback(
                        self._placement_payload,
                        self._cursor_scene_pos,
                    )
                self.viewport().update()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if self._placement_payload is not None:
                self.cancel_placement(announce=False)
            else:
                self.diagram_scene.cancel_connection(announce=False)
                self.diagram_scene.cancel_physical_line(announce=False)
            self._space_pressed = True
            self._tool_state.activate(EditorTool.PAN)
            self._emit_tool_state()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._placement_payload is not None:
            transition = self._tool_state.escape(
                has_selection=bool(self.diagram_scene.selectedItems())
            )
            self._clear_placement_visuals()
            self.diagram_scene.cancel_physical_line(announce=False)
            self._emit_tool_state(transition.message)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = False
            if not self._panning:
                self.unsetCursor()
                self._tool_state.select_tool()
                self._emit_tool_state()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._placement_payload is not None
            and event.button() == Qt.MouseButton.RightButton
        ):
            self.cancel_placement()
            event.accept()
            return
        if (
            self._placement_payload is not None
            and self.diagram_scene.physical_line_active
            and event.button() == Qt.MouseButton.LeftButton
        ):
            point = self.mapToScene(event.position().toPoint())
            if self.diagram_scene.handle_physical_line_click(point):
                self.cancel_placement()
            event.accept()
            return
        if self._placement_payload is not None and event.button() == Qt.MouseButton.LeftButton:
            point = self.mapToScene(event.position().toPoint())
            self._update_equipment_drop_feedback(self._placement_payload, point)
            if not self._placement_valid:
                self.placement_failed(
                    "пересечение с "
                    + (self._placement_conflict_name or "другим оборудованием")
                )
                event.accept()
                return
            if isinstance(self._placement_payload, Mapping) and self._placement_payload.get("target_kind") == "physical_line":
                self.addEquipmentRequested.emit(self._placement_payload, point.x(), point.y())
                if self.diagram_scene.physical_line_active:
                    self._physical_press_position = QPointF(point)
            else:
                self._equipment_press_position = QPointF(point)
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._space_pressed
        ):
            self.diagram_scene.cancel_object_drag()
            self.diagram_scene.finish_object_rotation(commit=False)
            if self._placement_payload is not None:
                self.cancel_placement(announce=False)
            else:
                self.diagram_scene.cancel_connection(announce=False)
                self.diagram_scene.cancel_physical_line(announce=False)
            self._panning = True
            self._tool_state.activate(EditorTool.PAN)
            self._emit_tool_state()
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            scene_point = self.mapToScene(event.position().toPoint())
            target = self.diagram_scene.resolve_hit_target(
                scene_point,
                self.transform(),
            )
            raw_item = target.item
            if (
                target.kind is HitTestKind.CANVAS
                and self._tool_state.tool is not EditorTool.DRAW_CONNECTION
            ):
                transition = self._tool_state.activate(
                    EditorTool.MARQUEE_SELECT,
                    tool_name="Рамочное выделение",
                )
                if transition.accepted:
                    self._emit_tool_state()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._cursor_scene_pos = self.mapToScene(event.position().toPoint())
        self.diagram_scene.update_connection_drag(self._cursor_scene_pos)
        self.diagram_scene.update_connection_cursor(self._cursor_scene_pos)
        self.diagram_scene.update_physical_line_cursor(self._cursor_scene_pos)
        if self._panning:
            delta = event.position().toPoint() - self._pan_start
            self._pan_start = event.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
        else:
            super().mouseMoveEvent(event)
        if self._placement_payload is not None:
            self._update_equipment_drop_feedback(
                self._placement_payload, self._cursor_scene_pos
            )
            self.viewport().update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._equipment_press_position is not None:
            self._equipment_press_position = None
            if self._placement_payload is not None:
                point = self.mapToScene(event.position().toPoint())
                self._update_equipment_drop_feedback(self._placement_payload, point)
                if self._placement_valid:
                    if self._placement_proposal is not None:
                        self.equipmentPlacementRequested.emit(self._placement_proposal)
                    else:
                        self.addEquipmentRequested.emit(self._placement_payload, point.x(), point.y())
                else:
                    self.statusMessage.emit(self._placement_conflict_name)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._physical_press_position is not None:
            start = self._physical_press_position
            self._physical_press_position = None
            point = self.mapToScene(event.position().toPoint())
            delta = point - start
            if (self.diagram_scene.physical_line_active
                    and math.hypot(delta.x(), delta.y()) * self.zoom_factor >= QApplication.startDragDistance()):
                self.diagram_scene.update_physical_line_cursor(point)
                target = self.diagram_scene._physical_line_tool.target
                if target is None or target.feedback is ConnectionTargetFeedback.COMPATIBLE:
                    self.diagram_scene.finish_physical_line(free_target=target is None)
                else:
                    self.statusMessage.emit(target.message)
                    self.cancel_placement(announce=False)
                event.accept()
                return
        if self._panning and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self._panning = False
            self._tool_state.select_tool()
            self._emit_tool_state()
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._space_pressed else Qt.CursorShape.ArrowCursor)
            self.viewportChanged.emit(self.viewport_state())
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._tool_state.tool is EditorTool.MARQUEE_SELECT
        ):
            self._tool_state.select_tool()
            self._emit_tool_state()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        if self._physical_press_position is not None or self._equipment_press_position is not None:
            self.cancel_placement(announce=False)
        self.diagram_scene.cancel_connection_drag()
        self.diagram_scene.cancel_object_drag()
        self.diagram_scene.finish_object_rotation(commit=False)
        super().focusOutEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.UngrabMouse and not self.diagram_scene._body_drag_releasing:
            if self._physical_press_position is not None or self._equipment_press_position is not None:
                self.cancel_placement(announce=False)
            self.diagram_scene.cancel_connection_drag()
            self.diagram_scene.cancel_object_drag(release_mouse=False)
        return super().viewportEvent(event)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        # Qt's background-cache painter does not inherit the viewport hints.
        # Preserve the same antialiased grid raster in both paint paths.
        painter.setRenderHints(self.renderHints())
        painter.fillRect(rect, QColor("#FBFCFE"))
        if not self.diagram_scene.grid_visible:
            return
        step = self.diagram_scene.grid_size
        zoom = max(self.zoom_factor, MIN_ZOOM)
        while step * zoom < 7.0:
            step *= 5.0
        left = math.floor(rect.left() / step) * step
        top = math.floor(rect.top() / step) * step
        minor = QPen(QColor(219, 226, 237, 150), 0)
        major = QPen(QColor(195, 206, 222, 180), 0)
        x = left
        index = int(round(left / step))
        while x <= rect.right():
            painter.setPen(major if index % 5 == 0 else minor)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
            index += 1
        y = top
        index = int(round(top / step))
        while y <= rect.bottom():
            painter.setPen(major if index % 5 == 0 else minor)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step
            index += 1

    @staticmethod
    def _inline_split_feedback_geometry(
        target: QPointF,
        orientation: str | None,
        scale: float,
    ) -> tuple[QRectF, tuple[tuple[QPointF, QPointF], ...]]:
        """Геометрия маркера разрыва вдоль фактического сегмента линии."""

        length = 24.0 / max(scale, MIN_ZOOM)
        thickness = 14.0 / max(scale, MIN_ZOOM)
        if orientation == "vertical":
            return (
                QRectF(
                    target.x() - thickness / 2.0,
                    target.y() - length / 2.0,
                    thickness,
                    length,
                ),
                (
                    (
                        QPointF(target.x(), target.y() - length),
                        QPointF(target.x(), target.y() - length / 2.0),
                    ),
                    (
                        QPointF(target.x(), target.y() + length / 2.0),
                        QPointF(target.x(), target.y() + length),
                    ),
                ),
            )
        return (
            QRectF(
                target.x() - length / 2.0,
                target.y() - thickness / 2.0,
                length,
                thickness,
            ),
            (
                (
                    QPointF(target.x() - length, target.y()),
                    QPointF(target.x() - length / 2.0, target.y()),
                ),
                (
                    QPointF(target.x() + length / 2.0, target.y()),
                    QPointF(target.x() + length, target.y()),
                ),
            ),
        )

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        del rect
        payload = self._placement_payload or self._external_drag_payload
        if payload is None or self._cursor_scene_pos is None:
            return
        options = payload if isinstance(payload, Mapping) else {}
        if options.get("target_kind") == "physical_line":
            if self.diagram_scene.physical_line_active:
                return  # The real scene preview owns the start, trace and end.
            point = self._drop_equipment_point or self._snap_scene_point(self._cursor_scene_pos)
            scale = max(self.zoom_factor, MIN_ZOOM)
            color = QColor("#16A34A" if self._placement_valid else COLORS["red"])
            pen = QPen(color, 1.4)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            radius = 8.0 / scale
            painter.drawEllipse(point, radius, radius)
            painter.setBrush(color)
            painter.drawEllipse(point, 2.5 / scale, 2.5 / scale)
            return
        point = self._cursor_scene_pos
        if self.diagram_scene.snap_enabled:
            step = self.diagram_scene.grid_size
            point = QPointF(round(point.x() / step) * step, round(point.y() / step) * step)
        pen = QPen(QColor(COLORS["blue"]), 1.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        span = 24.0 / max(self.zoom_factor, MIN_ZOOM)
        painter.drawLine(QPointF(point.x() - span, point.y()), QPointF(point.x() + span, point.y()))
        painter.drawLine(QPointF(point.x(), point.y() - span), QPointF(point.x(), point.y() + span))
        preview_point = (
            self._drop_equipment_point or self._drop_feedback_point or point
        )
        options = (
            payload
            if isinstance(payload, Mapping)
            else {}
        )
        width, height = self._placement_size(options)
        preview_color = QColor(
            COLORS["blue"] if self._placement_valid else COLORS["red"]
        )
        painter.save()
        painter.translate(preview_point)
        painter.rotate(float(self._placement_rotation_deg))
        body_pen = QPen(preview_color, 1.7, Qt.PenStyle.DashLine)
        body_pen.setCosmetic(True)
        painter.setPen(body_pen)
        painter.setBrush(QColor(preview_color.red(), preview_color.green(), preview_color.blue(), 24))
        body = QRectF(-width / 2.0, -height / 2.0, width, height)
        #  Призрак размещения рисуется ТЕМИ ЖЕ примитивами, что и поставленный
        #  объект. Раньше здесь был отдельный набор рисунков от руки: у
        #  выключателя и разъединителя он был ОДИН И ТОТ ЖЕ — две чёрточки и
        #  диагональ, — и человек, выбрав в палитре выключатель, видел под
        #  курсором чужое обозначение. Это ровно та «вторая графика», которую
        #  проект уже один раз убирал: два рисунка одного и того же неизбежно
        #  расходятся.
        preview_symbol = self._placement_symbol(options, width, height)
        if preview_symbol is None:
            painter.drawRoundedRect(body, 5.0, 5.0)
        else:
            for primitive in preview_symbol.primitives:
                if primitive.direction_marker:
                    continue          # стрелка принадлежит трассе, не призраку
                center = primitive.center or (0.0, 0.0)
                if primitive.kind == "rect":
                    painter.drawRect(QRectF(
                        center[0] - float(primitive.half_width or 0.0),
                        center[1] - float(primitive.half_height or 0.0),
                        2.0 * float(primitive.half_width or 0.0),
                        2.0 * float(primitive.half_height or 0.0),
                    ))
                elif primitive.kind in {"circle", "ellipse", "arc"}:
                    painter.drawEllipse(
                        QPointF(*center),
                        float(primitive.radius or 0.0),
                        float(primitive.radius_y or primitive.radius or 0.0),
                    )
                elif primitive.points:
                    painter.drawPolyline(QPolygonF(
                        [QPointF(*point) for point in primitive.points]
                    ))
        clearance = DEFAULT_SAFE_GAP
        clearance_pen = QPen(preview_color, 0.8, Qt.PenStyle.DotLine)
        clearance_pen.setCosmetic(True)
        painter.setPen(clearance_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            body.adjusted(-clearance, -clearance, clearance, clearance), 6.0, 6.0
        )
        painter.restore()
        preview_geometry = self._equipment_preview_geometry(
            payload,
            preview_point,
        )
        if preview_geometry is not None:
            port_pen = QPen(preview_color, 1.4)
            port_pen.setCosmetic(True)
            painter.setPen(port_pen)
            painter.setBrush(QColor("#FFFFFF"))
            port_radius = 3.5 / max(self.zoom_factor, MIN_ZOOM)
            for zone in preview_geometry.port_connection_zones:
                painter.drawEllipse(
                    QPointF(zone.shape.center_x, zone.shape.center_y),
                    port_radius,
                    port_radius,
                )
        if self._placement_proposal is not None:
            from .equipment_placement_preview import paint_proposal_wires
            paint_proposal_wires(painter, self._placement_proposal, max(self.zoom_factor, MIN_ZOOM))
            return
        if self._drop_feedback_point is None or self._drop_feedback_kind is None:
            return
        target = self._drop_feedback_point
        scale = max(self.zoom_factor, MIN_ZOOM)
        halo = 11.0 / scale
        feedback_pen = QPen(
            QColor(COLORS["blue"] if self._placement_valid else COLORS["red"]),
            2.0,
        )
        feedback_pen.setCosmetic(True)
        painter.setPen(feedback_pen)
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawEllipse(target, halo, halo)
        if self._drop_feedback_kind is EquipmentPlacementKind.INLINE_SERIES:
            marker, leads = self._inline_split_feedback_geometry(
                target,
                self._drop_feedback_orientation,
                scale,
            )
            painter.drawRect(marker)
            for first, second in leads:
                painter.drawLine(first, second)
        else:
            painter.setBrush(
                QColor(COLORS["blue"] if self._placement_valid else COLORS["red"])
            )
            painter.drawEllipse(target, 3.5 / scale, 3.5 / scale)
            painter.drawLine(
                target,
                preview_point,
            )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasFormat(EQUIPMENT_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        if event.mimeData().hasFormat(EQUIPMENT_MIME_TYPE):
            try:
                raw = bytes(
                    event.mimeData().data(EQUIPMENT_MIME_TYPE)
                ).decode("utf-8", errors="strict")
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            point = self.mapToScene(event.position().toPoint())
            self._cursor_scene_pos = point
            self._external_drag_payload = payload
            self._update_equipment_drop_feedback(payload, point)
            self.viewport().update()
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self._external_drag_payload = None
        self._placement_proposal = None
        self.diagram_scene._reset_connection_highlights()
        self._drop_feedback_kind = None
        self._drop_feedback_point = None
        self._drop_equipment_point = None
        self._drop_feedback_route_id = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(EQUIPMENT_MIME_TYPE):
            super().dropEvent(event)
            return
        raw = bytes(event.mimeData().data(EQUIPMENT_MIME_TYPE)).decode("utf-8", errors="strict")
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        point = self.mapToScene(event.position().toPoint())
        self._update_equipment_drop_feedback(payload, point)
        if not self._placement_valid:
            self.statusMessage.emit(
                ui_text(
                    "status.placement_collision",
                    name=self._placement_conflict_name or "другим оборудованием",
                )
            )
            event.ignore()
            self._external_drag_payload = None
            self._placement_proposal = None
            self._drop_feedback_point = None
            self._drop_equipment_point = None
            self.viewport().update()
            return
        proposal = self._placement_proposal
        self._external_drag_payload = None
        self._placement_proposal = None
        self.diagram_scene._reset_connection_highlights()
        self._drop_feedback_kind = None
        self._drop_feedback_point = None
        self._drop_equipment_point = None
        self._drop_feedback_route_id = None
        if isinstance(payload, Mapping):
            effective = dict(payload)
            graphics = dict(_mapping(effective.get("graphics")))
            graphics["rotation_deg"] = self._placement_rotation_deg
            graphics.setdefault("orientation_mode", "auto")
            effective["graphics"] = graphics
            payload = effective
        if proposal is not None:
            self.equipmentPlacementRequested.emit(proposal)
        else:
            self.addEquipmentRequested.emit(payload, point.x(), point.y())
        event.acceptProposedAction()


class EditorCanvas(QWidget):
    """Связка холста с нейтральным ``ProjectEditorController``.

    Контроллер не зависит от Qt и остаётся единственным владельцем командной
    истории.  GUI никогда не создаёт вторую электрическую модель.
    """

    selectionChanged = Signal(object)
    routeSelectionChanged = Signal(object)
    commandCompleted = Signal(object)
    errorOccurred = Signal(str)
    statusMessage = Signal(str)
    deleteConfirmationRequested = Signal(object)
    routeDeleteConfirmationRequested = Signal(object)
    propertiesRequested = Signal(object)
    toolStateChanged = Signal(object)
    placementFinished = Signal(object)

    def __init__(
        self,
        controller: Any,
        parent: QWidget | None = None,
        *,
        confirm_deletions: bool = False,
    ):
        super().__init__(parent)
        self.controller = controller
        self.confirm_deletions = bool(confirm_deletions)
        self._pending_tap_branch: PendingTapBranch | None = None
        self._navigation_back = []
        self._navigation_highlight = None
        self._navigation_timer = QTimer(self)
        self._navigation_timer.setSingleShot(True)
        self._navigation_timer.timeout.connect(self._clear_navigation_highlight)
        self.scene = DiagramGraphicsScene(self)
        self.scene._bus_attachment_preview = getattr(controller, "preview_bus_attachment_move", None)
        self.scene._move_preview = getattr(controller, "preview_move_representations", None)
        self.scene._route_segment_constraints = getattr(controller, "preview_route_segment_constraints", None)
        self.scene._connection_voltage_preview = getattr(controller, "preview_connection_voltage", None)
        self.scene._bus_exit_direction_preview = self._preview_bus_exit_direction
        self.view = DiagramGraphicsView(self.scene, self)
        self.view._placement_preview_provider = self._preview_equipment_placement
        self.scene._equipment_move_preview = self._preview_existing_equipment
        self.hint = QLabel(ui_text("canvas.edit_hint"))
        self.hint.setObjectName("muted")
        self.hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 2, 12, 6)
        self.connect_button = QPushButton("Соединить", self)
        self.connect_button.setToolTip("Протяните от вывода, шины или проводника. Выберите провод, ВЛ или КЛ после отпускания.")
        self.connect_button.clicked.connect(self.activate_connection_tool)
        footer.addWidget(self.connect_button)
        self.back_button = QPushButton("Назад", self)
        self.back_button.setEnabled(False)
        self.back_button.setToolTip("Вернуться к месту перехода между листами (Alt+←)")
        self.back_button.clicked.connect(self.navigate_back)
        footer.addWidget(self.back_button)
        self._back_shortcut = QShortcut(QKeySequence("Alt+Left"), self)
        self._back_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._back_shortcut.activated.connect(self.navigate_back)
        footer.addWidget(self.hint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)
        layout.addLayout(footer)

        self.scene.representationSelectionChanged.connect(self.selectionChanged.emit)
        self.scene.moveRequested.connect(self._move)
        self.scene.equipmentPlacementRequested.connect(self._apply_prepared_equipment)
        self.scene.labelMoveRequested.connect(self._move_label)
        self.scene.routeLabelMoveRequested.connect(self._move_route_label)
        self.scene.copyRequested.connect(self.copy)
        self.scene.pasteRequested.connect(self.paste)
        self.scene.duplicateRequested.connect(self.duplicate)
        self.scene.deleteRequested.connect(self.delete_from_project)
        self.scene.removeFromPageRequested.connect(self.remove_from_page)
        self.scene.undoRequested.connect(self.undo)
        self.scene.redoRequested.connect(self.redo)
        self.scene.connectionDraftRequested.connect(self._commit_connection_draft)
        self.scene.connectionDragDraftRequested.connect(self._commit_dragged_connection)
        self.scene.routeSelectionChanged.connect(self.routeSelectionChanged.emit)
        self.scene.physicalLineDraftRequested.connect(self._commit_physical_line_draft)
        self.scene.connectionStatusMessage.connect(self.statusMessage.emit)
        self.scene.placementCancelled.connect(self._clear_pending_tap_branch)
        self.scene.routeContextActionRequested.connect(self._route_context_action)
        self.scene.routeDeleteRequested.connect(self.delete_route)
        self.scene.equipmentContextActionRequested.connect(
            self._equipment_context_action
        )
        self.scene.routeWaypointEditRequested.connect(self._edit_route_waypoints)
        self.scene.routeSegmentMoveRequested.connect(self._move_route_segment)
        self.scene.busAttachmentMoveRequested.connect(self._move_bus_attachment)
        self.scene.propertiesRequested.connect(self.propertiesRequested.emit)
        self.scene.linkedPageRequested.connect(self.navigate_linked_page)
        self.scene.rotateRequested.connect(self._rotate_selected)
        self.scene.rotationPositionRequested.connect(self._set_selected_orientation)
        self.scene.autoOrientationRequested.connect(self._auto_orient_selected)
        self.scene.toolStateChanged.connect(self.toolStateChanged.emit)
        self.view.addEquipmentRequested.connect(self._add_equipment)
        self.view.equipmentPlacementRequested.connect(self._apply_prepared_equipment)
        self.view.viewportChanged.connect(self._save_viewport)
        self.view.statusMessage.connect(self.statusMessage.emit)
        self.view.toolStateChanged.connect(self.toolStateChanged.emit)
        self.scene.placementCancelled.connect(
            lambda: self.view.cancel_placement(announce=False)
        )
        self.refresh()

    def activate_connection_tool(self) -> None:
        if self.scene._mode is not CanvasMode.EDIT:
            return
        self.view.cancel_placement(announce=False)
        self.scene.cancel_connection(announce=False)
        self.view._tool_state.activate(EditorTool.DRAW_CONNECTION, tool_name="Соединить")
        self.view._emit_tool_state("Протяните соединение от вывода, шины или проводника")
        self.view.setFocus()

    def _clear_pending_tap_branch(self) -> None:
        self._pending_tap_branch = None

    @property
    def page_id(self) -> PageId | None:
        return self.scene.page_id

    def refresh(self, *, keep_selection: bool = True) -> None:
        self._clear_navigation_highlight()
        selected = self.scene.selected_representation_ids() if keep_selection else ()
        page_id = self.scene.page_id
        if not getattr(self.controller, "diagram").pages:
            self._call("ensure_default_page")
        if page_id is None:
            active_page = getattr(
                getattr(self.controller, "workspace_state", None),
                "active_page_id",
                None,
            )
            if active_page:
                candidate = PageId(str(active_page))
                if candidate in self.controller.diagram.pages:
                    page_id = candidate
        self.scene.sync_document(
            self.controller.diagram,
            self.controller.model,
            page_id=page_id,
            operating_state_id=getattr(
                self.controller, "active_operating_state_id", None
            ),
        )
        self.scene.set_mode(getattr(self.controller, "mode", CanvasMode.EDIT))
        self.connect_button.setEnabled(self.scene._mode is CanvasMode.EDIT)
        if selected:
            self.scene.select_representations(selected)
        self._restore_workspace_settings()
        conflicts = self.scene.label_layout_conflicts()
        if conflicts and self.scene._label_options[0]:
            count = len({row.representation_id for row in conflicts})
            self.hint.setText(f"Подписи с пересечениями: {count}. Сохранённые положения не изменены. Меню «Подписи» → «Автоподписи».")
        else:
            self.hint.setText(ui_text("canvas.analysis_hint" if self.scene._mode is CanvasMode.ANALYSIS else "canvas.edit_hint"))

    def show_page(self, page_id: PageId) -> None:
        self._clear_navigation_highlight()
        setter = getattr(self.controller, "set_active_page", None)
        if callable(setter):
            self._call("set_active_page", page_id)
        self.scene.sync_document(
            self.controller.diagram,
            self.controller.model,
            page_id=page_id,
            operating_state_id=getattr(
                self.controller, "active_operating_state_id", None
            ),
        )

    def _clear_navigation_highlight(self) -> None:
        self._navigation_timer.stop()
        if self._navigation_highlight is not None:
            self.scene.removeItem(self._navigation_highlight)
            self._navigation_highlight = None

    def navigate_linked_page(self, representation_id) -> bool:
        from .page_navigation import linked_page_target

        try:
            target = linked_page_target(self.controller.diagram, representation_id)
        except (ValueError, TypeError) as exc:
            self.statusMessage.emit(str(exc))
            return False
        previous = (self.page_id, self.view.viewport_state(),
                    self.scene.selected_representation_ids(), self.scene.selected_route_ids())
        self.view.cancel_placement(announce=False)
        self.scene.cancel_connection(announce=False)
        self.scene.cancel_object_drag()
        self.scene.finish_object_rotation(commit=False)
        self.show_page(target.page_id)
        self.scene.select_representations((target.id,))
        self.view.centerOn(target.x, target.y)
        self.view.viewportChanged.emit(self.view.viewport_state())
        radius = 16.0 / max(self.view.zoom_factor, MIN_ZOOM)
        path = QPainterPath()
        path.addEllipse(QPointF(target.x, target.y), radius, radius)
        marker = QGraphicsPathItem(path)
        pen = QPen(QColor("#2563EB"), 2.5)
        pen.setCosmetic(True)
        marker.setPen(pen)
        marker.setBrush(QColor(37, 99, 235, 30))
        marker.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        marker.setZValue(1000)
        self.scene.addItem(marker)
        self._navigation_highlight = marker
        self._navigation_timer.start(2200)
        if previous[0] is not None:
            self._navigation_back.append(previous)
            self._navigation_back = self._navigation_back[-100:]
        self.back_button.setEnabled(bool(self._navigation_back))
        self.view.setFocus()
        self.statusMessage.emit(f"Связанный объект: {self.controller.diagram.pages[target.page_id].name}. Вернуться — «Назад».")
        return True

    def navigate_back(self) -> bool:
        while self._navigation_back:
            page_id, viewport, representations, routes = self._navigation_back.pop()
            if page_id not in self.controller.diagram.pages:
                continue
            self.view.cancel_placement(announce=False)
            self.show_page(page_id)
            self.scene.select_representations(representations)
            for route_id in routes:
                item = self.scene._route_items_by_id.get(route_id)
                if item is not None:
                    item.setSelected(True)
            self.view.restore_viewport(viewport)
            self.view.viewportChanged.emit(self.view.viewport_state())
            self.back_button.setEnabled(bool(self._navigation_back))
            self.view.setFocus()
            self.statusMessage.emit("Возврат к месту межлистового перехода")
            return True
        self.back_button.setEnabled(False)
        return False

    def set_mode(self, mode: CanvasMode | str) -> None:
        result = self._call("set_mode", mode)
        if result is not None:
            self.scene.set_mode(getattr(self.controller, "mode", mode))
            analysis = self.scene._mode is CanvasMode.ANALYSIS
            self.connect_button.setEnabled(not analysis)
            transition = self.view._tool_state.set_editor_mode(
                EditorMode.ANALYSIS if analysis else EditorMode.EDIT
            )
            if transition.cancelled:
                self.view._clear_placement_visuals()
                self.scene.cancel_connection(announce=False)
                self.scene.cancel_physical_line(announce=False)
            self.view._emit_tool_state(transition.message)
            self.hint.setText(ui_text("canvas.analysis_hint" if analysis else "canvas.edit_hint"))

    def set_grid_visible(self, enabled: bool) -> None:
        self._call("set_grid", enabled)
        self.scene.set_grid(visible=enabled)

    def set_snap_enabled(self, enabled: bool) -> None:
        self._call("set_snap", enabled)
        self.scene.set_snap_enabled(enabled)

    def set_label_display(self, **options) -> None:
        self._call("set_label_display", **options)
        self.refresh()

    def reset_label_positions(self, ids=None) -> None:
        values = tuple(ids or self.scene.selected_representation_ids())
        routes = self.scene.selected_route_ids()
        if not values and not routes:
            values = tuple(self.scene._items_by_id)
            routes = tuple(item.route_id for item in self.scene._route_items_by_id.values() if item.route.kind is DiagramRouteKind.EQUIPMENT_BRANCH)
        if values or routes:
            result = self._call("reset_label_positions", values, route_ids=routes)
            if result is not None:
                self.refresh()
                self.commandCompleted.emit(result)

    def set_developer_overlay(self, enabled: bool) -> None:
        setter = getattr(self.controller, "set_developer_diagnostics", None)
        if callable(setter):
            self._call("set_developer_diagnostics", enabled)
        self.scene.set_developer_overlay(enabled)

    def copy(self, ids: object | None = None) -> None:
        values = tuple(ids or self.scene.selected_representation_ids())
        if values:
            self._call("copy", values)

    def paste(self) -> None:
        try:
            offset_x, offset_y = self.controller.suggested_copy_offset()
        except Exception as exc:
            self.errorOccurred.emit(str(exc))
            return
        result = self._call(
            "paste",
            page_id=self.page_id,
            offset_x=offset_x,
            offset_y=offset_y,
            validate_collision=True,
        )
        self._after_structure_command(result)

    def duplicate(self, ids: object | None = None) -> None:
        values = tuple(ids or self.scene.selected_representation_ids())
        if values:
            try:
                offset_x, offset_y = self.controller.suggested_copy_offset(values)
            except Exception as exc:
                self.errorOccurred.emit(str(exc))
                return
            result = self._call(
                "duplicate",
                values,
                offset_x=offset_x,
                offset_y=offset_y,
                validate_collision=True,
            )
            self._after_structure_command(result)

    def delete_from_project(
        self,
        ids: object | None = None,
        *,
        confirmed: bool = False,
    ) -> None:
        values = tuple(ids or self.scene.selected_representation_ids())
        if values:
            if self.confirm_deletions and not confirmed:
                self.deleteConfirmationRequested.emit(values)
                return
            result = self._call("delete_from_project", values)
            self._after_structure_command(result, keep_selection=False)

    def remove_from_page(self, ids: object | None = None) -> None:
        values = tuple(ids or self.scene.selected_representation_ids())
        if values:
            result = self._call("remove_from_page", values, mark_as_unplaced=True)
            self._after_structure_command(result, keep_selection=False)

    def delete_route(self, route_id: object, *, confirmed: bool = False) -> None:
        if self.confirm_deletions and not confirmed:
            self.routeDeleteConfirmationRequested.emit(route_id)
            return
        result = self._call("delete_diagram_route", route_id)
        self._after_structure_command(result, keep_selection=False)

    def undo(self) -> None:
        if self.scene._mode is CanvasMode.ANALYSIS:
            self.statusMessage.emit(ui_text("status.analysis_locked"))
            return
        result = self._call("undo")
        self._after_structure_command(result, keep_selection=False)

    def redo(self) -> None:
        if self.scene._mode is CanvasMode.ANALYSIS:
            self.statusMessage.emit(ui_text("status.analysis_locked"))
            return
        result = self._call("redo")
        self._after_structure_command(result, keep_selection=False)

    def _move(self, ids: object, dx: float, dy: float, bypass_snap: bool) -> None:
        result = self._call(
            "move_representations", ids, dx, dy, bypass_snap=bypass_snap
        )
        if result is None:
            self.refresh()
            return
        self.refresh()
        self.commandCompleted.emit(result)

    def _rotate_selected(self, ids: object, delta_deg: int) -> None:
        del delta_deg  # With two positions clockwise/counterclockwise coincide.
        values = tuple(ids or self.scene.selected_representation_ids())
        if len(values) == 1:
            representation = self.controller.diagram.representations.get(values[0])
            if representation is not None:
                self._set_selected_orientation(values, next_editor_rotation(representation.rotation_deg))
        elif values:
            self.statusMessage.emit("Поверните объекты по одному")

    def _set_selected_orientation(self, ids: object, angle: int) -> None:
        if self.scene._mode is not CanvasMode.EDIT:
            self.statusMessage.emit(ui_text("status.analysis_locked"))
            return
        if angle not in (90, 180) or isinstance(angle, bool):
            self.statusMessage.emit(ui_text("status.orientation_two_positions"))
            return
        values = tuple(ids or self.scene.selected_representation_ids())
        if not values:
            return
        if len(values) > 1:
            self.statusMessage.emit(
                "Групповой поворот не входит в этот этап; поверните объекты по одному"
            )
            return
        representation_id = values[0]
        representation = self.controller.diagram.representations.get(
            representation_id
        )
        if representation is None:
            return
        target = int(angle)
        if math.isclose(representation.rotation_deg, target):
            return
        conflict = self.scene.rotation_collision(representation_id, target)
        if conflict is not None:
            conflict.set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE)
            item = self.scene._items_by_id.get(representation_id)
            if item is not None:
                item.set_target_feedback(ConnectionTargetFeedback.INCOMPATIBLE)
            self.statusMessage.emit(ui_text("status.rotation_blocked"))
            return
        result = self._call(
            "rotate_representation", representation_id, target
        )
        if result is None:
            self.refresh()
            return
        self.refresh()
        self.scene.select_representations((representation_id,))
        self.commandCompleted.emit(result)
        self.statusMessage.emit(ui_text("status.rotation_done"))

    def _auto_orient_selected(self, ids: object) -> None:
        if self.scene._mode is not CanvasMode.EDIT:
            self.statusMessage.emit(ui_text("status.analysis_locked"))
            return
        values = tuple(ids or self.scene.selected_representation_ids())
        if len(values) != 1:
            return
        representation_id = values[0]
        representation = self.controller.diagram.representations.get(
            representation_id
        )
        if representation is None:
            return
        suggest = getattr(
            self.controller, "suggested_representation_orientation", None
        )
        if callable(suggest):
            try:
                target = int(suggest(representation_id))
            except Exception as exc:
                self.errorOccurred.emit(str(exc))
                return
        else:
            target = int(representation.rotation_deg)
        target = editor_rotation(target)
        conflict = self.scene.rotation_collision(representation_id, target)
        if conflict is not None:
            self.statusMessage.emit(ui_text("status.rotation_blocked"))
            return
        method = getattr(self.controller, "auto_orient_representation", None)
        if callable(method):
            result = self._call(
                "auto_orient_representation", representation_id, target
            )
        else:
            result = self._call(
                "rotate_representation", representation_id, target
            )
        if result is not None:
            self.refresh()
            self.scene.select_representations((representation_id,))
            self.commandCompleted.emit(result)
            self.statusMessage.emit(
                "Ориентация автоматически согласована с подключением"
            )

    def _move_route_label(self, route_id: object, x: float, y: float) -> None:
        result = self._call("set_route_label", route_id, label_x=x, label_y=y)
        if result is not None:
            self.refresh()
            self.commandCompleted.emit(result)

    def _move_label(self, representation_id: object, x: float, y: float) -> None:
        result = self._call(
            "set_label",
            representation_id,
            label_x=x,
            label_y=y,
        )
        if result is None:
            self.refresh()
            return
        self.refresh()
        self.commandCompleted.emit(result)

    def _complete_equipment_placement(self, result: Any) -> None:
        if result is None:
            self.view.placement_failed("операция не выполнена")
            return
        self._after_structure_command(result)
        self.view.placement_succeeded()
        self.placementFinished.emit(result)

    def _preview_equipment_placement(self, payload, point, *, representation_id=None):
        if not isinstance(payload, Mapping) or payload.get("target_kind", "equipment") != "equipment":
            return None
        from .equipment_placement_preview import equipment_proposal
        from ..editor.equipment_attachment import EquipmentPlacementProposal
        try:
            return equipment_proposal(self, payload, point, representation_id=representation_id)
        except (DomainInvariantError, KeyError, TypeError, ValueError) as exc:
            return EquipmentPlacementProposal(False, str(exc), "place", point.x(), point.y())

    def _preview_bus_exit_direction(self, target, peer):
        from ..domain.diagram import RouteEndpointAnchor
        planner = getattr(self.controller, "_bus_exit_direction", None)
        if not callable(planner) or peer is None:
            return target.direction
        representation_id = GraphicalRepresentationId(target.representation_id)
        anchor = RouteEndpointAnchor(RouteAnchorKind.BUS, representation_id,
                                    ElectricalNodeId(target.target_id), anchor_key=target.anchor_key)
        return planner(self.controller.diagram.representations[representation_id], anchor,
                       RouteVertex(target.x,target.y), RouteVertex(peer.x,peer.y))

    def _preview_existing_equipment(self, representation_id, point):
        representation = self.controller.diagram.representations[representation_id]
        equipment = self.controller.model.equipment[representation.equipment_id]
        graphics = dict(_graphics_extensions(representation))
        graphics["rotation_deg"] = representation.rotation_deg
        payload = {"type_id": equipment.type_id.value, "type_version": equipment.type_version,
                   "name": equipment.name, "symbol_key": representation.symbol_key,
                   "graphics": graphics}
        return self._preview_equipment_placement(payload, point, representation_id=representation_id)

    def _apply_prepared_equipment(self, proposal):
        if not proposal.valid:
            self.statusMessage.emit(proposal.reason)
            return
        result = self._call("apply_equipment_placement", proposal)
        self._complete_equipment_placement(result)

    def _placement_collision_rejected(
        self,
        payload: object,
        point: QPointF,
        rotation_deg: int | float,
    ) -> bool:
        allowed, conflict_name = self.view._check_placement_collision(
            payload,
            point,
            candidate_point=point,
            rotation_deg=rotation_deg,
        )
        if allowed:
            return False
        self.view._placement_valid = False
        self.view._placement_conflict_name = conflict_name
        self.statusMessage.emit(
            ui_text(
                "status.placement_collision",
                name=conflict_name or "другим оборудованием",
            )
        )
        self.view.viewport().update()
        return True

    def _context_recloser_preflight(
        self,
        route: DiagramRoute,
        point: QPointF,
        name: str,
    ) -> tuple[QPointF, int] | None:
        """Проверить точную позицию контекстной вставки до доменной команды."""

        projected = self.scene._project_to_route(route, point)
        effective_point = self.view._snap_scene_point(
            projected[0] if projected is not None else point
        )
        segment = closest_directed_segment(
            ((item.x, item.y) for item in route.waypoints),
            effective_point.x(),
            effective_point.y(),
        )
        rotation_deg = editor_rotation(int(segment.quarter_turn)) if segment is not None else 90
        try:
            definition = self.controller.model.equipment_type(
                EquipmentTypeId("builtin.recloser"),
                1,
            )
            candidate = geometry_for_equipment_preview(
                definition,
                page_id=route.page_id,
                x=effective_point.x(),
                y=effective_point.y(),
                rotation_deg=rotation_deg,
                display_name=name,
            )
        except (KeyError, TypeError, ValueError, DomainInvariantError) as exc:
            self.statusMessage.emit(
                f"Не удалось проверить место вставки реклоузера: {exc}"
            )
            return None
        snapshot = self.view.collision_snapshot()
        if snapshot is None:
            self.statusMessage.emit(
                "Не удалось проверить место вставки реклоузера."
            )
            return None
        check = snapshot.check_placement(candidate)
        if not check.allowed:
            for conflict_id in check.conflicting_ids:
                item = self.scene._items_by_id.get(conflict_id)
                if item is not None:
                    item.set_target_feedback(
                        ConnectionTargetFeedback.INCOMPATIBLE
                    )
            self.statusMessage.emit(check.message)
            self.view.viewport().update()
            return None
        return effective_point, rotation_deg

    def _add_equipment(self, payload: object, x: float, y: float) -> None:
        if self.scene._mode is CanvasMode.ANALYSIS:
            self.statusMessage.emit(ui_text("status.analysis_locked"))
            return
        if self.page_id is None:
            self._call("ensure_default_page")
        raw_point = QPointF(x, y)
        standalone_point = self.view._snap_scene_point(raw_point)
        options = payload if isinstance(payload, Mapping) else {}
        target_kind = str(options.get("target_kind", "equipment"))
        type_id = str(options.get("type_id", payload))
        name = str(options.get("name", ""))
        symbol_key = str(options.get("symbol_key", ""))
        graphics = _mapping(options.get("graphics"))
        width = _number(graphics, "width", 80.0)
        height = _number(graphics, "height", 50.0)
        rotation_deg = editor_rotation(
            float(self.view._placement_rotation_deg)
            if self.view._placement_payload is not None
            else _number(graphics, "rotation_deg", 90.0)
        )
        orientation_mode = str(graphics.get("orientation_mode", "auto"))
        if target_kind == "physical_line":
            line_kind = (
                LineKind.CABLE
                if "cable" in type_id.casefold()
                else LineKind.OVERHEAD
            )
            self.scene.begin_physical_line(
                name=name or (
                    ui_text("equipment.cable_line")
                    if line_kind is LineKind.CABLE
                    else ui_text("equipment.overhead_line")
                ),
                line_kind=line_kind,
                scene_pos=standalone_point,
            )
            return
        if target_kind == "electrical_node":
            result = self._call(
                "add_electrical_node",
                name,
                page_id=self.page_id,
                x=standalone_point.x(),
                y=standalone_point.y(),
                symbol_key=symbol_key or "electrical_node",
                width=width,
                height=height,
                rotation_deg=rotation_deg,
            )
            self._complete_equipment_placement(result)
            return
        try:
            typed_id = EquipmentTypeId(type_id)
            type_version = int(options.get("type_version", 1))
            definition = self.controller.model.equipment_type(
                typed_id, type_version
            )
            name = name or definition.display_name
        except Exception:
            typed_id = type_id
            type_version = 1
            definition = None
            name = name or "Оборудование"

        physical_hit = self.scene.physical_route_at(raw_point)
        if physical_hit is not None and definition is not None:
            route, route_fraction = physical_hit
            projected = self.scene._project_to_route(route, raw_point)
            line_point = self.view._snap_scene_point(
                projected[0] if projected is not None else raw_point
            )
            x, y = line_point.x(), line_point.y()
            assert route.equipment_id is not None
            placement = EquipmentPlacementRegistry.for_model(
                self.controller.model.equipment_types.values()
            ).resolve_definition(definition)
            manual_placement_confirmed = False
            if placement is EquipmentPlacementKind.MANUAL_SELECTION:
                selected, accepted = QInputDialog.getItem(
                    self,
                    "Способ подключения к линии",
                    "Выберите электрическое действие:",
                    (
                        "Вставить последовательно",
                        "Подключить ответвлением",
                        "Отмена",
                    ),
                    0,
                    False,
                )
                if not accepted or selected == "Отмена":
                    return
                placement = (
                    EquipmentPlacementKind.INLINE_SERIES
                    if selected == "Вставить последовательно"
                    else EquipmentPlacementKind.BRANCH_ATTACHMENT
                )
                manual_placement_confirmed = True

            if placement is EquipmentPlacementKind.INLINE_SERIES:
                if normalize_orientation_mode(orientation_mode) is OrientationMode.AUTO:
                    segment = closest_directed_segment(
                        ((point.x, point.y) for point in route.waypoints), x, y
                    )
                    if segment is not None:
                        rotation_deg = editor_rotation(segment.quarter_turn)
                if self._placement_collision_rejected(
                    options,
                    line_point,
                    rotation_deg,
                ):
                    return
                accepted, offset_mm = self._ask_physical_split_offset(
                    route.equipment_id,
                    route_fraction,
                    title="Вставить аппарат в линию",
                )
                if not accepted:
                    return
                if definition.id.value == "builtin.recloser":
                    method = getattr(self.controller, "insert_recloser", None)
                    orientation_kwargs = self._supported_controller_kwargs(
                        method,
                        rotation_deg=rotation_deg,
                        orientation_mode=orientation_mode,
                    )
                    result = self._call(
                        "insert_recloser",
                        route.equipment_id,
                        offset_mm,
                        name,
                        page_id=self.page_id,
                        x=x,
                        y=y,
                        **orientation_kwargs,
                    )
                else:
                    method = getattr(
                        self.controller, "insert_series_equipment", None
                    )
                    kwargs = self._supported_controller_kwargs(
                        method,
                        type_version=definition.schema_version,
                        manual_placement_confirmed=manual_placement_confirmed,
                        rotation_deg=rotation_deg,
                        orientation_mode=orientation_mode,
                    )
                    result = self._call(
                        "insert_series_equipment",
                        route.equipment_id,
                        offset_mm,
                        definition.id,
                        name,
                        page_id=self.page_id,
                        x=x,
                        y=y,
                        **kwargs,
                    )
                self._complete_equipment_placement(result)
                return

            if placement is EquipmentPlacementKind.BRANCH_ATTACHMENT:
                port_definitions = tuple(definition.port_definitions)
                if not port_definitions:
                    self.errorOccurred.emit(
                        "У оборудования нет электрических терминалов для отпайки."
                    )
                    return
                if len(port_definitions) == 1:
                    terminal_role = port_definitions[0].role
                else:
                    display_name_counts = {
                        item.display_name: sum(
                            other.display_name == item.display_name
                            for other in port_definitions
                        )
                        for item in port_definitions
                    }
                    labels = tuple(
                        (
                            f"{item.display_name} — терминал № {index + 1}"
                            if display_name_counts[item.display_name] > 1
                            else item.display_name
                        )
                        for index, item in enumerate(port_definitions)
                    )
                    selected, accepted = QInputDialog.getItem(
                        self,
                        "Подключить оборудование ответвлением",
                        "Выберите терминал оборудования:",
                        labels,
                        0,
                        False,
                    )
                    if not accepted:
                        return
                    terminal_role = port_definitions[labels.index(selected)].role
                tap_point = QPointF(x, y)
                equipment_point = (
                    self.view._branch_attachment_equipment_point(tap_point)
                )
                try:
                    effective_mode = normalize_orientation_mode(
                        orientation_mode
                    )
                except ValueError:
                    effective_mode = OrientationMode.AUTO
                if effective_mode is OrientationMode.AUTO:
                    automatic_rotation = self.view._branch_auto_rotation(
                        options,
                        equipment_point,
                        tap_point,
                        terminal_role=terminal_role,
                    )
                    if automatic_rotation is not None:
                        rotation_deg = float(automatic_rotation)
                if self._placement_collision_rejected(
                    options,
                    equipment_point,
                    rotation_deg,
                ):
                    return
                accepted, offset_mm = self._ask_physical_split_offset(
                    route.equipment_id,
                    route_fraction,
                    title="Создать отпайку к оборудованию",
                )
                if not accepted:
                    return
                method = getattr(
                    self.controller, "attach_equipment_to_line", None
                )
                kwargs = self._supported_controller_kwargs(
                    method,
                    manual_placement_confirmed=manual_placement_confirmed,
                    rotation_deg=rotation_deg,
                    orientation_mode=orientation_mode,
                    equipment_x=equipment_point.x(),
                    equipment_y=equipment_point.y(),
                )
                result = self._call(
                    "attach_equipment_to_line",
                    route.equipment_id,
                    offset_mm,
                    definition.id,
                    name,
                    terminal_role=terminal_role,
                    type_version=definition.schema_version,
                    page_id=self.page_id,
                    tap_x=x,
                    tap_y=y,
                    **kwargs,
                )
                self._complete_equipment_placement(result)
                return
        if self._placement_collision_rejected(
            options,
            standalone_point,
            rotation_deg,
        ):
            return
        result = self._call(
            "add_equipment",
            typed_id,
            name,
            page_id=self.page_id,
            x=standalone_point.x(),
            y=standalone_point.y(),
            symbol_key=symbol_key or None,
            width=width,
            height=height,
            rotation_deg=rotation_deg,
            orientation_mode=orientation_mode,
        )
        self._complete_equipment_placement(result)

    @staticmethod
    def _persisted_waypoints(
        draft: ConnectionDraft | PhysicalLineDraft,
        *,
        reverse: bool = False,
    ) -> tuple[RouteWaypoint, ...]:
        vertices = tuple(reversed(draft.vertices)) if reverse else draft.vertices
        return tuple(
            RouteWaypoint(
                RouteWaypointId.new(),
                item.x,
                item.y,
                RouteWaypointSource.USER
                if item.source is RouteVertexSource.USER
                else RouteWaypointSource.AUTOMATIC,
                False,  # Clicked draft goals shape this gesture; pinning is explicit.
            )
            for item in vertices
        )

    @classmethod
    def _route_geometry_kwargs(
        cls,
        method: object,
        draft: ConnectionDraft | PhysicalLineDraft,
        *,
        reverse: bool = False,
    ) -> dict[str, object]:
        """Передать геометрию только если controller объявил такой контракт."""

        if not callable(method):
            return {}
        parameters = inspect.signature(method).parameters
        waypoints = cls._persisted_waypoints(draft, reverse=reverse)
        if "route_waypoints" in parameters:
            return {"route_waypoints": waypoints}
        if "waypoints" in parameters:
            return {"waypoints": waypoints}
        return {}

    @staticmethod
    def _supported_controller_kwargs(
        method: object,
        **candidates: object,
    ) -> dict[str, object]:
        if not callable(method):
            return {}
        parameters = inspect.signature(method).parameters
        return {
            key: value
            for key, value in candidates.items()
            if key in parameters and value is not None
        }

    @staticmethod
    def _controller_connection_target(target: ConnectionTarget) -> object:
        from ..editor.controller import NewNodeTarget, NodeTarget, PortTarget

        representation_id = (
            GraphicalRepresentationId(target.representation_id)
            if target.representation_id
            else None
        )
        if target.kind is ConnectionTargetKind.EQUIPMENT_PORT:
            return PortTarget(
                PortId(target.target_id),
                representation_id,
                target.anchor_key,
            )
        if target.kind in {
            ConnectionTargetKind.ELECTRICAL_NODE,
            ConnectionTargetKind.BUS,
            ConnectionTargetKind.NODE_CONNECTION,
        }:
            return NodeTarget(
                ElectricalNodeId(target.target_id),
                representation_id,
                target.anchor_key,
                target.x if representation_id is None else None,
                target.y if representation_id is None else None,
                DiagramRouteId(target.route_id) if target.route_id else None,
            )
        if target.kind is ConnectionTargetKind.FREE:
            return NewNodeTarget(
                "Новый электрический узел",
                target.x,
                target.y,
                anchor_key=target.anchor_key,
            )
        raise ValueError("Неизвестная цель электрического соединения.")

    #  Что предлагается на выбор при отпускании протяжки. Порядок и подписи
    #  заданы здесь один раз: меню, подсказки и тесты берут их отсюда.
    DRAGGED_CONNECTION_CHOICES = (
        ("wire", "Провод (соединение без сопротивления)"),
        ("overhead", "Воздушная линия (ВЛ)"),
        ("cable", "Кабельная линия (КЛ)"),
    )

    def _ask_dragged_connection_kind(self, screen_pos: QPoint) -> str | None:
        """Чем стала протяжка: проводом ошиновки или физической линией.

        Провод и линия — РАЗНЫЕ электрические объекты, а не два вида одной
        картинки: провод не имеет сопротивления, линия имеет и меняет токи КЗ.
        Поэтому выбор делает человек в момент отпускания, а не программа по
        длине или направлению нарисованного отрезка.

        Возврат ``None`` — отказ (Esc или щелчок мимо); проект не меняется.
        """
        menu = QMenu(self)
        menu.setTitle("Чем соединить")
        actions = {}
        for key, title in self.DRAGGED_CONNECTION_CHOICES:
            actions[menu.addAction(title)] = key
        chosen = menu.exec(screen_pos)
        return actions.get(chosen)

    def _physical_line_draft_from_connection(
        self,
        draft: ConnectionDraft,
        line_kind: LineKind,
    ) -> PhysicalLineDraft | None:
        """Перевести жест протяжки в черновик физической линии.

        Начало жеста — вывод оборудования, поэтому источник описывается как
        цель типа EQUIPMENT_PORT с теми же постоянными ID и той же долей на
        шине. Точки трассы переносятся как есть: пользователь их уже нарисовал,
        и перекладывать их автоматическим маршрутизатором значит потерять то,
        что он показал.
        """
        vertices = tuple(draft.vertices)
        if not vertices:
            self.errorOccurred.emit("Протяжка не содержит ни одной точки.")
            return None
        source = draft.source_target or ConnectionTarget(
            ConnectionTargetKind.EQUIPMENT_PORT,
            vertices[0].x,
            vertices[0].y,
            target_id=draft.source_port_id,
            representation_id=draft.source_representation_id,
            feedback=ConnectionTargetFeedback.COMPATIBLE,
            anchor_key=draft.source_anchor_key,
        )
        return PhysicalLineDraft(
            name=(
                ui_text("equipment.cable_line")
                if line_kind is LineKind.CABLE
                else ui_text("equipment.overhead_line")
            ),
            line_kind=str(line_kind.value),
            source=source,
            target=draft.target,
            vertices=vertices,
            manual_vertices=tuple(draft.manual_vertices),
        )

    def _commit_dragged_connection(self, value: object, screen_pos: object) -> None:
        if not isinstance(value, ConnectionDraft) or not isinstance(screen_pos, QPoint):
            self.errorOccurred.emit("Получен повреждённый черновик соединения.")
            return
        # Решение заказчика 31.08.2026: протяжка от вывода предлагает выбор
        # «Провод / ВЛ / КЛ». Прежнее поведение — «жест всегда обычный провод,
        # линия только из палитры» — заменено, потому что ошиновка и отходящая
        # линия рисуются одним и тем же движением, и заставлять вставлять
        # отдельный объект ради второго случая было лишней работой.
        # Переподключение существующего конца типа не спрашивает: там линия уже
        # есть, и менять её электрическую природу молча нельзя.
        from ..editor.connection_tool import ConnectionToolMode

        #  Уточнение 02.09.2026: молча переподключается только перетаскивание
        #  маркера конца уже существующей ВЛ/КЛ. Протяжка от вывода аппарата
        #  спрашивает тип всегда, даже если вывод уже подключён: на реальной
        #  схеме свободных выводов почти нет, и прежнее правило делало выбор
        #  недоступным именно там, где он нужен.
        if getattr(value, "from_route_endpoint", False):
            self._commit_connection_draft(value)
            return
        choice = self._ask_dragged_connection_kind(screen_pos)
        if choice is None:
            self.statusMessage.emit("Соединение отменено: тип не выбран")
            return
        if choice == "wire":
            self._commit_connection_draft(value)
            return
        line_draft = self._physical_line_draft_from_connection(
            value,
            LineKind.CABLE if choice == "cable" else LineKind.OVERHEAD,
        )
        if line_draft is not None:
            self._commit_physical_line_draft(line_draft)

    def _commit_connection_draft(self, draft: object) -> None:
        if not isinstance(draft, ConnectionDraft):
            self.errorOccurred.emit("Получен повреждённый черновик соединения.")
            return
        from ..editor.connection_tool import ConnectionToolMode
        from ..editor.controller import NewNodeTarget, NodeTarget, PortTarget

        if draft.target.kind is ConnectionTargetKind.PHYSICAL_LINE:
            self._commit_tap_from_connection(draft)
            return
        if draft.source_target is not None:
            source = self._controller_connection_target(draft.source_target)
            target = self._controller_connection_target(draft.target)
            target_node = (self.controller.model.node_for_port(target.port_id)
                           if isinstance(target, PortTarget) else None)
            node_id = target_node.id if target_node is not None else getattr(target, "node_id", None)
            if source.node_id == node_id:
                self.statusMessage.emit("Уже соединено одним электрическим узлом; провод не добавлен")
                return
            result = self._call("connect_from_node", source, target, page_id=self.page_id,
                                route_waypoints=self._persisted_waypoints(draft))
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit(ui_text("status.connection_completed"))
            return
        source_port_id = PortId(draft.source_port_id)
        target = draft.target
        page_id = self.page_id
        # Разрешение same-node в preview нужно только для меню ВЛ/КЛ.
        # Провод здесь уже существует; не пересоздаём его и не добавляем
        # графическую трассу или пустую команду истории.
        source_connection = self.controller.model.connection_for_port(source_port_id)
        target_node = None
        if target.kind is ConnectionTargetKind.EQUIPMENT_PORT:
            target_connection = self.controller.model.connection_for_port(PortId(target.target_id))
            if target_connection is not None:
                target_node = target_connection.electrical_node_id
        elif target.kind in {
            ConnectionTargetKind.ELECTRICAL_NODE, ConnectionTargetKind.BUS,
            ConnectionTargetKind.NODE_CONNECTION,
        }:
            target_node = ElectricalNodeId(target.target_id)
        if source_connection is not None and source_connection.electrical_node_id == target_node:
            self.statusMessage.emit("Уже соединено одним электрическим узлом; провод не добавлен")
            return

        if (target.kind is ConnectionTargetKind.NODE_CONNECTION
                and draft.mode is ConnectionToolMode.CREATE):
            result = self._call(
                "connect_from_node", self._controller_connection_target(target),
                PortTarget(source_port_id, GraphicalRepresentationId(draft.source_representation_id), draft.source_anchor_key),
                page_id=page_id, route_waypoints=tuple(reversed(self._persisted_waypoints(draft))),
            )
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit(ui_text("status.connection_completed"))
            return

        if target.kind is ConnectionTargetKind.EQUIPMENT_PORT:
            controller_target: object = self._controller_connection_target(target)
        elif target.kind in {
            ConnectionTargetKind.ELECTRICAL_NODE,
            ConnectionTargetKind.BUS,
            ConnectionTargetKind.NODE_CONNECTION,
        }:
            controller_target = self._controller_connection_target(target)
        elif target.kind is ConnectionTargetKind.FREE:
            voltage_id = endpoint_voltage(self.controller.model, source_port_id).voltage_class_id
            controller_target = NewNodeTarget(
                "Новый электрический узел",
                target.x,
                target.y,
                voltage_class_id=voltage_id,
            )
        else:
            self.errorOccurred.emit("Неизвестная цель электрического соединения.")
            return

        if draft.mode is ConnectionToolMode.RECONNECT:
            method_name = "reconnect_port"
            method = getattr(self.controller, method_name, None)
            source_port = self.controller.model.ports.get(source_port_id)
            source_is_physical_branch = (
                draft.from_route_endpoint or (source_port is not None
                and source_port.equipment_id
                in self.controller.model.line_sections)
            )
            kwargs = (
                {}
                if source_is_physical_branch
                else self._route_geometry_kwargs(method, draft)
            )
            kwargs.update(self._supported_controller_kwargs(
                method,
                source_representation_id=GraphicalRepresentationId(
                    draft.source_representation_id
                ),
                source_anchor_key=draft.source_anchor_key,
                target_anchor_key=target.anchor_key,
            ))
            result = self._call(
                method_name,
                source_port_id,
                controller_target,
                page_id=page_id,
                **kwargs,
            )
        elif target.kind is ConnectionTargetKind.EQUIPMENT_PORT:
            method_name = "connect_ports"
            method = getattr(self.controller, method_name, None)
            kwargs = self._route_geometry_kwargs(method, draft)
            kwargs.update(self._supported_controller_kwargs(
                method,
                first_representation_id=GraphicalRepresentationId(
                    draft.source_representation_id
                ),
                second_representation_id=(
                    GraphicalRepresentationId(target.representation_id)
                    if target.representation_id
                    else None
                ),
                first_anchor_key=draft.source_anchor_key,
                second_anchor_key=target.anchor_key,
            ))
            result = self._call(
                method_name,
                source_port_id,
                PortId(target.target_id),
                page_id=page_id,
                **kwargs,
            )
        elif target.kind is ConnectionTargetKind.FREE:
            method_name = "finish_port_on_new_node"
            method = getattr(self.controller, method_name, None)
            kwargs = self._route_geometry_kwargs(method, draft)
            kwargs.update(self._supported_controller_kwargs(
                method,
                source_representation_id=GraphicalRepresentationId(
                    draft.source_representation_id
                ),
                source_anchor_key=draft.source_anchor_key,
            ))
            result = self._call(
                method_name,
                source_port_id,
                controller_target,
                page_id=page_id,
                **kwargs,
            )
        else:
            method_name = "connect_port_to_node"
            method = getattr(self.controller, method_name, None)
            kwargs = self._route_geometry_kwargs(method, draft)
            kwargs.update(self._supported_controller_kwargs(
                method,
                source_representation_id=GraphicalRepresentationId(
                    draft.source_representation_id
                ),
                node_representation_id=(
                    GraphicalRepresentationId(target.representation_id)
                    if target.representation_id
                    else None
                ),
                source_anchor_key=draft.source_anchor_key,
                target_anchor_key=target.anchor_key,
            ))
            result = self._call(
                method_name,
                source_port_id,
                ElectricalNodeId(target.target_id),
                page_id=page_id,
                **kwargs,
            )

        if result is not None:
            self._after_structure_command(result)
            self.statusMessage.emit(ui_text("status.connection_completed"))

    def _ask_new_physical_line_parameters(
        self,
        draft: PhysicalLineDraft,
    ) -> object:
        from .line_parameters import ask_line_parameters
        return ask_line_parameters(self, draft, self.controller._project)

    def _line_parameter_values(self, draft):
        answer = self._ask_new_physical_line_parameters(draft)
        # Existing embedded callers can still supply the private tuple seam.
        if isinstance(answer, tuple):
            accepted, name, physical = answer
            return accepted, name, physical, {}
        return answer.accepted, answer.name, answer.physical, {
            "catalog_entry": answer.catalog_entry,
            "remember_catalog_entry": answer.remember_catalog_entry,
        }

    def _commit_physical_line_draft(self, value: object) -> None:
        if not isinstance(value, PhysicalLineDraft):
            self.errorOccurred.emit("Получен повреждённый черновик физической линии.")
            return
        pending_tap = self._pending_tap_branch
        # Завершение через Enter/двойной щелчок тоже должно выключить режим
        # библиотеки, а не оставлять курсор в полусостоянии размещения.
        self.view.cancel_placement(announce=False)
        if value.source.kind is ConnectionTargetKind.PHYSICAL_LINE:
            if (
                pending_tap is None
                or value.source.target_id != pending_tap.section_id.value
            ):
                self.errorOccurred.emit(
                    "Параметры создаваемой отпайки потеряны; проект не изменён."
                )
                return
            try:
                end_target = self._controller_connection_target(value.target)
                line_kind = LineKind(value.line_kind)
            except (TypeError, ValueError) as exc:
                self.errorOccurred.emit(str(exc))
                return
            method = getattr(self.controller, "create_tap", None)
            kwargs = self._route_geometry_kwargs(method, value)  # type: ignore[arg-type]
            result = self._call(
                "create_tap",
                pending_tap.section_id,
                pending_tap.offset_mm,
                value.name,
                line_kind,
                end_target,
                physical=pending_tap.physical,
                page_id=self.page_id,
                tap_x=pending_tap.tap_x,
                tap_y=pending_tap.tap_y,
                **kwargs,
            )
            self._pending_tap_branch = None
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit("Отпайка создана одной операцией")
            return
        if value.target.kind is ConnectionTargetKind.PHYSICAL_LINE:
            accepted, name, physical, catalog_kwargs = self._line_parameter_values(value)
            if not accepted:
                return
            section_id = EquipmentId(value.target.target_id)
            accepted, offset_mm = self._ask_physical_split_offset(
                section_id,
                value.target.route_fraction,
                title=ui_text("action.add_tap"),
            )
            if not accepted:
                return
            try:
                # create_tap строит ветвь от нового узла отпайки к branch_target,
                # поэтому начало нарисованной пользователем линии передаётся
                # как цель ветви, а её точки сохраняются в обратном порядке.
                branch_target = self._controller_connection_target(value.source)
                line_kind = LineKind(value.line_kind)
            except (TypeError, ValueError) as exc:
                self.errorOccurred.emit(str(exc))
                return
            method = getattr(self.controller, "create_tap", None)
            kwargs = self._route_geometry_kwargs(method, value, reverse=True)
            kwargs.update(catalog_kwargs)
            result = self._call(
                "create_tap",
                section_id,
                offset_mm,
                name,
                line_kind,
                branch_target,
                physical=physical,
                page_id=self.page_id,
                tap_x=value.target.x,
                tap_y=value.target.y,
                tap_route_id=DiagramRouteId(value.target.route_id) if value.target.route_id else None,
                **kwargs,
            )
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit("Отпайка создана одной операцией")
            return
        accepted, name, physical, catalog_kwargs = self._line_parameter_values(value)
        if not accepted:
            return
        try:
            start_target = self._controller_connection_target(value.source)
            end_target = self._controller_connection_target(value.target)
            line_kind = LineKind(value.line_kind)
        except (TypeError, ValueError) as exc:
            self.errorOccurred.emit(str(exc))
            return
        method = getattr(self.controller, "create_physical_line", None)
        kwargs = self._route_geometry_kwargs(method, value)  # type: ignore[arg-type]
        kwargs.update(catalog_kwargs)
        #  Оба конца жеста могут оказаться на одном узле: так выглядит вставка
        #  линии в уже существующую связь. Разрешаем это только для протяжки от
        #  вывода аппарата — там понятно, какой конец переезжает на новый узел.
        try:
            import inspect

            if method is not None and "split_shared_node" in inspect.signature(method).parameters:
                kwargs["split_shared_node"] = (
                    value.source.kind is ConnectionTargetKind.EQUIPMENT_PORT
                    or value.target.kind is ConnectionTargetKind.EQUIPMENT_PORT
                )
        except (TypeError, ValueError):
            pass
        result = self._call(
            "create_physical_line",
            name,
            line_kind,
            start_target,
            end_target,
            physical=physical,
            page_id=self.page_id,
            **kwargs,
        )
        if result is not None:
            self._after_structure_command(result)
            self.propertiesRequested.emit(result.route_id)
            self.statusMessage.emit(ui_text("status.physical_line_completed"))

    def _ask_physical_split_offset(
        self,
        section_id: EquipmentId,
        graphical_fraction: float | None,
        *,
        title: str,
    ) -> tuple[bool, int | None]:
        # Графическая доля нужна только preview-маркеру. Физическое положение
        # всегда вводится отдельно и никогда не выводится из пикселей холста.
        del graphical_fraction
        section = self.controller.model.line_sections.get(section_id)
        notice = ""
        if section is not None:
            length_mm = section.length_mm
        else:
            from ..editor.legacy_line_split import legacy_line_split_info
            try:
                info = legacy_line_split_info(self.controller.model, section_id)
            except (ValueError, DomainInvariantError) as exc:
                self.errorOccurred.emit(str(exc))
                return False, None
            length_mm = info.length_mm
            if info.protected:
                notice = ("У исходной линии есть защита. После отпайки расчёт будет заблокирован "
                          "до подтверждения её зоны защиты.\n\n")
        if length_mm is None:
            answer = QMessageBox.question(
                self,
                title,
                "Физическая длина линии не подтверждена. Создать топологию с неподтверждёнными длинами частей?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes, None
        neutral_mm = max(1, min(length_mm - 1, length_mm // 2))
        value_m, accepted = QInputDialog.getDouble(
            self,
            title,
            notice + "Введите физическое расстояние от начала линии, м "
            "(положение курсора не используется):",
            neutral_mm / 1_000.0,
            0.001,
            (length_mm - 1) / 1_000.0,
            3,
        )
        return bool(accepted), round(value_m * 1_000.0) if accepted else None

    def _ask_branch_parameters(self) -> tuple[bool, str, LineKind, object]:
        from ..editor.controller import PhysicalLineInput

        name, accepted = QInputDialog.getText(
            self,
            ui_text("action.add_tap"),
            "Наименование отходящей линии:",
            text="Отпайка",
        )
        if not accepted:
            return False, "", LineKind.OVERHEAD, PhysicalLineInput(None)
        kind_text, accepted = QInputDialog.getItem(
            self,
            ui_text("action.add_tap"),
            "Тип отходящей линии:",
            ("Воздушная линия", "Кабельная линия"),
            0,
            False,
        )
        if not accepted:
            return False, "", LineKind.OVERHEAD, PhysicalLineInput(None)
        length_m, accepted = QInputDialog.getDouble(
            self,
            ui_text("action.add_tap"),
            "Физическая длина отходящей линии, м (0 — неизвестна):",
            0.0,
            0.0,
            1_000_000.0,
            3,
        )
        if not accepted:
            return False, "", LineKind.OVERHEAD, PhysicalLineInput(None)
        line_kind = (
            LineKind.CABLE if kind_text == "Кабельная линия" else LineKind.OVERHEAD
        )
        length_mm = round(length_m * 1_000.0) if length_m > 0.0 else None
        physical = PhysicalLineInput(
            length_mm,
            DataConfirmation.CONFIRMED
            if length_mm is not None
            else DataConfirmation.UNCONFIRMED,
        )
        return True, name.strip() or "Отпайка", line_kind, physical

    def _commit_tap_from_connection(self, draft: ConnectionDraft) -> None:
        section_id = EquipmentId(draft.target.target_id)
        accepted, offset_mm = self._ask_physical_split_offset(
            section_id,
            draft.target.route_fraction,
            title=ui_text("action.add_tap"),
        )
        if not accepted:
            return
        if draft.source_target is not None:
            result = self._call("connect_node_to_tap",
                self._controller_connection_target(draft.source_target), section_id, offset_mm,
                page_id=self.page_id, route_waypoints=self._persisted_waypoints(draft),
                tap_x=draft.target.x, tap_y=draft.target.y,
                tap_route_id=DiagramRouteId(draft.target.route_id) if draft.target.route_id else None)
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit("Провод подключён к новой точке отпайки одной операцией")
            return
        if draft.source_port_id:
            method = getattr(self.controller, "reconnect_port_to_tap", None)
            source_port = self.controller.model.ports.get(
                PortId(draft.source_port_id)
            )
            source_is_physical_branch = (
                source_port is not None
                and source_port.equipment_id
                in self.controller.model.line_sections
            )
            kwargs = (
                {}
                if source_is_physical_branch
                else self._route_geometry_kwargs(method, draft)
            )
            kwargs.update(self._supported_controller_kwargs(
                method,
                source_representation_id=GraphicalRepresentationId(
                    draft.source_representation_id
                ),
                source_anchor_key=draft.source_anchor_key,
                tap_x=draft.target.x,
                tap_y=draft.target.y,
                tap_route_id=DiagramRouteId(draft.target.route_id) if draft.target.route_id else None,
            ))
            result = self._call(
                "reconnect_port_to_tap",
                PortId(draft.source_port_id),
                section_id,
                offset_mm,
                page_id=self.page_id,
                **kwargs,
            )
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit(
                    "Порт переподключён к новой точке отпайки одной операцией"
                )
            return

    def _route_context_action(self, action: str, payload: object) -> None:
        if not isinstance(payload, Mapping):
            self.errorOccurred.emit("Не удалось определить выбранную графическую трассу.")
            return
        if action in {"pin_route_bends", "unpin_route_bends", "auto_route"}:
            route_id = payload.get("route_id")
            if not isinstance(route_id, DiagramRouteId):
                self.errorOccurred.emit("Не удалось определить графическую трассу.")
                return
            result = (self._call("auto_route_diagram_route", route_id) if action == "auto_route"
                      else self._call("set_route_bends_pinned", route_id, action == "pin_route_bends"))
            if result is not None:
                self._after_structure_command(result)
                self.statusMessage.emit({"pin_route_bends": "Изгибы закреплены: при переносе сохраняются",
                    "unpin_route_bends": "Изгибы освобождены: маршрут может сокращаться автоматически",
                    "auto_route": "Построен короткий маршрут с прежними электрическими концами"}[action])
            return
        if action == "delete_route":
            route_id = payload.get("route_id")
            if not isinstance(route_id, DiagramRouteId):
                self.errorOccurred.emit("Не удалось определить графическую трассу.")
                return
            self.delete_route(route_id)
            return
        equipment_id = payload.get("equipment_id")
        if not isinstance(equipment_id, EquipmentId):
            self.errorOccurred.emit("Не удалось определить выбранную физическую линию.")
            return
        if action == "add_tap":
            accepted, offset_mm = self._ask_physical_split_offset(
                equipment_id,
                payload.get("route_fraction"),
                title=ui_text("action.add_tap"),
            )
            if not accepted:
                return
            accepted, name, line_kind, physical = self._ask_branch_parameters()
            if not accepted:
                return
            tap_x = float(payload.get("scene_x", 0.0))
            tap_y = float(payload.get("scene_y", 0.0))
            source = ConnectionTarget(
                ConnectionTargetKind.PHYSICAL_LINE,
                tap_x,
                tap_y,
                equipment_id.value,
                feedback=ConnectionTargetFeedback.COMPATIBLE,
                message="Отпустите кнопку, чтобы создать отпайку",
                route_id=str(
                    getattr(payload.get("route_id"), "value", "")
                ),
                route_fraction=payload.get("route_fraction"),
            )
            self._pending_tap_branch = PendingTapBranch(
                equipment_id,
                offset_mm,
                physical,
                tap_x,
                tap_y,
            )
            started = self.scene.begin_physical_line_from_target(
                name=name,
                line_kind=line_kind,
                source=source,
            )
            if not started:
                self._pending_tap_branch = None
            return
        if action == "insert_recloser":
            accepted, offset_mm = self._ask_physical_split_offset(
                equipment_id,
                payload.get("route_fraction"),
                title=ui_text("action.insert_recloser"),
            )
            if not accepted:
                return
            name, accepted = QInputDialog.getText(
                self,
                ui_text("action.insert_recloser"),
                "Наименование реклоузера:",
                text="Реклоузер",
            )
            if not accepted:
                return
            route_id = payload.get("route_id")
            if not isinstance(route_id, DiagramRouteId):
                try:
                    route_id = DiagramRouteId(str(route_id))
                except (TypeError, ValueError):
                    self.statusMessage.emit(
                        "Не найдена графическая трасса для вставки реклоузера."
                    )
                    return
            route = self.controller.diagram.routes.get(route_id)
            if route is None:
                self.statusMessage.emit(
                    "Не найдена графическая трасса для вставки реклоузера."
                )
                return
            checked = self._context_recloser_preflight(
                route,
                QPointF(
                    float(payload.get("scene_x", 0.0)),
                    float(payload.get("scene_y", 0.0)),
                ),
                name.strip() or "Реклоузер",
            )
            if checked is None:
                return
            effective_point, rotation_deg = checked
            result = self._call(
                "insert_recloser",
                equipment_id,
                offset_mm,
                name.strip() or "Реклоузер",
                page_id=self.page_id,
                x=effective_point.x(),
                y=effective_point.y(),
                rotation_deg=rotation_deg,
                orientation_mode=OrientationMode.AUTO,
            )
            self._after_structure_command(result)
            return
        if action == "split_line":
            accepted, offset_mm = self._ask_physical_split_offset(
                equipment_id,
                payload.get("route_fraction"),
                title=ui_text("action.split_line"),
            )
            if not accepted:
                return
            result = self._call(
                "split_physical_line",
                equipment_id,
                offset_mm,
                page_id=self.page_id,
                split_x=float(payload.get("scene_x", 0.0)),
                split_y=float(payload.get("scene_y", 0.0)),
            )
            self._after_structure_command(result)
            return
        if action == "confirm_length":
            section = self.controller.model.line_sections.get(equipment_id)
            if section is None:
                self.errorOccurred.emit("Выбранная физическая линия не найдена.")
                return
            default_m = (
                section.length_mm / 1_000.0
                if section.length_mm is not None
                else 0.001
            )
            length_m, accepted = QInputDialog.getDouble(
                self,
                ui_text("action.confirm_length"),
                "Подтверждённая физическая длина линии, м:",
                default_m,
                0.001,
                1_000_000.0,
                3,
            )
            if not accepted:
                return
            result = self._call(
                "confirm_line_length",
                equipment_id,
                round(length_m * 1_000.0),
            )
            self._after_structure_command(result)
            return
        method_by_action = {
            "remove_tap": "remove_tap",
        }
        method = method_by_action.get(action)
        if method is None or not callable(getattr(self.controller, method, None)):
            self.errorOccurred.emit("Эта команда пока недоступна для выбранного объекта.")
            return
        result = self._call(method, equipment_id)
        self._after_structure_command(result)

    def _equipment_context_action(self, action: str, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        equipment_id = payload.get("equipment_id")
        if action == "remove_recloser_from_line":
            # Совместимость сохранённых сигналов/тестов Этапа 4. Новый
            # контекстный путь ниже использует generic-команду и подтверждение.
            if not isinstance(equipment_id, EquipmentId):
                self.errorOccurred.emit("Не удалось определить реклоузер.")
                return
            result = self._call(
                "remove_recloser_from_line",
                equipment_id,
                page_id=self.page_id,
            )
            self._after_structure_command(result, keep_selection=False)
            return
        if action == "remove_series_equipment_from_line":
            if not isinstance(equipment_id, EquipmentId):
                self.errorOccurred.emit("Не удалось определить последовательный аппарат.")
                return
            answer = QMessageBox.question(
                self,
                "Удаление последовательного аппарата",
                "Восстановить непрерывную линию?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            result = self._call(
                "remove_series_equipment_from_line",
                equipment_id,
                page_id=self.page_id,
            )
            self._after_structure_command(result, keep_selection=False)
            return
        if action != "switch":
            return
        position = payload.get("position")
        if not isinstance(equipment_id, EquipmentId) or not isinstance(
            position, SwitchPosition
        ):
            self.errorOccurred.emit("Не удалось определить коммутационный аппарат.")
            return
        method = getattr(self.controller, "switch_equipment", None)
        if callable(method):
            if self.controller.workspace_state.confirm_switching:
                equipment = self.controller.model.equipment.get(equipment_id)
                verb = "Включить" if position is SwitchPosition.CLOSED else "Отключить"
                answer = QMessageBox.question(
                    self,
                    "Подтверждение переключения",
                    f"{verb} {equipment.name if equipment is not None else 'коммутационный аппарат'}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            try:
                result = method(equipment_id, position, confirmed=True)
            except Exception as exc:
                self.errorOccurred.emit(str(exc))
                return
            self.refresh()
            self.commandCompleted.emit(result)
            return
        self.errorOccurred.emit(
            "Не выбран рабочий режим для переключения коммутационного аппарата."
        )

    def _move_route_segment(self, route_id: object, segment_index: int, dx: float, dy: float) -> None:
        result = self._call("move_route_segment", route_id, segment_index, dx=dx, dy=dy)
        if result is not None:
            self._after_structure_command(result)
        else:
            self.refresh()

    def _move_bus_attachment(self, route_id: object, at_start: bool, fraction: float) -> None:
        result = self._call("move_bus_attachment", route_id, at_start=at_start, fraction=fraction)
        if result is not None:
            self._after_structure_command(result)
        else:
            self.refresh()

    def _edit_route_waypoints(self, route_id: object, waypoints: object) -> None:
        method = getattr(self.controller, "reroute_diagram_route", None)
        if not callable(method):
            self.errorOccurred.emit(
                "Изменение сохранённой трассы пока не поддерживается контроллером."
            )
            self.refresh()
            return
        result = self._call(
            "reroute_diagram_route",
            route_id,
            tuple(waypoints or ()),
        )
        if result is not None:
            self._after_structure_command(result)
            self.statusMessage.emit("Графическая трасса изменена без изменения электрической топологии")
        else:
            self.refresh()

    def _after_structure_command(self, result: Any, *, keep_selection: bool = True) -> None:
        if result is None:
            return
        self.refresh(keep_selection=keep_selection)
        representation_ids = self._result_representation_ids(result)
        if representation_ids:
            self.scene.select_representations(representation_ids)
        self.commandCompleted.emit(result)

    @staticmethod
    def _result_representation_ids(result: Any) -> tuple[Any, ...]:
        value = getattr(result, "representation_ids", None)
        if value is not None:
            return tuple(value)
        value = getattr(result, "representation_id", None)
        if value is not None:
            return (value,)
        nested = getattr(result, "result", None)
        if nested is not None and nested is not result:
            return EditorCanvas._result_representation_ids(nested)
        return ()

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            result = getattr(self.controller, method)(*args, **kwargs)
        except Exception as exc:  # Controller гарантирует русский EditorCommandError.
            self.errorOccurred.emit(str(exc))
            return None
        return result

    def _save_viewport(self, state: CanvasViewportState) -> None:
        self._call("set_view", state.zoom, state.center_x, state.center_y)

    def _restore_workspace_settings(self) -> None:
        state = getattr(self.controller, "workspace_state", None)
        if state is None:
            return
        visible = getattr(state, "grid_visible", getattr(state, "grid_enabled", True))
        snap = getattr(state, "snap_enabled", True)
        grid_size = getattr(state, "grid_size", self.scene.grid_size)
        self.scene.set_grid(visible=bool(visible), size=float(grid_size))
        self.scene.set_snap_enabled(bool(snap))
        zoom = getattr(state, "zoom", None)
        x = getattr(state, "view_x", getattr(state, "center_x", None))
        y = getattr(state, "view_y", getattr(state, "center_y", None))
        if zoom is not None and x is not None and y is not None:
            current = self.view.viewport_state()
            desired = CanvasViewportState(float(zoom), float(x), float(y))
            if current != desired:
                self.view.restore_viewport(desired)


__all__ = [
    "CanvasMode",
    "CanvasViewportState",
    "DiagramGraphicsScene",
    "DiagramGraphicsView",
    "DiagramObjectItem",
    "EditorCanvas",
    "EQUIPMENT_MIME_TYPE",
]
