# -*- coding: utf-8 -*-
"""Изолированная foundation-модель системного и пользовательского каталогов.

Каталог не владеет ``ElectricalModel`` и не изменяет его. Запись только
материализует независимые параметры либо ``EquipmentInstance``; добавление
экземпляра и его портов остаётся ответственностью агрегата электрической
модели.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping

from .electrical import (
    DomainInvariantError,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeId,
    PortId,
    StableId,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)


class CatalogEntryId(StableId):
    prefix = "catalog"


class CatalogId(StableId):
    """Постоянный ID корня каталога, независимый от его записей."""

    prefix = "catalog_root"


class CatalogCategoryId(StableId):
    prefix = "category"


RECLOSERS = CatalogCategoryId("reclosers")


class CatalogOrigin(StrEnum):
    SYSTEM = "system"
    USER = "user"


# Эти значения являются частью формата данных: старые JSON без ID получают
# один и тот же детерминированный идентификатор при каждом чтении.
DEFAULT_SYSTEM_CATALOG_ID = CatalogId("catalog.system.default")
DEFAULT_USER_CATALOG_ID = CatalogId("catalog.user.default")
DEFAULT_PROJECT_CATALOG_ID = CatalogId("catalog.project.snapshots")


def _require_string(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        expected = "строкой" if allow_empty else "непустой строкой"
        raise DomainInvariantError(f"{field_name} должен быть {expected}.")


def _require_positive_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DomainInvariantError(f"{field_name} должен быть целым числом ≥ 1.")


def _require_mapping(value: Any, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise DomainInvariantError(f"{field_name} должен быть объектом ключ-значение.")


def _freeze_json(value: Any) -> Any:
    """Глубокая immutable-копия JSON-совместимого значения."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainInvariantError("NaN и бесконечность нельзя хранить в каталоге.")
        return value
    if isinstance(value, StableId):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DomainInvariantError(
                    "Ключ JSON-объекта каталога должен быть непустой строкой."
                )
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise DomainInvariantError(
        f"Значение типа {type(value).__name__} нельзя хранить в каталоге."
    )


def _normal_position(value: Any, field_name: str) -> SwitchPosition | None:
    if value is None or isinstance(value, SwitchPosition):
        return value
    try:
        return SwitchPosition(value)
    except (TypeError, ValueError) as exc:
        raise DomainInvariantError(
            f"{field_name} должен быть OPEN, CLOSED или null."
        ) from exc


def _voltage_mapping(
    value: Mapping[str, VoltageClassId],
    field_name: str,
) -> Mapping[str, VoltageClassId]:
    _require_mapping(value, field_name)
    result: dict[str, VoltageClassId] = {}
    for group, voltage_id in value.items():
        if not isinstance(group, str) or not group.strip():
            raise DomainInvariantError(
                f"{field_name}: группа должна быть непустой строкой."
            )
        if not isinstance(voltage_id, VoltageClassId):
            raise DomainInvariantError(
                f"{field_name}: класс напряжения задаётся VoltageClassId."
            )
        result[group] = voltage_id
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class EquipmentInstanceParameters:
    """Материализованные параметры для ``ElectricalModel.create_equipment``."""

    type_id: EquipmentTypeId
    type_version: int
    properties: Mapping[str, Any] = field(default_factory=dict)
    voltage_class_by_group: Mapping[str, VoltageClassId] = field(default_factory=dict)
    normal_position: SwitchPosition | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type_id, EquipmentTypeId):
            raise DomainInvariantError(
                "EquipmentInstanceParameters.type_id должен быть EquipmentTypeId."
            )
        _require_positive_int(self.type_version, "EquipmentInstanceParameters.type_version")
        _require_mapping(self.properties, "EquipmentInstanceParameters.properties")
        _require_mapping(self.extensions, "EquipmentInstanceParameters.extensions")
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        object.__setattr__(
            self,
            "voltage_class_by_group",
            _voltage_mapping(
                self.voltage_class_by_group,
                "EquipmentInstanceParameters.voltage_class_by_group",
            ),
        )
        object.__setattr__(
            self,
            "normal_position",
            _normal_position(
                self.normal_position,
                "EquipmentInstanceParameters.normal_position",
            ),
        )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))

    def as_create_kwargs(self) -> dict[str, Any]:
        """Обычные dict/list, пригодные для передачи в create_equipment()."""
        return {
            "type_version": self.type_version,
            "properties": thaw_json(self.properties),
            "voltage_class_by_group": dict(self.voltage_class_by_group),
            "normal_position": self.normal_position,
            "extensions": thaw_json(self.extensions),
        }


