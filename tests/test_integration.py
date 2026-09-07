# -*- coding: utf-8 -*-
"""Интеграционная проверка проектов после исправлений расчётной безопасности."""
from __future__ import annotations

import io
import json
import math
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.cli import main as cli_main
from rza_calc.core.engine import CalculationInputError, run
from rza_calc.core.result import FAIL, OK, UNRESOLVED
from rza_calc.io.project import load, load_project, save_project
from test_domain import _structure

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = (
    ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya.json",
    ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json",
)
PS = EXAMPLES[0]
LEGACY_PS = ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya_v1.json"


def _write(tmp_path: Path, raw: dict, name: str = "project.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _assert_optional_equal(left: float | None, right: float | None) -> None:
    if left is None or right is None:
        assert left is right
    else:
        assert abs(left - right) < 1e-9


def test_all_example_result_numbers_are_finite():
    for path in EXAMPLES:
        project = load_project(path)
        result = run(project.network, project.methodology)
        assert result.all_results(), path.name
        for protection in result.all_results():
            assert protection.status in {OK, FAIL, UNRESOLVED}
            for value in (
                protection.i_calc,
                protection.i_primary,
                protection.i_secondary,
                protection.t,
            ):
                if value is not None:
                    assert math.isfinite(value), (path.name, protection.branch_id)
                    assert value >= 0, (path.name, protection.branch_id)
            for check in protection.checks:
                assert check.status in {OK, FAIL, UNRESOLVED}
                if check.value is not None:
                    assert math.isfinite(check.value)


def test_v1_to_current_roundtrip_preserves_settings_and_ct_sides(tmp_path):
    project = load_project(LEGACY_PS)
    before = run(project.network, project.methodology)
    out = tmp_path / "ps-current.json"
    save_project(out, project, methodology_file=str(project.methodology.path))

    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["format_version"] == 7
    legacy_branches = [
        item["properties"]["legacy_payload"]
        for item in raw["electrical_model"]["equipment"]
        if item["type_id"].startswith("compat.rza_calc.")
        and item["extensions"]["legacy_calculation"]["category"] == "branch"
    ]
    assert all("ct_node" in branch for branch in legacy_branches
               if branch.get("ct_ratio") is not None)

    reopened = load_project(out)
    after = run(reopened.network, reopened.methodology)
    for branch_id, branch in project.network.branches.items():
        assert reopened.network.branches[branch_id].ct_node == branch.ct_node
    for branch_id, protections in before.results.items():
        for kind, first in protections.items():
            second = after.get(branch_id, kind)
            assert second is not None
            assert second.status == first.status
            _assert_optional_equal(first.i_calc, second.i_calc)
            _assert_optional_equal(first.i_primary, second.i_primary)
            _assert_optional_equal(first.i_secondary, second.i_secondary)
            _assert_optional_equal(first.t, second.t)


def test_invalid_ct_ratio_blocks_before_calculation():
    net, methodology, _ = load(PS)
    net.branches["F1"].ct_ratio = (400, 0)
    with pytest.raises(CalculationInputError):
        run(net, methodology)


def test_ct_node_outside_branch_blocks_before_calculation():
    net, methodology, _ = load(PS)
    net.branches["F1"].ct_node = "b10_2"
    with pytest.raises(CalculationInputError):
        run(net, methodology)


def test_empty_terminal_scale_blocks_before_calculation():
    net, methodology, _ = load(PS)
    net.branches["F1"].prot.i_scale = []
    with pytest.raises(CalculationInputError):
        run(net, methodology)


def test_state_of_non_switchable_branch_blocks_calculation():
    net, methodology, _ = load(PS)
    next(iter(net.modes.values())).states["F1"] = False
    with pytest.raises(CalculationInputError):
        run(net, methodology)


def test_duplicate_branch_id_in_json_is_rejected(tmp_path):
    raw = json.loads(LEGACY_PS.read_text(encoding="utf-8"))
    raw["branches"].append(deepcopy(raw["branches"][0]))
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, raw))


def test_unknown_top_level_json_section_is_rejected(tmp_path):
    raw = json.loads(PS.read_text(encoding="utf-8"))
    raw["mystery"] = {}
    with pytest.raises(ValueError):
        load_project(_write(tmp_path, raw))


def test_cli_reports_blocking_error_without_traceback(tmp_path):
    raw = json.loads(PS.read_text(encoding="utf-8"))
    raw["methodology"] = {"file": "missing.json"}
    path = _write(tmp_path, raw)
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli_main([str(path), "check"])
    text = output.getvalue()
    assert exit_code == 2
    assert "блокирующую ошибку" in text
    assert "Traceback" not in text


def test_invalid_physical_structure_cannot_be_saved(tmp_path):
    project = load_project(PS)
    # Эта структура ссылается на узлы учебной тестовой сети, а не проекта ПС.
    project.structure = _structure()
    with pytest.raises(ValueError):
        save_project(
            tmp_path / "bad-structure.json",
            project,
            methodology_file=str(project.methodology.path),
        )
