"""A2.1: паспорт на диске воспроизводит методику после изменения её файла."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from rza_calc.core.engine import CalculationCase, CalculationCaseError, run
from rza_calc.core.fingerprint import methodology_fingerprint, network_fingerprint
from rza_calc.core.methodology import Methodology
from rza_calc.core.short_circuit import NodeNotEnergizedError
from rza_calc.io.project import load

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests/fixtures/legacy_projects/ps_severnaya.json"


def test_saved_case_replays_after_external_methodology_file_is_replaced_and_deleted(tmp_path):
    net, initial_methodology, _ = load(EXAMPLE)
    external = tmp_path / "methodology.json"
    initial_methodology.save(external)
    original = run(net, Methodology.load(external))
    passport = tmp_path / "calculation.rza-case.json"
    original.calculation_case.save(passport)

    changed = json.loads(external.read_text(encoding="utf-8"))
    changed["mtz"]["k_ots"]["value"] = 9.99
    changed["references"]["ПУЭ"] = "different source"
    external.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    assert not original.is_current_for(net, Methodology.load(external))
    restored = CalculationCase.load(passport)
    external.unlink()  # Only this test's temporary file, never a project file.
    replayed = restored.replay(net)

    assert replayed.results == original.results
    assert replayed.pairs == original.pairs
    assert replayed.warnings == original.warnings
    assert replayed.calculation_case.methodology_fingerprint == original.calculation_case.methodology_fingerprint
    assert methodology_fingerprint(restored.to_methodology()) == restored.methodology_fingerprint
    assert replayed.ctx.meth.path is None
    for mode_id, solver in original.ctx.solvers.items():
        for node_id in net.nodes:
            try:
                expected = solver.at(node_id)
            except NodeNotEnergizedError as original_error:
                with pytest.raises(NodeNotEnergizedError) as replay_error:
                    replayed.ctx.solvers[mode_id].at(node_id)
                assert str(replay_error.value) == str(original_error)
            else:
                assert replayed.ctx.solvers[mode_id].at(node_id) == expected


def test_case_refuses_replay_on_different_network():
    net, meth, _ = load(EXAMPLE)
    case = run(net, meth).calculation_case
    net.nodes[next(iter(net.nodes))].name += " changed"
    with pytest.raises(CalculationCaseError, match="модель отличается"):
        case.replay(net)


@pytest.mark.parametrize("collection", ["nodes", "branches", "transformers3w", "loads", "modes"])
def test_case_guards_declaration_order_without_changing_network_fingerprint(collection):
    example = ROOT / "tests/fixtures/legacy_projects/gtes_sever.json" if collection == "transformers3w" else EXAMPLE
    net, meth, _ = load(example)
    result = run(net, meth)
    original = getattr(net, collection)
    assert len(original) > 1, "Fixture must exercise a real order change."
    setattr(net, collection, dict(reversed(tuple(original.items()))))
    assert network_fingerprint(net) == result.calculation_case.model_fingerprint
    assert not result.is_current_for(net, meth)
    with pytest.raises(CalculationCaseError, match="Порядок объявления"):
        result.calculation_case.replay(net)


@pytest.mark.parametrize("invalid", [None, {}, {"modes": []}, {"modes": ["same", "same"]}])
def test_case_rejects_incomplete_or_invalid_order_metadata(invalid):
    net, meth, _ = load(EXAMPLE)
    payload = run(net, meth).calculation_case.as_dict()
    payload["declaration_order"] = invalid
    with pytest.raises(CalculationCaseError, match="порядок объявления"):
        CalculationCase.from_dict(payload)


@pytest.mark.parametrize("field", ["kernel_version", "algorithm_version"])
def test_case_refuses_replay_under_different_calculation_version(field):
    net, meth, _ = load(EXAMPLE)
    case = replace(run(net, meth).calculation_case, **{field: "another-version"})
    with pytest.raises(CalculationCaseError, match="Версия ядра или алгоритма"):
        case.replay(net)


def test_case_checks_profile_fingerprint_and_does_not_accept_metadata_only_snapshot():
    net, meth, _ = load(EXAMPLE)
    payload = run(net, meth).calculation_case.as_dict()
    payload["methodology_fingerprint"] = "0" * 64
    with pytest.raises(CalculationCaseError, match="Отпечаток методики"):
        CalculationCase.from_dict(payload)
    with pytest.raises(CalculationCaseError):
        CalculationCase.from_dict({"schema": "rza-calculation-case/1"})


def test_case_save_refuses_to_overwrite_existing_file(tmp_path):
    net, meth, _ = load(EXAMPLE)
    case = run(net, meth).calculation_case
    path = tmp_path / "case.json"
    path.write_text("keep me", encoding="utf-8")
    with pytest.raises(FileExistsError):
        case.save(path)
    assert path.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("text", [
    "not json", '{"schema": NaN}', '{"schema": 1, "schema": 2}',
    pytest.param('{"schema": ' + "1" * 5000 + "}", id="oversized-integer"),
    pytest.param("[" * 3000 + "0" + "]" * 3000, id="too-deep"),
])
def test_case_load_rejects_corrupt_or_ambiguous_json(tmp_path, text):
    path = tmp_path / "case.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CalculationCaseError):
        CalculationCase.load(path)
