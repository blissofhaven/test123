"""A2.1: реальные коды процесса и неизменность старого протокола."""
from pathlib import Path
import json
import subprocess
import sys

import pytest

from rza_calc.cli import cmd_check, cmd_methodology, cmd_report, main
from rza_calc.core.engine import CalculationCase, run
from rza_calc.core.fingerprint import network_fingerprint
from rza_calc.io.project import load

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests/fixtures/legacy_projects/ps_severnaya.json"


@pytest.mark.parametrize("command", ["feeder", "explain"])
def test_unknown_feeder_process_exits_with_input_error(command):
    process = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "rza_calc", str(EXAMPLE),
         command, "__does_not_exist__"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert process.returncode == 2
    assert "не найдено" in process.stdout
    assert "Traceback" not in process.stderr


def test_old_protocol_uses_frozen_sources_name_status_and_warning():
    net, methodology, _ = load(EXAMPLE)
    result = run(net, methodology)
    before = cmd_check(result)
    before_coefficients = cmd_methodology(result)
    before_report = cmd_report(result)
    methodology.data["references"]["ПУЭ"] = "EDITED AFTER CALCULATION"
    methodology.data["name"] = "EDITED PROFILE"
    methodology.data["status"] = "ЗАГЛУШКА"
    methodology.data["warning"] = "EDITED WARNING"
    methodology.data["mtz"]["k_ots"]["value"] = 9.99
    methodology.data["short_circuit"]["u_avg"]["10"] = 21
    assert not result.is_current_for(net, methodology)
    assert cmd_methodology(result) == before_coefficients
    assert cmd_check(result) == before
    assert cmd_report(result) == before_report
    assert result.ctx.meth is not methodology


def test_cli_saves_passport_without_concealing_failed_protections(tmp_path, capsys):
    target = tmp_path / "case.json"
    expected = main([str(EXAMPLE), "table"])
    capsys.readouterr()
    assert main([str(EXAMPLE), "save-case", str(target)]) == expected
    case = CalculationCase.load(target)
    net, meth, _ = load(EXAMPLE)
    assert case.replay(net).results == run(net, meth).results
    assert "Паспорт расчёта сохранён" in capsys.readouterr().out


def test_cli_passport_save_does_not_overwrite_existing_file(tmp_path, capsys):
    target = tmp_path / "case.json"
    target.write_text("keep me", encoding="utf-8")
    assert main([str(EXAMPLE), "save-case", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == "keep me"
    assert "Не удалось записать" in capsys.readouterr().out


def _cli(project, *args):
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "rza_calc", str(project), *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
    )


def _external_project(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    _, meth, _ = load(EXAMPLE)
    external = tmp_path / "внешняя методика.json"
    meth.save(external)
    raw["methodology"] = {"file": external.name}
    project = tmp_path / "проект.json"
    project.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return project, external


@pytest.mark.parametrize("change", ["deleted", "corrupt", "changed"])
def test_cli_replays_in_new_process_without_the_original_methodology_file(tmp_path, change):
    project, external = _external_project(tmp_path)
    passport = tmp_path / "паспорт расчёта.json"
    before = _cli(project, "report")
    assert before.returncode == 1  # Existing engineering failures are retained.
    saved = _cli(project, "save-case", passport)
    assert saved.returncode == before.returncode
    project_bytes, passport_bytes = project.read_bytes(), passport.read_bytes()
    if change == "deleted":
        external.unlink()  # This fixture's temporary file only.
    elif change == "corrupt":
        external.write_text("not valid JSON", encoding="utf-8")
    else:
        profile = json.loads(external.read_text(encoding="utf-8"))
        profile["mtz"]["k_ots"]["value"] = 9.99
        profile["short_circuit"]["u_avg"]["10"] = 21
        profile["references"]["ПУЭ"] = "CHANGED AFTER SAVING"
        external.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    ordinary = _cli(project, "report")
    if change != "changed":
        assert ordinary.returncode == 2
    else:
        assert ordinary.stdout != before.stdout

    replayed = _cli(project, "replay-case", passport)
    assert replayed.returncode == before.returncode
    assert replayed.stdout.startswith("Расчёт повторён по паспорту:")
    assert replayed.stdout.endswith(before.stdout)  # Entire protocol, not a selected number.
    assert "Traceback" not in replayed.stderr
    assert "CHANGED AFTER SAVING" not in replayed.stdout
    assert project.read_bytes() == project_bytes
    assert passport.read_bytes() == passport_bytes


@pytest.mark.parametrize("change", ["model", "mode-order", "kernel", "algorithm", "profile-hash"])
def test_cli_replay_rejects_changed_inputs_or_incompatible_passports(tmp_path, change):
    project, _ = _external_project(tmp_path)
    passport = tmp_path / "case.json"
    assert _cli(project, "save-case", passport).returncode == 1
    if change in ("model", "mode-order"):
        raw = json.loads(project.read_text(encoding="utf-8"))
        if change == "model":
            raw["project"]["name"] += " changed"
            raw["electrical_model"]["name"] = raw["project"]["name"]
        else:
            states = raw["electrical_model"]["operating_states"]
            for order, state in enumerate(reversed(states)):
                state["extensions"]["legacy_calculation"]["source_order"] = order
        project.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        if change == "mode-order":
            net, _, _ = load(project)
            case = CalculationCase.load(passport)
            assert network_fingerprint(net) == case.model_fingerprint
            assert tuple(net.modes) != dict(case.declaration_order)["modes"]
    else:
        raw = json.loads(passport.read_text(encoding="utf-8"))
        if change == "profile-hash":
            raw["methodology_fingerprint"] = "0" * 64
        else:
            raw[change + "_version"] = "incompatible"
        passport.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    replayed = _cli(project, "replay-case", passport)
    assert replayed.returncode == 2
    assert "Расчёт не выполнен" in replayed.stdout
    assert "Traceback" not in replayed.stderr
    assert "Расчёт повторён" not in replayed.stdout


@pytest.mark.parametrize("args", [(), ("one.json", "two.json")])
def test_cli_replay_requires_exactly_one_passport_path(args, capsys):
    assert main([str(EXAMPLE), "replay-case", *args]) == 2
    assert "требует один путь" in capsys.readouterr().out


@pytest.mark.parametrize("payload", [None, "not JSON", '{"schema": NaN}', '{"schema": "rza-calculation-case/1"}'])
def test_cli_replay_rejects_missing_corrupt_or_incomplete_passport(tmp_path, payload):
    passport = tmp_path / "case.json"
    if payload is not None:
        passport.write_text(payload, encoding="utf-8")
    replayed = _cli(EXAMPLE, "replay-case", passport)
    assert replayed.returncode == 2
    assert "Расчёт не выполнен" in replayed.stdout
    assert "Traceback" not in replayed.stderr
