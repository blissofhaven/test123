# -*- coding: utf-8 -*-
"""Строгий JSON codec каталожных снимков проекта v1."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.catalog import (
    CatalogCategoryId,
    CatalogEntryId,
    CatalogId,
    CatalogOrigin,
    DEFAULT_PROJECT_CATALOG_ID,
)
from ..domain.catalog_snapshot import (
    CatalogBinding,
    CatalogEntrySnapshot,
    ParameterOverride,
    ProjectCatalogSnapshots,
)
from ..domain.electrical import (
    ElectricalModel,
    EquipmentId,
    EquipmentTypeId,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)


CATALOG_SNAPSHOTS_FORMAT_VERSION = 1


class CatalogSnapshotsFormatError(ValueError):
    """JSON каталожных снимков не соответствует схеме v1."""


_ENTRY_FIELDS = {
    "id", "origin", "category_id", "display_name", "equipment_type_id",
    "equipment_type_version", "manufacturer", "model", "properties",
    "voltage_class_by_group", "normal_position", "source", "description",
    "modified_at", "entry_version", "extensions", "source_catalog_id",
}
_BINDING_FIELDS = {
    "equipment_id", "entry", "instance_overrides", "calculated_values",
    "parameter_overrides", "extensions",
}
_OVERRIDE_FIELDS = {
    "parameter_key", "source_value", "override_value", "reason", "source",
    "changed_at", "manual",
}


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogSnapshotsFormatError(f"{context}: ожидался объект JSON.")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise CatalogSnapshotsFormatError(f"{context}: ожидался массив JSON.")
    return value


def _strict(
    value: Any,
    fields: set[str],
    context: str,
    *,
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    row = _object(value, context)
    unknown = set(row) - fields
    missing = fields - set(optional) - set(row)
    if unknown:
        raise CatalogSnapshotsFormatError(f"{context}: неизвестные поля {sorted(unknown)}.")
    if missing:
        raise CatalogSnapshotsFormatError(f"{context}: отсутствуют поля {sorted(missing)}.")
    return row


def _string(value: Any, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise CatalogSnapshotsFormatError(f"{context}: ожидалась {'строка' if empty else 'непустая строка'}.")
    return value


def _integer(value: Any, context: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CatalogSnapshotsFormatError(f"{context}: ожидалось целое число не меньше {minimum}.")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise CatalogSnapshotsFormatError(f"{context}: ожидалось логическое значение.")
    return value


def _datetime(value: Any, context: str) -> datetime:
    raw = _string(value, context)
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CatalogSnapshotsFormatError(f"{context}: неверная дата ISO 8601.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise CatalogSnapshotsFormatError(f"{context}: часовой пояс обязателен.")
    return result


def _entry_to_dict(item: CatalogEntrySnapshot) -> dict[str, Any]:
    if not isinstance(item.source_catalog_id, CatalogId):
        raise TypeError("CatalogEntrySnapshot.source_catalog_id должен быть CatalogId.")
    return {
        "id": item.id.value,
        "source_catalog_id": item.source_catalog_id.value,
        "origin": item.origin.value,
        "category_id": item.category_id.value,
        "display_name": item.display_name,
        "equipment_type_id": item.equipment_type_id.value,
        "equipment_type_version": item.equipment_type_version,
        "manufacturer": item.manufacturer,
        "model": item.model,
        "properties": thaw_json(item.properties),
        "voltage_class_by_group": {
            key: value.value for key, value in item.voltage_class_by_group.items()
        },
        "normal_position": item.normal_position.value if item.normal_position else None,
        "source": item.source,
        "description": item.description,
        "modified_at": item.modified_at.isoformat(),
        "entry_version": item.entry_version,
        "extensions": thaw_json(item.extensions),
    }


def _override_to_dict(item: ParameterOverride) -> dict[str, Any]:
    return {
        "parameter_key": item.parameter_key,
        "source_value": thaw_json(item.source_value),
        "override_value": thaw_json(item.override_value),
        "reason": item.reason,
        "source": item.source,
        "changed_at": item.changed_at.isoformat(),
        "manual": item.manual,
    }


def catalog_snapshots_to_dict(value: ProjectCatalogSnapshots) -> dict[str, Any]:
    if not isinstance(value, ProjectCatalogSnapshots):
        raise TypeError("catalog_snapshots_to_dict ожидает ProjectCatalogSnapshots.")
    return {
        "format_version": CATALOG_SNAPSHOTS_FORMAT_VERSION,
        "id": value.id.value,
        "bindings": [
            {
                "equipment_id": binding.equipment_id.value,
                "entry": _entry_to_dict(binding.entry),
                "instance_overrides": thaw_json(binding.instance_overrides),
                "calculated_values": thaw_json(binding.calculated_values),
                "parameter_overrides": [
                    _override_to_dict(item)
                    for item in binding.parameter_overrides.values()
                ],
                "extensions": thaw_json(binding.extensions),
            }
            for binding in value.bindings.values()
        ],
    }


def _parse_entry(value: Any, context: str) -> CatalogEntrySnapshot:
    row = _strict(
        value,
        _ENTRY_FIELDS,
        context,
        optional={"source_catalog_id"},
    )
    voltage_rows = _object(row["voltage_class_by_group"], context + ".voltage_class_by_group")
    raw_position = row["normal_position"]
    try:
        return CatalogEntrySnapshot(
            CatalogEntryId(_string(row["id"], context + ".id")),
            CatalogOrigin(_string(row["origin"], context + ".origin")),
            CatalogCategoryId(_string(row["category_id"], context + ".category_id")),
            _string(row["display_name"], context + ".display_name"),
            EquipmentTypeId(_string(row["equipment_type_id"], context + ".equipment_type_id")),
            _integer(row["equipment_type_version"], context + ".equipment_type_version"),
            _string(row["manufacturer"], context + ".manufacturer", empty=True),
            _string(row["model"], context + ".model", empty=True),
            _object(row["properties"], context + ".properties"),
            {
                _string(key, context + ".voltage_class_by_group key"):
                VoltageClassId(_string(voltage_id, context + f".voltage_class_by_group.{key}"))
                for key, voltage_id in voltage_rows.items()
            },
            None if raw_position is None else SwitchPosition(_string(raw_position, context + ".normal_position")),
            _string(row["source"], context + ".source"),
            _string(row["description"], context + ".description", empty=True),
            _datetime(row["modified_at"], context + ".modified_at"),
            _integer(row["entry_version"], context + ".entry_version"),
            _object(row["extensions"], context + ".extensions"),
            None
            if "source_catalog_id" not in row
            else CatalogId(
                _string(
                    row["source_catalog_id"],
                    context + ".source_catalog_id",
                )
            ),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogSnapshotsFormatError(f"{context}: {exc}") from exc


def _parse_override(value: Any, context: str) -> ParameterOverride:
    row = _strict(value, _OVERRIDE_FIELDS, context)
    try:
        return ParameterOverride(
            _string(row["parameter_key"], context + ".parameter_key"),
            row["source_value"],
            row["override_value"],
            _string(row["reason"], context + ".reason"),
            _string(row["source"], context + ".source"),
            _datetime(row["changed_at"], context + ".changed_at"),
            _boolean(row["manual"], context + ".manual"),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogSnapshotsFormatError(f"{context}: {exc}") from exc


def catalog_snapshots_from_dict(
    value: Any, electrical_model: ElectricalModel | None = None
) -> ProjectCatalogSnapshots:
    root = _strict(
        value,
        {"format_version", "id", "bindings"},
        "catalog_snapshots",
        optional={"id"},
    )
    version = _integer(root["format_version"], "catalog_snapshots.format_version")
    if version != CATALOG_SNAPSHOTS_FORMAT_VERSION:
        raise CatalogSnapshotsFormatError(f"Формат catalog snapshots v{version} не поддерживается.")
    bindings: list[CatalogBinding] = []
    seen: set[EquipmentId] = set()
    for index, raw in enumerate(_array(root["bindings"], "catalog_snapshots.bindings")):
        context = f"catalog_snapshots.bindings[{index}]"
        row = _strict(raw, _BINDING_FIELDS, context)
        equipment_id = EquipmentId(_string(row["equipment_id"], context + ".equipment_id"))
        if equipment_id in seen:
            raise CatalogSnapshotsFormatError(f"{context}: equipment_id '{equipment_id}' повторяется.")
        seen.add(equipment_id)
        overrides: dict[str, ParameterOverride] = {}
        for override_index, raw_override in enumerate(_array(row["parameter_overrides"], context + ".parameter_overrides")):
            override = _parse_override(raw_override, f"{context}.parameter_overrides[{override_index}]")
            if override.parameter_key in overrides:
                raise CatalogSnapshotsFormatError(f"{context}: override '{override.parameter_key}' повторяется.")
            overrides[override.parameter_key] = override
        try:
            bindings.append(CatalogBinding(
                equipment_id,
                _parse_entry(row["entry"], context + ".entry"),
                _object(row["instance_overrides"], context + ".instance_overrides"),
                _object(row["calculated_values"], context + ".calculated_values"),
                overrides,
                _object(row["extensions"], context + ".extensions"),
            ))
        except (TypeError, ValueError) as exc:
            raise CatalogSnapshotsFormatError(f"{context}: {exc}") from exc
    try:
        catalog_id = (
            DEFAULT_PROJECT_CATALOG_ID
            if "id" not in root
            else CatalogId(_string(root["id"], "catalog_snapshots.id"))
        )
        result = ProjectCatalogSnapshots.from_bindings(
            bindings,
            catalog_id=catalog_id,
        )
        if electrical_model is not None:
            problems = result.validate_targets(electrical_model)
            if problems:
                raise CatalogSnapshotsFormatError("Каталожные снимки содержат повреждённые ссылки:\n- " + "\n- ".join(problems))
        return result
    except CatalogSnapshotsFormatError:
        raise
    except (TypeError, ValueError) as exc:
        raise CatalogSnapshotsFormatError(f"catalog_snapshots: {exc}") from exc


__all__ = [
    "CATALOG_SNAPSHOTS_FORMAT_VERSION",
    "CatalogSnapshotsFormatError",
    "catalog_snapshots_from_dict",
    "catalog_snapshots_to_dict",
]
