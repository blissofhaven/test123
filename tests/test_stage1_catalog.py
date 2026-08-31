# -*- coding: utf-8 -*-
"""Stage 1 acceptance for isolated system/user equipment catalogs."""
from __future__ import annotations

import json
import sys
import tempfile
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.domain.catalog import (  # noqa: E402
    CatalogEntryId,
    CatalogOrigin,
    RECLOSERS,
    UserCatalog,
    builtin_system_catalog,
)
from rza_calc.domain.electrical import (  # noqa: E402
    DomainInvariantError,
    ElectricalModel,
    EquipmentId,
    PortId,
    PortInstance,
    SwitchPosition,
    VoltageClassId,
    thaw_json,
)
from rza_calc.io.catalog import (  # noqa: E402
    CatalogFormatError,
    load_user_catalog,
    save_user_catalog,
    user_catalog_from_dict,
    user_catalog_to_dict,
)


def _raises(exception_type, operation) -> Exception:
    try:
        operation()
    except exception_type as exc:
        return exc
    raise AssertionError(f"Ожидалось исключение {exception_type.__name__}.")


def _recloser():
    entries = builtin_system_catalog().by_category(RECLOSERS)
    assert entries
    return entries[0]


def test_system_catalog_and_entries_are_deeply_immutable():
    catalog = builtin_system_catalog()
    entry = _recloser()
    assert entry.origin is CatalogOrigin.SYSTEM
    assert entry.category_id == RECLOSERS
    assert entry.equipment_type_id.value == "builtin.recloser"

    def replace_entry():
        catalog.entries[entry.id] = entry

    def replace_property():
        entry.properties["rated_current_a"] = 999

    def replace_field():
        entry.display_name = "Повреждено"

    _raises(TypeError, replace_entry)
    _raises(TypeError, replace_property)
    _raises(FrozenInstanceError, replace_field)


def test_instance_overrides_do_not_mutate_catalog_entry():
    entry = _recloser()
    original = thaw_json(entry.properties)
    params = entry.instance_parameters(
        property_overrides={
            "rated_current_a": 800,
            "protection": {"pickup_a": 420},
        },
        voltage_overrides={
            "main": VoltageClassId("builtin.voltage.ac.6kv"),
        },
        normal_position=SwitchPosition.OPEN,
        instance_extensions={"project_note": "Опытная установка"},
    )
    assert entry.properties["rated_current_a"] == 630
    assert thaw_json(entry.properties) == original
    assert params.properties["rated_current_a"] == 800
    assert params.properties["rated_breaking_current_a"] == 12_500
    assert params.voltage_class_by_group["main"].value.endswith("6kv")
    assert params.normal_position is SwitchPosition.OPEN
    assert params.extensions["catalog"]["entry_id"] == entry.id.value

    instance = entry.create_equipment_instance(
        EquipmentId("qf_recloser"),
        (PortId("qf_recloser_a"), PortId("qf_recloser_b")),
        name="Реклоузер на ВЛ-1",
        property_overrides={"rated_current_a": 800},
    )
    assert instance.name == "Реклоузер на ВЛ-1"
    assert instance.type_id.value == "builtin.recloser"
    assert instance.properties["rated_current_a"] == 800
    assert instance.normal_position is SwitchPosition.CLOSED
    assert entry.properties["rated_current_a"] == 630

    # The catalog object is a genuine current-domain EquipmentInstance, not a
    # parallel DTO: callers only add the role-bearing PortInstance records.
    electrical_model = ElectricalModel.with_builtins("Catalog integration")
    electrical_model.add_equipment_instance(
        instance,
        (
            PortInstance(instance.port_ids[0], instance.id, "a"),
            PortInstance(instance.port_ids[1], instance.id, "b"),
        ),
    )
    assert electrical_model.equipment[instance.id] is instance
    electrical_model.set_equipment_property(instance.id, "rated_current_a", 900)
    electrical_model.set_equipment_note(instance.id, "Параметры изменены в проекте")
    assert electrical_model.equipment[instance.id].properties["rated_current_a"] == 900
    assert electrical_model.equipment[instance.id].note == "Параметры изменены в проекте"
    assert entry.properties["rated_current_a"] == 630
    revision = electrical_model.revision
    _raises(
        DomainInvariantError,
        lambda: electrical_model.set_equipment_property(
            instance.id, "rated_current_a", -1
        ),
    )
    assert electrical_model.revision == revision
    assert electrical_model.equipment[instance.id].properties["rated_current_a"] == 900