_UNSET = object()


@dataclass(frozen=True, slots=True)
class CatalogEntry:
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

    def __post_init__(self) -> None:
        if not isinstance(self.id, CatalogEntryId):
            raise DomainInvariantError("CatalogEntry.id должен быть CatalogEntryId.")
        if not isinstance(self.origin, CatalogOrigin):
            try:
                object.__setattr__(self, "origin", CatalogOrigin(self.origin))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError(
                    "CatalogEntry.origin должен быть system или user."
                ) from exc
        if not isinstance(self.category_id, CatalogCategoryId):
            raise DomainInvariantError(
                "CatalogEntry.category_id должен быть CatalogCategoryId."
            )
        if not isinstance(self.equipment_type_id, EquipmentTypeId):
            raise DomainInvariantError(
                "CatalogEntry.equipment_type_id должен быть EquipmentTypeId."
            )
        _require_positive_int(
            self.equipment_type_version,
            "CatalogEntry.equipment_type_version",
        )
        _require_positive_int(self.entry_version, "CatalogEntry.entry_version")
        _require_string(self.display_name, "CatalogEntry.display_name", allow_empty=False)
        _require_string(self.manufacturer, "CatalogEntry.manufacturer")
        _require_string(self.model, "CatalogEntry.model")
        _require_string(self.source, "CatalogEntry.source", allow_empty=False)
        _require_string(self.description, "CatalogEntry.description")
        _require_mapping(self.properties, "CatalogEntry.properties")
        _require_mapping(self.extensions, "CatalogEntry.extensions")
        if not isinstance(self.modified_at, datetime):
            raise DomainInvariantError("CatalogEntry.modified_at должен быть datetime.")
        if self.modified_at.tzinfo is None or self.modified_at.utcoffset() is None:
            raise DomainInvariantError(
                "CatalogEntry.modified_at должен содержать часовой пояс."
            )
        object.__setattr__(self, "properties", _freeze_json(self.properties))
        object.__setattr__(
            self,
            "voltage_class_by_group",
            _voltage_mapping(
                self.voltage_class_by_group,
                "CatalogEntry.voltage_class_by_group",
            ),
        )
        object.__setattr__(
            self,
            "normal_position",
            _normal_position(self.normal_position, "CatalogEntry.normal_position"),
        )
        object.__setattr__(
            self,
            "modified_at",
            self.modified_at.astimezone(timezone.utc),
        )
        object.__setattr__(self, "extensions", _freeze_json(self.extensions))

    def instance_parameters(
        self,
        *,
        property_overrides: Mapping[str, Any] | None = None,
        voltage_overrides: Mapping[str, VoltageClassId] | None = None,
        normal_position: Any = _UNSET,
        instance_extensions: Mapping[str, Any] | None = None,
    ) -> EquipmentInstanceParameters:
        """Материализовать копию записи, не изменяя каталог.

        Overrides применяются поверх базовых значений на верхнем уровне.
        Вложенный JSON-объект заменяется целиком, что исключает скрытое
        частичное наследование.
        """
        if property_overrides is not None:
            _require_mapping(property_overrides, "property_overrides")
        if voltage_overrides is not None:
            _require_mapping(voltage_overrides, "voltage_overrides")
        if instance_extensions is not None:
            _require_mapping(instance_extensions, "instance_extensions")

        properties = thaw_json(self.properties)
        if self.category_id == RECLOSERS:
            properties["manufacturer"] = self.manufacturer
            properties["model"] = self.model
        properties.update(thaw_json(_freeze_json(property_overrides or {})))
        voltages = dict(self.voltage_class_by_group)
        voltages.update(voltage_overrides or {})
        extensions = thaw_json(_freeze_json(instance_extensions or {}))
        # Provenance is authoritative and cannot be spoofed by an override.
        extensions["catalog"] = {
            "entry_id": self.id.value,
            "origin": self.origin.value,
            "entry_version": self.entry_version,
        }
        position = (
            self.normal_position
            if normal_position is _UNSET
            else _normal_position(normal_position, "normal_position")
        )
        return EquipmentInstanceParameters(
            self.equipment_type_id,
            self.equipment_type_version,
            properties,
            voltages,
            position,
            extensions,
        )

    def create_equipment_instance(
        self,
        equipment_id: EquipmentId,
        port_ids: Iterable[PortId],
        *,
        name: str | None = None,
        property_overrides: Mapping[str, Any] | None = None,
        voltage_overrides: Mapping[str, VoltageClassId] | None = None,
        normal_position: Any = _UNSET,
        note: str = "",
        instance_extensions: Mapping[str, Any] | None = None,
    ) -> EquipmentInstance:
        """Создать независимый EquipmentInstance без добавления в модель."""
        params = self.instance_parameters(
            property_overrides=property_overrides,
            voltage_overrides=voltage_overrides,
            normal_position=normal_position,
            instance_extensions=instance_extensions,
        )
        return EquipmentInstance(
            equipment_id,
            params.type_id,
            params.type_version,
            self.display_name if name is None else name,
            tuple(port_ids),
            params.properties,
            params.voltage_class_by_group,
            params.normal_position,
            note,
            params.extensions,
        )


