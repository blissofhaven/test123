# -*- coding: utf-8 -*-
"""Неизменяемые снимки каталожных данных внутри проекта.

Запись каталога копируется целиком в момент привязки. Позднейшее
изменение системного или пользовательского каталога не меняет
уже созданный проект.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .catalog import (
    CatalogCategoryId,
    CatalogEntry,
    CatalogEntryId,
    CatalogId,
    CatalogOrigin,
    DEFAULT_PROJECT_CATALOG_ID,
    DEFAULT_SYSTEM_CATALOG_ID,
    DEFAULT_USER_CATALOG_ID,
)
from .electrical import (
    DomainInvariantError,
    ElectricalModel,
    EquipmentId,
    EquipmentTypeId,
    StableId,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainInvariantError("NaN и Infinity нельзя хранить в каталожном снимке.")
        return value
    if isinstance(value, StableId):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DomainInvariantError("Ключ JSON-объекта должен быть непустой строкой.")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    raise DomainInvariantError(
        f"Тип {type(value).__name__} нельзя хранить в каталожном снимке."
    )


def _freeze_voltage_map(
    value: Mapping[str, VoltageClassId], context: str
) -> Mapping[str, VoltageClassId]:
    result: dict[str, VoltageClassId] = {}
    for key, voltage_id in value.items():
        if not isinstance(key, str) or not key.strip():
            raise DomainInvariantError(f"{context}: группа должна быть непустой строкой.")
        if not isinstance(voltage_id, VoltageClassId):
            raise DomainInvariantError(f"{context}: ожидался VoltageClassId.")
        result[key] = voltage_id
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True, slots=True)
class ParameterOverride:
    """Явное аудируемое переопределение расчётного параметра."""

    parameter_key: str
    source_value: Any
    override_value: Any
    reason: str
    source: str
    changed_at: datetime
    manual: bool = True

    def __post_init__(self) -> None:
        for name in ("parameter_key", "reason", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainInvariantError(f"ParameterOverride.{name} должен быть непустой строкой.")
        if not isinstance(self.changed_at, datetime) or self.changed_at.tzinfo is None or self.changed_at.utcoffset() is None:
            raise DomainInvariantError("ParameterOverride.changed_at должен содержать часовой пояс.")
        if not isinstance(self.manual, bool):
            raise DomainInvariantError("ParameterOverride.manual должен быть bool.")
        object.__setattr__(self, "source_value", _freeze_json(self.source_value))
        object.__setattr__(self, "override_value", _freeze_json(self.override_value))
        object.__setattr__(self, "changed_at", self.changed_at.astimezone(timezone.utc))


@dataclass(frozen=True, slots=True)
class CatalogEntrySnapshot:
    """Полная копия одной версии записи каталога."""

    id: CatalogEntryId
    origin: CatalogOrigin
    category_id: CatalogCategoryId
    display_name: str
    equipment_type_id: EquipmentTypeId
    equipment_type_version: int
    manufacturer: str = ""
    model: str = ""
    properties: Mapping[str, Any] = field(default_factory=dict)
    voltage_class_by_group: Mapping[str, VoltageClassId] = field(default_factory=dict)
    normal_position: SwitchPosition | None = None
    source: str = ""
    description: str = ""
    modified_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_version: int = 1
    extensions: Mapping[str, Any] = field(default_factory=dict)
    source_catalog_id: CatalogId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, CatalogEntryId):
            raise DomainInvariantError("CatalogEntrySnapshot.id должен быть CatalogEntryId.")
        if not isinstance(self.origin, CatalogOrigin):
            object.__setattr__(self, "origin", CatalogOrigin(self.origin))
        if self.source_catalog_id is None:
            object.__setattr__(
                self,
                "source_catalog_id",
                DEFAULT_SYSTEM_CATALOG_ID
                if self.origin is CatalogOrigin.SYSTEM
                else DEFAULT_USER_CATALOG_ID,
            )
        elif not isinstance(self.source_catalog_id, CatalogId):
            raise DomainInvariantError(
                "CatalogEntrySnapshot.source_catalog_id должен быть CatalogId."
            )
        if not isinstance(self.category_id, CatalogCategoryId):
            raise DomainInvariantError("CatalogEntrySnapshot.category_id должен быть CatalogCategoryId.")
        if not isinstance(self.equipment_type_id, EquipmentTypeId):
            raise DomainInvariantError("CatalogEntrySnapshot.equipment_type_id должен быть EquipmentTypeId.")
        if isinstance(self.equipment_type_version, bool) or not isinstance(self.equipment_type_version, int) or self.equipment_type_version < 1:
            raise DomainInvariantError("equipment_type_version должна быть целой и положительной.")
        if isinstance(self.entry_version, bool) or not isinstance(self.entry_version, int) or self.entry_version < 1:
            raise DomainInvariantError("entry_version должна быть целой и положительной.")
        for name in ("display_name", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainInvariantError(f"CatalogEntrySnapshot.{name} должен быть непустой строкой.")
        for name in ("manufacturer", "model", "description"):
            if not isinstance(getattr(self, name), str):
                raise DomainInvariantError(f"CatalogEntrySnapshot.{name} должен быть строкой.")
        if not isinstance(self.modified_at, datetime) or self.modified_at.tzinfo is None or self.modified_at.utcoffset() is None:
            raise DomainInvariantError("CatalogEntrySnapshot.modified_at должен содержать часовой пояс.")
        if self.normal_position is not None and not isinstance(self.normal_position, SwitchPosition):
            object.__setattr__(self, "normal_position", SwitchPosition(self.normal_position))
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))
        object.__setattr__(self, "voltage_class_by_group", _freeze_voltage_map(self.voltage_class_by_group, "CatalogEntrySnapshot.voltage_class_by_group"))
        object.__setattr__(self, "modified_at", self.modified_at.astimezone(timezone.utc))

    @classmethod
    def from_entry(
        cls,
        entry: CatalogEntry,
        *,
        source_catalog_id: CatalogId | None = None,
    ) -> "CatalogEntrySnapshot":
        if not isinstance(entry, CatalogEntry):
            raise TypeError("CatalogEntrySnapshot.from_entry ожидает CatalogEntry.")
        return cls(
            entry.id,
            entry.origin,
            entry.category_id,
            entry.display_name,
            entry.equipment_type_id,
            entry.equipment_type_version,
            entry.manufacturer,
            entry.model,
            thaw_json(entry.properties),
            dict(entry.voltage_class_by_group),
            entry.normal_position,
            entry.source,
            entry.description,
            entry.modified_at,
            entry.entry_version,
            thaw_json(entry.extensions),
            source_catalog_id,
        )


@dataclass(frozen=True, slots=True)
class CatalogBinding:
    """Привязка экземпляра к неизменяемому снимку каталога."""

    equipment_id: EquipmentId
    entry: CatalogEntrySnapshot
    instance_overrides: Mapping[str, Any] = field(default_factory=dict)
    calculated_values: Mapping[str, Any] = field(default_factory=dict)
    parameter_overrides: Mapping[str, ParameterOverride] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.equipment_id, EquipmentId):
            raise DomainInvariantError("CatalogBinding.equipment_id должен быть EquipmentId.")
        if not isinstance(self.entry, CatalogEntrySnapshot):
            raise DomainInvariantError("CatalogBinding.entry должен быть CatalogEntrySnapshot.")
        object.__setattr__(self, "instance_overrides", _freeze_json(self.instance_overrides))
        object.__setattr__(self, "calculated_values", _freeze_json(self.calculated_values))
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))
        overrides: dict[str, ParameterOverride] = {}
        for key, value in self.parameter_overrides.items():
            if not isinstance(key, str) or not key or not isinstance(value, ParameterOverride):
                raise DomainInvariantError("CatalogBinding.parameter_overrides имеет неверный тип.")
            if key != value.parameter_key:
                raise DomainInvariantError("ParameterOverride не совпадает с ключом binding.")
            overrides[key] = value
        object.__setattr__(self, "parameter_overrides", MappingProxyType(dict(sorted(overrides.items()))))

    @property
    def effective_properties(self) -> Mapping[str, Any]:
        if self.extensions.get("parameter_schema") == "v1":
            from .catalog_compatibility import catalog_properties_for_type, apply_catalog_parameter_overrides
            codec = self.extensions.get("parameter_codec")
            target = str(self.entry.equipment_type_id)
            if codec:
                prefix = f"rza.parameters.v1:{self.entry.equipment_type_id}->"
                if not isinstance(codec, str) or not codec.startswith(prefix):
                    raise DomainInvariantError("Неверный код преобразования каталожного снимка.")
                target = codec[len(prefix):]
            values = catalog_properties_for_type(self.entry, target)
            values.update(thaw_json(self.instance_overrides))
            return _freeze_json(apply_catalog_parameter_overrides(values, target, self.parameter_overrides))
        values = thaw_json(self.entry.properties)
        values.update(thaw_json(self.instance_overrides))
        values.update({
            key: thaw_json(value.override_value)
            for key, value in self.parameter_overrides.items()
        })
        return _freeze_json(values)

    @property
    def source_catalog_id(self) -> CatalogId:
        """Источник snapshot без дублирования provenance в binding."""

        value = self.entry.source_catalog_id
        if not isinstance(value, CatalogId):  # защищает и запуск с ``python -O``
            raise DomainInvariantError(
                "CatalogBinding.entry не содержит постоянный ID каталога."
            )
        return value


@dataclass(frozen=True, slots=True)
class ProjectCatalogSnapshots:
    """Неизменяемый набор каталожных привязок одного проекта."""

    bindings: Mapping[EquipmentId, CatalogBinding] = field(default_factory=dict)
    schema_version: int = 1
    id: CatalogId = DEFAULT_PROJECT_CATALOG_ID

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise DomainInvariantError("Поддерживается только ProjectCatalogSnapshots v1.")
        if not isinstance(self.id, CatalogId):
            raise DomainInvariantError("ProjectCatalogSnapshots.id должен быть CatalogId.")
        values: dict[EquipmentId, CatalogBinding] = {}
        for equipment_id, binding in self.bindings.items():
            if not isinstance(equipment_id, EquipmentId) or not isinstance(binding, CatalogBinding):
                raise DomainInvariantError("ProjectCatalogSnapshots.bindings имеет неверный тип.")
            if equipment_id != binding.equipment_id:
                raise DomainInvariantError("CatalogBinding не совпадает с ключом equipment_id.")
            values[equipment_id] = binding
        object.__setattr__(self, "bindings", MappingProxyType(dict(sorted(values.items(), key=lambda item: item[0].value))))

    @classmethod
    def from_bindings(
        cls,
        bindings: Iterable[CatalogBinding],
        *,
        catalog_id: CatalogId = DEFAULT_PROJECT_CATALOG_ID,
    ) -> "ProjectCatalogSnapshots":
        values: dict[EquipmentId, CatalogBinding] = {}
        for binding in bindings:
            if binding.equipment_id in values:
                raise DomainInvariantError(f"Оборудование '{binding.equipment_id}' привязано дважды.")
            values[binding.equipment_id] = binding
        return cls(values, id=catalog_id)

    def validate_targets(self, model: ElectricalModel) -> tuple[str, ...]:
        problems: list[str] = []
        for equipment_id, binding in self.bindings.items():
            equipment = model.equipment.get(equipment_id)
            if equipment is None:
                problems.append(f"Каталожный снимок ссылается на удалённое оборудование '{equipment_id}'.")
                continue
            if binding.extensions.get("parameter_schema") == "v1":
                from .catalog_compatibility import catalog_codec
                try:
                    expected = catalog_codec(model, equipment, binding.entry)
                    if binding.extensions.get("parameter_codec") != expected:
                        raise DomainInvariantError("Код преобразования не соответствует типам марки и аппарата.")
                except DomainInvariantError as exc:
                    problems.append(f"Оборудование '{equipment_id}': {exc}")
                continue
            if (
                equipment.type_id != binding.entry.equipment_type_id
                or equipment.type_version != binding.entry.equipment_type_version
            ):
                problems.append(f"Оборудование '{equipment_id}' не совпадает с типом каталожного снимка.")
        return tuple(problems)

    def with_binding(self, binding: CatalogBinding) -> "ProjectCatalogSnapshots":
        values = dict(self.bindings)
        values[binding.equipment_id] = binding
        return replace(self, bindings=values)

    def without_equipment(self, equipment_id: EquipmentId) -> "ProjectCatalogSnapshots":
        if equipment_id not in self.bindings:
            return self
        values = dict(self.bindings)
        del values[equipment_id]
        return replace(self, bindings=values)


__all__ = [
    "CatalogBinding",
    "CatalogEntrySnapshot",
    "ParameterOverride",
    "ProjectCatalogSnapshots",
]
