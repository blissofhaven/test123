# -*- coding: utf-8 -*-
"""Каноническая электрическая Domain Model.

Модуль не импортирует ``core`` и не знает о SVG, координатах или формулах.
Электрическая связность хранится только как подключения стабильных портов к
стабильным электрическим узлам. Расчётное представление строит отдельный
адаптер.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping
from uuid import UUID, uuid4, uuid5


class DomainInvariantError(ValueError):
    """Операция нарушает инвариант электрической модели."""


def _require_string(value: Any, field_name: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        expected = "строкой" if allow_empty else "непустой строкой"
        raise DomainInvariantError(f"{field_name} должен быть {expected}.")


def _require_bool(value: Any, field_name: str) -> None:
    if not isinstance(value, bool):
        raise DomainInvariantError(f"{field_name} должен быть логическим значением.")


def _require_positive_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DomainInvariantError(f"{field_name} должен быть целым числом ≥ 1.")


def _require_mapping(value: Any, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise DomainInvariantError(f"{field_name} должен быть объектом ключ-значение.")


def _freeze_json(value: Any) -> Any:
    """Скопировать JSON-совместимое значение в неизменяемое представление."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainInvariantError("NaN и бесконечность нельзя хранить в Domain Model.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DomainInvariantError("Ключ JSON-объекта должен быть непустой строкой.")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StableId):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    raise DomainInvariantError(
        f"Значение типа {type(value).__name__} нельзя хранить в Domain Model."
    )


def thaw_json(value: Any) -> Any:
    """Вернуть обычные dict/list для JSON codec и legacy adapter."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    if isinstance(value, StableId):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    return value


_PROPERTY_VALUE_KINDS = frozenset(
    {"json", "string", "boolean", "integer", "number", "object", "array"}
)


def _matches_property_value_kind(value: Any, value_kind: str) -> bool:
    """Return whether a frozen or ordinary JSON value matches its schema kind."""
    return (
        value_kind == "json"
        or (value_kind == "string" and isinstance(value, str))
        or (value_kind == "boolean" and isinstance(value, bool))
        or (
            value_kind == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
        )
        or (
            value_kind == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
        or (value_kind == "object" and isinstance(value, Mapping))
        or (value_kind == "array" and isinstance(value, (list, tuple)))
    )


@dataclass(frozen=True, slots=True, order=True)
class StableId:
    """Runtime-типизированный постоянный ID, сериализуемый одной строкой."""

    value: str
    prefix: ClassVar[str] = "id"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise DomainInvariantError("ID должен быть непустой строкой.")
        if self.value != self.value.strip():
            raise DomainInvariantError("ID не должен начинаться или заканчиваться пробелом.")
        if any(ord(char) < 32 for char in self.value):
            raise DomainInvariantError("ID не должен содержать управляющие символы.")

    def __str__(self) -> str:
        return self.value

    @classmethod
    def new(cls):
        return cls(f"{cls.prefix}_{uuid4().hex}")


class EquipmentId(StableId):
    prefix = "eq"


class PortId(StableId):
    prefix = "port"


class ElectricalNodeId(StableId):
    prefix = "node"


class ConnectionId(StableId):
    prefix = "conn"


class OperatingStateId(StableId):
    prefix = "state"


class EquipmentTypeId(StableId):
    prefix = "type"


class VoltageClassId(StableId):
    prefix = "voltage"


class PortKindId(StableId):
    prefix = "portkind"


class LogicalLineId(StableId):
    """Stable user-facing identity of one line route."""

    prefix = "lline"


class FeederId(StableId):
    """Стабильная принадлежность частей маршрута, разделённых аппаратом."""

    prefix = "feeder"


class LineConstructionSegmentId(StableId):
    """Постоянный ID конструктивного участка внутри электрической ветви."""

    prefix = "lseg"


LEGACY_ID_NAMESPACE = UUID("4d68dc70-3cce-4e1d-a99a-7bb5846fd26f")


def _require_id(value: Any, expected_type: type[StableId], field_name: str) -> None:
    if not isinstance(value, expected_type):
        raise DomainInvariantError(
            f"{field_name} должен иметь runtime-тип {expected_type.__name__}."
        )


def deterministic_id(id_type: type[StableId], *parts: str) -> StableId:
    """Детерминированный ID для миграции, независимый от имени и порядка строк."""
    if not isinstance(id_type, type) or not issubclass(id_type, StableId):
        raise DomainInvariantError("Фабрике ID нужен конкретный runtime-тип StableId.")
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise DomainInvariantError("Для детерминированного ID нужны непустые части.")
    # Length-prefix не допускает коллизию границ вроде ('a/b', 'c') и
    # ('a', 'b/c'), в отличие от простого join с разделителем.
    canonical = "".join(f"{len(part)}:{part}" for part in parts)
    token = uuid5(LEGACY_ID_NAMESPACE, canonical).hex
    return id_type(f"{id_type.prefix}_{token}")


@dataclass(frozen=True, slots=True)
class VoltageClass:
    id: VoltageClassId
    nominal_voltage_v: int
    display_name: str
    system_kind: str = "ac"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.id, VoltageClassId, "VoltageClass.id")
        if isinstance(self.nominal_voltage_v, bool) or not isinstance(
            self.nominal_voltage_v, int
        ):
            raise DomainInvariantError("Номинальное напряжение задаётся целым числом вольт.")
        if self.nominal_voltage_v <= 0:
            raise DomainInvariantError("Номинальное напряжение должно быть больше нуля.")
        _require_string(self.display_name, "VoltageClass.display_name", allow_empty=False)
        _require_string(self.system_kind, "VoltageClass.system_kind", allow_empty=False)
        _require_mapping(self.extensions, "VoltageClass.extensions")
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class PropertyDefinition:
    key: str
    value_kind: str = "json"
    required: bool = False
    unit: str = ""
    default: Any = None

    def __post_init__(self) -> None:
        _require_string(self.key, "PropertyDefinition.key", allow_empty=False)
        _require_string(
            self.value_kind, "PropertyDefinition.value_kind", allow_empty=False
        )
        _require_bool(self.required, "PropertyDefinition.required")
        _require_string(self.unit, "PropertyDefinition.unit")
        if self.value_kind not in _PROPERTY_VALUE_KINDS:
            raise DomainInvariantError(
                f"PropertyDefinition.value_kind должен быть одним из "
                f"{sorted(_PROPERTY_VALUE_KINDS)}."
            )
        frozen_default = _freeze_json(self.default)
        if frozen_default is not None and not _matches_property_value_kind(
            frozen_default, self.value_kind
        ):
            raise DomainInvariantError(
                f"Значение по умолчанию свойства '{self.key}' не соответствует "
                f"value_kind='{self.value_kind}'."
            )
        object.__setattr__(self, "default", frozen_default)


@dataclass(frozen=True, slots=True)
class PortDefinition:
    role: str
    display_name: str
    kind_id: PortKindId
    voltage_group: str | None = None
    allowed_voltage_class_ids: frozenset[VoltageClassId] | None = None
    required: bool = True
    max_connections: int = 1

    def __post_init__(self) -> None:
        _require_id(self.kind_id, PortKindId, "PortDefinition.kind_id")
        _require_string(self.role, "PortDefinition.role", allow_empty=False)
        _require_string(
            self.display_name, "PortDefinition.display_name", allow_empty=False
        )
        if self.voltage_group is not None:
            _require_string(
                self.voltage_group,
                "PortDefinition.voltage_group",
                allow_empty=False,
            )
        _require_bool(self.required, "PortDefinition.required")
        _require_positive_int(self.max_connections, "PortDefinition.max_connections")
        if self.max_connections != 1:
            raise DomainInvariantError(
                "В нормализованной модели порт подключается ровно к одному узлу. "
                "Многоточечная связь представляется несколькими портами одного узла."
            )
        if self.allowed_voltage_class_ids is not None:
            for voltage_id in self.allowed_voltage_class_ids:
                _require_id(
                    voltage_id,
                    VoltageClassId,
                    "PortDefinition.allowed_voltage_class_ids[]",
                )
            object.__setattr__(
                self,
                "allowed_voltage_class_ids",
                frozenset(self.allowed_voltage_class_ids),
            )


@dataclass(frozen=True, slots=True)
class EquipmentTypeDefinition:
    id: EquipmentTypeId
    schema_version: int
    display_name: str
    behavior_key: str
    port_definitions: tuple[PortDefinition, ...]
    property_definitions: tuple[PropertyDefinition, ...] = ()
    capabilities: frozenset[str] = frozenset()
    allow_additional_properties: bool = True
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.id, EquipmentTypeId, "EquipmentTypeDefinition.id")
        _require_positive_int(
            self.schema_version, "EquipmentTypeDefinition.schema_version"
        )
        _require_string(
            self.display_name,
            "EquipmentTypeDefinition.display_name",
            allow_empty=False,
        )
        _require_string(
            self.behavior_key,
            "EquipmentTypeDefinition.behavior_key",
            allow_empty=False,
        )
        _require_bool(
            self.allow_additional_properties,
            "EquipmentTypeDefinition.allow_additional_properties",
        )
        _require_mapping(self.extensions, "EquipmentTypeDefinition.extensions")
        object.__setattr__(self, "port_definitions", tuple(self.port_definitions))
        object.__setattr__(self, "property_definitions", tuple(self.property_definitions))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if any(not isinstance(item, str) or not item.strip()
               for item in self.capabilities):
            raise DomainInvariantError(
                "EquipmentTypeDefinition.capabilities содержит некорректное значение."
            )
        if any(not isinstance(item, PortDefinition) for item in self.port_definitions):
            raise DomainInvariantError(
                "EquipmentTypeDefinition.port_definitions содержит неверный тип."
            )
        if any(not isinstance(item, PropertyDefinition)
               for item in self.property_definitions):
            raise DomainInvariantError(
                "EquipmentTypeDefinition.property_definitions содержит неверный тип."
            )
        roles = [item.role for item in self.port_definitions]
        if len(roles) != len(set(roles)):
            raise DomainInvariantError(
                f"Тип '{self.id}': роли портов должны быть уникальны."
            )
        keys = [item.key for item in self.property_definitions]
        if len(keys) != len(set(keys)):
            raise DomainInvariantError(
                f"Тип '{self.id}': определения свойств должны быть уникальны."
            )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


class SwitchPosition(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class EquipmentAvailability(StrEnum):
    """Service availability of equipment in one operating state.

    Absence from :attr:`OperatingState.availability` intentionally means the
    default ``IN_SERVICE``.  The sparse mapping therefore records only an
    explicit operating-mode decision and does not duplicate catalog/passport
    data on every state.
    """

    IN_SERVICE = "IN_SERVICE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


EQUIPMENT_AVAILABILITY_CAPABILITY = "equipment.availability"
PLACEMENT_INLINE_SERIES_CAPABILITY = "placement.inline_series"
PLACEMENT_BRANCH_ATTACHMENT_CAPABILITY = "placement.branch_attachment"
PLACEMENT_NODE_REPRESENTATION_CAPABILITY = "placement.node_representation"
PLACEMENT_MANUAL_SELECTION_CAPABILITY = "placement.manual_selection"


class LineKind(StrEnum):
    OVERHEAD = "overhead"
    CABLE = "cable"
    BUSDUCT = "busduct"


class DataConfirmation(StrEnum):
    """Инженерная подтверждённость физического исходного параметра."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class EquipmentInstance:
    id: EquipmentId
    type_id: EquipmentTypeId
    type_version: int
    name: str
    port_ids: tuple[PortId, ...]
    properties: Mapping[str, Any] = field(default_factory=dict)
    voltage_class_by_group: Mapping[str, VoltageClassId] = field(default_factory=dict)
    normal_position: SwitchPosition | None = None
    note: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.id, EquipmentId, "EquipmentInstance.id")
        _require_id(self.type_id, EquipmentTypeId, "EquipmentInstance.type_id")
        for port_id in self.port_ids:
            _require_id(port_id, PortId, "EquipmentInstance.port_ids[]")
        _require_string(self.name, "EquipmentInstance.name", allow_empty=False)
        _require_positive_int(self.type_version, "EquipmentInstance.type_version")
        _require_string(self.note, "EquipmentInstance.note")
        _require_mapping(self.properties, "EquipmentInstance.properties")
        _require_mapping(
            self.voltage_class_by_group,
            "EquipmentInstance.voltage_class_by_group",
        )
        _require_mapping(self.extensions, "EquipmentInstance.extensions")
        if len(self.port_ids) != len(set(self.port_ids)):
            raise DomainInvariantError(f"Оборудование '{self.id}': повторяется ID порта.")
        object.__setattr__(self, "port_ids", tuple(self.port_ids))
        if self.normal_position is not None and not isinstance(
            self.normal_position, SwitchPosition
        ):
            try:
                object.__setattr__(
                    self, "normal_position", SwitchPosition(self.normal_position)
                )
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Нормальное положение должно быть OPEN или CLOSED."
                ) from exc
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        voltage_groups: dict[str, VoltageClassId] = {}
        for group, voltage_id in self.voltage_class_by_group.items():
            if not isinstance(group, str) or not group.strip():
                raise DomainInvariantError("Группа напряжения должна быть непустой строкой.")
            if not isinstance(voltage_id, VoltageClassId):
                raise DomainInvariantError(
                    "Класс напряжения оборудования адресуется VoltageClassId."
                )
            voltage_groups[group] = voltage_id
        object.__setattr__(
            self, "voltage_class_by_group", MappingProxyType(voltage_groups)
        )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class PortInstance:
    id: PortId
    equipment_id: EquipmentId
    role: str

    def __post_init__(self) -> None:
        _require_id(self.id, PortId, "PortInstance.id")
        _require_id(self.equipment_id, EquipmentId, "PortInstance.equipment_id")
        _require_string(self.role, "PortInstance.role", allow_empty=False)


@dataclass(frozen=True, slots=True)
class ElectricalNode:
    id: ElectricalNodeId
    name: str = ""
    kind_id: PortKindId = field(
        default_factory=lambda: PortKindId("builtin.port.ac_power")
    )
    declared_voltage_class_id: VoltageClassId | None = None
    note: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.id, ElectricalNodeId, "ElectricalNode.id")
        _require_id(self.kind_id, PortKindId, "ElectricalNode.kind_id")
        _require_string(self.name, "ElectricalNode.name")
        _require_string(self.note, "ElectricalNode.note")
        _require_mapping(self.extensions, "ElectricalNode.extensions")
        if self.declared_voltage_class_id is not None:
            _require_id(
                self.declared_voltage_class_id,
                VoltageClassId,
                "ElectricalNode.declared_voltage_class_id",
            )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class Connection:
    """Единственная нормализованная связь: terminal port → equipotential node."""

    id: ConnectionId
    port_id: PortId
    electrical_node_id: ElectricalNodeId
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.id, ConnectionId, "Connection.id")
        _require_id(self.port_id, PortId, "Connection.port_id")
        _require_mapping(self.extensions, "Connection.extensions")
        _require_id(
            self.electrical_node_id,
            ElectricalNodeId,
            "Connection.electrical_node_id",
        )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class OperatingState:
    id: OperatingStateId
    name: str
    positions: Mapping[EquipmentId, SwitchPosition] = field(default_factory=dict)
    system: str = "max"
    description: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)
    availability: Mapping[EquipmentId, EquipmentAvailability] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        _require_id(self.id, OperatingStateId, "OperatingState.id")
        _require_string(self.name, "OperatingState.name", allow_empty=False)
        _require_string(self.system, "OperatingState.system", allow_empty=False)
        if self.system not in {"max", "min"}:
            raise DomainInvariantError(
                "OperatingState.system должен быть 'max' или 'min'."
            )
        _require_string(self.description, "OperatingState.description")
        _require_mapping(self.positions, "OperatingState.positions")
        _require_mapping(self.extensions, "OperatingState.extensions")
        _require_mapping(self.availability, "OperatingState.availability")
        normalized_positions: dict[EquipmentId, SwitchPosition] = {}
        for equipment_id, position in self.positions.items():
            if not isinstance(equipment_id, EquipmentId):
                raise DomainInvariantError("Положение аппарата адресуется EquipmentId.")
            if not isinstance(position, SwitchPosition):
                try:
                    position = SwitchPosition(position)
                except (TypeError, ValueError) as exc:
                    raise DomainInvariantError(
                        "Положение коммутационного аппарата должно быть OPEN или CLOSED."
                    ) from exc
            normalized_positions[equipment_id] = position
        normalized_availability: dict[EquipmentId, EquipmentAvailability] = {}
        for equipment_id, availability in self.availability.items():
            if not isinstance(equipment_id, EquipmentId):
                raise DomainInvariantError(
                    "Эксплуатационная доступность адресуется EquipmentId."
                )
            if not isinstance(availability, EquipmentAvailability):
                try:
                    availability = EquipmentAvailability(availability)
                except (TypeError, ValueError) as exc:
                    raise DomainInvariantError(
                        "Эксплуатационная доступность должна быть IN_SERVICE "
                        "или OUT_OF_SERVICE."
                    ) from exc
            normalized_availability[equipment_id] = availability
        object.__setattr__(
            self, "positions", MappingProxyType(normalized_positions)
        )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))
        object.__setattr__(
            self,
            "availability",
            MappingProxyType(normalized_availability),
        )


@dataclass(frozen=True, slots=True)
class LogicalLine:
    """One line as seen by a user; it is not itself electrically conductive.

    Conductivity belongs exclusively to the ordered two-terminal equipment in
    ``section_equipment_ids``.  A tap is therefore an ordinary shared
    :class:`ElectricalNode`, never a special parent/child relation.
    """

    id: LogicalLineId
    name: str
    line_kind: LineKind
    voltage_class_id: VoltageClassId | None
    section_equipment_ids: tuple[EquipmentId, ...]
    inherited_properties: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)
    feeder_id: FeederId | None = None

    def __post_init__(self) -> None:
        _require_id(self.id, LogicalLineId, "LogicalLine.id")
        _require_string(self.name, "LogicalLine.name", allow_empty=False)
        if not isinstance(self.line_kind, LineKind):
            try:
                object.__setattr__(self, "line_kind", LineKind(self.line_kind))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Вид линии должен быть overhead, cable или busduct."
                ) from exc
        if self.voltage_class_id is not None:
            _require_id(
                self.voltage_class_id,
                VoltageClassId,
                "LogicalLine.voltage_class_id",
            )
        if self.feeder_id is not None:
            _require_id(self.feeder_id, FeederId, "LogicalLine.feeder_id")
        if not self.section_equipment_ids:
            raise DomainInvariantError("Логическая линия должна содержать хотя бы участок.")
        for equipment_id in self.section_equipment_ids:
            _require_id(
                equipment_id,
                EquipmentId,
                "LogicalLine.section_equipment_ids[]",
            )
        if len(self.section_equipment_ids) != len(set(self.section_equipment_ids)):
            raise DomainInvariantError("Участок не может повторяться в логической линии.")
        _require_mapping(
            self.inherited_properties, "LogicalLine.inherited_properties"
        )
        _require_string(self.note, "LogicalLine.note")
        _require_mapping(self.extensions, "LogicalLine.extensions")
        object.__setattr__(
            self, "section_equipment_ids", tuple(self.section_equipment_ids)
        )
        object.__setattr__(
            self, "inherited_properties", _freeze_json(self.inherited_properties)
        )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))


