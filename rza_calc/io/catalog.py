# -*- coding: utf-8 -*-
"""Строгий JSON codec только для пользовательского каталога."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..domain.catalog import (
    CatalogCategoryId,
    CatalogEntry,
    CatalogEntryId,
    CatalogId,
    CatalogOrigin,
    DEFAULT_USER_CATALOG_ID,
    UserCatalog,
)
from ..domain.electrical import (
    DomainInvariantError,
    EquipmentTypeId,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)


USER_CATALOG_FORMAT_VERSION = 1
USER_CATALOG_KIND = "user"
ROOT_FIELDS = {"format_version", "id", "catalog_kind", "entries"}
ENTRY_FIELDS = {
    "id",
    "category_id",
    "display_name",
    "equipment_type_id",
    "equipment_type_version",
    "manufacturer",
    "model",
    "properties",
    "voltage_class_by_group",
    "normal_position",
    "source",
    "description",
    "modified_at",
    "entry_version",
    "extensions",
}


class CatalogFormatError(ValueError):
    """JSON не соответствует строгому формату user catalog v1."""


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogFormatError(f"{context}: ожидался объект JSON.")
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
        raise CatalogFormatError(f"{context}: неизвестные поля {sorted(unknown)}.")
    if missing:
        raise CatalogFormatError(
            f"{context}: отсутствуют обязательные поля {sorted(missing)}."
        )
    return row


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise CatalogFormatError(f"{context}: ожидался массив JSON.")
    return value


def _string(value: Any, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        expected = "строка" if empty else "непустая строка"
        raise CatalogFormatError(f"{context}: ожидалась {expected}.")
    return value


def _integer(value: Any, context: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CatalogFormatError(
            f"{context}: ожидалось целое число не меньше {minimum}."
        )
    return value


def _parse_datetime(value: Any, context: str) -> datetime:
    raw = _string(value, context)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CatalogFormatError(f"{context}: неверная дата ISO 8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CatalogFormatError(f"{context}: часовой пояс обязателен.")
    return parsed


def _entry_to_dict(entry: CatalogEntry) -> dict[str, Any]:
    if entry.origin is not CatalogOrigin.USER:
        raise TypeError("User catalog codec не сериализует системные записи.")
    return {
        "id": entry.id.value,
        "category_id": entry.category_id.value,
        "display_name": entry.display_name,
        "equipment_type_id": entry.equipment_type_id.value,
        "equipment_type_version": entry.equipment_type_version,
        "manufacturer": entry.manufacturer,
        "model": entry.model,
        "properties": thaw_json(entry.properties),
        "voltage_class_by_group": {
            group: voltage_id.value
            for group, voltage_id in entry.voltage_class_by_group.items()
        },
        "normal_position": (
            entry.normal_position.value if entry.normal_position is not None else None
        ),
        "source": entry.source,
        "description": entry.description,
        "modified_at": entry.modified_at.isoformat(),
        "entry_version": entry.entry_version,
        "extensions": thaw_json(entry.extensions),
    }


def user_catalog_to_dict(catalog: UserCatalog) -> dict[str, Any]:
    if not isinstance(catalog, UserCatalog):
        raise TypeError("user_catalog_to_dict ожидает UserCatalog.")
    return {
        "format_version": USER_CATALOG_FORMAT_VERSION,
        "id": catalog.id.value,
        "catalog_kind": USER_CATALOG_KIND,
        "entries": [
            _entry_to_dict(entry)
            for entry in sorted(catalog.entries.values(), key=lambda item: item.id.value)
        ],
    }


def _parse_entry(value: Any, index: int) -> CatalogEntry:
    context = f"user_catalog.entries[{index}]"
    row = _strict(value, ENTRY_FIELDS, context)
    voltages = _object(
        row["voltage_class_by_group"], context + ".voltage_class_by_group"
    )
    raw_position = row["normal_position"]
    try:
        return CatalogEntry(
            CatalogEntryId(_string(row["id"], context + ".id")),
            CatalogOrigin.USER,
            CatalogCategoryId(
                _string(row["category_id"], context + ".category_id")
            ),
            _string(row["display_name"], context + ".display_name"),
            EquipmentTypeId(
                _string(row["equipment_type_id"], context + ".equipment_type_id")
            ),
            _integer(
                row["equipment_type_version"],
                context + ".equipment_type_version",
            ),
            _string(row["manufacturer"], context + ".manufacturer", empty=True),
            _string(row["model"], context + ".model", empty=True),
            _object(row["properties"], context + ".properties"),
            {
                _string(group, context + ".voltage_class_by_group key"):
                VoltageClassId(
                    _string(
                        voltage_id,
                        context + f".voltage_class_by_group.{group}",
                    )
                )
                for group, voltage_id in voltages.items()
            },
            None
            if raw_position is None
            else SwitchPosition(
                _string(raw_position, context + ".normal_position")
            ),
            _string(row["source"], context + ".source"),
            _string(row["description"], context + ".description", empty=True),
            _parse_datetime(row["modified_at"], context + ".modified_at"),
            _integer(row["entry_version"], context + ".entry_version"),
            _object(row["extensions"], context + ".extensions"),
        )
    except (DomainInvariantError, ValueError) as exc:
        raise CatalogFormatError(f"{context}: {exc}") from exc


def user_catalog_from_dict(value: Any) -> UserCatalog:
    row = _strict(value, ROOT_FIELDS, "user_catalog", optional={"id"})
    version = _integer(
        row["format_version"], "user_catalog.format_version"
    )
    if version != USER_CATALOG_FORMAT_VERSION:
        raise CatalogFormatError(
            f"Формат user catalog v{version} не поддерживается; "
            f"ожидался v{USER_CATALOG_FORMAT_VERSION}."
        )
    kind = _string(row["catalog_kind"], "user_catalog.catalog_kind")
    if kind != USER_CATALOG_KIND:
        raise CatalogFormatError("Codec принимает только catalog_kind='user'.")
    entries = tuple(
        _parse_entry(item, index)
        for index, item in enumerate(_array(row["entries"], "user_catalog.entries"))
    )
    try:
        catalog_id = (
            DEFAULT_USER_CATALOG_ID
            if "id" not in row
            else CatalogId(_string(row["id"], "user_catalog.id"))
        )
        return UserCatalog(entries, catalog_id=catalog_id)
    except DomainInvariantError as exc:
        raise CatalogFormatError(str(exc)) from exc


def save_user_catalog(path: str | Path, catalog: UserCatalog) -> None:
    raw = user_catalog_to_dict(catalog)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(raw, stream, ensure_ascii=False, indent=2)


def load_user_catalog(path: str | Path) -> UserCatalog:
    with open(path, encoding="utf-8") as stream:
        raw = json.load(stream)
    return user_catalog_from_dict(raw)


__all__ = [
    "CatalogFormatError",
    "USER_CATALOG_FORMAT_VERSION",
    "load_user_catalog",
    "save_user_catalog",
    "user_catalog_from_dict",
    "user_catalog_to_dict",
]