def test_equipment_instance_can_be_saved_as_new_user_entry():
    instance = _recloser().create_equipment_instance(
        EquipmentId("qf_user"),
        (PortId("qf_user_a"), PortId("qf_user_b")),
        property_overrides={
            "rated_current_a": 1000,
            "custom_parameters": {"custom_curve": [1, 2, 3]},
        },
        note="Настроено по проекту",
    )
    catalog = UserCatalog()
    entry = catalog.save_equipment_instance(
        instance,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.recloser.site_a"),
        display_name="Реклоузер площадки А",
        manufacturer="Пользователь",
        model="R-1000",
        source="Паспорт площадки А",
        modified_at=datetime(2026, 8, 25, 12, 30, tzinfo=timezone.utc),
    )
    assert entry.origin is CatalogOrigin.USER
    assert entry.properties["rated_current_a"] == 1000
    assert tuple(entry.properties["custom_parameters"]["custom_curve"]) == (1, 2, 3)
    assert entry.description == "Настроено по проекту"
    assert catalog.get(entry.id) is entry
    _raises(
        DomainInvariantError,
        lambda: catalog.save_equipment_instance(
            instance,
            category_id=RECLOSERS,
            entry_id=entry.id,
        ),
    )


def test_user_catalog_strict_codec_round_trip_and_file_io():
    instance = _recloser().create_equipment_instance(
        EquipmentId("qf_roundtrip"),
        (PortId("qf_roundtrip_a"), PortId("qf_roundtrip_b")),
        property_overrides={"rated_current_a": 700},
    )
    catalog = UserCatalog()
    catalog.save_equipment_instance(
        instance,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.recloser.roundtrip"),
        source="Проверка round-trip",
        modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    raw = user_catalog_to_dict(catalog)
    assert raw["catalog_kind"] == "user"
    assert "origin" not in raw["entries"][0]
    restored = user_catalog_from_dict(deepcopy(raw))
    assert user_catalog_to_dict(restored) == raw

    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "user-catalog.json"
        save_user_catalog(path, catalog)
        encoded = json.loads(path.read_text(encoding="utf-8"))
        assert encoded == raw
        assert user_catalog_to_dict(load_user_catalog(path)) == raw

    _raises(TypeError, lambda: user_catalog_to_dict(builtin_system_catalog()))


def test_user_catalog_codec_rejects_unknown_fields_wrong_kind_and_duplicates():
    instance = _recloser().create_equipment_instance(
        EquipmentId("qf_strict"),
        (PortId("qf_strict_a"), PortId("qf_strict_b")),
    )
    catalog = UserCatalog()
    catalog.save_equipment_instance(
        instance,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.recloser.strict"),
        modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    raw = user_catalog_to_dict(catalog)

    unknown_root = deepcopy(raw)
    unknown_root["system_entries"] = []
    _raises(CatalogFormatError, lambda: user_catalog_from_dict(unknown_root))

    unknown_entry = deepcopy(raw)
    unknown_entry["entries"][0]["origin"] = "system"
    _raises(CatalogFormatError, lambda: user_catalog_from_dict(unknown_entry))

    wrong_kind = deepcopy(raw)
    wrong_kind["catalog_kind"] = "system"
    _raises(CatalogFormatError, lambda: user_catalog_from_dict(wrong_kind))

    duplicate = deepcopy(raw)
    duplicate["entries"].append(deepcopy(duplicate["entries"][0]))
    _raises(CatalogFormatError, lambda: user_catalog_from_dict(duplicate))


if __name__ == "__main__":
    tests = tuple(
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
    for test in tests:
        test()
        print(f"[OK] {test.__name__}")
    print(f"Пройдено: {len(tests)}/{len(tests)}")