@dataclass(frozen=True, slots=True)
class LineConstructionSegment:
    """Конструктивный участок без собственного электрического узла.

    Несколько таких записей образуют одну двухполюсную электрическую ветвь.
    ``line_kind=None`` допускается только как кратковременный compatibility-
    маркер при чтении старого ``LineSection(length_mm=...)``; при регистрации
    логической линии модель заменяет его на фактический вид линии.
    """

    id: LineConstructionSegmentId
    line_kind: LineKind | None
    length_mm: int | None
    properties: Mapping[str, Any] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    length_confirmation: DataConfirmation | None = None
    impedance_confirmation: DataConfirmation | None = None

    def __post_init__(self) -> None:
        _require_id(
            self.id,
            LineConstructionSegmentId,
            "LineConstructionSegment.id",
        )
        if self.line_kind is not None and not isinstance(self.line_kind, LineKind):
            try:
                object.__setattr__(self, "line_kind", LineKind(self.line_kind))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Вид конструктивного участка должен быть overhead, cable "
                    "или busduct."
                ) from exc
        if self.length_mm is not None:
            _require_positive_int(
                self.length_mm, "LineConstructionSegment.length_mm"
            )
        _require_mapping(self.properties, "LineConstructionSegment.properties")
        _require_mapping(self.extensions, "LineConstructionSegment.extensions")
        if {"length_mm", "length_km"} & set(self.properties):
            raise DomainInvariantError(
                "Физическая длина хранится только в "
                "LineConstructionSegment.length_mm."
            )
        length_confirmation = self.length_confirmation
        if length_confirmation is None:
            length_confirmation = (
                DataConfirmation.CONFIRMED
                if self.length_mm is not None
                else DataConfirmation.UNCONFIRMED
            )
        elif not isinstance(length_confirmation, DataConfirmation):
            try:
                length_confirmation = DataConfirmation(length_confirmation)
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Подтверждённость длины должна быть confirmed или unconfirmed."
                ) from exc
        if (
            self.length_mm is None
            and length_confirmation is DataConfirmation.CONFIRMED
        ):
            raise DomainInvariantError(
                "Неизвестную физическую длину нельзя пометить подтверждённой."
            )

        impedance_confirmation = self.impedance_confirmation
        if impedance_confirmation is None:
            impedance_confirmation = (
                DataConfirmation.CONFIRMED
                if self.properties.get("r1_ohm_per_km") is not None
                and self.properties.get("x1_ohm_per_km") is not None
                else DataConfirmation.UNCONFIRMED
            )
        elif not isinstance(impedance_confirmation, DataConfirmation):
            try:
                impedance_confirmation = DataConfirmation(impedance_confirmation)
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Подтверждённость сопротивлений должна быть confirmed "
                    "или unconfirmed."
                ) from exc
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))
        object.__setattr__(self, "length_confirmation", length_confirmation)
        object.__setattr__(
            self, "impedance_confirmation", impedance_confirmation
        )


@dataclass(frozen=True, slots=True, init=False)
class LineSection:
    """Двухполюсная электрическая ветвь логической линии.

    Имя сохранено для обратной совместимости формата v4/v5. Физическая длина
    ветви является исключительно суммой упорядоченных конструктивных участков.
    Старый вызов ``LineSection(equipment_id, line_id, length_mm, extensions)``
    создаёт один служебный участок; его вид уточняется в ``add_logical_line``.
    """

    equipment_id: EquipmentId
    logical_line_id: LogicalLineId
    construction_segments: tuple[LineConstructionSegment, ...]
    extensions: Mapping[str, Any]

    def __init__(
        self,
        equipment_id: EquipmentId,
        logical_line_id: LogicalLineId,
        length_mm: int | None = None,
        extensions: Mapping[str, Any] | None = None,
        *,
        construction_segments: Iterable[LineConstructionSegment] | None = None,
    ) -> None:
        _require_id(equipment_id, EquipmentId, "LineSection.equipment_id")
        _require_id(
            logical_line_id, LogicalLineId, "LineSection.logical_line_id"
        )
        normalized_extensions = {} if extensions is None else extensions
        _require_mapping(normalized_extensions, "LineSection.extensions")
        if construction_segments is None:
            if length_mm is not None:
                _require_positive_int(length_mm, "LineSection.length_mm")
            segment_id = deterministic_id(
                LineConstructionSegmentId,
                "legacy-line-section",
                equipment_id.value,
            )
            segments = (
                LineConstructionSegment(segment_id, None, length_mm),
            )
        else:
            segments = tuple(construction_segments)
            if not segments:
                raise DomainInvariantError(
                    "Электрическая ветвь должна содержать хотя бы один "
                    "конструктивный участок."
                )
            if any(
                not isinstance(item, LineConstructionSegment) for item in segments
            ):
                raise DomainInvariantError(
                    "LineSection.construction_segments содержит неверный тип."
                )
            total_length = (
                sum(item.length_mm for item in segments if item.length_mm is not None)
                if all(item.length_mm is not None for item in segments)
                else None
            )
            if length_mm is not None:
                _require_positive_int(length_mm, "LineSection.length_mm")
                if length_mm != total_length:
                    raise DomainInvariantError(
                        "Длина электрической ветви не совпадает с суммой "
                        "конструктивных участков."
                    )
        segment_ids = [item.id for item in segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise DomainInvariantError(
                "ID конструктивного участка повторяется внутри ветви."
            )
        object.__setattr__(self, "equipment_id", equipment_id)
        object.__setattr__(self, "logical_line_id", logical_line_id)
        object.__setattr__(self, "construction_segments", segments)
        object.__setattr__(
            self, "extensions", _freeze_json(normalized_extensions)
        )

    @property
    def length_mm(self) -> int | None:
        if any(item.length_mm is None for item in self.construction_segments):
            return None
        return sum(
            item.length_mm for item in self.construction_segments
            if item.length_mm is not None
        )


@dataclass(frozen=True, slots=True)
class SplitLineSectionResult:
    logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    tap_node_id: ElectricalNodeId
    change: ChangeSet


@dataclass(frozen=True, slots=True)
class InsertSeriesEquipmentResult:
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
    change: ChangeSet


@dataclass(frozen=True, slots=True)
class RemoveSeriesEquipmentResult:
    feeder_id: FeederId
    logical_line_id: LogicalLineId
    removed_right_logical_line_id: LogicalLineId
    removed_equipment_id: EquipmentId
    removed_left_section_id: EquipmentId
    removed_right_section_id: EquipmentId
    removed_left_node_id: ElectricalNodeId
    removed_right_node_id: ElectricalNodeId
    merged_section_id: EquipmentId
    terminal_roles: tuple[str, str]
    change: ChangeSet


@dataclass(frozen=True, slots=True)
class InsertRecloserResult:
    feeder_id: FeederId
    left_logical_line_id: LogicalLineId
    right_logical_line_id: LogicalLineId
    removed_section_id: EquipmentId
    left_section_id: EquipmentId
    right_section_id: EquipmentId
    left_node_id: ElectricalNodeId
    right_node_id: ElectricalNodeId
    recloser_id: EquipmentId
    change: ChangeSet


@dataclass(frozen=True, slots=True)
class RemoveRecloserResult:
    feeder_id: FeederId
    logical_line_id: LogicalLineId
    removed_right_logical_line_id: LogicalLineId
    removed_recloser_id: EquipmentId
    removed_left_section_id: EquipmentId
    removed_right_section_id: EquipmentId
    removed_left_node_id: ElectricalNodeId
    removed_right_node_id: ElectricalNodeId
    merged_section_id: EquipmentId
    change: ChangeSet


@dataclass(frozen=True, slots=True)
class DomainIssue:
    code: str
    message: str
    object_id: str = ""
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class ChangeSet:
    revision: int
    added_ids: tuple[str, ...] = ()
    changed_ids: tuple[str, ...] = ()
    removed_ids: tuple[str, ...] = ()


AC_POWER = PortKindId("builtin.port.ac_power")

STANDARD_VOLTAGES: tuple[tuple[str, int, str], ...] = (
    ("builtin.voltage.ac.220kv", 220_000, "220 кВ"),
    ("builtin.voltage.ac.110kv", 110_000, "110 кВ"),
    ("builtin.voltage.ac.35kv", 35_000, "35 кВ"),
    ("builtin.voltage.ac.10kv", 10_000, "10 кВ"),
    ("builtin.voltage.ac.6kv", 6_000, "6 кВ"),
    ("builtin.voltage.ac.0_4kv", 400, "0,4 кВ"),
)


def _port(role: str, name: str, group: str | None = "main") -> PortDefinition:
    return PortDefinition(role, name, AC_POWER, voltage_group=group)


def builtin_equipment_types() -> tuple[EquipmentTypeDefinition, ...]:
    """Начальный расширяемый реестр; это не закрытый enum типов."""
    rows = (
        ("builtin.external_grid", "Внешняя энергосистема", "source", (_port("terminal", "Вывод"),), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY, PLACEMENT_BRANCH_ATTACHMENT_CAPABILITY})),
        ("builtin.generator", "Генератор", "generator", (_port("terminal", "Вывод"),), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY, PLACEMENT_BRANCH_ATTACHMENT_CAPABILITY})),
        ("builtin.busbar", "Шина", "bus", (_port("terminal", "Электрический узел"),), frozenset({PLACEMENT_NODE_REPRESENTATION_CAPABILITY})),
        ("builtin.connection_point", "Точка подключения", "bus", (_port("terminal", "Электрический узел"),), frozenset({PLACEMENT_NODE_REPRESENTATION_CAPABILITY})),
        ("builtin.line", "Воздушная линия", "line", (_port("from", "Начало"), _port("to", "Конец")), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY})),
        ("builtin.cable", "Кабельная линия", "line", (_port("from", "Начало"), _port("to", "Конец")), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY})),
        ("builtin.circuit_breaker", "Выключатель", "switch", (_port("a", "A"), _port("b", "B")), frozenset({"switch.position", EQUIPMENT_AVAILABILITY_CAPABILITY, PLACEMENT_INLINE_SERIES_CAPABILITY})),
        ("builtin.transformer_2w", "Двухобмоточный трансформатор", "transformer_2w", (_port("hv", "ВН", "hv"), _port("lv", "НН", "lv")), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY, PLACEMENT_BRANCH_ATTACHMENT_CAPABILITY})),
        ("builtin.transformer_3w", "Трёхобмоточный трансформатор", "transformer_3w", (_port("hv", "ВН", "hv"), _port("mv", "СН", "mv"), _port("lv", "НН", "lv")), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY, PLACEMENT_BRANCH_ATTACHMENT_CAPABILITY})),
        ("builtin.load", "Нагрузка", "load", (_port("terminal", "Вывод"),), frozenset({EQUIPMENT_AVAILABILITY_CAPABILITY, PLACEMENT_BRANCH_ATTACHMENT_CAPABILITY})),
    )
    ordinary = tuple(
        EquipmentTypeDefinition(
            EquipmentTypeId(type_id), 1, display_name, behavior, ports,
            capabilities=capabilities,
        )
        for type_id, display_name, behavior, ports, capabilities in rows
    )
    section_properties = (
        PropertyDefinition("conductor_mark", "string"),
        PropertyDefinition("cross_section_mm2", "number", unit="mm2"),
        PropertyDefinition("material", "string"),
        PropertyDefinition("parallel_count", "integer", default=1),
        PropertyDefinition("r1_ohm_per_km", "number", unit="ohm/km"),
        PropertyDefinition("x1_ohm_per_km", "number", unit="ohm/km"),
        PropertyDefinition("r2_ohm_per_km", "number", unit="ohm/km"),
        PropertyDefinition("x2_ohm_per_km", "number", unit="ohm/km"),
        PropertyDefinition("r0_ohm_per_km", "number", unit="ohm/km"),
        PropertyDefinition("x0_ohm_per_km", "number", unit="ohm/km"),
        PropertyDefinition("negative_sequence_equal_positive", "boolean", default=False),
        PropertyDefinition("zero_sequence_connection", "string"),
        PropertyDefinition(
            "capacitive_current_a_per_km", "number", unit="A/km", default=0.0
        ),
        PropertyDefinition("custom_parameters", "object", default={}),
    )
    line_section_types = tuple(
        EquipmentTypeDefinition(
            EquipmentTypeId(type_id),
            1,
            display_name,
            "line_section",
            (_port("from", "Начало"), _port("to", "Конец")),
            property_definitions=section_properties,
            capabilities=frozenset(
                {"line.physical_section", EQUIPMENT_AVAILABILITY_CAPABILITY}
            ),
            allow_additional_properties=False,
            extensions={
                "line_kind": line_kind.value,
                "diagram_symbol_key": "line_section",
            },
        )
        for type_id, display_name, line_kind in (
            (
                "builtin.line_section.overhead",
                "Участок воздушной линии",
                LineKind.OVERHEAD,
            ),
            (
                "builtin.line_section.cable",
                "Участок кабельной линии",
                LineKind.CABLE,
            ),
            (
                "builtin.line_section.busduct",
                "Участок шинопровода",
                LineKind.BUSDUCT,
            ),
        )
    )
    recloser_properties = (
        PropertyDefinition("manufacturer", "string", default=""),
        PropertyDefinition("model", "string", default=""),
        PropertyDefinition("rated_voltage_v", "integer", unit="V"),
        PropertyDefinition("rated_current_a", "number", required=True, unit="A"),
        PropertyDefinition("rated_breaking_current_a", "number", unit="A"),
        PropertyDefinition("short_circuit_limit_a", "number", unit="A"),
        PropertyDefinition("thermal_short_time_current_a", "number", unit="A"),
        PropertyDefinition("thermal_duration_s", "number", unit="s"),
        PropertyDefinition("dynamic_peak_current_a", "number", unit="A"),
        PropertyDefinition("full_opening_time_s", "number", unit="s", default=0.0),
        PropertyDefinition("protection_settings", "object", default={}),
        PropertyDefinition("auto_reclose_settings", "object", default={}),
        PropertyDefinition("directional_settings", "object", default={}),
        PropertyDefinition("control_settings", "object", default={}),
        PropertyDefinition("scada_settings", "object", default={}),
        PropertyDefinition("custom_parameters", "object", default={}),
    )
    recloser = EquipmentTypeDefinition(
        EquipmentTypeId("builtin.recloser"),
        1,
        "Реклоузер",
        "recloser",
        (_port("a", "A"), _port("b", "B")),
        property_definitions=recloser_properties,
        capabilities=frozenset(
            {
                "switch.position",
                EQUIPMENT_AVAILABILITY_CAPABILITY,
                PLACEMENT_INLINE_SERIES_CAPABILITY,
                "switch.interrupting",
                "protection.auto_reclose",
            }
        ),
        allow_additional_properties=False,
        extensions={
            "catalog_category_id": "reclosers",
            "diagram_symbol_key": "recloser",
        },
    )
    disconnector = EquipmentTypeDefinition(
        EquipmentTypeId("builtin.disconnector"),
        1,
        "Разъединитель",
        "switch",
        (_port("a", "A"), _port("b", "B")),
        capabilities=frozenset(
            {
                "switch.position",
                EQUIPMENT_AVAILABILITY_CAPABILITY,
                PLACEMENT_INLINE_SERIES_CAPABILITY,
            }
        ),
        extensions={
            "diagram_symbol_key": "disconnector",
            "default_normal_position": SwitchPosition.CLOSED.value,
        },
    )
    return ordinary + line_section_types + (recloser, disconnector)


def _legacy_mode_keys_for_equipment(
    equipment: EquipmentInstance,
) -> frozenset[str]:
    """Mode keys owned by one imported compatibility equipment record.

    The legacy adapter keeps the complete sparse ``Mode.states`` dictionary in
    ``OperatingState.extensions``.  Direct branch state, endpoint switches and
    generated Transformer3W legs all belong to the owning domain equipment and
    must therefore be removed with it.  Exact keys avoid prefix-based deletion
    of an unrelated legacy branch.
    """
    marker = equipment.extensions.get("legacy_calculation")
    if not isinstance(marker, Mapping):
        return frozenset()
    legacy_id = marker.get("legacy_id")
    if not isinstance(legacy_id, str) or not legacy_id:
        return frozenset()
    category = marker.get("category")
    if category == "branch":
        branch_ids = (legacy_id,)
    elif category == "transformer3w":
        branch_ids = (legacy_id, f"{legacy_id}_mv", f"{legacy_id}_lv")
    else:
        return frozenset()
    return frozenset(
        (*branch_ids, *(
            f"SW:{branch_id}:{end}"
            for branch_id in branch_ids
            for end in ("from", "to")
        ))
    )


def _without_legacy_mode_references(
    state: OperatingState,
    equipment: EquipmentInstance,
) -> Mapping[str, Any] | None:
    """Return changed extensions, or ``None`` when this state has no refs."""
    owned_keys = _legacy_mode_keys_for_equipment(equipment)
    if not owned_keys:
        return None
    marker = state.extensions.get("legacy_calculation")
    if not isinstance(marker, Mapping):
        return None
    raw_states = marker.get("states")
    if not isinstance(raw_states, Mapping):
        return None
    removed_keys = owned_keys.intersection(raw_states)
    if not removed_keys:
        return None

    extensions = thaw_json(state.extensions)
    mutable_states = extensions["legacy_calculation"]["states"]
    for key in removed_keys:
        del mutable_states[key]
    return extensions


