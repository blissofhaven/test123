# -*- coding: utf-8 -*-
"""Контроллер базового редактора, работающий с настоящим ProjectData.

Модуль не зависит от Qt. Холст вызывает команды контроллера, а затем локально
обновляет только затронутые graphics items. Электрические соединения здесь не
рисуются и не создаются: это явная граница этапа 4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from rza_calc.domain.catalog import CatalogEntry
from rza_calc.domain.catalog_snapshot import CatalogBinding, CatalogEntrySnapshot
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramPage,
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RoutePoint,
    RouteWaypoint,
    RouteWaypointId,
    RouteWaypointSource,
)
from rza_calc.domain.electrical import (
    Connection,
    ConnectionId,
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeId,
    FeederId,
    InsertRecloserResult,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    LineSection,
    LogicalLine,
    LogicalLineId,
    OperatingState,
    OperatingStateId,
    PortId,
    PortInstance,
    PortKindId,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)

from .history import (
    EditableProject,
    ProjectChangeSet,
    ProjectCommandHistory,
    ProjectDraft,
    ProjectHistoryEntry,
    ProjectHistoryError,
    ProjectHistoryEvent,
)
from .connected_geometry import shift_route_segment
from .connection_voltage import VoltageCompatibility, check_connection_voltage, endpoint_voltage
from .connection_reflow import reflow_degree_two_connections
from .deletion_cleanup import cleanup_after_deletion
from .legacy_voltage import preserve_disconnected_legacy_voltages, set_legacy_port_voltage
from .legacy_ct import preserve_legacy_ct_bindings
from .bus_connections import (BUS_ATTACHMENT_GAP, available_bus_fraction,
                              endpoint_fraction)
from .orientation import (
    OrientationMode,
    PortAnchorGeometry,
    QuarterTurn,
    bus_anchor_geometry,
    closest_directed_segment,
    direction_toward_point,
    normalize_orientation_mode,
    normalize_quarter_turn,
    port_anchor_by_id,
    quarter_turn_between_directions,
    quarter_turn_for_port_toward_point,
)
from .orthogonal_routing import (
    RouteDirection,
    RouteVertex,
    RouteVertexSource,
    RoutingObstacle,
    RoutingError,
    RoutingRequest,
    build_orthogonal_route,
)
from .placement import EquipmentPlacementKind, EquipmentPlacementRegistry
from .state import (
    EditorMode,
    EditorWorkspaceState,
    GRAPHICS_EXTENSION_KEY,
    RepresentationGraphics,
    UNPLACED_EXTENSION_KEY,
    diagram_with_workspace,
    label_is_manual,
    orientation_mode_for_representation,
    representation_with_graphics,
    representation_with_orientation_mode,
)
from .strings import tr
from .symbols import canonical_key


class EditorCommandError(RuntimeError):
    """Пользовательская ошибка команды редактора с русским сообщением."""


@dataclass(frozen=True, slots=True)
class AddedElement:
    equipment_id: EquipmentId
    port_ids: tuple[PortId, ...]
    representation_id: GraphicalRepresentationId
    page_id: PageId


@dataclass(frozen=True, slots=True)
class AddedNode:
    node_id: ElectricalNodeId
    representation_id: GraphicalRepresentationId
    page_id: PageId


@dataclass(frozen=True, slots=True)
class PortTarget:
    port_id: PortId
    representation_id: GraphicalRepresentationId | None = None
    anchor_key: str = ""


@dataclass(frozen=True, slots=True)
class NodeTarget:
    node_id: ElectricalNodeId
    representation_id: GraphicalRepresentationId | None = None
    anchor_key: str = ""
    x: float | None = None
    y: float | None = None
    route_id: DiagramRouteId | None = None


@dataclass(frozen=True, slots=True)
class NewNodeTarget:
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    symbol_key: str = "electrical_node"
    voltage_class_id: VoltageClassId | None = None
    anchor_key: str = ""


ConnectionTarget = PortTarget | NodeTarget | NewNodeTarget


@dataclass(frozen=True, slots=True)
class _VoltagePreviewMemo:
    model: ElectricalModel
    inputs: ElectricalModelMemento
    endpoints: tuple[tuple[str, Any], tuple[str, Any]]
    result: VoltageCompatibility


@dataclass(frozen=True, slots=True)
class ConnectionCompatibility:
    valid: bool
    severity: str
    message: str
    effective_voltage_id: VoltageClassId | None = None


@dataclass(frozen=True, slots=True)
class ConnectionEditResult:
    node_id: ElectricalNodeId
    connection_ids: tuple[ConnectionId, ...]
    route_id: DiagramRouteId | None = None
    equipment_id: EquipmentId | None = None


@dataclass(frozen=True, slots=True)
class PhysicalLineInput:
    length_mm: int | None
    length_confirmation: DataConfirmation = DataConfirmation.CONFIRMED
    properties: Mapping[str, Any] = field(default_factory=dict)
    impedance_confirmation: DataConfirmation = DataConfirmation.UNCONFIRMED


@dataclass(frozen=True, slots=True)
class PhysicalLineEditResult:
    logical_line_id: LogicalLineId
    section_id: EquipmentId
    start_node_id: ElectricalNodeId
    end_node_id: ElectricalNodeId
    route_id: DiagramRouteId


@dataclass(frozen=True, slots=True)
class SplitPhysicalLineEditResult:
    logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    split_node_id: ElectricalNodeId
    route_ids: tuple[DiagramRouteId, DiagramRouteId]


@dataclass(frozen=True, slots=True)
class ConfirmedLineLengthResult:
    section_id: EquipmentId
    length_mm: int
    confirmation: DataConfirmation


@dataclass(frozen=True, slots=True)
class TapEditResult:
    main_logical_line_id: LogicalLineId | None
    branch_logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    branch_section_id: EquipmentId
    tap_node_id: ElectricalNodeId
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class BranchAttachmentEditResult:
    main_logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    tap_node_id: ElectricalNodeId
    equipment_id: EquipmentId
    connected_port_id: PortId
    representation_id: GraphicalRepresentationId
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesEquipmentEditResult:
    feeder_id: FeederId
    left_logical_line_id: LogicalLineId
    right_logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    left_section_id: EquipmentId
    right_section_id: EquipmentId
    left_node_id: ElectricalNodeId
    right_node_id: ElectricalNodeId
    equipment_id: EquipmentId
    terminal_roles: tuple[str, str]
    representation_id: GraphicalRepresentationId | None = None
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class RecloserEditResult:
    feeder_id: FeederId
    left_logical_line_id: LogicalLineId
    right_logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    left_section_id: EquipmentId
    right_section_id: EquipmentId
    left_node_id: ElectricalNodeId
    right_node_id: ElectricalNodeId
    recloser_id: EquipmentId
    representation_id: GraphicalRepresentationId | None = None
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoveRecloserEditResult:
    feeder_id: FeederId
    logical_line_id: LogicalLineId
    removed_right_logical_line_id: LogicalLineId
    removed_recloser_id: EquipmentId
    merged_section_id: EquipmentId
    route_id: DiagramRouteId


@dataclass(frozen=True, slots=True)
class RemoveSeriesEquipmentEditResult:
    feeder_id: FeederId
    logical_line_id: LogicalLineId
    removed_right_logical_line_id: LogicalLineId
    removed_equipment_id: EquipmentId
    merged_section_id: EquipmentId
    route_id: DiagramRouteId


@dataclass(frozen=True, slots=True)
class RemoveTapEditResult:
    main_logical_line_id: LogicalLineId
    removed_branch_line_ids: tuple[LogicalLineId, ...]
    removed_tap_node_id: ElectricalNodeId | None
    merged_section_id: EquipmentId | None
    route_id: DiagramRouteId | None = None


@dataclass(frozen=True, slots=True)
class LineTapConnectionEditResult:
    main_logical_line_id: LogicalLineId | None
    removed_section_id: EquipmentId
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    tap_node_id: ElectricalNodeId
    connection_id: ConnectionId | None
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class _TapSplit:
    logical_line_id: LogicalLineId | None
    removed_section_id: EquipmentId
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    tap_node_id: ElectricalNodeId


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    node_id: ElectricalNodeId
    representation_id: GraphicalRepresentationId
    anchor_kind: RouteAnchorKind
    target_port_id: PortId | None = None
    anchor_key: str = ""


@dataclass(frozen=True, slots=True)
class PasteResult:
    equipment_ids: tuple[EquipmentId, ...]
    node_ids: tuple[ElectricalNodeId, ...]
    connection_ids: tuple[ConnectionId, ...]
    representation_ids: tuple[GraphicalRepresentationId, ...]
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class DeleteResult:
    equipment_ids: tuple[EquipmentId, ...] = ()
    node_ids: tuple[ElectricalNodeId, ...] = ()
    representation_ids: tuple[GraphicalRepresentationId, ...] = ()
    route_ids: tuple[DiagramRouteId, ...] = ()


@dataclass(frozen=True, slots=True)
class ClipboardLineGroup:
    logical_line: LogicalLine
    sections: tuple[LineSection, ...]


@dataclass(frozen=True, slots=True)
class ProjectClipboard:
    representations: tuple[GraphicalRepresentation, ...]
    equipment: tuple[EquipmentInstance, ...]
    ports: tuple[PortInstance, ...]
    nodes: tuple[ElectricalNode, ...]
    connections: tuple[Connection, ...]
    line_groups: tuple[ClipboardLineGroup, ...]
    catalog_bindings: tuple[CatalogBinding, ...]
    routes: tuple[DiagramRoute, ...] = ()


_TRANSIENT_EXTENSION_KEYS = frozenset({
    "calculation_result",
    "calculation_results",
    "calculated_values",
    "diagnostic",
    "diagnostics",
    "temporary",
    "temporary_diagnostics",
    "runtime_cache",
    "selection",
})

# Явный признак оборудования, созданного редактором до подключения его портов
# на Этапе 4. Он не исключает аппарат из ElectricalModel и не ослабляет
# топологию; признак нужен только persistence-слою, чтобы отличить честный
# черновик от неоднозначного или повреждённого электрического режима.
EDITOR_DRAFT_EXTENSION_KEY = "stage3_editor"


def _clean_extensions(value: Mapping[str, Any]) -> dict[str, Any]:
    """Не переносить результаты расчёта и временное состояние GUI."""
    source = thaw_json(value)

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: clean(child)
                for key, child in item.items()
                if key.casefold() not in _TRANSIENT_EXTENSION_KEYS
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return clean(source)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditorCommandError(f"{label} должно быть числом.")
    result = float(value)
    if not math.isfinite(result):
        raise EditorCommandError(f"{label}: NaN и Infinity недопустимы.")
    return result


def _diagram_with_representations(
    diagram: DiagramDocument,
    values: Mapping[GraphicalRepresentationId, GraphicalRepresentation],
) -> DiagramDocument:
    return replace(
        diagram,
        representations=dict(values),
        revision=diagram.revision + 1,
    )


def _diagram_with_routes(
    diagram: DiagramDocument,
    values: Mapping[DiagramRouteId, DiagramRoute],
) -> DiagramDocument:
    return replace(diagram, routes=dict(values), revision=diagram.revision + 1)


def _diagram_with_pages(
    diagram: DiagramDocument,
    values: Mapping[PageId, DiagramPage],
) -> DiagramDocument:
    return replace(diagram, pages=dict(values), revision=diagram.revision + 1)


def _target_token(representation: GraphicalRepresentation) -> str:
    return representation.target_id.value


def _with_label_graphics(
    row: GraphicalRepresentation | DiagramRoute,
    changes: Mapping[str, Any],
) -> tuple[GraphicalRepresentation | DiagramRoute, dict[str, Any]]:
    """Patch only labels in the canvas's existing effective container.

    A cosmetic edit must never materialize generic symbol dimensions, discard
    unknown extension metadata, or shadow an older graphics container.
    """

    extensions = thaw_json(row.extensions)
    current = extensions.get(GRAPHICS_EXTENSION_KEY, {})
    if not isinstance(current, Mapping):
        raise EditorCommandError("Сохранённое графическое оформление повреждено.")
    fallback = extensions.get("graphics")
    if current:
        key: str | None = GRAPHICS_EXTENSION_KEY
        graphics = dict(current)
    elif isinstance(fallback, Mapping) and fallback:
        key = "graphics"
        graphics = dict(fallback)
    elif any(key in extensions for key in (
        "width", "height", "label_x", "label_y", "label_manual",
        "label_offset_x", "label_offset_y", "label_visible",
        "line_width", "orientation",
    )):
        key = None
        graphics = dict(extensions)
    else:
        key = GRAPHICS_EXTENSION_KEY
        graphics = {}
    graphics.update(changes)
    if key is None:
        extensions.update(changes)
    else:
        extensions[key] = graphics
    return replace(row, extensions=extensions), graphics


class ProjectEditorController:
    """Единая точка мутаций ProjectData для холста и панелей этапа 3."""

    def __init__(self, project: EditableProject):
        self._project = project
        self._history = ProjectCommandHistory(project)
        self._clipboard: ProjectClipboard | None = None

    @property
    def model(self) -> ElectricalModel:
        return self._project.electrical_model

    @property
    def diagram(self) -> DiagramDocument:
        return self._project.diagram

    @property
    def workspace_state(self) -> EditorWorkspaceState:
        return EditorWorkspaceState.from_diagram(self.diagram)

    @property
    def mode(self) -> EditorMode:
        return self.workspace_state.mode

    @property
    def active_operating_state_id(self) -> OperatingStateId | None:
        value = self.workspace_state.active_operating_state_id
        if value is None:
            return None
        selected = OperatingStateId(value)
        return selected if selected in self.model.operating_states else None

    @property
    def can_undo(self) -> bool:
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        return self._history.can_redo

    @property
    def journal(self) -> tuple[ProjectHistoryEntry, ...]:
        return self._history.journal

    @property
    def history(self) -> ProjectCommandHistory:
        """Общая история для привязки состояния кнопок «Отменить/Повторить»."""
        return self._history

    @property
    def clipboard(self) -> ProjectClipboard | None:
        return self._clipboard

    def subscribe(
        self, listener: Callable[[ProjectHistoryEvent], None]
    ) -> Callable[[], None]:
        return self._history.subscribe(listener)

    def _raise_user_error(self, exc: Exception) -> EditorCommandError:
        if isinstance(exc, EditorCommandError):
            return exc
        return EditorCommandError(tr("error.command_failed", details=str(exc)))

    def _execute(
        self,
        description: str,
        command: Callable[[ProjectDraft], Any],
    ) -> Any:
        before_model = self.model

        def preserving_disconnected_voltage(draft: ProjectDraft) -> Any:
            result = command(draft)
            draft.electrical_model = preserve_disconnected_legacy_voltages(
                before_model, draft.electrical_model,
            )
            draft.electrical_model = preserve_legacy_ct_bindings(
                before_model, draft.electrical_model,
            )
            return result

        try:
            return self._history.execute(description, preserving_disconnected_voltage).result
        except (DomainInvariantError, ProjectHistoryError, RoutingError) as exc:
            raise self._raise_user_error(exc) from exc

    @staticmethod
    def _catalog_binding_payload(binding: CatalogBinding) -> tuple[Any, ...]:
        """Сравнить снимки без зависящего от экземпляра equipment_id."""
        return (
            binding.entry,
            binding.instance_overrides,
            binding.calculated_values,
            binding.parameter_overrides,
            binding.extensions,
        )

    @classmethod
    def _split_catalog_binding(
        cls,
        draft: ProjectDraft,
        source_id: EquipmentId,
        result_ids: Iterable[EquipmentId],
    ) -> None:
        """Перенести один неизменяемый snapshot на части физической линии."""
        binding = draft.catalog_snapshots.bindings.get(source_id)
        if binding is None:
            return
        snapshots = draft.catalog_snapshots.without_equipment(source_id)
        for equipment_id in tuple(result_ids):
            snapshots = snapshots.with_binding(
                replace(binding, equipment_id=equipment_id)
            )
        draft.catalog_snapshots = snapshots

    @classmethod
    def _merge_catalog_bindings(
        cls,
        draft: ProjectDraft,
        source_ids: Iterable[EquipmentId],
        result_id: EquipmentId,
    ) -> None:
        """Без потери данных объединить только одинаковые каталожные снимки."""
        ids = tuple(source_ids)
        bindings = tuple(
            draft.catalog_snapshots.bindings.get(equipment_id)
            for equipment_id in ids
        )
        present = tuple(binding for binding in bindings if binding is not None)
        if not present:
            return
        if len(present) != len(bindings):
            raise EditorCommandError(
                "Части линии имеют разные каталожные привязки; "
                "автоматическое объединение небезопасно."
            )
        expected = cls._catalog_binding_payload(present[0])
        if any(cls._catalog_binding_payload(item) != expected for item in present[1:]):
            raise EditorCommandError(
                "Части линии ссылаются на разные каталожные данные; "
                "автоматическое объединение небезопасно."
            )
        snapshots = draft.catalog_snapshots
        for equipment_id in ids:
            snapshots = snapshots.without_equipment(equipment_id)
        draft.catalog_snapshots = snapshots.with_binding(
            replace(present[0], equipment_id=result_id)
        )

    def _require_edit(self, *, geometry: bool = False) -> None:
        if self.mode is EditorMode.ANALYSIS:
            key = "error.analysis_move" if geometry else "error.edit_mode_required"
            raise EditorCommandError(tr(key))

    @staticmethod
    def _representation_id(
        value: GraphicalRepresentationId | str,
    ) -> GraphicalRepresentationId:
        return value if isinstance(value, GraphicalRepresentationId) else GraphicalRepresentationId(value)

    @staticmethod
    def _page_id(value: PageId | str) -> PageId:
        return value if isinstance(value, PageId) else PageId(value)

    @classmethod
    def _representation_ids(
        cls, values: Iterable[GraphicalRepresentationId | str]
    ) -> tuple[GraphicalRepresentationId, ...]:
        result = tuple(dict.fromkeys(cls._representation_id(item) for item in values))
        if not result:
            raise EditorCommandError(tr("error.empty_selection"))
        return result

    @staticmethod
    def _require_representation(
        diagram: DiagramDocument,
        representation_id: GraphicalRepresentationId,
    ) -> GraphicalRepresentation:
        try:
            return diagram.representations[representation_id]
        except KeyError as exc:
            raise EditorCommandError(tr("error.representation_missing")) from exc

    @staticmethod
    def _add_representation(
        diagram: DiagramDocument,
        representation: GraphicalRepresentation,
    ) -> DiagramDocument:
        if representation.id in diagram.representations:
            raise EditorCommandError("Такое графическое представление уже существует.")
        values = dict(diagram.representations)
        values[representation.id] = representation
        return _diagram_with_representations(diagram, values)

    @staticmethod
    def _workspace_with_active_page(
        diagram: DiagramDocument, page_id: PageId
    ) -> DiagramDocument:
        workspace = EditorWorkspaceState.from_diagram(diagram)
        return diagram_with_workspace(
            diagram, replace(workspace, active_page_id=page_id.value)
        )

    @classmethod
    def _resolve_page(
        cls,
        draft: ProjectDraft,
        page_id: PageId | str | None,
    ) -> PageId:
        if page_id is not None:
            selected = cls._page_id(page_id)
            if selected not in draft.diagram.pages:
                raise EditorCommandError(tr("error.page_missing"))
            return selected
        workspace = EditorWorkspaceState.from_diagram(draft.diagram)
        if workspace.active_page_id:
            selected = PageId(workspace.active_page_id)
            if selected in draft.diagram.pages:
                return selected
        if draft.diagram.pages:
            return next(iter(draft.diagram.pages))
        selected = PageId.new()
        pages = dict(draft.diagram.pages)
        pages[selected] = DiagramPage(selected, tr("default.page"))
        draft.diagram = _diagram_with_pages(draft.diagram, pages)
        draft.diagram = cls._workspace_with_active_page(draft.diagram, selected)
        return selected

    @classmethod
    def _ensure_node_representation(
        cls,
        draft: ProjectDraft,
        node_id: ElectricalNodeId,
        page_id: PageId,
        *,
        name: str = "",
        x: float = 0.0,
        y: float = 0.0,
        symbol_key: str = "electrical_node",
    ) -> GraphicalRepresentation:
        existing = next((
            item
            for item in draft.diagram.representations_for_node(node_id)
            if item.page_id == page_id
        ), None)
        if existing is not None:
            return existing
        representation = GraphicalRepresentation(
            GraphicalRepresentationId.new(),
            page_id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_id,
            x=x,
            y=y,
            symbol_key=symbol_key,
            label=name,
        )
        draft.diagram = cls._add_representation(
            draft.diagram, representation
        )
        return representation

    @classmethod
    def _ensure_equipment_representation(
        cls,
        draft: ProjectDraft,
        equipment_id: EquipmentId,
        page_id: PageId,
        *,
        x: float = 0.0,
        y: float = 0.0,
        rotation_deg: int | float | QuarterTurn = QuarterTurn.DEG_0,
        orientation_mode: OrientationMode | str = OrientationMode.MANUAL,
    ) -> GraphicalRepresentation:
        existing = next((
            item
            for item in draft.diagram.representations_for_equipment(equipment_id)
            if item.page_id == page_id
        ), None)
        if existing is not None:
            return existing
        equipment = draft.electrical_model.equipment[equipment_id]
        definition = draft.electrical_model.equipment_type(
            equipment.type_id, equipment.type_version
        )
        try:
            turn = normalize_quarter_turn(rotation_deg)
            mode = normalize_orientation_mode(orientation_mode)
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc
        representation = GraphicalRepresentation(
            GraphicalRepresentationId.new(),
            page_id,
            RepresentationTargetKind.EQUIPMENT,
            equipment_id=equipment_id,
            x=x,
            y=y,
            rotation_deg=float(turn),
            symbol_key=str(definition.extensions.get("diagram_symbol_key", "")),
            label=equipment.name,
        )
        representation = representation_with_orientation_mode(
            representation, mode
        )
        draft.diagram = cls._add_representation(
            draft.diagram, representation
        )
        return representation

    @classmethod
    def _equipment_representation(
        cls,
        draft: ProjectDraft,
        equipment_id: EquipmentId,
        page_id: PageId,
        requested_id: GraphicalRepresentationId | None,
        *,
        x: float = 0.0,
        y: float = 0.0,
    ) -> GraphicalRepresentation:
        if requested_id is None:
            return cls._ensure_equipment_representation(
                draft, equipment_id, page_id, x=x, y=y
            )
        representation = cls._require_representation(draft.diagram, requested_id)
        if (
            representation.page_id != page_id
            or representation.equipment_id != equipment_id
        ):
            raise EditorCommandError(
                "Выбранное графическое представление не соответствует оборудованию."
            )
        return representation

    @classmethod
    def _resolve_conductor_target(
        cls, draft: ProjectDraft, target: NodeTarget, page_id: PageId,
    ) -> _ResolvedTarget:
        """Split graphical ink at a junction; reuse the electrical node unchanged."""
        route = draft.diagram.routes.get(target.route_id)
        if (route is None or route.page_id != page_id
                or route.kind is not DiagramRouteKind.NODE_CONNECTION
                or route.electrical_node_id != target.node_id):
            raise EditorCommandError("Выбранный проводник не представляет этот электрический узел на странице.")
        if (target.x is None or target.y is None
                or not all(math.isfinite(value) for value in (target.x, target.y))):
            raise EditorCommandError("Для присоединения к проводнику нужна точная точка на схеме.")
        x, y = target.x, target.y
        def coincides(point):
            return math.isclose(x, point.x, abs_tol=1e-8, rel_tol=0) and math.isclose(y, point.y, abs_tol=1e-8, rel_tol=0)
        for anchor, point in ((route.start_anchor, route.waypoints[0]),
                              (route.end_anchor, route.waypoints[-1])):
            if coincides(point):
                return _ResolvedTarget(target.node_id, anchor.representation_id,
                    anchor.kind, anchor.target_port_id, anchor.anchor_key)
        index = next((index for index, (a, b) in enumerate(zip(route.waypoints, route.waypoints[1:]))
            if min(a.x, b.x) - 1e-8 <= x <= max(a.x, b.x) + 1e-8
            and min(a.y, b.y) - 1e-8 <= y <= max(a.y, b.y) + 1e-8
            and ((a.x == b.x and math.isclose(x, a.x, abs_tol=1e-8, rel_tol=0))
                 or (a.y == b.y and math.isclose(y, a.y, abs_tol=1e-8, rel_tol=0)))), None)
        if index is None:
            raise EditorCommandError("Точка присоединения больше не лежит на выбранном проводнике.")
        representation = GraphicalRepresentation(
            GraphicalRepresentationId.new(), page_id, RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=target.node_id, x=x, y=y, symbol_key="connection_point",
            extensions={"graphics": {"width": 8.0, "height": 8.0, "label_visible": False}},
        )
        anchor = RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, representation.id, target.node_id)
        left = list(route.waypoints[:index + 1])
        right = list(route.waypoints[index + 1:])
        if not coincides(left[-1]):
            left.append(RouteWaypoint(RouteWaypointId.new(), x, y))
        if not coincides(right[0]):
            right.insert(0, RouteWaypoint(RouteWaypointId.new(), x, y))
        # A saved bend may become a junction; its old ID remains in one half.
        elif left[-1].id == right[0].id:
            right[0] = replace(right[0], id=RouteWaypointId.new())
        routes = dict(draft.diagram.routes)
        routes[route.id] = replace(route, end_anchor=anchor, waypoints=tuple(left))
        second = replace(route, id=DiagramRouteId.new(), start_anchor=anchor, waypoints=tuple(right))
        routes[second.id] = second
        draft.diagram = replace(draft.diagram,
            representations={**draft.diagram.representations, representation.id: representation}, routes=routes)
        return _ResolvedTarget(target.node_id, representation.id, RouteAnchorKind.ELECTRICAL_NODE)

    @classmethod
    def _resolve_connection_target(
        cls,
        draft: ProjectDraft,
        target: ConnectionTarget,
        page_id: PageId,
        *,
        fallback_x: float = 0.0,
        fallback_y: float = 0.0,
    ) -> _ResolvedTarget:
        model = draft.electrical_model
        if isinstance(target, NodeTarget):
            node = model.electrical_nodes.get(target.node_id)
            if node is None:
                raise EditorCommandError("Электрический узел не найден.")
            if target.route_id is not None:
                return cls._resolve_conductor_target(draft, target, page_id)
            if target.representation_id is not None:
                representation = cls._require_representation(
                    draft.diagram, target.representation_id
                )
                if (
                    representation.page_id != page_id
                    or representation.electrical_node_id != node.id
                ):
                    raise EditorCommandError(
                        "Выбранная графическая привязка не представляет целевой узел."
                    )
            else:
                if (target.x is None) != (target.y is None):
                    raise EditorCommandError(
                        "Графическая привязка узла задаётся двумя координатами."
                    )
                representation = cls._ensure_node_representation(
                    draft,
                    node.id,
                    page_id,
                    name=node.name,
                    x=target.x if target.x is not None else fallback_x,
                    y=target.y if target.y is not None else fallback_y,
                )
            kind = (
                RouteAnchorKind.BUS
                if "busbar" in representation.symbol_key
                else RouteAnchorKind.ELECTRICAL_NODE
            )
            return _ResolvedTarget(
                node.id, representation.id, kind, anchor_key=target.anchor_key
            )
        if isinstance(target, NewNodeTarget):
            node = ElectricalNode(
                ElectricalNodeId.new(),
                target.name,
                declared_voltage_class_id=target.voltage_class_id,
                extensions={
                    "creation_origin": "automatic_free_endpoint",
                    "junction_kind": "automatic_endpoint",
                },
            )
            model.add_node(node)
            representation = cls._ensure_node_representation(
                draft,
                node.id,
                page_id,
                name=node.name,
                x=target.x,
                y=target.y,
                symbol_key=target.symbol_key,
            )
            kind = (
                RouteAnchorKind.BUS
                if "busbar" in target.symbol_key
                else RouteAnchorKind.ELECTRICAL_NODE
            )
            return _ResolvedTarget(
                node.id, representation.id, kind, anchor_key=target.anchor_key
            )
        if not isinstance(target, PortTarget):
            raise EditorCommandError("Неизвестная цель электрического соединения.")
        port = model.ports.get(target.port_id)
        if port is None:
            raise EditorCommandError("Электрический порт не найден.")
        connection = model.connection_for_port(port.id)
        if connection is None:
            voltage_id = model.port_voltage_class(port.id)
            port_definition = model.port_definition(port.id)
            node = ElectricalNode(
                ElectricalNodeId.new(),
                f"Узел порта «{port_definition.display_name}»",
                port_definition.kind_id,
                voltage_id,
                extensions={
                    "creation_origin": "automatic_port_endpoint",
                    "junction_kind": "automatic_endpoint",
                },
            )
            model.add_node(node)
            model.connect_port(port.id, node.id)
        else:
            node = model.electrical_nodes[connection.electrical_node_id]
        if target.representation_id is not None:
            representation = cls._require_representation(
                draft.diagram, target.representation_id
            )
            if (
                representation.page_id != page_id
                or representation.equipment_id != port.equipment_id
            ):
                raise EditorCommandError(
                    "Выбранная графическая привязка не представляет оборудование порта."
                )
        else:
            representation = cls._ensure_equipment_representation(
                draft,
                port.equipment_id,
                page_id,
                x=fallback_x,
                y=fallback_y,
            )
        return _ResolvedTarget(
            node.id,
            representation.id,
            RouteAnchorKind.EQUIPMENT_PORT,
            port.id,
            target.anchor_key,
        )

    @staticmethod
    def _route_waypoints(
        first: GraphicalRepresentation,
        second: GraphicalRepresentation,
    ) -> tuple[RouteWaypoint, ...]:
        x1, y1 = first.x, first.y
        x2, y2 = second.x, second.y
        if x1 == x2 and y1 == y2:
            x2 += 120.0
        points = [(x1, y1)]
        if x1 != x2 and y1 != y2:
            points.append((x2, y1))
        points.append((x2, y2))
        return tuple(
            RouteWaypoint(
                RouteWaypointId.new(),
                x,
                y,
                RouteWaypointSource.AUTOMATIC,
            )
            for x, y in points
        )

    @classmethod
    def _effective_route_waypoints(
        cls,
        first: GraphicalRepresentation,
        second: GraphicalRepresentation,
        supplied: Iterable[RouteWaypoint] | None,
    ) -> tuple[RouteWaypoint, ...]:
        """Use committed GUI geometry verbatim or build a safe local default."""
        if supplied is None:
            return cls._route_waypoints(first, second)
        result = tuple(supplied)
        if any(not isinstance(item, RouteWaypoint) for item in result):
            raise EditorCommandError(
                "Графическая трасса должна содержать точки маршрута."
            )
        if len(result) < 2:
            raise EditorCommandError(
                "Графическая трасса должна содержать не менее двух точек."
            )
        return result

    @classmethod
    def _add_route(
        cls,
        draft: ProjectDraft,
        route: DiagramRoute,
    ) -> None:
        values = dict(draft.diagram.routes)
        if route.id in values:
            raise EditorCommandError("Такая графическая трасса уже существует.")
        route = cls._route_with_available_bus_anchors(draft, route, preserve_user_goals=True)
        values[route.id] = route
        draft.diagram = _diagram_with_routes(draft.diagram, values)

    @classmethod
    def _route_with_available_bus_anchors(
        cls, draft: ProjectDraft, route: DiagramRoute, *,
        endpoint_indices: tuple[int, ...] = (0, 1),
        preserve_user_goals: bool = False,
    ) -> DiagramRoute:
        """Allocate only newly added/reconnected bus endpoints, not old taps."""
        anchors = [route.start_anchor, route.end_anchor]
        geometry_changed = False
        for index in endpoint_indices:
            anchor = anchors[index]
            if anchor.kind is not RouteAnchorKind.BUS:
                continue
            row = cls._require_representation(draft.diagram, anchor.representation_id)
            width, height = cls._equipment_symbol_size(row, None)
            point = route.waypoints[0 if index == 0 else -1]
            requested = endpoint_fraction(row, width, height, anchor, point)
            try:
                fraction = available_bus_fraction(
                    draft.diagram, row, width=width, height=height,
                    requested=requested, exclude_route_ids=(route.id,),
                )
            except ValueError as exc:
                raise EditorCommandError(str(exc)) from exc
            anchors[index] = replace(anchor, anchor_key=format(fraction, ".17g"))
            x, y, _ = bus_anchor_geometry(
                width=width, height=height, rotation=row.rotation_deg,
                center_x=row.x, center_y=row.y, fraction=fraction,
            )
            geometry_changed |= not (
                math.isclose(point.x, x, abs_tol=1e-8, rel_tol=0.0)
                and math.isclose(point.y, y, abs_tol=1e-8, rel_tol=0.0)
            )
            # An interior bus tap leaves across the bar, never along its ink.
            # Normalize newly supplied old-style elbows before saving them.
            peer = route.waypoints[1 if index == 0 else -2]
            expected = cls._bus_exit_direction(row, anchors[index], RouteVertex(x,y),
                RouteVertex(route.waypoints[-1 if index == 0 else 0].x,
                            route.waypoints[-1 if index == 0 else 0].y))
            if (peer.x,peer.y)!=(x,y):
                actual=direction_toward_point(x,y,peer.x,peer.y)
                geometry_changed |= actual != expected
        updated = replace(route, start_anchor=anchors[0], end_anchor=anchors[1])
        if geometry_changed:
            return cls._reroute_to_current_port_anchors(
                draft, updated, obstacles=cls._page_routing_obstacles(draft, route.page_id),
                preserve_user_goals=preserve_user_goals,
            )
        return updated

    @classmethod
    def _bus_exit_direction(cls, representation, anchor, point, peer):
        width,height=cls._equipment_symbol_size(representation,None)
        horizontal=width>=height
        if int(normalize_quarter_turn(representation.rotation_deg))%180:
            horizontal=not horizontal
        try:
            fraction=float(anchor.anchor_key)
        except (TypeError,ValueError):
            fraction=endpoint_fraction(representation,width,height,anchor,point)
        # Endpoint section ties may leave along the open end of a bus.
        if (math.isclose(fraction,0.0,abs_tol=1e-8) or math.isclose(fraction,1.0,abs_tol=1e-8)) and (
            math.isclose(point.y,peer.y,abs_tol=1e-8) if horizontal else
            math.isclose(point.x,peer.x,abs_tol=1e-8)):
            return direction_toward_point(point.x,point.y,peer.x,peer.y)
        if horizontal:
            return RouteDirection.UP if peer.y<point.y else RouteDirection.DOWN
        return RouteDirection.LEFT if peer.x<point.x else RouteDirection.RIGHT

    @staticmethod
    def _route_for_equipment(
        diagram: DiagramDocument,
        equipment_id: EquipmentId,
        page_id: PageId | None = None,
    ) -> DiagramRoute | None:
        rows = tuple(
            item
            for item in diagram.routes.values()
            if item.equipment_id == equipment_id
            and (page_id is None or item.page_id == page_id)
        )
        if not rows:
            return None
        if len(rows) > 1 and page_id is None:
            raise EditorCommandError(
                "У физической ветви несколько графических трасс; укажите страницу."
            )
        return rows[0]

    @staticmethod
    def _route_midpoint(route: DiagramRoute) -> tuple[float, float]:
        """Return a graphical midpoint; never interpret it as physical length."""
        points = route.waypoints
        lengths = [
            abs(second.x - first.x) + abs(second.y - first.y)
            for first, second in zip(points, points[1:])
        ]
        total = sum(lengths)
        if total <= 0:
            return points[0].x, points[0].y
        cursor = 0.0
        target = total / 2.0
        for first, second, length in zip(points, points[1:], lengths):
            if cursor + length >= target:
                ratio = (target - cursor) / length
                return (
                    first.x + (second.x - first.x) * ratio,
                    first.y + (second.y - first.y) * ratio,
                )
            cursor += length
        return points[-1].x, points[-1].y

    @staticmethod
    def _equipment_symbol_size(
        representation: GraphicalRepresentation,
        definition: Any,
    ) -> tuple[float, float]:
        """Вернуть те же эффективные габариты, которые использует холст."""

        raw = representation.extensions.get(GRAPHICS_EXTENSION_KEY, {})
        if not isinstance(raw, Mapping):
            raise EditorCommandError("Сохранённое графическое оформление повреждено.")
        key = representation.symbol_key.casefold()
        behavior = str(getattr(definition, "behavior_key", "equipment")).casefold()
        orientation = str(raw.get("orientation", "horizontal")).casefold()
        if behavior == "bus" or "busbar" in key:
            default_width, default_height = (
                (22.0, 150.0)
                if orientation == "vertical"
                else (150.0, 22.0)
            )
        elif behavior in {"line", "line_section"} or "line" in key:
            default_width, default_height = 120.0, 24.0
        else:
            default_width, default_height = 74.0, 54.0
        try:
            width = _finite(raw.get("width", default_width), "Ширина символа")
            height = _finite(raw.get("height", default_height), "Высота символа")
        except (TypeError, ValueError) as exc:
            raise EditorCommandError(str(exc)) from exc
        if width <= 0.0 or height <= 0.0:
            raise EditorCommandError("Габариты символа должны быть больше нуля.")
        return width, height

    @classmethod
    def _port_anchor_geometry(
        cls,
        model: ElectricalModel,
        representation: GraphicalRepresentation,
        port_id: PortId,
        *,
        rotation_deg: int | float | QuarterTurn | None = None,
    ) -> PortAnchorGeometry:
        if representation.equipment_id is None:
            raise EditorCommandError(
                "Графическая привязка порта должна представлять оборудование."
            )
        equipment = model.equipment.get(representation.equipment_id)
        if equipment is None or port_id not in equipment.port_ids:
            raise EditorCommandError(
                "Графическое представление не содержит выбранный электрический порт."
            )
        definition = model.equipment_type(
            equipment.type_id, equipment.type_version
        )
        width, height = cls._equipment_symbol_size(representation, definition)
        try:
            return port_anchor_by_id(
                equipment,
                definition,
                port_id,
                width=width,
                height=height,
                rotation=(
                    representation.rotation_deg
                    if rotation_deg is None
                    else rotation_deg
                ),
                center_x=representation.x,
                center_y=representation.y,
            )
        except (KeyError, ValueError) as exc:
            raise EditorCommandError(str(exc)) from exc

    @classmethod
    def _route_endpoint_geometry(
        cls,
        draft: ProjectDraft,
        anchor: RouteEndpointAnchor,
        fallback: RouteWaypoint,
        *,
        rotation_overrides: Mapping[GraphicalRepresentationId, QuarterTurn]
        | None = None,
        previous_representations: Mapping[GraphicalRepresentationId, GraphicalRepresentation]
        | None = None,
    ) -> tuple[RouteVertex, Any]:
        representation = draft.diagram.representations.get(anchor.representation_id)
        if representation is None:
            return RouteVertex(fallback.x, fallback.y), None
        override = (rotation_overrides or {}).get(representation.id)
        if anchor.target_port_id is None:
            if anchor.kind is RouteAnchorKind.BUS:
                width, height = cls._equipment_symbol_size(representation, None)
                try:
                    fraction = float(anchor.anchor_key)
                    if not math.isfinite(fraction):
                        raise ValueError
                except (TypeError, ValueError):
                    # Legacy buses may have no fractional anchor. Recover its
                    # local position BEFORE rotation, not on the new bus axis.
                    previous = (previous_representations or {}).get(
                        representation.id, representation
                    )
                    radians = math.radians(-previous.rotation_deg)
                    dx, dy = fallback.x - previous.x, fallback.y - previous.y
                    local_x = dx * math.cos(radians) - dy * math.sin(radians)
                    local_y = dx * math.sin(radians) + dy * math.cos(radians)
                    fraction = (local_y / height if height > width else local_x / width) + 0.5
                x, y, direction = bus_anchor_geometry(
                    width=width, height=height,
                    rotation=representation.rotation_deg if override is None else override,
                    center_x=representation.x, center_y=representation.y,
                    fraction=fraction,
                )
                return RouteVertex(x, y), direction
            return RouteVertex(fallback.x, fallback.y), None
        geometry = cls._port_anchor_geometry(
            draft.electrical_model,
            representation,
            anchor.target_port_id,
            rotation_deg=override,
        )
        if anchor.branch_port_id == anchor.target_port_id:
            equipment=draft.electrical_model.equipment[representation.equipment_id]
            definition=draft.electrical_model.equipment_type(equipment.type_id,equipment.type_version)
            if canonical_key(str(definition.extensions.get("diagram_symbol_key",representation.symbol_key)),definition.behavior_key) in {"line","line_section"}:
                # The line's OWN conductor continues into its terminal. A
                # different conductor attached there leaves outward. Giving
                # both the same direction would falsely overlap their leads.
                opposite={RouteDirection.LEFT:RouteDirection.RIGHT,RouteDirection.RIGHT:RouteDirection.LEFT,
                          RouteDirection.UP:RouteDirection.DOWN,RouteDirection.DOWN:RouteDirection.UP}
                return RouteVertex(geometry.x,geometry.y),opposite[geometry.direction]
        return RouteVertex(geometry.x, geometry.y), geometry.direction

    @classmethod
    def _route_endpoint_obstacles(
        cls,
        draft: ProjectDraft,
        route: DiagramRoute,
        rotation_overrides: Mapping[GraphicalRepresentationId, QuarterTurn] | None = None,
    ) -> tuple[RoutingObstacle, ...]:
        """Keep a rotated wire outside its own equipment, including its leads."""
        result = []
        for representation_id in dict.fromkeys((
            route.start_anchor.representation_id, route.end_anchor.representation_id,
        )):
            row = draft.diagram.representations.get(representation_id)
            if row is None or row.equipment_id is None:
                continue
            equipment = draft.electrical_model.equipment[row.equipment_id]
            definition = draft.electrical_model.equipment_type(
                equipment.type_id, equipment.type_version
            )
            if canonical_key(str(definition.extensions.get("diagram_symbol_key",row.symbol_key)),definition.behavior_key) in {"line","line_section"}:
                continue  # A conductor symbol is not a solid apparatus body.
            width, height = cls._equipment_symbol_size(row, definition)
            angle = (rotation_overrides or {}).get(row.id, row.rotation_deg)
            if int(normalize_quarter_turn(angle)) % 180:
                width, height = height, width
            result.append(RoutingObstacle(
                row.x - width / 2, row.y - height / 2,
                row.x + width / 2, row.y + height / 2,
            ))
        return tuple(result)

    @classmethod
    def _page_routing_obstacles(
        cls, draft: ProjectDraft, page_id: PageId,
    ) -> tuple[RoutingObstacle, ...]:
        """Apparatus bodies are solid; line and bus conductors may cross."""
        result = []
        for row in draft.diagram.representations.values():
            if row.page_id != page_id or row.equipment_id is None:
                continue
            equipment = draft.electrical_model.equipment[row.equipment_id]
            definition = draft.electrical_model.equipment_type(equipment.type_id, equipment.type_version)
            key = canonical_key(
                str(definition.extensions.get("diagram_symbol_key", row.symbol_key)),
                definition.behavior_key,
            )
            if key in {"line", "line_section", "busbar"}:
                continue
            width, height = cls._equipment_symbol_size(row, definition)
            if int(normalize_quarter_turn(row.rotation_deg)) % 180:
                width, height = height, width
            result.append(RoutingObstacle(row.x - width / 2, row.y - height / 2,
                                          row.x + width / 2, row.y + height / 2))
        return tuple(result)

    @classmethod
    def _reroute_to_current_port_anchors(
        cls,
        draft: ProjectDraft,
        route: DiagramRoute,
        *,
        rotation_overrides: Mapping[GraphicalRepresentationId, QuarterTurn]
        | None = None,
        previous_representations: Mapping[GraphicalRepresentationId, GraphicalRepresentation]
        | None = None,
        obstacles: tuple[RoutingObstacle, ...] | None = None,
        preserve_node_direction_ids: tuple[GraphicalRepresentationId, ...] = (),
        occupied_segments: tuple[tuple[float,float,float,float], ...] | None = None,
        preserve_user_goals: bool = False,
    ) -> DiagramRoute:
        """Локально перестроить одну трассу, сохранив ручные точки и их ID."""

        if len(route.waypoints) < 2:
            return route
        start, start_direction = cls._route_endpoint_geometry(
            draft,
            route.start_anchor,
            route.waypoints[0],
            rotation_overrides=rotation_overrides,
            previous_representations=previous_representations,
        )
        end, end_direction = cls._route_endpoint_geometry(
            draft,
            route.end_anchor,
            route.waypoints[-1],
            rotation_overrides=rotation_overrides,
            previous_representations=previous_representations,
        )
        # A bus has no fixed outward terminal side. Keep the saved attachment
        # fraction, but leave it toward its real peer instead of always UP.
        if start.point != end.point:
            if route.start_anchor.kind is RouteAnchorKind.BUS:
                start_direction = cls._bus_exit_direction(draft.diagram.representations[route.start_anchor.representation_id],
                    route.start_anchor,start,end)
            if route.end_anchor.kind is RouteAnchorKind.BUS:
                end_direction = cls._bus_exit_direction(draft.diagram.representations[route.end_anchor.representation_id],
                    route.end_anchor,end,start)
        if route.start_anchor.representation_id in preserve_node_direction_ids:
            peer = route.waypoints[1]
            start_direction = direction_toward_point(start.x, start.y, peer.x, peer.y)
        if route.end_anchor.representation_id in preserve_node_direction_ids:
            peer = route.waypoints[-2]
            end_direction = direction_toward_point(end.x, end.y, peer.x, peer.y)
        manual_rows = tuple(
            item
            for item in route.waypoints[1:-1]
            if item.source is RouteWaypointSource.USER or item.pinned
        )
        vertices = build_orthogonal_route(
            RoutingRequest(
                start,
                end,
                start_direction,
                end_direction,
                tuple(
                    RouteVertex(
                        item.x,
                        item.y,
                        RouteVertexSource.USER,
                        item.pinned or preserve_user_goals,
                    )
                    for item in manual_rows
                ),
                obstacles=(cls._route_endpoint_obstacles(draft, route, rotation_overrides)
                           if obstacles is None else obstacles),
                port_stub=12.0,
                occupied_segments=occupied_segments if occupied_segments is not None else tuple(
                    (a.x, a.y, b.x, b.y)
                    for other in draft.diagram.routes.values()
                    if other.page_id == route.page_id and other.id != route.id
                    for a, b in zip(other.waypoints, other.waypoints[1:])
                    if (a.x, a.y) != (b.x, b.y)
                ),
            )
        )
        if len(vertices) < 2:
            raise EditorCommandError(
                "После поворота невозможно построить корректную локальную трассу."
            )
        manual_ids = {(item.x, item.y): item.id for item in route.waypoints[1:-1]}
        original_pins={(item.x,item.y):item.pinned for item in route.waypoints[1:-1]}
        waypoints: list[RouteWaypoint] = []
        for index, vertex in enumerate(vertices):
            if index == 0:
                waypoints.append(replace(route.waypoints[0], x=vertex.x, y=vertex.y))
                continue
            elif index == len(vertices) - 1:
                waypoints.append(replace(route.waypoints[-1], x=vertex.x, y=vertex.y))
                continue
            else:
                waypoint_id = manual_ids.get(
                    (vertex.x, vertex.y), RouteWaypointId.new()
                )
            waypoints.append(
                RouteWaypoint(
                    waypoint_id,
                    vertex.x,
                    vertex.y,
                    (
                        RouteWaypointSource.USER
                        if vertex.source is RouteVertexSource.USER
                        else RouteWaypointSource.AUTOMATIC
                    ),
                    original_pins.get((vertex.x,vertex.y),vertex.pinned) if preserve_user_goals else vertex.pinned,
                )
            )
        return replace(route, waypoints=tuple(waypoints))

    @staticmethod
    def _route_quarter_turn_at(
        route: DiagramRoute | None,
        x: float,
        y: float,
    ) -> QuarterTurn:
        if route is None:
            return QuarterTurn.DEG_0
        segment = closest_directed_segment(
            ((item.x, item.y) for item in route.waypoints), x, y
        )
        return segment.quarter_turn if segment is not None else QuarterTurn.DEG_0

    @classmethod
    def _automatic_quarter_turn(
        cls,
        draft: ProjectDraft,
        representation: GraphicalRepresentation,
    ) -> QuarterTurn:
        """Ориентировать главный подключённый порт по ближайшей трассе."""

        incident = sorted(
            (
                route
                for route in draft.diagram.routes.values()
                if representation.id
                in {
                    route.start_anchor.representation_id,
                    route.end_anchor.representation_id,
                }
            ),
            key=lambda item: item.id.value,
        )
        equipment = (
            draft.electrical_model.equipment.get(representation.equipment_id)
            if representation.equipment_id is not None
            else None
        )
        if equipment is not None and len(equipment.port_ids) == 2:
            far_by_port: dict[PortId, RouteWaypoint] = {}
            for route in incident:
                if (
                    route.start_anchor.representation_id == representation.id
                    and route.start_anchor.target_port_id is not None
                ):
                    far_by_port[route.start_anchor.target_port_id] = (
                        route.waypoints[-1]
                    )
                if (
                    route.end_anchor.representation_id == representation.id
                    and route.end_anchor.target_port_id is not None
                ):
                    far_by_port[route.end_anchor.target_port_id] = (
                        route.waypoints[0]
                    )
            first_id, second_id = equipment.port_ids
            if first_id in far_by_port and second_id in far_by_port:
                first_far = far_by_port[first_id]
                second_far = far_by_port[second_id]
                target = closest_directed_segment(
                    (
                        (first_far.x, first_far.y),
                        (second_far.x, second_far.y),
                    ),
                    first_far.x,
                    first_far.y,
                )
                definition = draft.electrical_model.equipment_type(
                    equipment.type_id, equipment.type_version
                )
                width, height = cls._equipment_symbol_size(
                    representation, definition
                )
                first_base = port_anchor_by_id(
                    equipment,
                    definition,
                    first_id,
                    width=width,
                    height=height,
                    rotation=QuarterTurn.DEG_0,
                )
                second_base = port_anchor_by_id(
                    equipment,
                    definition,
                    second_id,
                    width=width,
                    height=height,
                    rotation=QuarterTurn.DEG_0,
                )
                source = closest_directed_segment(
                    (
                        (first_base.x, first_base.y),
                        (second_base.x, second_base.y),
                    ),
                    first_base.x,
                    first_base.y,
                )
                if source is not None and target is not None:
                    return quarter_turn_between_directions(
                        source.direction, target.direction
                    )
        for route in incident:
            if route.start_anchor.representation_id == representation.id:
                anchor = route.start_anchor
                points = route.waypoints[:2]
            else:
                anchor = route.end_anchor
                points = tuple(reversed(route.waypoints[-2:]))
            if anchor.target_port_id is None or len(points) < 2:
                continue
            base = cls._port_anchor_geometry(
                draft.electrical_model,
                representation,
                anchor.target_port_id,
                rotation_deg=QuarterTurn.DEG_0,
            )
            segment = closest_directed_segment(
                ((item.x, item.y) for item in points),
                points[0].x,
                points[0].y,
            )
            if segment is not None:
                return quarter_turn_between_directions(
                    base.direction, segment.direction
                )
        return QuarterTurn.DEG_0

    @staticmethod
    def _reroute_for_moved_representations(
        route: DiagramRoute,
        deltas: Mapping[GraphicalRepresentationId, tuple[float, float]],
    ) -> DiagramRoute:
        start_delta = deltas.get(route.start_anchor.representation_id, (0.0, 0.0))
        end_delta = deltas.get(route.end_anchor.representation_id, (0.0, 0.0))
        if start_delta == (0.0, 0.0) and end_delta == (0.0, 0.0):
            return route
        if start_delta == end_delta:
            dx, dy = start_delta
            return replace(
                route,
                waypoints=tuple(
                    replace(point, x=point.x + dx, y=point.y + dy)
                    for point in route.waypoints
                ),
            )

        old_points = route.waypoints
        start = replace(
            old_points[0],
            x=old_points[0].x + start_delta[0],
            y=old_points[0].y + start_delta[1],
        )
        finish = replace(
            old_points[-1],
            x=old_points[-1].x + end_delta[0],
            y=old_points[-1].y + end_delta[1],
        )
        anchors = [
            start,
            *(
                point
                for point in old_points[1:-1]
                if point.source is RouteWaypointSource.USER or point.pinned
            ),
            finish,
        ]
        rebuilt: list[RouteWaypoint] = [anchors[0]]
        for target in anchors[1:]:
            current = rebuilt[-1]
            if current.x == target.x and current.y == target.y:
                # A user point may coincide with a moved endpoint. Keeping the
                # electrical anchor is more important than a zero-length leg;
                # the next manual point (if any) remains untouched.
                continue
            if current.x != target.x and current.y != target.y:
                rebuilt.append(RouteWaypoint(
                    RouteWaypointId.new(),
                    target.x,
                    current.y,
                    RouteWaypointSource.AUTOMATIC,
                ))
            rebuilt.append(target)
        if len(rebuilt) < 2:
            raise EditorCommandError(
                "После перемещения невозможно построить корректную трассу."
            )
        return replace(route, waypoints=tuple(rebuilt))

    @classmethod
    def _section_route_endpoints(
        cls,
        draft: ProjectDraft,
        section_id: EquipmentId,
        page_id: PageId,
        route: DiagramRoute | None,
    ) -> tuple[
        RouteEndpointAnchor,
        RouteEndpointAnchor,
        GraphicalRepresentation,
        GraphicalRepresentation,
    ]:
        if route is not None:
            return (
                route.start_anchor,
                route.end_anchor,
                draft.diagram.representations[route.start_anchor.representation_id],
                draft.diagram.representations[route.end_anchor.representation_id],
            )
        model = draft.electrical_model
        from_port = model.port_by_role(section_id, "from")
        to_port = model.port_by_role(section_id, "to")
        from_connection = model.connection_for_port(from_port.id)
        to_connection = model.connection_for_port(to_port.id)
        if from_connection is None or to_connection is None:
            raise EditorCommandError(
                "Физический участок должен быть подключён с двух сторон."
            )
        first_node = model.electrical_nodes[from_connection.electrical_node_id]
        second_node = model.electrical_nodes[to_connection.electrical_node_id]
        first_representation = cls._ensure_node_representation(
            draft,
            first_node.id,
            page_id,
            name=first_node.name,
        )
        second_representation = cls._ensure_node_representation(
            draft,
            second_node.id,
            page_id,
            name=second_node.name,
            x=first_representation.x + 120.0,
            y=first_representation.y,
        )
        return (
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                first_representation.id,
                first_node.id,
                branch_port_id=from_port.id,
            ),
            RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                second_representation.id,
                second_node.id,
                branch_port_id=to_port.id,
            ),
            first_representation,
            second_representation,
        )

    def create_page(
        self,
        name: str,
        *,
        parent_id: PageId | str | None = None,
    ) -> PageId:
        self._require_edit()

        def command(draft: ProjectDraft) -> PageId:
            page_id = PageId.new()
            parent = self._page_id(parent_id) if parent_id is not None else None
            if parent is not None and parent not in draft.diagram.pages:
                raise EditorCommandError(tr("error.page_missing"))
            pages = dict(draft.diagram.pages)
            pages[page_id] = DiagramPage(page_id, name, parent, len(pages))
            draft.diagram = _diagram_with_pages(draft.diagram, pages)
            draft.diagram = self._workspace_with_active_page(draft.diagram, page_id)
            return page_id

        return self._execute(tr("command.create_page"), command)

    def ensure_default_page(self) -> PageId:
        workspace = self.workspace_state
        if workspace.active_page_id:
            selected = PageId(workspace.active_page_id)
            if selected in self.diagram.pages:
                return selected
        if self.diagram.pages:
            return next(iter(self.diagram.pages))
        return self.create_page(tr("default.page"))

    def add_equipment(
        self,
        type_id: EquipmentTypeId | str,
        name: str,
        *,
        page_id: PageId | str | None = None,
        x: float = 0.0,
        y: float = 0.0,
        properties: Mapping[str, Any] | None = None,
        voltage_class_by_group: Mapping[str, VoltageClassId] | None = None,
        normal_position: SwitchPosition | None = None,
        note: str = "",
        symbol_key: str | None = None,
        width: float = 80.0,
        height: float = 50.0,
        rotation_deg: float = 0.0,
        orientation_mode: OrientationMode | str = OrientationMode.AUTO,
        label: str | None = None,
    ) -> AddedElement:
        self._require_edit()
        x, y = self.snap_point(x, y)
        try:
            rotation = normalize_quarter_turn(rotation_deg)
            normalized_orientation_mode = normalize_orientation_mode(
                orientation_mode
            )
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc

        def command(draft: ProjectDraft) -> AddedElement:
            selected_page = self._resolve_page(draft, page_id)
            normalized_type_id = (
                type_id if isinstance(type_id, EquipmentTypeId)
                else EquipmentTypeId(type_id)
            )
            definition = draft.electrical_model.equipment_type(
                normalized_type_id, 1
            )
            effective_normal_position = normal_position
            if (
                effective_normal_position is None
                and "switch.position" in definition.capabilities
            ):
                effective_normal_position = SwitchPosition(
                    definition.extensions.get(
                        "default_normal_position", SwitchPosition.CLOSED.value
                    )
                )
            effective_properties = dict(properties or {})
            effective_voltage_groups = dict(voltage_class_by_group or {})
            if definition.behavior_key == "recloser":
                if "main" not in effective_voltage_groups:
                    preferred = VoltageClassId("builtin.voltage.ac.10kv")
                    voltage_id = (
                        preferred
                        if preferred in draft.electrical_model.voltage_classes
                        else next(iter(draft.electrical_model.voltage_classes), None)
                    )
                    if voltage_id is None:
                        raise EditorCommandError(
                            "Для реклоузера в проекте должен быть зарегистрирован класс напряжения."
                        )
                    effective_voltage_groups["main"] = voltage_id
                try:
                    voltage = draft.electrical_model.voltage_classes[
                        effective_voltage_groups["main"]
                    ]
                except KeyError as exc:
                    raise EditorCommandError(
                        "Для реклоузера выбран неизвестный класс напряжения."
                    ) from exc
                effective_properties.setdefault("rated_current_a", 630.0)
                effective_properties.setdefault(
                    "rated_voltage_v", voltage.nominal_voltage_v
                )
            equipment, _ = draft.electrical_model.create_equipment(
                normalized_type_id,
                name,
                properties=effective_properties,
                voltage_class_by_group=effective_voltage_groups,
                normal_position=effective_normal_position,
                note=note,
                extensions={EDITOR_DRAFT_EXTENSION_KEY: {"draft": True}},
            )
            default_symbol = definition.extensions.get("diagram_symbol_key", "")
            representation = GraphicalRepresentation(
                GraphicalRepresentationId.new(),
                selected_page,
                RepresentationTargetKind.EQUIPMENT,
                equipment_id=equipment.id,
                x=x,
                y=y,
                rotation_deg=float(rotation),
                symbol_key=symbol_key if symbol_key is not None else str(default_symbol),
                label=name if label is None else label,
            )
            representation = representation_with_graphics(
                representation,
                RepresentationGraphics(
                    width=width,
                    height=height,
                    orientation_mode=normalized_orientation_mode,
                    label_manual=False,
                ),
            )
            draft.diagram = self._add_representation(draft.diagram, representation)
            return AddedElement(
                equipment.id,
                equipment.port_ids,
                representation.id,
                selected_page,
            )

        return self._execute(tr("command.add_equipment"), command)

    def preview_equipment_attachment(self, type_id, name, target=None, **kwargs):
        from .equipment_attachment import preview_attachment
        return preview_attachment(self, type_id, name, target, **kwargs)

    def preview_inline_equipment(self, route_id, x, y, **kwargs):
        from .ordinary_wire_insertion import preview_inline
        return preview_inline(self, route_id, x, y, **kwargs)

    def apply_equipment_placement(self, proposal):
        from .equipment_attachment import apply_placement
        return apply_placement(self, proposal)

    def set_route_bends_pinned(self, route_id, pinned):
        from .route_geometry_rules import set_route_bends_pinned
        self._require_edit()
        def command(draft):
            route = draft.diagram.routes.get(route_id)
            if route is None:
                raise EditorCommandError("Маршрут не найден.")
            updated = set_route_bends_pinned(route, pinned)
            draft.diagram = replace(draft.diagram, routes={**draft.diagram.routes, route_id: updated})
            return updated
        return self._execute("Закрепить изгибы" if pinned else "Освободить изгибы", command)

    def auto_route_diagram_route(self, route_id):
        from .route_geometry_rules import set_route_bends_pinned
        self._require_edit()
        def command(draft):
            route = draft.diagram.routes.get(route_id)
            if route is None:
                raise EditorCommandError("Маршрут не найден.")
            updated = self._reroute_to_current_port_anchors(draft,
                set_route_bends_pinned(route, False),
                obstacles=self._page_routing_obstacles(draft, route.page_id))
            draft.diagram = replace(draft.diagram, routes={**draft.diagram.routes, route_id: updated})
            return updated
        return self._execute("Упростить маршрут автоматически", command)

    def add_electrical_node(
        self,
        name: str = "",
        *,
        page_id: PageId | str | None = None,
        x: float = 0.0,
        y: float = 0.0,
        kind_id: PortKindId | None = None,
        voltage_class_id: VoltageClassId | None = None,
        symbol_key: str = "electrical_node",
        width: float = 80.0,
        height: float = 12.0,
        rotation_deg: float = 0.0,
        label: str | None = None,
    ) -> AddedNode:
        self._require_edit()
        x, y = self.snap_point(x, y)
        try:
            rotation = normalize_quarter_turn(rotation_deg)
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc

        def command(draft: ProjectDraft) -> AddedNode:
            selected_page = self._resolve_page(draft, page_id)
            node = ElectricalNode(
                ElectricalNodeId.new(),
                name,
                kind_id or PortKindId("builtin.port.ac_power"),
                voltage_class_id,
            )
            draft.electrical_model.add_node(node)
            representation = GraphicalRepresentation(
                GraphicalRepresentationId.new(),
                selected_page,
                RepresentationTargetKind.ELECTRICAL_NODE,
                electrical_node_id=node.id,
                x=x,
                y=y,
                rotation_deg=float(rotation),
                symbol_key=symbol_key,
                label=(name or tr("default.node")) if label is None else label,
            )
            representation = representation_with_graphics(
                representation,
                RepresentationGraphics(width=width, height=height, label_manual=False),
            )
            draft.diagram = self._add_representation(draft.diagram, representation)
            return AddedNode(node.id, representation.id, selected_page)

        return self._execute(tr("command.add_node"), command)

    def preview_connection_voltage(
        self,
        first: ConnectionTarget | PortId | ElectricalNodeId | VoltageClassId,
        second: ConnectionTarget | PortId | ElectricalNodeId | VoltageClassId,
    ) -> VoltageCompatibility:
        """Preview the command's voltage rule, including first-connection adoption.

        The copy may adopt an empty group; the live model and history stay
        unchanged. Occupied ports are allowed here because reconnecting and
        inserting a line need the same voltage check. This method does not
        authorize an electrical topology operation by itself.
        """
        def semantic_endpoint(value):
            if isinstance(value, PortTarget):
                value = value.port_id
            elif isinstance(value, NodeTarget):
                value = value.node_id
            elif isinstance(value, NewNodeTarget):
                voltage = value.voltage_class_id
                return (("new_node", voltage)
                        if voltage is None or isinstance(voltage, VoltageClassId)
                        else None)
            if isinstance(value, PortId):
                return ("port", value)
            if isinstance(value, ElectricalNodeId):
                return ("node", value)
            if isinstance(value, VoltageClassId):
                return ("voltage_class", value)
            return None  # Unsupported inputs must take the ordinary guard.

        live_model = self.model
        endpoints = (semantic_endpoint(first), semantic_endpoint(second))
        # Memento copies every canonical store. Their records and nested JSON
        # are immutable, so replacement/direct same-revision edits compare
        # unequal without serializing the whole model on every mouse move.
        inputs = ElectricalModelMemento.capture(live_model)
        cached = getattr(self, "_voltage_preview_memo", None)
        cacheable = all(endpoint is not None for endpoint in endpoints)
        if (cacheable and cached is not None and cached.model is live_model
                and cached.endpoints == endpoints and cached.inputs == inputs):
            return cached.result
        self._voltage_preview_memo = None
        model = live_model._transaction_copy()
        try:
            _, _, voltage = self._guard_connection_voltage(model, first, second, adopt=True)
            result = VoltageCompatibility(True, "Соединение допустимо.", voltage)
        except (DomainInvariantError, EditorCommandError) as exc:
            result = VoltageCompatibility(False, str(exc))
        # Never retain a result under an input signature captured before a
        # concurrent/reentrant edit. Commit still performs its own preflight.
        if (cacheable and self.model is live_model
                and ElectricalModelMemento.capture(live_model) == inputs):
            self._voltage_preview_memo = _VoltagePreviewMemo(
                live_model, inputs, endpoints, result,
            )
        return result

    def validate_connection(
        self,
        source_port_id: PortId,
        target: ConnectionTarget,
    ) -> ConnectionCompatibility:
        """Проверить цель без мутации проекта и без записи undo."""
        model = self.model._transaction_copy()
        try:
            source = model.ports.get(source_port_id)
            if source is None:
                raise DomainInvariantError("Исходный электрический порт не найден.")
            if model.connection_for_port(source_port_id) is not None:
                raise DomainInvariantError("Исходный электрический порт уже подключён.")
            _, target, effective_voltage = self._guard_connection_voltage(
                model, source_port_id, target, adopt=True,
            )
            if isinstance(target, PortTarget):
                target_port = model.ports.get(target.port_id)
                if target_port is None:
                    raise DomainInvariantError("Целевой электрический порт не найден.")
                model.connect_ports(source_port_id, target.port_id)
                effective_voltage = (
                    effective_voltage or model.port_voltage_class(target.port_id)
                )
            elif isinstance(target, NodeTarget):
                node = model.electrical_nodes.get(target.node_id)
                if node is None:
                    raise DomainInvariantError("Целевой электрический узел не найден.")
                model.connect_port(source_port_id, node.id)
                effective_voltage = (
                    effective_voltage or node.declared_voltage_class_id
                )
            elif isinstance(target, NewNodeTarget):
                node = ElectricalNode(
                    ElectricalNodeId.new(),
                    target.name,
                    model.port_definition(source_port_id).kind_id,
                    target.voltage_class_id or effective_voltage,
                )
                model.add_node(node)
                model.connect_port(source_port_id, node.id)
                effective_voltage = (
                    effective_voltage or node.declared_voltage_class_id
                )
            else:
                raise DomainInvariantError("Неизвестная цель электрического соединения.")
            return ConnectionCompatibility(
                True, "ok", "Соединение допустимо.", effective_voltage
            )
        except (DomainInvariantError, EditorCommandError) as exc:
            return ConnectionCompatibility(False, "error", str(exc), None)

    @staticmethod
    def _adopt_voltage_from_peer(model, target, peer) -> bool:
        """Заполнить пустой класс группы аппарата классом того, к чему ведут.

        Ничего не делает, если класс уже задан, если цель — не вывод
        оборудования, или если класс второго конца сам неизвестен: подставлять
        неизвестное вместо неизвестного бессмысленно.
        """
        if not isinstance(target, PortId) or target not in model.ports:
            return False
        definition = model.port_definition(target)
        group = getattr(definition, "voltage_group", "")
        equipment = model.equipment.get(model.ports[target].equipment_id)
        if not group or equipment is None:
            return False
        if equipment.voltage_class_by_group.get(group) is not None:
            return False
        known = endpoint_voltage(model, peer) if peer is not None else None
        if known is None or not known.valid:
            return False
        try:
            return model.adopt_group_voltage_class(
                equipment.id, group, known.voltage_class_id
            )
        except DomainInvariantError:
            return False

    @staticmethod
    def _guard_connection_voltage(model, first, second, *, adopt=False):
        """Preflight before allocating nodes/ports or disconnecting a source.

        A new endpoint and, with adopt=True, an empty equipment voltage group
        at its first connection may inherit the known peer voltage. Explicit
        classes, existing unknown nodes and already connected groups are not
        relabelled. Adoption runs only on a command draft or a preview copy.
        """
        def reference(value):
            if isinstance(value, PortTarget):
                return value.port_id
            if isinstance(value, NodeTarget):
                return value.node_id
            if isinstance(value, NewNodeTarget):
                return value.voltage_class_id
            return value

        first_ref, second_ref = reference(first), reference(second)
        #  Решение заказчика 31.08.2026: класс напряжения нового аппарата
        #  подставляется ПРИ ПЕРВОМ подключении. Раньше выключатель, только что
        #  взятый из палитры, нельзя было довести до шины, пока не выберешь
        #  класс руками, — а класс этот и так однозначно следует из того, к чему
        #  его ведут. Заполняется только пустое поле: заданный класс остаётся
        #  решением человека, и подмена его молча меняла бы весь расчёт.
        #  Подстановка класса — ИЗМЕНЕНИЕ модели, поэтому она разрешена только
        #  на черновике команды или на отдельной копии для предварительной
        #  проверки. Подсветка цели не меняет рабочую модель и историю.
        if adopt:
            ProjectEditorController._adopt_voltage_from_peer(model, first_ref, second_ref)
            ProjectEditorController._adopt_voltage_from_peer(model, second_ref, first_ref)
        if first_ref is None and isinstance(first, NewNodeTarget) and second_ref is not None:
            known = endpoint_voltage(model, second_ref)
            if not known.valid:
                raise EditorCommandError(known.message)
            first = replace(first, voltage_class_id=known.voltage_class_id)
            first_ref = known.voltage_class_id
        if second_ref is None and isinstance(second, NewNodeTarget) and first_ref is not None:
            known = endpoint_voltage(model, first_ref)
            if not known.valid:
                raise EditorCommandError(known.message)
            second = replace(second, voltage_class_id=known.voltage_class_id)
            second_ref = known.voltage_class_id
        if first_ref is None or second_ref is None:
            raise EditorCommandError("Подключение запрещено: напряжение концов не определено. Задайте класс напряжения.")
        compatibility = check_connection_voltage(model, first_ref, second_ref)
        if not compatibility.valid:
            raise EditorCommandError(compatibility.message)
        return first, second, compatibility.voltage_class_id

    @classmethod
    def _line_connection_voltage(cls, model, section_id):
        """A tap cannot bypass voltage checks by referring to a physical route."""
        first = model.port_by_role(section_id, "from").id
        second = model.port_by_role(section_id, "to").id
        return cls._guard_connection_voltage(model, first, second)[2]

    def connect_ports(
        self,
        first_port_id: PortId,
        second_port_id: PortId,
        *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        first_representation_id: GraphicalRepresentationId | None = None,
        second_representation_id: GraphicalRepresentationId | None = None,
        first_anchor_key: str = "",
        second_anchor_key: str = "",
    ) -> ConnectionEditResult:
        self._require_edit()

        def command(draft: ProjectDraft) -> ConnectionEditResult:
            selected_page = self._resolve_page(draft, page_id)
            self._guard_connection_voltage(draft.electrical_model, first_port_id, second_port_id, adopt=True)
            draft.electrical_model.connect_ports(first_port_id, second_port_id)
            first_connection = draft.electrical_model.connection_for_port(first_port_id)
            second_connection = draft.electrical_model.connection_for_port(second_port_id)
            if first_connection is None or second_connection is None:
                raise EditorCommandError("Соединение портов не было завершено.")
            node_id = first_connection.electrical_node_id
            first_port = draft.electrical_model.ports[first_port_id]
            second_port = draft.electrical_model.ports[second_port_id]
            first_representation = self._equipment_representation(
                draft,
                first_port.equipment_id,
                selected_page,
                first_representation_id,
            )
            second_representation = self._equipment_representation(
                draft,
                second_port.equipment_id,
                selected_page,
                second_representation_id,
                x=120.0,
            )
            route = DiagramRoute(
                DiagramRouteId.new(),
                selected_page,
                DiagramRouteKind.NODE_CONNECTION,
                RouteEndpointAnchor(
                    RouteAnchorKind.EQUIPMENT_PORT,
                    first_representation.id,
                    node_id,
                    target_port_id=first_port_id,
                    anchor_key=first_anchor_key,
                ),
                RouteEndpointAnchor(
                    RouteAnchorKind.EQUIPMENT_PORT,
                    second_representation.id,
                    node_id,
                    target_port_id=second_port_id,
                    anchor_key=second_anchor_key,
                ),
                electrical_node_id=node_id,
                waypoints=self._effective_route_waypoints(
                    first_representation,
                    second_representation,
                    route_waypoints,
                ),
            )
            self._add_route(draft, route)
            return ConnectionEditResult(
                node_id,
                (first_connection.id, second_connection.id),
                route.id,
            )

        return self._execute("Создать электрическое соединение", command)

    def connect_from_node(
        self, source: NodeTarget, target: ConnectionTarget, *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
    ) -> ConnectionEditResult:
        """An explicit zero-impedance conductor, including bus-to-bus joins.

        Merge the electrical nodes and every saved graphical reference in one
        transaction. The domain merge guard prevents shorting an apparatus.
        """
        self._require_edit()
        if not isinstance(source, NodeTarget):
            raise EditorCommandError("Начало соединения должно быть электрическим узлом.")

        def command(draft: ProjectDraft) -> ConnectionEditResult:
            model = draft.electrical_model
            selected_page = self._resolve_page(draft, page_id)
            first, second, _ = self._guard_connection_voltage(model, source, target, adopt=True)
            start = self._resolve_connection_target(draft, first, selected_page)
            end = self._resolve_connection_target(draft, second, selected_page)
            if start.node_id == end.node_id:
                raise EditorCommandError("Уже соединено одним электрическим узлом; провод не добавлен.")
            model.merge_nodes(start.node_id, end.node_id)
            def remap(anchor):
                return (replace(anchor, electrical_node_id=start.node_id)
                        if anchor.electrical_node_id == end.node_id else anchor)
            draft.diagram = replace(
                draft.diagram,
                representations={key: (replace(row, electrical_node_id=start.node_id)
                    if row.electrical_node_id == end.node_id else row)
                    for key, row in draft.diagram.representations.items()},
                routes={key: replace(row, start_anchor=remap(row.start_anchor),
                    end_anchor=remap(row.end_anchor), electrical_node_id=(start.node_id
                        if row.electrical_node_id == end.node_id else row.electrical_node_id))
                    for key, row in draft.diagram.routes.items()},
            )
            route = DiagramRoute(
                DiagramRouteId.new(), selected_page, DiagramRouteKind.NODE_CONNECTION,
                RouteEndpointAnchor(start.anchor_kind, start.representation_id, start.node_id,
                    target_port_id=start.target_port_id, anchor_key=start.anchor_key),
                RouteEndpointAnchor(end.anchor_kind, end.representation_id, start.node_id,
                    target_port_id=end.target_port_id, anchor_key=end.anchor_key),
                electrical_node_id=start.node_id,
                waypoints=self._effective_route_waypoints(
                    draft.diagram.representations[start.representation_id],
                    draft.diagram.representations[end.representation_id], route_waypoints),
            )
            self._add_route(draft, route)
            return ConnectionEditResult(start.node_id, tuple(
                row.id for row in model.connections.values()
                if row.electrical_node_id == start.node_id), route.id)
        return self._execute("Соединить электрические узлы проводом", command)

    def connect_port_to_node(
        self,
        port_id: PortId,
        node_id: ElectricalNodeId,
        *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        source_representation_id: GraphicalRepresentationId | None = None,
        node_representation_id: GraphicalRepresentationId | None = None,
        source_anchor_key: str = "",
        target_anchor_key: str = "",
    ) -> ConnectionEditResult:
        self._require_edit()

        def command(draft: ProjectDraft) -> ConnectionEditResult:
            selected_page = self._resolve_page(draft, page_id)
            self._guard_connection_voltage(draft.electrical_model, port_id, node_id, adopt=True)
            connection, _ = draft.electrical_model.connect_port(port_id, node_id)
            port = draft.electrical_model.ports[port_id]
            equipment_representation = self._equipment_representation(
                draft,
                port.equipment_id,
                selected_page,
                source_representation_id,
            )
            resolved = self._resolve_connection_target(
                draft,
                NodeTarget(node_id, node_representation_id, target_anchor_key),
                selected_page,
                fallback_x=equipment_representation.x + 120.0,
                fallback_y=equipment_representation.y,
            )
            node_representation = draft.diagram.representations[
                resolved.representation_id
            ]
            route = DiagramRoute(
                DiagramRouteId.new(),
                selected_page,
                DiagramRouteKind.NODE_CONNECTION,
                RouteEndpointAnchor(
                    RouteAnchorKind.EQUIPMENT_PORT,
                    equipment_representation.id,
                    node_id,
                    target_port_id=port_id,
                    anchor_key=source_anchor_key,
                ),
                RouteEndpointAnchor(
                    resolved.anchor_kind,
                    node_representation.id,
                    node_id,
                    anchor_key=target_anchor_key,
                ),
                electrical_node_id=node_id,
                waypoints=self._effective_route_waypoints(
                    equipment_representation,
                    node_representation,
                    route_waypoints,
                ),
            )
            self._add_route(draft, route)
            return ConnectionEditResult(node_id, (connection.id,), route.id)

        return self._execute("Подключить порт к электрическому узлу", command)

    def finish_port_on_new_node(
        self,
        port_id: PortId,
        target: NewNodeTarget,
        *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        source_representation_id: GraphicalRepresentationId | None = None,
        source_anchor_key: str = "",
    ) -> ConnectionEditResult:
        self._require_edit()

        def command(draft: ProjectDraft) -> ConnectionEditResult:
            selected_page = self._resolve_page(draft, page_id)
            _, normalized_target, _ = self._guard_connection_voltage(draft.electrical_model, port_id, target, adopt=True)
            resolved = self._resolve_connection_target(
                draft, normalized_target, selected_page
            )
            connection, _ = draft.electrical_model.connect_port(
                port_id, resolved.node_id
            )
            port = draft.electrical_model.ports[port_id]
            equipment_representation = self._equipment_representation(
                draft,
                port.equipment_id,
                selected_page,
                source_representation_id,
            )
            node_representation = draft.diagram.representations[
                resolved.representation_id
            ]
            route = DiagramRoute(
                DiagramRouteId.new(),
                selected_page,
                DiagramRouteKind.NODE_CONNECTION,
                RouteEndpointAnchor(
                    RouteAnchorKind.EQUIPMENT_PORT,
                    equipment_representation.id,
                    resolved.node_id,
                    target_port_id=port_id,
                    anchor_key=source_anchor_key,
                ),
                RouteEndpointAnchor(
                    resolved.anchor_kind,
                    resolved.representation_id,
                    resolved.node_id,
                    anchor_key=resolved.anchor_key,
                ),
                electrical_node_id=resolved.node_id,
                waypoints=self._effective_route_waypoints(
                    equipment_representation,
                    node_representation,
                    route_waypoints,
                ),
            )
            self._add_route(draft, route)
            return ConnectionEditResult(
                resolved.node_id, (connection.id,), route.id
            )

        return self._execute("Завершить соединение новым узлом", command)

    def reconnect_port(
        self,
        port_id: PortId,
        target: ConnectionTarget,
        *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        source_representation_id: GraphicalRepresentationId | None = None,
        source_anchor_key: str = "",
        target_anchor_key: str = "",
    ) -> ConnectionEditResult:
        self._require_edit()

        def command(draft: ProjectDraft) -> ConnectionEditResult:
            model = draft.electrical_model
            selected_page = self._resolve_page(draft, page_id)
            port = model.ports.get(port_id)
            if port is None:
                raise EditorCommandError("Электрический порт не найден.")
            if model.connection_for_port(port_id) is None:
                raise EditorCommandError("Переподключаемый порт ещё не подключён.")
            if any(route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
                   and any(anchor.target_port_id == port_id
                           and anchor.branch_port_id != port_id
                           for anchor in (route.start_anchor, route.end_anchor))
                   for route in draft.diagram.routes.values()):
                raise EditorCommandError(
                    "К выводу привязана физическая линия. Для переноса конца линии "
                    "используйте её конечный маркер; перенос аппарата меняет только геометрию."
                )
            _, normalized_target, _ = self._guard_connection_voltage(model, port_id, target, adopt=True)
            resolved = self._resolve_connection_target(
                draft, normalized_target, selected_page
            )
            replacement, _ = model.reconnect_port(port_id, resolved.node_id)

            updated_routes = dict(draft.diagram.routes)
            affected_route_ids: list[DiagramRouteId] = []
            branch_route_changed = False
            for route_id, route in tuple(updated_routes.items()):
                start_matches = (
                    route.start_anchor.target_port_id == port_id
                    or route.start_anchor.branch_port_id == port_id
                )
                end_matches = (
                    route.end_anchor.target_port_id == port_id
                    or route.end_anchor.branch_port_id == port_id
                )
                if not start_matches and not end_matches:
                    continue
                if route.kind is DiagramRouteKind.NODE_CONNECTION:
                    del updated_routes[route_id]
                    affected_route_ids.append(route_id)
                    continue
                branch_port_id = (
                    port_id
                    if route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
                    else None
                )
                new_anchor = RouteEndpointAnchor(
                    resolved.anchor_kind,
                    resolved.representation_id,
                    resolved.node_id,
                    branch_port_id=branch_port_id,
                    target_port_id=resolved.target_port_id,
                    anchor_key=target_anchor_key or resolved.anchor_key,
                )
                start_anchor = new_anchor if start_matches else route.start_anchor
                end_anchor = new_anchor if end_matches else route.end_anchor
                start_representation = draft.diagram.representations[
                    start_anchor.representation_id
                ]
                end_representation = draft.diagram.representations[
                    end_anchor.representation_id
                ]
                updated = replace(
                    route,
                    start_anchor=start_anchor,
                    end_anchor=end_anchor,
                    waypoints=self._effective_route_waypoints(
                        start_representation,
                        end_representation,
                        route_waypoints,
                    ),
                )
                allocation_draft = ProjectDraft(
                    model, replace(draft.diagram, routes=updated_routes), draft.catalog_snapshots,
                )
                updated_routes[route_id] = self._route_with_available_bus_anchors(
                    allocation_draft, updated,
                    endpoint_indices=tuple(index for index, matches in enumerate(
                        (start_matches, end_matches)) if matches),
                )
                branch_route_changed = True
                affected_route_ids.append(route_id)
            new_route_id: DiagramRouteId | None = None
            if not branch_route_changed:
                source_representation = self._equipment_representation(
                    draft,
                    port.equipment_id,
                    selected_page,
                    source_representation_id,
                )
                target_representation = draft.diagram.representations[
                    resolved.representation_id
                ]
                route = DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.NODE_CONNECTION,
                    RouteEndpointAnchor(
                        RouteAnchorKind.EQUIPMENT_PORT,
                        source_representation.id,
                        resolved.node_id,
                        target_port_id=port_id,
                        anchor_key=source_anchor_key,
                    ),
                    RouteEndpointAnchor(
                        resolved.anchor_kind,
                        resolved.representation_id,
                        resolved.node_id,
                        target_port_id=resolved.target_port_id,
                        anchor_key=target_anchor_key or resolved.anchor_key,
                    ),
                    electrical_node_id=resolved.node_id,
                    waypoints=self._effective_route_waypoints(
                        source_representation,
                        target_representation,
                        route_waypoints,
                    ),
                )
                allocation_draft = ProjectDraft(
                    model, replace(draft.diagram, routes=updated_routes), draft.catalog_snapshots,
                )
                updated_routes[route.id] = self._route_with_available_bus_anchors(
                    allocation_draft, route,
                )
                new_route_id = route.id
            if affected_route_ids or new_route_id is not None:
                draft.diagram = _diagram_with_routes(
                    draft.diagram, updated_routes
                )
            draft.diagram = cleanup_after_deletion(
                self.model, self.diagram, model, draft.diagram,
                affected_page_ids={selected_page},
            ).diagram
            connection_ids = [replacement.id]
            if isinstance(target, PortTarget):
                peer_connection = model.connection_for_port(target.port_id)
                if peer_connection is not None:
                    connection_ids.append(peer_connection.id)
            return ConnectionEditResult(
                resolved.node_id,
                tuple(dict.fromkeys(connection_ids)),
                new_route_id or (
                    affected_route_ids[0] if affected_route_ids else None
                ),
                port.equipment_id,
            )

        return self._execute("Переподключить конец ветви", command)

    def _detach_port_to_new_node(
        self,
        draft: ProjectDraft,
        resolved: _ResolvedTarget,
        peer: _ResolvedTarget,
    ) -> _ResolvedTarget:
        """Вывести порт из общего узла, чтобы вставить между ними линию.

        Решение заказчика 02.09.2026: протяжка от вывода, который уже подключён,
        предлагает тот же выбор «Провод / ВЛ / КЛ». Для ВЛ/КЛ общий узел
        разрывается: вывод переезжает на собственный узел, а линия встаёт между
        новым узлом и прежним. Остальные присоединения прежнего узла не
        затрагиваются; всё это часть одной команды и одного шага отмены.

        Собственная ветвь выделяемого порта следует за портом. Внешние ветви
        остаются на прежнем узле: меняется только их графическая привязка.
        Из веера проводов удаляется лишь заменённая связь с выбранной целью,
        остальные провода сохраняют видимость, ID и ручные точки.
        """
        model = draft.electrical_model
        port_id = resolved.target_port_id
        if port_id is None:
            raise EditorCommandError(
                "Вставить линию можно только в связь, начатую от вывода оборудования."
            )
        if peer.target_port_id == port_id:
            raise EditorCommandError("Нельзя вставить линию от вывода к нему самому.")
        definition = model.port_definition(port_id)
        node = ElectricalNode(
            ElectricalNodeId.new(),
            f"Узел вывода «{definition.display_name}»",
            definition.kind_id,
            model.port_voltage_class(port_id),
            extensions={
                "creation_origin": "automatic_line_insertion",
                "junction_kind": "automatic_endpoint",
            },
        )
        model.add_node(node)
        model.reconnect_port(port_id, node.id)

        def anchor_on_page(target, page_id, branch_port_id, fallback):
            representation = draft.diagram.representations[target.representation_id]
            if representation.page_id == page_id:
                return RouteEndpointAnchor(
                    target.anchor_kind, representation.id, target.node_id,
                    branch_port_id=branch_port_id, target_port_id=target.target_port_id,
                    anchor_key=target.anchor_key,
                )
            # Other sheets may show the same terminal. Reuse the corresponding
            # symbol on that sheet; never point a route into a different page.
            if target.target_port_id is not None:
                equipment_id = model.ports[target.target_port_id].equipment_id
                representation = next((row for row in
                    draft.diagram.representations_for_equipment(equipment_id)
                    if row.page_id == page_id), None)
                if representation is not None:
                    return RouteEndpointAnchor(
                        RouteAnchorKind.EQUIPMENT_PORT, representation.id, target.node_id,
                        branch_port_id=branch_port_id, target_port_id=target.target_port_id,
                        anchor_key=target.anchor_key,
                    )
            representation = self._ensure_node_representation(
                draft, target.node_id, page_id,
                name=model.electrical_nodes[target.node_id].name,
                x=fallback.x + 40.0, y=fallback.y,
            )
            kind = (RouteAnchorKind.BUS if "busbar" in representation.symbol_key
                    else RouteAnchorKind.ELECTRICAL_NODE)
            return RouteEndpointAnchor(kind, representation.id, target.node_id,
                                       branch_port_id=branch_port_id)

        moved = replace(resolved, node_id=node.id)
        routes = dict(draft.diagram.routes)
        for route_id, route in tuple(routes.items()):
            anchors = [route.start_anchor, route.end_anchor]
            changed = False
            for index, anchor in enumerate(anchors):
                if anchor.branch_port_id == port_id:
                    # This is the selected physical branch's own terminal.
                    target = moved
                elif anchor.target_port_id == port_id:
                    # This is another branch merely drawn against the terminal.
                    # Its physical Connection must stay on the original node.
                    target = peer
                else:
                    continue
                representation = draft.diagram.representations[anchor.representation_id]
                fallback = (route.waypoints[0 if index == 0 else -1]
                            if route.waypoints else representation)
                anchors[index] = anchor_on_page(target, route.page_id,
                                                 anchor.branch_port_id, fallback)
                changed = True
            if not changed:
                continue
            if (route.kind is DiagramRouteKind.NODE_CONNECTION
                    and anchors[0].representation_id == anchors[1].representation_id
                    and anchors[0].target_port_id == anchors[1].target_port_id):
                # The selected wire has become a graphical self-link on the
                # old node. The new physical line replaces this link only.
                del routes[route_id]
                continue
            updated = replace(route, start_anchor=anchors[0], end_anchor=anchors[1])
            routes[route_id] = self._reroute_to_current_port_anchors(draft, updated)
        draft.diagram = _diagram_with_routes(draft.diagram, routes)
        return replace(resolved, node_id=node.id)

    @staticmethod
    def _line_catalog_properties(draft, line_kind, physical, entry, remember, voltage_id):
        properties = dict(physical.properties)
        if entry is None:
            if remember:
                raise EditorCommandError("Для сохранения марки нужна запись справочника.")
            return properties
        expected_type = draft.electrical_model._line_section_type_id(LineKind(line_kind))
        if entry.equipment_type_id != expected_type or entry.equipment_type_version != 1:
            raise EditorCommandError("Запись справочника не соответствует выбранному виду линии.")
        if any(value != voltage_id for value in entry.voltage_class_by_group.values()):
            raise EditorCommandError("Класс напряжения записи справочника не соответствует концам линии.")
        catalog = getattr(draft, "user_catalog", None)
        if catalog is None:
            raise EditorCommandError("В проекте отсутствует пользовательский справочник.")
        if remember:
            existing = catalog.entries.get(entry.id)
            if existing is None:
                catalog.add(entry)
            elif existing != entry:
                raise EditorCommandError("Запись справочника уже существует с другими параметрами. Сохраните новую марку.")
        elif catalog.entries.get(entry.id) != entry:
            raise EditorCommandError("Выбранная запись справочника была изменена. Выберите её заново.")
        return {**entry.properties, **properties}

    @staticmethod
    def _bind_line_catalog(draft, equipment_id, physical, entry):
        if entry is None:
            return
        binding = CatalogBinding(equipment_id,
            CatalogEntrySnapshot.from_entry(entry, source_catalog_id=draft.user_catalog.id),
            instance_overrides={key: value for key, value in physical.properties.items()
                if key not in entry.properties or value != entry.properties[key]})
        draft.catalog_snapshots = draft.catalog_snapshots.with_binding(binding)

    def create_physical_line(
        self,
        name: str,
        line_kind: LineKind,
        start_target: ConnectionTarget,
        end_target: ConnectionTarget,
        *,
        physical: PhysicalLineInput,
        page_id: PageId | str | None = None,
        inherited_properties: Mapping[str, Any] | None = None,
        note: str = "",
        feeder_id: FeederId | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        split_shared_node: bool = False,
        catalog_entry: CatalogEntry | None = None,
        remember_catalog_entry: bool = False,
    ) -> PhysicalLineEditResult:
        self._require_edit()
        if not isinstance(physical, PhysicalLineInput):
            raise EditorCommandError("Не заданы физические параметры линии.")

        def command(draft: ProjectDraft) -> PhysicalLineEditResult:
            selected_page = self._resolve_page(draft, page_id)
            normalized_start, normalized_end, voltage_id = self._guard_connection_voltage(
                draft.electrical_model, start_target, end_target, adopt=True,
            )
            properties = self._line_catalog_properties(draft, line_kind, physical,
                catalog_entry, remember_catalog_entry, voltage_id)
            start = self._resolve_connection_target(
                draft, normalized_start, selected_page
            )
            end = self._resolve_connection_target(
                draft,
                normalized_end,
                selected_page,
                fallback_x=120.0,
            )
            if start.node_id == end.node_id:
                if not split_shared_node:
                    raise EditorCommandError(
                        "Физическая линия должна соединять два разных узла."
                    )
                if start.target_port_id is not None:
                    start = self._detach_port_to_new_node(draft, start, end)
                elif end.target_port_id is not None:
                    end = self._detach_port_to_new_node(draft, end, start)
                else:
                    raise EditorCommandError("Вставить линию в общий узел можно только у вывода оборудования.")
            model = draft.electrical_model
            logical_line, section, _ = model.create_logical_line(
                name,
                line_kind,
                start.node_id,
                end.node_id,
                physical.length_mm,
                voltage_class_id=voltage_id,
                inherited_properties=inherited_properties,
                section_properties=properties,
                note=note,
                length_confirmation=physical.length_confirmation,
                impedance_confirmation=physical.impedance_confirmation,
                feeder_id=feeder_id,
            )
            self._bind_line_catalog(draft, section.equipment_id, physical, catalog_entry)
            branch_from_port = model.port_by_role(section.equipment_id, "from").id
            branch_to_port = model.port_by_role(section.equipment_id, "to").id
            start_representation = draft.diagram.representations[
                start.representation_id
            ]
            end_representation = draft.diagram.representations[end.representation_id]
            route = DiagramRoute(
                DiagramRouteId.new(),
                selected_page,
                DiagramRouteKind.EQUIPMENT_BRANCH,
                RouteEndpointAnchor(
                    start.anchor_kind,
                    start.representation_id,
                    start.node_id,
                    branch_port_id=branch_from_port,
                    target_port_id=start.target_port_id,
                    anchor_key=start.anchor_key,
                ),
                RouteEndpointAnchor(
                    end.anchor_kind,
                    end.representation_id,
                    end.node_id,
                    branch_port_id=branch_to_port,
                    target_port_id=end.target_port_id,
                    anchor_key=end.anchor_key,
                ),
                equipment_id=section.equipment_id,
                waypoints=self._effective_route_waypoints(
                    start_representation,
                    end_representation,
                    route_waypoints,
                ),
            )
            self._add_route(draft, route)
            return PhysicalLineEditResult(
                logical_line.id,
                section.equipment_id,
                start.node_id,
                end.node_id,
                route.id,
            )

        return self._execute("Создать физическую линию", command)

    def split_physical_line(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        *,
        page_id: PageId | str | None = None,
        split_x: float | None = None,
        split_y: float | None = None,
        first_route_waypoints: Iterable[RouteWaypoint] | None = None,
        second_route_waypoints: Iterable[RouteWaypoint] | None = None,
    ) -> SplitPhysicalLineEditResult:
        """Разделить физическую ветвь и её маршрут одной undo-командой."""
        self._require_edit()
        if (split_x is None) != (split_y is None):
            raise EditorCommandError(
                "Графическое положение узла разбиения задаётся двумя координатами."
            )

        def command(draft: ProjectDraft) -> SplitPhysicalLineEditResult:
            source_route = self._route_for_equipment(
                draft.diagram,
                section_id,
                self._page_id(page_id) if page_id is not None else None,
            )
            selected_page = (
                source_route.page_id
                if source_route is not None and page_id is None
                else self._resolve_page(draft, page_id)
            )
            start_anchor, end_anchor, start_representation, end_representation = (
                self._section_route_endpoints(
                    draft, section_id, selected_page, source_route
                )
            )
            if split_x is None:
                if source_route is not None:
                    node_x, node_y = self._route_midpoint(source_route)
                else:
                    node_x = (
                        start_representation.x + end_representation.x
                    ) / 2.0
                    node_y = (
                        start_representation.y + end_representation.y
                    ) / 2.0
            else:
                workspace = EditorWorkspaceState.from_diagram(draft.diagram)
                node_x, node_y = self._snap_with_state(
                    _finite(split_x, "Координата X узла разбиения"),
                    _finite(split_y, "Координата Y узла разбиения"),
                    workspace,
                )

            split = draft.electrical_model.split_line_section(
                section_id, offset_mm
            )
            self._split_catalog_binding(
                draft,
                split.removed_section_id,
                (split.first_section_id, split.second_section_id),
            )
            node = draft.electrical_model.electrical_nodes[split.tap_node_id]
            node_representation = self._ensure_node_representation(
                draft,
                node.id,
                selected_page,
                name=node.name,
                x=node_x,
                y=node_y,
                symbol_key="line_tap",
            )
            remaining_routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if route.equipment_id != section_id
            }
            if len(remaining_routes) != len(draft.diagram.routes):
                draft.diagram = _diagram_with_routes(
                    draft.diagram, remaining_routes
                )

            model = draft.electrical_model
            first_from = model.port_by_role(split.first_section_id, "from").id
            first_to = model.port_by_role(split.first_section_id, "to").id
            second_from = model.port_by_role(split.second_section_id, "from").id
            second_to = model.port_by_role(split.second_section_id, "to").id
            split_anchor = RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                node_representation.id,
                split.tap_node_id,
                branch_port_id=first_to,
            )
            routes = (
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(start_anchor, branch_port_id=first_from),
                    split_anchor,
                    equipment_id=split.first_section_id,
                    waypoints=self._effective_route_waypoints(
                        start_representation,
                        node_representation,
                        first_route_waypoints,
                    ),
                ),
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(split_anchor, branch_port_id=second_from),
                    replace(end_anchor, branch_port_id=second_to),
                    equipment_id=split.second_section_id,
                    waypoints=self._effective_route_waypoints(
                        node_representation,
                        end_representation,
                        second_route_waypoints,
                    ),
                ),
            )
            for route in routes:
                self._add_route(draft, route)
            return SplitPhysicalLineEditResult(
                split.logical_line_id,
                split.removed_section_id,
                split.first_section_id,
                split.second_section_id,
                split.tap_node_id,
                (routes[0].id, routes[1].id),
            )

        return self._execute("Разделить физическую линию", command)

    def confirm_line_length(
        self,
        section_id: EquipmentId,
        length_mm: int,
    ) -> ConfirmedLineLengthResult:
        """Записать подтверждённую физическую длину без изменения трассы."""
        self._require_edit()

        def command(draft: ProjectDraft) -> ConfirmedLineLengthResult:
            draft.electrical_model.set_section_length(
                section_id,
                length_mm,
                confirmation=DataConfirmation.CONFIRMED,
            )
            section = draft.electrical_model.line_section_for_equipment(
                section_id
            )
            return ConfirmedLineLengthResult(
                section_id,
                section.length_mm,
                DataConfirmation.CONFIRMED,
            )

        return self._execute("Подтвердить длину линии", command)

    def _split_line_for_tap(self, draft, section_id, offset_mm) -> _TapSplit:
        model = draft.electrical_model
        if section_id in model.line_sections:
            split = model.split_line_section(section_id, offset_mm)
            self._split_catalog_binding(draft, section_id,
                (split.first_section_id, split.second_section_id))
            logical_id = split.logical_line_id
        else:
            # Imported snapshots need an explicit migration policy. Do not
            # silently clone a binding with protection or absolute impedances.
            if section_id in draft.catalog_snapshots.bindings:
                raise EditorCommandError("Отпайка импортированной линии со связью справочника требует отдельного переноса параметров.")
            from .legacy_line_split import split_legacy_line
            split = split_legacy_line(model, section_id, offset_mm)
            logical_id = None
        return _TapSplit(logical_id, section_id, split.first_section_id,
                         split.second_section_id, split.tap_node_id)

    def _tap_route_inputs(self, draft, section_id, page_id, tap_route_id, tap_x, tap_y):
        if tap_route_id is not None:
            source = draft.diagram.routes.get(DiagramRouteId(str(tap_route_id)))
            if (source is None or source.equipment_id != section_id
                    or source.kind is not DiagramRouteKind.EQUIPMENT_BRANCH
                    or (page_id is not None and source.page_id != self._page_id(page_id))):
                raise EditorCommandError("Выбранная графическая трасса отпайки больше не соответствует линии и листу.")
        else:
            source = self._route_for_equipment(draft.diagram, section_id,
                self._page_id(page_id) if page_id is not None else None)
        page = source.page_id if source is not None and page_id is None else self._resolve_page(draft, page_id)
        original_from = draft.electrical_model.port_by_role(section_id, "from").id
        if source is None:
            a, b, first, last = self._section_route_endpoints(draft, section_id, page, None)
            source = DiagramRoute(DiagramRouteId.new(), page, DiagramRouteKind.EQUIPMENT_BRANCH,
                a, b, equipment_id=section_id, waypoints=self._route_waypoints(first, last))
        views = tuple(route for route in draft.diagram.routes.values()
            if route.equipment_id == section_id and route.kind is DiagramRouteKind.EQUIPMENT_BRANCH) or (source,)
        x, y = (self._route_midpoint(source) if tap_x is None else
            (_finite(tap_x, "Координата X отпайки"), _finite(tap_y, "Координата Y отпайки")))
        return source, views, page, original_from, x, y

    def create_tap(
        self, section_id: EquipmentId, offset_mm: int | None,
        branch_name: str, branch_kind: LineKind, branch_target: ConnectionTarget, *,
        physical: PhysicalLineInput, page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        tap_x: float | None = None, tap_y: float | None = None,
        catalog_entry: CatalogEntry | None = None, remember_catalog_entry: bool = False,
        tap_route_id: DiagramRouteId | None = None,
    ) -> TapEditResult:
        """Split every view and add one outgoing physical branch atomically."""
        self._require_edit()
        if not isinstance(physical, PhysicalLineInput):
            raise EditorCommandError("Не заданы физические параметры отпайки.")
        if (tap_x is None) != (tap_y is None):
            raise EditorCommandError("Графическое положение отпайки задаётся двумя координатами.")

        def command(draft: ProjectDraft) -> TapEditResult:
            model = draft.electrical_model
            voltage = self._line_connection_voltage(model, section_id)
            properties = self._line_catalog_properties(draft, branch_kind, physical,
                catalog_entry, remember_catalog_entry, voltage)
            _, normalized_target, _ = self._guard_connection_voltage(
                model, voltage, branch_target, adopt=True)
            source, views, page, original_from, x, y = self._tap_route_inputs(
                draft, section_id, page_id, tap_route_id, tap_x, tap_y)
            branch = self._resolve_connection_target(draft, normalized_target, page,
                fallback_x=x, fallback_y=y + 140.0)
            split = self._split_line_for_tap(draft, section_id, offset_mm)
            branch_line, branch_section, _ = model.create_logical_line(
                branch_name, branch_kind, split.tap_node_id, branch.node_id,
                physical.length_mm, voltage_class_id=voltage,
                section_properties=properties,
                length_confirmation=physical.length_confirmation,
                impedance_confirmation=physical.impedance_confirmation)
            self._bind_line_catalog(draft, branch_section.equipment_id, physical, catalog_entry)
            from .tap_routes import split_physical_route_views
            draft.diagram, tap, route_ids = split_physical_route_views(
                draft.diagram, views, original_from_port=original_from,
                first_equipment_id=split.first_section_id, second_equipment_id=split.second_section_id,
                first_ports=(model.port_by_role(split.first_section_id, "from").id,
                             model.port_by_role(split.first_section_id, "to").id),
                second_ports=(model.port_by_role(split.second_section_id, "from").id,
                              model.port_by_role(split.second_section_id, "to").id),
                tap_node_id=split.tap_node_id, selected_route_id=source.id,
                selected_point=(x, y), midpoint=self._route_midpoint,
                outer_anchor=lambda anchor, point, page: (anchor, None))
            target_representation = draft.diagram.representations[branch.representation_id]
            route = DiagramRoute(DiagramRouteId.new(), page, DiagramRouteKind.EQUIPMENT_BRANCH,
                RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, tap.id, split.tap_node_id,
                    branch_port_id=model.port_by_role(branch_section.equipment_id, "from").id),
                RouteEndpointAnchor(branch.anchor_kind, branch.representation_id, branch.node_id,
                    branch_port_id=model.port_by_role(branch_section.equipment_id, "to").id,
                    target_port_id=branch.target_port_id, anchor_key=branch.anchor_key),
                equipment_id=branch_section.equipment_id,
                waypoints=self._effective_route_waypoints(tap, target_representation, route_waypoints))
            self._add_route(draft, route)
            return TapEditResult(split.logical_line_id, branch_line.id, split.removed_section_id,
                split.first_section_id, split.second_section_id, branch_section.equipment_id,
                split.tap_node_id, (*route_ids, route.id))

        return self._execute("Создать отпайку", command)


    def attach_equipment_to_line(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        type_id: EquipmentTypeId | str,
        name: str,
        *,
        terminal_role: str,
        type_version: int = 1,
        properties: Mapping[str, Any] | None = None,
        voltage_class_by_group: Mapping[str, VoltageClassId] | None = None,
        normal_position: SwitchPosition | None = None,
        note: str = "",
        page_id: PageId | str | None = None,
        tap_x: float | None = None,
        tap_y: float | None = None,
        equipment_x: float | None = None,
        equipment_y: float | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        rotation_deg: int | float | QuarterTurn | None = None,
        orientation_mode: OrientationMode | str = OrientationMode.MANUAL,
        manual_placement_confirmed: bool = False,
    ) -> BranchAttachmentEditResult:
        """Создать оборудование и присоединить его к линии одной отпайкой.

        Оборудование не вставляется последовательно: выбранный терминал
        подключается к новому узлу отпайки эквипотенциальной графической
        трассой. Для многопортового оборудования роль терминала задаётся
        явно, поэтому команда не угадывает ВН/СН/НН по имени или SVG.
        """
        self._require_edit()
        if not isinstance(terminal_role, str) or not terminal_role.strip():
            raise EditorCommandError("Нужно явно выбрать электрический терминал.")
        if (tap_x is None) != (tap_y is None):
            raise EditorCommandError(
                "Графическое положение отпайки задаётся двумя координатами."
            )
        if (equipment_x is None) != (equipment_y is None):
            raise EditorCommandError(
                "Графическое положение оборудования задаётся двумя координатами."
            )

        normalized_type_id = (
            type_id if isinstance(type_id, EquipmentTypeId) else EquipmentTypeId(type_id)
        )

        def command(draft: ProjectDraft) -> BranchAttachmentEditResult:
            model = draft.electrical_model
            definition = model.equipment_type(normalized_type_id, type_version)
            registry = EquipmentPlacementRegistry.for_model(
                model.equipment_types.values()
            )
            placement = registry.resolve_definition(definition)
            if placement is not EquipmentPlacementKind.BRANCH_ATTACHMENT and not (
                placement is EquipmentPlacementKind.MANUAL_SELECTION
                and manual_placement_confirmed
            ):
                raise EditorCommandError(
                    f"Оборудование «{definition.display_name}» не предназначено "
                    "для подключения ответвлением."
                )
            port_definition = next(
                (
                    item
                    for item in definition.port_definitions
                    if item.role == terminal_role
                ),
                None,
            )
            if port_definition is None:
                raise EditorCommandError(
                    f"У типа «{definition.display_name}» нет терминала "
                    "с указанным назначением."
                )

            source_line = model.logical_line_for_section(section_id)
            self._line_connection_voltage(model, section_id)
            source_route = self._route_for_equipment(
                draft.diagram,
                section_id,
                self._page_id(page_id) if page_id is not None else None,
            )
            selected_page = (
                source_route.page_id
                if source_route is not None and page_id is None
                else self._resolve_page(draft, page_id)
            )
            start_anchor, end_anchor, start_representation, end_representation = (
                self._section_route_endpoints(
                    draft, section_id, selected_page, source_route
                )
            )
            if tap_x is None:
                if source_route is not None:
                    junction_x, junction_y = self._route_midpoint(source_route)
                else:
                    junction_x = (
                        start_representation.x + end_representation.x
                    ) / 2.0
                    junction_y = (
                        start_representation.y + end_representation.y
                    ) / 2.0
            else:
                workspace = EditorWorkspaceState.from_diagram(draft.diagram)
                junction_x, junction_y = self._snap_with_state(
                    _finite(tap_x, "Координата X отпайки"),
                    _finite(tap_y, "Координата Y отпайки"),
                    workspace,
                )
            if equipment_x is None:
                object_x, object_y = junction_x, junction_y + 140.0
            else:
                workspace = EditorWorkspaceState.from_diagram(draft.diagram)
                object_x, object_y = self._snap_with_state(
                    _finite(equipment_x, "Координата X оборудования"),
                    _finite(equipment_y, "Координата Y оборудования"),
                    workspace,
                )
            try:
                normalized_orientation_mode = normalize_orientation_mode(
                    orientation_mode
                )
                if rotation_deg is None:
                    effective_rotation = (
                        quarter_turn_for_port_toward_point(
                            definition,
                            terminal_role,
                            equipment_x=object_x,
                            equipment_y=object_y,
                            target_x=junction_x,
                            target_y=junction_y,
                        )
                        if normalized_orientation_mode is OrientationMode.AUTO
                        else QuarterTurn.DEG_0
                    )
                else:
                    effective_rotation = normalize_quarter_turn(rotation_deg)
            except (KeyError, ValueError) as exc:
                raise EditorCommandError(str(exc)) from exc

            voltage_groups = dict(voltage_class_by_group or {})
            group = port_definition.voltage_group
            if group is not None and source_line.voltage_class_id is not None:
                selected_voltage = voltage_groups.get(group)
                if (
                    selected_voltage is not None
                    and selected_voltage != source_line.voltage_class_id
                ):
                    raise EditorCommandError(
                        "Класс напряжения выбранного терминала не совпадает с линией."
                    )
                voltage_groups[group] = source_line.voltage_class_id
            effective_position = normal_position
            if (
                effective_position is None
                and "switch.position" in definition.capabilities
            ):
                effective_position = SwitchPosition(
                    definition.extensions.get(
                        "default_normal_position", SwitchPosition.CLOSED.value
                    )
                )
            placement_extensions: dict[str, Any] = {
                "placement_origin": "automatic_branch_attachment",
            }
            if len(definition.port_definitions) > 1:
                placement_extensions[EDITOR_DRAFT_EXTENSION_KEY] = {
                    "draft": True
                }
            equipment, _ = model.create_equipment(
                definition.id,
                name,
                type_version=definition.schema_version,
                properties=properties or {},
                voltage_class_by_group=voltage_groups,
                normal_position=effective_position,
                note=note,
                extensions=placement_extensions,
            )
            selected_port = model.port_by_role(equipment.id, terminal_role)
            self._guard_connection_voltage(model, selected_port.id, self._line_connection_voltage(model, section_id))
            split = model.split_line_section(section_id, offset_mm)
            self._split_catalog_binding(
                draft,
                split.removed_section_id,
                (split.first_section_id, split.second_section_id),
            )
            model.connect_port(selected_port.id, split.tap_node_id)

            tap_node = model.electrical_nodes[split.tap_node_id]
            tap_representation = self._ensure_node_representation(
                draft,
                tap_node.id,
                selected_page,
                name=tap_node.name,
                x=junction_x,
                y=junction_y,
                symbol_key="line_tap",
            )
            equipment_representation = self._ensure_equipment_representation(
                draft,
                equipment.id,
                selected_page,
                x=object_x,
                y=object_y,
                rotation_deg=effective_rotation,
                orientation_mode=normalized_orientation_mode,
            )
            remaining_routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if route.equipment_id != section_id
            }
            if len(remaining_routes) != len(draft.diagram.routes):
                draft.diagram = _diagram_with_routes(
                    draft.diagram, remaining_routes
                )

            first_from = model.port_by_role(split.first_section_id, "from").id
            first_to = model.port_by_role(split.first_section_id, "to").id
            second_from = model.port_by_role(split.second_section_id, "from").id
            second_to = model.port_by_role(split.second_section_id, "to").id
            tap_first = RouteEndpointAnchor(
                RouteAnchorKind.ELECTRICAL_NODE,
                tap_representation.id,
                split.tap_node_id,
                branch_port_id=first_to,
            )
            routes = (
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(start_anchor, branch_port_id=first_from),
                    tap_first,
                    equipment_id=split.first_section_id,
                    waypoints=self._route_waypoints(
                        start_representation, tap_representation
                    ),
                ),
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(tap_first, branch_port_id=second_from),
                    replace(end_anchor, branch_port_id=second_to),
                    equipment_id=split.second_section_id,
                    waypoints=self._route_waypoints(
                        tap_representation, end_representation
                    ),
                ),
                self._reroute_to_current_port_anchors(
                    draft,
                    DiagramRoute(
                        DiagramRouteId.new(),
                        selected_page,
                        DiagramRouteKind.NODE_CONNECTION,
                        RouteEndpointAnchor(
                            RouteAnchorKind.EQUIPMENT_PORT,
                            equipment_representation.id,
                            split.tap_node_id,
                            target_port_id=selected_port.id,
                            anchor_key=terminal_role,
                        ),
                        RouteEndpointAnchor(
                            RouteAnchorKind.ELECTRICAL_NODE,
                            tap_representation.id,
                            split.tap_node_id,
                        ),
                        electrical_node_id=split.tap_node_id,
                        waypoints=self._effective_route_waypoints(
                            equipment_representation,
                            tap_representation,
                            route_waypoints,
                        ),
                    ),
                ),
            )
            for route in routes:
                self._add_route(draft, route)
            return BranchAttachmentEditResult(
                split.logical_line_id,
                split.removed_section_id,
                split.first_section_id,
                split.second_section_id,
                split.tap_node_id,
                equipment.id,
                selected_port.id,
                equipment_representation.id,
                tuple(item.id for item in routes),
            )

        return self._execute("Подключить оборудование ответвлением", command)

    def reconnect_port_to_tap(
        self, port_id: PortId, section_id: EquipmentId, offset_mm: int | None, *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        source_representation_id: GraphicalRepresentationId | None = None,
        source_anchor_key: str = "", tap_x: float | None = None, tap_y: float | None = None,
        tap_route_id: DiagramRouteId | None = None,
    ) -> LineTapConnectionEditResult:
        """Connect a free port or reconnect an occupied one with a plain wire."""
        return self._wire_to_tap(PortTarget(port_id, source_representation_id, source_anchor_key),
            section_id, offset_mm, page_id=page_id, route_waypoints=route_waypoints,
            tap_x=tap_x, tap_y=tap_y, tap_route_id=tap_route_id)

    def connect_node_to_tap(
        self, source: NodeTarget, section_id: EquipmentId, offset_mm: int | None, *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
        tap_x: float | None = None, tap_y: float | None = None,
        tap_route_id: DiagramRouteId | None = None,
    ) -> LineTapConnectionEditResult:
        """Split and connect an existing bus/conductor node in one transaction."""
        if not isinstance(source, NodeTarget):
            raise EditorCommandError("Начало провода должно представлять электрический узел.")
        return self._wire_to_tap(source, section_id, offset_mm, page_id=page_id,
            route_waypoints=route_waypoints, tap_x=tap_x, tap_y=tap_y, tap_route_id=tap_route_id)

    def _wire_to_tap(
        self, source: PortTarget | NodeTarget, section_id: EquipmentId,
        offset_mm: int | None, *, page_id=None, route_waypoints=None, tap_x=None, tap_y=None,
        tap_route_id=None,
    ) -> LineTapConnectionEditResult:
        self._require_edit()
        if (tap_x is None) != (tap_y is None):
            raise EditorCommandError("Графическое положение отпайки задаётся двумя координатами.")

        def command(draft: ProjectDraft) -> LineTapConnectionEditResult:
            model = draft.electrical_model
            port = model.ports.get(source.port_id) if isinstance(source, PortTarget) else None
            if isinstance(source, PortTarget):
                if port is None:
                    raise EditorCommandError("Электрический порт не найден.")
                if port.equipment_id == section_id:
                    raise EditorCommandError("Нельзя присоединить разрезаемый участок к самому себе.")
                if port.equipment_id in model.line_sections:
                    raise EditorCommandError("Конец другой физической линии подключается отдельной командой объединения ветвей.")
            self._guard_connection_voltage(model, source,
                self._line_connection_voltage(model, section_id), adopt=True)
            source_route, views, selected_page, original_from, junction_x, junction_y = self._tap_route_inputs(
                draft, section_id, page_id, tap_route_id, tap_x, tap_y)
            if isinstance(source, NodeTarget):
                resolved = self._resolve_connection_target(draft, source, selected_page)
                source_representation = draft.diagram.representations[resolved.representation_id]
            else:
                source_representation = self._equipment_representation(draft, port.equipment_id,
                    selected_page, source.representation_id)

            split = self._split_line_for_tap(draft, section_id, offset_mm)
            connection_id = None
            if isinstance(source, NodeTarget):
                # Keep the user's existing node and all its prior connections.
                # The domain guard rejects an attempted shunt of a line half.
                model.merge_nodes(resolved.node_id, split.tap_node_id)
                # The review marker records current split nodes, unlike its
                # immutable original source/zone fields. Remap only this tap.
                from .legacy_line_split import REVIEW_MARKER
                for equipment in tuple(model.equipment.values()):
                    review = equipment.extensions.get(REVIEW_MARKER)
                    if isinstance(review, Mapping) and split.tap_node_id.value in review.get("split_node_ids", ()):
                        marker = {**review, "split_node_ids": [
                            resolved.node_id.value if key == split.tap_node_id.value else key
                            for key in review["split_node_ids"]]}
                        model._equipment[equipment.id] = replace(equipment,
                            extensions={**equipment.extensions, REVIEW_MARKER: marker})
                split = replace(split, tap_node_id=resolved.node_id)
                source_anchor = RouteEndpointAnchor(resolved.anchor_kind,
                    resolved.representation_id, split.tap_node_id,
                    target_port_id=resolved.target_port_id, anchor_key=resolved.anchor_key)
            else:
                existing = model.connection_for_port(source.port_id)
                if existing is None:
                    connection, _ = model.connect_port(source.port_id, split.tap_node_id)
                else:
                    connection, _ = model.reconnect_port(source.port_id, split.tap_node_id)
                connection_id = connection.id
                source_anchor = RouteEndpointAnchor(RouteAnchorKind.EQUIPMENT_PORT,
                    source_representation.id, split.tap_node_id, target_port_id=source.port_id,
                    anchor_key=source.anchor_key)
                # Only the explicitly moved terminal's old zero-impedance
                # leads disappear. Other terminals and other sheets survive.
                draft.diagram = _diagram_with_routes(draft.diagram, {
                    key: route for key, route in draft.diagram.routes.items()
                    if not (route.kind is DiagramRouteKind.NODE_CONNECTION
                        and any(anchor.target_port_id == source.port_id
                                for anchor in (route.start_anchor, route.end_anchor)))})

            detached = {}
            def outer_anchor(anchor, point, page):
                target_connection = (model.connection_for_port(anchor.target_port_id)
                                     if anchor.target_port_id is not None else None)
                if anchor.target_port_id is None or (target_connection is not None
                        and target_connection.electrical_node_id == anchor.electrical_node_id):
                    return anchor, None
                key = (page, anchor.electrical_node_id, anchor.target_port_id)
                if key not in detached:
                    detached[key] = GraphicalRepresentation(GraphicalRepresentationId.new(), page,
                        RepresentationTargetKind.ELECTRICAL_NODE, electrical_node_id=anchor.electrical_node_id,
                        x=point.x, y=point.y, symbol_key="connection_point",
                        extensions={"graphics": {"width": 8.0, "height": 8.0, "label_visible": False}})
                row = detached[key]
                return RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, row.id,
                    anchor.electrical_node_id, branch_port_id=anchor.branch_port_id), row

            from .tap_routes import split_physical_route_views
            draft.diagram, tap_representation, route_ids = split_physical_route_views(
                draft.diagram, views, original_from_port=original_from,
                first_equipment_id=split.first_section_id, second_equipment_id=split.second_section_id,
                first_ports=(model.port_by_role(split.first_section_id, "from").id,
                             model.port_by_role(split.first_section_id, "to").id),
                second_ports=(model.port_by_role(split.second_section_id, "from").id,
                              model.port_by_role(split.second_section_id, "to").id),
                tap_node_id=split.tap_node_id, selected_route_id=source_route.id,
                selected_point=(junction_x, junction_y), midpoint=self._route_midpoint,
                outer_anchor=outer_anchor)
            # Other branches may share the explicitly moved apparatus port.
            # Repair every old graphical anchor into the same local fallback.
            repaired = {}
            extra_representations = {}
            for route in draft.diagram.routes.values():
                start, extra = outer_anchor(route.start_anchor, route.waypoints[0], route.page_id)
                if extra is not None:
                    extra_representations[extra.id] = extra
                end, extra = outer_anchor(route.end_anchor, route.waypoints[-1], route.page_id)
                if extra is not None:
                    extra_representations[extra.id] = extra
                if start != route.start_anchor or end != route.end_anchor:
                    repaired[route.id] = replace(route, start_anchor=start, end_anchor=end)
            if repaired:
                draft.diagram = replace(draft.diagram,
                    routes={**draft.diagram.routes, **repaired},
                    representations={**draft.diagram.representations, **extra_representations})
            lead = DiagramRoute(DiagramRouteId.new(), selected_page, DiagramRouteKind.NODE_CONNECTION,
                source_anchor, RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE,
                    tap_representation.id, split.tap_node_id), electrical_node_id=split.tap_node_id,
                waypoints=self._effective_route_waypoints(source_representation, tap_representation, route_waypoints))
            # Old tails are withdrawn by the same atomic command below. Their
            # temporary, still-coincident position must not block the new lead.
            # separate_detached_terminals checks the complete final candidates.
            detached_ids = {row.id for row in detached.values()}
            pending_detached = tuple(route.id for route in draft.diagram.routes.values()
                if detached_ids & {route.start_anchor.representation_id,route.end_anchor.representation_id})
            from .equipment_attachment import occupied_segments as page_segments
            if route_waypoints is None:
                lead = self._reroute_to_current_port_anchors(draft, lead,
                    obstacles=self._page_routing_obstacles(draft, selected_page),
                    occupied_segments=page_segments(draft.diagram,selected_page,pending_detached))
            self._add_route(draft, lead)
            if detached:
                from .tap_routes import separate_detached_terminals
                draft.diagram = separate_detached_terminals(draft.diagram, detached.values(),
                    obstacles_for_page=lambda page: self._page_routing_obstacles(draft, page),
                    reroute=lambda route, obstacles, preserve_direction: self._reroute_to_current_port_anchors(draft, route,
                        obstacles=obstacles,
                        occupied_segments=page_segments(draft.diagram,route.page_id,pending_detached),
                        preserve_node_direction_ids=tuple(
                            anchor.representation_id for anchor in (route.start_anchor, route.end_anchor)
                            if preserve_direction and anchor.kind is RouteAnchorKind.ELECTRICAL_NODE
                            and anchor.representation_id not in detached_ids)))
            return LineTapConnectionEditResult(split.logical_line_id, split.removed_section_id,
                split.first_section_id, split.second_section_id, split.tap_node_id,
                connection_id, (*route_ids, lead.id))

        return self._execute("Подключить провод к отпайке", command)

    def insert_series_equipment(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        type_id: EquipmentTypeId | str,
        name: str,
        *,
        type_version: int = 1,
        terminal_roles: tuple[str, str] | None = None,
        properties: Mapping[str, Any] | None = None,
        voltage_class_by_group: Mapping[str, VoltageClassId] | None = None,
        normal_position: SwitchPosition | None = None,
        note: str = "",
        page_id: PageId | str | None = None,
        x: float | None = None,
        y: float | None = None,
        rotation_deg: float | None = None,
        orientation_mode: OrientationMode | str = OrientationMode.AUTO,
        left_route_waypoints: Iterable[RouteWaypoint] | None = None,
        right_route_waypoints: Iterable[RouteWaypoint] | None = None,
        manual_placement_confirmed: bool = False,
    ) -> SeriesEquipmentEditResult:
        """Вставить зарегистрированный двухполюсный аппарат в линию.

        Электрическая операция выполняется доменной командой; контроллер
        только добавляет представление и две трассы результата в ту же
        пользовательскую транзакцию истории.
        """
        self._require_edit()
        if (x is None) != (y is None):
            raise EditorCommandError(
                "Графическое положение аппарата задаётся двумя координатами."
            )
        normalized_type_id = (
            type_id if isinstance(type_id, EquipmentTypeId) else EquipmentTypeId(type_id)
        )
        try:
            explicit_rotation = (
                None
                if rotation_deg is None
                else normalize_quarter_turn(rotation_deg)
            )
            normalized_orientation_mode = normalize_orientation_mode(
                orientation_mode
            )
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc

        def command(draft: ProjectDraft) -> SeriesEquipmentEditResult:
            model = draft.electrical_model
            self._line_connection_voltage(model, section_id)
            definition = model.equipment_type(normalized_type_id, type_version)
            placement = EquipmentPlacementRegistry.for_model(
                model.equipment_types.values()
            ).resolve_definition(definition)
            if placement is not EquipmentPlacementKind.INLINE_SERIES and not (
                placement is EquipmentPlacementKind.MANUAL_SELECTION
                and manual_placement_confirmed
            ):
                raise EditorCommandError(
                    f"Оборудование «{definition.display_name}» не предназначено "
                    "для последовательной вставки."
                )
            declared_roles = tuple(
                item.role for item in definition.port_definitions
            )
            selected_roles = terminal_roles or declared_roles
            if (
                len(declared_roles) != 2
                or len(selected_roles) != 2
                or selected_roles[0] == selected_roles[1]
                or set(selected_roles) != set(declared_roles)
            ):
                raise EditorCommandError(
                    "Для последовательной вставки нужны два явно определённых "
                    "электрических терминала."
                )

            source_route = self._route_for_equipment(
                draft.diagram,
                section_id,
                self._page_id(page_id) if page_id is not None else None,
            )
            selected_page = (
                source_route.page_id
                if source_route is not None and page_id is None
                else self._resolve_page(draft, page_id)
            )
            start_anchor, end_anchor, start_representation, end_representation = (
                self._section_route_endpoints(
                    draft, section_id, selected_page, source_route
                )
            )
            if x is None:
                if source_route is not None:
                    device_x, device_y = self._route_midpoint(source_route)
                else:
                    device_x = (
                        start_representation.x + end_representation.x
                    ) / 2.0
                    device_y = (
                        start_representation.y + end_representation.y
                    ) / 2.0
            else:
                workspace = EditorWorkspaceState.from_diagram(draft.diagram)
                device_x, device_y = self._snap_with_state(
                    _finite(x, "Координата X аппарата"),
                    _finite(y, "Координата Y аппарата"),
                    workspace,
                )
            effective_rotation = (
                explicit_rotation
                if explicit_rotation is not None
                else self._route_quarter_turn_at(
                    source_route, device_x, device_y
                )
            )

            try:
                domain_kwargs: dict[str, Any] = {}
                if manual_placement_confirmed:
                    domain_kwargs["manual_placement_confirmed"] = True
                inserted = model.insert_series_equipment_in_line(
                    section_id,
                    offset_mm,
                    definition.id,
                    name,
                    type_version=definition.schema_version,
                    terminal_roles=tuple(selected_roles),
                    properties=properties or {},
                    voltage_class_by_group=voltage_class_by_group or {},
                    normal_position=normal_position,
                    note=note,
                    **domain_kwargs,
                )
            except DomainInvariantError as exc:
                raise EditorCommandError(str(exc)) from exc
            self._split_catalog_binding(
                draft,
                inserted.removed_section_id,
                (inserted.left_section_id, inserted.right_section_id),
            )
            representation = self._ensure_equipment_representation(
                draft,
                inserted.equipment_id,
                selected_page,
                x=device_x,
                y=device_y,
                rotation_deg=effective_rotation,
                orientation_mode=normalized_orientation_mode,
            )
            route_values = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if route.equipment_id != section_id
            }
            if len(route_values) != len(draft.diagram.routes):
                draft.diagram = _diagram_with_routes(draft.diagram, route_values)

            left_from = model.port_by_role(inserted.left_section_id, "from").id
            left_to = model.port_by_role(inserted.left_section_id, "to").id
            right_from = model.port_by_role(inserted.right_section_id, "from").id
            right_to = model.port_by_role(inserted.right_section_id, "to").id
            left_terminal = model.port_by_role(
                inserted.equipment_id, inserted.terminal_roles[0]
            ).id
            right_terminal = model.port_by_role(
                inserted.equipment_id, inserted.terminal_roles[1]
            ).id
            raw_routes = (
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(start_anchor, branch_port_id=left_from),
                    RouteEndpointAnchor(
                        RouteAnchorKind.EQUIPMENT_PORT,
                        representation.id,
                        inserted.left_node_id,
                        branch_port_id=left_to,
                        target_port_id=left_terminal,
                        anchor_key=inserted.terminal_roles[0],
                    ),
                    equipment_id=inserted.left_section_id,
                    waypoints=self._effective_route_waypoints(
                        start_representation,
                        representation,
                        left_route_waypoints,
                    ),
                ),
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    RouteEndpointAnchor(
                        RouteAnchorKind.EQUIPMENT_PORT,
                        representation.id,
                        inserted.right_node_id,
                        branch_port_id=right_from,
                        target_port_id=right_terminal,
                        anchor_key=inserted.terminal_roles[1],
                    ),
                    replace(end_anchor, branch_port_id=right_to),
                    equipment_id=inserted.right_section_id,
                    waypoints=self._effective_route_waypoints(
                        representation,
                        end_representation,
                        right_route_waypoints,
                    ),
                ),
            )
            routes = tuple(
                self._reroute_to_current_port_anchors(draft, route)
                for route in raw_routes
            )
            for route in routes:
                self._add_route(draft, route)
            return SeriesEquipmentEditResult(
                inserted.feeder_id,
                inserted.left_logical_line_id,
                inserted.right_logical_line_id,
                inserted.removed_section_id,
                inserted.left_section_id,
                inserted.right_section_id,
                inserted.left_node_id,
                inserted.right_node_id,
                inserted.equipment_id,
                inserted.terminal_roles,
                representation.id,
                tuple(item.id for item in routes),
            )

        return self._execute("Вставить аппарат в линию", command)

    def insert_recloser(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        name: str,
        *,
        properties: Mapping[str, Any] | None = None,
        page_id: PageId | str | None = None,
        normal_position: SwitchPosition = SwitchPosition.CLOSED,
        note: str = "",
        x: float | None = None,
        y: float | None = None,
        rotation_deg: float | None = None,
        orientation_mode: OrientationMode | str = OrientationMode.AUTO,
        left_route_waypoints: Iterable[RouteWaypoint] | None = None,
        right_route_waypoints: Iterable[RouteWaypoint] | None = None,
    ) -> RecloserEditResult:
        """Insert one real two-port recloser into a physical line atomically."""
        self._require_edit()
        if (x is None) != (y is None):
            raise EditorCommandError(
                "Графическое положение реклоузера задаётся двумя координатами."
            )
        try:
            explicit_rotation = (
                None
                if rotation_deg is None
                else normalize_quarter_turn(rotation_deg)
            )
            normalized_orientation_mode = normalize_orientation_mode(
                orientation_mode
            )
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc

        def command(draft: ProjectDraft) -> RecloserEditResult:
            self._line_connection_voltage(draft.electrical_model, section_id)
            source_route = self._route_for_equipment(
                draft.diagram,
                section_id,
                self._page_id(page_id) if page_id is not None else None,
            )
            selected_page = (
                source_route.page_id
                if source_route is not None and page_id is None
                else self._resolve_page(draft, page_id)
            )
            start_anchor, end_anchor, start_representation, end_representation = (
                self._section_route_endpoints(
                    draft, section_id, selected_page, source_route
                )
            )
            if x is None:
                if source_route is not None:
                    device_x, device_y = self._route_midpoint(source_route)
                else:
                    device_x = (start_representation.x + end_representation.x) / 2.0
                    device_y = (start_representation.y + end_representation.y) / 2.0
            else:
                workspace = EditorWorkspaceState.from_diagram(draft.diagram)
                device_x, device_y = self._snap_with_state(
                    _finite(x, "Координата X реклоузера"),
                    _finite(y, "Координата Y реклоузера"),
                    workspace,
                )
            effective_rotation = (
                explicit_rotation
                if explicit_rotation is not None
                else self._route_quarter_turn_at(
                    source_route, device_x, device_y
                )
            )

            source_line = draft.electrical_model.logical_line_for_section(section_id)
            if source_line.voltage_class_id is None:
                raise EditorCommandError(
                    "Перед установкой реклоузера укажите класс напряжения линии."
                )
            voltage = draft.electrical_model.voltage_classes[
                source_line.voltage_class_id
            ]
            effective_properties = dict(properties or {})
            effective_properties.setdefault("rated_current_a", 630.0)
            effective_properties.setdefault(
                "rated_voltage_v", voltage.nominal_voltage_v
            )
            inserted = draft.electrical_model.insert_recloser_in_line(
                section_id,
                offset_mm,
                name,
                properties=effective_properties,
                normal_position=normal_position,
                note=note,
            )
            self._split_catalog_binding(
                draft,
                inserted.removed_section_id,
                (inserted.left_section_id, inserted.right_section_id),
            )
            recloser_representation = self._ensure_equipment_representation(
                draft,
                inserted.recloser_id,
                selected_page,
                x=device_x,
                y=device_y,
                rotation_deg=effective_rotation,
                orientation_mode=normalized_orientation_mode,
            )
            route_values = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if route.equipment_id != section_id
            }
            if len(route_values) != len(draft.diagram.routes):
                draft.diagram = _diagram_with_routes(draft.diagram, route_values)

            model = draft.electrical_model
            left_from = model.port_by_role(inserted.left_section_id, "from").id
            left_to = model.port_by_role(inserted.left_section_id, "to").id
            right_from = model.port_by_role(inserted.right_section_id, "from").id
            right_to = model.port_by_role(inserted.right_section_id, "to").id
            recloser_a = model.port_by_role(inserted.recloser_id, "a").id
            recloser_b = model.port_by_role(inserted.recloser_id, "b").id
            raw_routes = (
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(start_anchor, branch_port_id=left_from),
                    RouteEndpointAnchor(
                        RouteAnchorKind.EQUIPMENT_PORT,
                        recloser_representation.id,
                        inserted.left_node_id,
                        branch_port_id=left_to,
                        target_port_id=recloser_a,
                        anchor_key="a",
                    ),
                    equipment_id=inserted.left_section_id,
                    waypoints=self._effective_route_waypoints(
                        start_representation,
                        recloser_representation,
                        left_route_waypoints,
                    ),
                ),
                DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    RouteEndpointAnchor(
                        RouteAnchorKind.EQUIPMENT_PORT,
                        recloser_representation.id,
                        inserted.right_node_id,
                        branch_port_id=right_from,
                        target_port_id=recloser_b,
                        anchor_key="b",
                    ),
                    replace(end_anchor, branch_port_id=right_to),
                    equipment_id=inserted.right_section_id,
                    waypoints=self._effective_route_waypoints(
                        recloser_representation,
                        end_representation,
                        right_route_waypoints,
                    ),
                ),
            )
            routes = tuple(
                self._reroute_to_current_port_anchors(draft, route)
                for route in raw_routes
            )
            for route in routes:
                self._add_route(draft, route)
            return RecloserEditResult(
                inserted.feeder_id,
                inserted.left_logical_line_id,
                inserted.right_logical_line_id,
                inserted.removed_section_id,
                inserted.left_section_id,
                inserted.right_section_id,
                inserted.left_node_id,
                inserted.right_node_id,
                inserted.recloser_id,
                recloser_representation.id,
                tuple(item.id for item in routes),
            )

        return self._execute("Вставить реклоузер", command)

    def remove_series_equipment_from_line(
        self,
        equipment_id: EquipmentId,
        *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
    ) -> RemoveSeriesEquipmentEditResult:
        """Удалить последовательный аппарат и безопасно восстановить линию."""
        self._require_edit()

        def command(draft: ProjectDraft) -> RemoveSeriesEquipmentEditResult:
            model = draft.electrical_model
            equipment = model.equipment.get(equipment_id)
            if equipment is None:
                raise EditorCommandError("Последовательный аппарат не найден.")
            insertion = equipment.extensions.get("line_insertion")
            if not isinstance(insertion, Mapping):
                raise EditorCommandError(
                    "Аппарат не является безопасной последовательной вставкой."
                )
            raw_roles = insertion.get("terminal_roles")
            if not isinstance(raw_roles, (tuple, list)) or len(raw_roles) != 2:
                raise EditorCommandError(
                    "Данные терминалов последовательного аппарата повреждены."
                )
            terminal_roles = (str(raw_roles[0]), str(raw_roles[1]))
            try:
                left_line_id = LogicalLineId(
                    str(insertion["left_logical_line_id"])
                )
                right_line_id = LogicalLineId(
                    str(insertion["right_logical_line_id"])
                )
                left_section_id = model.logical_lines[
                    left_line_id
                ].section_equipment_ids[-1]
                right_section_id = model.logical_lines[
                    right_line_id
                ].section_equipment_ids[0]
            except (KeyError, TypeError, ValueError) as exc:
                raise EditorCommandError(
                    "Данные вставки аппарата повреждены."
                ) from exc
            requested_page = (
                self._page_id(page_id) if page_id is not None else None
            )
            left_route = self._route_for_equipment(
                draft.diagram, left_section_id, requested_page
            )
            right_route = self._route_for_equipment(
                draft.diagram, right_section_id, requested_page
            )
            if left_route is None or right_route is None:
                raise EditorCommandError(
                    "Графические трассы по сторонам аппарата не найдены."
                )
            if left_route.page_id != right_route.page_id:
                raise EditorCommandError(
                    "Части линии аппарата находятся на разных страницах."
                )
            selected_page = left_route.page_id
            left_node_id = model.node_for_port(
                model.port_by_role(equipment_id, terminal_roles[0]).id
            ).id
            right_node_id = model.node_for_port(
                model.port_by_role(equipment_id, terminal_roles[1]).id
            ).id
            left_start, left_end, left_start_rep, left_end_rep = (
                self._section_route_endpoints(
                    draft, left_section_id, selected_page, left_route
                )
            )
            if left_start.electrical_node_id == left_node_id:
                left_start, left_end = left_end, left_start
                left_start_rep, left_end_rep = left_end_rep, left_start_rep
            right_start, right_end, right_start_rep, right_end_rep = (
                self._section_route_endpoints(
                    draft, right_section_id, selected_page, right_route
                )
            )
            if right_end.electrical_node_id == right_node_id:
                right_start, right_end = right_end, right_start
                right_start_rep, right_end_rep = right_end_rep, right_start_rep

            merged_section_id = EquipmentId.new()
            self._merge_catalog_bindings(
                draft,
                (left_section_id, right_section_id),
                merged_section_id,
            )
            try:
                result = model.remove_series_equipment_from_line(
                    equipment_id,
                    terminal_roles=terminal_roles,
                    merged_section_id=merged_section_id,
                )
            except DomainInvariantError as exc:
                raise EditorCommandError(str(exc)) from exc
            draft.catalog_snapshots = (
                draft.catalog_snapshots.without_equipment(equipment_id)
            )
            representations = {
                representation_id: representation
                for representation_id, representation
                in draft.diagram.representations.items()
                if (
                    representation.equipment_id is None
                    or representation.equipment_id in model.equipment
                )
                and (
                    representation.electrical_node_id is None
                    or representation.electrical_node_id in model.electrical_nodes
                )
            }
            routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if (
                    route.equipment_id is None
                    or route.equipment_id in model.equipment
                )
                and all(
                    anchor.representation_id in representations
                    and anchor.electrical_node_id in model.electrical_nodes
                    and (
                        anchor.branch_port_id is None
                        or anchor.branch_port_id in model.ports
                    )
                    and (
                        anchor.target_port_id is None
                        or anchor.target_port_id in model.ports
                    )
                    for anchor in (route.start_anchor, route.end_anchor)
                )
            }
            draft.diagram = replace(
                draft.diagram,
                representations=representations,
                routes=routes,
                revision=draft.diagram.revision + 1,
            )
            merged_from = model.port_by_role(
                result.merged_section_id, "from"
            ).id
            merged_to = model.port_by_role(
                result.merged_section_id, "to"
            ).id
            merged_route = DiagramRoute(
                DiagramRouteId.new(),
                selected_page,
                DiagramRouteKind.EQUIPMENT_BRANCH,
                replace(left_start, branch_port_id=merged_from),
                replace(right_end, branch_port_id=merged_to),
                equipment_id=result.merged_section_id,
                waypoints=self._effective_route_waypoints(
                    left_start_rep,
                    right_end_rep,
                    route_waypoints,
                ),
            )
            self._add_route(draft, merged_route)
            draft.diagram = cleanup_after_deletion(
                self.model, self.diagram, model, draft.diagram,
                affected_page_ids={selected_page},
            ).diagram
            return RemoveSeriesEquipmentEditResult(
                result.feeder_id,
                result.logical_line_id,
                result.removed_right_logical_line_id,
                result.removed_equipment_id,
                result.merged_section_id,
                merged_route.id,
            )

        return self._execute("Удалить аппарат и восстановить линию", command)

    def remove_recloser_from_line(
        self,
        recloser_id: EquipmentId,
        *,
        page_id: PageId | str | None = None,
        route_waypoints: Iterable[RouteWaypoint] | None = None,
    ) -> RemoveRecloserEditResult:
        """Безопасно удалить линейный реклоузер и объединить его части."""
        self._require_edit()

        def command(draft: ProjectDraft) -> RemoveRecloserEditResult:
            model = draft.electrical_model
            equipment = model.equipment.get(recloser_id)
            if equipment is None:
                raise EditorCommandError("Реклоузер не найден.")
            insertion = equipment.extensions.get("line_insertion")
            if not isinstance(insertion, Mapping):
                raise EditorCommandError(
                    "Реклоузер не является безопасной вставкой в линию."
                )
            try:
                left_line_id = LogicalLineId(
                    str(insertion["left_logical_line_id"])
                )
                right_line_id = LogicalLineId(
                    str(insertion["right_logical_line_id"])
                )
                left_section_id = model.logical_lines[
                    left_line_id
                ].section_equipment_ids[-1]
                right_section_id = model.logical_lines[
                    right_line_id
                ].section_equipment_ids[0]
            except (KeyError, TypeError, ValueError) as exc:
                raise EditorCommandError(
                    "Данные вставки реклоузера повреждены."
                ) from exc
            requested_page = (
                self._page_id(page_id) if page_id is not None else None
            )
            left_route = self._route_for_equipment(
                draft.diagram, left_section_id, requested_page
            )
            right_route = self._route_for_equipment(
                draft.diagram, right_section_id, requested_page
            )
            if left_route is None or right_route is None:
                raise EditorCommandError(
                    "Графические трассы по сторонам реклоузера не найдены."
                )
            if left_route.page_id != right_route.page_id:
                raise EditorCommandError(
                    "Части линии реклоузера находятся на разных страницах."
                )
            selected_page = left_route.page_id
            left_node_id = model.node_for_port(
                model.port_by_role(recloser_id, "a").id
            ).id
            right_node_id = model.node_for_port(
                model.port_by_role(recloser_id, "b").id
            ).id
            left_start, left_end, left_start_rep, left_end_rep = (
                self._section_route_endpoints(
                    draft, left_section_id, selected_page, left_route
                )
            )
            if left_start.electrical_node_id == left_node_id:
                left_start, left_end = left_end, left_start
                left_start_rep, left_end_rep = left_end_rep, left_start_rep
            right_start, right_end, right_start_rep, right_end_rep = (
                self._section_route_endpoints(
                    draft, right_section_id, selected_page, right_route
                )
            )
            if right_end.electrical_node_id == right_node_id:
                right_start, right_end = right_end, right_start
                right_start_rep, right_end_rep = right_end_rep, right_start_rep

            merged_section_id = EquipmentId.new()
            self._merge_catalog_bindings(
                draft,
                (left_section_id, right_section_id),
                merged_section_id,
            )
            result = model.remove_recloser_from_line(
                recloser_id,
                merged_section_id=merged_section_id,
            )
            draft.catalog_snapshots = (
                draft.catalog_snapshots.without_equipment(recloser_id)
            )
            representations = {
                representation_id: representation
                for representation_id, representation
                in draft.diagram.representations.items()
                if (
                    representation.equipment_id is None
                    or representation.equipment_id in model.equipment
                )
                and (
                    representation.electrical_node_id is None
                    or representation.electrical_node_id in model.electrical_nodes
                )
            }
            routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if (
                    route.equipment_id is None
                    or route.equipment_id in model.equipment
                )
                and all(
                    anchor.representation_id in representations
                    and anchor.electrical_node_id in model.electrical_nodes
                    and (
                        anchor.branch_port_id is None
                        or anchor.branch_port_id in model.ports
                    )
                    and (
                        anchor.target_port_id is None
                        or anchor.target_port_id in model.ports
                    )
                    for anchor in (route.start_anchor, route.end_anchor)
                )
            }
            draft.diagram = replace(
                draft.diagram,
                representations=representations,
                routes=routes,
                revision=draft.diagram.revision + 1,
            )
            merged_from = model.port_by_role(
                result.merged_section_id, "from"
            ).id
            merged_to = model.port_by_role(
                result.merged_section_id, "to"
            ).id
            merged_route = DiagramRoute(
                DiagramRouteId.new(),
                selected_page,
                DiagramRouteKind.EQUIPMENT_BRANCH,
                replace(left_start, branch_port_id=merged_from),
                replace(right_end, branch_port_id=merged_to),
                equipment_id=result.merged_section_id,
                waypoints=self._effective_route_waypoints(
                    left_start_rep,
                    right_end_rep,
                    route_waypoints,
                ),
            )
            self._add_route(draft, merged_route)
            return RemoveRecloserEditResult(
                result.feeder_id,
                result.logical_line_id,
                result.removed_right_logical_line_id,
                result.removed_recloser_id,
                result.merged_section_id,
                merged_route.id,
            )

        return self._execute("Удалить реклоузер из линии", command)

    def remove_tap(
        self,
        main_logical_line_id: LogicalLineId | EquipmentId,
        tap_node_id: ElectricalNodeId | None = None,
        *,
        branch_line_ids: Iterable[LogicalLineId] = (),
        collapse: bool = True,
        page_id: PageId | str | None = None,
    ) -> RemoveTapEditResult:
        """Remove explicit tap branches; an EquipmentId is a safe GUI shortcut.

        With an ``EquipmentId`` the controller infers the branch line, its tap
        node and the only main line containing two adjacent sections at that
        node. Ambiguous layouts are rejected instead of guessing.
        """
        self._require_edit()
        selected_branch_ids = tuple(branch_line_ids)
        selected_main_id: LogicalLineId
        selected_tap_id: ElectricalNodeId
        if isinstance(main_logical_line_id, EquipmentId):
            branch_line = self.model.logical_line_for_section(main_logical_line_id)
            endpoint_ids = self.model._section_endpoint_ids(main_logical_line_id)
            junctions = tuple(
                node_id
                for node_id in endpoint_ids
                if self.model.electrical_nodes[node_id].extensions.get(
                    "junction_kind"
                ) == "line_tap"
            )
            if len(junctions) != 1:
                raise EditorCommandError(
                    "Выбранный участок не определяет единственную отпайку."
                )
            selected_tap_id = junctions[0]
            candidates = []
            for line in self.model.logical_lines.values():
                if line.id == branch_line.id:
                    continue
                if any(
                    self.model._section_endpoint_ids(first_id)[1]
                    == selected_tap_id
                    and self.model._section_endpoint_ids(second_id)[0]
                    == selected_tap_id
                    for first_id, second_id in zip(
                        line.section_equipment_ids,
                        line.section_equipment_ids[1:],
                    )
                ):
                    candidates.append(line.id)
            if len(candidates) != 1:
                raise EditorCommandError(
                    "Основную линию отпайки нельзя определить однозначно."
                )
            selected_main_id = candidates[0]
            selected_branch_ids = (branch_line.id,)
        else:
            selected_main_id = main_logical_line_id
            if tap_node_id is None:
                raise EditorCommandError("Не указан электрический узел отпайки.")
            selected_tap_id = tap_node_id

        def command(draft: ProjectDraft) -> RemoveTapEditResult:
            model = draft.electrical_model
            main = model.logical_lines.get(selected_main_id)
            if main is None:
                raise EditorCommandError("Основная логическая линия не найдена.")
            pairs = [
                (first_id, second_id)
                for first_id, second_id in zip(
                    main.section_equipment_ids,
                    main.section_equipment_ids[1:],
                )
                if model._section_endpoint_ids(first_id)[1] == selected_tap_id
                and model._section_endpoint_ids(second_id)[0] == selected_tap_id
            ]
            branch_equipment_ids = {
                equipment_id
                for branch_id in selected_branch_ids
                for equipment_id in (
                    model.logical_lines[branch_id].section_equipment_ids
                    if branch_id in model.logical_lines
                    else ()
                )
            }
            remaining_tap_connections = tuple(
                connection
                for connection in model.connections.values()
                if connection.electrical_node_id == selected_tap_id
                and model.ports[connection.port_id].equipment_id
                not in branch_equipment_ids
            )
            # Удаление одной боковой ветви из многоветвевого узла не должно
            # пытаться схлопнуть сам узел. Объединяем основную линию только
            # когда после удаления действительно остаются ровно две её части.
            effective_collapse = (
                collapse
                and len(pairs) == 1
                and len(remaining_tap_connections) == 2
            )
            selected_page = self._resolve_page(draft, page_id)
            merged_id = EquipmentId.new() if effective_collapse else None
            merged_ports = (
                {"from": PortId.new(), "to": PortId.new()}
                if effective_collapse
                else None
            )
            merged_connections = (
                (ConnectionId.new(), ConnectionId.new())
                if effective_collapse
                else None
            )
            outer_start: RouteEndpointAnchor | None = None
            outer_end: RouteEndpointAnchor | None = None
            outer_start_representation: GraphicalRepresentation | None = None
            outer_end_representation: GraphicalRepresentation | None = None
            if effective_collapse:
                first_id, second_id = pairs[0]
                assert merged_id is not None
                self._merge_catalog_bindings(
                    draft,
                    (first_id, second_id),
                    merged_id,
                )
                first_route = self._route_for_equipment(
                    draft.diagram, first_id, selected_page
                )
                second_route = self._route_for_equipment(
                    draft.diagram, second_id, selected_page
                )
                first_start, first_end, first_start_rep, first_end_rep = (
                    self._section_route_endpoints(
                        draft, first_id, selected_page, first_route
                    )
                )
                second_start, second_end, second_start_rep, second_end_rep = (
                    self._section_route_endpoints(
                        draft, second_id, selected_page, second_route
                    )
                )
                if first_start.electrical_node_id == selected_tap_id:
                    first_start, first_end = first_end, first_start
                    first_start_rep, first_end_rep = first_end_rep, first_start_rep
                if second_end.electrical_node_id == selected_tap_id:
                    second_start, second_end = second_end, second_start
                    second_start_rep, second_end_rep = second_end_rep, second_start_rep
                outer_start, outer_end = first_start, second_end
                outer_start_representation = first_start_rep
                outer_end_representation = second_end_rep

            equipment_before = set(model.equipment)
            model.remove_line_tap(
                selected_main_id,
                selected_tap_id,
                branch_line_ids=selected_branch_ids,
                collapse=effective_collapse,
                merged_section_id=merged_id,
                merged_port_ids_by_role=merged_ports,
                merged_connection_ids=merged_connections,
            )
            removed_equipment = equipment_before - set(model.equipment)
            for equipment_id in removed_equipment:
                draft.catalog_snapshots = (
                    draft.catalog_snapshots.without_equipment(equipment_id)
                )

            representations = {
                representation_id: representation
                for representation_id, representation
                in draft.diagram.representations.items()
                if (
                    representation.equipment_id is None
                    or representation.equipment_id in model.equipment
                )
                and (
                    representation.electrical_node_id is None
                    or representation.electrical_node_id in model.electrical_nodes
                )
            }
            routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if (
                    route.equipment_id is None
                    or route.equipment_id in model.equipment
                )
                and (
                    route.electrical_node_id is None
                    or route.electrical_node_id in model.electrical_nodes
                )
                and all(
                    anchor.representation_id in representations
                    and anchor.electrical_node_id in model.electrical_nodes
                    and (
                        anchor.branch_port_id is None
                        or anchor.branch_port_id in model.ports
                    )
                    and (
                        anchor.target_port_id is None
                        or anchor.target_port_id in model.ports
                    )
                    for anchor in (route.start_anchor, route.end_anchor)
                )
            }
            draft.diagram = replace(
                draft.diagram,
                representations=representations,
                routes=routes,
                revision=draft.diagram.revision + 1,
            )
            merged_route_id: DiagramRouteId | None = None
            if effective_collapse:
                assert merged_id is not None and merged_ports is not None
                assert outer_start is not None and outer_end is not None
                assert outer_start_representation is not None
                assert outer_end_representation is not None
                merged_route = DiagramRoute(
                    DiagramRouteId.new(),
                    selected_page,
                    DiagramRouteKind.EQUIPMENT_BRANCH,
                    replace(
                        outer_start,
                        branch_port_id=merged_ports["from"],
                    ),
                    replace(
                        outer_end,
                        branch_port_id=merged_ports["to"],
                    ),
                    equipment_id=merged_id,
                    waypoints=self._route_waypoints(
                        outer_start_representation,
                        outer_end_representation,
                    ),
                )
                self._add_route(draft, merged_route)
                merged_route_id = merged_route.id
            draft.diagram = cleanup_after_deletion(
                self.model, self.diagram, model, draft.diagram,
                affected_page_ids={selected_page},
            ).diagram
            return RemoveTapEditResult(
                selected_main_id,
                selected_branch_ids,
                selected_tap_id if effective_collapse else None,
                merged_id,
                merged_route_id,
            )

        return self._execute("Удалить отпайку", command)

    def is_physical_line(self, equipment_id: EquipmentId) -> bool:
        return equipment_id in self.model.line_sections

    def _place_existing(
        self,
        target_kind: RepresentationTargetKind,
        target_id: EquipmentId | ElectricalNodeId,
        *,
        page_id: PageId | str | None,
        x: float,
        y: float,
        symbol_key: str,
        label: str,
    ) -> GraphicalRepresentationId:
        self._require_edit()
        x, y = self.snap_point(x, y)

        def command(draft: ProjectDraft) -> GraphicalRepresentationId:
            if target_kind is RepresentationTargetKind.EQUIPMENT:
                if target_id not in draft.electrical_model.equipment:
                    raise EditorCommandError(tr("error.equipment_missing"))
                equipment_id, node_id = target_id, None
            else:
                if target_id not in draft.electrical_model.electrical_nodes:
                    raise EditorCommandError(tr("error.node_missing"))
                equipment_id, node_id = None, target_id
            selected_page = self._resolve_page(draft, page_id)
            representation = GraphicalRepresentation(
                GraphicalRepresentationId.new(),
                selected_page,
                target_kind,
                equipment_id=equipment_id,  # type: ignore[arg-type]
                electrical_node_id=node_id,  # type: ignore[arg-type]
                x=x,
                y=y,
                symbol_key=symbol_key,
                label=label,
            )
            draft.diagram = self._add_representation(draft.diagram, representation)
            extensions = thaw_json(draft.diagram.extensions)
            unplaced = set(extensions.get(UNPLACED_EXTENSION_KEY, []))
            unplaced.discard(target_id.value)
            extensions[UNPLACED_EXTENSION_KEY] = sorted(unplaced)
            draft.diagram = replace(
                draft.diagram,
                extensions=extensions,
                revision=draft.diagram.revision + 1,
            )
            return representation.id

        return self._execute(tr("command.place"), command)

    def place_existing_equipment(
        self,
        equipment_id: EquipmentId,
        *,
        page_id: PageId | str | None = None,
        x: float = 0.0,
        y: float = 0.0,
        symbol_key: str = "",
        label: str = "",
    ) -> GraphicalRepresentationId:
        return self._place_existing(
            RepresentationTargetKind.EQUIPMENT,
            equipment_id,
            page_id=page_id,
            x=x,
            y=y,
            symbol_key=symbol_key,
            label=label,
        )

    def place_existing_node(
        self,
        node_id: ElectricalNodeId,
        *,
        page_id: PageId | str | None = None,
        x: float = 0.0,
        y: float = 0.0,
        symbol_key: str = "electrical_node",
        label: str = "",
    ) -> GraphicalRepresentationId:
        return self._place_existing(
            RepresentationTargetKind.ELECTRICAL_NODE,
            node_id,
            page_id=page_id,
            x=x,
            y=y,
            symbol_key=symbol_key,
            label=label,
        )

    def _plan_move_representations(
        self, draft: ProjectDraft, ids: tuple[GraphicalRepresentationId, ...],
        dx: float, dy: float, *, bypass_snap: bool,
    ) -> DiagramDocument:
        """Pure shared geometry for mouse preview and the one undoable commit."""
        original = draft.diagram
        rows = [self._require_representation(original, item) for item in ids]
        workspace = EditorWorkspaceState.from_diagram(original)
        if workspace.snap_enabled and not bypass_snap:
            anchor = rows[0]
            x, y = self._snap_with_state(anchor.x + dx, anchor.y + dy, workspace)
            dx, dy = x - anchor.x, y - anchor.y
        if dx == 0.0 and dy == 0.0:
            return original
        values = dict(original.representations)
        deltas = {row.id: (dx, dy) for row in rows}
        for row in rows:
            values[row.id] = replace(row, x=row.x + dx, y=row.y + dy)
        moved = ProjectDraft(draft.electrical_model,
                             replace(original, representations=values), draft.catalog_snapshots)
        by_page = {page_id: self._page_routing_obstacles(moved, page_id)
                   for page_id in {row.page_id for row in rows}}
        try:
            shifted = {key: self._reroute_for_moved_representations(route, deltas)
                       for key, route in original.routes.items()}
            values, routes = reflow_degree_two_connections(
                moved.electrical_model, values, shifted, ids,
                endpoint_geometry=lambda anchor, fallback: self._route_endpoint_geometry(
                    moved, anchor, fallback, previous_representations=original.representations),
                obstacles_for_pair=lambda first, second: by_page[first.page_id],
            )
            # Route all changed conductors against the final, evolving drawing:
            # unchanged routes plus the already accepted new geometry. Old
            # locations of other pending conductors disappear in this same
            # command and must not act as ghost obstacles against their peers.
            changed={key:route for key,route in routes.items() if route!=original.routes[key]}
            accepted={key:route for key,route in routes.items() if key not in changed}
            moved.diagram=replace(moved.diagram,representations=values,routes=accepted)
            for key,route in changed.items():
                accepted[key] = self._reroute_to_current_port_anchors(
                    moved, route, previous_representations=original.representations,
                    obstacles=by_page[route.page_id],
                )
                moved.diagram=replace(moved.diagram,routes=accepted)
            routes=accepted
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc
        return replace(original, representations=values, routes=routes,
                       revision=original.revision + 1)

    def preview_move_representations(
        self, representation_ids: Iterable[GraphicalRepresentationId | str],
        dx: float, dy: float, *, bypass_snap: bool = False,
    ) -> DiagramDocument:
        """Return proposed coordinates only; model and history are untouched."""
        self._require_edit(geometry=True)
        return self._plan_move_representations(
            ProjectDraft(self.model, self.diagram, self._project.catalog_snapshots),
            self._representation_ids(representation_ids),
            _finite(dx, "Смещение X"), _finite(dy, "Смещение Y"), bypass_snap=bypass_snap,
        )

    def move_representations(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str],
        dx: float,
        dy: float,
        *,
        bypass_snap: bool = False,
    ) -> Mapping[GraphicalRepresentationId, tuple[float, float]]:
        self._require_edit(geometry=True)
        ids = self._representation_ids(representation_ids)
        dx = _finite(dx, "Смещение X")
        dy = _finite(dy, "Смещение Y")

        def command(draft: ProjectDraft) -> Mapping[GraphicalRepresentationId, tuple[float, float]]:
            draft.diagram = self._plan_move_representations(
                draft, ids, dx, dy, bypass_snap=bypass_snap)
            return {key: (draft.diagram.representations[key].x,
                          draft.diagram.representations[key].y) for key in ids}

        return self._execute(tr("command.move"), command)

    def set_representation_position(
        self,
        representation_id: GraphicalRepresentationId | str,
        x: float,
        y: float,
        *,
        bypass_snap: bool = False,
    ) -> tuple[float, float]:
        row = self._require_representation(
            self.diagram, self._representation_id(representation_id)
        )
        result = self.move_representations(
            (row.id,), x - row.x, y - row.y, bypass_snap=bypass_snap
        )
        return result[row.id]

    def reroute_diagram_route(
        self,
        route_id: DiagramRouteId,
        waypoints: Iterable[RouteWaypoint],
    ) -> DiagramRouteId:
        """Commit graphical geometry only; electrical topology stays untouched."""
        self._require_edit(geometry=True)
        points = tuple(waypoints)
        try:
            original = self.diagram.routes[route_id]
        except KeyError as exc:
            raise EditorCommandError("Маршрут не найден.") from exc
        if points == original.waypoints:
            return route_id
        from .equipment_attachment import occupied_segments
        from .orthogonal_routing import segments_overlap, _segment_hits_obstacle
        occupied,obstacles = self.preview_route_segment_constraints(route_id)
        if any(segments_overlap((a.x,a.y),(b.x,b.y),wire)
               for a,b in zip(points,points[1:]) for wire in occupied):
            raise EditorCommandError("Маршрут совпадает с другой линией на общем участке. Выберите свободное положение.")
        if any(_segment_hits_obstacle((a.x,a.y),(b.x,b.y),body)
               for a,b in zip(points,points[1:]) for body in obstacles):
            raise EditorCommandError("Маршрут пересекает тело аппарата. Выберите свободное положение.")

        def command(draft: ProjectDraft) -> DiagramRouteId:
            draft.diagram = draft.diagram.rerouted_route(route_id, points)
            return route_id

        return self._execute("Изменить графическую трассу", command)

    def move_route_segment(
        self, route_id: DiagramRouteId, segment_index: int, *, dx: float, dy: float,
    ) -> DiagramRouteId:
        """Move a wire without moving/reconnecting either electrical endpoint."""
        self._require_edit(geometry=True)
        try:
            route = self.diagram.routes[route_id]
            occupied,obstacles=self.preview_route_segment_constraints(route_id)
            points = shift_route_segment(route, segment_index, dx=dx, dy=dy,
                occupied_segments=occupied,obstacles=obstacles)
        except (KeyError, ValueError) as exc:
            raise EditorCommandError(str(exc)) from exc
        if points == route.waypoints:
            return route_id
        return self.reroute_diagram_route(route_id, points)

    def preview_route_segment_constraints(self, route_id):
        """One read-only constraint snapshot shared by GUI drag and commit."""
        from .equipment_attachment import occupied_segments, page_routing_snapshot
        try:
            route=self.diagram.routes[route_id]
        except KeyError as exc:
            raise EditorCommandError("Маршрут не найден.") from exc
        obstacles,_=page_routing_snapshot(self,route.page_id)
        return occupied_segments(self.diagram,route.page_id,(route_id,)),obstacles

    @classmethod
    def _bus_attachment_routes(
        cls, draft: ProjectDraft, route_id: DiagramRouteId, *, at_start: bool,
        fraction: float, force_separation: bool = False,
    ) -> tuple[DiagramRoute, ...]:
        """Preview one independent bus endpoint; coincidence never groups taps."""
        if not isinstance(at_start, bool):
            raise EditorCommandError("Конец трассы должен быть выбран явно.")
        fraction = min(1.0, max(0.0, _finite(fraction, "Положение присоединения")))
        try:
            selected = draft.diagram.routes[route_id]
        except KeyError as exc:
            raise EditorCommandError("Графическая трасса не найдена.") from exc
        anchor = selected.start_anchor if at_start else selected.end_anchor
        if anchor.kind is not RouteAnchorKind.BUS:
            raise EditorCommandError("Перемещать вдоль шины можно только её присоединение.")
        row = cls._require_representation(draft.diagram, anchor.representation_id)
        width, height = cls._equipment_symbol_size(row, None)
        selected_point = selected.waypoints[0 if at_start else -1]
        requested_x, requested_y, _ = bus_anchor_geometry(
            width=width, height=height, rotation=row.rotation_deg,
            center_x=row.x, center_y=row.y, fraction=fraction,
        )
        if not force_separation and (
            math.isclose(selected_point.x, requested_x, abs_tol=1e-8, rel_tol=0.0)
            and math.isclose(selected_point.y, requested_y, abs_tol=1e-8, rel_tol=0.0)
        ):
            return ()
        try:
            fraction = available_bus_fraction(
                draft.diagram, row, width=width, height=height,
                requested=fraction, exclude_route_ids=(route_id,),
                #  Явная команда «Разделить присоединения» просит развести их
                #  читаемо, поэтому здесь действует полная раздвижка. Обычное
                #  рисование её больше не применяет: там точка остаётся под
                #  проводом и сдвигается, только если слилась с соседней.
                **({"merge_tolerance": BUS_ATTACHMENT_GAP} if force_separation else {}),
            )
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc
        desired_x, desired_y, _ = bus_anchor_geometry(
            width=width, height=height, rotation=row.rotation_deg,
            center_x=row.x, center_y=row.y, fraction=fraction,
        )
        if (math.isclose(selected_point.x, desired_x, abs_tol=1e-8, rel_tol=0.0)
                and math.isclose(selected_point.y, desired_y, abs_tol=1e-8, rel_tol=0.0)):
            return ()
        updated = replace(selected, **{
            "start_anchor" if at_start else "end_anchor": replace(
                anchor, anchor_key=format(fraction, ".17g"),
            ),
        })
        return (cls._reroute_to_current_port_anchors(
            draft, updated,
            obstacles=cls._page_routing_obstacles(draft, selected.page_id),
        ),)

    def preview_bus_attachment_move(
        self, route_id: DiagramRouteId, *, at_start: bool, fraction: float,
    ) -> tuple[DiagramRoute, ...]:
        """Read-only geometry for a bus-tap gesture; no history/model writes."""
        return self._bus_attachment_routes(
            ProjectDraft(self.model, self.diagram, self._project.catalog_snapshots),
            route_id, at_start=at_start, fraction=fraction,
        )

    def move_bus_attachment(
        self, route_id: DiagramRouteId, *, at_start: bool, fraction: float,
    ) -> tuple[DiagramRouteId, ...]:
        """Slide only the selected bus attachment in one graphical command."""
        self._require_edit(geometry=True)

        def command(draft: ProjectDraft) -> tuple[DiagramRouteId, ...]:
            updates = self._bus_attachment_routes(
                draft, route_id, at_start=at_start, fraction=fraction,
            )
            if not updates:
                return ()
            values = dict(draft.diagram.routes)
            values.update((route.id, route) for route in updates)
            draft.diagram = _diagram_with_routes(draft.diagram, values)
            return tuple(route.id for route in updates)

        return self._execute("Передвинуть присоединение вдоль шины", command)

    def separate_bus_attachments(
        self, page_id: PageId | str | None = None,
    ) -> tuple[DiagramRouteId, ...]:
        """Explicitly separate saved coincident endpoints; leave other taps as is.

        The first endpoint in stable route-ID order keeps its manual position.
        This command never runs implicitly on load or during unrelated edits.
        """
        self._require_edit(geometry=True)
        selected_page = self._page_id(page_id) if page_id is not None else None

        def command(draft: ProjectDraft) -> tuple[DiagramRouteId, ...]:
            if selected_page is not None and selected_page not in draft.diagram.pages:
                raise EditorCommandError("Страница схемы не найдена.")
            occupied: dict[GraphicalRepresentationId, list[float]] = {}
            duplicates: list[tuple[DiagramRouteId, bool, float]] = []
            for route in sorted(draft.diagram.routes.values(), key=lambda row: row.id.value):
                if selected_page is not None and route.page_id != selected_page:
                    continue
                for at_start, anchor, point in (
                    (True, route.start_anchor, route.waypoints[0]),
                    (False, route.end_anchor, route.waypoints[-1]),
                ):
                    if anchor.kind is not RouteAnchorKind.BUS:
                        continue
                    representation = self._require_representation(
                        draft.diagram, anchor.representation_id,
                    )
                    width, height = self._equipment_symbol_size(representation, None)
                    fraction = endpoint_fraction(representation, width, height, anchor, point)
                    axis_position = fraction * max(width, height)
                    previous = occupied.setdefault(representation.id, [])
                    # Scene-coordinate representation roundoff only, not a
                    # new spacing policy for previously distinct manual taps.
                    if any(math.isclose(axis_position, value, abs_tol=1e-8, rel_tol=0.0)
                           for value in previous):
                        duplicates.append((route.id, at_start, fraction))
                    else:
                        previous.append(axis_position)
            changed: list[DiagramRouteId] = []
            for route_id, at_start, fraction in duplicates:
                updates = self._bus_attachment_routes(
                    draft, route_id, at_start=at_start, fraction=fraction,
                    force_separation=True,
                )
                if updates:
                    values = dict(draft.diagram.routes)
                    values.update((route.id, route) for route in updates)
                    # One public command, one diagram revision and one Undo.
                    draft.diagram = replace(draft.diagram, routes=values)
                    changed.extend(route.id for route in updates)
            if changed:
                draft.diagram = replace(draft.diagram, revision=draft.diagram.revision + 1)
            return tuple(dict.fromkeys(changed))

        return self._execute("Разнести совпадающие присоединения шин", command)

    def resize_representation(
        self,
        representation_id: GraphicalRepresentationId | str,
        width: float,
        height: float,
    ) -> RepresentationGraphics:
        self._require_edit(geometry=True)
        representation_id = self._representation_id(representation_id)

        def command(draft: ProjectDraft) -> RepresentationGraphics:
            row = self._require_representation(draft.diagram, representation_id)
            graphics = replace(
                RepresentationGraphics.from_representation(row),
                width=width,
                height=height,
                label_manual=label_is_manual(row),
            )
            values = dict(draft.diagram.representations)
            values[row.id] = representation_with_graphics(row, graphics)
            draft.diagram = _diagram_with_representations(draft.diagram, values)
            return graphics

        return self._execute(tr("command.resize"), command)

    def _change_representation_orientation(
        self,
        representation_id: GraphicalRepresentationId | str,
        rotation_deg: float | QuarterTurn | None,
        *,
        mode: OrientationMode,
        description: str,
    ) -> float:
        self._require_edit(geometry=True)
        representation_id = self._representation_id(representation_id)
        try:
            requested = (
                None
                if rotation_deg is None
                else normalize_quarter_turn(rotation_deg)
            )
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc

        def command(draft: ProjectDraft) -> float:
            row = self._require_representation(draft.diagram, representation_id)
            turn = (
                self._automatic_quarter_turn(draft, row)
                if requested is None
                else requested
            )
            updated = replace(row, rotation_deg=float(turn))
            try:
                updated = representation_with_orientation_mode(updated, mode)
            except DomainInvariantError as exc:
                raise EditorCommandError(str(exc)) from exc
            values = dict(draft.diagram.representations)
            values[row.id] = updated
            draft.diagram = _diagram_with_representations(draft.diagram, values)

            routes = dict(draft.diagram.routes)
            for route_id, route in tuple(routes.items()):
                if row.id not in {
                    route.start_anchor.representation_id,
                    route.end_anchor.representation_id,
                }:
                    continue
                if not any(
                    anchor.representation_id == row.id
                    and (anchor.target_port_id is not None or anchor.kind is RouteAnchorKind.BUS)
                    for anchor in (route.start_anchor, route.end_anchor)
                ):
                    continue
                routes[route_id] = self._reroute_to_current_port_anchors(
                    draft, route, previous_representations={row.id: row}
                )
            if routes != dict(draft.diagram.routes):
                draft.diagram = _diagram_with_routes(draft.diagram, routes)
            return float(turn)

        return self._execute(description, command)

    def rotate_representation(
        self,
        representation_id: GraphicalRepresentationId | str,
        rotation_deg: float,
    ) -> float:
        """Ручной дискретный поворот с локальной перестройкой трасс."""

        return self._change_representation_orientation(
            representation_id,
            rotation_deg,
            mode=OrientationMode.MANUAL,
            description=tr("command.rotate"),
        )

    def rotate_representation_clockwise(
        self,
        representation_id: GraphicalRepresentationId | str,
    ) -> float:
        representation_id = self._representation_id(representation_id)
        row = self._require_representation(self.diagram, representation_id)
        try:
            target = normalize_quarter_turn(row.rotation_deg).shifted(1)
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc
        return self.rotate_representation(representation_id, float(target))

    def rotate_representation_counterclockwise(
        self,
        representation_id: GraphicalRepresentationId | str,
    ) -> float:
        representation_id = self._representation_id(representation_id)
        row = self._require_representation(self.diagram, representation_id)
        try:
            target = normalize_quarter_turn(row.rotation_deg).shifted(-1)
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc
        return self.rotate_representation(representation_id, float(target))

    def rotate_representation_180(
        self,
        representation_id: GraphicalRepresentationId | str,
    ) -> float:
        representation_id = self._representation_id(representation_id)
        row = self._require_representation(self.diagram, representation_id)
        try:
            target = normalize_quarter_turn(row.rotation_deg).shifted(2)
        except ValueError as exc:
            raise EditorCommandError(str(exc)) from exc
        return self.rotate_representation(representation_id, float(target))

    def auto_orient_representation(
        self,
        representation_id: GraphicalRepresentationId | str,
        rotation_deg: float | None = None,
    ) -> float:
        """Вернуть AUTO и подобрать угол по подключению либо явной подсказке."""

        return self._change_representation_orientation(
            representation_id,
            rotation_deg,
            mode=OrientationMode.AUTO,
            description="Автоматически ориентировать по соединению",
        )

    def suggested_representation_orientation(
        self,
        representation_id: GraphicalRepresentationId | str,
    ) -> float:
        """Рассчитать AUTO-угол без изменения проекта и истории команд."""

        representation_id = self._representation_id(representation_id)
        row = self._require_representation(self.diagram, representation_id)
        draft = ProjectDraft(
            self.model,
            self.diagram,
            self._project.catalog_snapshots,
        )
        return float(self._automatic_quarter_turn(draft, row))

    def representation_orientation_mode(
        self,
        representation_id: GraphicalRepresentationId | str,
    ) -> OrientationMode:
        row = self._require_representation(
            self.diagram, self._representation_id(representation_id)
        )
        try:
            return orientation_mode_for_representation(row)
        except DomainInvariantError as exc:
            raise EditorCommandError(str(exc)) from exc

    def set_label(
        self,
        representation_id: GraphicalRepresentationId | str,
        *,
        text: str | None = None,
        label_x: float | None = None,
        label_y: float | None = None,
        visible: bool | None = None,
    ) -> RepresentationGraphics:
        self._require_edit(geometry=True)
        representation_id = self._representation_id(representation_id)

        def command(draft: ProjectDraft) -> RepresentationGraphics:
            row = self._require_representation(draft.diagram, representation_id)
            changes: dict[str, Any] = {
                "label_manual": (
                    True if label_x is not None or label_y is not None
                    else label_is_manual(row)
                ),
            }
            if label_x is not None:
                changes["label_x"] = label_x
            if label_y is not None:
                changes["label_y"] = label_y
            if visible is not None:
                changes["label_visible"] = visible
            updated, stored = _with_label_graphics(
                replace(row, label=row.label if text is None else text), changes
            )
            known = {
                key: value for key, value in stored.items() if key in {
                    "width", "height", "label_x", "label_y", "label_visible",
                    "line_width", "orientation_mode", "label_manual",
                }
            }
            for axis in ("x", "y"):
                alias = f"label_offset_{axis}"
                if f"label_{axis}" not in known and alias in stored:
                    known[f"label_{axis}"] = stored[alias]
            # Validate known values without persisting the dataclass's default
            # dimensions. Unrelated/unknown metadata is retained in `updated`.
            graphics = RepresentationGraphics(**known)
            values = dict(draft.diagram.representations)
            values[row.id] = updated
            draft.diagram = _diagram_with_representations(draft.diagram, values)
            return graphics

        return self._execute(tr("command.label"), command)

    def reset_label_positions(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str] = (),
        *,
        route_ids: Iterable[DiagramRouteId | str] = (),
    ) -> tuple[GraphicalRepresentationId | DiagramRouteId, ...]:
        """Opt selected labels into auto placement as one graphical command.

        Only the flag changes: original offsets remain available to undo and
        no legacy symbol dimensions or other extension values are materialized.
        """

        self._require_edit(geometry=True)
        ids = tuple(dict.fromkeys(
            self._representation_id(item) for item in representation_ids
        ))
        selected_routes = tuple(dict.fromkeys(
            item if isinstance(item, DiagramRouteId) else DiagramRouteId(item)
            for item in route_ids
        ))
        if not ids and not selected_routes:
            raise EditorCommandError(tr("error.empty_selection"))

        def command(draft: ProjectDraft) -> tuple[GraphicalRepresentationId | DiagramRouteId, ...]:
            values = dict(draft.diagram.representations)
            routes = dict(draft.diagram.routes)
            for representation_id in ids:
                row = self._require_representation(draft.diagram, representation_id)
                values[row.id] = _with_label_graphics(row, {"label_manual": False})[0]
            for route_id in selected_routes:
                row = routes.get(route_id)
                if row is None:
                    raise EditorCommandError("Графическая трасса не найдена.")
                routes[route_id] = _with_label_graphics(row, {"label_manual": False})[0]
            draft.diagram = replace(
                draft.diagram,
                representations=values,
                routes=routes,
                revision=draft.diagram.revision + 1,
            )
            return (*ids, *selected_routes)

        return self._execute("Автоматически разместить подписи", command)

    def reset_route_label_positions(
        self, route_ids: Iterable[DiagramRouteId | str]
    ) -> tuple[GraphicalRepresentationId | DiagramRouteId, ...]:
        return self.reset_label_positions(route_ids=route_ids)

    def set_route_label(
        self,
        route_id: DiagramRouteId | str,
        *,
        label_x: float | None = None,
        label_y: float | None = None,
        visible: bool | None = None,
    ) -> Mapping[str, Any]:
        """Store label offsets from the route anchor, without touching its path."""

        self._require_edit(geometry=True)
        selected = route_id if isinstance(route_id, DiagramRouteId) else DiagramRouteId(route_id)
        x = None if label_x is None else _finite(label_x, "Смещение подписи X")
        y = None if label_y is None else _finite(label_y, "Смещение подписи Y")
        if visible is not None and not isinstance(visible, bool):
            raise EditorCommandError("Видимость подписи должна иметь логическое значение.")

        def command(draft: ProjectDraft) -> Mapping[str, Any]:
            row = draft.diagram.routes.get(selected)
            if row is None:
                raise EditorCommandError("Графическая трасса не найдена.")
            if row.kind is not DiagramRouteKind.EQUIPMENT_BRANCH:
                raise EditorCommandError("Подпись параметров доступна только для физической ветви.")
            changes: dict[str, Any] = {}
            if x is not None:
                changes["label_x"] = x
            if y is not None:
                changes["label_y"] = y
            if visible is not None:
                changes["label_visible"] = visible
            changes["label_manual"] = (
                True if x is not None or y is not None else label_is_manual(row)
            )
            updated, graphics = _with_label_graphics(row, changes)
            routes = dict(draft.diagram.routes)
            routes[selected] = updated
            draft.diagram = replace(
                draft.diagram, routes=routes, revision=draft.diagram.revision + 1
            )
            return dict(graphics)

        return self._execute("Изменить подпись физической линии", command)

    def rename_equipment(self, equipment_id: EquipmentId, name: str) -> None:
        self._require_edit()

        def command(draft: ProjectDraft) -> None:
            draft.electrical_model.rename_equipment(equipment_id, name)

        self._execute(tr("command.rename"), command)

    def set_equipment_property(
        self, equipment_id: EquipmentId, key: str, value: Any
    ) -> None:
        self._require_edit()

        def command(draft: ProjectDraft) -> None:
            draft.electrical_model.set_equipment_property(equipment_id, key, value)

        self._execute(tr("command.property"), command)

    def set_line_section_property(
        self, section_id: EquipmentId, key: str, value: Any,
    ) -> None:
        """Edit a native physical section through its existing Domain contract."""
        self._require_edit()

        def command(draft: ProjectDraft) -> None:
            draft.electrical_model.set_section_override(section_id, key, value)

        self._execute("Изменить параметр участка линии", command)

    def set_equipment_voltage_class(
        self, equipment_id: EquipmentId, group_key: str, voltage_class_id: VoltageClassId,
    ) -> None:
        def _voltage_title(model, class_id) -> str:
            """Читаемое имя класса: «10 кВ», а не builtin.voltage.ac.10kv."""
            entry = model.voltage_classes.get(class_id) if class_id else None
            return getattr(entry, "display_name", None) or "не задан"

        """Explicitly assign an unconnected group, without replacing any IDs."""
        self._require_edit()

        def command(draft: ProjectDraft) -> None:
            model = draft.electrical_model
            equipment = model.equipment.get(equipment_id)
            if equipment is None:
                raise EditorCommandError("Оборудование не найдено.")
            if voltage_class_id not in model.voltage_classes:
                raise EditorCommandError("Класс напряжения не зарегистрирован в проекте.")
            ports = tuple(port_id for port_id in equipment.port_ids
                          if model.port_definition(port_id).voltage_group == group_key)
            if not group_key or not ports:
                raise EditorCommandError("Группа напряжения не задана типом оборудования.")
            #  Заказчик 31.08.2026 просил разрешить смену класса у подключённого
            #  аппарата с предупреждением вместо запрета. Разрешить её нельзя
            #  правкой одного этого места: домен держит инвариант «класс вывода
            #  совпадает с классом узла», и на нём стоит весь расчётный путь —
            #  адаптер, топология, приведение токов. Модель с несовпадением
            #  просто не соберётся. Ломать инвариант ради предупреждения я не
            #  стал: это отдельное решение, а не побочный эффект правки
            #  интерфейса. Вместо глухого отказа даётся сообщение, которое
            #  называет обе стороны и говорит, что делать.
            connected = [port_id for port_id in ports
                         if model.connection_for_port(port_id) is not None]
            if connected:
                node = model.electrical_nodes.get(
                    model.connection_for_port(connected[0]).electrical_node_id
                )
                node_class = endpoint_voltage(model, node.id) if node is not None else None
                effective = (node_class.voltage_class_id
                             if node_class is not None and node_class.valid else None)
                raise EditorCommandError(
                    f"«{equipment.name}» подключён к узлу "
                    f"«{node.name if node else '?'}» "
                    f"({_voltage_title(model, effective)}). Класс напряжения "
                    f"аппарата ({_voltage_title(model, equipment.voltage_class_by_group.get(group_key))}) "
                    "обязан совпадать с классом узла, поэтому у подключённого "
                    "аппарата его менять нельзя. Отсоедините вывод — и класс "
                    "станет доступен для правки; чтобы поставить "
                    f"{_voltage_title(model, voltage_class_id)}, отсоедините "
                    "вывод, смените класс и подключите снова."
                )
            groups = {**equipment.voltage_class_by_group, group_key: voltage_class_id}
            model.validate_equipment_parameters(
                equipment.type_id, type_version=equipment.type_version,
                properties=equipment.properties, voltage_class_by_group=groups,
                normal_position=equipment.normal_position,
            )
            if groups == equipment.voltage_class_by_group:
                return
            saved = ElectricalModelMemento.capture(model)
            draft.electrical_model = replace(
                saved, revision=saved.revision + 1,
                equipment={**saved.equipment, equipment_id: replace(equipment, voltage_class_by_group=groups)},
            ).to_model()

        self._execute("Задать класс напряжения группы", command)

    def set_legacy_port_voltage_class(
        self, port_id: PortId, voltage_class_id: VoltageClassId,
    ) -> None:
        """Explicit nominal class of one disconnected compatibility terminal."""
        self._require_edit()

        def command(draft: ProjectDraft) -> None:
            draft.electrical_model = set_legacy_port_voltage(
                draft.electrical_model, port_id, voltage_class_id, source="explicit",
            )

        self._execute("Задать класс напряжения вывода", command)

    def set_node_voltage_class(
        self, node_id: ElectricalNodeId, voltage_class_id: VoltageClassId,
    ) -> None:
        """Assign an isolated node explicitly; connected network zones are read-only."""
        self._require_edit()

        def command(draft: ProjectDraft) -> None:
            model = draft.electrical_model
            node = model.electrical_nodes.get(node_id)
            if node is None:
                raise EditorCommandError("Электрический узел не найден.")
            if voltage_class_id not in model.voltage_classes:
                raise EditorCommandError("Класс напряжения не зарегистрирован в проекте.")
            if any(connection.electrical_node_id == node_id for connection in model.connections.values()):
                raise EditorCommandError("Класс напряжения подключённого узла нельзя менять.")
            if node.declared_voltage_class_id == voltage_class_id:
                return
            saved = ElectricalModelMemento.capture(model)
            draft.electrical_model = replace(
                saved, revision=saved.revision + 1,
                electrical_nodes={**saved.electrical_nodes,
                                  node_id: replace(node, declared_voltage_class_id=voltage_class_id)},
            ).to_model()

        self._execute("Задать класс напряжения узла", command)

    def set_switch_position(
        self,
        state_id: OperatingStateId,
        equipment_id: EquipmentId,
        position: SwitchPosition,
        *,
        confirmed: bool = False,
    ) -> None:
        self.switch_equipment(
            equipment_id,
            position,
            state_id=state_id,
            confirmed=confirmed,
        )

    def switch_equipment(
        self,
        equipment_id: EquipmentId,
        position: SwitchPosition,
        *,
        state_id: OperatingStateId | None = None,
        confirmed: bool = False,
    ) -> OperatingStateId:
        """Switch equipment in an explicit active sparse operating state."""
        if self.workspace_state.confirm_switching and not confirmed:
            raise EditorCommandError(tr("error.switch_confirmation"))
        if not isinstance(position, SwitchPosition):
            try:
                position = SwitchPosition(position)
            except (TypeError, ValueError) as exc:
                raise EditorCommandError(
                    "Положение аппарата должно быть «Включён» или «Отключён»."
                ) from exc

        def command(draft: ProjectDraft) -> OperatingStateId:
            workspace = EditorWorkspaceState.from_diagram(draft.diagram)
            selected = state_id
            if selected is None and workspace.active_operating_state_id:
                candidate = OperatingStateId(workspace.active_operating_state_id)
                if candidate in draft.electrical_model.operating_states:
                    selected = candidate
            if selected is None:
                selected = OperatingStateId.new()
                draft.electrical_model.add_operating_state(OperatingState(
                    selected,
                    "Рабочий режим редактора",
                    description=(
                        "Изменения коммутационных аппаратов, выполненные на схеме."
                    ),
                    extensions={"editor_working_state": True},
                ))
            elif selected not in draft.electrical_model.operating_states:
                raise EditorCommandError("Выбранный режим электрической сети не найден.")
            draft.electrical_model.set_switch_position(
                selected, equipment_id, position
            )
            draft.diagram = diagram_with_workspace(
                draft.diagram,
                replace(
                    workspace,
                    active_operating_state_id=selected.value,
                ),
            )
            return selected

        return self._execute(tr("command.switch"), command)

    def effective_switch_position(
        self,
        equipment_id: EquipmentId,
        state_id: OperatingStateId | None = None,
    ) -> SwitchPosition | None:
        equipment = self.model.equipment.get(equipment_id)
        if equipment is None:
            raise EditorCommandError(tr("error.equipment_missing"))
        selected = state_id or self.active_operating_state_id
        if selected is not None:
            state = self.model.operating_states.get(selected)
            if state is None:
                raise EditorCommandError("Выбранный режим электрической сети не найден.")
            if equipment_id in state.positions:
                return state.positions[equipment_id]
        return equipment.normal_position

    def _build_clipboard(
        self,
        representation_ids: tuple[GraphicalRepresentationId, ...],
        route_ids: tuple[DiagramRouteId, ...] = (),
    ) -> ProjectClipboard:
        model = self.model
        explicit_routes: list[DiagramRoute] = []
        for route_id in route_ids:
            try:
                explicit_routes.append(self.diagram.routes[route_id])
            except KeyError as exc:
                raise EditorCommandError("Графическая трасса не найдена.") from exc
        representation_id_set = set(representation_ids)
        for route in explicit_routes:
            representation_id_set.update((
                route.start_anchor.representation_id,
                route.end_anchor.representation_id,
            ))
        representations = tuple(
            self._require_representation(self.diagram, item)
            for item in representation_id_set
        )
        equipment_ids = {
            item.equipment_id for item in representations
            if item.equipment_id is not None
        }
        equipment_ids.update(
            item.equipment_id
            for item in explicit_routes
            if item.equipment_id is not None
        )
        explicit_node_ids = {
            item.electrical_node_id for item in representations
            if item.electrical_node_id is not None
        }
        explicit_node_ids.update(
            node_id
            for route in explicit_routes
            for node_id in (
                route.electrical_node_id,
                route.start_anchor.electrical_node_id,
                route.end_anchor.electrical_node_id,
            )
            if node_id is not None
        )
        selected_port_ids = {
            port_id
            for equipment_id in equipment_ids
            for port_id in model.equipment[equipment_id].port_ids
        }
        selected_connections_by_node: dict[ElectricalNodeId, list[Connection]] = {}
        for connection in model.connections.values():
            if connection.port_id in selected_port_ids:
                selected_connections_by_node.setdefault(
                    connection.electrical_node_id, []
                ).append(connection)
        selected_line_ids = equipment_ids & set(model.line_sections)
        selected_line_ports = {
            port_id
            for equipment_id in selected_line_ids
            for port_id in model.equipment[equipment_id].port_ids
        }
        node_ids = set(explicit_node_ids)
        for node_id, connections in selected_connections_by_node.items():
            if len(connections) >= 2 or any(
                item.port_id in selected_line_ports for item in connections
            ):
                node_ids.add(node_id)
        connections = tuple(sorted(
            (
                item for item in model.connections.values()
                if item.port_id in selected_port_ids
                and item.electrical_node_id in node_ids
            ),
            key=lambda item: item.id.value,
        ))
        selected_routes = tuple(sorted(
            (
                route
                for route in self.diagram.routes.values()
                if (
                    route.id in route_ids
                    or (
                        route.start_anchor.representation_id
                        in representation_id_set
                        and route.end_anchor.representation_id
                        in representation_id_set
                        and (
                            route.equipment_id is None
                            or route.equipment_id in equipment_ids
                        )
                        and (
                            route.electrical_node_id is None
                            or route.electrical_node_id in node_ids
                        )
                    )
                )
            ),
            key=lambda item: item.id.value,
        ))

        line_groups: list[ClipboardLineGroup] = []
        for line in model.logical_lines.values():
            indexes = [
                index for index, equipment_id in enumerate(line.section_equipment_ids)
                if equipment_id in equipment_ids
            ]
            if not indexes:
                continue
            run: list[int] = []
            for index in indexes:
                if run and index != run[-1] + 1:
                    line_groups.append(ClipboardLineGroup(
                        line,
                        tuple(model.line_sections[line.section_equipment_ids[item]] for item in run),
                    ))
                    run = []
                run.append(index)
            if run:
                line_groups.append(ClipboardLineGroup(
                    line,
                    tuple(model.line_sections[line.section_equipment_ids[item]] for item in run),
                ))

        return ProjectClipboard(
            tuple(sorted(representations, key=lambda item: item.id.value)),
            tuple(sorted(
                (model.equipment[item] for item in equipment_ids),
                key=lambda item: item.id.value,
            )),
            tuple(sorted(
                (model.ports[item] for item in selected_port_ids),
                key=lambda item: item.id.value,
            )),
            tuple(sorted(
                (model.electrical_nodes[item] for item in node_ids),
                key=lambda item: item.id.value,
            )),
            connections,
            tuple(line_groups),
            tuple(
                self._project.catalog_snapshots.bindings[item]
                for item in sorted(equipment_ids, key=lambda value: value.value)
                if item in self._project.catalog_snapshots.bindings
            ),
            selected_routes,
        )

    def copy(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str] = (),
        *,
        route_ids: Iterable[DiagramRouteId | str] = (),
    ) -> ProjectClipboard:
        self._require_edit()
        ids = tuple(dict.fromkeys(
            self._representation_id(item) for item in representation_ids
        ))
        normalized_route_ids = tuple(dict.fromkeys(
            item if isinstance(item, DiagramRouteId) else DiagramRouteId(item)
            for item in route_ids
        ))
        if not ids and not normalized_route_ids:
            raise EditorCommandError(tr("error.empty_selection"))
        self._clipboard = self._build_clipboard(ids, normalized_route_ids)
        return self._clipboard

    def suggested_copy_offset(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str] | None = None,
    ) -> tuple[float, float]:
        """Вернуть один предсказуемый offset для GUI-copy/paste.

        Группа ставится справа на ширину её общего габарита плюс
        безопасный зазор. Поиск свободной ячейки не выполняется:
        если это единственное место занято, атомарный preflight отклонит
        команду с понятным сообщением.
        """

        if representation_ids is None:
            if self._clipboard is None:
                raise EditorCommandError(tr("error.clipboard_empty"))
            representations = tuple(self._clipboard.representations)
        else:
            ids = tuple(dict.fromkeys(
                self._representation_id(item) for item in representation_ids
            ))
            representations = tuple(
                self._require_representation(self.diagram, item)
                for item in sorted(ids, key=lambda value: value.value)
            )
        workspace = self.workspace_state
        if not representations:
            return (workspace.grid_size if workspace.snap_enabled else 20.0, 0.0)

        from .collision import DEFAULT_SAFE_GAP, DiagramCollisionService

        geometries = DiagramCollisionService(self.diagram, self.model).geometries
        shapes = tuple(
            geometries[item.id].body_collision_shape
            or geometries[item.id].visual_bounds
            for item in representations
            if item.id in geometries
        )
        if not shapes:
            required = workspace.grid_size if workspace.snap_enabled else 20.0
        else:
            left = min(item.bounds.left for item in shapes)
            right = max(item.bounds.right for item in shapes)
            required = right - left + DEFAULT_SAFE_GAP
        anchor = representations[0]
        if not workspace.snap_enabled:
            return required, 0.0
        step = workspace.grid_size
        target_x = round((anchor.x + required) / step) * step
        effective = target_x - anchor.x
        if effective + 1e-9 < required:
            target_x += step
            effective = target_x - anchor.x
        return effective, 0.0

    @staticmethod
    def _copy_name(value: str) -> str:
        return value + tr("copy.suffix")

    def _paste_clipboard(
        self,
        clipboard: ProjectClipboard,
        *,
        page_id: PageId | str | None,
        offset_x: float,
        offset_y: float,
        description: str,
        validate_collision: bool = False,
    ) -> PasteResult:
        offset_x = _finite(offset_x, "Смещение вставки X")
        offset_y = _finite(offset_y, "Смещение вставки Y")

        def command(draft: ProjectDraft) -> PasteResult:
            existing_diagram = draft.diagram
            target_page = self._resolve_page(draft, page_id) if page_id is not None else None
            equipment_map = {item.id: EquipmentId.new() for item in clipboard.equipment}
            port_map = {item.id: PortId.new() for item in clipboard.ports}
            node_map = {item.id: ElectricalNodeId.new() for item in clipboard.nodes}
            connection_map = {item.id: ConnectionId.new() for item in clipboard.connections}
            feeder_map = {
                item.logical_line.feeder_id: FeederId.new()
                for item in clipboard.line_groups
                if item.logical_line.feeder_id is not None
            }

            ports_by_equipment: dict[EquipmentId, list[PortInstance]] = {}
            for port in clipboard.ports:
                copied = PortInstance(
                    port_map[port.id], equipment_map[port.equipment_id], port.role
                )
                ports_by_equipment.setdefault(port.equipment_id, []).append(copied)
            line_equipment = {
                section.equipment_id
                for group in clipboard.line_groups
                for section in group.sections
            }
            for equipment in clipboard.equipment:
                copied_ports_by_id = {
                    item.id: item for item in ports_by_equipment[equipment.id]
                }
                ordered_port_ids = tuple(
                    port_map[item] for item in equipment.port_ids
                )
                copied = EquipmentInstance(
                    equipment_map[equipment.id],
                    equipment.type_id,
                    equipment.type_version,
                    self._copy_name(equipment.name),
                    ordered_port_ids,
                    thaw_json(equipment.properties),
                    dict(equipment.voltage_class_by_group),
                    equipment.normal_position,
                    equipment.note,
                    _clean_extensions(equipment.extensions),
                )
                ordered_ports = tuple(
                    copied_ports_by_id[item] for item in ordered_port_ids
                )
                draft.electrical_model.add_equipment_instance(
                    copied,
                    ordered_ports,
                    _allow_unowned_line_section=equipment.id in line_equipment,
                )

            for node in clipboard.nodes:
                draft.electrical_model.add_node(ElectricalNode(
                    node_map[node.id],
                    node.name,
                    node.kind_id,
                    node.declared_voltage_class_id,
                    node.note,
                    _clean_extensions(node.extensions),
                ))
            for connection in clipboard.connections:
                draft.electrical_model.add_connection(Connection(
                    connection_map[connection.id],
                    port_map[connection.port_id],
                    node_map[connection.electrical_node_id],
                    _clean_extensions(connection.extensions),
                ))

            for group in clipboard.line_groups:
                new_line_id = LogicalLineId.new()
                copied_sections: list[LineSection] = []
                for section in group.sections:
                    copied_segments = tuple(
                        LineConstructionSegment(
                            LineConstructionSegmentId.new(),
                            segment.line_kind,
                            segment.length_mm,
                            thaw_json(segment.properties),
                            _clean_extensions(segment.extensions),
                            segment.length_confirmation,
                            segment.impedance_confirmation,
                        )
                        for segment in section.construction_segments
                    )
                    copied_sections.append(LineSection(
                        equipment_map[section.equipment_id],
                        new_line_id,
                        construction_segments=copied_segments,
                        extensions=_clean_extensions(section.extensions),
                    ))
                copied_line = LogicalLine(
                    new_line_id,
                    self._copy_name(group.logical_line.name),
                    group.logical_line.line_kind,
                    group.logical_line.voltage_class_id,
                    tuple(item.equipment_id for item in copied_sections),
                    thaw_json(group.logical_line.inherited_properties),
                    group.logical_line.note,
                    _clean_extensions(group.logical_line.extensions),
                    (
                        feeder_map[group.logical_line.feeder_id]
                        if group.logical_line.feeder_id is not None
                        else None
                    ),
                )
                draft.electrical_model.add_logical_line(
                    copied_line, copied_sections
                )

            for binding in clipboard.catalog_bindings:
                copied_binding = CatalogBinding(
                    equipment_map[binding.equipment_id],
                    binding.entry,
                    thaw_json(binding.instance_overrides),
                    {},
                    dict(binding.parameter_overrides),
                    _clean_extensions(binding.extensions),
                )
                draft.catalog_snapshots = draft.catalog_snapshots.with_binding(
                    copied_binding
                )

            representation_map: dict[
                GraphicalRepresentationId, GraphicalRepresentationId
            ] = {}
            workspace = EditorWorkspaceState.from_diagram(draft.diagram)
            if clipboard.representations and workspace.snap_enabled:
                anchor = clipboard.representations[0]
                snapped_x, snapped_y = self._snap_with_state(
                    anchor.x + offset_x, anchor.y + offset_y, workspace
                )
                effective_x = snapped_x - anchor.x
                effective_y = snapped_y - anchor.y
            else:
                effective_x, effective_y = offset_x, offset_y
            values = dict(draft.diagram.representations)
            for source in clipboard.representations:
                selected_page = target_page or source.page_id
                if selected_page not in draft.diagram.pages:
                    raise EditorCommandError(tr("error.page_missing"))
                representation_id = GraphicalRepresentationId.new()
                representation_map[source.id] = representation_id
                copied = GraphicalRepresentation(
                    representation_id,
                    selected_page,
                    source.target_kind,
                    equipment_id=(
                        equipment_map[source.equipment_id]
                        if source.equipment_id is not None else None
                    ),
                    electrical_node_id=(
                        node_map[source.electrical_node_id]
                        if source.electrical_node_id is not None else None
                    ),
                    x=source.x + effective_x,
                    y=source.y + effective_y,
                    rotation_deg=source.rotation_deg,
                    z_index=source.z_index,
                    symbol_key=source.symbol_key,
                    label=self._copy_name(source.label) if source.label else "",
                    route_points=tuple(
                        RoutePoint(item.x + effective_x, item.y + effective_y)
                        for item in source.route_points
                    ),
                    extensions=_clean_extensions(source.extensions),
                )
                values[copied.id] = copied
            draft.diagram = _diagram_with_representations(draft.diagram, values)
            route_map: dict[DiagramRouteId, DiagramRouteId] = {}
            route_values = dict(draft.diagram.routes)
            for source in clipboard.routes:
                selected_page = target_page or source.page_id
                if selected_page not in draft.diagram.pages:
                    raise EditorCommandError(tr("error.page_missing"))
                route_id = DiagramRouteId.new()
                route_map[source.id] = route_id

                def copied_anchor(anchor: RouteEndpointAnchor) -> RouteEndpointAnchor:
                    try:
                        representation_id = representation_map[
                            anchor.representation_id
                        ]
                        electrical_node_id = node_map[anchor.electrical_node_id]
                    except KeyError as exc:
                        raise EditorCommandError(
                            "Буфер обмена не содержит цель графической трассы."
                        ) from exc
                    return RouteEndpointAnchor(
                        anchor.kind,
                        representation_id,
                        electrical_node_id,
                        branch_port_id=(
                            port_map[anchor.branch_port_id]
                            if anchor.branch_port_id is not None
                            else None
                        ),
                        target_port_id=(
                            port_map[anchor.target_port_id]
                            if anchor.target_port_id is not None
                            else None
                        ),
                        anchor_key=anchor.anchor_key,
                    )

                copied_route = DiagramRoute(
                    route_id,
                    selected_page,
                    source.kind,
                    copied_anchor(source.start_anchor),
                    copied_anchor(source.end_anchor),
                    equipment_id=(
                        equipment_map[source.equipment_id]
                        if source.equipment_id is not None
                        else None
                    ),
                    electrical_node_id=(
                        node_map[source.electrical_node_id]
                        if source.electrical_node_id is not None
                        else None
                    ),
                    waypoints=tuple(
                        RouteWaypoint(
                            RouteWaypointId.new(),
                            point.x + effective_x,
                            point.y + effective_y,
                            point.source,
                            point.pinned,
                        )
                        for point in source.waypoints
                    ),
                    routing_algorithm_version=source.routing_algorithm_version,
                    extensions=_clean_extensions(source.extensions),
                )
                route_values[copied_route.id] = copied_route
            if clipboard.routes:
                draft.diagram = _diagram_with_routes(
                    draft.diagram, route_values
                )
            if validate_collision and representation_map:
                from .collision import (
                    DiagramCollisionService,
                    geometry_for_representation,
                )

                candidates = tuple(
                    geometry_for_representation(
                        draft.diagram.representations[representation_id],
                        draft.electrical_model,
                    )
                    for representation_id in representation_map.values()
                )
                check = DiagramCollisionService(
                    existing_diagram,
                    draft.electrical_model,
                ).check_placements(candidates)
                if not check.allowed:
                    raise EditorCommandError(check.message)
            return PasteResult(
                tuple(equipment_map.values()),
                tuple(node_map.values()),
                tuple(connection_map.values()),
                tuple(representation_map.values()),
                tuple(route_map.values()),
            )

        return self._execute(description, command)

    def paste(
        self,
        *,
        page_id: PageId | str | None = None,
        offset_x: float = 20.0,
        offset_y: float = 20.0,
        validate_collision: bool = False,
    ) -> PasteResult:
        self._require_edit()
        if self._clipboard is None:
            raise EditorCommandError(tr("error.clipboard_empty"))
        return self._paste_clipboard(
            self._clipboard,
            page_id=page_id,
            offset_x=offset_x,
            offset_y=offset_y,
            description=tr("command.paste"),
            validate_collision=validate_collision,
        )

    def duplicate(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str] = (),
        *,
        route_ids: Iterable[DiagramRouteId | str] = (),
        page_id: PageId | str | None = None,
        offset_x: float = 20.0,
        offset_y: float = 20.0,
        validate_collision: bool = False,
    ) -> PasteResult:
        self._require_edit()
        ids = tuple(dict.fromkeys(
            self._representation_id(item) for item in representation_ids
        ))
        normalized_route_ids = tuple(dict.fromkeys(
            item if isinstance(item, DiagramRouteId) else DiagramRouteId(item)
            for item in route_ids
        ))
        if not ids and not normalized_route_ids:
            raise EditorCommandError(tr("error.empty_selection"))
        clipboard = self._build_clipboard(ids, normalized_route_ids)
        return self._paste_clipboard(
            clipboard,
            page_id=page_id,
            offset_x=offset_x,
            offset_y=offset_y,
            description=tr("command.duplicate"),
            validate_collision=validate_collision,
        )

    def delete_from_project(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str],
        *,
        confirmed: bool = True,
    ) -> DeleteResult:
        self._require_edit()
        if not confirmed:
            raise EditorCommandError(tr("error.delete_confirmation"))
        ids = self._representation_ids(representation_ids)
        structure = getattr(self._project, "structure", None)
        if structure is not None:
            has_calculation_refs = any(
                getattr(item, "calculation_refs", ())
                for item in getattr(structure, "equipment", {}).values()
            ) or any(
                getattr(item, "calculation_node_id", None) is not None
                for item in getattr(structure, "bus_sections", {}).values()
            )
            if has_calculation_refs:
                raise EditorCommandError(tr("error.structure_delete"))

        def command(draft: ProjectDraft) -> DeleteResult:
            selected = [
                self._require_representation(draft.diagram, item) for item in ids
            ]
            equipment_ids = {
                item.equipment_id for item in selected
                if item.equipment_id is not None
            }
            node_ids = {
                item.electrical_node_id for item in selected
                if item.electrical_node_id is not None
            }
            line_section_ids = equipment_ids & set(draft.electrical_model.line_sections)
            for line in tuple(draft.electrical_model.logical_lines.values()):
                chosen = set(line.section_equipment_ids) & line_section_ids
                if chosen and chosen == set(line.section_equipment_ids):
                    draft.electrical_model.remove_logical_line(
                        line.id, cascade=True
                    )
            remaining_sections = [
                item for item in line_section_ids
                if item in draft.electrical_model.line_sections
            ]
            for equipment_id in sorted(
                remaining_sections, key=lambda value: value.value
            ):
                draft.electrical_model.remove_line_section(equipment_id)
            for equipment_id in sorted(
                equipment_ids - line_section_ids,
                key=lambda value: value.value,
            ):
                if equipment_id in draft.electrical_model.equipment:
                    draft.electrical_model.remove_equipment(
                        equipment_id, cascade=True
                    )
            removed_nodes: set[ElectricalNodeId] = set()
            for node_id in sorted(node_ids, key=lambda value: value.value):
                if node_id in draft.electrical_model.electrical_nodes:
                    draft.electrical_model.remove_node(node_id, cascade=True)
                    removed_nodes.add(node_id)

            for equipment_id in equipment_ids:
                draft.catalog_snapshots = (
                    draft.catalog_snapshots.without_equipment(equipment_id)
                )
            values = {
                representation_id: item
                for representation_id, item in draft.diagram.representations.items()
                if item.equipment_id not in equipment_ids
                and item.electrical_node_id not in removed_nodes
            }
            removed_representation_ids = tuple(
                item for item in draft.diagram.representations if item not in values
            )
            routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if route.start_anchor.representation_id in values
                and route.end_anchor.representation_id in values
                and (
                    route.equipment_id is None
                    or route.equipment_id in draft.electrical_model.equipment
                )
                and (
                    route.electrical_node_id is None
                    or route.electrical_node_id
                    in draft.electrical_model.electrical_nodes
                )
                and all(
                    anchor.electrical_node_id
                    in draft.electrical_model.electrical_nodes
                    and (
                        anchor.branch_port_id is None
                        or anchor.branch_port_id in draft.electrical_model.ports
                    )
                    and (
                        anchor.target_port_id is None
                        or anchor.target_port_id in draft.electrical_model.ports
                    )
                    for anchor in (route.start_anchor, route.end_anchor)
                )
            }
            removed_route_ids = tuple(
                item for item in draft.diagram.routes if item not in routes
            )
            extensions = thaw_json(draft.diagram.extensions)
            unplaced = set(extensions.get(UNPLACED_EXTENSION_KEY, []))
            unplaced.difference_update(
                item.value for item in (*equipment_ids, *removed_nodes)
            )
            extensions[UNPLACED_EXTENSION_KEY] = sorted(unplaced)
            draft.diagram = replace(
                draft.diagram,
                representations=values,
                routes=routes,
                extensions=extensions,
                revision=draft.diagram.revision + 1,
            )
            cleanup = cleanup_after_deletion(
                self.model, self.diagram, draft.electrical_model, draft.diagram,
                affected_page_ids={item.page_id for item in selected},
            )
            draft.diagram = cleanup.diagram
            removed_nodes.update(cleanup.node_ids)
            removed_representation_ids = tuple(
                item for item in self.diagram.representations
                if item not in draft.diagram.representations
            )
            removed_route_ids = tuple(
                item for item in self.diagram.routes
                if item not in draft.diagram.routes
            )
            return DeleteResult(
                tuple(sorted(equipment_ids, key=lambda value: value.value)),
                tuple(sorted(removed_nodes, key=lambda value: value.value)),
                removed_representation_ids,
                removed_route_ids,
            )

        return self._execute(tr("command.delete_project"), command)

    def delete_diagram_route(
        self,
        route_id: DiagramRouteId | str,
        *,
        confirmed: bool = True,
    ) -> DeleteResult:
        """Удалить выбранную трассу вместе с её явной электрической сущностью."""
        self._require_edit()
        if not confirmed:
            raise EditorCommandError(tr("error.delete_confirmation"))
        normalized_id = (
            route_id
            if isinstance(route_id, DiagramRouteId)
            else DiagramRouteId(route_id)
        )

        def command(draft: ProjectDraft) -> DeleteResult:
            route = draft.diagram.routes.get(normalized_id)
            if route is None:
                raise EditorCommandError("Графическая трасса не найдена.")
            model = draft.electrical_model
            removed_equipment: set[EquipmentId] = set()
            removed_nodes: set[ElectricalNodeId] = set()
            disconnected_ports: set[PortId] = set()

            if route.kind is DiagramRouteKind.EQUIPMENT_BRANCH:
                equipment_id = route.equipment_id
                if equipment_id is None or equipment_id not in model.line_sections:
                    raise EditorCommandError(
                        "Физическая трасса не ссылается на участок линии."
                    )
                line = model.logical_line_for_section(equipment_id)
                if len(line.section_equipment_ids) == 1:
                    model.remove_logical_line(line.id, cascade=True)
                else:
                    model.remove_line_section(equipment_id)
                removed_equipment.add(equipment_id)
                draft.catalog_snapshots = (
                    draft.catalog_snapshots.without_equipment(equipment_id)
                )
            else:
                candidate_ports = {
                    port_id
                    for anchor in (route.start_anchor, route.end_anchor)
                    for port_id in (anchor.target_port_id,)
                    if port_id is not None
                }
                # Один уже подключённый порт может быть графической опорой
                # нескольких трасс одного электрического узла. Выбранная
                # трасса владеет только тем подключением, которое больше ни
                # одна трасса не использует.
                disconnected_ports = {
                    port_id
                    for port_id in candidate_ports
                    if not any(
                        other.id != normalized_id
                        and any(
                            anchor.target_port_id == port_id
                            for anchor in (
                                other.start_anchor,
                                other.end_anchor,
                            )
                        )
                        for other in draft.diagram.routes.values()
                    )
                }
                for port_id in sorted(
                    disconnected_ports, key=lambda value: value.value
                ):
                    connection = model.connection_for_port(port_id)
                    if connection is not None:
                        model.remove_connection(connection.id)

                node_id = route.electrical_node_id
                if node_id is not None:
                    has_connections = any(
                        item.electrical_node_id == node_id
                        for item in model.connections.values()
                    )
                    has_representation = any(
                        item.electrical_node_id == node_id
                        for item in draft.diagram.representations.values()
                    )
                    has_other_route = any(
                        other.id != normalized_id
                        and (
                            other.electrical_node_id == node_id
                            or other.start_anchor.electrical_node_id == node_id
                            or other.end_anchor.electrical_node_id == node_id
                        )
                        for other in draft.diagram.routes.values()
                    )
                    if (
                        not has_connections
                        and not has_representation
                        and not has_other_route
                        and node_id in model.electrical_nodes
                    ):
                        model.remove_node(node_id)
                        removed_nodes.add(node_id)

            representations = {
                representation_id: representation
                for representation_id, representation
                in draft.diagram.representations.items()
                if (
                    representation.equipment_id is None
                    or representation.equipment_id in model.equipment
                )
                and (
                    representation.electrical_node_id is None
                    or representation.electrical_node_id in model.electrical_nodes
                )
            }
            routes = {}
            for candidate_id, candidate in draft.diagram.routes.items():
                if candidate_id == normalized_id:
                    continue
                if any(
                    anchor.target_port_id in disconnected_ports
                    for anchor in (candidate.start_anchor, candidate.end_anchor)
                ):
                    continue
                if (
                    candidate.equipment_id is not None
                    and candidate.equipment_id not in model.equipment
                ):
                    continue
                if (
                    candidate.electrical_node_id is not None
                    and candidate.electrical_node_id not in model.electrical_nodes
                ):
                    continue
                if not all(
                    anchor.representation_id in representations
                    and anchor.electrical_node_id in model.electrical_nodes
                    and (
                        anchor.branch_port_id is None
                        or anchor.branch_port_id in model.ports
                    )
                    and (
                        anchor.target_port_id is None
                        or anchor.target_port_id in model.ports
                    )
                    for anchor in (candidate.start_anchor, candidate.end_anchor)
                ):
                    continue
                routes[candidate_id] = candidate
            removed_representation_ids = tuple(
                representation_id
                for representation_id in draft.diagram.representations
                if representation_id not in representations
            )
            removed_route_ids = tuple(
                candidate_id
                for candidate_id in draft.diagram.routes
                if candidate_id not in routes
            )
            draft.diagram = replace(
                draft.diagram,
                representations=representations,
                routes=routes,
                revision=draft.diagram.revision + 1,
            )
            cleanup = cleanup_after_deletion(
                self.model, self.diagram, model, draft.diagram,
                affected_page_ids={route.page_id},
            )
            draft.diagram = cleanup.diagram
            removed_nodes.update(cleanup.node_ids)
            removed_representation_ids = tuple(
                item for item in self.diagram.representations
                if item not in draft.diagram.representations
            )
            removed_route_ids = tuple(
                item for item in self.diagram.routes
                if item not in draft.diagram.routes
            )
            return DeleteResult(
                tuple(sorted(removed_equipment, key=lambda value: value.value)),
                tuple(sorted(removed_nodes, key=lambda value: value.value)),
                removed_representation_ids,
                removed_route_ids,
            )

        return self._execute("Удалить электрическое соединение", command)

    def remove_from_page(
        self,
        representation_ids: Iterable[GraphicalRepresentationId | str],
        *,
        mark_as_unplaced: bool = False,
    ) -> DeleteResult:
        self._require_edit()
        ids = self._representation_ids(representation_ids)

        def command(draft: ProjectDraft) -> DeleteResult:
            selected = [
                self._require_representation(draft.diagram, item) for item in ids
            ]
            removing = set(ids)
            remaining_targets = {
                _target_token(item)
                for representation_id, item in draft.diagram.representations.items()
                if representation_id not in removing
            }
            last_targets = {
                _target_token(item) for item in selected
                if _target_token(item) not in remaining_targets
            }
            if last_targets and not mark_as_unplaced:
                raise EditorCommandError(tr("error.remove_last_representation"))
            values = {
                representation_id: item
                for representation_id, item in draft.diagram.representations.items()
                if representation_id not in removing
            }
            routes = {
                route_id: route
                for route_id, route in draft.diagram.routes.items()
                if route.start_anchor.representation_id in values
                and route.end_anchor.representation_id in values
            }
            removed_route_ids = tuple(
                route_id
                for route_id in draft.diagram.routes
                if route_id not in routes
            )
            extensions = thaw_json(draft.diagram.extensions)
            unplaced = set(extensions.get(UNPLACED_EXTENSION_KEY, []))
            if mark_as_unplaced:
                unplaced.update(last_targets)
            extensions[UNPLACED_EXTENSION_KEY] = sorted(unplaced)
            draft.diagram = replace(
                draft.diagram,
                representations=values,
                routes=routes,
                extensions=extensions,
                revision=draft.diagram.revision + 1,
            )
            cleanup = cleanup_after_deletion(
                self.model, self.diagram, draft.electrical_model, draft.diagram,
                affected_page_ids={item.page_id for item in selected},
                remove_electrical_nodes=False,
            )
            draft.diagram = cleanup.diagram
            return DeleteResult(
                representation_ids=tuple(
                    item for item in self.diagram.representations
                    if item not in draft.diagram.representations
                ),
                route_ids=tuple(
                    item for item in self.diagram.routes
                    if item not in draft.diagram.routes
                ),
            )

        return self._execute(tr("command.remove_page"), command)

    def undo(self) -> ProjectChangeSet:
        try:
            return self._history.undo()
        except ProjectHistoryError as exc:
            raise self._raise_user_error(exc) from exc

    def redo(self) -> ProjectChangeSet:
        try:
            return self._history.redo()
        except ProjectHistoryError as exc:
            raise self._raise_user_error(exc) from exc

    @staticmethod
    def _snap_with_state(
        x: float, y: float, state: EditorWorkspaceState
    ) -> tuple[float, float]:
        if not state.snap_enabled:
            return x, y
        return (
            round(x / state.grid_size) * state.grid_size,
            round(y / state.grid_size) * state.grid_size,
        )

    def snap_point(
        self, x: float, y: float, *, bypass_snap: bool = False
    ) -> tuple[float, float]:
        x = _finite(x, "Координата X")
        y = _finite(y, "Координата Y")
        if bypass_snap:
            return x, y
        return self._snap_with_state(x, y, self.workspace_state)

    def _set_workspace(self, **changes: Any) -> EditorWorkspaceState:
        try:
            state = replace(self.workspace_state, **changes)
            diagram = diagram_with_workspace(self.diagram, state)
            self._history.apply_workspace_diagram(diagram)
            return state
        except (DomainInvariantError, ProjectHistoryError) as exc:
            raise self._raise_user_error(exc) from exc

    def set_mode(self, mode: EditorMode | str) -> EditorWorkspaceState:
        try:
            normalized = mode if isinstance(mode, EditorMode) else EditorMode(mode)
        except (TypeError, ValueError) as exc:
            raise EditorCommandError("Неизвестный режим редактора.") from exc
        return self._set_workspace(mode=normalized)

    def set_grid(
        self, visible: bool, *, grid_size: float | None = None
    ) -> EditorWorkspaceState:
        values: dict[str, Any] = {"grid_visible": visible}
        if grid_size is not None:
            values["grid_size"] = grid_size
        return self._set_workspace(**values)

    def set_snap(self, enabled: bool) -> EditorWorkspaceState:
        return self._set_workspace(snap_enabled=enabled)

    def set_label_display(
        self,
        *,
        show_labels: bool | None = None,
        show_parameters: bool | None = None,
        show_results: bool | None = None,
    ) -> EditorWorkspaceState:
        """Persist display preferences, including in analysis mode.

        These are view settings like zoom, not an electrical edit or a separate
        undo record. Results is a reserved B4 visibility preference only.
        """

        changes = {
            key: value
            for key, value in (
                ("show_labels", show_labels),
                ("show_parameters", show_parameters),
                ("show_results", show_results),
            )
            if value is not None
        }
        return self._set_workspace(**changes) if changes else self.workspace_state

    def set_view(
        self,
        zoom: float,
        x: float | None = None,
        y: float | None = None,
        *,
        view_x: float | None = None,
        view_y: float | None = None,
    ) -> EditorWorkspaceState:
        """Сохранить масштаб и pan; принимает и Qt-позиционные x/y, и keywords."""
        resolved_x = view_x if view_x is not None else x
        resolved_y = view_y if view_y is not None else y
        if resolved_x is None or resolved_y is None:
            raise EditorCommandError(
                "Для области просмотра необходимо задать координаты X и Y."
            )
        return self._set_workspace(
            zoom=zoom, view_x=resolved_x, view_y=resolved_y
        )

    def set_open_panels(self, panels: Iterable[str]) -> EditorWorkspaceState:
        return self._set_workspace(open_panels=tuple(panels))

    def set_developer_diagnostics(self, enabled: bool) -> EditorWorkspaceState:
        return self._set_workspace(developer_diagnostics=enabled)

    def set_confirm_switching(self, enabled: bool) -> EditorWorkspaceState:
        return self._set_workspace(confirm_switching=enabled)

    def set_active_page(self, page_id: PageId | str) -> EditorWorkspaceState:
        selected = self._page_id(page_id)
        if selected not in self.diagram.pages:
            raise EditorCommandError(tr("error.page_missing"))
        return self._set_workspace(active_page_id=selected.value)


__all__ = [
    "AddedElement",
    "AddedNode",
    "ClipboardLineGroup",
    "DeleteResult",
    "EditorCommandError",
    "PasteResult",
    "ProjectClipboard",
    "ProjectEditorController",
]
