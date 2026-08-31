"""Relocated developer commands work without relying on the caller's cwd.

Every subprocess uses a disposable project copy. Even a broken main guard
must never rebuild the working project's examples or update its baseline.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.io.project import load_project


ROOT = Path(__file__).resolve().parents[1]
TOOLS = (
    "autolayout", "build_demo_network", "build_substation_demo",
    "rebuild_demo", "update_baseline",
)


@pytest.fixture
def isolated_project(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in ("rza_calc", "tools"):
        shutil.copytree(ROOT / name, project / name, ignore=ignored)
    (project / "tests").mkdir()
    shutil.copy2(ROOT / "tests/baseline_snapshot.py", project / "tests")
    shutil.copytree(ROOT / "tests/baseline", project / "tests/baseline")
    outside = tmp_path / "unrelated working directory"
    outside.mkdir()
    return project, outside


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def run_python(arguments, *, cwd):
    environment = dict(os.environ)
    environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH="", QT_QPA_PLATFORM="offscreen")
    return subprocess.run(
        [sys.executable, *map(str, arguments)], cwd=cwd, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("foreign_cwd", [False, True])
def test_help_works_without_writing_from_either_directory(
        isolated_project, tool, foreign_cwd):
    project, outside = isolated_project
    before = snapshot(project)
    cwd = outside if foreign_cwd else project
    script = project / "tools" / f"{tool}.py" if foreign_cwd else Path("tools") / f"{tool}.py"
    result = run_python([script, "--help"], cwd=cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--help" in result.stdout
    assert snapshot(project) == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("tool", TOOLS)
def test_importing_tools_does_not_rebuild_projects(isolated_project, tool):
    project, outside = isolated_project
    before = snapshot(project)
    code = (
        "import importlib, pathlib, sys; "
        f"root = pathlib.Path({str(project)!r}); "
        "sys.path.insert(0, str(root)); "
        f"module = importlib.import_module('tools.{tool}'); "
        "assert module.ROOT == root.resolve()"
    )
    result = run_python(["-c", code], cwd=outside)
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(project) == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("tool, example", [
    ("build_demo_network", "energoraion"),
    ("build_substation_demo", "ps_promyshlennaya"),
    ("rebuild_demo", "gtes_sever"),
])
def test_generators_write_only_their_examples_in_the_copy(
        isolated_project, tool, example):
    project, outside = isolated_project
    examples = project / "rza_calc/examples"
    target = examples / f"{example}.json"
    target.unlink()  # This path is always inside pytest's disposable copy.
    before = snapshot(project)
    result = run_python([project / "tools" / f"{tool}.py"], cwd=outside)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.is_file()
    data = load_project(target)
    assert data.electrical_model.equipment
    assert data.network.modes
    if tool == "build_substation_demo":
        assert data.diagram.routes
        assert not data.diagram.validate_targets(data.electrical_model)
    v1 = json.loads((examples / f"{example}_v1.json").read_text(encoding="utf-8"))
    assert v1["format_version"] == 1
    assert v1["nodes"] and v1["branches"]
    after = snapshot(project)
    changed = {name for name in before.keys() | after.keys()
               if before.get(name) != after.get(name)}
    assert changed <= {f"rza_calc/examples/{example}.json",
                       f"rza_calc/examples/{example}_v1.json"}
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("explicit_target", [False, True])
def test_autolayout_uses_default_root_or_explicit_absolute_target(
        isolated_project, explicit_target):
    project, outside = isolated_project
    default = project / "rza_calc/examples/energoraion.json"
    target = outside / "selected project.json" if explicit_target else default
    if explicit_target:
        shutil.copy2(default, target)
        # Preserve the example's ../data methodology reference in the fixture.
        shutil.copytree(project / "rza_calc/data", outside.parent / "data")
    before = electrical_model_fingerprint(load_project(target).electrical_model)
    script = project / "tools/autolayout.py"
    result = run_python([script, *([target] if explicit_target else [])], cwd=outside)
    assert result.returncode == 0, result.stdout + result.stderr
    data = load_project(target)
    assert data.diagram.routes
    assert not data.diagram.validate_targets(data.electrical_model)
    assert electrical_model_fingerprint(data.electrical_model) == before
    assert not (outside / "rza_calc").exists()


def test_baseline_preview_from_foreign_directory_is_read_only(isolated_project):
    project, outside = isolated_project
    before = snapshot(project)
    result = run_python([
        project / "tools/update_baseline.py", "--stage", "path-check",
        "--reason", "Check relocated command", "--evidence", "Isolated copy",
    ], cwd=outside)
    # A real difference may be present during ongoing development. Without
    # --yes it must still remain a preview; the baseline test owns equality.
    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert "Расхождений с эталоном нет." in result.stdout or "Запись НЕ выполнена" in result.stdout
    assert snapshot(project) == before
    assert list(outside.iterdir()) == []
