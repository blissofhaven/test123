# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from rza_calc.domain.electrical import (
    EquipmentTypeDefinition,
    EquipmentTypeId,
    builtin_equipment_types,
)
from rza_calc.editor.placement import (
    EquipmentPlacementKind,
    EquipmentPlacementRegistry,
)


def _definition(
    suffix: str,
    *capabilities: str,
    version: int = 1,
    behavior_key: str = "custom",
) -> EquipmentTypeDefinition:
    return EquipmentTypeDefinition(
        EquipmentTypeId(f"custom.placement.{suffix}"),
        version,
        f"Пользовательский тип {suffix}",
        behavior_key,
        (),
        capabilities=frozenset(capabilities),
        extensions={"diagram_symbol_key": "unrelated-symbol"},
    )


@pytest.mark.parametrize(
    ("capability", "expected"),
    (
        (
            "placement.inline_series",
            EquipmentPlacementKind.INLINE_SERIES,
        ),
        (
            "placement.branch_attachment",
            EquipmentPlacementKind.BRANCH_ATTACHMENT,
        ),
        (
            "placement.node_representation",
            EquipmentPlacementKind.NODE_REPRESENTATION,
        ),
        (
            "placement.manual_selection",
            EquipmentPlacementKind.MANUAL_SELECTION,
        ),
    ),
)
def test_registry_resolves_all_explicit_placement_capabilities(
    capability: str,
    expected: EquipmentPlacementKind,
) -> None:
    definition = _definition(expected.value, capability)
    registry = EquipmentPlacementRegistry((definition,))

    assert registry.resolve(definition.id, definition.schema_version) is expected
    assert registry.resolve(definition.id.value, 1) is expected
    assert registry.resolve_definition(definition) is expected


def test_definition_without_placement_capability_defaults_to_manual_selection() -> None:
    definition = _definition("default", "switch.position", "custom.feature")

    registry = EquipmentPlacementRegistry((definition,))

    assert (
        registry.resolve_definition(definition)
        is EquipmentPlacementKind.MANUAL_SELECTION
    )


def test_conflicting_placement_capabilities_are_rejected_in_russian() -> None:
    definition = _definition(
        "conflict",
        "placement.inline_series",
        "placement.branch_attachment",
    )

    with pytest.raises(ValueError, match="несколько.*правил размещения"):
        EquipmentPlacementRegistry((definition,))


def test_unknown_placement_capability_is_not_guessed() -> None:
    definition = _definition("unknown", "placement.from_svg")

    with pytest.raises(ValueError, match="неизвестное правило размещения"):
        EquipmentPlacementRegistry((definition,))


def test_custom_type_uses_only_capability_not_type_name_symbol_or_behavior() -> None:
    definition = _definition(
        "looks-like-inline-breaker",
        "placement.branch_attachment",
        behavior_key="switch",
    )

    registry = EquipmentPlacementRegistry((definition,))

    assert (
        registry.resolve_definition(definition)
        is EquipmentPlacementKind.BRANCH_ATTACHMENT
    )


def test_registry_is_immutable_and_deterministic() -> None:
    first = _definition("z", "placement.inline_series", version=2)
    second = _definition("a", "placement.node_representation")
    registry = EquipmentPlacementRegistry((first, second))
    reversed_registry = EquipmentPlacementRegistry((second, first))

    assert registry.signature == reversed_registry.signature
    assert registry.fingerprint == reversed_registry.fingerprint
    assert len(registry) == 2
    assert tuple(registry) == tuple(registry.placements.items())
    with pytest.raises(TypeError):
        registry.placements[(first.id, first.schema_version)] = (
            EquipmentPlacementKind.MANUAL_SELECTION
        )
    with pytest.raises(AttributeError):
        registry.extra = "mutable"  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        registry._fingerprint = "mutable"  # type: ignore[misc]


def test_model_registry_keeps_exact_builtin_v7_placement_compatibility() -> None:
    legacy_recloser = EquipmentTypeDefinition(
        EquipmentTypeId("builtin.recloser"),
        1,
        "Реклоузер старого проекта",
        "legacy-behavior-is-not-used",
        (),
    )
    custom = _definition("legacy-custom-without-placement")

    registry = EquipmentPlacementRegistry.for_model((legacy_recloser, custom))

    assert (
        registry.resolve_definition(legacy_recloser)
        is EquipmentPlacementKind.INLINE_SERIES
    )
    assert (
        registry.resolve_definition(custom)
        is EquipmentPlacementKind.MANUAL_SELECTION
    )


def test_builtin_compatibility_is_bound_to_exact_version() -> None:
    future_recloser = EquipmentTypeDefinition(
        EquipmentTypeId("builtin.recloser"),
        2,
        "Реклоузер версии 2",
        "recloser",
        (),
    )

    registry = EquipmentPlacementRegistry.for_model((future_recloser,))

    assert (
        registry.resolve_definition(future_recloser)
        is EquipmentPlacementKind.MANUAL_SELECTION
    )


def test_fresh_builtin_definitions_have_explicit_placement_capabilities() -> None:
    definitions = {
        item.id.value: item for item in builtin_equipment_types()
    }
    registry = EquipmentPlacementRegistry(definitions.values())
    expected = {
        "builtin.external_grid": EquipmentPlacementKind.BRANCH_ATTACHMENT,
        "builtin.generator": EquipmentPlacementKind.BRANCH_ATTACHMENT,
        "builtin.transformer_2w": EquipmentPlacementKind.BRANCH_ATTACHMENT,
        "builtin.transformer_3w": EquipmentPlacementKind.BRANCH_ATTACHMENT,
        "builtin.load": EquipmentPlacementKind.BRANCH_ATTACHMENT,
        "builtin.busbar": EquipmentPlacementKind.NODE_REPRESENTATION,
        "builtin.connection_point": EquipmentPlacementKind.NODE_REPRESENTATION,
        "builtin.circuit_breaker": EquipmentPlacementKind.INLINE_SERIES,
        "builtin.disconnector": EquipmentPlacementKind.INLINE_SERIES,
        "builtin.recloser": EquipmentPlacementKind.INLINE_SERIES,
    }

    assert {
        type_id: registry.resolve_definition(definitions[type_id])
        for type_id in expected
    } == expected