def _catalog_entries(
    entries: Iterable[CatalogEntry],
    origin: CatalogOrigin,
) -> dict[CatalogEntryId, CatalogEntry]:
    result: dict[CatalogEntryId, CatalogEntry] = {}
    for entry in entries:
        if not isinstance(entry, CatalogEntry):
            raise DomainInvariantError("Каталог может содержать только CatalogEntry.")
        if entry.origin is not origin:
            raise DomainInvariantError(
                f"Каталог {origin.value} не принимает запись {entry.origin.value}."
            )
        if entry.id in result:
            raise DomainInvariantError(
                f"Запись каталога '{entry.id}' указана повторно."
            )
        result[entry.id] = entry
    return result


def _entry_id(value: CatalogEntryId | str) -> CatalogEntryId:
    return value if isinstance(value, CatalogEntryId) else CatalogEntryId(value)


def _category_id(value: CatalogCategoryId | str) -> CatalogCategoryId:
    return value if isinstance(value, CatalogCategoryId) else CatalogCategoryId(value)


@dataclass(frozen=True, slots=True, init=False)
class SystemCatalog:
    """Полностью immutable системный каталог."""

    _id: CatalogId
    _entries: Mapping[CatalogEntryId, CatalogEntry]

    def __init__(
        self,
        entries: Iterable[CatalogEntry] = (),
        *,
        catalog_id: CatalogId = DEFAULT_SYSTEM_CATALOG_ID,
    ) -> None:
        if not isinstance(catalog_id, CatalogId):
            raise DomainInvariantError("SystemCatalog.catalog_id должен быть CatalogId.")
        object.__setattr__(self, "_id", catalog_id)
        object.__setattr__(
            self,
            "_entries",
            MappingProxyType(_catalog_entries(entries, CatalogOrigin.SYSTEM)),
        )

    @property
    def id(self) -> CatalogId:
        return self._id

    @property
    def entries(self) -> Mapping[CatalogEntryId, CatalogEntry]:
        return self._entries

    def get(self, entry_id: CatalogEntryId | str) -> CatalogEntry:
        key = _entry_id(entry_id)
        try:
            return self._entries[key]
        except KeyError as exc:
            raise DomainInvariantError(
                f"Системная запись каталога '{key}' не найдена."
            ) from exc

    def by_category(
        self, category_id: CatalogCategoryId | str
    ) -> tuple[CatalogEntry, ...]:
        key = _category_id(category_id)
        return tuple(item for item in self._entries.values() if item.category_id == key)

    def __iter__(self) -> Iterator[CatalogEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


class UserCatalog:
    """Mutable registry of immutable user entries."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_id" and hasattr(self, "_id"):
            raise AttributeError("Постоянный ID пользовательского каталога неизменяем.")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        entries: Iterable[CatalogEntry] = (),
        *,
        catalog_id: CatalogId = DEFAULT_USER_CATALOG_ID,
    ) -> None:
        if not isinstance(catalog_id, CatalogId):
            raise DomainInvariantError("UserCatalog.catalog_id должен быть CatalogId.")
        self._id = catalog_id
        self._entries = _catalog_entries(entries, CatalogOrigin.USER)

    @property
    def id(self) -> CatalogId:
        return self._id

    @property
    def entries(self) -> Mapping[CatalogEntryId, CatalogEntry]:
        return MappingProxyType(self._entries)

    def get(self, entry_id: CatalogEntryId | str) -> CatalogEntry:
        key = _entry_id(entry_id)
        try:
            return self._entries[key]
        except KeyError as exc:
            raise DomainInvariantError(
                f"Пользовательская запись каталога '{key}' не найдена."
            ) from exc

    def by_category(
        self, category_id: CatalogCategoryId | str
    ) -> tuple[CatalogEntry, ...]:
        key = _category_id(category_id)
        return tuple(item for item in self._entries.values() if item.category_id == key)

    def add(self, entry: CatalogEntry) -> CatalogEntry:
        rows = _catalog_entries((entry,), CatalogOrigin.USER)
        if entry.id in self._entries:
            raise DomainInvariantError(
                f"Пользовательская запись каталога '{entry.id}' уже существует."
            )
        self._entries.update(rows)
        return entry

    def replace(self, entry: CatalogEntry) -> CatalogEntry:
        _catalog_entries((entry,), CatalogOrigin.USER)
        if entry.id not in self._entries:
            raise DomainInvariantError(
                f"Пользовательская запись каталога '{entry.id}' не найдена."
            )
        self._entries[entry.id] = entry
        return entry

    def remove(self, entry_id: CatalogEntryId | str) -> CatalogEntry:
        key = _entry_id(entry_id)
        try:
            return self._entries.pop(key)
        except KeyError as exc:
            raise DomainInvariantError(
                f"Пользовательская запись каталога '{key}' не найдена."
            ) from exc

    def save_equipment_instance(
        self,
        equipment: EquipmentInstance,
        *,
        category_id: CatalogCategoryId | str,
        entry_id: CatalogEntryId | None = None,
        display_name: str | None = None,
        manufacturer: str = "",
        model: str = "",
        source: str = "Пользовательская запись",
        description: str | None = None,
        modified_at: datetime | None = None,
        entry_version: int = 1,
        extensions: Mapping[str, Any] | None = None,
    ) -> CatalogEntry:
        """Сохранить материализованные параметры экземпляра как новый шаблон."""
        if not isinstance(equipment, EquipmentInstance):
            raise DomainInvariantError(
                "save_equipment_instance ожидает EquipmentInstance."
            )
        entry = CatalogEntry(
            entry_id or CatalogEntryId.new(),
            CatalogOrigin.USER,
            _category_id(category_id),
            equipment.name if display_name is None else display_name,
            equipment.type_id,
            equipment.type_version,
            manufacturer,
            model,
            thaw_json(equipment.properties),
            dict(equipment.voltage_class_by_group),
            equipment.normal_position,
            source,
            equipment.note if description is None else description,
            modified_at or datetime.now(timezone.utc),
            entry_version,
            extensions or {},
        )
        return self.add(entry)

    def __iter__(self) -> Iterator[CatalogEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


_BUILTIN_RECLOSER = CatalogEntry(
    CatalogEntryId("system.recloser.generic_10kv"),
    CatalogOrigin.SYSTEM,
    RECLOSERS,
    "Типовой реклоузер 10 кВ",
    EquipmentTypeId("builtin.recloser"),
    1,
    manufacturer="",
    model="GENERIC-10",
    properties={
        "manufacturer": "",
        "model": "GENERIC-10",
        "rated_voltage_v": 10_000,
        "rated_current_a": 630,
        "rated_breaking_current_a": 12_500,
        "short_circuit_limit_a": 31_500,
        "auto_reclose_settings": {"cycles": 3, "times_s": []},
    },
    voltage_class_by_group={
        "main": VoltageClassId("builtin.voltage.ac.10kv"),
    },
    normal_position=SwitchPosition.CLOSED,
    source="RZA Calc: типовая системная запись; параметры требуют сверки по паспорту",
    description="Нейтральный шаблон реклоузера без привязки к производителю.",
    modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    extensions={"system_seed": True},
)

_SYSTEM_CATALOG = SystemCatalog((_BUILTIN_RECLOSER,))


def builtin_system_catalog() -> SystemCatalog:
    """Вернуть общий immutable системный каталог."""
    return _SYSTEM_CATALOG


__all__ = [
    "CatalogCategoryId",
    "CatalogEntry",
    "CatalogEntryId",
    "CatalogId",
    "CatalogOrigin",
    "DEFAULT_PROJECT_CATALOG_ID",
    "DEFAULT_SYSTEM_CATALOG_ID",
    "DEFAULT_USER_CATALOG_ID",
    "EquipmentInstanceParameters",
    "RECLOSERS",
    "SystemCatalog",
    "UserCatalog",
    "builtin_system_catalog",
]
