# -*- coding: utf-8 -*-
"""Immutable public records returned by the Stage-2 topology engine.

This module deliberately contains no compiler and keeps no reference to the
mutable :class:`~rza_calc.domain.electrical.ElectricalModel`.  A snapshot is a
self-contained, deeply immutable result which may safely be shared by the UI,
calculation adapters and diagnostics code.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping, TypeVar

from rza_calc.domain.electrical import (
    ConnectionId,
    ElectricalNodeId,
    EquipmentAvailability,
    EquipmentId,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)


class TopologyError(Exception):
    """Base class for public topology errors."""


class TopologyQueryError(TopologyError, LookupError):
    """A query used an ID which does not belong to this snapshot."""


class TopologyCompatibilityError(TopologyError, ValueError):
    """A snapshot was supplied for another model, revision or state."""


class DiagnosticSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class Conductivity(StrEnum):
    CONDUCTING = "CONDUCTING"
    OPEN = "OPEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INVALID = "INVALID"


class Energization(StrEnum):
    ENERGIZED = "ENERGIZED"
    DEENERGIZED = "DEENERGIZED"
    UNKNOWN = "UNKNOWN"


class VoltageStatus(StrEnum):
    RESOLVED = "RESOLVED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class StateOrigin(StrEnum):
    EXPLICIT_STATE = "EXPLICIT_STATE"
    NORMAL_POSITION = "NORMAL_POSITION"
    LEGACY_COMPAT = "LEGACY_COMPAT"
    DEFAULT_AVAILABILITY = "DEFAULT_AVAILABILITY"


class SourceKind(StrEnum):
    EXTERNAL_GRID = "EXTERNAL_GRID"
    GENERATOR = "GENERATOR"


def _require_text(value: object, field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "string" if allow_empty else "non-empty string"
        raise TypeError(f"{field_name} must be a {qualifier}.")


def _coerce_enum(value: object, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be {enum_type.__name__}.") from exc


def _id_key(value: object) -> tuple[str, str]:
    return (type(value).__name__, getattr(value, "value", str(value)))


_T = TypeVar("_T")


def _unique_sorted(values: Iterable[_T]) -> tuple[_T, ...]:
    return tuple(sorted(set(values), key=_id_key))


def _ordered_mapping(values: Mapping[_T, Any]) -> Mapping[_T, Any]:
    return MappingProxyType(
        {key: values[key] for key in sorted(values, key=_id_key)}
    )


@dataclass(frozen=True, slots=True, order=True)
class TopologyComponentId:
    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "TopologyComponentId.value")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class TopologyLinkId:
    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "TopologyLinkId.value")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class VoltageZoneId:
    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "VoltageZoneId.value")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TopologyDiagnostic:
    severity: DiagnosticSeverity
    code: str
    message: str
    equipment_id: EquipmentId | None = None
    port_id: PortId | None = None
    electrical_node_id: ElectricalNodeId | None = None
    connection_id: ConnectionId | None = None
    related_equipment_ids: tuple[EquipmentId, ...] = ()
    related_port_ids: tuple[PortId, ...] = ()
    related_node_ids: tuple[ElectricalNodeId, ...] = ()
    related_connection_ids: tuple[ConnectionId, ...] = ()
    context: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "severity",
            _coerce_enum(self.severity, DiagnosticSeverity, "severity"),
        )
        _require_text(self.code, "TopologyDiagnostic.code")
        _require_text(self.message, "TopologyDiagnostic.message")
        object.__setattr__(
            self, "related_equipment_ids", _unique_sorted(self.related_equipment_ids)
        )
        object.__setattr__(
            self, "related_port_ids", _unique_sorted(self.related_port_ids)
        )
        object.__setattr__(
            self, "related_node_ids", _unique_sorted(self.related_node_ids)
        )
        object.__setattr__(
            self,
            "related_connection_ids",
            _unique_sorted(self.related_connection_ids),
        )
        frozen_context: dict[str, str] = {}
        for key, value in self.context.items():
            _require_text(key, "TopologyDiagnostic.context key")
            if not isinstance(value, str):
                raise TypeError("TopologyDiagnostic.context values must be strings.")
            frozen_context[key] = value
        object.__setattr__(
            self,
            "context",
            MappingProxyType(dict(sorted(frozen_context.items()))),
        )

    @property
    def is_blocking(self) -> bool:
        return self.severity is DiagnosticSeverity.ERROR

    def sort_key(self) -> tuple[str, ...]:
        return (
            self.severity.value,
            self.code,
            self.equipment_id.value if self.equipment_id else "",
            self.port_id.value if self.port_id else "",
            self.electrical_node_id.value if self.electrical_node_id else "",
            self.connection_id.value if self.connection_id else "",
            self.message,
        )


@dataclass(frozen=True, slots=True)
class ResolvedPosition:
    equipment_id: EquipmentId
    position: SwitchPosition | None
    conductivity: Conductivity
    origin: StateOrigin
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.equipment_id, EquipmentId):
            raise TypeError("ResolvedPosition.equipment_id must be EquipmentId.")
        if self.position is not None:
            object.__setattr__(
                self,
                "position",
                _coerce_enum(self.position, SwitchPosition, "position"),
            )
        object.__setattr__(
            self,
            "conductivity",
            _coerce_enum(self.conductivity, Conductivity, "conductivity"),
        )
        object.__setattr__(
            self, "origin", _coerce_enum(self.origin, StateOrigin, "origin")
        )
        _require_text(self.reason, "ResolvedPosition.reason", allow_empty=True)


@dataclass(frozen=True, slots=True)
class ResolvedOperatingState:
    selection_kind: str
    state_id: OperatingStateId | None = None
    state_name: str = ""
    system: str | None = None
    positions: Mapping[EquipmentId, ResolvedPosition] = field(default_factory=dict)
    availability: Mapping[EquipmentId, EquipmentAvailability] = field(
        default_factory=dict
    )
    availability_origins: Mapping[EquipmentId, StateOrigin] = field(
        default_factory=dict
    )
    topology_signature: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.selection_kind, "ResolvedOperatingState.selection_kind")
        _require_text(
            self.state_name, "ResolvedOperatingState.state_name", allow_empty=True
        )
        if self.system is not None:
            _require_text(self.system, "ResolvedOperatingState.system")

        positions: dict[EquipmentId, ResolvedPosition] = {}
        for equipment_id, position in self.positions.items():
            if not isinstance(equipment_id, EquipmentId):
                raise TypeError("ResolvedOperatingState.positions uses EquipmentId keys.")
            if not isinstance(position, ResolvedPosition):
                raise TypeError("ResolvedOperatingState.positions values must be ResolvedPosition.")
            if position.equipment_id != equipment_id:
                raise ValueError("ResolvedPosition does not match its mapping key.")
            positions[equipment_id] = position

        availability: dict[EquipmentId, EquipmentAvailability] = {}
        for equipment_id, value in self.availability.items():
            if not isinstance(equipment_id, EquipmentId):
                raise TypeError("ResolvedOperatingState.availability uses EquipmentId keys.")
            availability[equipment_id] = _coerce_enum(
                value, EquipmentAvailability, "availability"
            )

        origins: dict[EquipmentId, StateOrigin] = {}
        for equipment_id, value in self.availability_origins.items():
            if not isinstance(equipment_id, EquipmentId):
                raise TypeError(
                    "ResolvedOperatingState.availability_origins uses EquipmentId keys."
                )
            origins[equipment_id] = _coerce_enum(
                value, StateOrigin, "availability origin"
            )
        if set(origins) - set(availability):
            raise ValueError("Availability origin exists without an availability value.")

        signature = tuple(self.topology_signature)
        if any(not isinstance(part, str) for part in signature):
            raise TypeError("ResolvedOperatingState.topology_signature must contain strings.")
        object.__setattr__(self, "positions", _ordered_mapping(positions))
        object.__setattr__(self, "availability", _ordered_mapping(availability))
        object.__setattr__(self, "availability_origins", _ordered_mapping(origins))
        object.__setattr__(self, "topology_signature", signature)


@dataclass(frozen=True, slots=True)
class VoltageEvidence:
    voltage_class_id: VoltageClassId
    origin: str
    electrical_node_id: ElectricalNodeId | None = None
    equipment_id: EquipmentId | None = None
    port_id: PortId | None = None
    voltage_group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.voltage_class_id, VoltageClassId):
            raise TypeError("VoltageEvidence.voltage_class_id must be VoltageClassId.")
        _require_text(self.origin, "VoltageEvidence.origin")
        if self.voltage_group is not None:
            _require_text(self.voltage_group, "VoltageEvidence.voltage_group")

    def sort_key(self) -> tuple[str, ...]:
        return (
            self.voltage_class_id.value,
            self.origin,
            self.electrical_node_id.value if self.electrical_node_id else "",
            self.equipment_id.value if self.equipment_id else "",
            self.port_id.value if self.port_id else "",
            self.voltage_group or "",
        )


@dataclass(frozen=True, slots=True)
class VoltageResolution:
    status: VoltageStatus
    voltage_class_id: VoltageClassId | None = None
    candidate_voltage_class_ids: tuple[VoltageClassId, ...] = ()
    evidence: tuple[VoltageEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _coerce_enum(self.status, VoltageStatus, "status")
        )
        candidates = set(self.candidate_voltage_class_ids)
        if self.voltage_class_id is not None:
            if not isinstance(self.voltage_class_id, VoltageClassId):
                raise TypeError("voltage_class_id must be VoltageClassId.")
            candidates.add(self.voltage_class_id)
        if any(not isinstance(item, VoltageClassId) for item in candidates):
            raise TypeError("candidate_voltage_class_ids must contain VoltageClassId.")
        evidence = tuple(sorted(self.evidence, key=lambda item: item.sort_key()))
        if any(not isinstance(item, VoltageEvidence) for item in evidence):
            raise TypeError("VoltageResolution.evidence must contain VoltageEvidence.")
        if self.status is VoltageStatus.RESOLVED and self.voltage_class_id is None:
            raise ValueError("A resolved voltage requires voltage_class_id.")
        if self.status is not VoltageStatus.RESOLVED and self.voltage_class_id is not None:
            raise ValueError("Only RESOLVED voltage may expose voltage_class_id.")
        object.__setattr__(
            self, "candidate_voltage_class_ids", _unique_sorted(candidates)
        )
        object.__setattr__(self, "evidence", evidence)

    @property
    def voltage_class_ids(self) -> tuple[VoltageClassId, ...]:
        """All candidate classes (one for RESOLVED, several for CONFLICT)."""
        return self.candidate_voltage_class_ids


@dataclass(frozen=True, slots=True)
class VoltageZone:
    id: VoltageZoneId
    resolution: VoltageResolution
    node_ids: tuple[ElectricalNodeId, ...] = ()
    port_ids: tuple[PortId, ...] = ()
    equipment_groups: tuple[tuple[EquipmentId, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, VoltageZoneId):
            raise TypeError("VoltageZone.id must be VoltageZoneId.")
        if not isinstance(self.resolution, VoltageResolution):
            raise TypeError("VoltageZone.resolution must be VoltageResolution.")
        object.__setattr__(self, "node_ids", _unique_sorted(self.node_ids))
        object.__setattr__(self, "port_ids", _unique_sorted(self.port_ids))
        groups: set[tuple[EquipmentId, str]] = set()
        for equipment_id, group in self.equipment_groups:
            if not isinstance(equipment_id, EquipmentId):
                raise TypeError("VoltageZone equipment group uses EquipmentId.")
            _require_text(group, "VoltageZone voltage group")
            groups.add((equipment_id, group))
        object.__setattr__(
            self,
            "equipment_groups",
            tuple(sorted(groups, key=lambda item: (item[0].value, item[1]))),
        )


@dataclass(frozen=True, slots=True)
class TopologyLink:
    id: TopologyLinkId
    equipment_id: EquipmentId
    behavior_key: str
    port_ids: tuple[PortId, ...]
    node_ids: tuple[ElectricalNodeId, ...]
    active_port_ids: tuple[PortId, ...]
    active_node_ids: tuple[ElectricalNodeId, ...]
    active: bool
    conductivity: Conductivity
    voltage_boundary: bool = False
    resolved_position: ResolvedPosition | None = None
    availability: EquipmentAvailability = EquipmentAvailability.IN_SERVICE

    def __post_init__(self) -> None:
        if not isinstance(self.id, TopologyLinkId):
            raise TypeError("TopologyLink.id must be TopologyLinkId.")
        if not isinstance(self.equipment_id, EquipmentId):
            raise TypeError("TopologyLink.equipment_id must be EquipmentId.")
        _require_text(self.behavior_key, "TopologyLink.behavior_key")
        if not isinstance(self.active, bool):
            raise TypeError("TopologyLink.active must be bool.")
        if not isinstance(self.voltage_boundary, bool):
            raise TypeError("TopologyLink.voltage_boundary must be bool.")
        ports = _unique_sorted(self.port_ids)
        nodes = _unique_sorted(self.node_ids)
        active_ports = _unique_sorted(self.active_port_ids)
        active_nodes = _unique_sorted(self.active_node_ids)
        if not set(active_ports) <= set(ports):
            raise ValueError("active_port_ids must be a subset of port_ids.")
        if not set(active_nodes) <= set(nodes):
            raise ValueError("active_node_ids must be a subset of node_ids.")
        if not self.active and (active_ports or active_nodes):
            raise ValueError("An inactive link cannot expose active terminals.")
        if self.resolved_position is not None:
            if not isinstance(self.resolved_position, ResolvedPosition):
                raise TypeError("resolved_position must be ResolvedPosition.")
            if self.resolved_position.equipment_id != self.equipment_id:
                raise ValueError("resolved_position belongs to another equipment item.")
        object.__setattr__(
            self,
            "conductivity",
            _coerce_enum(self.conductivity, Conductivity, "conductivity"),
        )
        object.__setattr__(
            self,
            "availability",
            _coerce_enum(self.availability, EquipmentAvailability, "availability"),
        )
        object.__setattr__(self, "port_ids", ports)
        object.__setattr__(self, "node_ids", nodes)
        object.__setattr__(self, "active_port_ids", active_ports)
        object.__setattr__(self, "active_node_ids", active_nodes)

    @property
    def electrical_node_ids(self) -> tuple[ElectricalNodeId, ...]:
        return self.node_ids

    @property
    def active_electrical_node_ids(self) -> tuple[ElectricalNodeId, ...]:
        return self.active_node_ids


@dataclass(frozen=True, slots=True)
class TopologySource:
    equipment_id: EquipmentId
    kind: SourceKind
    port_id: PortId
    node_id: ElectricalNodeId
    active: bool
    availability: EquipmentAvailability = EquipmentAvailability.IN_SERVICE
    voltage_class_id: VoltageClassId | None = None
    component_id: TopologyComponentId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.equipment_id, EquipmentId):
            raise TypeError("TopologySource.equipment_id must be EquipmentId.")
        if not isinstance(self.port_id, PortId):
            raise TypeError("TopologySource.port_id must be PortId.")
        if not isinstance(self.node_id, ElectricalNodeId):
            raise TypeError("TopologySource.node_id must be ElectricalNodeId.")
        if not isinstance(self.active, bool):
            raise TypeError("TopologySource.active must be bool.")
        object.__setattr__(self, "kind", _coerce_enum(self.kind, SourceKind, "kind"))
        object.__setattr__(
            self,
            "availability",
            _coerce_enum(self.availability, EquipmentAvailability, "availability"),
        )

    @property
    def source_kind(self) -> SourceKind:
        return self.kind


@dataclass(frozen=True, slots=True)
class TopologyComponent:
    id: TopologyComponentId
    node_ids: tuple[ElectricalNodeId, ...]
    link_ids: tuple[TopologyLinkId, ...]
    equipment_ids: tuple[EquipmentId, ...]
    source_equipment_ids: tuple[EquipmentId, ...]
    energization: Energization
    is_meshed: bool = False
    cycle_rank: int = 0
    parallel_link_groups: tuple[tuple[TopologyLinkId, ...], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, TopologyComponentId):
            raise TypeError("TopologyComponent.id must be TopologyComponentId.")
        if not isinstance(self.is_meshed, bool):
            raise TypeError("TopologyComponent.is_meshed must be bool.")
        if isinstance(self.cycle_rank, bool) or not isinstance(self.cycle_rank, int):
            raise TypeError("TopologyComponent.cycle_rank must be int.")
        if self.cycle_rank < 0:
            raise ValueError("TopologyComponent.cycle_rank cannot be negative.")
        object.__setattr__(
            self,
            "energization",
            _coerce_enum(self.energization, Energization, "energization"),
        )
        object.__setattr__(self, "node_ids", _unique_sorted(self.node_ids))
        object.__setattr__(self, "link_ids", _unique_sorted(self.link_ids))
        object.__setattr__(self, "equipment_ids", _unique_sorted(self.equipment_ids))
        object.__setattr__(
            self,
            "source_equipment_ids",
            _unique_sorted(self.source_equipment_ids),
        )
        groups = {
            _unique_sorted(group) for group in self.parallel_link_groups if len(group) > 1
        }
        object.__setattr__(
            self,
            "parallel_link_groups",
            tuple(sorted(groups, key=lambda group: tuple(item.value for item in group))),
        )

    @property
    def source_ids(self) -> tuple[EquipmentId, ...]:
        return self.source_equipment_ids


@dataclass(frozen=True, slots=True)
class TopologyNeighbor:
    node_id: ElectricalNodeId
    link_id: TopologyLinkId
    equipment_id: EquipmentId

    @property
    def via_link_id(self) -> TopologyLinkId:
        return self.link_id


@dataclass(frozen=True, slots=True)
class TopologyPathStep:
    from_node_id: ElectricalNodeId
    to_node_id: ElectricalNodeId
    link_id: TopologyLinkId
    equipment_id: EquipmentId


@dataclass(frozen=True, slots=True)
class TopologyPath:
    start_node_id: ElectricalNodeId
    end_node_id: ElectricalNodeId
    steps: tuple[TopologyPathStep, ...] = ()

    def __post_init__(self) -> None:
        steps = tuple(self.steps)
        current = self.start_node_id
        for step in steps:
            if not isinstance(step, TopologyPathStep):
                raise TypeError("TopologyPath.steps must contain TopologyPathStep.")
            if step.from_node_id != current:
                raise ValueError("TopologyPath.steps are not contiguous.")
            current = step.to_node_id
        if current != self.end_node_id:
            raise ValueError("TopologyPath does not terminate at end_node_id.")
        object.__setattr__(self, "steps", steps)

    @property
    def node_ids(self) -> tuple[ElectricalNodeId, ...]:
        return (self.start_node_id,) + tuple(step.to_node_id for step in self.steps)

    @property
    def link_ids(self) -> tuple[TopologyLinkId, ...]:
        return tuple(step.link_id for step in self.steps)

    @property
    def equipment_ids(self) -> tuple[EquipmentId, ...]:
        return tuple(step.equipment_id for step in self.steps)

    def __len__(self) -> int:
        return len(self.steps)


@dataclass(frozen=True, slots=True)
class FeederView:
    source_equipment_id: EquipmentId
    root_node_id: ElectricalNodeId
    node_ids: tuple[ElectricalNodeId, ...]
    parent_by_node: Mapping[ElectricalNodeId, TopologyPathStep]
    depth_by_node: Mapping[ElectricalNodeId, int]
    tree_link_ids: tuple[TopologyLinkId, ...]
    non_tree_link_ids: tuple[TopologyLinkId, ...]

    def __post_init__(self) -> None:
        nodes = _unique_sorted(self.node_ids)
        if self.root_node_id not in nodes:
            raise ValueError("FeederView root must be included in node_ids.")
        parent = dict(self.parent_by_node)
        depths = dict(self.depth_by_node)
        if self.root_node_id in parent:
            raise ValueError("FeederView root cannot have a parent.")
        if set(parent) != set(nodes) - {self.root_node_id}:
            raise ValueError("Every non-root feeder node must have exactly one parent.")
        if set(depths) != set(nodes) or depths.get(self.root_node_id) != 0:
            raise ValueError("FeederView depths must cover all nodes and root depth is zero.")
        object.__setattr__(self, "node_ids", nodes)
        object.__setattr__(self, "parent_by_node", _ordered_mapping(parent))
        object.__setattr__(self, "depth_by_node", _ordered_mapping(depths))
        object.__setattr__(self, "tree_link_ids", _unique_sorted(self.tree_link_ids))
        object.__setattr__(
            self, "non_tree_link_ids", _unique_sorted(self.non_tree_link_ids)
        )

    def path_to(self, node_id: ElectricalNodeId) -> TopologyPath:
        if node_id not in self.depth_by_node:
            raise TopologyQueryError(f"Unknown feeder node '{node_id}'.")
        reversed_steps: list[TopologyPathStep] = []
        current = node_id
        while current != self.root_node_id:
            step = self.parent_by_node[current]
            reversed_steps.append(step)
            current = step.from_node_id
        return TopologyPath(
            self.root_node_id,
            node_id,
            tuple(reversed(reversed_steps)),
        )


@dataclass(frozen=True, slots=True)
class TopologySnapshot:
    """A complete immutable view of one model revision and operating state."""

    model_identity: str
    model_revision: int
    model_fingerprint: str
    registry_signature: str
    operating_state: ResolvedOperatingState
    node_ids: tuple[ElectricalNodeId, ...]
    port_to_node: Mapping[PortId, ElectricalNodeId]
    links: Mapping[TopologyLinkId, TopologyLink]
    link_ids_by_equipment: Mapping[EquipmentId, tuple[TopologyLinkId, ...]]
    sources: Mapping[EquipmentId, TopologySource]
    components: Mapping[TopologyComponentId, TopologyComponent]
    component_by_node: Mapping[ElectricalNodeId, TopologyComponentId]
    voltage_zones: Mapping[VoltageZoneId, VoltageZone]
    voltage_zone_by_node: Mapping[ElectricalNodeId, VoltageZoneId]
    voltage_zone_by_port: Mapping[PortId, VoltageZoneId]
    voltage_zone_by_equipment_group: Mapping[
        tuple[EquipmentId, str], VoltageZoneId
    ]
    diagnostics: tuple[TopologyDiagnostic, ...] = ()
    _adjacency: Mapping[ElectricalNodeId, tuple[TopologyNeighbor, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _require_text(self.model_identity, "TopologySnapshot.model_identity")
        _require_text(self.model_fingerprint, "TopologySnapshot.model_fingerprint")
        _require_text(
            self.registry_signature,
            "TopologySnapshot.registry_signature",
            allow_empty=True,
        )
        if isinstance(self.model_revision, bool) or not isinstance(
            self.model_revision, int
        ):
            raise TypeError("TopologySnapshot.model_revision must be int.")
        if self.model_revision < 0:
            raise ValueError("TopologySnapshot.model_revision cannot be negative.")
        if not isinstance(self.operating_state, ResolvedOperatingState):
            raise TypeError("operating_state must be ResolvedOperatingState.")

        nodes = _unique_sorted(self.node_ids)
        node_set = set(nodes)
        port_to_node = dict(self.port_to_node)
        if any(node_id not in node_set for node_id in port_to_node.values()):
            raise ValueError("port_to_node references a node outside node_ids.")

        links = dict(self.links)
        for link_id, link in links.items():
            if not isinstance(link_id, TopologyLinkId) or not isinstance(
                link, TopologyLink
            ):
                raise TypeError("TopologySnapshot.links has invalid key or value.")
            if link.id != link_id:
                raise ValueError("TopologyLink does not match its mapping key.")
            if not set(link.node_ids) <= node_set:
                raise ValueError("TopologyLink references a node outside node_ids.")

        by_equipment: dict[EquipmentId, tuple[TopologyLinkId, ...]] = {}
        for equipment_id, link_ids in self.link_ids_by_equipment.items():
            normalized = _unique_sorted(link_ids)
            if any(link_id not in links for link_id in normalized):
                raise ValueError("link_ids_by_equipment references an unknown link.")
            if any(links[link_id].equipment_id != equipment_id for link_id in normalized):
                raise ValueError("link_ids_by_equipment references another equipment item.")
            by_equipment[equipment_id] = normalized
        expected_by_equipment: dict[EquipmentId, set[TopologyLinkId]] = {}
        for link_id, link in links.items():
            expected_by_equipment.setdefault(link.equipment_id, set()).add(link_id)
        if {
            key: set(value) for key, value in by_equipment.items()
        } != expected_by_equipment:
            raise ValueError("link_ids_by_equipment must index every topology link.")

        sources = dict(self.sources)
        for equipment_id, source in sources.items():
            if source.equipment_id != equipment_id:
                raise ValueError("TopologySource does not match its mapping key.")
            if source.node_id not in node_set:
                raise ValueError("TopologySource references a node outside node_ids.")

        components = dict(self.components)
        for component_id, component in components.items():
            if component.id != component_id:
                raise ValueError("TopologyComponent does not match its mapping key.")
            if not set(component.node_ids) <= node_set:
                raise ValueError("TopologyComponent references a node outside node_ids.")
            if not set(component.link_ids) <= set(links):
                raise ValueError("TopologyComponent references an unknown link.")
            if not set(component.source_equipment_ids) <= set(sources):
                raise ValueError("TopologyComponent references an unknown source.")

        component_by_node = dict(self.component_by_node)
        if set(component_by_node) != node_set:
            raise ValueError("component_by_node must cover every topology node.")
        for node_id, component_id in component_by_node.items():
            component = components.get(component_id)
            if component is None or node_id not in component.node_ids:
                raise ValueError("component_by_node is inconsistent with components.")

        zones = dict(self.voltage_zones)
        for zone_id, zone in zones.items():
            if zone.id != zone_id:
                raise ValueError("VoltageZone does not match its mapping key.")
        zone_by_node = dict(self.voltage_zone_by_node)
        zone_by_port = dict(self.voltage_zone_by_port)
        zone_by_group = dict(self.voltage_zone_by_equipment_group)
        for lookup in (zone_by_node, zone_by_port, zone_by_group):
            if any(zone_id not in zones for zone_id in lookup.values()):
                raise ValueError("Voltage lookup references an unknown zone.")
        if any(node_id not in node_set for node_id in zone_by_node):
            raise ValueError("voltage_zone_by_node references an unknown node.")

        diagnostics = tuple(sorted(self.diagnostics, key=lambda item: item.sort_key()))
        if any(not isinstance(item, TopologyDiagnostic) for item in diagnostics):
            raise TypeError("diagnostics must contain TopologyDiagnostic.")

        adjacency: dict[ElectricalNodeId, list[TopologyNeighbor]] = {
            node_id: [] for node_id in nodes
        }
        for link in links.values():
            if not link.active:
                continue
            active_nodes = _unique_sorted(link.active_node_ids)
            for from_node in active_nodes:
                for to_node in active_nodes:
                    if from_node == to_node:
                        continue
                    adjacency[from_node].append(
                        TopologyNeighbor(to_node, link.id, link.equipment_id)
                    )
        frozen_adjacency = MappingProxyType(
            {
                node_id: tuple(
                    sorted(
                        values,
                        key=lambda item: (
                            item.node_id.value,
                            item.equipment_id.value,
                            item.link_id.value,
                        ),
                    )
                )
                for node_id, values in sorted(
                    adjacency.items(), key=lambda item: item[0].value
                )
            }
        )

        object.__setattr__(self, "node_ids", nodes)
        object.__setattr__(self, "port_to_node", _ordered_mapping(port_to_node))
        object.__setattr__(self, "links", _ordered_mapping(links))
        object.__setattr__(self, "link_ids_by_equipment", _ordered_mapping(by_equipment))
        object.__setattr__(self, "sources", _ordered_mapping(sources))
        object.__setattr__(self, "components", _ordered_mapping(components))
        object.__setattr__(self, "component_by_node", _ordered_mapping(component_by_node))
        object.__setattr__(self, "voltage_zones", _ordered_mapping(zones))
        object.__setattr__(self, "voltage_zone_by_node", _ordered_mapping(zone_by_node))
        object.__setattr__(self, "voltage_zone_by_port", _ordered_mapping(zone_by_port))
        object.__setattr__(
            self,
            "voltage_zone_by_equipment_group",
            MappingProxyType(
                {
                    key: zone_by_group[key]
                    for key in sorted(
                        zone_by_group, key=lambda item: (item[0].value, item[1])
                    )
                }
            ),
        )
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "_adjacency", frozen_adjacency)

    @property
    def is_valid(self) -> bool:
        return not any(item.is_blocking for item in self.diagnostics)

    @property
    def operating_state_id(self) -> OperatingStateId | None:
        return self.operating_state.state_id

    @property
    def resolved_state(self) -> ResolvedOperatingState:
        return self.operating_state

    @property
    def active_links(self) -> tuple[TopologyLink, ...]:
        return tuple(link for link in self.links.values() if link.active)

    @property
    def islands(self) -> tuple[TopologyComponent, ...]:
        return tuple(self.components.values())

    @property
    def component_id_by_node(
        self,
    ) -> Mapping[ElectricalNodeId, TopologyComponentId]:
        return self.component_by_node

    @property
    def voltage_by_node(self) -> Mapping[ElectricalNodeId, VoltageResolution]:
        return MappingProxyType(
            {
                node_id: self.voltage_zones[zone_id].resolution
                for node_id, zone_id in self.voltage_zone_by_node.items()
            }
        )

    @property
    def source_reachability(
        self,
    ) -> Mapping[EquipmentId, frozenset[ElectricalNodeId]]:
        return MappingProxyType(
            {
                source_id: frozenset(paths)
                for source_id in self.sources
                for paths in (self.paths_from_source(source_id),)
            }
        )

    def require_valid(self) -> TopologySnapshot:
        if not self.is_valid:
            raise TopologyCompilationError(self)
        return self

    def _require_node(self, node_id: ElectricalNodeId) -> None:
        if not isinstance(node_id, ElectricalNodeId) or node_id not in self.component_by_node:
            raise TopologyQueryError(f"Unknown topology node '{node_id}'.")

    def component_of(self, node_id: ElectricalNodeId) -> TopologyComponent:
        self._require_node(node_id)
        return self.components[self.component_by_node[node_id]]

    def voltage_zone_of(
        self,
        object_id: ElectricalNodeId | PortId | tuple[EquipmentId, str],
    ) -> VoltageZone:
        if isinstance(object_id, ElectricalNodeId):
            zone_id = self.voltage_zone_by_node.get(object_id)
        elif isinstance(object_id, PortId):
            zone_id = self.voltage_zone_by_port.get(object_id)
        elif (
            isinstance(object_id, tuple)
            and len(object_id) == 2
            and isinstance(object_id[0], EquipmentId)
            and isinstance(object_id[1], str)
        ):
            zone_id = self.voltage_zone_by_equipment_group.get(object_id)
        else:
            raise TopologyQueryError(f"Unsupported voltage-zone key '{object_id}'.")
        if zone_id is None:
            raise TopologyQueryError(f"No voltage zone for '{object_id}'.")
        return self.voltage_zones[zone_id]

    def sources_for(
        self, object_id: ElectricalNodeId | TopologyComponentId
    ) -> tuple[EquipmentId, ...]:
        if isinstance(object_id, ElectricalNodeId):
            component = self.component_of(object_id)
        elif isinstance(object_id, TopologyComponentId):
            component = self.components.get(object_id)
            if component is None:
                raise TopologyQueryError(f"Unknown topology component '{object_id}'.")
        else:
            raise TopologyQueryError(f"Unsupported component key '{object_id}'.")
        return component.source_equipment_ids

    def source_records_for(
        self, object_id: ElectricalNodeId | TopologyComponentId
    ) -> tuple[TopologySource, ...]:
        return tuple(self.sources[source_id] for source_id in self.sources_for(object_id))

    def energization_of(
        self, object_id: ElectricalNodeId | TopologyComponentId
    ) -> Energization:
        if isinstance(object_id, ElectricalNodeId):
            return self.component_of(object_id).energization
        component = self.components.get(object_id)
        if component is None:
            raise TopologyQueryError(f"Unknown topology component '{object_id}'.")
        return component.energization

    def is_energized(
        self, object_id: ElectricalNodeId | TopologyComponentId
    ) -> bool | None:
        value = self.energization_of(object_id)
        if value is Energization.UNKNOWN:
            return None
        return value is Energization.ENERGIZED

    def neighbors(self, node_id: ElectricalNodeId) -> tuple[TopologyNeighbor, ...]:
        self._require_node(node_id)
        return self._adjacency[node_id]

    def links_between(
        self,
        first: ElectricalNodeId,
        second: ElectricalNodeId,
        *,
        active_only: bool = False,
    ) -> tuple[TopologyLink, ...]:
        self._require_node(first)
        self._require_node(second)
        links = (
            link
            for link in self.links.values()
            if first in link.node_ids
            and second in link.node_ids
            and (
                not active_only
                or (
                    link.active
                    and first in link.active_node_ids
                    and second in link.active_node_ids
                )
            )
        )
        return tuple(sorted(links, key=lambda item: item.id.value))

    def edges_between(
        self,
        first: ElectricalNodeId,
        second: ElectricalNodeId,
        *,
        active_only: bool = False,
    ) -> tuple[TopologyLink, ...]:
        return self.links_between(first, second, active_only=active_only)

    def has_path(self, first: ElectricalNodeId, second: ElectricalNodeId) -> bool:
        return self.shortest_path(first, second) is not None

    def shortest_path(
        self, first: ElectricalNodeId, second: ElectricalNodeId
    ) -> TopologyPath | None:
        self._require_node(first)
        self._require_node(second)
        if first == second:
            return TopologyPath(first, second)
        if self.component_by_node[first] != self.component_by_node[second]:
            return None

        parents: dict[ElectricalNodeId, TopologyPathStep] = {}
        seen = {first}
        queue: deque[ElectricalNodeId] = deque((first,))
        while queue:
            current = queue.popleft()
            for neighbor in self._adjacency[current]:
                if neighbor.node_id in seen:
                    continue
                seen.add(neighbor.node_id)
                parents[neighbor.node_id] = TopologyPathStep(
                    current,
                    neighbor.node_id,
                    neighbor.link_id,
                    neighbor.equipment_id,
                )
                if neighbor.node_id == second:
                    return self._path_from_parents(first, second, parents)
                queue.append(neighbor.node_id)
        return None

    def find_path(
        self, first: ElectricalNodeId, second: ElectricalNodeId
    ) -> TopologyPath | None:
        return self.shortest_path(first, second)

    @staticmethod
    def _path_from_parents(
        root: ElectricalNodeId,
        target: ElectricalNodeId,
        parents: Mapping[ElectricalNodeId, TopologyPathStep],
    ) -> TopologyPath:
        reversed_steps: list[TopologyPathStep] = []
        current = target
        while current != root:
            step = parents[current]
            reversed_steps.append(step)
            current = step.from_node_id
        return TopologyPath(root, target, tuple(reversed(reversed_steps)))

    def source_paths(
        self, node_id: ElectricalNodeId
    ) -> Mapping[EquipmentId, TopologyPath]:
        """Return one deterministic shortest path from every active source."""
        self._require_node(node_id)
        paths: dict[EquipmentId, TopologyPath] = {}
        for source in self.source_records_for(node_id):
            if not source.active:
                continue
            path = self.shortest_path(source.node_id, node_id)
            if path is not None:
                paths[source.equipment_id] = path
        return _ordered_mapping(paths)

    def paths_from_source(
        self, source_equipment_id: EquipmentId
    ) -> Mapping[ElectricalNodeId, TopologyPath]:
        source = self.sources.get(source_equipment_id)
        if source is None:
            raise TopologyQueryError(f"Unknown topology source '{source_equipment_id}'.")
        if not source.active:
            return MappingProxyType({})
        component = self.component_of(source.node_id)
        paths: dict[ElectricalNodeId, TopologyPath] = {}
        for node_id in component.node_ids:
            path = self.shortest_path(source.node_id, node_id)
            if path is not None:
                paths[node_id] = path
        return _ordered_mapping(paths)

    def feeder_view(self, source_equipment_id: EquipmentId) -> FeederView:
        source = self.sources.get(source_equipment_id)
        if source is None:
            raise TopologyQueryError(f"Unknown topology source '{source_equipment_id}'.")
        if not source.active:
            raise TopologyQueryError(
                f"Source '{source_equipment_id}' is not active in this state."
            )
        root = source.node_id
        parents: dict[ElectricalNodeId, TopologyPathStep] = {}
        depths = {root: 0}
        seen = {root}
        queue: deque[ElectricalNodeId] = deque((root,))
        while queue:
            current = queue.popleft()
            for neighbor in self._adjacency[current]:
                if neighbor.node_id in seen:
                    continue
                seen.add(neighbor.node_id)
                depths[neighbor.node_id] = depths[current] + 1
                parents[neighbor.node_id] = TopologyPathStep(
                    current,
                    neighbor.node_id,
                    neighbor.link_id,
                    neighbor.equipment_id,
                )
                queue.append(neighbor.node_id)

        tree_links = {step.link_id for step in parents.values()}
        component = self.component_of(root)
        component_links = {
            link_id
            for link_id in component.link_ids
            if self.links[link_id].active
            and len(
                set(self.links[link_id].active_node_ids) & set(component.node_ids)
            ) >= 2
        }
        return FeederView(
            source_equipment_id=source_equipment_id,
            root_node_id=root,
            node_ids=tuple(seen),
            parent_by_node=parents,
            depth_by_node=depths,
            tree_link_ids=tuple(tree_links),
            non_tree_link_ids=tuple(component_links - tree_links),
        )

    def feeder_tree(self, source_equipment_id: EquipmentId) -> FeederView:
        return self.feeder_view(source_equipment_id)

    def semantic_signature(self) -> tuple[object, ...]:
        """Return a deterministic, name/coordinate-independent topology value."""
        port_signature = tuple(
            (port_id.value, node_id.value)
            for port_id, node_id in self.port_to_node.items()
        )
        link_signature = tuple(
            (
                link.id.value,
                link.equipment_id.value,
                link.behavior_key,
                tuple(item.value for item in link.port_ids),
                tuple(item.value for item in link.node_ids),
                link.active,
                link.conductivity.value,
                tuple(item.value for item in link.active_port_ids),
                tuple(item.value for item in link.active_node_ids),
                link.voltage_boundary,
                link.availability.value,
            )
            for link in self.links.values()
        )
        source_signature = tuple(
            (
                source.equipment_id.value,
                source.kind.value,
                source.node_id.value,
                source.active,
                source.availability.value,
            )
            for source in self.sources.values()
        )
        component_signature = tuple(
            (
                component.id.value,
                tuple(item.value for item in component.node_ids),
                tuple(item.value for item in component.link_ids),
                tuple(item.value for item in component.source_equipment_ids),
                component.energization.value,
                component.cycle_rank,
            )
            for component in self.components.values()
        )
        voltage_signature = tuple(
            (
                zone.id.value,
                zone.resolution.status.value,
                zone.resolution.voltage_class_id.value
                if zone.resolution.voltage_class_id
                else "",
                tuple(
                    item.value
                    for item in zone.resolution.candidate_voltage_class_ids
                ),
                tuple(item.value for item in zone.node_ids),
            )
            for zone in self.voltage_zones.values()
        )
        diagnostic_signature = tuple(
            (
                diagnostic.severity.value,
                diagnostic.code,
                diagnostic.equipment_id.value if diagnostic.equipment_id else "",
                diagnostic.port_id.value if diagnostic.port_id else "",
                diagnostic.electrical_node_id.value
                if diagnostic.electrical_node_id
                else "",
            )
            for diagnostic in self.diagnostics
        )
        return (
            self.operating_state.topology_signature,
            tuple(item.value for item in self.node_ids),
            port_signature,
            link_signature,
            source_signature,
            component_signature,
            voltage_signature,
            diagnostic_signature,
        )

    @property
    def topology_fingerprint(self) -> str:
        """SHA-256 of semantic electrical content, excluding names/revisions."""
        canonical = json.dumps(
            self.semantic_signature(),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def assert_compatible(
        self,
        *,
        model_identity: str,
        model_revision: int,
        model_fingerprint: str | None = None,
        registry_signature: str | None = None,
        topology_signature: tuple[str, ...] | None = None,
    ) -> None:
        """Reject reuse against a different mutable-model compilation input."""
        mismatches: list[str] = []
        if self.model_identity != model_identity:
            mismatches.append("model identity")
        if self.model_revision != model_revision:
            mismatches.append("model revision")
        if (
            model_fingerprint is not None
            and self.model_fingerprint != model_fingerprint
        ):
            mismatches.append("model content")
        if registry_signature is not None and self.registry_signature != registry_signature:
            mismatches.append("behavior registry")
        if (
            topology_signature is not None
            and self.operating_state.topology_signature != tuple(topology_signature)
        ):
            mismatches.append("operating state")
        if mismatches:
            raise TopologyCompatibilityError(
                "Topology snapshot is incompatible by " + ", ".join(mismatches) + "."
            )

    def assert_compatible_with(
        self,
        *,
        model_identity: str,
        model_revision: int,
        model_fingerprint: str | None = None,
        registry_signature: str | None = None,
        topology_signature: tuple[str, ...] | None = None,
    ) -> None:
        self.assert_compatible(
            model_identity=model_identity,
            model_revision=model_revision,
            model_fingerprint=model_fingerprint,
            registry_signature=registry_signature,
            topology_signature=topology_signature,
        )


class TopologyCompilationError(TopologyError):
    """Raised by :meth:`TopologySnapshot.require_valid` for blocker diagnostics."""

    def __init__(
        self,
        snapshot: TopologySnapshot,
        message: str | None = None,
    ) -> None:
        self.snapshot = snapshot
        blockers = sum(item.is_blocking for item in snapshot.diagnostics)
        super().__init__(
            message
            or f"Topology snapshot has {blockers} blocking diagnostic(s)."
        )


__all__ = [
    "Conductivity",
    "DiagnosticSeverity",
    "Energization",
    "FeederView",
    "ResolvedOperatingState",
    "ResolvedPosition",
    "SourceKind",
    "StateOrigin",
    "TopologyCompilationError",
    "TopologyCompatibilityError",
    "TopologyComponent",
    "TopologyComponentId",
    "TopologyDiagnostic",
    "TopologyError",
    "TopologyLink",
    "TopologyLinkId",
    "TopologyNeighbor",
    "TopologyPath",
    "TopologyPathStep",
    "TopologyQueryError",
    "TopologySnapshot",
    "TopologySource",
    "VoltageEvidence",
    "VoltageResolution",
    "VoltageStatus",
    "VoltageZone",
    "VoltageZoneId",
]