class ElectricalModel:
    """Агрегат канонической электрической модели.

    Публичные коллекции доступны только для чтения. Любое изменение проходит
    через проверяемую команду и увеличивает ``revision`` ровно один раз.
    """

    def __init__(self, name: str = "Проект", *, neutral: Mapping[str, str] | None = None,
                 extensions: Mapping[str, Any] | None = None):
        _require_string(name, "ElectricalModel.name", allow_empty=False)
        if neutral is not None:
            _require_mapping(neutral, "ElectricalModel.neutral")
            if any(not isinstance(key, str) or not key or not isinstance(value, str)
                   for key, value in neutral.items()):
                raise DomainInvariantError(
                    "ElectricalModel.neutral должен отображать непустые строки в строки."
                )
        if extensions is not None:
            _require_mapping(extensions, "ElectricalModel.extensions")
        self._name = name
        self._revision = 0
        self._voltage_classes: dict[VoltageClassId, VoltageClass] = {}
        self._equipment_types: dict[tuple[EquipmentTypeId, int], EquipmentTypeDefinition] = {}
        self._equipment: dict[EquipmentId, EquipmentInstance] = {}
        self._ports: dict[PortId, PortInstance] = {}
        self._electrical_nodes: dict[ElectricalNodeId, ElectricalNode] = {}
        self._connections: dict[ConnectionId, Connection] = {}
        self._operating_states: dict[OperatingStateId, OperatingState] = {}
        self._logical_lines: dict[LogicalLineId, LogicalLine] = {}
        self._line_sections: dict[EquipmentId, LineSection] = {}
        self._neutral = _freeze_json(neutral or {})
        self._extensions = _freeze_json(extensions or {})

    @classmethod
    def with_builtins(cls, name: str = "Проект", *, neutral: Mapping[str, str] | None = None,
                      extensions: Mapping[str, Any] | None = None) -> "ElectricalModel":
        model = cls(name, neutral=neutral, extensions=extensions)
        for voltage_id, value, display in STANDARD_VOLTAGES:
            model._voltage_classes[VoltageClassId(voltage_id)] = VoltageClass(
                VoltageClassId(voltage_id), value, display
            )
        for definition in builtin_equipment_types():
            model._equipment_types[(definition.id, definition.schema_version)] = definition
        return model

    @property
    def name(self) -> str:
        return self._name

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def neutral(self) -> Mapping[str, str]:
        return self._neutral

    @property
    def extensions(self) -> Mapping[str, Any]:
        return self._extensions

    def _restore_revision(self, revision: int) -> None:
        """Loader-only restoration; ordinary domain mutations use _changed()."""
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise DomainInvariantError("ElectricalModel.revision должен быть целым ≥ 0.")
        self._revision = revision

    @property
    def voltage_classes(self) -> Mapping[VoltageClassId, VoltageClass]:
        return MappingProxyType(self._voltage_classes)

    @property
    def equipment_types(self) -> Mapping[tuple[EquipmentTypeId, int], EquipmentTypeDefinition]:
        return MappingProxyType(self._equipment_types)

    @property
    def equipment(self) -> Mapping[EquipmentId, EquipmentInstance]:
        return MappingProxyType(self._equipment)

    @property
    def ports(self) -> Mapping[PortId, PortInstance]:
        return MappingProxyType(self._ports)

    @property
    def electrical_nodes(self) -> Mapping[ElectricalNodeId, ElectricalNode]:
        return MappingProxyType(self._electrical_nodes)

    @property
    def connections(self) -> Mapping[ConnectionId, Connection]:
        return MappingProxyType(self._connections)

    @property
    def operating_states(self) -> Mapping[OperatingStateId, OperatingState]:
        return MappingProxyType(self._operating_states)

    @property
    def logical_lines(self) -> Mapping[LogicalLineId, LogicalLine]:
        return MappingProxyType(self._logical_lines)

    @property
    def line_sections(self) -> Mapping[EquipmentId, LineSection]:
        return MappingProxyType(self._line_sections)

    def _object_id_values(self) -> set[str]:
        values = {item.value for item in self._voltage_classes}
        values.update(item[0].value for item in self._equipment_types)
        for store in (
            self._equipment, self._ports, self._electrical_nodes,
            self._connections, self._operating_states, self._logical_lines,
        ):
            values.update(item.value for item in store)
        values.update(
            segment.id.value
            for section in self._line_sections.values()
            for segment in section.construction_segments
        )
        values.update(
            line.feeder_id.value
            for line in self._logical_lines.values()
            if line.feeder_id is not None
        )
        return values

    def _ensure_ids_available(self, ids: Iterable[StableId]) -> None:
        known = self._object_id_values()
        pending: set[str] = set()
        for object_id in ids:
            if object_id.value == "GRID":
                raise DomainInvariantError("ID 'GRID' зарезервирован расчётным адаптером.")
            if object_id.value in known or object_id.value in pending:
                raise DomainInvariantError(
                    f"ID '{object_id.value}' уже используется в электрической модели."
                )
            pending.add(object_id.value)

    def _changed(self, *, added: Iterable[StableId] = (),
                 changed: Iterable[StableId] = (),
                 removed: Iterable[StableId] = ()) -> ChangeSet:
        self._revision += 1
        return ChangeSet(
            self.revision,
            tuple(item.value for item in added),
            tuple(item.value for item in changed),
            tuple(item.value for item in removed),
        )

    def _transaction_copy(self) -> "ElectricalModel":
        """Shallow copy immutable records for an all-or-nothing compound command."""
        staged = ElectricalModel(
            self._name,
            neutral=thaw_json(self._neutral),
            extensions=thaw_json(self._extensions),
        )
        staged._revision = self._revision
        staged._voltage_classes = dict(self._voltage_classes)
        staged._equipment_types = dict(self._equipment_types)
        staged._equipment = dict(self._equipment)
        staged._ports = dict(self._ports)
        staged._electrical_nodes = dict(self._electrical_nodes)
        staged._connections = dict(self._connections)
        staged._operating_states = dict(self._operating_states)
        staged._logical_lines = dict(self._logical_lines)
        staged._line_sections = dict(self._line_sections)
        return staged

    def _commit_transaction(
        self,
        staged: "ElectricalModel",
        *,
        added: Iterable[StableId] = (),
        changed: Iterable[StableId] = (),
        removed: Iterable[StableId] = (),
    ) -> ChangeSet:
        self._name = staged._name
        self._neutral = staged._neutral
        self._extensions = staged._extensions
        self._voltage_classes = staged._voltage_classes
        self._equipment_types = staged._equipment_types
        self._equipment = staged._equipment
        self._ports = staged._ports
        self._electrical_nodes = staged._electrical_nodes
        self._connections = staged._connections
        self._operating_states = staged._operating_states
        self._logical_lines = staged._logical_lines
        self._line_sections = staged._line_sections
        return self._changed(added=added, changed=changed, removed=removed)

    def register_voltage_class(self, voltage: VoltageClass) -> ChangeSet:
        existing = self._voltage_classes.get(voltage.id)
        if existing is not None:
            if existing == voltage:
                return ChangeSet(self.revision)
            raise DomainInvariantError(f"Класс напряжения '{voltage.id}' уже зарегистрирован.")
        self._ensure_ids_available((voltage.id,))
        if any(item.nominal_voltage_v == voltage.nominal_voltage_v
               for item in self._voltage_classes.values()):
            raise DomainInvariantError(
                f"Класс {voltage.nominal_voltage_v} В уже зарегистрирован под другим ID."
            )
        self._voltage_classes[voltage.id] = voltage
        return self._changed(added=(voltage.id,))

    def ensure_voltage_class(self, nominal_voltage_v: int) -> VoltageClass:
        for item in self._voltage_classes.values():
            if item.nominal_voltage_v == nominal_voltage_v:
                return item
        voltage_id = VoltageClassId.new()
        value_kv = nominal_voltage_v / 1000.0
        display = f"{value_kv:g} кВ".replace(".", ",")
        voltage = VoltageClass(voltage_id, nominal_voltage_v, display)
        self.register_voltage_class(voltage)
        return voltage

    def register_equipment_type(self, definition: EquipmentTypeDefinition) -> ChangeSet:
        key = (definition.id, definition.schema_version)
        existing = self._equipment_types.get(key)
        if existing is not None:
            if existing == definition:
                return ChangeSet(self.revision)
            raise DomainInvariantError(
                f"Тип '{definition.id}' версии {definition.schema_version} уже зарегистрирован."
            )
        if definition.id.value not in {item[0].value for item in self._equipment_types}:
            self._ensure_ids_available((definition.id,))
        unknown_voltage_ids = {
            voltage_id
            for port in definition.port_definitions
            for voltage_id in (port.allowed_voltage_class_ids or ())
            if voltage_id not in self._voltage_classes
        }
        if unknown_voltage_ids:
            raise DomainInvariantError(
                f"Тип '{definition.id}' ссылается на неизвестные классы напряжения: "
                + ", ".join(sorted(item.value for item in unknown_voltage_ids))
            )
        self._equipment_types[key] = definition
        return self._changed(added=(definition.id,))

    def equipment_type(self, type_id: EquipmentTypeId, version: int = 1
                       ) -> EquipmentTypeDefinition:
        try:
            return self._equipment_types[(type_id, version)]
        except KeyError as exc:
            raise DomainInvariantError(
                f"Тип оборудования '{type_id}' версии {version} не зарегистрирован."
            ) from exc

    def add_node(self, node: ElectricalNode) -> ChangeSet:
        self._ensure_ids_available((node.id,))
        if node.declared_voltage_class_id is not None:
            if node.declared_voltage_class_id not in self._voltage_classes:
                raise DomainInvariantError(
                    f"Узел '{node.id}' ссылается на неизвестный класс напряжения."
                )
        self._electrical_nodes[node.id] = node
        return self._changed(added=(node.id,))

    def _validate_properties(self, definition: EquipmentTypeDefinition,
                             properties: Mapping[str, Any]) -> None:
        declared = {item.key: item for item in definition.property_definitions}
        missing = [key for key, item in declared.items()
                   if item.required and key not in properties and item.default is None]
        if missing:
            raise DomainInvariantError(
                f"Тип '{definition.id}': не заданы обязательные свойства {missing}."
            )
        if not definition.allow_additional_properties:
            unknown = set(properties) - set(declared)
            if unknown:
                raise DomainInvariantError(
                    f"Тип '{definition.id}': неизвестные свойства {sorted(unknown)}."
                )
        for key, value in properties.items():
            property_definition = declared.get(key)
            if property_definition is None or value is None:
                continue
            kind = property_definition.value_kind
            valid = _matches_property_value_kind(value, kind)
            if not valid:
                raise DomainInvariantError(
                    f"Тип '{definition.id}': свойство '{key}' не соответствует "
                    f"value_kind='{kind}'."
                )

    def effective_equipment_properties(
        self, equipment_id: EquipmentId
    ) -> Mapping[str, Any]:
        """Resolve type defaults, logical-line inheritance and instance overrides."""
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        definition = self.equipment_type(equipment.type_id, equipment.type_version)
        effective: dict[str, Any] = {
            item.key: thaw_json(item.default)
            for item in definition.property_definitions
            if item.default is not None
        }
        section = self._line_sections.get(equipment_id)
        if section is not None:
            line = self._logical_lines.get(section.logical_line_id)
            if line is None:
                raise DomainInvariantError(
                    f"Участок '{equipment_id}' ссылается на неизвестную линию."
                )
            effective.update(thaw_json(line.inherited_properties))
        effective.update(thaw_json(equipment.properties))
        self._validate_properties(definition, effective)
        return MappingProxyType({key: _freeze_json(value) for key, value in effective.items()})

    def _validate_equipment_semantics(
        self,
        definition: EquipmentTypeDefinition,
        equipment: EquipmentInstance,
    ) -> None:
        is_switch = bool(
            {"switch.position", "legacy.switch.position"} & definition.capabilities
        )
        if equipment.normal_position is not None and not is_switch:
            raise DomainInvariantError(
                f"Оборудование '{equipment.id}' не является коммутационным аппаратом."
            )
        if definition.behavior_key != "recloser":
            return
        if equipment.normal_position is None:
            raise DomainInvariantError(
                f"Реклоузер '{equipment.id}' должен иметь нормальное положение OPEN/CLOSED."
            )
        voltage_id = equipment.voltage_class_by_group.get("main")
        if voltage_id is None:
            raise DomainInvariantError(
                f"Реклоузер '{equipment.id}' должен иметь класс напряжения."
            )
        rated_voltage = equipment.properties.get("rated_voltage_v")
        voltage = self._voltage_classes.get(voltage_id)
        if rated_voltage is not None and (
            voltage is None or rated_voltage != voltage.nominal_voltage_v
        ):
            raise DomainInvariantError(
                f"Реклоузер '{equipment.id}': номинальное напряжение не "
                "совпадает с классом напряжения."
            )
        for key in (
            "rated_current_a",
            "rated_breaking_current_a",
            "short_circuit_limit_a",
            "thermal_short_time_current_a",
            "thermal_duration_s",
            "dynamic_peak_current_a",
        ):
            value = equipment.properties.get(key)
            if value is not None and value <= 0:
                raise DomainInvariantError(
                    f"Реклоузер '{equipment.id}': свойство '{key}' должно быть > 0."
                )

    def _validate_equipment_configuration(
        self,
        definition: EquipmentTypeDefinition,
        equipment: EquipmentInstance,
    ) -> None:
        """Validate reusable type/property/voltage semantics before mutation."""
        effective_properties: dict[str, Any] = {
            item.key: thaw_json(item.default)
            for item in definition.property_definitions
            if item.default is not None
        }
        effective_properties.update(thaw_json(equipment.properties))
        self._validate_properties(definition, effective_properties)
        for voltage_id in equipment.voltage_class_by_group.values():
            if voltage_id not in self._voltage_classes:
                raise DomainInvariantError(
                    f"Оборудование '{equipment.id}': неизвестный класс "
                    f"напряжения '{voltage_id}'."
                )
        known_groups = {
            item.voltage_group
            for item in definition.port_definitions
            if item.voltage_group is not None
        }
        unknown_groups = set(equipment.voltage_class_by_group) - known_groups
        if unknown_groups:
            raise DomainInvariantError(
                f"Оборудование '{equipment.id}': неизвестные группы напряжения "
                f"{sorted(unknown_groups)}."
            )
        for port_definition in definition.port_definitions:
            group = port_definition.voltage_group
            assigned = equipment.voltage_class_by_group.get(group) if group else None
            if (
                assigned is not None
                and port_definition.allowed_voltage_class_ids is not None
                and assigned not in port_definition.allowed_voltage_class_ids
            ):
                raise DomainInvariantError(
                    f"Оборудование '{equipment.id}': класс '{assigned}' недопустим "
                    f"для группы '{group}'."
                )
        semantic_equipment = replace(
            equipment,
            properties=effective_properties,
        )
        self._validate_equipment_semantics(definition, semantic_equipment)

    def validate_equipment_parameters(
        self,
        type_id: EquipmentTypeId | str,
        *,
        type_version: int = 1,
        properties: Mapping[str, Any] | None = None,
        voltage_class_by_group: Mapping[str, VoltageClassId] | None = None,
        normal_position: SwitchPosition | None = None,
    ) -> None:
        """Validate catalog/editor parameters through the instance contract.

        No object is inserted and the model revision is not changed.
        """
        if isinstance(type_id, str):
            type_id = EquipmentTypeId(type_id)
        definition = self.equipment_type(type_id, type_version)
        candidate = EquipmentInstance(
            EquipmentId("validation.equipment"),
            type_id,
            type_version,
            "Проверка параметров",
            (),
            properties or {},
            voltage_class_by_group or {},
            normal_position,
        )
        self._validate_equipment_configuration(definition, candidate)

    def add_equipment_instance(
        self,
        equipment: EquipmentInstance,
        ports: Iterable[PortInstance],
        *,
        _allow_unowned_line_section: bool = False,
    ) -> ChangeSet:
        definition = self.equipment_type(equipment.type_id, equipment.type_version)
        if (
            definition.behavior_key == "line_section"
            and not _allow_unowned_line_section
        ):
            raise DomainInvariantError(
                "Физический участок создаётся только составной командой "
                "логической линии."
            )
        port_rows = tuple(ports)
        required = {item.role for item in definition.port_definitions if item.required}
        allowed = {item.role for item in definition.port_definitions}
        roles = [item.role for item in port_rows]
        role_set = set(roles)
        if (not required <= role_set or not role_set <= allowed
                or len(roles) != len(role_set)):
            raise DomainInvariantError(
                f"Оборудование '{equipment.id}': роли портов {roles}; "
                f"обязательны {sorted(required)}, допустимы {sorted(allowed)}."
            )
        if tuple(item.id for item in port_rows) != equipment.port_ids:
            raise DomainInvariantError(
                f"Оборудование '{equipment.id}': список port_ids не совпадает с портами."
            )
        if any(item.equipment_id != equipment.id for item in port_rows):
            raise DomainInvariantError(
                f"Оборудование '{equipment.id}': найден порт с другим владельцем."
            )
        self._validate_equipment_configuration(definition, equipment)
        self._ensure_ids_available((equipment.id, *(item.id for item in port_rows)))
        self._equipment[equipment.id] = equipment
        for item in port_rows:
            self._ports[item.id] = item
        return self._changed(added=(equipment.id, *(item.id for item in port_rows)))

    def create_equipment(
        self,
        type_id: EquipmentTypeId | str,
        name: str,
        *,
        type_version: int = 1,
        properties: Mapping[str, Any] | None = None,
        voltage_class_by_group: Mapping[str, VoltageClassId] | None = None,
        normal_position: SwitchPosition | None = None,
        note: str = "",
        extensions: Mapping[str, Any] | None = None,
        equipment_id: EquipmentId | None = None,
        port_ids_by_role: Mapping[str, PortId] | None = None,
        _allow_unowned_line_section: bool = False,
    ) -> tuple[EquipmentInstance, ChangeSet]:
        if isinstance(type_id, str):
            type_id = EquipmentTypeId(type_id)
        definition = self.equipment_type(type_id, type_version)
        equipment_id = equipment_id or EquipmentId.new()
        supplied = dict(port_ids_by_role or {})
        required_roles = {item.role for item in definition.port_definitions if item.required}
        allowed_roles = {item.role for item in definition.port_definitions}
        if supplied and (not required_roles <= set(supplied)
                         or not set(supplied) <= allowed_roles):
            raise DomainInvariantError(
                f"Явные ID портов заданы для ролей {sorted(supplied)}, "
                f"обязательны {sorted(required_roles)}, допустимы {sorted(allowed_roles)}."
            )
        selected_roles = set(supplied) if supplied else required_roles
        ports = tuple(
            PortInstance(
                supplied.get(item.role, PortId.new()), equipment_id, item.role
            )
            for item in definition.port_definitions if item.role in selected_roles
        )
        equipment = EquipmentInstance(
            equipment_id, type_id, type_version, name,
            tuple(item.id for item in ports),
            properties or {}, voltage_class_by_group or {}, normal_position,
            note, extensions or {},
        )
        change = self.add_equipment_instance(
            equipment,
            ports,
            _allow_unowned_line_section=_allow_unowned_line_section,
        )
        return equipment, change

    def adopt_group_voltage_class(
        self,
        equipment_id: EquipmentId,
        group_key: str,
        voltage_class_id: "VoltageClassId",
    ) -> bool:
        """Принять класс напряжения группы, если он ещё НЕ задан.

        Это не «подстройка под соединение», а заполнение пустого поля: класс
        берётся ровно один раз, пока его никто не выбрал. Уже заданный класс —
        решение человека, и молча переписывать его нельзя: от класса зависят
        приведение токов и все уставки, и подмена была бы незаметной сменой
        расчёта.

        Возвращает ``True``, если поле действительно заполнено.
        """
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        if not group_key or equipment.voltage_class_by_group.get(group_key) is not None:
            return False
        if voltage_class_id not in self._voltage_classes:
            raise DomainInvariantError(
                "Класс напряжения не зарегистрирован в проекте."
            )
        groups = {**equipment.voltage_class_by_group, group_key: voltage_class_id}
        replacement = replace(equipment, voltage_class_by_group=groups)
        definition = self.equipment_type(
            replacement.type_id, replacement.type_version
        )
        self._validate_equipment_configuration(definition, replacement)
        self._equipment[equipment_id] = replacement
        self._changed(changed=(equipment_id,))
        return True

    def update_equipment_properties(
        self,
        equipment_id: EquipmentId,
        updates: Mapping[str, Any] | None = None,
        *,
        clear: Iterable[str] = (),
    ) -> ChangeSet:
        """Atomically update one equipment instance without touching its catalog."""
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        if equipment_id in self._line_sections:
            raise DomainInvariantError(
                "Для участка линии используйте set_section_override()/"
                "clear_section_override()."
            )
        _require_mapping(updates or {}, "updates")
        clear_keys = tuple(clear)
        if any(not isinstance(key, str) or not key for key in clear_keys):
            raise DomainInvariantError("clear должен содержать непустые ключи свойств.")
        properties = thaw_json(equipment.properties)
        for key in clear_keys:
            properties.pop(key, None)
        properties.update(thaw_json(_freeze_json(updates or {})))
        replacement = replace(equipment, properties=properties)
        definition = self.equipment_type(
            replacement.type_id, replacement.type_version
        )
        self._validate_equipment_configuration(definition, replacement)
        if replacement == equipment:
            return ChangeSet(self.revision)
        self._equipment[equipment_id] = replacement
        return self._changed(changed=(equipment_id,))

    def set_equipment_property(
        self, equipment_id: EquipmentId, key: str, value: Any
    ) -> ChangeSet:
        _require_string(key, "property key", allow_empty=False)
        return self.update_equipment_properties(equipment_id, {key: value})

    def replace_parameter_records(
        self, equipment_records: Iterable[EquipmentInstance],
        section_records: Iterable[LineSection] = (),
    ) -> ChangeSet:
        """Apply an already typed parameter batch without changing topology.

        One detached model validates the complete batch. No intermediate row
        becomes visible, and this API cannot replace ports, types, voltage
        groups, segment identities or a protection-review marker.
        """
        equipment_rows, sections = tuple(equipment_records), tuple(section_records)
        if len({row.id for row in equipment_rows}) != len(equipment_rows):
            raise DomainInvariantError("Оборудование повторяется в пакете параметров.")
        if len({row.equipment_id for row in sections}) != len(sections):
            raise DomainInvariantError("Ветвь линии повторяется в пакете параметров.")
        def protected_extensions(extensions, *, ct=False):
            result = thaw_json(extensions)
            result.pop("rza_calc.parameter_provenance", None)
            if ct:
                result.pop("editor_legacy_ct_ports", None)
                result.pop("rza_calc.ct_port_id", None)
                result.pop("rza_calc.nameplate", None)
                result.pop("rza_calc.ct_parameters", None)
            return result
        staged = self._transaction_copy()
        changed = []
        for row in equipment_rows:
            old = self._equipment.get(row.id)
            if old is None or replace(old, name=row.name, note=row.note,
                    properties=row.properties, extensions=row.extensions,
                    normal_position=row.normal_position) != row:
                raise DomainInvariantError("Пакет параметров не может менять тип, ID, порты или класс сети.")
            if protected_extensions(old.extensions, ct=True) != protected_extensions(row.extensions, ct=True):
                raise DomainInvariantError("Пакет параметров не может менять служебные ограничения оборудования.")
            staged._equipment[row.id] = row
            if row != old:
                changed.append(row.id)
        for section in sections:
            old = self._line_sections.get(section.equipment_id)
            if (old is None or old.logical_line_id != section.logical_line_id
                    or old.extensions != section.extensions
                    or tuple(s.id for s in old.construction_segments) != tuple(s.id for s in section.construction_segments)):
                raise DomainInvariantError("Пакет параметров не может менять состав или порядок участков линии.")
            for before, after in zip(old.construction_segments, section.construction_segments):
                if before.line_kind != after.line_kind or protected_extensions(before.extensions) != protected_extensions(after.extensions):
                    raise DomainInvariantError("Пакет параметров не может менять тип или служебные данные участка.")
            staged._line_sections[section.equipment_id] = section
            if section != old and section.equipment_id not in changed:
                changed.append(section.equipment_id)
        issues = [item.message for item in staged.validate_integrity() if item.severity == "error"]
        if issues:
            raise DomainInvariantError("Некорректные параметры оборудования: " + "; ".join(issues))
        if not changed:
            return ChangeSet(self.revision)
        return self._commit_transaction(staged, changed=tuple(changed))

    def clear_equipment_property(
        self, equipment_id: EquipmentId, key: str
    ) -> ChangeSet:
        _require_string(key, "property key", allow_empty=False)
        return self.update_equipment_properties(equipment_id, clear=(key,))

    def set_equipment_note(
        self, equipment_id: EquipmentId, note: str
    ) -> ChangeSet:
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        _require_string(note, "EquipmentInstance.note")
        if equipment.note == note:
            return ChangeSet(self.revision)
        self._equipment[equipment_id] = replace(equipment, note=note)
        return self._changed(changed=(equipment_id,))

    @staticmethod
    def _line_section_type_id(line_kind: LineKind) -> EquipmentTypeId:
        type_ids = {
            LineKind.OVERHEAD: "builtin.line_section.overhead",
            LineKind.CABLE: "builtin.line_section.cable",
            LineKind.BUSDUCT: "builtin.line_section.busduct",
        }
        try:
            return EquipmentTypeId(type_ids[line_kind])
        except KeyError as exc:
            raise DomainInvariantError(
                f"Неизвестный вид электрической ветви '{line_kind}'."
            ) from exc

    def _section_endpoint_ids(
        self, equipment_id: EquipmentId
    ) -> tuple[ElectricalNodeId, ElectricalNodeId]:
        first = self.port_by_role(equipment_id, "from")
        second = self.port_by_role(equipment_id, "to")
        first_connection = self.connection_for_port(first.id)
        second_connection = self.connection_for_port(second.id)
        if first_connection is None or second_connection is None:
            raise DomainInvariantError(
                f"Оба порта участка '{equipment_id}' должны быть подключены."
            )
        return (
            first_connection.electrical_node_id,
            second_connection.electrical_node_id,
        )

    @staticmethod
    def _normalize_line_section_kind(
        line: LogicalLine, section: LineSection
    ) -> LineSection:
        """Resolve the compatibility ``line_kind=None`` marker once."""
        if all(
            segment.line_kind is not None
            for segment in section.construction_segments
        ):
            return section
        segments = tuple(
            segment
            if segment.line_kind is not None
            else replace(segment, line_kind=line.line_kind)
            for segment in section.construction_segments
        )
        return LineSection(
            section.equipment_id,
            section.logical_line_id,
            extensions=thaw_json(section.extensions),
            construction_segments=segments,
        )

    def _effective_segment_properties(
        self,
        line: LogicalLine,
        equipment: EquipmentInstance,
        segment: LineConstructionSegment,
    ) -> Mapping[str, Any]:
        if segment.line_kind is None:
            raise DomainInvariantError(
                f"У конструктивного участка '{segment.id}' не задан вид."
            )
        definition = self.equipment_type(
            self._line_section_type_id(segment.line_kind), 1
        )
        effective: dict[str, Any] = {
            item.key: thaw_json(item.default)
            for item in definition.property_definitions
            if item.default is not None
        }
        effective.update(thaw_json(line.inherited_properties))
        # Старые v4/v5 overrides остаются branch-level значениями. Новые
        # segment properties имеют наивысший приоритет и не создают узел.
        effective.update(thaw_json(equipment.properties))
        effective.update(thaw_json(segment.properties))
        self._validate_properties(definition, effective)
        if {"length_mm", "length_km"} & set(effective):
            raise DomainInvariantError(
                "Физическая длина хранится только в конструктивном участке."
            )
        return MappingProxyType(
            {key: _freeze_json(value) for key, value in effective.items()}
        )

    def effective_line_construction_segment_properties(
        self,
        section_id: EquipmentId,
        segment_id: LineConstructionSegmentId,
    ) -> Mapping[str, Any]:
        """Resolve defaults and overrides for one construction segment."""
        section = self.line_section_for_equipment(section_id)
        line = self.logical_line_for_section(section_id)
        equipment = self._equipment[section_id]
        segment = next(
            (
                item
                for item in section.construction_segments
                if item.id == segment_id
            ),
            None,
        )
        if segment is None:
            raise DomainInvariantError(
                f"Конструктивный участок '{segment_id}' не найден в ветви "
                f"'{section_id}'."
            )
        return self._effective_segment_properties(line, equipment, segment)

    def _validate_logical_line_records(
        self, line: LogicalLine, sections: Iterable[LineSection]
    ) -> None:
        section_rows = tuple(sections)
        by_equipment = {item.equipment_id: item for item in section_rows}
        if len(by_equipment) != len(section_rows):
            raise DomainInvariantError(
                f"Линия '{line.id}': запись участка указана повторно."
            )
        if tuple(by_equipment) != line.section_equipment_ids:
            # Dict iteration preserves the caller's order.  Keeping the same
            # order here makes malformed codecs fail instead of guessing it.
            raise DomainInvariantError(
                f"Линия '{line.id}': порядок записей участков не совпадает с линией."
            )
        if (
            line.voltage_class_id is not None
            and line.voltage_class_id not in self._voltage_classes
        ):
            raise DomainInvariantError(
                f"Линия '{line.id}' ссылается на неизвестный класс напряжения."
            )
        expected_type_id = self._line_section_type_id(line.line_kind)
        endpoint_chain: list[ElectricalNodeId] = []
        known_segment_owners = {
            segment.id: owner_id
            for owner_id, existing_section in self._line_sections.items()
            for segment in existing_section.construction_segments
        }
        batch_segment_ids: set[LineConstructionSegmentId] = set()
        for index, equipment_id in enumerate(line.section_equipment_ids):
            section = by_equipment[equipment_id]
            if section.logical_line_id != line.id:
                raise DomainInvariantError(
                    f"Участок '{equipment_id}' принадлежит другой логической линии."
                )
            if equipment_id in self._line_sections:
                raise DomainInvariantError(
                    f"Участок '{equipment_id}' уже входит в логическую линию."
                )
            equipment = self._equipment.get(equipment_id)
            if equipment is None:
                raise DomainInvariantError(
                    f"Участок '{equipment_id}' не найден среди оборудования."
                )
            definition = self.equipment_type(
                equipment.type_id, equipment.type_version
            )
            if (
                definition.behavior_key != "line_section"
                or equipment.type_id != expected_type_id
            ):
                raise DomainInvariantError(
                    f"Оборудование '{equipment_id}' не является участком "
                    f"линии вида '{line.line_kind.value}'."
                )
            if (
                line.voltage_class_id is not None
                and equipment.voltage_class_by_group.get("main")
                != line.voltage_class_id
            ):
                raise DomainInvariantError(
                    f"Участок '{equipment_id}' имеет другой класс напряжения."
                )
            effective = {
                item.key: thaw_json(item.default)
                for item in definition.property_definitions
                if item.default is not None
            }
            effective.update(thaw_json(line.inherited_properties))
            effective.update(thaw_json(equipment.properties))
            self._validate_properties(definition, effective)
            if {"length_mm", "length_km"} & set(effective):
                raise DomainInvariantError(
                    "Длина участка хранится только в LineSection.length_mm."
                )
            for segment in section.construction_segments:
                if segment.id in batch_segment_ids:
                    raise DomainInvariantError(
                        f"Конструктивный участок '{segment.id}' указан повторно."
                    )
                owner_id = known_segment_owners.get(segment.id)
                if owner_id is not None and owner_id != equipment_id:
                    raise DomainInvariantError(
                        f"Конструктивный участок '{segment.id}' уже принадлежит "
                        f"ветви '{owner_id}'."
                    )
                batch_segment_ids.add(segment.id)
                self._effective_segment_properties(line, equipment, segment)
            start, finish = self._section_endpoint_ids(equipment_id)
            if index == 0:
                endpoint_chain.extend((start, finish))
            else:
                if endpoint_chain[-1] != start:
                    raise DomainInvariantError(
                        f"Участки линии '{line.id}' не образуют непрерывную цепочку."
                    )
                endpoint_chain.append(finish)
        if len(endpoint_chain) != len(set(endpoint_chain)):
            raise DomainInvariantError(
                f"Логическая линия '{line.id}' не должна содержать скрытый цикл."
            )

    def add_logical_line(
        self, line: LogicalLine, sections: Iterable[LineSection]
    ) -> ChangeSet:
        """Register a prepared line after its equipment and connections exist."""
        section_rows = tuple(
            self._normalize_line_section_kind(line, item) for item in sections
        )
        feeder_is_known = bool(
            line.feeder_id is not None
            and any(
                existing.feeder_id == line.feeder_id
                for existing in self._logical_lines.values()
            )
        )
        self._ensure_ids_available((
            line.id,
            *((line.feeder_id,) if line.feeder_id is not None and not feeder_is_known else ()),
            *(
                segment.id
                for section in section_rows
                for segment in section.construction_segments
            ),
        ))
        self._validate_logical_line_records(line, section_rows)
        self._logical_lines[line.id] = line
        self._line_sections.update(
            {item.equipment_id: item for item in section_rows}
        )
        return self._changed(added=(
            line.id,
            *((line.feeder_id,) if line.feeder_id is not None and not feeder_is_known else ()),
            *(
                segment.id
                for section in section_rows
                for segment in section.construction_segments
            ),
        ))

    def create_logical_line(
        self,
        name: str,
        line_kind: LineKind,
        from_node_id: ElectricalNodeId,
        to_node_id: ElectricalNodeId,
        length_mm: int | None,
        *,
        voltage_class_id: VoltageClassId | None = None,
        inherited_properties: Mapping[str, Any] | None = None,
        section_properties: Mapping[str, Any] | None = None,
        note: str = "",
        extensions: Mapping[str, Any] | None = None,
        logical_line_id: LogicalLineId | None = None,
        section_equipment_id: EquipmentId | None = None,
        port_ids_by_role: Mapping[str, PortId] | None = None,
        connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
        construction_segments: Iterable[LineConstructionSegment] | None = None,
        length_confirmation: DataConfirmation | None = None,
        impedance_confirmation: DataConfirmation | None = None,
        feeder_id: FeederId | None = None,
    ) -> tuple[LogicalLine, LineSection, ChangeSet]:
        """Create one logical line containing one physical section atomically."""
        if not isinstance(line_kind, LineKind):
            try:
                line_kind = LineKind(line_kind)
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Вид линии должен быть overhead, cable или busduct."
                ) from exc
        if length_mm is not None:
            _require_positive_int(length_mm, "LineSection.length_mm")
        if from_node_id == to_node_id:
            raise DomainInvariantError("Начало и конец участка должны быть разными узлами.")
        first_node = self._electrical_nodes.get(from_node_id)
        second_node = self._electrical_nodes.get(to_node_id)
        if first_node is None or second_node is None:
            raise DomainInvariantError("Оба конечных электрических узла должны существовать.")
        inferred = {
            item
            for item in (
                first_node.declared_voltage_class_id,
                second_node.declared_voltage_class_id,
            )
            if item is not None
        }
        if voltage_class_id is None:
            if len(inferred) > 1:
                raise DomainInvariantError(
                    "Класс напряжения линии нельзя однозначно вывести из конечных узлов."
                )
            if inferred:
                voltage_class_id = next(iter(inferred))
        if voltage_class_id is not None and any(
            item != voltage_class_id for item in inferred
        ):
            raise DomainInvariantError(
                "Класс напряжения линии не совпадает с конечным узлом."
            )
        if (
            voltage_class_id is not None
            and voltage_class_id not in self._voltage_classes
        ):
            raise DomainInvariantError("Класс напряжения линии не зарегистрирован.")

        line_id = logical_line_id or LogicalLineId.new()
        feeder_is_new = bool(
            feeder_id is not None
            and all(
                existing.feeder_id != feeder_id
                for existing in self._logical_lines.values()
            )
        )
        equipment_id = section_equipment_id or EquipmentId.new()
        supplied_ports = dict(port_ids_by_role or {})
        if not supplied_ports:
            supplied_ports = {"from": PortId.new(), "to": PortId.new()}
        ids = connection_ids or (ConnectionId.new(), ConnectionId.new())
        if len(ids) != 2:
            raise DomainInvariantError("Для участка нужны два ID подключения.")

        staged = self._transaction_copy()
        equipment, _ = staged.create_equipment(
            self._line_section_type_id(line_kind),
            name,
            equipment_id=equipment_id,
            port_ids_by_role=supplied_ports,
            properties=section_properties or {},
            voltage_class_by_group=(
                {"main": voltage_class_id}
                if voltage_class_id is not None
                else {}
            ),
            note=note,
            _allow_unowned_line_section=True,
        )
        staged.connect_port(
            staged.port_by_role(equipment.id, "from").id,
            from_node_id,
            connection_id=ids[0],
        )
        staged.connect_port(
            staged.port_by_role(equipment.id, "to").id,
            to_node_id,
            connection_id=ids[1],
        )
        line = LogicalLine(
            line_id,
            name,
            line_kind,
            voltage_class_id,
            (equipment.id,),
            inherited_properties or {},
            note,
            extensions or {},
            feeder_id,
        )
        effective_line_properties = dict(inherited_properties or {})
        effective_line_properties.update(section_properties or {})
        supplied_segments = (
            tuple(construction_segments)
            if construction_segments is not None
            else (
                LineConstructionSegment(
                    LineConstructionSegmentId.new(),
                    line_kind,
                    length_mm,
                    {},
                    {},
                    length_confirmation,
                    (
                        impedance_confirmation
                        if impedance_confirmation is not None
                        else (
                            DataConfirmation.CONFIRMED
                            if effective_line_properties.get("r1_ohm_per_km")
                            is not None
                            and effective_line_properties.get("x1_ohm_per_km")
                            is not None
                            else DataConfirmation.UNCONFIRMED
                        )
                    ),
                ),
            )
        )
        section = LineSection(
            equipment.id,
            line.id,
            length_mm,
            construction_segments=supplied_segments,
        )
        staged.add_logical_line(line, (section,))
        change = self._commit_transaction(
            staged,
            added=(
                line.id,
                *((feeder_id,) if feeder_is_new else ()),
                equipment.id,
                *equipment.port_ids,
                *ids,
                *(item.id for item in section.construction_segments),
            ),
        )
        return line, section, change

    def line_section_for_equipment(self, equipment_id: EquipmentId) -> LineSection:
        try:
            return self._line_sections[equipment_id]
        except KeyError as exc:
            raise DomainInvariantError(
                f"Оборудование '{equipment_id}' не является участком логической линии."
            ) from exc

    def logical_line_for_section(self, equipment_id: EquipmentId) -> LogicalLine:
        section = self.line_section_for_equipment(equipment_id)
        try:
            return self._logical_lines[section.logical_line_id]
        except KeyError as exc:
            raise DomainInvariantError(
                f"Логическая линия '{section.logical_line_id}' не найдена."
            ) from exc

    @staticmethod
    def _line_split_provenance(section: LineSection) -> dict[str, Any]:
        """Return lossless physical source data for an unconfirmed split.

        The payload is diagnostic/provenance data only.  It deliberately does
        not make either new branch calculable until an engineer confirms the
        physical position and distributes the source construction records.
        """
        return {
            "source_section_id": section.equipment_id.value,
            "source_logical_line_id": section.logical_line_id.value,
            "source_total_length_mm": section.length_mm,
            "source_construction_segments": [
                {
                    "id": item.id.value,
                    "line_kind": (
                        item.line_kind.value if item.line_kind is not None else None
                    ),
                    "length_mm": item.length_mm,
                    "properties": thaw_json(item.properties),
                    "extensions": thaw_json(item.extensions),
                    "length_confirmation": item.length_confirmation.value,
                    "impedance_confirmation": item.impedance_confirmation.value,
                }
                for item in section.construction_segments
            ],
        }

    @staticmethod
    def _split_construction_segments(
        section: LineSection,
        offset_mm: int | None,
    ) -> tuple[
        tuple[LineConstructionSegment, ...],
        tuple[LineConstructionSegment, ...],
    ]:
        """Partition ordered physical data at an offset from branch start."""
        if offset_mm is None:
            source_rows = section.construction_segments

            def placeholder(
                source: LineConstructionSegment,
                side: str,
            ) -> LineConstructionSegment:
                segment_extensions = thaw_json(source.extensions)
                segment_extensions["unconfirmed_physical_split"] = {
                    "source_section_id": section.equipment_id.value,
                    "source_segment_ids": [item.id.value for item in source_rows],
                    "side": side,
                }
                # With one source record its per-km data remains truthful even
                # though the distributed length is unknown.  With several
                # records no one set of properties may be assigned to a side.
                one_source = len(source_rows) == 1
                return LineConstructionSegment(
                    LineConstructionSegmentId.new(),
                    source.line_kind,
                    None,
                    properties=(thaw_json(source.properties) if one_source else {}),
                    extensions=segment_extensions,
                    length_confirmation=DataConfirmation.UNCONFIRMED,
                    impedance_confirmation=(
                        source.impedance_confirmation
                        if one_source
                        else DataConfirmation.UNCONFIRMED
                    ),
                )

            first_source = source_rows[0]
            second_source = source_rows[-1]
            first = placeholder(first_source, "before")
            second = placeholder(second_source, "after")
            return ((first,), (second,))
        if section.length_mm is None:
            raise DomainInvariantError(
                "Для линии с неизвестной длиной нельзя задавать абсолютное "
                "место разбиения."
            )
        first: list[LineConstructionSegment] = []
        second: list[LineConstructionSegment] = []
        cursor = 0
        for segment in section.construction_segments:
            if segment.length_mm is None:
                raise DomainInvariantError(
                    "Известная общая длина несовместима с неизвестным конструктивным участком."
                )
            finish = cursor + segment.length_mm
            if finish <= offset_mm:
                first.append(segment)
            elif cursor >= offset_mm:
                second.append(segment)
            else:
                first_length = offset_mm - cursor
                second_length = finish - offset_mm
                # offset is strictly inside the branch. Equality with a
                # segment boundary is handled by the branches above.
                first.append(replace(
                    segment,
                    id=LineConstructionSegmentId.new(),
                    length_mm=first_length,
                ))
                second.append(replace(
                    segment,
                    id=LineConstructionSegmentId.new(),
                    length_mm=second_length,
                ))
            cursor = finish
        if not first or not second:
            raise DomainInvariantError(
                "Разбиение должно оставить конструктивные участки с обеих сторон."
            )
        return tuple(first), tuple(second)

    def split_line_section(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        *,
        tap_node_id: ElectricalNodeId | None = None,
        first_section_id: EquipmentId | None = None,
        second_section_id: EquipmentId | None = None,
        first_port_ids_by_role: Mapping[str, PortId] | None = None,
        second_port_ids_by_role: Mapping[str, PortId] | None = None,
        connection_ids: tuple[ConnectionId, ConnectionId, ConnectionId, ConnectionId]
        | None = None,
    ) -> SplitLineSectionResult:
        """Replace one physical section by two around a new electrical node."""
        section = self.line_section_for_equipment(section_id)
        if offset_mm is not None and (
            isinstance(offset_mm, bool) or not isinstance(offset_mm, int)
        ):
            raise DomainInvariantError("Место разбиения задаётся целым числом миллиметров.")
        if (
            section.length_mm is not None
            and offset_mm is not None
            and not 0 < offset_mm < section.length_mm
        ):
            raise DomainInvariantError("Место разбиения должно лежать внутри участка.")
        if section.length_mm is None and offset_mm is not None:
            raise DomainInvariantError(
                "Физическая длина линии не подтверждена; место разбиения "
                "должно оставаться неподтверждённым."
            )
        line = self.logical_line_for_section(section_id)
        old = self._equipment[section_id]
        start_node_id, finish_node_id = self._section_endpoint_ids(section_id)
        old_connections = tuple(
            connection
            for connection in self._connections.values()
            if connection.port_id in old.port_ids
        )
        if len(old_connections) != 2:
            raise DomainInvariantError("Разбиваемый участок должен иметь два подключения.")

        tap_id = tap_node_id or ElectricalNodeId.new()
        first_id = first_section_id or EquipmentId.new()
        second_id = second_section_id or EquipmentId.new()
        first_ports = dict(first_port_ids_by_role or {}) or {
            "from": PortId.new(),
            "to": PortId.new(),
        }
        second_ports = dict(second_port_ids_by_role or {}) or {
            "from": PortId.new(),
            "to": PortId.new(),
        }
        new_connection_ids = connection_ids or tuple(
            ConnectionId.new() for _ in range(4)
        )
        if len(new_connection_ids) != 4:
            raise DomainInvariantError("После разбиения нужны четыре ID подключения.")

        first_segments, second_segments = self._split_construction_segments(
            section, offset_mm
        )
        old_segment_ids = {
            item.id for item in section.construction_segments
        }
        new_segment_ids = {
            item.id for item in (*first_segments, *second_segments)
        }
        self._ensure_ids_available(new_segment_ids - old_segment_ids)

        staged = self._transaction_copy()
        tap_node = ElectricalNode(
            tap_id,
            f"Узел отпайки: {line.name}",
            declared_voltage_class_id=line.voltage_class_id,
            extensions={
                "junction_kind": "line_tap",
                "creation_origin": "automatic_line_split",
                "physical_offset_mm": offset_mm,
                "physical_position_confirmed": offset_mm is not None,
            },
        )
        staged.add_node(tap_node)
        first, _ = staged.create_equipment(
            old.type_id,
            f"{old.name} — участок 1",
            type_version=old.type_version,
            equipment_id=first_id,
            port_ids_by_role=first_ports,
            properties=thaw_json(old.properties),
            voltage_class_by_group=dict(old.voltage_class_by_group),
            note=old.note,
            extensions=thaw_json(old.extensions),
            _allow_unowned_line_section=True,
        )
        second, _ = staged.create_equipment(
            old.type_id,
            f"{old.name} — участок 2",
            type_version=old.type_version,
            equipment_id=second_id,
            port_ids_by_role=second_ports,
            properties=thaw_json(old.properties),
            voltage_class_by_group=dict(old.voltage_class_by_group),
            note=old.note,
            extensions=thaw_json(old.extensions),
            _allow_unowned_line_section=True,
        )
        for port_id, node_id, connection_id in (
            (staged.port_by_role(first.id, "from").id, start_node_id, new_connection_ids[0]),
            (staged.port_by_role(first.id, "to").id, tap_id, new_connection_ids[1]),
            (staged.port_by_role(second.id, "from").id, tap_id, new_connection_ids[2]),
            (staged.port_by_role(second.id, "to").id, finish_node_id, new_connection_ids[3]),
        ):
            staged.connect_port(port_id, node_id, connection_id=connection_id)

        position = line.section_equipment_ids.index(section_id)
        replacement_ids = (
            *line.section_equipment_ids[:position],
            first.id,
            second.id,
            *line.section_equipment_ids[position + 1 :],
        )
        staged._logical_lines[line.id] = replace(
            line, section_equipment_ids=replacement_ids
        )
        del staged._line_sections[section_id]
        first_section_extensions = thaw_json(section.extensions)
        second_section_extensions = thaw_json(section.extensions)
        if offset_mm is None:
            provenance = self._line_split_provenance(section)
            first_section_extensions["unconfirmed_physical_split"] = {
                **provenance,
                "physical_position_confirmed": False,
            }
            second_section_extensions["unconfirmed_physical_split"] = {
                **provenance,
                "physical_position_confirmed": False,
            }
        staged._line_sections[first.id] = LineSection(
            first.id,
            line.id,
            extensions=first_section_extensions,
            construction_segments=first_segments,
        )
        staged._line_sections[second.id] = LineSection(
            second.id,
            line.id,
            extensions=second_section_extensions,
            construction_segments=second_segments,
        )
        staged.remove_equipment(section_id, cascade=True)
        staged._validate_logical_line_records_for_existing(line.id)
        removed_ids = (
            old.id,
            *old.port_ids,
            *(item.id for item in old_connections),
        )
        change = self._commit_transaction(
            staged,
            added=(tap_id, first.id, *first.port_ids, second.id, *second.port_ids,
                   *new_connection_ids,
                   *sorted(
                       new_segment_ids - old_segment_ids,
                       key=lambda item: item.value,
                   )),
            changed=(line.id,),
            removed=(
                *removed_ids,
                *sorted(
                    old_segment_ids - new_segment_ids,
                    key=lambda item: item.value,
                ),
            ),
        )
        return SplitLineSectionResult(
            line.id, section_id, first.id, second.id, tap_id, change
        )

    def create_tap_line(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        branch_name: str,
        branch_kind: LineKind,
        branch_to_node_id: ElectricalNodeId,
        branch_length_mm: int | None,
        *,
        branch_inherited_properties: Mapping[str, Any] | None = None,
        branch_section_properties: Mapping[str, Any] | None = None,
        branch_length_confirmation: DataConfirmation | None = None,
        branch_impedance_confirmation: DataConfirmation | None = None,
    ) -> tuple[SplitLineSectionResult, LogicalLine, LineSection, ChangeSet]:
        """Split an existing line and start a new line from that junction atomically."""
        source_line = self.logical_line_for_section(section_id)
        staged = self._transaction_copy()
        split = staged.split_line_section(section_id, offset_mm)
        branch, branch_section, branch_change = staged.create_logical_line(
            branch_name,
            branch_kind,
            split.tap_node_id,
            branch_to_node_id,
            branch_length_mm,
            voltage_class_id=source_line.voltage_class_id,
            inherited_properties=branch_inherited_properties,
            section_properties=branch_section_properties,
            length_confirmation=branch_length_confirmation,
            impedance_confirmation=branch_impedance_confirmation,
        )
        added_values = tuple(dict.fromkeys(
            (*split.change.added_ids, *branch_change.added_ids)
        ))
        changed_values = tuple(dict.fromkeys(
            (*split.change.changed_ids, *branch_change.changed_ids)
        ))
        removed_values = tuple(dict.fromkeys(
            (*split.change.removed_ids, *branch_change.removed_ids)
        ))
        change = self._commit_transaction(
            staged,
            added=(StableId(item) for item in added_values),
            changed=(StableId(item) for item in changed_values),
            removed=(StableId(item) for item in removed_values),
        )
        return (
            replace(split, change=change),
            branch,
            branch_section,
            change,
        )

    def insert_series_equipment_in_line(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        equipment_type_id: EquipmentTypeId | str,
        name: str,
        *,
        type_version: int = 1,
        terminal_roles: tuple[str, str] | None = None,
        manual_placement_confirmed: bool = False,
        properties: Mapping[str, Any] | None = None,
        voltage_class_by_group: Mapping[str, VoltageClassId] | None = None,
        normal_position: SwitchPosition | None = None,
        note: str = "",
        extensions: Mapping[str, Any] | None = None,
        feeder_id: FeederId | None = None,
        right_logical_line_id: LogicalLineId | None = None,
        right_node_id: ElectricalNodeId | None = None,
        equipment_id: EquipmentId | None = None,
        equipment_port_ids_by_role: Mapping[str, PortId] | None = None,
        equipment_connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
    ) -> InsertSeriesEquipmentResult:
        """Вставить поддерживаемый двухполюсный аппарат одной транзакцией.

        Две непрерывные части линии остаются строгими ``LogicalLine``. Их
        общая пользовательская принадлежность хранится typed ``FeederId``;
        поэтому валидатор не ослабляет порядок участков и не притворяется,
        будто два разных узла по сторонам аппарата являются одним узлом.
        Разрешение на такую установку является частью определения типа и не
        зависит от SVG или графического символа.
        """
        if isinstance(equipment_type_id, str):
            equipment_type_id = EquipmentTypeId(equipment_type_id)
        definition = self.equipment_type(equipment_type_id, type_version)
        _require_bool(
            manual_placement_confirmed,
            "manual_placement_confirmed",
        )
        has_inline_placement = (
            PLACEMENT_INLINE_SERIES_CAPABILITY in definition.capabilities
        )
        has_manual_placement = (
            PLACEMENT_MANUAL_SELECTION_CAPABILITY in definition.capabilities
        )
        if not has_inline_placement and not (
            has_manual_placement and manual_placement_confirmed
        ):
            raise DomainInvariantError(
                f"Тип '{definition.id}' нельзя устанавливать последовательно в линию."
            )
        declared_roles = tuple(item.role for item in definition.port_definitions)
        if len(declared_roles) != 2:
            raise DomainInvariantError(
                "Последовательная вставка поддерживает только тип с двумя "
                "электрическими портами."
            )
        if terminal_roles is None:
            selected_terminal_roles = (declared_roles[0], declared_roles[1])
        else:
            if not isinstance(terminal_roles, (tuple, list)):
                raise DomainInvariantError(
                    "Роли выводов должны быть последовательностью из двух значений."
                )
            selected_terminal_roles = tuple(terminal_roles)
            if (
                len(selected_terminal_roles) != 2
                or selected_terminal_roles[0] == selected_terminal_roles[1]
                or set(selected_terminal_roles) != set(declared_roles)
            ):
                raise DomainInvariantError(
                    "Роли двух выводов последовательного аппарата не "
                    "соответствуют определению его типа."
                )
        left_terminal_role, right_terminal_role = selected_terminal_roles
        source_line = self.logical_line_for_section(section_id)
        if source_line.voltage_class_id is None:
            raise DomainInvariantError(
                "Перед вставкой аппарата нужно определить класс напряжения линии."
            )
        selected_voltage_groups = dict(voltage_class_by_group or {})
        for port_definition in definition.port_definitions:
            group = port_definition.voltage_group
            if group is None:
                continue
            assigned_voltage = selected_voltage_groups.setdefault(
                group, source_line.voltage_class_id
            )
            if assigned_voltage != source_line.voltage_class_id:
                raise DomainInvariantError(
                    "Класс напряжения последовательного аппарата должен "
                    "совпадать с классом напряжения линии."
                )
        effective_normal_position = normal_position
        if (
            effective_normal_position is None
            and "switch.position" in definition.capabilities
        ):
            try:
                effective_normal_position = SwitchPosition(
                    definition.extensions.get(
                        "default_normal_position", SwitchPosition.CLOSED.value
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    f"Тип '{definition.id}' содержит неверное нормальное "
                    "положение коммутационного аппарата."
                ) from exc
        selected_feeder_id = feeder_id or source_line.feeder_id or FeederId.new()
        continuation_id = right_logical_line_id or LogicalLineId.new()
        selected_right_node_id = right_node_id or ElectricalNodeId.new()
        selected_equipment_id = equipment_id or EquipmentId.new()
        selected_equipment_ports = dict(equipment_port_ids_by_role or {}) or {
            left_terminal_role: PortId.new(),
            right_terminal_role: PortId.new(),
        }
        if set(selected_equipment_ports) != set(selected_terminal_roles):
            raise DomainInvariantError(
                "ID портов последовательного аппарата должны быть заданы "
                "ровно для двух выбранных ролей."
            )
        selected_equipment_connections = equipment_connection_ids or (
            ConnectionId.new(),
            ConnectionId.new(),
        )
        if len(selected_equipment_connections) != 2:
            raise DomainInvariantError(
                "Для последовательного аппарата нужны два ID электрических подключений."
            )

        staged = self._transaction_copy()
        split = staged.split_line_section(section_id, offset_mm)
        split_line = staged._logical_lines[source_line.id]
        split_index = split_line.section_equipment_ids.index(split.first_section_id)
        if (
            split_index + 1 >= len(split_line.section_equipment_ids)
            or split_line.section_equipment_ids[split_index + 1]
            != split.second_section_id
        ):
            raise DomainInvariantError(
                "После разбиения части линии потеряли физический порядок."
            )

        feeder_is_new = all(
            line.feeder_id != selected_feeder_id
            for line in staged._logical_lines.values()
        )
        pending_ids: list[StableId] = [
            continuation_id,
            selected_right_node_id,
            selected_equipment_id,
            *selected_equipment_ports.values(),
            *selected_equipment_connections,
        ]
        if feeder_is_new:
            pending_ids.append(selected_feeder_id)
        staged._ensure_ids_available(pending_ids)

        left_ids = split_line.section_equipment_ids[: split_index + 1]
        right_ids = split_line.section_equipment_ids[split_index + 1 :]
        staged._logical_lines[source_line.id] = replace(
            split_line,
            section_equipment_ids=left_ids,
            feeder_id=selected_feeder_id,
        )
        continuation = LogicalLine(
            continuation_id,
            f"{source_line.name} — продолжение",
            source_line.line_kind,
            source_line.voltage_class_id,
            right_ids,
            thaw_json(source_line.inherited_properties),
            source_line.note,
            thaw_json(source_line.extensions),
            selected_feeder_id,
        )
        staged._logical_lines[continuation.id] = continuation
        for item_id in right_ids:
            staged._line_sections[item_id] = replace(
                staged._line_sections[item_id],
                logical_line_id=continuation.id,
            )

        left_node = staged._electrical_nodes[split.tap_node_id]
        staged._electrical_nodes[left_node.id] = replace(
            left_node,
            name=f"Узел перед аппаратом: {name}",
            extensions={
                "junction_kind": "inline_device_left",
                "creation_origin": "automatic_inline_insertion",
                "physical_offset_mm": offset_mm,
                "physical_position_confirmed": offset_mm is not None,
            },
        )
        right_node = ElectricalNode(
            selected_right_node_id,
            f"Узел после аппарата: {name}",
            declared_voltage_class_id=source_line.voltage_class_id,
            extensions={
                "junction_kind": "inline_device_right",
                "creation_origin": "automatic_inline_insertion",
                "physical_offset_mm": offset_mm,
                "physical_position_confirmed": offset_mm is not None,
            },
        )
        staged._electrical_nodes[right_node.id] = right_node

        second_from_port = staged.port_by_role(split.second_section_id, "from")
        second_connection = staged.connection_for_port(second_from_port.id)
        if (
            second_connection is None
            or second_connection.electrical_node_id != left_node.id
        ):
            raise DomainInvariantError(
                "Начало второй части линии не подключено к узлу разбиения."
            )
        staged._connections[second_connection.id] = replace(
            second_connection, electrical_node_id=right_node.id
        )

        marker = thaw_json(extensions or {})
        marker["line_insertion"] = {
            "kind": "inline_series",
            "equipment_type_id": definition.id.value,
            "equipment_type_version": definition.schema_version,
            "terminal_roles": [left_terminal_role, right_terminal_role],
            "feeder_id": selected_feeder_id.value,
            "left_logical_line_id": source_line.id.value,
            "right_logical_line_id": continuation.id.value,
            "left_node_id": left_node.id.value,
            "right_node_id": right_node.id.value,
            "manual_placement_confirmed": manual_placement_confirmed,
            "physical_offset_mm": offset_mm,
            "physical_position_confirmed": offset_mm is not None,
        }
        inline_equipment, _ = staged.create_equipment(
            definition.id,
            name,
            type_version=definition.schema_version,
            equipment_id=selected_equipment_id,
            port_ids_by_role=selected_equipment_ports,
            properties=properties or {},
            voltage_class_by_group=selected_voltage_groups,
            normal_position=effective_normal_position,
            note=note,
            extensions=marker,
        )
        staged.connect_port(
            staged.port_by_role(inline_equipment.id, left_terminal_role).id,
            left_node.id,
            connection_id=selected_equipment_connections[0],
        )
        staged.connect_port(
            staged.port_by_role(inline_equipment.id, right_terminal_role).id,
            right_node.id,
            connection_id=selected_equipment_connections[1],
        )
        staged._validate_logical_line_records_for_existing(source_line.id)
        staged._validate_logical_line_records_for_existing(continuation.id)

        added = [
            *(StableId(item) for item in split.change.added_ids),
            continuation.id,
            right_node.id,
            inline_equipment.id,
            *inline_equipment.port_ids,
            *selected_equipment_connections,
        ]
        if feeder_is_new:
            added.append(selected_feeder_id)
        changed = [
            *(StableId(item) for item in split.change.changed_ids),
            source_line.id,
            second_connection.id,
        ]
        removed = [StableId(item) for item in split.change.removed_ids]
        change = self._commit_transaction(
            staged,
            added=tuple(dict.fromkeys(added)),
            changed=tuple(dict.fromkeys(changed)),
            removed=tuple(dict.fromkeys(removed)),
        )
        return InsertSeriesEquipmentResult(
            selected_feeder_id,
            source_line.id,
            continuation.id,
            section_id,
            split.first_section_id,
            split.second_section_id,
            left_node.id,
            right_node.id,
            inline_equipment.id,
            selected_terminal_roles,
            change,
        )

    def insert_recloser_in_line(
        self,
        section_id: EquipmentId,
        offset_mm: int | None,
        name: str,
        *,
        properties: Mapping[str, Any],
        normal_position: SwitchPosition = SwitchPosition.CLOSED,
        note: str = "",
        extensions: Mapping[str, Any] | None = None,
        feeder_id: FeederId | None = None,
        right_logical_line_id: LogicalLineId | None = None,
        right_node_id: ElectricalNodeId | None = None,
        recloser_id: EquipmentId | None = None,
        recloser_port_ids_by_role: Mapping[str, PortId] | None = None,
        recloser_connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
    ) -> InsertRecloserResult:
        """Compatibility wrapper for the Stage 1 recloser contract."""
        result = self.insert_series_equipment_in_line(
            section_id,
            offset_mm,
            "builtin.recloser",
            name,
            terminal_roles=("a", "b"),
            properties=properties,
            normal_position=normal_position,
            note=note,
            extensions=extensions,
            feeder_id=feeder_id,
            right_logical_line_id=right_logical_line_id,
            right_node_id=right_node_id,
            equipment_id=recloser_id,
            equipment_port_ids_by_role=recloser_port_ids_by_role,
            equipment_connection_ids=recloser_connection_ids,
        )
        return InsertRecloserResult(
            result.feeder_id,
            result.left_logical_line_id,
            result.right_logical_line_id,
            result.removed_section_id,
            result.left_section_id,
            result.right_section_id,
            result.left_node_id,
            result.right_node_id,
            result.equipment_id,
            result.change,
        )

    def remove_series_equipment_from_line(
        self,
        equipment_id: EquipmentId,
        *,
        terminal_roles: tuple[str, str] | None = None,
        merged_section_id: EquipmentId | None = None,
        merged_port_ids_by_role: Mapping[str, PortId] | None = None,
        merged_connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
    ) -> RemoveSeriesEquipmentResult:
        """Удалить ранее вставленный аппарат и восстановить цельную линию.

        Команда намеренно принимает только аппарат с typed-маркером
        ``line_insertion`` и placement-capability. Узлы по его сторонам должны быть служебными и не
        иметь иных присоединений. Если половины линии после вставки были
        изменены несовместимо, операция отклоняется без частичных изменений.
        """
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(
                f"Оборудование '{equipment_id}' не найдено."
            )
        definition = self.equipment_type(equipment.type_id, equipment.type_version)
        has_inline_placement = (
            PLACEMENT_INLINE_SERIES_CAPABILITY in definition.capabilities
        )
        has_manual_placement = (
            PLACEMENT_MANUAL_SELECTION_CAPABILITY in definition.capabilities
        )
        if not has_inline_placement and not has_manual_placement:
            raise DomainInvariantError(
                "Выбранное оборудование не поддерживает последовательную вставку."
            )
        insertion = equipment.extensions.get("line_insertion")
        if not isinstance(insertion, Mapping):
            raise DomainInvariantError(
                "Оборудование не содержит данных безопасной вставки в линию."
            )
        if (
            not has_inline_placement
            and insertion.get("manual_placement_confirmed") is not True
        ):
            raise DomainInvariantError(
                "Ручное последовательное размещение оборудования не подтверждено."
            )
        marker_kind = insertion.get("kind")
        if marker_kind is not None and marker_kind != "inline_series":
            raise DomainInvariantError(
                "Маркер оборудования не описывает последовательную вставку."
            )
        marker_type_id = insertion.get("equipment_type_id")
        if marker_type_id is not None and marker_type_id != equipment.type_id.value:
            raise DomainInvariantError(
                "Тип оборудования не соответствует данным его вставки в линию."
            )
        marker_type_version = insertion.get("equipment_type_version")
        if (
            marker_type_version is not None
            and marker_type_version != equipment.type_version
        ):
            raise DomainInvariantError(
                "Версия типа оборудования не соответствует данным вставки."
            )
        declared_roles = tuple(item.role for item in definition.port_definitions)
        marker_roles = insertion.get("terminal_roles")
        raw_roles = terminal_roles if terminal_roles is not None else marker_roles
        if raw_roles is None:
            selected_terminal_roles = declared_roles
        elif isinstance(raw_roles, (tuple, list)):
            selected_terminal_roles = tuple(raw_roles)
        else:
            raise DomainInvariantError(
                "Маркер содержит неверный формат ролей выводов аппарата."
            )
        if (
            len(declared_roles) != 2
            or len(selected_terminal_roles) != 2
            or selected_terminal_roles[0] == selected_terminal_roles[1]
            or set(selected_terminal_roles) != set(declared_roles)
        ):
            raise DomainInvariantError(
                "Роли выводов последовательного аппарата повреждены или "
                "не соответствуют определению типа."
            )
        left_terminal_role, right_terminal_role = selected_terminal_roles
        try:
            feeder_id = FeederId(str(insertion["feeder_id"]))
            left_line_id = LogicalLineId(
                str(insertion["left_logical_line_id"])
            )
            right_line_id = LogicalLineId(
                str(insertion["right_logical_line_id"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainInvariantError(
                "Данные вставки оборудования повреждены."
            ) from exc
        left_line = self._logical_lines.get(left_line_id)
        right_line = self._logical_lines.get(right_line_id)
        if left_line is None or right_line is None:
            raise DomainInvariantError(
                "Части линии по сторонам аппарата не найдены."
            )
        if left_line_id == right_line_id:
            raise DomainInvariantError(
                "Последовательный аппарат должен разделять две разные логические линии."
            )
        if (
            left_line.feeder_id != feeder_id
            or right_line.feeder_id != feeder_id
        ):
            raise DomainInvariantError(
                "Части линии потеряли общую принадлежность фидеру."
            )
        if (
            left_line.line_kind != right_line.line_kind
            or left_line.voltage_class_id != right_line.voltage_class_id
            or left_line.inherited_properties != right_line.inherited_properties
            or left_line.note != right_line.note
            or left_line.extensions != right_line.extensions
        ):
            raise DomainInvariantError(
                "Части линии имеют несовместимые общие параметры и не могут "
                "быть объединены автоматически."
            )

        left_section_id = left_line.section_equipment_ids[-1]
        right_section_id = right_line.section_equipment_ids[0]
        left_node_id = self._section_endpoint_ids(left_section_id)[1]
        right_node_id = self._section_endpoint_ids(right_section_id)[0]
        left_connection = self.connection_for_port(
            self.port_by_role(equipment_id, left_terminal_role).id
        )
        right_connection = self.connection_for_port(
            self.port_by_role(equipment_id, right_terminal_role).id
        )
        if (
            left_connection is None
            or right_connection is None
            or left_connection.electrical_node_id != left_node_id
            or right_connection.electrical_node_id != right_node_id
            or left_node_id == right_node_id
        ):
            raise DomainInvariantError(
                "Электрические подключения аппарата не соответствуют "
                "сохранённой вставке в линию."
            )
        marker_left_node = insertion.get("left_node_id")
        marker_right_node = insertion.get("right_node_id")
        if (
            marker_left_node is not None
            and marker_left_node != left_node_id.value
        ) or (
            marker_right_node is not None
            and marker_right_node != right_node_id.value
        ):
            raise DomainInvariantError(
                "Служебные узлы не соответствуют данным вставки оборудования."
            )
        for node_id, adjacent_id in (
            (left_node_id, left_section_id),
            (right_node_id, right_section_id),
        ):
            connected_equipment = {
                self._ports[item.port_id].equipment_id
                for item in self._connections.values()
                if item.electrical_node_id == node_id
            }
            if connected_equipment != {adjacent_id, equipment_id}:
                raise DomainInvariantError(
                    "К служебному узлу последовательного аппарата подключено другое "
                    "оборудование; автоматическое удаление небезопасно."
                )

        selected_merged_id = merged_section_id or EquipmentId.new()
        selected_ports = dict(merged_port_ids_by_role or {}) or {
            "from": PortId.new(),
            "to": PortId.new(),
        }
        selected_connections = merged_connection_ids or (
            ConnectionId.new(),
            ConnectionId.new(),
        )
        if len(selected_connections) != 2:
            raise DomainInvariantError(
                "Для восстановленного участка нужны два подключения."
            )

        staged = self._transaction_copy()
        remove_device_change = staged.remove_equipment(
            equipment_id, cascade=True
        )
        merge_nodes_change = staged.merge_nodes(left_node_id, right_node_id)
        staged_left = staged._logical_lines[left_line_id]
        staged_right = staged._logical_lines[right_line_id]
        staged._logical_lines[left_line_id] = replace(
            staged_left,
            section_equipment_ids=(
                *staged_left.section_equipment_ids,
                *staged_right.section_equipment_ids,
            ),
        )
        for section_id in staged_right.section_equipment_ids:
            staged._line_sections[section_id] = replace(
                staged._line_sections[section_id],
                logical_line_id=left_line_id,
            )
        del staged._logical_lines[right_line_id]
        staged._validate_logical_line_records_for_existing(left_line_id)
        collapse_change = staged.remove_line_tap(
            left_line_id,
            left_node_id,
            collapse=True,
            merged_section_id=selected_merged_id,
            merged_port_ids_by_role=selected_ports,
            merged_connection_ids=selected_connections,
        )
        added_ids = tuple(dict.fromkeys(collapse_change.added_ids))
        changed_ids = tuple(dict.fromkeys((
            *remove_device_change.changed_ids,
            *merge_nodes_change.changed_ids,
            *collapse_change.changed_ids,
            left_line_id.value,
        )))
        removed_ids = tuple(dict.fromkeys((
            *remove_device_change.removed_ids,
            *merge_nodes_change.removed_ids,
            *collapse_change.removed_ids,
            right_line_id.value,
        )))
        change = self._commit_transaction(
            staged,
            added=(StableId(item) for item in added_ids),
            changed=(StableId(item) for item in changed_ids),
            removed=(StableId(item) for item in removed_ids),
        )
        return RemoveSeriesEquipmentResult(
            feeder_id,
            left_line_id,
            right_line_id,
            equipment_id,
            left_section_id,
            right_section_id,
            left_node_id,
            right_node_id,
            selected_merged_id,
            selected_terminal_roles,
            change,
        )

    def remove_recloser_from_line(
        self,
        recloser_id: EquipmentId,
        *,
        merged_section_id: EquipmentId | None = None,
        merged_port_ids_by_role: Mapping[str, PortId] | None = None,
        merged_connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
    ) -> RemoveRecloserResult:
        """Compatibility wrapper for the Stage 4 recloser result contract."""
        equipment = self._equipment.get(recloser_id)
        if equipment is None:
            raise DomainInvariantError(
                f"Реклоузер '{recloser_id}' не найден."
            )
        if equipment.type_id != EquipmentTypeId("builtin.recloser"):
            raise DomainInvariantError(
                "Выбранное оборудование не является реклоузером."
            )
        result = self.remove_series_equipment_from_line(
            recloser_id,
            terminal_roles=("a", "b"),
            merged_section_id=merged_section_id,
            merged_port_ids_by_role=merged_port_ids_by_role,
            merged_connection_ids=merged_connection_ids,
        )
        return RemoveRecloserResult(
            result.feeder_id,
            result.logical_line_id,
            result.removed_right_logical_line_id,
            result.removed_equipment_id,
            result.removed_left_section_id,
            result.removed_right_section_id,
            result.removed_left_node_id,
            result.removed_right_node_id,
            result.merged_section_id,
            result.change,
        )

    def _validate_logical_line_records_for_existing(
        self, logical_line_id: LogicalLineId
    ) -> None:
        """Validate a line already placed in this aggregate's stores."""
        line = self._logical_lines[logical_line_id]
        rows = tuple(self._line_sections[item] for item in line.section_equipment_ids)
        # Temporarily hide membership because the shared validator rejects a
        # second registration of an existing section.
        hidden = self._line_sections
        self._line_sections = {
            key: value for key, value in hidden.items()
            if value.logical_line_id != logical_line_id
        }
        try:
            self._validate_logical_line_records(line, rows)
        finally:
            self._line_sections = hidden

    def set_line_inherited_property(
        self, logical_line_id: LogicalLineId, key: str, value: Any
    ) -> ChangeSet:
        line = self._logical_lines.get(logical_line_id)
        if line is None:
            raise DomainInvariantError(f"Логическая линия '{logical_line_id}' не найдена.")
        properties = thaw_json(line.inherited_properties)
        properties[key] = value
        staged = self._transaction_copy()
        staged._logical_lines[line.id] = replace(line, inherited_properties=properties)
        if key in {"r1_ohm_per_km", "x1_ohm_per_km"}:
            staged._refresh_line_impedance_confirmation(line.id, previous=self)
        staged._validate_logical_line_records_for_existing(line.id)
        return self._commit_transaction(staged, changed=(line.id,))

    def set_section_override(
        self, section_id: EquipmentId, key: str, value: Any
    ) -> ChangeSet:
        section = self.line_section_for_equipment(section_id)
        equipment = self._equipment[section_id]
        properties = thaw_json(equipment.properties)
        properties[key] = value
        staged = self._transaction_copy()
        staged._equipment[section_id] = replace(equipment, properties=properties)
        if key in {"r1_ohm_per_km", "x1_ohm_per_km"}:
            staged._refresh_line_impedance_confirmation(section.logical_line_id, previous=self)
        staged._validate_logical_line_records_for_existing(section.logical_line_id)
        return self._commit_transaction(staged, changed=(section_id,))

    def clear_section_override(
        self, section_id: EquipmentId, key: str
    ) -> ChangeSet:
        section = self.line_section_for_equipment(section_id)
        equipment = self._equipment[section_id]
        if key not in equipment.properties:
            return ChangeSet(self.revision)
        properties = thaw_json(equipment.properties)
        del properties[key]
        staged = self._transaction_copy()
        staged._equipment[section_id] = replace(equipment, properties=properties)
        if key in {"r1_ohm_per_km", "x1_ohm_per_km"}:
            staged._refresh_line_impedance_confirmation(section.logical_line_id, previous=self)
        staged._validate_logical_line_records_for_existing(section.logical_line_id)
        return self._commit_transaction(staged, changed=(section_id,))

    def _refresh_line_impedance_confirmation(
        self, logical_line_id: LogicalLineId, *, previous: "ElectricalModel"
    ) -> None:
        line = self._logical_lines[logical_line_id]
        for section_id in line.section_equipment_ids:
            section = self._line_sections[section_id]
            equipment = self._equipment[section_id]
            equipment_extensions = thaw_json(equipment.extensions)
            equipment_provenance = equipment_extensions.get("rza_calc.parameter_provenance", {})
            old_equipment_values = previous.effective_equipment_properties(section_id)
            equipment_values = self.effective_equipment_properties(section_id)
            if isinstance(equipment_provenance, dict):
                for key in ("r1_ohm_per_km", "x1_ohm_per_km"):
                    if equipment_values.get(key) != old_equipment_values.get(key) and isinstance(equipment_provenance.get(key), dict):
                        equipment_provenance[key]["confirmation"] = DataConfirmation.UNCONFIRMED.value
            if equipment_extensions != equipment.extensions:
                equipment = replace(equipment, extensions=equipment_extensions)
                self._equipment[section_id] = equipment
            updated: list[LineConstructionSegment] = []
            for segment in section.construction_segments:
                effective = self._effective_segment_properties(
                    line, equipment, segment
                )
                old_effective = previous.effective_line_construction_segment_properties(section_id, segment.id)
                unchanged = all(effective.get(key) == old_effective.get(key)
                                for key in ("r1_ohm_per_km", "x1_ohm_per_km"))
                confirmed = (
                    unchanged
                    and segment.impedance_confirmation is DataConfirmation.CONFIRMED
                    and
                    effective.get("r1_ohm_per_km") is not None
                    and effective.get("x1_ohm_per_km") is not None
                )
                extensions = thaw_json(segment.extensions)
                provenance = extensions.get("rza_calc.parameter_provenance", {})
                if isinstance(provenance, dict):
                    for key in ("r1_ohm_per_km", "x1_ohm_per_km"):
                        if effective.get(key) != old_effective.get(key) and isinstance(provenance.get(key), dict):
                            provenance[key]["confirmation"] = DataConfirmation.UNCONFIRMED.value
                updated.append(replace(
                    segment,
                    extensions=extensions,
                    impedance_confirmation=(
                        DataConfirmation.CONFIRMED
                        if confirmed
                        else DataConfirmation.UNCONFIRMED
                    ),
                ))
            self._line_sections[section_id] = LineSection(
                section.equipment_id,
                section.logical_line_id,
                extensions=thaw_json(section.extensions),
                construction_segments=updated,
            )

    def replace_line_construction_segments(
        self,
        section_id: EquipmentId,
        segments: Iterable[LineConstructionSegment],
    ) -> ChangeSet:
        """Atomically replace ordered construction data without changing topology."""
        section = self.line_section_for_equipment(section_id)
        replacement_segments = tuple(segments)
        replacement = LineSection(
            section.equipment_id,
            section.logical_line_id,
            extensions=thaw_json(section.extensions),
            construction_segments=replacement_segments,
        )
        if replacement == section:
            return ChangeSet(self.revision)

        old_ids = {item.id for item in section.construction_segments}
        known_values = self._object_id_values() - {
            item.value for item in old_ids
        }
        pending_values: set[str] = set()
        for segment in replacement.construction_segments:
            value = segment.id.value
            if value == "GRID":
                raise DomainInvariantError(
                    "ID 'GRID' зарезервирован расчётным адаптером."
                )
            if value in known_values or value in pending_values:
                raise DomainInvariantError(
                    f"ID '{value}' уже используется в электрической модели."
                )
            pending_values.add(value)

        staged = self._transaction_copy()
        staged._line_sections[section_id] = replacement
        staged._validate_logical_line_records_for_existing(
            section.logical_line_id
        )
        new_ids = {item.id for item in replacement.construction_segments}
        return self._commit_transaction(
            staged,
            added=tuple(sorted(new_ids - old_ids, key=lambda item: item.value)),
            changed=(section_id,),
            removed=tuple(sorted(old_ids - new_ids, key=lambda item: item.value)),
        )

    def add_line_construction_segment(
        self,
        section_id: EquipmentId,
        segment: LineConstructionSegment,
        *,
        index: int | None = None,
    ) -> ChangeSet:
        """Insert a segment; no electrical node or connection is created."""
        if not isinstance(segment, LineConstructionSegment):
            raise DomainInvariantError(
                "Добавлять можно только LineConstructionSegment."
            )
        section = self.line_section_for_equipment(section_id)
        if index is None:
            index = len(section.construction_segments)
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= len(section.construction_segments)
        ):
            raise DomainInvariantError(
                "Позиция конструктивного участка находится вне ветви."
            )
        segments = list(section.construction_segments)
        segments.insert(index, segment)
        return self.replace_line_construction_segments(section_id, segments)

    def update_line_construction_segment(
        self,
        section_id: EquipmentId,
        segment: LineConstructionSegment,
    ) -> ChangeSet:
        """Replace one segment by stable ID, preserving its sequence position."""
        if not isinstance(segment, LineConstructionSegment):
            raise DomainInvariantError(
                "Изменять можно только LineConstructionSegment."
            )
        section = self.line_section_for_equipment(section_id)
        matches = [
            index
            for index, item in enumerate(section.construction_segments)
            if item.id == segment.id
        ]
        if len(matches) != 1:
            raise DomainInvariantError(
                f"Конструктивный участок '{segment.id}' не найден в ветви "
                f"'{section_id}'."
            )
        segments = list(section.construction_segments)
        segments[matches[0]] = segment
        return self.replace_line_construction_segments(section_id, segments)

    def remove_line_construction_segment(
        self,
        section_id: EquipmentId,
        segment_id: LineConstructionSegmentId,
    ) -> ChangeSet:
        """Remove one segment while keeping every electrical connection intact."""
        _require_id(
            segment_id,
            LineConstructionSegmentId,
            "segment_id",
        )
        section = self.line_section_for_equipment(section_id)
        segments = tuple(
            item
            for item in section.construction_segments
            if item.id != segment_id
        )
        if len(segments) == len(section.construction_segments):
            raise DomainInvariantError(
                f"Конструктивный участок '{segment_id}' не найден в ветви "
                f"'{section_id}'."
            )
        if not segments:
            raise DomainInvariantError(
                "Электрическая ветвь должна содержать хотя бы один "
                "конструктивный участок."
            )
        return self.replace_line_construction_segments(section_id, segments)

    def set_section_length(
        self,
        section_id: EquipmentId,
        length_mm: int,
        *,
        confirmation: DataConfirmation = DataConfirmation.CONFIRMED,
    ) -> ChangeSet:
        _require_positive_int(length_mm, "LineSection.length_mm")
        section = self.line_section_for_equipment(section_id)
        if len(section.construction_segments) != 1:
            raise DomainInvariantError(
                "Длину ветви с несколькими конструктивными участками "
                "изменяйте отдельно для каждого участка."
            )
        segment = replace(
            section.construction_segments[0],
            length_mm=length_mm,
            length_confirmation=confirmation,
        )
        return self.update_line_construction_segment(section_id, segment)

    def port_definition(self, port_id: PortId) -> PortDefinition:
        port = self._ports.get(port_id)
        if port is None:
            raise DomainInvariantError(f"Порт '{port_id}' не найден.")
        equipment = self._equipment[port.equipment_id]
        definition = self.equipment_type(equipment.type_id, equipment.type_version)
        return next(item for item in definition.port_definitions if item.role == port.role)

    def port_voltage_class(self, port_id: PortId) -> VoltageClassId | None:
        port = self._ports.get(port_id)
        if port is None:
            raise DomainInvariantError(f"Порт '{port_id}' не найден.")
        equipment = self._equipment[port.equipment_id]
        definition = self.port_definition(port_id)
        if definition.voltage_group is None:
            return None
        return equipment.voltage_class_by_group.get(definition.voltage_group)

    def connection_for_port(self, port_id: PortId) -> Connection | None:
        return next(
            (item for item in self._connections.values() if item.port_id == port_id), None
        )

    def node_for_port(self, port_id: PortId) -> ElectricalNode | None:
        connection = self.connection_for_port(port_id)
        return self._electrical_nodes.get(connection.electrical_node_id) if connection else None

    def ports_of(self, equipment_id: EquipmentId) -> tuple[PortInstance, ...]:
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        return tuple(self._ports[item] for item in equipment.port_ids)

    def port_by_role(self, equipment_id: EquipmentId, role: str) -> PortInstance:
        for port in self.ports_of(equipment_id):
            if port.role == role:
                return port
        raise DomainInvariantError(
            f"У оборудования '{equipment_id}' нет порта с ролью '{role}'."
        )

    def _check_connection_compatibility(
        self, port: PortInstance, node: ElectricalNode, *,
        _connections_by_node: Mapping[ElectricalNodeId, list[Connection]] | None = None,
        _connections_by_equipment: Mapping[EquipmentId, list[Connection]] | None = None,
    ) -> None:
        definition = self.port_definition(port.id)
        if definition.kind_id != node.kind_id:
            raise DomainInvariantError(
                f"Порт '{port.id}' и узел '{node.id}' имеют разные электрические типы."
            )
        port_voltage = self.port_voltage_class(port.id)
        node_voltage = node.declared_voltage_class_id
        if port_voltage is not None and node_voltage is not None and port_voltage != node_voltage:
            raise DomainInvariantError(
                f"Порт '{port.id}' и узел '{node.id}' имеют несовместимые классы напряжения."
            )
        if (definition.allowed_voltage_class_ids is not None and node_voltage is not None
                and node_voltage not in definition.allowed_voltage_class_ids):
            raise DomainInvariantError(
                f"Класс напряжения узла '{node.id}' недопустим для порта '{port.id}'."
            )
        voltage_ids = {node_voltage, port_voltage} - {None}
        node_connections = (
            self._connections.values() if _connections_by_node is None
            else _connections_by_node.get(node.id, ())
        )
        for connection in node_connections:
            if (connection.electrical_node_id != node.id
                    or connection.port_id == port.id):
                continue
            if connection.port_id not in self._ports:
                continue
            peer_voltage = self.port_voltage_class(connection.port_id)
            if peer_voltage is not None:
                voltage_ids.add(peer_voltage)
        if len(voltage_ids) > 1:
            raise DomainInvariantError(
                f"Узел '{node.id}' нельзя объединить с портами разных классов напряжения."
            )
        equipment = self._equipment.get(port.equipment_id)
        if equipment is not None and len(equipment.port_ids) > 1:
            equipment_connections = (
                self._connections.values() if _connections_by_equipment is None
                else _connections_by_equipment.get(equipment.id, ())
            )
            for connection in equipment_connections:
                peer = self._ports.get(connection.port_id)
                if (
                    peer is not None
                    and peer.equipment_id == equipment.id
                    and peer.id != port.id
                    and connection.electrical_node_id == node.id
                ):
                    raise DomainInvariantError(
                        f"Разные электрические порты одного оборудования "
                        f"'{equipment.name}' нельзя подключить к одному узлу."
                    )

    def add_connection(self, connection: Connection) -> ChangeSet:
        self._ensure_ids_available((connection.id,))
        port = self._ports.get(connection.port_id)
        node = self._electrical_nodes.get(connection.electrical_node_id)
        if port is None:
            raise DomainInvariantError(
                f"Connection '{connection.id}': порт '{connection.port_id}' не найден."
            )
        if node is None:
            raise DomainInvariantError(
                f"Connection '{connection.id}': узел '{connection.electrical_node_id}' не найден."
            )
        if self.connection_for_port(port.id) is not None:
            raise DomainInvariantError(f"Порт '{port.id}' уже подключён к электрическому узлу.")
        self._check_connection_compatibility(port, node)
        self._connections[connection.id] = connection
        return self._changed(added=(connection.id,))

    def connect_port(self, port_id: PortId, node_id: ElectricalNodeId, *,
                     connection_id: ConnectionId | None = None,
                     extensions: Mapping[str, Any] | None = None
                     ) -> tuple[Connection, ChangeSet]:
        connection = Connection(
            connection_id or ConnectionId.new(), port_id, node_id, extensions or {}
        )
        return connection, self.add_connection(connection)

    def connect_ports(
        self,
        first_port_id: PortId,
        second_port_id: PortId,
        *,
        node_id: ElectricalNodeId | None = None,
        node_name: str = "",
        connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
    ) -> ChangeSet:
        if first_port_id == second_port_id:
            raise DomainInvariantError("Нельзя соединить порт с самим собой.")
        if first_port_id not in self._ports or second_port_id not in self._ports:
            raise DomainInvariantError("Оба соединяемых порта должны существовать.")
        if (
            self._ports[first_port_id].equipment_id
            == self._ports[second_port_id].equipment_id
        ):
            raise DomainInvariantError(
                "Разные электрические порты одного оборудования нельзя "
                "подключить к одному узлу."
            )
        first_connection = self.connection_for_port(first_port_id)
        second_connection = self.connection_for_port(second_port_id)
        if first_connection and second_connection:
            if first_connection.electrical_node_id == second_connection.electrical_node_id:
                raise DomainInvariantError("Порты уже принадлежат одному электрическому узлу.")
            raise DomainInvariantError(
                "Порты принадлежат разным узлам; используйте явную команду merge_nodes()."
            )

        existing_node_id = (
            first_connection.electrical_node_id if first_connection else
            second_connection.electrical_node_id if second_connection else None
        )
        added: list[StableId] = []
        created_node: ElectricalNode | None = None
        if existing_node_id is None:
            node_id = node_id or ElectricalNodeId.new()
            first_voltage = self.port_voltage_class(first_port_id)
            second_voltage = self.port_voltage_class(second_port_id)
            if first_voltage and second_voltage and first_voltage != second_voltage:
                raise DomainInvariantError("Соединяемые порты имеют разные классы напряжения.")
            created_node = ElectricalNode(
                node_id, node_name, self.port_definition(first_port_id).kind_id,
                first_voltage or second_voltage,
                extensions={"creation_origin": "automatic_port_to_port"},
            )
            target_node = created_node
        else:
            if node_id is not None and node_id != existing_node_id:
                raise DomainInvariantError("Явный node_id не совпадает с существующим узлом.")
            target_node = self._electrical_nodes[existing_node_id]

        ids = connection_ids or tuple(ConnectionId.new() for _ in range(2))
        if len(ids) != 2:
            raise DomainInvariantError("Для connect_ports нужны два ID подключения.")
        pending_connections = [
            (port_id, connection_id)
            for port_id, connection, connection_id in (
                (first_port_id, first_connection, ids[0]),
                (second_port_id, second_connection, ids[1]),
            )
            if connection is None
        ]
        # Сначала конструируем все records: runtime-типы ID проверяются до
        # первой мутации, поэтому ошибка во втором connection не оставит узел.
        new_connections = tuple(
            Connection(connection_id, port_id, target_node.id)
            for port_id, connection_id in pending_connections
        )
        pending_ids: list[StableId] = [item.id for item in new_connections]
        if created_node is not None:
            pending_ids.insert(0, created_node.id)
        self._ensure_ids_available(pending_ids)
        for connection in new_connections:
            self._check_connection_compatibility(
                self._ports[connection.port_id], target_node
            )

        if created_node is not None:
            self._electrical_nodes[created_node.id] = created_node
            added.append(created_node.id)
        for connection in new_connections:
            self._connections[connection.id] = connection
            added.append(connection.id)
        return self._changed(added=added)

    def reconnect_port(
        self,
        port_id: PortId,
        node_id: ElectricalNodeId,
    ) -> tuple[Connection, ChangeSet]:
        """Атомарно перенести существующий конец, сохранив ID связи и порта."""
        port = self._ports.get(port_id)
        node = self._electrical_nodes.get(node_id)
        connection = self.connection_for_port(port_id)
        if port is None or node is None:
            raise DomainInvariantError("Порт и новый электрический узел должны существовать.")
        if connection is None:
            raise DomainInvariantError(f"Порт '{port_id}' ещё не подключён.")
        if connection.electrical_node_id == node_id:
            raise DomainInvariantError("Порт уже подключён к выбранному узлу.")

        staged = self._transaction_copy()
        staged_connection = staged._connections[connection.id]
        # Убираем старую запись до проверки: capacity порта остаётся равной
        # единице, а проверка новой цели видит только будущих соседей.
        del staged._connections[connection.id]
        staged._check_connection_compatibility(staged._ports[port_id], node)
        replacement = replace(
            staged_connection, electrical_node_id=node_id
        )
        staged._connections[replacement.id] = replacement
        for logical_line_id in {
            staged._line_sections[equipment_id].logical_line_id
            for equipment_id in (port.equipment_id,)
            if equipment_id in staged._line_sections
        }:
            staged._validate_logical_line_records_for_existing(logical_line_id)
        issues = [
            item for item in staged.validate_integrity()
            if item.severity == "error"
        ]
        if issues:
            raise DomainInvariantError(issues[0].message)
        change = self._commit_transaction(
            staged, changed=(connection.id, port.equipment_id)
        )
        return replacement, change

    def merge_nodes(self, keep_id: ElectricalNodeId,
                    remove_id: ElectricalNodeId) -> ChangeSet:
        if keep_id == remove_id:
            raise DomainInvariantError("Для слияния нужны два разных узла.")
        keep = self._electrical_nodes.get(keep_id)
        removed = self._electrical_nodes.get(remove_id)
        if keep is None or removed is None:
            raise DomainInvariantError("Оба сливаемых узла должны существовать.")
        if keep.kind_id != removed.kind_id:
            raise DomainInvariantError("Нельзя слить узлы разных электрических типов.")
        if (keep.declared_voltage_class_id and removed.declared_voltage_class_id
                and keep.declared_voltage_class_id != removed.declared_voltage_class_id):
            raise DomainInvariantError("Нельзя слить узлы разных классов напряжения.")
        effective_keep = keep
        if keep.declared_voltage_class_id is None and removed.declared_voltage_class_id:
            effective_keep = replace(
                keep,
                declared_voltage_class_id=removed.declared_voltage_class_id,
            )
        replacements: dict[ConnectionId, Connection] = {}
        merged_voltage_ids = {
            effective_keep.declared_voltage_class_id,
        } - {None}
        for connection in self._connections.values():
            if connection.electrical_node_id not in {keep_id, remove_id}:
                continue
            if connection.port_id in self._ports:
                voltage_id = self.port_voltage_class(connection.port_id)
                if voltage_id is not None:
                    merged_voltage_ids.add(voltage_id)
        if len(merged_voltage_ids) > 1:
            raise DomainInvariantError(
                "Нельзя слить узлы, к которым подключены порты разных классов напряжения."
            )
        merged_equipment_ports: dict[EquipmentId, set[PortId]] = {}
        for connection in self._connections.values():
            if connection.electrical_node_id not in {keep_id, remove_id}:
                continue
            port = self._ports.get(connection.port_id)
            if port is None:
                continue
            merged_equipment_ports.setdefault(port.equipment_id, set()).add(port.id)
        duplicated_equipment = tuple(
            equipment_id
            for equipment_id, port_ids in merged_equipment_ports.items()
            if len(port_ids) > 1
        )
        if duplicated_equipment:
            raise DomainInvariantError(
                "Слияние узлов подключит разные электрические порты одного "
                "оборудования к одному узлу."
            )
        for connection in self._connections.values():
            if connection.electrical_node_id in {keep_id, remove_id}:
                port = self._ports.get(connection.port_id)
                if port is None:
                    raise DomainInvariantError(
                        f"Connection '{connection.id}' ссылается на отсутствующий порт."
                    )
                self._check_connection_compatibility(port, effective_keep)
            if connection.electrical_node_id == remove_id:
                replacements[connection.id] = replace(
                    connection, electrical_node_id=keep_id
                )
        for line in self._logical_lines.values():
            chain: list[ElectricalNodeId] = []
            for index, section_id in enumerate(line.section_equipment_ids):
                start, finish = self._section_endpoint_ids(section_id)
                start = keep_id if start == remove_id else start
                finish = keep_id if finish == remove_id else finish
                if index == 0:
                    chain.extend((start, finish))
                else:
                    chain.append(finish)
            if len(chain) != len(set(chain)):
                raise DomainInvariantError(
                    f"Слияние узлов создаст скрытый цикл внутри линии '{line.id}'."
                )
        self._electrical_nodes[keep_id] = effective_keep
        self._connections.update(replacements)
        del self._electrical_nodes[remove_id]
        return self._changed(
            changed=(*replacements.keys(), keep_id), removed=(remove_id,)
        )

    def remove_connection(self, connection_id: ConnectionId) -> ChangeSet:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise DomainInvariantError(f"Connection '{connection_id}' не найден.")
        port = self._ports.get(connection.port_id)
        if port is not None and port.equipment_id in self._line_sections:
            raise DomainInvariantError(
                "Подключение физического участка изменяется только составной "
                "командой линии."
            )
        del self._connections[connection_id]
        return self._changed(removed=(connection_id,))

    def remove_equipment(self, equipment_id: EquipmentId, *, cascade: bool = False
                         ) -> ChangeSet:
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        if equipment_id in self._line_sections:
            raise DomainInvariantError(
                "Участок логической линии удаляется только командой "
                "remove_line_section() или remove_logical_line()."
            )
        connection_ids = [
            item.id for item in self._connections.values()
            if item.port_id in equipment.port_ids
        ]
        if connection_ids and not cascade:
            raise DomainInvariantError(
                f"Оборудование '{equipment_id}' подключено; используйте cascade=True."
            )
        # Prepare every immutable OperatingState replacement before mutating
        # aggregate stores.  This keeps deletion atomic even when extensions
        # contain legacy Mode data in addition to normalized positions.
        state_replacements: dict[OperatingStateId, OperatingState] = {}
        for state_id, state in self._operating_states.items():
            position_changed = equipment_id in state.positions
            availability_changed = equipment_id in state.availability
            extensions = _without_legacy_mode_references(state, equipment)
            if not position_changed and not availability_changed and extensions is None:
                continue
            positions = dict(state.positions)
            positions.pop(equipment_id, None)
            availability = dict(state.availability)
            availability.pop(equipment_id, None)
            state_replacements[state_id] = replace(
                state,
                positions=positions,
                extensions=state.extensions if extensions is None else extensions,
                availability=availability,
            )

        removed_ids: list[StableId] = [equipment.id, *equipment.port_ids, *connection_ids]
        for connection_id in connection_ids:
            del self._connections[connection_id]
        for port_id in equipment.port_ids:
            del self._ports[port_id]
        del self._equipment[equipment_id]
        self._operating_states.update(state_replacements)
        return self._changed(
            changed=state_replacements, removed=removed_ids
        )

    def remove_logical_line(
        self, logical_line_id: LogicalLineId, *, cascade: bool = False
    ) -> ChangeSet:
        """Remove a user line explicitly; its junction nodes are preserved."""
        line = self._logical_lines.get(logical_line_id)
        if line is None:
            raise DomainInvariantError(f"Логическая линия '{logical_line_id}' не найдена.")
        if not cascade:
            raise DomainInvariantError(
                "Логическая линия содержит физические участки; используйте cascade=True."
            )
        staged = self._transaction_copy()
        del staged._logical_lines[line.id]
        removed: list[StableId] = [line.id]
        changed_states: set[OperatingStateId] = set()
        for section_id in line.section_equipment_ids:
            equipment = staged._equipment[section_id]
            section = staged._line_sections[section_id]
            connection_ids = tuple(
                item.id
                for item in staged._connections.values()
                if item.port_id in equipment.port_ids
            )
            del staged._line_sections[section_id]
            before_states = dict(staged._operating_states)
            staged.remove_equipment(section_id, cascade=True)
            changed_states.update(
                state_id
                for state_id, state in staged._operating_states.items()
                if before_states.get(state_id) != state
            )
            removed.extend((
                equipment.id,
                *equipment.port_ids,
                *connection_ids,
                *(item.id for item in section.construction_segments),
            ))
        return self._commit_transaction(
            staged,
            changed=changed_states,
            removed=removed,
        )

    def remove_line_section(
        self,
        section_id: EquipmentId,
        *,
        continuation_line_id: LogicalLineId | None = None,
    ) -> ChangeSet:
        """Remove one section without leaving dangling logical-line references.

        Removing a middle section naturally splits the user grouping into two
        logical lines.  The upstream part retains the original line ID; the
        downstream part receives a new explicit ID.
        """
        section = self.line_section_for_equipment(section_id)
        line = self.logical_line_for_section(section_id)
        index = line.section_equipment_ids.index(section_id)
        equipment = self._equipment[section_id]
        connection_ids = tuple(
            item.id
            for item in self._connections.values()
            if item.port_id in equipment.port_ids
        )
        staged = self._transaction_copy()
        del staged._line_sections[section_id]
        staged.remove_equipment(section_id, cascade=True)

        added: list[StableId] = []
        changed: list[StableId] = []
        removed: list[StableId] = [
            equipment.id,
            *equipment.port_ids,
            *connection_ids,
            *(item.id for item in section.construction_segments),
        ]
        prefix = line.section_equipment_ids[:index]
        suffix = line.section_equipment_ids[index + 1 :]
        if not prefix and not suffix:
            del staged._logical_lines[line.id]
            removed.append(line.id)
        elif not prefix or not suffix:
            remaining = prefix or suffix
            staged._logical_lines[line.id] = replace(
                line, section_equipment_ids=remaining
            )
            staged._validate_logical_line_records_for_existing(line.id)
            changed.append(line.id)
        else:
            new_id = continuation_line_id or LogicalLineId.new()
            staged._ensure_ids_available((new_id,))
            staged._logical_lines[line.id] = replace(
                line, section_equipment_ids=prefix
            )
            continuation = LogicalLine(
                new_id,
                f"{line.name} — продолжение",
                line.line_kind,
                line.voltage_class_id,
                suffix,
                thaw_json(line.inherited_properties),
                line.note,
                thaw_json(line.extensions),
            )
            staged._logical_lines[new_id] = continuation
            for item_id in suffix:
                staged._line_sections[item_id] = replace(
                    staged._line_sections[item_id], logical_line_id=new_id
                )
            staged._validate_logical_line_records_for_existing(line.id)
            staged._validate_logical_line_records_for_existing(new_id)
            added.append(new_id)
            changed.append(line.id)
        return self._commit_transaction(
            staged, added=added, changed=changed, removed=removed
        )

    def remove_line_tap(
        self,
        logical_line_id: LogicalLineId,
        tap_node_id: ElectricalNodeId,
        *,
        branch_line_ids: Iterable[LogicalLineId] = (),
        collapse: bool = True,
        merged_section_id: EquipmentId | None = None,
        merged_port_ids_by_role: Mapping[str, PortId] | None = None,
        merged_connection_ids: tuple[ConnectionId, ConnectionId] | None = None,
    ) -> ChangeSet:
        """Remove named tap branches and optionally merge equal main sections."""
        main = self._logical_lines.get(logical_line_id)
        if main is None:
            raise DomainInvariantError(
                f"Логическая линия '{logical_line_id}' не найдена."
            )
        if tap_node_id not in self._electrical_nodes:
            raise DomainInvariantError(f"Узел отпайки '{tap_node_id}' не найден.")
        branch_ids = tuple(branch_line_ids)
        if len(branch_ids) != len(set(branch_ids)) or logical_line_id in branch_ids:
            raise DomainInvariantError("Список линий отпайки некорректен.")
        for branch_id in branch_ids:
            branch = self._logical_lines.get(branch_id)
            if branch is None:
                raise DomainInvariantError(f"Линия отпайки '{branch_id}' не найдена.")
            touches_tap = any(
                tap_node_id in self._section_endpoint_ids(section_id)
                for section_id in branch.section_equipment_ids
            )
            if not touches_tap:
                raise DomainInvariantError(
                    f"Линия '{branch_id}' не подключена к узлу '{tap_node_id}'."
                )

        staged = self._transaction_copy()
        changed_values: list[str] = []
        removed_values: list[str] = []
        for branch_id in branch_ids:
            result = staged.remove_logical_line(branch_id, cascade=True)
            changed_values.extend(result.changed_ids)
            removed_values.extend(result.removed_ids)
        if not collapse:
            return self._commit_transaction(
                staged,
                changed=(StableId(item) for item in dict.fromkeys(changed_values)),
                removed=(StableId(item) for item in dict.fromkeys(removed_values)),
            )

        main = staged._logical_lines[logical_line_id]
        pairs = [
            (index, first_id, second_id)
            for index, (first_id, second_id) in enumerate(zip(
                main.section_equipment_ids, main.section_equipment_ids[1:]
            ))
            if staged._section_endpoint_ids(first_id)[1] == tap_node_id
            and staged._section_endpoint_ids(second_id)[0] == tap_node_id
        ]
        tap_connections = tuple(
            item for item in staged._connections.values()
            if item.electrical_node_id == tap_node_id
        )
        if len(pairs) != 1 or len(tap_connections) != 2:
            raise DomainInvariantError(
                "После удаления выбранных ветвей узел должен соединять только "
                "два соседних участка основной линии."
            )
        index, first_id, second_id = pairs[0]
        first = staged._equipment[first_id]
        second = staged._equipment[second_id]
        first_section = staged._line_sections[first_id]
        second_section = staged._line_sections[second_id]
        if (
            first.type_id != second.type_id
            or first.type_version != second.type_version
            or first.voltage_class_by_group != second.voltage_class_by_group
            or first.properties != second.properties
            or first.note != second.note
            or first.extensions != second.extensions
            or first_section.extensions != second_section.extensions
            or staged.effective_equipment_properties(first_id)
            != staged.effective_equipment_properties(second_id)
        ):
            raise DomainInvariantError(
                "Соседние участки имеют разные параметры и не могут быть схлопнуты."
            )

        start_node_id = staged._section_endpoint_ids(first_id)[0]
        finish_node_id = staged._section_endpoint_ids(second_id)[1]
        new_id = merged_section_id or EquipmentId.new()
        port_ids = dict(merged_port_ids_by_role or {}) or {
            "from": PortId.new(), "to": PortId.new()
        }
        connection_ids = merged_connection_ids or (
            ConnectionId.new(), ConnectionId.new()
        )
        if len(connection_ids) != 2:
            raise DomainInvariantError("Для объединённого участка нужны два подключения.")
        merged, _ = staged.create_equipment(
            first.type_id,
            f"{main.name} — объединённый участок",
            type_version=first.type_version,
            equipment_id=new_id,
            port_ids_by_role=port_ids,
            properties=thaw_json(first.properties),
            voltage_class_by_group=dict(first.voltage_class_by_group),
            note=first.note,
            extensions=thaw_json(first.extensions),
            _allow_unowned_line_section=True,
        )
        staged.connect_port(
            staged.port_by_role(merged.id, "from").id,
            start_node_id,
            connection_id=connection_ids[0],
        )
        staged.connect_port(
            staged.port_by_role(merged.id, "to").id,
            finish_node_id,
            connection_id=connection_ids[1],
        )
        staged._logical_lines[main.id] = replace(
            main,
            section_equipment_ids=(
                *main.section_equipment_ids[:index],
                merged.id,
                *main.section_equipment_ids[index + 2:],
            ),
        )
        staged._line_sections[merged.id] = LineSection(
            merged.id,
            main.id,
            extensions=thaw_json(first_section.extensions),
            construction_segments=(
                *first_section.construction_segments,
                *second_section.construction_segments,
            ),
        )
        for equipment_id in (first_id, second_id):
            equipment = staged._equipment[equipment_id]
            old_connections = tuple(
                item.id for item in staged._connections.values()
                if item.port_id in equipment.port_ids
            )
            del staged._line_sections[equipment_id]
            staged.remove_equipment(equipment_id, cascade=True)
            removed_values.extend((
                equipment.id.value,
                *(item.value for item in equipment.port_ids),
                *(item.value for item in old_connections),
            ))
        staged.remove_node(tap_node_id)
        removed_values.append(tap_node_id.value)
        staged._validate_logical_line_records_for_existing(main.id)
        changed_values.append(main.id.value)
        added_values = (
            merged.id.value,
            *(item.value for item in merged.port_ids),
            *(item.value for item in connection_ids),
        )
        return self._commit_transaction(
            staged,
            added=(StableId(item) for item in added_values),
            changed=(StableId(item) for item in dict.fromkeys(changed_values)),
            removed=(StableId(item) for item in dict.fromkeys(removed_values)),
        )

    def remove_node(self, node_id: ElectricalNodeId, *, cascade: bool = False
                    ) -> ChangeSet:
        if node_id not in self._electrical_nodes:
            raise DomainInvariantError(f"Узел '{node_id}' не найден.")
        connection_ids = [
            item.id for item in self._connections.values()
            if item.electrical_node_id == node_id
        ]
        connected_section_ids = {
            self._ports[item.port_id].equipment_id
            for item in self._connections.values()
            if item.electrical_node_id == node_id and item.port_id in self._ports
            and self._ports[item.port_id].equipment_id in self._line_sections
        }
        if connected_section_ids:
            raise DomainInvariantError(
                "Узел участвует в логической линии; используйте составную "
                "команду удаления/схлопывания отпайки."
            )
        if connection_ids and not cascade:
            raise DomainInvariantError(
                f"Узел '{node_id}' имеет подключения; используйте cascade=True."
            )
        for connection_id in connection_ids:
            del self._connections[connection_id]
        del self._electrical_nodes[node_id]
        return self._changed(removed=(node_id, *connection_ids))

    def rename_equipment(self, equipment_id: EquipmentId, name: str) -> ChangeSet:
        equipment = self._equipment.get(equipment_id)
        if equipment is None:
            raise DomainInvariantError(f"Оборудование '{equipment_id}' не найдено.")
        self._equipment[equipment_id] = replace(equipment, name=name)
        return self._changed(changed=(equipment_id,))

    def add_operating_state(self, state: OperatingState) -> ChangeSet:
        self._ensure_ids_available((state.id,))
        for equipment_id in state.positions:
            equipment = self._equipment.get(equipment_id)
            if equipment is None:
                raise DomainInvariantError(
                    f"Режим '{state.id}' ссылается на отсутствующее оборудование."
                )
            definition = self.equipment_type(equipment.type_id, equipment.type_version)
            if not ({"switch.position", "legacy.switch.position"}
                    & definition.capabilities):
                raise DomainInvariantError(
                    f"Оборудование '{equipment_id}' не имеет состояния OPEN/CLOSED."
                )
        for equipment_id in state.availability:
            equipment = self._equipment.get(equipment_id)
            if equipment is None:
                raise DomainInvariantError(
                    f"Режим '{state.id}' ссылается на отсутствующее оборудование."
                )
            definition = self.equipment_type(equipment.type_id, equipment.type_version)
            if EQUIPMENT_AVAILABILITY_CAPABILITY not in definition.capabilities:
                raise DomainInvariantError(
                    f"Оборудование '{equipment_id}' не поддерживает "
                    "эксплуатационную доступность."
                )
        self._operating_states[state.id] = state
        return self._changed(added=(state.id,))

    def set_switch_position(self, state_id: OperatingStateId,
                            equipment_id: EquipmentId,
                            position: SwitchPosition) -> ChangeSet:
        state = self._operating_states.get(state_id)
        equipment = self._equipment.get(equipment_id)
        if state is None or equipment is None:
            raise DomainInvariantError("Режим и оборудование должны существовать.")
        definition = self.equipment_type(equipment.type_id, equipment.type_version)
        if "switch.position" not in definition.capabilities:
            raise DomainInvariantError(
                "Изменять OPEN/CLOSED можно только у реального коммутационного аппарата."
            )
        if not isinstance(position, SwitchPosition):
            try:
                position = SwitchPosition(position)
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Положение коммутационного аппарата должно быть OPEN или CLOSED."
                ) from exc
        positions = dict(state.positions)
        positions[equipment_id] = position
        self._operating_states[state_id] = replace(state, positions=positions)
        return self._changed(changed=(state_id, equipment_id))

    def set_equipment_availability(
        self,
        state_id: OperatingStateId,
        equipment_id: EquipmentId,
        availability: EquipmentAvailability,
    ) -> ChangeSet:
        """Set explicit service availability without changing switch position.

        The availability map is sparse.  An absent entry means ``IN_SERVICE``;
        this command records the caller's explicit state selection, including
        an explicit ``IN_SERVICE`` when requested.
        """
        state = self._operating_states.get(state_id)
        equipment = self._equipment.get(equipment_id)
        if state is None or equipment is None:
            raise DomainInvariantError("Режим и оборудование должны существовать.")
        definition = self.equipment_type(equipment.type_id, equipment.type_version)
        if EQUIPMENT_AVAILABILITY_CAPABILITY not in definition.capabilities:
            raise DomainInvariantError(
                "Эксплуатационная доступность применима только к оборудованию "
                "с capability 'equipment.availability'."
            )
        if not isinstance(availability, EquipmentAvailability):
            try:
                availability = EquipmentAvailability(availability)
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "Эксплуатационная доступность должна быть IN_SERVICE "
                    "или OUT_OF_SERVICE."
                ) from exc
        values = dict(state.availability)
        values[equipment_id] = availability
        self._operating_states[state_id] = replace(state, availability=values)
        return self._changed(changed=(state_id, equipment_id))

    def validate_integrity(self) -> list[DomainIssue]:
        issues: list[DomainIssue] = []
        values: dict[str, str] = {}
        for voltage_id in self._voltage_classes:
            values[voltage_id.value] = "voltage_class"
        for type_id in {key[0] for key in self._equipment_types}:
            previous = values.get(type_id.value)
            if previous:
                issues.append(DomainIssue(
                    "duplicate_id",
                    f"ID '{type_id.value}' используется как {previous} и equipment_type.",
                    type_id.value,
                ))
            values[type_id.value] = "equipment_type"
        for category, store in (
            ("equipment", self._equipment), ("port", self._ports),
            ("node", self._electrical_nodes), ("connection", self._connections),
            ("state", self._operating_states), ("logical_line", self._logical_lines),
        ):
            for object_id in store:
                previous = values.get(object_id.value)
                if previous:
                    issues.append(DomainIssue(
                        "duplicate_id",
                        f"ID '{object_id.value}' используется как {previous} и {category}.",
                        object_id.value,
                    ))
                values[object_id.value] = category
        for feeder_id in {
            line.feeder_id
            for line in self._logical_lines.values()
            if line.feeder_id is not None
        }:
            previous = values.get(feeder_id.value)
            if previous:
                issues.append(DomainIssue(
                    "duplicate_id",
                    f"ID '{feeder_id.value}' используется как {previous} и feeder.",
                    feeder_id.value,
                ))
            values[feeder_id.value] = "feeder"
        for section_id, section in self._line_sections.items():
            for segment in section.construction_segments:
                previous = values.get(segment.id.value)
                if previous:
                    issues.append(DomainIssue(
                        "duplicate_id",
                        f"ID '{segment.id.value}' используется как {previous} и "
                        "line_construction_segment.",
                        segment.id.value,
                    ))
                values[segment.id.value] = "line_construction_segment"

        for value, category in values.items():
            if value == "GRID":
                issues.append(DomainIssue(
                    "reserved_grid_id",
                    f"Зарезервированный ID 'GRID' используется как {category}.",
                    value,
                ))

        for definition in self._equipment_types.values():
            for port_definition in definition.port_definitions:
                for voltage_id in port_definition.allowed_voltage_class_ids or ():
                    if voltage_id not in self._voltage_classes:
                        issues.append(DomainIssue(
                            "unknown_allowed_voltage_class",
                            "Тип оборудования ссылается на неизвестный класс напряжения.",
                            definition.id.value,
                        ))

        for node in self._electrical_nodes.values():
            if (node.declared_voltage_class_id is not None
                    and node.declared_voltage_class_id not in self._voltage_classes):
                issues.append(DomainIssue(
                    "unknown_node_voltage_class",
                    "Узел ссылается на неизвестный класс напряжения.",
                    node.id.value,
                ))

        for equipment in self._equipment.values():
            definition = self._equipment_types.get(
                (equipment.type_id, equipment.type_version)
            )
            if definition is None:
                issues.append(DomainIssue(
                    "unknown_equipment_type",
                    "Экземпляр ссылается на незарегистрированную версию типа.",
                    equipment.id.value,
                ))
            for port_id in equipment.port_ids:
                port = self._ports.get(port_id)
                if port is None:
                    issues.append(DomainIssue(
                        "missing_equipment_port",
                        "Порт из equipment.port_ids отсутствует в хранилище портов.",
                        port_id.value,
                    ))
                elif port.equipment_id != equipment.id:
                    issues.append(DomainIssue(
                        "equipment_port_wrong_owner",
                        "Порт из equipment.port_ids принадлежит другому оборудованию.",
                        port_id.value,
                    ))
            for voltage_id in equipment.voltage_class_by_group.values():
                if voltage_id not in self._voltage_classes:
                    issues.append(DomainIssue(
                        "unknown_equipment_voltage_class",
                        "Оборудование ссылается на неизвестный класс напряжения.",
                        equipment.id.value,
                    ))
            if definition is not None:
                defined_roles = {item.role for item in definition.port_definitions}
                required_roles = {
                    item.role for item in definition.port_definitions if item.required
                }
                actual_roles = {
                    self._ports[port_id].role
                    for port_id in equipment.port_ids
                    if port_id in self._ports
                }
                if not required_roles <= actual_roles or not actual_roles <= defined_roles:
                    issues.append(DomainIssue(
                        "invalid_equipment_port_roles",
                        "Набор ролей портов не соответствует версии типа оборудования.",
                        equipment.id.value,
                    ))
                known_groups = {
                    item.voltage_group for item in definition.port_definitions
                    if item.voltage_group is not None
                }
                if set(equipment.voltage_class_by_group) - known_groups:
                    issues.append(DomainIssue(
                        "unknown_equipment_voltage_group",
                        "Оборудование использует неизвестную группу напряжения.",
                        equipment.id.value,
                    ))
                for port_definition in definition.port_definitions:
                    group = port_definition.voltage_group
                    assigned = (
                        equipment.voltage_class_by_group.get(group) if group else None
                    )
                    if (assigned is not None
                            and port_definition.allowed_voltage_class_ids is not None
                            and assigned not in port_definition.allowed_voltage_class_ids):
                        issues.append(DomainIssue(
                            "disallowed_equipment_voltage_class",
                            "Класс напряжения оборудования недопустим для порта.",
                            equipment.id.value,
                        ))
                switch_capable = bool(
                    {"switch.position", "legacy.switch.position"}
                    & definition.capabilities
                )
                if equipment.normal_position is not None and not switch_capable:
                    issues.append(DomainIssue(
                        "position_on_non_switch",
                        "Нормальное положение задано некоммутационному оборудованию.",
                        equipment.id.value,
                    ))
                if (
                    definition.behavior_key == "recloser"
                    and equipment.normal_position is None
                ):
                    issues.append(DomainIssue(
                        "recloser_without_normal_position",
                        "У реклоузера не задано нормальное положение OPEN/CLOSED.",
                        equipment.id.value,
                    ))
                try:
                    self.effective_equipment_properties(equipment.id)
                except DomainInvariantError as exc:
                    issues.append(DomainIssue(
                        "invalid_effective_properties",
                        str(exc),
                        equipment.id.value,
                    ))
        owner_roles: set[tuple[EquipmentId, str]] = set()
        for port in self._ports.values():
            equipment = self._equipment.get(port.equipment_id)
            if equipment is None:
                issues.append(DomainIssue(
                    "missing_port_owner", "Владелец порта не найден.", port.id.value
                ))
                continue
            if port.id not in equipment.port_ids:
                issues.append(DomainIssue(
                    "port_not_listed_by_owner", "Порт отсутствует в port_ids владельца.",
                    port.id.value,
                ))
            key = (port.equipment_id, port.role)
            if key in owner_roles:
                issues.append(DomainIssue(
                    "duplicate_port_role", "Роль порта повторяется у оборудования.",
                    port.id.value,
                ))
            owner_roles.add(key)
        connected_ports: set[PortId] = set()
        node_voltage_ids: dict[ElectricalNodeId, set[VoltageClassId]] = {
            node.id: ({node.declared_voltage_class_id}
                      if node.declared_voltage_class_id is not None else set())
            for node in self._electrical_nodes.values()
        }
        # These indexes belong only to this validation call. Rebuilding them
        # also sees malformed/imported stores changed without a new revision;
        # a persistent revision cache would hide such changes. Preserve source
        # order and the first connection for duplicate-port diagnostics.
        connections_by_node: dict[ElectricalNodeId, list[Connection]] = {}
        connections_by_equipment: dict[EquipmentId, list[Connection]] = {}
        first_connection_by_port: dict[PortId, Connection] = {}
        for connection in self._connections.values():
            connections_by_node.setdefault(connection.electrical_node_id, []).append(connection)
            first_connection_by_port.setdefault(connection.port_id, connection)
            port = self._ports.get(connection.port_id)
            if port is not None:
                connections_by_equipment.setdefault(port.equipment_id, []).append(connection)
        for connection in self._connections.values():
            port = self._ports.get(connection.port_id)
            node = self._electrical_nodes.get(connection.electrical_node_id)
            if port is None:
                issues.append(DomainIssue(
                    "dangling_connection_port", "Порт connection не найден.",
                    connection.id.value,
                ))
            if node is None:
                issues.append(DomainIssue(
                    "dangling_connection_node", "Узел connection не найден.",
                    connection.id.value,
                ))
            if port is not None and node is not None:
                try:
                    self._check_connection_compatibility(
                        port, node,
                        _connections_by_node=connections_by_node,
                        _connections_by_equipment=connections_by_equipment,
                    )
                except (DomainInvariantError, KeyError, StopIteration):
                    issues.append(DomainIssue(
                        "incompatible_connection",
                        "Connection нарушает ограничения типа порта или напряжения.",
                        connection.id.value,
                    ))
                try:
                    voltage_id = self.port_voltage_class(port.id)
                except (DomainInvariantError, KeyError, StopIteration):
                    voltage_id = None
                if voltage_id is not None:
                    node_voltage_ids[node.id].add(voltage_id)
            if connection.port_id in connected_ports:
                issues.append(DomainIssue(
                    "port_connected_twice", "Порт подключён более одного раза.",
                    connection.port_id.value,
                ))
            connected_ports.add(connection.port_id)
        for node_id, voltage_ids in node_voltage_ids.items():
            if len(voltage_ids) > 1:
                issues.append(DomainIssue(
                    "mixed_node_voltage_classes",
                    "К одному узлу подключены порты разных классов напряжения.",
                    node_id.value,
                ))
        for equipment in self._equipment.values():
            if len(equipment.port_ids) < 2:
                continue
            endpoint_nodes = [
                connection.electrical_node_id
                for port_id in equipment.port_ids
                if (connection := first_connection_by_port.get(port_id)) is not None
            ]
            if len(endpoint_nodes) != len(set(endpoint_nodes)):
                issues.append(DomainIssue(
                    "equipment_terminals_same_node",
                    "Разные электрические порты одного оборудования подключены "
                    "к одному узлу.",
                    equipment.id.value,
                ))
        for state in self._operating_states.values():
            for equipment_id in state.positions:
                equipment = self._equipment.get(equipment_id)
                if equipment is None:
                    issues.append(DomainIssue(
                        "dangling_state_equipment", "Состояние ссылается на удалённый аппарат.",
                        state.id.value,
                    ))
                    continue
                definition = self._equipment_types.get(
                    (equipment.type_id, equipment.type_version)
                )
                if (definition is None or not (
                    {"switch.position", "legacy.switch.position"}
                    & definition.capabilities
                )):
                    issues.append(DomainIssue(
                        "state_on_non_switch",
                        "Состояние OPEN/CLOSED задано некоммутационному оборудованию.",
                        state.id.value,
                    ))
            for equipment_id in state.availability:
                equipment = self._equipment.get(equipment_id)
                if equipment is None:
                    issues.append(DomainIssue(
                        "dangling_availability_equipment",
                        "Эксплуатационная доступность ссылается на удалённое оборудование.",
                        state.id.value,
                    ))
                    continue
                definition = self._equipment_types.get(
                    (equipment.type_id, equipment.type_version)
                )
                if (
                    definition is None
                    or EQUIPMENT_AVAILABILITY_CAPABILITY not in definition.capabilities
                ):
                    issues.append(DomainIssue(
                        "availability_on_unsupported_equipment",
                        "Эксплуатационная доступность задана неподдерживаемому оборудованию.",
                        state.id.value,
                    ))
        referenced_sections: set[EquipmentId] = set()
        for line_id, line in self._logical_lines.items():
            referenced_sections.update(line.section_equipment_ids)
            try:
                self._validate_logical_line_records_for_existing(line_id)
            except (DomainInvariantError, KeyError, StopIteration) as exc:
                issues.append(DomainIssue(
                    "invalid_logical_line",
                    str(exc) or "Логическая линия содержит некорректные участки.",
                    line.id.value,
                ))
        for section_id, section in self._line_sections.items():
            if section_id not in referenced_sections:
                issues.append(DomainIssue(
                    "orphan_line_section",
                    "Запись физического участка не включена в логическую линию.",
                    section_id.value,
                ))
        for equipment_id, equipment in self._equipment.items():
            definition = self._equipment_types.get(
                (equipment.type_id, equipment.type_version)
            )
            if (
                definition is not None
                and definition.behavior_key == "line_section"
                and equipment_id not in self._line_sections
            ):
                issues.append(DomainIssue(
                    "unowned_line_section_equipment",
                    "Оборудование участка не включено ни в одну логическую линию.",
                    equipment_id.value,
                ))
        return issues

    def connectivity_signature(self) -> tuple[tuple[str, str], ...]:
        """Сигнатура связности только по ID; имена намеренно отсутствуют."""
        return tuple(sorted(
            (item.port_id.value, item.electrical_node_id.value)
            for item in self._connections.values()
        ))
