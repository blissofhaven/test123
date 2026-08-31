# -*- coding: utf-8 -*-
"""Project-v4 migration and embedded user-catalog acceptance tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rza_calc.domain.catalog import (
    CatalogEntryId,
    DEFAULT_USER_CATALOG_ID,
    RECLOSERS,
    builtin_system_catalog,
)
from rza_calc.domain.electrical import (
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeId,
    PortId,
    VoltageClassId,
)
from rza_calc.io.project import FORMAT_VERSION, load_project, save_project


EXAMPLE = Path(__file__).resolve().parent.parent / "rza_calc" / "examples" / "ps_severnaya.json"


def test_v3_migrates_to_v4_without_guessing_existing_lines(tmp_path):
    project = load_project(EXAMPLE)
    assert project.source_format_version == 3
    assert project.electrical_model.logical_lines == {}
    assert project.electrical_model.line_sections == {}
    assert len(project.user_catalog) == 0

    target = tmp_path / "migrated-v4.json"
    save_project(target, project)
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["format_version"] == FORMAT_VERSION == 7
    assert raw["electrical_model"]["logical_lines"] == []
    assert raw["electrical_model"]["line_sections"] == []
    assert raw["catalogs"] == {
        "format_version": 1,
        "id": DEFAULT_USER_CATALOG_ID.value,
        "catalog_kind": "user",
        "entries": [],
    }
    assert load_project(target).source_format_version == 7


def test_user_recloser_catalog_is_embedded_and_roundtrips(tmp_path):
    project = load_project(EXAMPLE)
    system_entry = builtin_system_catalog().by_category(RECLOSERS)[0]
    instance = system_entry.create_equipment_instance(
        EquipmentId("catalog_seed_instance"),
        (PortId("catalog_seed_a"), PortId("catalog_seed_b")),
        property_overrides={"rated_current_a": 800},
    )
    user_entry = project.user_catalog.save_equipment_instance(
        instance,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.recloser.project"),
        display_name="Проектный реклоузер 800 А",
        manufacturer="Пользователь",
        model="R-800",
        source="Паспорт проекта",
        modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    target = tmp_path / "catalog-v4.json"
    save_project(target, project)
    reopened = load_project(target)
    restored = reopened.user_catalog.get(user_entry.id)
    assert restored.display_name == "Проектный реклоузер 800 А"
    assert restored.equipment_type_id.value == "builtin.recloser"
    assert restored.properties["rated_current_a"] == 800
    assert restored.normal_position == system_entry.normal_position


def test_v4_requires_strict_catalog_section(tmp_path):
    project = load_project(EXAMPLE)
    valid = tmp_path / "valid-v4.json"
    save_project(valid, project)
    raw = json.loads(valid.read_text(encoding="utf-8"))

    del raw["catalogs"]
    missing = tmp_path / "missing-catalogs-v4.json"
    missing.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_project(missing)

    raw = json.loads(valid.read_text(encoding="utf-8"))
    raw["catalogs"]["system_entries"] = []
    unknown = tmp_path / "unknown-catalogs-v4.json"
    unknown.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_project(unknown)


def test_project_rejects_catalog_entry_that_cannot_materialize(tmp_path):
    project = load_project(EXAMPLE)
    invalid = EquipmentInstance(
        EquipmentId("invalid_catalog_recloser"),
        EquipmentTypeId("builtin.recloser"),
        1,
        "Invalid catalog recloser",
        (PortId("invalid_catalog_a"), PortId("invalid_catalog_b")),
        {"rated_current_a": "not-a-number"},
        {"main": VoltageClassId("builtin.voltage.ac.10kv")},
        None,
    )
    project.user_catalog.save_equipment_instance(
        invalid,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.invalid.recloser"),
        display_name="Invalid recloser",
        source="Regression test",
        modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError):
        save_project(tmp_path / "must-not-save.json", project)


def test_project_loader_rejects_tampered_catalog_parameters(tmp_path):
    project = load_project(EXAMPLE)
    system_entry = builtin_system_catalog().by_category(RECLOSERS)[0]
    instance = system_entry.create_equipment_instance(
        EquipmentId("valid_catalog_recloser"),
        (PortId("valid_catalog_a"), PortId("valid_catalog_b")),
    )
    project.user_catalog.save_equipment_instance(
        instance,
        category_id=RECLOSERS,
        entry_id=CatalogEntryId("user.valid.recloser"),
        display_name="Valid recloser",
        source="Regression test",
        modified_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    valid = tmp_path / "valid-catalog.json"
    save_project(valid, project)
    raw = json.loads(valid.read_text(encoding="utf-8"))
    raw["catalogs"]["entries"][0]["properties"]["rated_current_a"] = (
        "not-a-number"
    )
    tampered = tmp_path / "tampered-catalog.json"
    tampered.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_project(tampered)
