# -*- coding: utf-8 -*-
"""Версия JSON, миграция и сохранение физических объектов."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.core.engine import run
from rza_calc.io.project import FORMAT_VERSION, load_project, save_project
from test_domain import _structure

ROOT = Path(__file__).resolve().parent.parent
GTES = ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json"
PS = ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya.json"
LEGACY_GTES = ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever_v1.json"
LEGACY_PS = ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya_v1.json"


def _write(tmp_path: Path, raw: dict, name: str = "project.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_version_1_is_migrated_without_guessing_structure():
    project = load_project(LEGACY_GTES)
    assert project.source_format_version == 1
    assert project.structure.facilities == {}
    assert len(project.network.transformers3w) == 2


def test_three_winding_transformers_survive_v3_roundtrip(tmp_path):
    project = load_project(GTES)
    before = run(project.network, project.methodology)
    out = tmp_path / "gtes-v3.json"
    save_project(out, project, methodology_file=str(project.methodology.path))

    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["format_version"] == FORMAT_VERSION == 7
    assert "electrical_model" in raw
    assert not ({"neutral", "nodes", "transformers3w", "branches", "loads", "modes"}
                & set(raw))
    equipment = raw["electrical_model"]["equipment"]
    transformers = [
        item for item in equipment
        if item["type_id"] == "compat.rza_calc.transformer_3w"
    ]
    assert {
        item["extensions"]["legacy_calculation"]["legacy_id"]
        for item in transformers
    } == {"VT1", "VT2"}
    assert all(
        item["properties"]["legacy_payload"]["ct_node"]
        == project.network.transformers3w[
            item["extensions"]["legacy_calculation"]["legacy_id"]
        ].ct_node
        for item in transformers
        if item["properties"]["legacy_payload"].get("ct_ratio") is not None
    )
    assert not any(
        node["extensions"]["legacy_calculation"]["legacy_id"].endswith("__star")
        for node in raw["electrical_model"]["electrical_nodes"]
    )

    loaded = load_project(out)
    assert loaded.source_format_version == 7
    assert set(loaded.network.transformers3w) == {"VT1", "VT2"}
    assert {"VT1", "VT1_mv", "VT1_lv"} <= set(loaded.network.branches)
    after = run(loaded.network, loaded.methodology)
    for mode_id in ("max", "min", "one"):
        before_i3 = before.ctx.solvers[mode_id].at("vost10_1").i3
        after_i3 = after.ctx.solvers[mode_id].at("vost10_1").i3
        assert abs(before_i3 - after_i3) < 1e-9


def test_physical_structure_and_bus_sections_survive_roundtrip(tmp_path):
    project = load_project(PS)
    project.structure = _structure()
    # Тестовая структура ссылается на тестовую сеть; для проверки формата
    # заменяем ссылки на реально существующие узлы и ветвь примера ПС.
    project.structure.bus_sections["gtes_10_bus"].calculation_node_id = "b35"
    project.structure.bus_sections["ps_35_bus"].calculation_node_id = "b10_1"
    project.structure.equipment["line"].calculation_refs[0] = (
        project.structure.equipment["line"].calculation_refs[0].__class__(
            "branch", "F1", "impedance"
        )
    )

    out = tmp_path / "structure-v3.json"
    save_project(out, project, methodology_file=str(project.methodology.path))
    loaded = load_project(out)

    assert set(loaded.structure.facilities) == {"gtes", "ps"}
    assert set(loaded.structure.bus_sections) == {"gtes_10_bus", "ps_35_bus"}
    assert loaded.structure.bays["ps_line_bay"].bus_section_id == "ps_35_bus"
    assert {p.terminal for p in loaded.structure.placements_of("line")} == {"from", "to"}


def test_newer_format_is_rejected(tmp_path):
    raw = json.loads(PS.read_text(encoding="utf-8"))
    raw["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, raw))


def test_missing_format_version_is_rejected(tmp_path):
    raw = json.loads(PS.read_text(encoding="utf-8"))
    del raw["format_version"]
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, raw))


def test_v2_requires_structure_section(tmp_path):
    raw = json.loads(LEGACY_PS.read_text(encoding="utf-8"))
    raw["format_version"] = 2
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, raw))
