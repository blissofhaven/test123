# -*- coding: utf-8 -*-
"""Явный реестр поведения размещения электрического оборудования.

Правило размещения берётся только из capability ``placement.*`` определения
типа оборудования. Имена типов, SVG-символы, ``behavior_key`` и число портов
не используются как неявные признаки.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from ..domain.electrical import (
    EquipmentTypeDefinition,
    EquipmentTypeId,
)


class EquipmentPlacementKind(StrEnum):
    """Поддерживаемые способы начала размещения оборудования."""

    INLINE_SERIES = "inline_series"
    BRANCH_ATTACHMENT = "branch_attachment"
    NODE_REPRESENTATION = "node_representation"
    MANUAL_SELECTION = "manual_selection"


_CAPABILITY_PREFIX = "placement."
_CAPABILITY_TO_KIND: Mapping[str, EquipmentPlacementKind] = MappingProxyType({
    f"{_CAPABILITY_PREFIX}{kind.value}": kind
    for kind in EquipmentPlacementKind
})

# Старые проекты формата v7 могли сохранить встроенные определения до
# появления capability ``placement.*``. Это именно системный реестр по
# постоянным ID и версии, а не эвристика по имени класса, SVG или числу портов.
# Пользовательские типы без явной capability по-прежнему требуют ручного
# выбора, поэтому совместимость не скрывает неоднозначность каталогов.
_BUILTIN_COMPATIBILITY_PLACEMENTS: Mapping[
    tuple[str, int], EquipmentPlacementKind
] = MappingProxyType({
    ("builtin.external_grid", 1): EquipmentPlacementKind.BRANCH_ATTACHMENT,
    ("builtin.generator", 1): EquipmentPlacementKind.BRANCH_ATTACHMENT,
    ("builtin.transformer_2w", 1): EquipmentPlacementKind.BRANCH_ATTACHMENT,
    ("builtin.transformer_3w", 1): EquipmentPlacementKind.BRANCH_ATTACHMENT,
    ("builtin.load", 1): EquipmentPlacementKind.BRANCH_ATTACHMENT,
    ("builtin.busbar", 1): EquipmentPlacementKind.NODE_REPRESENTATION,
    ("builtin.connection_point", 1): EquipmentPlacementKind.NODE_REPRESENTATION,
    ("builtin.circuit_breaker", 1): EquipmentPlacementKind.INLINE_SERIES,
    ("builtin.disconnector", 1): EquipmentPlacementKind.INLINE_SERIES,
    ("builtin.recloser", 1): EquipmentPlacementKind.INLINE_SERIES,
})


def _placement_from_definition(
    definition: EquipmentTypeDefinition,
) -> EquipmentPlacementKind:
    if not isinstance(definition, EquipmentTypeDefinition):
        raise TypeError(
            "Правило размещения можно определить только для "
            "EquipmentTypeDefinition."
        )
    capabilities = tuple(sorted(
        item
        for item in definition.capabilities
        if item.startswith(_CAPABILITY_PREFIX)
    ))
    if not capabilities:
        return EquipmentPlacementKind.MANUAL_SELECTION
    if len(capabilities) > 1:
        raise ValueError(
            f"Тип оборудования '{definition.id.value}' версии "
            f"{definition.schema_version} содержит несколько "
            "взаимоисключающих правил размещения: "
            + ", ".join(capabilities)
            + "."
        )
    capability = capabilities[0]
    try:
        return _CAPABILITY_TO_KIND[capability]
    except KeyError as exc:
        raise ValueError(
            f"Тип оборудования '{definition.id.value}' версии "
            f"{definition.schema_version} содержит неизвестное правило "
            f"размещения '{capability}'."
        ) from exc


@dataclass(frozen=True, slots=True, init=False)
class EquipmentPlacementRegistry:
    """Неизменяемое отображение ``(type_id, version)`` в правило размещения."""

    _placements: Mapping[
        tuple[EquipmentTypeId, int], EquipmentPlacementKind
    ]
    _signature: tuple[tuple[str, int, str], ...]
    _fingerprint: str
    _compatibility_defaults: bool

    def __init__(
        self,
        definitions: Iterable[EquipmentTypeDefinition],
        *,
        compatibility_defaults: bool = False,
    ):
        values: dict[
            tuple[EquipmentTypeId, int], EquipmentPlacementKind
        ] = {}
        for definition in definitions:
            if not isinstance(definition, EquipmentTypeDefinition):
                raise TypeError(
                    "Реестр размещения принимает только "
                    "EquipmentTypeDefinition."
                )
            key = (definition.id, definition.schema_version)
            if key in values:
                raise ValueError(
                    f"Тип оборудования '{definition.id.value}' версии "
                    f"{definition.schema_version} зарегистрирован в реестре "
                    "размещения дважды."
                )
            placement = _placement_from_definition(definition)
            if (
                compatibility_defaults
                and placement is EquipmentPlacementKind.MANUAL_SELECTION
                and not any(
                    item.startswith(_CAPABILITY_PREFIX)
                    for item in definition.capabilities
                )
            ):
                placement = _BUILTIN_COMPATIBILITY_PLACEMENTS.get(
                    (definition.id.value, definition.schema_version),
                    placement,
                )
            values[key] = placement

        ordered = dict(sorted(
            values.items(),
            key=lambda item: (item[0][0].value, item[0][1]),
        ))
        object.__setattr__(self, "_placements", MappingProxyType(ordered))
        object.__setattr__(
            self, "_compatibility_defaults", bool(compatibility_defaults)
        )
        signature = tuple(
            (type_id.value, version, placement.value)
            for (type_id, version), placement in ordered.items()
        )
        object.__setattr__(self, "_signature", signature)
        encoded = json.dumps(
            signature,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(
            self, "_fingerprint", hashlib.sha256(encoded).hexdigest()
        )

    @property
    def placements(
        self,
    ) -> Mapping[tuple[EquipmentTypeId, int], EquipmentPlacementKind]:
        return self._placements

    @property
    def signature(self) -> tuple[tuple[str, int, str], ...]:
        """Детерминированная семантическая сигнатура содержимого реестра."""
        return self._signature

    @property
    def fingerprint(self) -> str:
        """SHA-256 детерминированной сигнатуры реестра."""
        return self._fingerprint

    @classmethod
    def for_model(
        cls,
        definitions: Iterable[EquipmentTypeDefinition],
    ) -> "EquipmentPlacementRegistry":
        """Создать рабочий реестр с совместимостью встроенных типов v7."""

        return cls(definitions, compatibility_defaults=True)

    def resolve(
        self,
        type_id: EquipmentTypeId | str,
        schema_version: int,
    ) -> EquipmentPlacementKind:
        normalized_id = (
            type_id
            if isinstance(type_id, EquipmentTypeId)
            else EquipmentTypeId(type_id)
        )
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise TypeError("Версия типа оборудования должна быть целым числом.")
        key = (normalized_id, schema_version)
        try:
            return self._placements[key]
        except KeyError as exc:
            raise KeyError(
                f"Для типа оборудования '{normalized_id.value}' версии "
                f"{schema_version} не зарегистрировано правило размещения."
            ) from exc

    def resolve_definition(
        self,
        definition: EquipmentTypeDefinition,
    ) -> EquipmentPlacementKind:
        if not isinstance(definition, EquipmentTypeDefinition):
            raise TypeError(
                "resolve_definition ожидает EquipmentTypeDefinition."
            )
        registered = self.resolve(definition.id, definition.schema_version)
        actual = _placement_from_definition(definition)
        if (
            self._compatibility_defaults
            and actual is EquipmentPlacementKind.MANUAL_SELECTION
            and not any(
                item.startswith(_CAPABILITY_PREFIX)
                for item in definition.capabilities
            )
        ):
            actual = _BUILTIN_COMPATIBILITY_PLACEMENTS.get(
                (definition.id.value, definition.schema_version),
                actual,
            )
        if registered is not actual:
            raise ValueError(
                f"Определение типа оборудования '{definition.id.value}' версии "
                f"{definition.schema_version} не совпадает с зарегистрированным "
                "правилом размещения."
            )
        return registered

    def __iter__(
        self,
    ) -> Iterator[tuple[tuple[EquipmentTypeId, int], EquipmentPlacementKind]]:
        return iter(self._placements.items())

    def __len__(self) -> int:
        return len(self._placements)


__all__ = [
    "EquipmentPlacementKind",
    "EquipmentPlacementRegistry",
]
