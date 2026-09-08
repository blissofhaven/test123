"""Corrected external reference: nameplates, independent phases and native IO."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import pytest

from rza_calc.calculation.input import capture_project_input
from rza_calc.core.engine import run_input
from rza_calc.core.fault_types import FaultSpec
from rza_calc.core.short_circuit import ShortCircuitStatusError
from rza_calc.io.project import load_project, save_project


FIXTURE = Path(__file__).parent / "fixtures" / "control_examples" / "tkz_reference_01"
INPUTS = json.loads((FIXTURE / "testcase_inputs.json").read_text(encoding="utf8"))
REFERENCE = json.loads((FIXTURE / "testcase_reference.json").read_text(encoding="utf8"))
KINDS = {"3ph": "3ph", "LL_BC": "2ph", "LG_A": "1ph_g", "LLG_BCG_common": "2ph_g"}
SPEC = importlib.util.spec_from_file_location("independent_tkz_01", FIXTURE / "four_fault_oracle.py")
ORACLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ORACLE)


def triples(values):
    return tuple(complex(*value) for value in values)


@pytest.fixture(scope="module")
def native_result():
    project = load_project(FIXTURE / "project.json")
    result = run_input(capture_project_input(project))
    assert not result.ctx.errors
    return project, result


def test_reference_is_reproducible_from_inputs_without_expected_value_injection():
    expected = ORACLE.build_reference(INPUTS)
    assert expected["cases"] == REFERENCE["cases"]
    assert REFERENCE["input_sha256"] == hashlib.sha256(
        (FIXTURE / "testcase_inputs.json").read_bytes()).hexdigest()
    poisoned = deepcopy(INPUTS)
    poisoned["expected_results"] = {"I11": -999999}
    assert ORACLE.build_reference(poisoned)["cases"] == REFERENCE["cases"]
    assert len(REFERENCE["cases"]) == 40


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda c: c["id"])
def test_native_four_fault_query_matches_independent_complex_phase_solution(native_result, case):
    _, result = native_result
    solver = result.ctx.solvers[case["mode"]]
    node = case["point"].replace("K", "N")
    spec = FaultSpec(KINDS[case["fault"]])
    if case["scope"] == "conditional_ideal_isolated_no_capacitance":
        # This fixture does not turn the *real* capacitive fault into a zero.
        with pytest.raises(ShortCircuitStatusError) as error:
            solver.fault_at(node, spec)
        assert error.value.code in {"NO_ZERO_SEQUENCE_RETURN_PATH", "MISSING_SEQUENCE_DATA"}
        return
    actual = solver.fault_at(node, spec)
    expected = case["expected"]
    assert actual.iabc_ka == pytest.approx(triples(expected["Iabc_kA"]), rel=1e-9, abs=1e-9)
    assert actual.i012_ka == pytest.approx(triples(expected["I012_kA"]), rel=1e-9, abs=1e-9)
    assert actual.vabc_kv == pytest.approx(
        tuple(value / 1000 for value in triples(expected["Vabc_V"])), rel=1e-9, abs=1e-9)
    assert abs(actual.residual_current_ka) == pytest.approx(expected["residual_3I0_kA"], abs=1e-9)


def test_corrected_llg_reference_preserves_two_different_faulted_phases():
    rows = [c for c in REFERENCE["cases"] if c["fault"] == "LLG_BCG_common"
            and c["zero_path"] == "finite_impedance"]
    assert len(rows) == 4
    for row in rows:
        ia, ib, ic = row["expected"]["phase_magnitudes_kA"]
        assert ia < 1e-9
        assert abs(ib - ic) > 0.1
        assert row["expected"]["max_phase_kA"] == max(ib, ic)
        assert math.sqrt(ib * ic) != pytest.approx(max(ib, ic), rel=0.005)


def test_missing_zero_data_cannot_masquerade_as_the_isolated_limit():
    with pytest.raises(ValueError, match="unknown zero"):
        ORACLE.phase_solve(1j, 2j, None, 1000, "LG_A", zero_path="unknown")


def test_reference_roundtrip_preserves_inputs_and_comparison(tmp_path, native_result):
    original, old_result = native_result
    source = INPUTS["inputs"]
    network = original.network
    assert network.nodes["N1"].calculation_base_kv == source["voltage_kv"]["U1"]
    assert network.nodes["N4"].prefault_voltage_kv == pytest.approx(
        source["voltage_kv"]["U3"] * source["generator"]["emf_subtransient_pu"])
    assert original.methodology.k("short_circuit.temp_factor_min") == 1
    for key, line in source["lines"].items():
        assert network.branches[key].length_km == line["length_km"]
        assert network.branches[key].r0 == line["r_positive_ohm_km"]
        assert network.branches[key].x0 == line["x_positive_ohm_km"]
    generator = source["generator"]
    assert network.branches["G1"].r_pu == pytest.approx(
        generator["xd_subtransient_pu"] /
        (2 * math.pi * source["frequency_hz"] * generator["dc_time_constant_s"]))
    path = tmp_path / "roundtrip.json"
    save_project(path, original)
    new_result = run_input(capture_project_input(load_project(path)))
    for mode in ("MAX", "MIN"):
        for node in ("N4", "N5"):
            for kind in KINDS.values():
                old = old_result.ctx.solvers[mode].fault_at(node, FaultSpec(kind))
                new = new_result.ctx.solvers[mode].fault_at(node, FaultSpec(kind))
                assert new.iabc_ka == old.iabc_ka
                assert new.i012_ka == old.i012_ka
                assert new.z012_ohm == old.z012_ohm
