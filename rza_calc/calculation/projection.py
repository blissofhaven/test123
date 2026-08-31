# -*- coding: utf-8 -*-
"""Производное расчётное представление канонической электрической модели.

Модуль не является редактируемой «второй схемой».  Каждый
``CalculationProjection`` заново строится из ``ElectricalModel`` и
состояния сети. В нём нет координат, SVG или методов мутации.

Формулы ТКЗ здесь намеренно не реализованы. Слой только
нормализует узлы, ветви, владение и трассировку.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..domain.catalog_snapshot import ParameterOverride
from ..domain.electrical import (
    ElectricalModel,
    ElectricalNodeId,
    EquipmentAvailability,
    EquipmentId,
    OperatingState,
    OperatingStateId,
    PortId,
    StableId,
    VoltageClassId,
    deterministic_id,
    thaw_json,
)
from ..domain.fingerprint import electrical_model_fingerprint
from ..topology import (
    DiagnosticSeverity,
    TopologyEngine,
    TopologySnapshot,
    electrical_model_identity,
)


class CalculationProjectionError(ValueError):
    """Проекцию нельзя построить без домыслов."""


class CalculationNodeId(StableId):
    prefix = "calc_node"


class CalculationBranchId(StableId):
    prefix = "calc_branch"


class CalculationNodeKind(StrEnum):
    ELECTRICAL = "electrical"
    TRANSFORMER_STAR = "transformer_star"
    INTERNAL_EMF = "internal_emf"
    GROUND = "ground"


class CalculationBranchKind(StrEnum):
    LINE = "line"
    TRANSFORMER = "transformer"
    TRANSFORMER_WINDING = "transformer_winding"
    SWITCH = "switch"
    RECLOSER = "recloser"
    SOURCE = "source"
    GENERATOR = "generator"
    SHUNT = "shunt"
    CONNECTION = "connection"


class ProjectionBehaviorKind(StrEnum):
    NONE = "none"
    TWO_TERMINAL = "two_terminal"
    TRANSFORMER_3W = "transformer_3w"
    SOURCE = "source"
    GENERATOR = "generator"
    LOAD = "load"


def _finite_optional(value: float | int | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name}: ожидалось число или null.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name}: NaN и Infinity недопустимы.")
    return result


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN и Infinity нельзя хранить в расчётной проекции.")
        return value
    if isinstance(value, StableId):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError("Ключи JSON-объекта должны быть непустыми строками.")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"Тип {type(value).__name__} не является JSON-совместимым.")


def _ordered(values: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(
        dict(sorted(values.items(), key=lambda item: getattr(item[0], "value", str(item[0]))))
    )


@dataclass(frozen=True, slots=True)
class SequenceImpedance:
    """Контейнер параметров последовательностей без расчётных формул."""

    r1_ohm: float | None = None
    x1_ohm: float | None = None
    r2_ohm: float | None = None
    x2_ohm: float | None = None
    r0_ohm: float | None = None
    x0_ohm: float | None = None
    g1_siemens: float | None = None
    b1_siemens: float | None = None
    g0_siemens: float | None = None
    b0_siemens: float | None = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _finite_optional(getattr(self, name), name))

    @classmethod
    def from_mapping(
        cls, properties: Mapping[str, Any], *, prefix: str = ""
    ) -> "SequenceImpedance":
        names = cls.__dataclass_fields__
        return cls(**{
            name: properties.get(prefix + name)
            for name in names
            if properties.get(prefix + name) is not None
        })


@dataclass(frozen=True, slots=True)
class CalculationConstructionSegment:
    """Неизменяемый снимок конструктивного участка внутри ветви."""

    source_id: str
    length_mm: int
    properties: Mapping[str, Any] = field(default_factory=dict)
    line_kind: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise TypeError("source_id конструктивного участка должен быть непустой строкой.")
        if isinstance(self.length_mm, bool) or not isinstance(self.length_mm, int) or self.length_mm <= 0:
            raise ValueError("Длина конструктивного участка должна быть целой и положительной.")
        if self.line_kind is not None and (
            not isinstance(self.line_kind, str) or not self.line_kind.strip()
        ):
            raise TypeError("line_kind конструктивного участка должен быть строкой или null.")
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class CalculationNode:
    id: CalculationNodeId
    kind: CalculationNodeKind
    electrical_node_id: ElectricalNodeId | None = None
    owner_equipment_id: EquipmentId | None = None
    role: str = ""
    voltage_class_id: VoltageClassId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, CalculationNodeId):
            raise TypeError("CalculationNode.id должен быть CalculationNodeId.")
        if not isinstance(self.kind, CalculationNodeKind):
            object.__setattr__(self, "kind", CalculationNodeKind(self.kind))
        if self.kind is CalculationNodeKind.ELECTRICAL:
            if self.electrical_node_id is None or self.owner_equipment_id is not None:
                raise ValueError("Расчётный электрический узел должен ссылаться ровно на ElectricalNodeId.")
        elif self.owner_equipment_id is None or self.electrical_node_id is not None:
            raise ValueError("Внутренний расчётный узел должен иметь владельца EquipmentId.")


@dataclass(frozen=True, slots=True)
class CalculationBranch:
    id: CalculationBranchId
    kind: CalculationBranchKind
    equipment_id: EquipmentId
    from_node_id: CalculationNodeId
    to_node_id: CalculationNodeId
    active: bool
    role: str = "main"
    port_ids: tuple[PortId, ...] = ()
    sequence_impedance: SequenceImpedance = field(default_factory=SequenceImpedance)
    nominal_ratio: float | None = None
    construction_segments: tuple[CalculationConstructionSegment, ...] = ()
    properties: Mapping[str, Any] = field(default_factory=dict)
    parameter_overrides: Mapping[str, ParameterOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, CalculationBranchId):
            raise TypeError("CalculationBranch.id должен быть CalculationBranchId.")
        if not isinstance(self.kind, CalculationBranchKind):
            object.__setattr__(self, "kind", CalculationBranchKind(self.kind))
        if not isinstance(self.equipment_id, EquipmentId):
            raise TypeError("CalculationBranch.equipment_id должен быть EquipmentId.")
        if not isinstance(self.from_node_id, CalculationNodeId) or not isinstance(self.to_node_id, CalculationNodeId):
            raise TypeError("Концы расчётной ветви должны быть CalculationNodeId.")
        if not isinstance(self.active, bool):
            raise TypeError("CalculationBranch.active должен быть bool.")
        if not isinstance(self.role, str) or not self.role:
            raise TypeError("CalculationBranch.role должна быть непустой строкой.")
        if any(not isinstance(item, PortId) for item in self.port_ids):
            raise TypeError("CalculationBranch.port_ids содержит не PortId.")
        if len(self.port_ids) != len(set(self.port_ids)):
            raise ValueError("CalculationBranch.port_ids содержит повторы.")
        if not isinstance(self.sequence_impedance, SequenceImpedance):
            raise TypeError("sequence_impedance должен быть SequenceImpedance.")
        object.__setattr__(self, "nominal_ratio", _finite_optional(self.nominal_ratio, "nominal_ratio"))
        object.__setattr__(self, "construction_segments", tuple(self.construction_segments))
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        overrides: dict[str, ParameterOverride] = {}
        for key, value in self.parameter_overrides.items():
            if not isinstance(key, str) or not key or not isinstance(value, ParameterOverride):
                raise TypeError("parameter_overrides должен отображать строки в ParameterOverride.")
            if value.parameter_key != key:
                raise ValueError("ParameterOverride не совпадает с ключом mapping.")
            overrides[key] = value
        object.__setattr__(self, "parameter_overrides", MappingProxyType(dict(sorted(overrides.items()))))


@dataclass(frozen=True, slots=True)
class ProjectionDiagnostic:
    code: str
    message: str
    equipment_id: EquipmentId | None = None
    severity: str = "warning"


@dataclass(frozen=True, slots=True)
class ProjectionBehaviorHandler:
    behavior_key: str
    kind: ProjectionBehaviorKind
    roles: tuple[str, ...]
    branch_kind: CalculationBranchKind | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.behavior_key, str) or not self.behavior_key:
            raise TypeError("behavior_key должен быть непустой строкой.")
        object.__setattr__(self, "roles", tuple(self.roles))


class CalculationProjectionRegistry:
    """Неизменяемый реестр явных, не угадываемых behavior handlers."""

    def __init__(self, handlers: Iterable[ProjectionBehaviorHandler]):
        values: dict[str, ProjectionBehaviorHandler] = {}
        for handler in handlers:
            if handler.behavior_key in values:
                raise ValueError(f"Behavior '{handler.behavior_key}' зарегистрирован дважды.")
            values[handler.behavior_key] = handler
        self._handlers = MappingProxyType(dict(sorted(values.items())))

    @property
    def handlers(self) -> Mapping[str, ProjectionBehaviorHandler]:
        return self._handlers

    def get(self, behavior_key: str) -> ProjectionBehaviorHandler | None:
        return self._handlers.get(behavior_key)


def builtin_projection_registry() -> CalculationProjectionRegistry:
    H = ProjectionBehaviorHandler
    K = ProjectionBehaviorKind
    B = CalculationBranchKind
    return CalculationProjectionRegistry((
        H("bus", K.NONE, ("terminal",)),
        H("line", K.TWO_TERMINAL, ("from", "to"), B.LINE),
        H("line_section", K.TWO_TERMINAL, ("from", "to"), B.LINE),
        H("transformer_2w", K.TWO_TERMINAL, ("hv", "lv"), B.TRANSFORMER),
        H("transformer_3w", K.TRANSFORMER_3W, ("hv", "mv", "lv")),
        H("switch", K.TWO_TERMINAL, ("a", "b"), B.SWITCH),
        H("recloser", K.TWO_TERMINAL, ("a", "b"), B.RECLOSER),
        H("source", K.SOURCE, ("terminal",), B.SOURCE),
        H("generator", K.GENERATOR, ("terminal",), B.GENERATOR),
        H("load", K.LOAD, ("terminal",), B.SHUNT),
        H("legacy.line", K.TWO_TERMINAL, ("from", "to"), B.LINE),
        H("legacy.transformer_2w", K.TWO_TERMINAL, ("from", "to"), B.TRANSFORMER),
        H("legacy.transformer_3w", K.TRANSFORMER_3W, ("hv", "mv", "lv")),
        H("legacy.tie", K.TWO_TERMINAL, ("from", "to"), B.SWITCH),
        H("legacy.branch", K.TWO_TERMINAL, ("from", "to"), B.CONNECTION),
        H("legacy.source", K.SOURCE, ("terminal",), B.SOURCE),
        H("legacy.generator", K.GENERATOR, ("terminal",), B.GENERATOR),
        H("legacy.load", K.LOAD, ("terminal",), B.SHUNT),
    ))


def calculation_node_id(electrical_node_id: ElectricalNodeId) -> CalculationNodeId:
    return deterministic_id(
        CalculationNodeId, "electrical-node", electrical_node_id.value
    )


def internal_calculation_node_id(
    equipment_id: EquipmentId, role: str
) -> CalculationNodeId:
    return deterministic_id(
        CalculationNodeId, "equipment-internal-node", equipment_id.value, role
    )


def calculation_branch_id(
    equipment_id: EquipmentId, role: str = "main"
) -> CalculationBranchId:
    return deterministic_id(
        CalculationBranchId, "equipment-calculation-branch", equipment_id.value, role
    )


@dataclass(frozen=True, slots=True)
class CalculationProjection:
    model_identity: str
    model_revision: int
    model_fingerprint: str
    state_id: OperatingStateId | None
    topology_fingerprint: str
    nodes: Mapping[CalculationNodeId, CalculationNode]
    branches: Mapping[CalculationBranchId, CalculationBranch]
    electrical_node_map: Mapping[ElectricalNodeId, CalculationNodeId]
    equipment_to_branches: Mapping[EquipmentId, tuple[CalculationBranchId, ...]]
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        nodes = dict(self.nodes)
        branches = dict(self.branches)
        for node_id, node in nodes.items():
            if node.id != node_id:
                raise ValueError("Узел проекции не совпадает с ключом mapping.")
        for branch_id, branch in branches.items():
            if branch.id != branch_id:
                raise ValueError("Ветвь проекции не совпадает с ключом mapping.")
            if branch.from_node_id not in nodes or branch.to_node_id not in nodes:
                raise ValueError("Ветвь проекции ссылается на отсутствующий узел.")
        electrical_map = dict(self.electrical_node_map)
        if any(value not in nodes for value in electrical_map.values()):
            raise ValueError("electrical_node_map ссылается на отсутствующий узел.")
        by_equipment: dict[EquipmentId, tuple[CalculationBranchId, ...]] = {}
        for equipment_id, branch_ids in self.equipment_to_branches.items():
            normalized = tuple(sorted(set(branch_ids), key=lambda item: item.value))
            if any(item not in branches or branches[item].equipment_id != equipment_id for item in normalized):
                raise ValueError("equipment_to_branches повреждён.")
            by_equipment[equipment_id] = normalized
        object.__setattr__(self, "nodes", _ordered(nodes))
        object.__setattr__(self, "branches", _ordered(branches))
        object.__setattr__(self, "electrical_node_map", _ordered(electrical_map))
        object.__setattr__(self, "equipment_to_branches", _ordered(by_equipment))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def branches_for_equipment(
        self, equipment_id: EquipmentId
    ) -> tuple[CalculationBranch, ...]:
        return tuple(
            self.branches[item]
            for item in self.equipment_to_branches.get(equipment_id, ())
        )

    def semantic_fingerprint(self) -> str:
        payload = {
            "model_revision": self.model_revision,
            "model_fingerprint": self.model_fingerprint,
            "state_id": self.state_id.value if self.state_id else None,
            "nodes": [
                {
                    "id": item.id.value,
                    "kind": item.kind.value,
                    "electrical_node_id": item.electrical_node_id.value if item.electrical_node_id else None,
                    "owner_equipment_id": item.owner_equipment_id.value if item.owner_equipment_id else None,
                    "role": item.role,
                }
                for item in self.nodes.values()
            ],
            "branches": [
                {
                    "id": item.id.value,
                    "kind": item.kind.value,
                    "equipment_id": item.equipment_id.value,
                    "from": item.from_node_id.value,
                    "to": item.to_node_id.value,
                    "active": item.active,
                    "role": item.role,
                    "ports": [value.value for value in item.port_ids],
                    "sequence_impedance": {
                        key: getattr(item.sequence_impedance, key)
                        for key in item.sequence_impedance.__dataclass_fields__
                    },
                    "nominal_ratio": item.nominal_ratio,
                    "construction_segments": [
                        {
                            "source_id": segment.source_id,
                            "length_mm": segment.length_mm,
                            "line_kind": segment.line_kind,
                            "properties": thaw_json(segment.properties),
                            "extensions": thaw_json(segment.extensions),
                        }
                        for segment in item.construction_segments
                    ],
                    "properties": thaw_json(item.properties),
                }
                for item in self.branches.values()
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class CalculationProjectionBuilder:
    """Односторонний compiler: ElectricalModel -> immutable projection."""

    def __init__(
        self,
        registry: CalculationProjectionRegistry | None = None,
        topology_engine: TopologyEngine | None = None,
    ) -> None:
        self.registry = registry or builtin_projection_registry()
        self.topology_engine = topology_engine or TopologyEngine()

    @staticmethod
    def _port_and_node(
        model: ElectricalModel,
        equipment_id: EquipmentId,
        role: str,
        node_map: Mapping[ElectricalNodeId, CalculationNodeId],
    ) -> tuple[PortId, CalculationNodeId]:
        port = model.port_by_role(equipment_id, role)
        node = model.node_for_port(port.id)
        if node is None:
            raise CalculationProjectionError(
                f"Оборудование '{equipment_id}': порт '{role}' не подключён."
            )
        return port.id, node_map[node.id]

    @staticmethod
    def _active_for_equipment(
        snapshot: TopologySnapshot, equipment_id: EquipmentId
    ) -> bool:
        availability = snapshot.operating_state.availability.get(
            equipment_id, EquipmentAvailability.IN_SERVICE
        )
        if availability is EquipmentAvailability.OUT_OF_SERVICE:
            return False
        source = snapshot.sources.get(equipment_id)
        if source is not None:
            return source.active
        link_ids = snapshot.link_ids_by_equipment.get(equipment_id, ())
        if link_ids:
            return any(snapshot.links[item].active for item in link_ids)
        return True

    @staticmethod
    def _construction_segments(
        model: ElectricalModel,
        equipment_id: EquipmentId,
        effective_properties: Mapping[str, Any],
    ) -> tuple[CalculationConstructionSegment, ...]:
        line_section = model.line_sections.get(equipment_id)
        if line_section is None:
            return ()
        raw_segments = getattr(line_section, "construction_segments", None)
        if raw_segments:
            result: list[CalculationConstructionSegment] = []
            effective_resolver = getattr(
                model, "effective_line_construction_segment_properties", None
            )
            for index, segment in enumerate(raw_segments):
                segment_id = getattr(segment, "id", getattr(segment, "source_id", None))
                length_mm = getattr(segment, "length_mm", None)
                properties = getattr(segment, "properties", {})
                line_kind = getattr(segment, "line_kind", None)
                extensions = getattr(segment, "extensions", {})
                if isinstance(segment, Mapping):
                    segment_id = segment.get("id", segment.get("source_id", segment_id))
                    length_mm = segment.get("length_mm", length_mm)
                    properties = segment.get("properties", properties)
                    line_kind = segment.get("line_kind", line_kind)
                    extensions = segment.get("extensions", extensions)
                if callable(effective_resolver) and segment_id is not None:
                    # The Domain model is authoritative for inheritance and
                    # segment overrides.  The projection only freezes its
                    # already-resolved result and performs no catalog lookup.
                    properties = effective_resolver(equipment_id, segment_id)
                result.append(CalculationConstructionSegment(
                    getattr(segment_id, "value", str(segment_id or f"{equipment_id.value}:{index}")),
                    length_mm,
                    properties,
                    getattr(line_kind, "value", line_kind),
                    extensions,
                ))
            return tuple(result)
        # Compatibility with project v4/v5: one old LineSection becomes one
        # construction segment in the derived view.  No mutation is performed.
        return (CalculationConstructionSegment(
            equipment_id.value,
            line_section.length_mm,
            effective_properties,
            getattr(
                getattr(model.logical_line_for_section(equipment_id), "line_kind", None),
                "value",
                getattr(model.logical_line_for_section(equipment_id), "line_kind", None),
            ),
        ),)

    @staticmethod
    def _nominal_ratio(model: ElectricalModel, equipment: Any) -> float | None:
        hv_id = equipment.voltage_class_by_group.get("hv")
        lv_id = equipment.voltage_class_by_group.get("lv")
        if hv_id is None or lv_id is None:
            return None
        hv = model.voltage_classes.get(hv_id)
        lv = model.voltage_classes.get(lv_id)
        if hv is None or lv is None or lv.nominal_voltage_v == 0:
            return None
        return hv.nominal_voltage_v / lv.nominal_voltage_v

    def build(
        self,
        model: ElectricalModel,
        state: OperatingStateId | OperatingState | None = None,
        *,
        topology_snapshot: TopologySnapshot | None = None,
    ) -> CalculationProjection:
        if not isinstance(model, ElectricalModel):
            raise TypeError("CalculationProjectionBuilder.build ожидает ElectricalModel.")
        if state is not None and topology_snapshot is not None:
            raise ValueError("Задайте либо state, либо topology_snapshot.")
        integrity = model.validate_integrity()
        if integrity:
            raise CalculationProjectionError(
                "Электрическая модель нарушает целостность:\n- "
                + "\n- ".join(item.message for item in integrity)
            )
        snapshot = topology_snapshot or self.topology_engine.compile(model, state)
        model_fingerprint = electrical_model_fingerprint(model)
        snapshot.assert_compatible(
            model_identity=electrical_model_identity(model),
            model_revision=model.revision,
            model_fingerprint=model_fingerprint,
            registry_signature=self.topology_engine.handlers.fingerprint,
        )
        blockers = [
            item.message
            for item in snapshot.diagnostics
            if item.severity is DiagnosticSeverity.ERROR
        ]
        if blockers:
            raise CalculationProjectionError(
                "Топология содержит блокирующие ошибки:\n- " + "\n- ".join(blockers)
            )

        nodes: dict[CalculationNodeId, CalculationNode] = {}
        node_map: dict[ElectricalNodeId, CalculationNodeId] = {}
        for node in sorted(model.electrical_nodes.values(), key=lambda item: item.id.value):
            output_id = calculation_node_id(node.id)
            voltage = snapshot.voltage_by_node[node.id].voltage_class_id
            nodes[output_id] = CalculationNode(
                output_id,
                CalculationNodeKind.ELECTRICAL,
                electrical_node_id=node.id,
                voltage_class_id=voltage,
            )
            node_map[node.id] = output_id

        branches: dict[CalculationBranchId, CalculationBranch] = {}
        equipment_to_branches: dict[EquipmentId, tuple[CalculationBranchId, ...]] = {}
        diagnostics: list[ProjectionDiagnostic] = []

        for equipment in sorted(model.equipment.values(), key=lambda item: item.id.value):
            definition = model.equipment_type(equipment.type_id, equipment.type_version)
            handler = self.registry.get(definition.behavior_key)
            if handler is None:
                raise CalculationProjectionError(
                    f"Оборудование '{equipment.id}': behavior "
                    f"'{definition.behavior_key}' не имеет расчётного handler."
                )
            if handler.kind is ProjectionBehaviorKind.NONE:
                equipment_to_branches[equipment.id] = ()
                continue

            properties = thaw_json(model.effective_equipment_properties(equipment.id))
            active = self._active_for_equipment(snapshot, equipment.id)
            created: list[CalculationBranchId] = []

            if handler.kind in {ProjectionBehaviorKind.SOURCE, ProjectionBehaviorKind.GENERATOR}:
                port_id, terminal_node = self._port_and_node(
                    model, equipment.id, handler.roles[0], node_map
                )
                internal_id = internal_calculation_node_id(equipment.id, "emf")
                nodes[internal_id] = CalculationNode(
                    internal_id,
                    CalculationNodeKind.INTERNAL_EMF,
                    owner_equipment_id=equipment.id,
                    role="emf",
                    voltage_class_id=equipment.voltage_class_by_group.get("main"),
                )
                branch_id = calculation_branch_id(equipment.id)
                branches[branch_id] = CalculationBranch(
                    branch_id,
                    handler.branch_kind or CalculationBranchKind.SOURCE,
                    equipment.id,
                    internal_id,
                    terminal_node,
                    active,
                    port_ids=(port_id,),
                    sequence_impedance=SequenceImpedance.from_mapping(properties),
                    properties=properties,
                )
                created.append(branch_id)

            elif handler.kind is ProjectionBehaviorKind.LOAD:
                port_id, terminal_node = self._port_and_node(
                    model, equipment.id, handler.roles[0], node_map
                )
                ground_id = internal_calculation_node_id(equipment.id, "ground")
                nodes[ground_id] = CalculationNode(
                    ground_id,
                    CalculationNodeKind.GROUND,
                    owner_equipment_id=equipment.id,
                    role="ground",
                )
                branch_id = calculation_branch_id(equipment.id)
                branches[branch_id] = CalculationBranch(
                    branch_id,
                    CalculationBranchKind.SHUNT,
                    equipment.id,
                    terminal_node,
                    ground_id,
                    active,
                    port_ids=(port_id,),
                    sequence_impedance=SequenceImpedance.from_mapping(properties),
                    properties=properties,
                )
                created.append(branch_id)

            elif handler.kind is ProjectionBehaviorKind.TRANSFORMER_3W:
                star_id = internal_calculation_node_id(equipment.id, "star")
                nodes[star_id] = CalculationNode(
                    star_id,
                    CalculationNodeKind.TRANSFORMER_STAR,
                    owner_equipment_id=equipment.id,
                    role="star",
                )
                topology_links = tuple(
                    snapshot.links[item]
                    for item in snapshot.link_ids_by_equipment.get(equipment.id, ())
                )
                active_ports = {
                    port_id
                    for link in topology_links
                    for port_id in link.active_port_ids
                }
                for role in handler.roles:
                    port_id, terminal_node = self._port_and_node(
                        model, equipment.id, role, node_map
                    )
                    branch_id = calculation_branch_id(equipment.id, role)
                    leg_active = active and (not active_ports or port_id in active_ports)
                    branches[branch_id] = CalculationBranch(
                        branch_id,
                        CalculationBranchKind.TRANSFORMER_WINDING,
                        equipment.id,
                        terminal_node,
                        star_id,
                        leg_active,
                        role=role,
                        port_ids=(port_id,),
                        sequence_impedance=SequenceImpedance.from_mapping(
                            properties, prefix=role + "_"
                        ),
                        properties=properties,
                    )
                    created.append(branch_id)

            else:
                first_port, first_node = self._port_and_node(
                    model, equipment.id, handler.roles[0], node_map
                )
                second_port, second_node = self._port_and_node(
                    model, equipment.id, handler.roles[1], node_map
                )
                branch_id = calculation_branch_id(equipment.id)
                construction = self._construction_segments(
                    model, equipment.id, properties
                )
                branches[branch_id] = CalculationBranch(
                    branch_id,
                    handler.branch_kind or CalculationBranchKind.CONNECTION,
                    equipment.id,
                    first_node,
                    second_node,
                    active,
                    port_ids=(first_port, second_port),
                    sequence_impedance=SequenceImpedance.from_mapping(properties),
                    nominal_ratio=(
                        self._nominal_ratio(model, equipment)
                        if handler.branch_kind is CalculationBranchKind.TRANSFORMER
                        else None
                    ),
                    construction_segments=construction,
                    properties=properties,
                )
                created.append(branch_id)

            equipment_to_branches[equipment.id] = tuple(created)

        return CalculationProjection(
            electrical_model_identity(model),
            model.revision,
            model_fingerprint,
            snapshot.operating_state.state_id,
            snapshot.topology_fingerprint,
            nodes,
            branches,
            node_map,
            equipment_to_branches,
            tuple(diagnostics),
        )


__all__ = [
    "CalculationBranch",
    "CalculationBranchId",
    "CalculationBranchKind",
    "CalculationConstructionSegment",
    "CalculationNode",
    "CalculationNodeId",
    "CalculationNodeKind",
    "CalculationProjection",
    "CalculationProjectionBuilder",
    "CalculationProjectionError",
    "CalculationProjectionRegistry",
    "ProjectionBehaviorHandler",
    "ProjectionBehaviorKind",
    "ProjectionDiagnostic",
    "SequenceImpedance",
    "builtin_projection_registry",
    "calculation_branch_id",
    "calculation_node_id",
    "internal_calculation_node_id",
]
