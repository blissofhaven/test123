# -*- coding: utf-8 -*-
"""State-aware topology compiler for the canonical electrical Domain Model.

The compiler is intentionally independent from SVG, diagram coordinates and
the legacy calculation DTO.  It produces a deterministic immutable snapshot
from stable domain IDs and explicit behavior handlers.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any, Hashable, Iterable, Mapping, TypeVar
from weakref import WeakKeyDictionary

from ..domain.electrical import (
    EQUIPMENT_AVAILABILITY_CAPABILITY,
    Connection,
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentAvailability,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeDefinition,
    LogicalLine,
    OperatingState,
    OperatingStateId,
    PortDefinition,
    PortId,
    PortInstance,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)
from ..domain.fingerprint import electrical_model_fingerprint
from .handlers import (
    BehaviorKind,
    TopologyBehaviorHandler,
    TopologyBehaviorRegistry,
    builtin_topology_handlers,
)
from .model import (
    Conductivity,
    DiagnosticSeverity,
    Energization,
    ResolvedOperatingState,
    ResolvedPosition,
    SourceKind,
    StateOrigin,
    TopologyComponent,
    TopologyComponentId,
    TopologyDiagnostic,
    TopologyLink,
    TopologyLinkId,
    TopologySnapshot,
    TopologySource,
    VoltageEvidence,
    VoltageResolution,
    VoltageStatus,
    VoltageZone,
    VoltageZoneId,
)


class TopologyInputError(TypeError):
    """The compiler was called with an unsupported input object."""


class ConcurrentTopologyMutationError(RuntimeError):
    """The mutable aggregate changed while a snapshot was being compiled."""


@dataclass(frozen=True, slots=True)
class NormalStateSelection:
    """Select equipment normal positions and default service availability."""


NORMAL_STATE = NormalStateSelection()
StateSelection = NormalStateSelection | OperatingStateId | OperatingState | None


def electrical_model_identity(model: ElectricalModel) -> str:
    """Return the process-local identity used to reject stale snapshots."""

    if not isinstance(model, ElectricalModel):
        raise TopologyInputError("electrical_model_identity expects ElectricalModel")
    return f"electrical-model:{id(model):x}"


_K = TypeVar("_K", bound=Hashable)


class _UnionFind:
    def __init__(self, values: Iterable[_K]):
        self.parent: dict[_K, _K] = {value: value for value in values}
        self.rank: dict[_K, int] = {value: 0 for value in self.parent}

    def find(self, value: _K) -> _K:
        parent = self.parent[value]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while value != parent:
            next_value = self.parent[value]
            self.parent[value] = parent
            value = next_value
        return parent

    def union(self, first: _K, second: _K) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root == second_root:
            return
        first_rank, second_rank = self.rank[first_root], self.rank[second_root]
        if first_rank < second_rank:
            first_root, second_root = second_root, first_root
        self.parent[second_root] = first_root
        if first_rank == second_rank:
            self.rank[first_root] += 1


@dataclass(frozen=True, slots=True)
class _ModelView:
    revision: int
    voltage_classes: Mapping[VoltageClassId, Any]
    equipment_types: Mapping[tuple[Any, int], EquipmentTypeDefinition]
    equipment: Mapping[EquipmentId, EquipmentInstance]
    ports: Mapping[PortId, PortInstance]
    nodes: Mapping[ElectricalNodeId, ElectricalNode]
    connections: Mapping[ConnectionId, Connection]
    operating_states: Mapping[OperatingStateId, OperatingState]
    logical_lines: Mapping[Any, LogicalLine]
    line_sections: Mapping[EquipmentId, Any]


@dataclass(slots=True)
class _EquipmentPlan:
    equipment: EquipmentInstance
    definition: EquipmentTypeDefinition | None
    handler: TopologyBehaviorHandler | None
    ports_by_role: dict[str, PortInstance]
    nodes_by_role: dict[str, ElectricalNodeId]
    valid: bool = True


def _id_value(value: Any) -> str:
    return getattr(value, "value", str(value))


def _stable_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return {"type": type(value).__name__, "value": value.value}
    if isinstance(value, Mapping):
        rows = [
            (_stable_json_value(key), _stable_json_value(item))
            for key, item in value.items()
        ]
        rows.sort(
            key=lambda pair: json.dumps(
                pair[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return {"mapping": rows}
    if isinstance(value, (list, tuple)):
        return [_stable_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        rows = [_stable_json_value(item) for item in value]
        return {
            "set": sorted(
                rows,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
        }
    return {"type": type(value).__name__, "repr": repr(value)}


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _stable_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _severity(value: str) -> DiagnosticSeverity:
    normalized = str(value).strip().upper()
    if normalized == "INFO":
        return DiagnosticSeverity.INFO
    if normalized in {"WARNING", "WARN"}:
        return DiagnosticSeverity.WARNING
    return DiagnosticSeverity.ERROR


def _model_view(model: ElectricalModel) -> _ModelView:
    return _ModelView(
        revision=model.revision,
        voltage_classes=MappingProxyType(dict(model.voltage_classes)),
        equipment_types=MappingProxyType(dict(model.equipment_types)),
        equipment=MappingProxyType(dict(model.equipment)),
        ports=MappingProxyType(dict(model.ports)),
        nodes=MappingProxyType(dict(model.electrical_nodes)),
        connections=MappingProxyType(dict(model.connections)),
        operating_states=MappingProxyType(dict(model.operating_states)),
        logical_lines=MappingProxyType(dict(model.logical_lines)),
        line_sections=MappingProxyType(dict(model.line_sections)),
    )


def _selected_state(
    view: _ModelView, state: StateSelection
) -> tuple[str, OperatingState | None, OperatingStateId | None]:
    if state is None or isinstance(state, NormalStateSelection):
        return "NORMAL", None, None
    if isinstance(state, OperatingStateId):
        return "REGISTERED", view.operating_states.get(state), state
    if isinstance(state, OperatingState):
        return "DETACHED", state, state.id
    raise TopologyInputError(
        "state must be NORMAL_STATE, OperatingStateId, OperatingState or None"
    )


def _selection_cache_signature(view: _ModelView, state: StateSelection) -> str:
    kind, selected, requested_id = _selected_state(view, state)
    payload: dict[str, Any] = {
        "kind": kind,
        "requested_id": requested_id,
    }
    if selected is not None:
        payload["state"] = {
            "id": selected.id,
            "name": selected.name,
            "positions": selected.positions,
            "availability": selected.availability,
            "system": selected.system,
            "description": selected.description,
            "extensions": thaw_json(selected.extensions),
        }
    return _fingerprint(payload)


class TopologyEngine:
    """Compile and cache immutable electrical-topology snapshots."""

    def __init__(
        self,
        handlers: TopologyBehaviorRegistry | Iterable[TopologyBehaviorHandler] | None = None,
        *,
        cache_size: int = 8,
    ) -> None:
        if handlers is None:
            registry = builtin_topology_handlers()
        elif isinstance(handlers, TopologyBehaviorRegistry):
            registry = handlers
        else:
            registry = TopologyBehaviorRegistry(handlers)
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 1:
            raise ValueError("cache_size must be a positive integer")
        self._registry = registry
        self._cache_size = cache_size
        self._cache: WeakKeyDictionary[
            ElectricalModel, OrderedDict[tuple[str, str, str], TopologySnapshot]
        ] = WeakKeyDictionary()
        self._lock = RLock()

    @property
    def handlers(self) -> TopologyBehaviorRegistry:
        return self._registry

    def clear_cache(self, model: ElectricalModel | None = None) -> None:
        with self._lock:
            if model is None:
                self._cache.clear()
            else:
                self._cache.pop(model, None)

    def compile(
        self,
        model: ElectricalModel,
        state: StateSelection = NORMAL_STATE,
    ) -> TopologySnapshot:
        if not isinstance(model, ElectricalModel):
            raise TopologyInputError("TopologyEngine.compile expects ElectricalModel")
        start_revision = model.revision
        view = _model_view(model)
        if view.revision != start_revision:
            raise ConcurrentTopologyMutationError(
                "ElectricalModel changed while compiler inputs were captured"
            )
        content_fingerprint = electrical_model_fingerprint(model)
        state_signature = _selection_cache_signature(view, state)
        key = (content_fingerprint, state_signature, self._registry.fingerprint)
        with self._lock:
            model_cache = self._cache.get(model)
            if model_cache is not None and key in model_cache:
                snapshot = model_cache.pop(key)
                model_cache[key] = snapshot
                if model.revision != start_revision:
                    raise ConcurrentTopologyMutationError(
                        "ElectricalModel changed before cached topology was returned"
                    )
                if electrical_model_fingerprint(model) != content_fingerprint:
                    raise ConcurrentTopologyMutationError(
                        "ElectricalModel changed before cached topology was returned"
                    )
                return snapshot

        snapshot = self._compile_snapshot(
            model, view, state, content_fingerprint=content_fingerprint
        )
        if model.revision != start_revision:
            raise ConcurrentTopologyMutationError(
                "ElectricalModel changed while topology was being compiled"
            )
        with self._lock:
            model_cache = self._cache.setdefault(model, OrderedDict())
            model_cache[key] = snapshot
            while len(model_cache) > self._cache_size:
                model_cache.popitem(last=False)
        return snapshot

    @staticmethod
    def _definition(
        view: _ModelView, equipment: EquipmentInstance
    ) -> EquipmentTypeDefinition | None:
        return view.equipment_types.get((equipment.type_id, equipment.type_version))

    @staticmethod
    def _legacy_payload(equipment: EquipmentInstance) -> Mapping[str, Any]:
        value = equipment.properties.get("legacy_payload")
        return value if isinstance(value, Mapping) else MappingProxyType({})

    @staticmethod
    def _legacy_id(equipment: EquipmentInstance) -> str:
        marker = equipment.extensions.get("legacy_calculation")
        if isinstance(marker, Mapping):
            value = marker.get("legacy_id")
            if isinstance(value, str) and value:
                return value
        return equipment.id.value

    @staticmethod
    def _legacy_raw_states(state: OperatingState | None) -> Mapping[str, bool]:
        if state is None:
            return MappingProxyType({})
        marker = state.extensions.get("legacy_calculation")
        if not isinstance(marker, Mapping):
            return MappingProxyType({})
        values = marker.get("states")
        if not isinstance(values, Mapping):
            return MappingProxyType({})
        return MappingProxyType(
            {
                str(key): bool(value)
                for key, value in values.items()
                if isinstance(key, str) and isinstance(value, bool)
            }
        )

    @staticmethod
    def _legacy_raw_availability(
        state: OperatingState | None,
    ) -> Mapping[str, bool]:
        if state is None:
            return MappingProxyType({})
        marker = state.extensions.get("legacy_calculation")
        if not isinstance(marker, Mapping):
            return MappingProxyType({})
        values = marker.get("availability")
        if not isinstance(values, Mapping):
            return MappingProxyType({})
        return MappingProxyType(
            {
                str(key): bool(value)
                for key, value in values.items()
                if isinstance(key, str) and isinstance(value, bool)
            }
        )

    def _resolve_state(
        self,
        view: _ModelView,
        state: StateSelection,
        diagnostics: list[TopologyDiagnostic],
    ) -> tuple[ResolvedOperatingState, OperatingState | None]:
        selection_kind, selected, requested_id = _selected_state(view, state)
        if selection_kind == "REGISTERED" and selected is None:
            diagnostics.append(TopologyDiagnostic(
                DiagnosticSeverity.ERROR,
                "operating_state_not_found",
                f"Operating state '{requested_id}' is not registered in the model.",
                context={"operating_state_id": requested_id.value if requested_id else ""},
            ))

        explicit_positions = selected.positions if selected is not None else {}
        explicit_availability = selected.availability if selected is not None else {}

        for equipment_id in explicit_positions:
            equipment = view.equipment.get(equipment_id)
            if equipment is None:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "state_equipment_not_found",
                    f"Operating state references missing equipment '{equipment_id}'.",
                    equipment_id=equipment_id,
                ))
                continue
            definition = self._definition(view, equipment)
            if definition is None or not (
                {"switch.position", "legacy.switch.position"}
                & definition.capabilities
            ):
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "state_position_unsupported",
                    f"Equipment '{equipment_id}' does not support OPEN/CLOSED state.",
                    equipment_id=equipment_id,
                ))

        for equipment_id in explicit_availability:
            equipment = view.equipment.get(equipment_id)
            if equipment is None:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "availability_equipment_not_found",
                    f"Operating state references missing equipment '{equipment_id}'.",
                    equipment_id=equipment_id,
                ))
                continue
            definition = self._definition(view, equipment)
            if (
                definition is None
                or EQUIPMENT_AVAILABILITY_CAPABILITY not in definition.capabilities
            ):
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "state_availability_unsupported",
                    f"Equipment '{equipment_id}' does not support service availability.",
                    equipment_id=equipment_id,
                ))

        positions: dict[EquipmentId, ResolvedPosition] = {}
        availability: dict[EquipmentId, EquipmentAvailability] = {}
        availability_origins: dict[EquipmentId, StateOrigin] = {}
        for equipment_id in sorted(view.equipment, key=lambda item: item.value):
            equipment = view.equipment[equipment_id]
            definition = self._definition(view, equipment)
            handler = (
                self._registry.get(definition.behavior_key)
                if definition is not None
                else None
            )
            capabilities = definition.capabilities if definition is not None else ()

            explicit_service = explicit_availability.get(equipment_id)
            if (
                explicit_service is not None
                and EQUIPMENT_AVAILABILITY_CAPABILITY in capabilities
            ):
                availability[equipment_id] = explicit_service
                availability_origins[equipment_id] = StateOrigin.EXPLICIT_STATE
            else:
                availability[equipment_id] = EquipmentAvailability.IN_SERVICE
                availability_origins[equipment_id] = StateOrigin.DEFAULT_AVAILABILITY

            switch_capable = bool(
                {"switch.position", "legacy.switch.position"} & set(capabilities)
            )
            handler_requires_position = bool(
                handler is not None and handler.kind == BehaviorKind.SWITCH
            )
            if not switch_capable and not handler_requires_position:
                continue

            explicit = explicit_positions.get(equipment_id)
            if explicit is not None and switch_capable:
                resolved = ResolvedPosition(
                    equipment_id,
                    explicit,
                    Conductivity.CONDUCTING
                    if explicit == SwitchPosition.CLOSED
                    else Conductivity.OPEN,
                    StateOrigin.EXPLICIT_STATE,
                )
            elif equipment.normal_position is not None:
                resolved = ResolvedPosition(
                    equipment_id,
                    equipment.normal_position,
                    Conductivity.CONDUCTING
                    if equipment.normal_position == SwitchPosition.CLOSED
                    else Conductivity.OPEN,
                    StateOrigin.NORMAL_POSITION,
                )
            elif handler is not None and handler.compatibility:
                payload = self._legacy_payload(equipment)
                if payload.get("switchable", False) is False:
                    resolved = ResolvedPosition(
                        equipment_id,
                        SwitchPosition.CLOSED,
                        Conductivity.CONDUCTING,
                        StateOrigin.LEGACY_COMPAT,
                        "Legacy non-switchable equipment is in service by default.",
                    )
                else:
                    resolved = ResolvedPosition(
                        equipment_id,
                        None,
                        Conductivity.INVALID,
                        StateOrigin.LEGACY_COMPAT,
                        "Legacy switchable equipment has no explicit or normal position.",
                    )
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "ambiguous_switch_position",
                        f"Equipment '{equipment_id}' has no explicit or normal position.",
                        equipment_id=equipment_id,
                    ))
            else:
                resolved = ResolvedPosition(
                    equipment_id,
                    None,
                    Conductivity.INVALID,
                    StateOrigin.NORMAL_POSITION,
                    "No explicit or normal position is available.",
                )
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "ambiguous_switch_position",
                    f"Switching equipment '{equipment_id}' has no explicit or normal position.",
                    equipment_id=equipment_id,
                ))
            positions[equipment_id] = resolved

        raw_legacy = self._legacy_raw_states(selected)
        raw_legacy_availability = self._legacy_raw_availability(selected)
        signature = tuple(
            [
                *(
                    f"position:{equipment_id.value}:{resolved.position.value if resolved.position else 'UNKNOWN'}"
                    for equipment_id, resolved in sorted(
                        positions.items(), key=lambda item: item[0].value
                    )
                ),
                *(
                    f"availability:{equipment_id.value}:{value.value}"
                    for equipment_id, value in sorted(
                        availability.items(), key=lambda item: item[0].value
                    )
                ),
                *(
                    f"legacy:{key}:{'1' if value else '0'}"
                    for key, value in sorted(raw_legacy.items())
                ),
                *(
                    f"legacy_availability:{key}:{'1' if value else '0'}"
                    for key, value in sorted(raw_legacy_availability.items())
                ),
            ]
        )
        return ResolvedOperatingState(
            selection_kind=selection_kind,
            state_id=selected.id if selected is not None else requested_id,
            state_name=selected.name if selected is not None else "",
            system=selected.system if selected is not None else None,
            positions=positions,
            availability=availability,
            availability_origins=availability_origins,
            topology_signature=signature,
        ), selected

    def _legacy_main_closed(
        self,
        equipment: EquipmentInstance,
        resolved: ResolvedOperatingState,
        raw_states: Mapping[str, bool],
        *,
        branch_id: str | None = None,
    ) -> bool:
        key = branch_id or self._legacy_id(equipment)
        if key in raw_states:
            return bool(raw_states[key])
        position = resolved.positions.get(equipment.id)
        if position is not None:
            return position.position == SwitchPosition.CLOSED
        payload = self._legacy_payload(equipment)
        if payload.get("switchable", False) is False:
            return True
        return bool(payload.get("normally_closed", False))

    def _legacy_two_terminal_active(
        self,
        plan: _EquipmentPlan,
        resolved: ResolvedOperatingState,
        raw_states: Mapping[str, bool],
        raw_availability: Mapping[str, bool],
    ) -> bool:
        equipment = plan.equipment
        behavior = plan.definition.behavior_key if plan.definition else ""
        legacy_id = self._legacy_id(equipment)
        explicitly_available = (
            resolved.availability_origins.get(equipment.id)
            == StateOrigin.EXPLICIT_STATE
        )
        if (
            not explicitly_available
            and not raw_availability.get(legacy_id, True)
        ):
            return False
        if not self._legacy_main_closed(equipment, resolved, raw_states):
            return False
        payload = self._legacy_payload(equipment)
        if payload.get("switchable", False) is False:
            return True
        if behavior in {"legacy.line", "legacy.transformer_2w"}:
            ends = ("from", "to")
        elif behavior in {"legacy.tie", "legacy.generator"}:
            ends = ("from",)
        else:
            ends = ()
        return all(
            raw_states.get(f"SW:{legacy_id}:{end}", True)
            for end in ends
        )

    def _legacy_three_winding_roles(
        self,
        plan: _EquipmentPlan,
        resolved: ResolvedOperatingState,
        raw_states: Mapping[str, bool],
        raw_availability: Mapping[str, bool],
    ) -> tuple[str, ...]:
        equipment = plan.equipment
        legacy_id = self._legacy_id(equipment)
        payload = self._legacy_payload(equipment)
        switchable = bool(payload.get("switchable", False))
        main = self._legacy_main_closed(equipment, resolved, raw_states)
        explicitly_available = (
            resolved.availability_origins.get(equipment.id)
            == StateOrigin.EXPLICIT_STATE
        )
        active: list[str] = []
        for role, branch_id in (
            ("hv", legacy_id),
            ("mv", f"{legacy_id}_mv"),
            ("lv", f"{legacy_id}_lv"),
        ):
            if (
                not explicitly_available
                and not raw_availability.get(branch_id, True)
            ):
                continue
            leg_main = (
                bool(raw_states[branch_id])
                if branch_id in raw_states
                else main
            )
            if not leg_main:
                continue
            if switchable and not all(
                raw_states.get(f"SW:{branch_id}:{end}", True)
                for end in ("from", "to")
            ):
                continue
            active.append(role)
        return tuple(active)

    def _plan_is_active(
        self,
        plan: _EquipmentPlan,
        resolved: ResolvedOperatingState,
        raw_states: Mapping[str, bool],
        raw_availability: Mapping[str, bool],
    ) -> bool:
        if (
            resolved.availability.get(
                plan.equipment.id, EquipmentAvailability.IN_SERVICE
            )
            == EquipmentAvailability.OUT_OF_SERVICE
        ):
            return False
        handler = plan.handler
        if handler is None:
            return False
        if handler.compatibility:
            return self._legacy_two_terminal_active(
                plan, resolved, raw_states, raw_availability
            )
        if handler.kind == BehaviorKind.SWITCH:
            position = resolved.positions.get(plan.equipment.id)
            return bool(
                position is not None
                and position.position == SwitchPosition.CLOSED
                and position.conductivity == Conductivity.CONDUCTING
            )
        return True

    def _compile_snapshot(
        self,
        model: ElectricalModel,
        view: _ModelView,
        state: StateSelection,
        *,
        content_fingerprint: str,
    ) -> TopologySnapshot:
        diagnostics: list[TopologyDiagnostic] = []

        try:
            integrity = model.validate_integrity()
        except Exception as exc:  # defensive compiler boundary for malformed fixtures
            integrity = ()
            diagnostics.append(TopologyDiagnostic(
                DiagnosticSeverity.ERROR,
                "domain_integrity_check_failed",
                f"Domain integrity check failed: {exc}",
            ))
        for issue in integrity:
            object_value = issue.object_id
            equipment_id = next(
                (item for item in view.equipment if item.value == object_value), None
            )
            port_id = next(
                (item for item in view.ports if item.value == object_value), None
            )
            node_id = next(
                (item for item in view.nodes if item.value == object_value), None
            )
            connection_id = next(
                (item for item in view.connections if item.value == object_value), None
            )
            diagnostics.append(TopologyDiagnostic(
                _severity(issue.severity),
                issue.code,
                issue.message,
                equipment_id=equipment_id,
                port_id=port_id,
                electrical_node_id=node_id,
                connection_id=connection_id,
                context={"domain_object_id": object_value} if object_value else {},
            ))

        resolved_state, selected_state = self._resolve_state(
            view, state, diagnostics
        )
        raw_legacy_states = self._legacy_raw_states(selected_state)
        raw_legacy_availability = self._legacy_raw_availability(selected_state)

        port_to_node: dict[PortId, ElectricalNodeId] = {}
        connection_by_port: dict[PortId, Connection] = {}
        for connection_id in sorted(view.connections, key=lambda item: item.value):
            connection = view.connections[connection_id]
            port = view.ports.get(connection.port_id)
            node = view.nodes.get(connection.electrical_node_id)
            if port is None:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "dangling_connection_port",
                    f"Connection '{connection.id}' references a missing port.",
                    connection_id=connection.id,
                ))
            if node is None:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "dangling_connection_node",
                    f"Connection '{connection.id}' references a missing electrical node.",
                    port_id=connection.port_id if port is not None else None,
                    connection_id=connection.id,
                ))
            if port is None or node is None:
                continue
            previous = connection_by_port.get(port.id)
            if previous is not None:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "port_connected_twice",
                    f"Port '{port.id}' has more than one connection.",
                    equipment_id=port.equipment_id,
                    port_id=port.id,
                    connection_id=connection.id,
                    related_connection_ids=(previous.id,),
                ))
                continue
            connection_by_port[port.id] = connection
            port_to_node[port.id] = node.id

        plans: dict[EquipmentId, _EquipmentPlan] = {}
        incomplete_nodes: set[ElectricalNodeId] = set()
        for equipment_id in sorted(view.equipment, key=lambda item: item.value):
            equipment = view.equipment[equipment_id]
            definition = self._definition(view, equipment)
            handler = (
                self._registry.get(definition.behavior_key)
                if definition is not None
                else None
            )
            plan = _EquipmentPlan(equipment, definition, handler, {}, {})
            plans[equipment_id] = plan

            if definition is None:
                plan.valid = False
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "unknown_equipment_type",
                    f"Equipment '{equipment_id}' references an unknown type version.",
                    equipment_id=equipment_id,
                ))
            elif handler is None:
                plan.valid = False
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "unsupported_behavior",
                    f"Topology behavior '{definition.behavior_key}' is not registered.",
                    equipment_id=equipment_id,
                    context={"behavior_key": definition.behavior_key},
                ))

            for port_id in equipment.port_ids:
                port = view.ports.get(port_id)
                if port is None:
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "missing_equipment_port",
                        f"Equipment '{equipment_id}' lists a missing port '{port_id}'.",
                        equipment_id=equipment_id,
                        port_id=port_id,
                    ))
                    continue
                if port.equipment_id != equipment_id:
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "port_owner_mismatch",
                        f"Port '{port.id}' belongs to another equipment item.",
                        equipment_id=equipment_id,
                        port_id=port.id,
                        related_equipment_ids=(port.equipment_id,),
                    ))
                    continue
                if port.role in plan.ports_by_role:
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "duplicate_port_role",
                        f"Equipment '{equipment_id}' repeats port role '{port.role}'.",
                        equipment_id=equipment_id,
                        port_id=port.id,
                    ))
                    continue
                plan.ports_by_role[port.role] = port
                if port.id in port_to_node:
                    plan.nodes_by_role[port.role] = port_to_node[port.id]

            if definition is not None:
                definitions_by_role = {
                    item.role: item for item in definition.port_definitions
                }
                actual_roles = set(plan.ports_by_role)
                definition_roles = set(definitions_by_role)
                required_roles = {
                    item.role for item in definition.port_definitions if item.required
                }
                if not required_roles <= actual_roles or not actual_roles <= definition_roles:
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "equipment_port_roles_mismatch",
                        f"Equipment '{equipment_id}' port roles do not match its type.",
                        equipment_id=equipment_id,
                        related_port_ids=tuple(item.id for item in plan.ports_by_role.values()),
                        context={
                            "required_roles": ",".join(sorted(required_roles)),
                            "actual_roles": ",".join(sorted(actual_roles)),
                        },
                    ))
                if handler is not None and set(handler.roles) != actual_roles:
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "behavior_port_roles_mismatch",
                        f"Behavior '{handler.behavior_key}' requires exact roles {handler.roles}.",
                        equipment_id=equipment_id,
                        related_port_ids=tuple(item.id for item in plan.ports_by_role.values()),
                    ))

                for role in sorted(required_roles):
                    port = plan.ports_by_role.get(role)
                    if port is None:
                        continue
                    if port.id not in port_to_node:
                        plan.valid = False
                        diagnostics.append(TopologyDiagnostic(
                            DiagnosticSeverity.ERROR,
                            "required_port_unconnected",
                            f"Required port '{port.id}' is not connected.",
                            equipment_id=equipment_id,
                            port_id=port.id,
                        ))

                for role, port in plan.ports_by_role.items():
                    node_id = plan.nodes_by_role.get(role)
                    port_definition = definitions_by_role.get(role)
                    node = view.nodes.get(node_id) if node_id is not None else None
                    if (
                        port_definition is not None
                        and node is not None
                        and port_definition.kind_id != node.kind_id
                    ):
                        plan.valid = False
                        diagnostics.append(TopologyDiagnostic(
                            DiagnosticSeverity.ERROR,
                            "incompatible_port_kind",
                            f"Port '{port.id}' and node '{node.id}' have different electrical kinds.",
                            equipment_id=equipment_id,
                            port_id=port.id,
                            electrical_node_id=node.id,
                        ))

                if (
                    handler is not None
                    and handler.kind == BehaviorKind.SWITCH
                    and "switch.position" not in definition.capabilities
                    and "legacy.switch.position" not in definition.capabilities
                ):
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "switch_position_capability_missing",
                        f"Switch behavior '{handler.behavior_key}' lacks a position capability.",
                        equipment_id=equipment_id,
                    ))
                if (
                    handler is not None
                    and handler.kind != BehaviorKind.SWITCH
                    and not handler.compatibility
                    and bool(
                        {"switch.position", "legacy.switch.position"}
                        & definition.capabilities
                    )
                ):
                    plan.valid = False
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "switch_position_behavior_mismatch",
                        f"Equipment '{equipment_id}' declares OPEN/CLOSED capability "
                        f"but behavior '{handler.behavior_key}' is not a switch.",
                        equipment_id=equipment_id,
                    ))

            if handler is not None and handler.compatibility:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.INFO,
                    "legacy_compatibility_handler",
                    f"Equipment '{equipment_id}' uses an explicit legacy compatibility handler.",
                    equipment_id=equipment_id,
                ))

            if not plan.valid:
                incomplete_nodes.update(plan.nodes_by_role.values())

        # Ports whose owner omitted them from equipment.port_ids are blocking.
        for port_id in sorted(view.ports, key=lambda item: item.value):
            port = view.ports[port_id]
            owner = view.equipment.get(port.equipment_id)
            if owner is None:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "missing_port_owner",
                    f"Port '{port.id}' has no equipment owner.",
                    port_id=port.id,
                    equipment_id=port.equipment_id,
                ))
                if port.id in port_to_node:
                    incomplete_nodes.add(port_to_node[port.id])
            elif port.id not in owner.port_ids:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "port_not_listed_by_owner",
                    f"Port '{port.id}' is not listed by its owner.",
                    equipment_id=owner.id,
                    port_id=port.id,
                ))
                if port.id in port_to_node:
                    incomplete_nodes.add(port_to_node[port.id])

        links: dict[TopologyLinkId, TopologyLink] = {}
        link_ids_by_equipment: dict[EquipmentId, tuple[TopologyLinkId, ...]] = {}
        sources: dict[EquipmentId, TopologySource] = {}
        for equipment_id in sorted(plans, key=lambda item: item.value):
            plan = plans[equipment_id]
            handler = plan.handler
            definition = plan.definition
            if handler is None or definition is None:
                continue
            availability = resolved_state.availability.get(
                equipment_id, EquipmentAvailability.IN_SERVICE
            )
            resolved_position = resolved_state.positions.get(equipment_id)

            if handler.is_source:
                port = plan.ports_by_role.get("terminal")
                node_id = plan.nodes_by_role.get("terminal")
                if port is None or node_id is None:
                    diagnostics.append(TopologyDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "source_terminal_unconnected",
                        f"Source '{equipment_id}' has no connected terminal.",
                        equipment_id=equipment_id,
                        port_id=port.id if port is not None else None,
                    ))
                    continue
                active = plan.valid and self._plan_is_active(
                    plan,
                    resolved_state,
                    raw_legacy_states,
                    raw_legacy_availability,
                )
                port_definition = next(
                    (
                        item for item in definition.port_definitions
                        if item.role == "terminal"
                    ),
                    None,
                )
                voltage_id = None
                if port_definition is not None and port_definition.voltage_group:
                    voltage_id = plan.equipment.voltage_class_by_group.get(
                        port_definition.voltage_group
                    )
                if voltage_id is None:
                    voltage_id = view.nodes[node_id].declared_voltage_class_id
                sources[equipment_id] = TopologySource(
                    equipment_id,
                    SourceKind.EXTERNAL_GRID
                    if handler.kind == BehaviorKind.SOURCE
                    else SourceKind.GENERATOR,
                    port.id,
                    node_id,
                    active,
                    availability,
                    voltage_id,
                )
                continue

            if not handler.creates_link:
                continue
            # A malformed/unfinished item is preserved through diagnostics and
            # stable IDs, but never materialized as a physical topology link.
            # This prevents even an inactive "ghost branch" from being mistaken
            # for a complete element by later consumers.
            if not plan.valid:
                continue

            port_ids = tuple(
                plan.ports_by_role[role].id
                for role in handler.roles
                if role in plan.ports_by_role
            )
            node_ids = tuple(
                plan.nodes_by_role[role]
                for role in handler.roles
                if role in plan.nodes_by_role
            )
            equipment_active = plan.valid and self._plan_is_active(
                plan,
                resolved_state,
                raw_legacy_states,
                raw_legacy_availability,
            )
            if handler.kind == BehaviorKind.TRANSFORMER_3W and handler.compatibility:
                active_roles = self._legacy_three_winding_roles(
                    plan,
                    resolved_state,
                    raw_legacy_states,
                    raw_legacy_availability,
                ) if plan.valid and availability == EquipmentAvailability.IN_SERVICE else ()
            else:
                active_roles = handler.roles if equipment_active else ()
            active_port_ids = tuple(
                plan.ports_by_role[role].id
                for role in active_roles
                if role in plan.ports_by_role and role in plan.nodes_by_role
            )
            active_node_ids = tuple(
                plan.nodes_by_role[role]
                for role in active_roles
                if role in plan.nodes_by_role
            )
            # A topology link must connect at least two terminals.  In
            # particular, one remaining legacy 3W winding is observable
            # equipment, but it cannot form an electrical path on its own.
            active = len(active_roles) >= 2 and plan.valid
            if not active:
                active_port_ids = ()
                active_node_ids = ()

            if not plan.valid:
                conductivity = Conductivity.INVALID
            elif (
                resolved_position is not None
                and resolved_position.conductivity == Conductivity.INVALID
            ):
                conductivity = Conductivity.INVALID
            elif availability == EquipmentAvailability.OUT_OF_SERVICE:
                conductivity = Conductivity.OPEN
            elif active:
                conductivity = Conductivity.CONDUCTING
            else:
                conductivity = Conductivity.OPEN
            link_id = TopologyLinkId(f"topology-link:{equipment_id.value}")
            link = TopologyLink(
                link_id,
                equipment_id,
                definition.behavior_key,
                port_ids,
                node_ids,
                active_port_ids,
                active_node_ids,
                active,
                conductivity,
                handler.is_transformer,
                resolved_position,
                availability,
            )
            links[link_id] = link
            link_ids_by_equipment[equipment_id] = (link_id,)

            if len(port_ids) > 1 and len(set(node_ids)) < len(node_ids):
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.WARNING,
                    "redundant_self_loop",
                    f"Equipment '{equipment_id}' has multiple terminals on one node.",
                    equipment_id=equipment_id,
                    related_port_ids=port_ids,
                    related_node_ids=node_ids,
                ))

        components, component_by_node, sources = self._build_components(
            view,
            plans,
            links,
            sources,
            incomplete_nodes,
            diagnostics,
        )
        (
            voltage_zones,
            voltage_zone_by_node,
            voltage_zone_by_port,
            voltage_zone_by_equipment_group,
        ) = self._build_voltage_zones(
            view, plans, port_to_node, diagnostics
        )
        sources = {
            equipment_id: (
                source
                if source.voltage_class_id is not None
                else replace(
                    source,
                    voltage_class_id=voltage_zones[
                        voltage_zone_by_node[source.node_id]
                    ].resolution.voltage_class_id,
                )
            )
            for equipment_id, source in sources.items()
        }

        self._add_post_topology_diagnostics(
            links, components, component_by_node, diagnostics
        )
        diagnostics = self._deduplicate_diagnostics(diagnostics)
        return TopologySnapshot(
            model_identity=electrical_model_identity(model),
            model_revision=view.revision,
            model_fingerprint=content_fingerprint,
            registry_signature=self._registry.fingerprint,
            operating_state=resolved_state,
            node_ids=tuple(view.nodes),
            port_to_node=port_to_node,
            links=links,
            link_ids_by_equipment=link_ids_by_equipment,
            sources=sources,
            components=components,
            component_by_node=component_by_node,
            voltage_zones=voltage_zones,
            voltage_zone_by_node=voltage_zone_by_node,
            voltage_zone_by_port=voltage_zone_by_port,
            voltage_zone_by_equipment_group=voltage_zone_by_equipment_group,
            diagnostics=tuple(diagnostics),
        )

    def _build_components(
        self,
        view: _ModelView,
        plans: Mapping[EquipmentId, _EquipmentPlan],
        links: Mapping[TopologyLinkId, TopologyLink],
        sources: Mapping[EquipmentId, TopologySource],
        incomplete_nodes: set[ElectricalNodeId],
        diagnostics: list[TopologyDiagnostic],
    ) -> tuple[
        dict[TopologyComponentId, TopologyComponent],
        dict[ElectricalNodeId, TopologyComponentId],
        dict[EquipmentId, TopologySource],
    ]:
        union = _UnionFind(view.nodes)
        for link in links.values():
            if not link.active:
                continue
            active_nodes = tuple(dict.fromkeys(link.active_node_ids))
            if len(active_nodes) < 2:
                continue
            first = active_nodes[0]
            for node_id in active_nodes[1:]:
                union.union(first, node_id)

        groups: dict[ElectricalNodeId, list[ElectricalNodeId]] = defaultdict(list)
        for node_id in sorted(view.nodes, key=lambda item: item.value):
            groups[union.find(node_id)].append(node_id)

        component_by_node: dict[ElectricalNodeId, TopologyComponentId] = {}
        ids_by_root: dict[ElectricalNodeId, TopologyComponentId] = {}
        for root, nodes in groups.items():
            representative = min(nodes, key=lambda item: item.value)
            component_id = TopologyComponentId(
                f"topology-component:{representative.value}"
            )
            ids_by_root[root] = component_id
            for node_id in nodes:
                component_by_node[node_id] = component_id

        source_ids_by_component: dict[TopologyComponentId, list[EquipmentId]] = (
            defaultdict(list)
        )
        updated_sources: dict[EquipmentId, TopologySource] = {}
        for equipment_id, source in sources.items():
            component_id = component_by_node[source.node_id]
            updated = replace(source, component_id=component_id)
            updated_sources[equipment_id] = updated
            if source.active:
                source_ids_by_component[component_id].append(equipment_id)

        components: dict[TopologyComponentId, TopologyComponent] = {}
        for root, raw_nodes in sorted(
            groups.items(), key=lambda item: min(node.value for node in item[1])
        ):
            nodes = tuple(sorted(raw_nodes, key=lambda item: item.value))
            node_set = set(nodes)
            component_id = ids_by_root[root]
            component_links = tuple(
                sorted(
                    (
                        link.id
                        for link in links.values()
                        if link.active
                        and bool(set(link.active_node_ids) & node_set)
                        and set(link.active_node_ids) <= node_set
                    ),
                    key=lambda item: item.value,
                )
            )
            active_link_equipment = {
                links[link_id].equipment_id for link_id in component_links
            }
            equipment_ids = tuple(
                sorted(
                    (
                        equipment_id
                        for equipment_id, plan in plans.items()
                        if equipment_id in active_link_equipment
                        or (
                            plan.handler is not None
                            and not plan.handler.creates_link
                            and bool(set(plan.nodes_by_role.values()) & node_set)
                        )
                    ),
                    key=lambda item: item.value,
                )
            )
            source_ids = tuple(
                sorted(
                    source_ids_by_component.get(component_id, ()),
                    key=lambda item: item.value,
                )
            )

            expanded_link_vertices = 0
            expanded_edges = 0
            parallel: dict[tuple[str, ...], list[TopologyLinkId]] = defaultdict(list)
            for link in links.values():
                if not link.active:
                    continue
                active_nodes = tuple(
                    sorted(
                        set(link.active_node_ids) & node_set,
                        key=lambda item: item.value,
                    )
                )
                if len(active_nodes) < 2:
                    continue
                # One equipment-interior vertex per hyperedge.  This makes a
                # 3W transformer a star and prevents a false triangle cycle.
                expanded_link_vertices += 1
                expanded_edges += len(active_nodes)
                parallel[tuple(item.value for item in active_nodes)].append(link.id)
            cycle_rank = max(
                0,
                expanded_edges - (len(nodes) + expanded_link_vertices) + 1,
            )
            parallel_groups = tuple(
                tuple(sorted(values, key=lambda item: item.value))
                for _, values in sorted(parallel.items())
                if len(values) > 1
            )
            incomplete = bool(node_set & incomplete_nodes)
            if source_ids:
                energization = Energization.ENERGIZED
            elif incomplete:
                energization = Energization.UNKNOWN
            else:
                energization = Energization.DEENERGIZED
            component = TopologyComponent(
                component_id,
                nodes,
                component_links,
                equipment_ids,
                source_ids,
                energization,
                cycle_rank > 0,
                cycle_rank,
                parallel_groups,
            )
            components[component_id] = component

            if energization == Energization.DEENERGIZED:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.WARNING,
                    "component_without_source",
                    f"Electrical component '{component_id}' has no active source.",
                    electrical_node_id=nodes[0] if nodes else None,
                    related_node_ids=nodes,
                ))
            elif energization == Energization.UNKNOWN:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.WARNING,
                    "component_topology_incomplete",
                    f"Energization of component '{component_id}' is unknown because topology is incomplete.",
                    electrical_node_id=nodes[0] if nodes else None,
                    related_node_ids=nodes,
                ))
            if len(source_ids) > 1:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.INFO,
                    "multiple_active_sources",
                    f"Component '{component_id}' is energized by multiple sources.",
                    electrical_node_id=nodes[0] if nodes else None,
                    related_equipment_ids=source_ids,
                    related_node_ids=nodes,
                ))
            if cycle_rank > 0:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.INFO,
                    "meshed_component",
                    f"Component '{component_id}' contains {cycle_rank} independent cycle(s).",
                    electrical_node_id=nodes[0] if nodes else None,
                    related_equipment_ids=equipment_ids,
                    related_node_ids=nodes,
                ))

        return components, component_by_node, updated_sources

    def _build_voltage_zones(
        self,
        view: _ModelView,
        plans: Mapping[EquipmentId, _EquipmentPlan],
        port_to_node: Mapping[PortId, ElectricalNodeId],
        diagnostics: list[TopologyDiagnostic],
    ) -> tuple[
        dict[VoltageZoneId, VoltageZone],
        dict[ElectricalNodeId, VoltageZoneId],
        dict[PortId, VoltageZoneId],
        dict[tuple[EquipmentId, str], VoltageZoneId],
    ]:
        union = _UnionFind(view.nodes)
        for plan in plans.values():
            handler = plan.handler
            if handler is None or not plan.valid or not handler.same_voltage:
                continue
            nodes = [
                plan.nodes_by_role[role]
                for role in handler.roles
                if role in plan.nodes_by_role
            ]
            if len(nodes) >= 2:
                first = nodes[0]
                for node_id in nodes[1:]:
                    union.union(first, node_id)

        evidence_by_node: dict[ElectricalNodeId, list[VoltageEvidence]] = defaultdict(list)
        constraints_by_node: dict[
            ElectricalNodeId, list[tuple[PortId, EquipmentId, str | None, frozenset[VoltageClassId]]]
        ] = defaultdict(list)

        for node_id, node in view.nodes.items():
            voltage_id = node.declared_voltage_class_id
            if voltage_id is None:
                continue
            if voltage_id not in view.voltage_classes:
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "unknown_voltage_class_reference",
                    f"Node '{node_id}' references an unknown voltage class.",
                    electrical_node_id=node_id,
                ))
                continue
            evidence_by_node[node_id].append(VoltageEvidence(
                voltage_id,
                "node.declared_voltage_class_id",
                electrical_node_id=node_id,
            ))

        for equipment_id in sorted(plans, key=lambda item: item.value):
            plan = plans[equipment_id]
            definition = plan.definition
            if definition is None:
                continue
            definitions_by_role = {
                item.role: item for item in definition.port_definitions
            }
            for role, port in plan.ports_by_role.items():
                node_id = plan.nodes_by_role.get(role)
                port_definition = definitions_by_role.get(role)
                if node_id is None or port_definition is None:
                    continue
                group = port_definition.voltage_group
                if group is not None:
                    voltage_id = plan.equipment.voltage_class_by_group.get(group)
                    if voltage_id is not None:
                        if voltage_id not in view.voltage_classes:
                            diagnostics.append(TopologyDiagnostic(
                                DiagnosticSeverity.ERROR,
                                "unknown_voltage_class_reference",
                                f"Equipment '{equipment_id}' references an unknown voltage class.",
                                equipment_id=equipment_id,
                                port_id=port.id,
                                electrical_node_id=node_id,
                            ))
                        else:
                            evidence_by_node[node_id].append(VoltageEvidence(
                                voltage_id,
                                "equipment.voltage_class_by_group",
                                electrical_node_id=node_id,
                                equipment_id=equipment_id,
                                port_id=port.id,
                                voltage_group=group,
                            ))
                if port_definition.allowed_voltage_class_ids is not None:
                    constraints_by_node[node_id].append((
                        port.id,
                        equipment_id,
                        group,
                        frozenset(port_definition.allowed_voltage_class_ids),
                    ))

        for line in view.logical_lines.values():
            if line.voltage_class_id not in view.voltage_classes:
                continue
            for section_id in line.section_equipment_ids:
                plan = plans.get(section_id)
                if plan is None:
                    continue
                for role, node_id in plan.nodes_by_role.items():
                    port = plan.ports_by_role.get(role)
                    evidence_by_node[node_id].append(VoltageEvidence(
                        line.voltage_class_id,
                        "logical_line.voltage_class_id",
                        electrical_node_id=node_id,
                        equipment_id=section_id,
                        port_id=port.id if port is not None else None,
                        voltage_group="main",
                    ))

        groups: dict[ElectricalNodeId, list[ElectricalNodeId]] = defaultdict(list)
        for node_id in sorted(view.nodes, key=lambda item: item.value):
            groups[union.find(node_id)].append(node_id)

        zone_id_by_root: dict[ElectricalNodeId, VoltageZoneId] = {}
        voltage_zone_by_node: dict[ElectricalNodeId, VoltageZoneId] = {}
        for root, nodes in groups.items():
            representative = min(nodes, key=lambda item: item.value)
            zone_id = VoltageZoneId(f"voltage-zone:{representative.value}")
            zone_id_by_root[root] = zone_id
            for node_id in nodes:
                voltage_zone_by_node[node_id] = zone_id

        voltage_zone_by_port: dict[PortId, VoltageZoneId] = {
            port_id: voltage_zone_by_node[node_id]
            for port_id, node_id in port_to_node.items()
            if node_id in voltage_zone_by_node
        }
        groups_to_roots: dict[tuple[EquipmentId, str], set[ElectricalNodeId]] = (
            defaultdict(set)
        )
        for equipment_id, plan in plans.items():
            definition = plan.definition
            if definition is None:
                continue
            by_role = {item.role: item for item in definition.port_definitions}
            for role, node_id in plan.nodes_by_role.items():
                port_definition = by_role.get(role)
                if port_definition is None or port_definition.voltage_group is None:
                    continue
                groups_to_roots[(equipment_id, port_definition.voltage_group)].add(
                    union.find(node_id)
                )

        voltage_zone_by_equipment_group: dict[
            tuple[EquipmentId, str], VoltageZoneId
        ] = {}
        for equipment_group, roots in groups_to_roots.items():
            if len(roots) == 1:
                voltage_zone_by_equipment_group[equipment_group] = zone_id_by_root[
                    next(iter(roots))
                ]
            else:
                equipment_id, group = equipment_group
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "equipment_voltage_group_split",
                    f"Equipment '{equipment_id}' voltage group '{group}' spans separate nominal zones.",
                    equipment_id=equipment_id,
                ))

        zones: dict[VoltageZoneId, VoltageZone] = {}
        for root, raw_nodes in sorted(
            groups.items(), key=lambda item: min(node.value for node in item[1])
        ):
            nodes = tuple(sorted(raw_nodes, key=lambda item: item.value))
            node_set = set(nodes)
            zone_id = zone_id_by_root[root]
            relation_equipment_ids = tuple(sorted(
                (
                    equipment_id
                    for equipment_id, plan in plans.items()
                    if plan.valid
                    and plan.handler is not None
                    and plan.handler.same_voltage
                    and len(set(plan.nodes_by_role.values()) & node_set) >= 2
                ),
                key=lambda item: item.value,
            ))
            raw_evidence = [
                evidence
                for node_id in nodes
                for evidence in evidence_by_node.get(node_id, ())
            ]
            evidence_keys: set[tuple[str, ...]] = set()
            evidence: list[VoltageEvidence] = []
            for item in sorted(raw_evidence, key=lambda value: value.sort_key()):
                key = item.sort_key()
                if key in evidence_keys:
                    continue
                evidence_keys.add(key)
                evidence.append(item)

            constraint_rows = [
                row
                for node_id in nodes
                for row in constraints_by_node.get(node_id, ())
            ]
            allowed: set[VoltageClassId] | None = None
            for _, _, _, values in constraint_rows:
                allowed = set(values) if allowed is None else allowed & set(values)
            candidates = {item.voltage_class_id for item in evidence}
            constraint_conflict = allowed is not None and not allowed
            evidence_disallowed = bool(
                candidates and allowed is not None and not candidates <= allowed
            )
            if len(candidates) > 1 or constraint_conflict or evidence_disallowed:
                status = VoltageStatus.CONFLICT
                voltage_id = None
                exposed_candidates = set(candidates)
                if constraint_conflict:
                    for _, _, _, values in constraint_rows:
                        exposed_candidates.update(values)
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "voltage_conflict",
                    f"Nominal voltage conflict in zone '{zone_id}'.",
                    equipment_id=(
                        relation_equipment_ids[0]
                        if relation_equipment_ids else None
                    ),
                    electrical_node_id=nodes[0] if nodes else None,
                    related_equipment_ids=tuple(
                        sorted(
                            {
                                item.equipment_id
                                for item in evidence
                                if item.equipment_id is not None
                            } | set(relation_equipment_ids),
                            key=lambda item: item.value,
                        )
                    ),
                    related_port_ids=tuple(
                        sorted(
                            {
                                item.port_id
                                for item in evidence
                                if item.port_id is not None
                            },
                            key=lambda item: item.value,
                        )
                    ),
                    related_node_ids=nodes,
                    context={
                        "voltage_class_ids": ",".join(
                            sorted(item.value for item in exposed_candidates)
                        )
                    },
                ))
            elif len(candidates) == 1:
                status = VoltageStatus.RESOLVED
                voltage_id = next(iter(candidates))
                exposed_candidates = set(candidates)
            elif allowed is not None and len(allowed) == 1:
                status = VoltageStatus.RESOLVED
                voltage_id = next(iter(allowed))
                exposed_candidates = {voltage_id}
                row = next(
                    (item for item in constraint_rows if voltage_id in item[3]), None
                )
                if row is not None:
                    port_id, equipment_id, group, _ = row
                    node_id = port_to_node.get(port_id)
                    evidence.append(VoltageEvidence(
                        voltage_id,
                        "port.allowed_voltage_class_ids",
                        electrical_node_id=node_id,
                        equipment_id=equipment_id,
                        port_id=port_id,
                        voltage_group=group,
                    ))
            else:
                status = VoltageStatus.UNKNOWN
                voltage_id = None
                exposed_candidates = set()
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.WARNING,
                    "unknown_voltage",
                    f"Nominal voltage is unknown in zone '{zone_id}'.",
                    electrical_node_id=nodes[0] if nodes else None,
                    related_node_ids=nodes,
                ))

            port_ids = tuple(
                sorted(
                    (
                        port_id
                        for port_id, node_id in port_to_node.items()
                        if node_id in node_set
                    ),
                    key=lambda item: item.value,
                )
            )
            equipment_groups = tuple(
                sorted(
                    (
                        equipment_group
                        for equipment_group, mapped_zone in
                        voltage_zone_by_equipment_group.items()
                        if mapped_zone == zone_id
                    ),
                    key=lambda item: (item[0].value, item[1]),
                )
            )
            zones[zone_id] = VoltageZone(
                zone_id,
                VoltageResolution(
                    status,
                    voltage_id,
                    tuple(exposed_candidates),
                    tuple(evidence),
                ),
                nodes,
                port_ids,
                equipment_groups,
            )

        return (
            zones,
            voltage_zone_by_node,
            voltage_zone_by_port,
            voltage_zone_by_equipment_group,
        )

    @staticmethod
    def _add_post_topology_diagnostics(
        links: Mapping[TopologyLinkId, TopologyLink],
        components: Mapping[TopologyComponentId, TopologyComponent],
        component_by_node: Mapping[ElectricalNodeId, TopologyComponentId],
        diagnostics: list[TopologyDiagnostic],
    ) -> None:
        for link in links.values():
            if (
                link.active
                or link.conductivity != Conductivity.OPEN
                or link.resolved_position is None
                or link.resolved_position.position != SwitchPosition.OPEN
                or len(link.node_ids) < 2
            ):
                continue
            endpoint_components = {
                component_by_node[node_id]
                for node_id in link.node_ids
                if node_id in component_by_node
            }
            if endpoint_components and all(
                components[component_id].energization == Energization.ENERGIZED
                for component_id in endpoint_components
            ):
                diagnostics.append(TopologyDiagnostic(
                    DiagnosticSeverity.INFO,
                    "open_device_both_sides_energized",
                    f"Open switching equipment '{link.equipment_id}' is energized on both sides.",
                    equipment_id=link.equipment_id,
                    related_port_ids=link.port_ids,
                    related_node_ids=link.node_ids,
                ))

    @staticmethod
    def _deduplicate_diagnostics(
        diagnostics: Iterable[TopologyDiagnostic],
    ) -> list[TopologyDiagnostic]:
        values: dict[tuple[Any, ...], TopologyDiagnostic] = {}
        for item in diagnostics:
            key = (
                item.severity,
                item.code,
                item.message,
                item.equipment_id,
                item.port_id,
                item.electrical_node_id,
                item.connection_id,
                item.related_equipment_ids,
                item.related_port_ids,
                item.related_node_ids,
                item.related_connection_ids,
                tuple(item.context.items()),
            )
            values[key] = item
        return sorted(values.values(), key=lambda item: item.sort_key())


__all__ = [
    "ConcurrentTopologyMutationError",
    "NORMAL_STATE",
    "NormalStateSelection",
    "StateSelection",
    "TopologyEngine",
    "TopologyInputError",
    "electrical_model_identity",
]
