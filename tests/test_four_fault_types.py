# -*- coding: utf-8 -*-
"""Independent phase-boundary checks for the four point-fault equations.

The oracle below solves the phase-domain Thevenin circuit, not the sequence
fault connection formulas used by production. Tolerances are fixed before
comparison: 1e-11 relative / 1e-12 absolute for ordinary engineering values.
"""
from dataclasses import FrozenInstanceError
import math

import numpy as np
import pytest

from rza_calc.core.fault_types import FaultResult, FaultSpec, FaultType, solve_fault
from rza_calc.core.short_circuit import ShortCircuitStatusError


KINDS = tuple(FaultType)


def phase_oracle(e, z1, z2, z0, spec):
    """V=Eabc-Zabc*I with the physical phase boundary conditions at the fault."""
    rotation = np.exp(2j * np.pi / 3)
    transform = np.array([[1, 1, 1],
                          [1, rotation ** 2, rotation],
                          [1, rotation, rotation ** 2]], dtype=complex)
    phase_z = transform @ np.diag([z0, z1, z2]) @ np.linalg.inv(transform)
    phase_e = transform @ np.array([0, e, 0], dtype=complex)
    arm, ground = spec.phase_arm_impedance_ohm, spec.common_ground_impedance_ohm
    if spec.kind == FaultType.THREE_PHASE:
        matrix = phase_z + arm * np.eye(3)
        rhs = phase_e
    elif spec.kind == FaultType.LINE_LINE:
        # Ia=0; Ib+Ic=0; Vb-Vc=2*Zarm*Ib.
        matrix = np.array([[1, 0, 0], [0, 1, 1], phase_z[1] - phase_z[2]])
        matrix[2, 1] += 2 * arm
        rhs = np.array([0, 0, phase_e[1] - phase_e[2]])
    elif spec.kind == FaultType.LINE_GROUND:
        # Ib=Ic=0; Va=(Zarm+Zground)*Ia.
        matrix = np.array([[0, 1, 0], [0, 0, 1], phase_z[0]])
        matrix[2, 0] += arm + ground
        rhs = np.array([0, 0, phase_e[0]])
    else:
        # Ia=0; Vb=Zarm*Ib+Zground*(Ib+Ic), and likewise for C.
        matrix = np.array([[1, 0, 0], phase_z[1], phase_z[2]])
        matrix[1, 1] += arm + ground
        matrix[1, 2] += ground
        matrix[2, 1] += ground
        matrix[2, 2] += arm + ground
        rhs = np.array([0, phase_e[1], phase_e[2]])
    currents = np.linalg.solve(matrix, rhs)
    voltages = phase_e - phase_z @ currents
    return currents, voltages


@pytest.mark.parametrize("kind,expected", [
    (FaultType.THREE_PHASE, (0j, -1j, 0j)),
    (FaultType.LINE_LINE, (0j, -1j / 3, 1j / 3)),
    (FaultType.LINE_GROUND, (-1j / 6, -1j / 6, -1j / 6)),
    (FaultType.LINE_LINE_GROUND, (2j / 11, -5j / 11, 3j / 11)),
])
def test_manual_unequal_sequence_values(kind, expected):
    result = solve_fault(1, 1j, FaultSpec(kind), z2_ohm=2j, z0_ohm=3j)
    assert result.i012_ka == pytest.approx(expected, rel=1e-12, abs=1e-14)
    if kind == FaultType.LINE_LINE_GROUND:
        assert result.iabc_ka == pytest.approx((
            0j, complex(-4 * math.sqrt(3), 3) / 11,
            complex(4 * math.sqrt(3), 3) / 11,
        ), rel=1e-12, abs=1e-14)
        assert result.residual_current_ka == pytest.approx(6j / 11)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("impedances", [
    (1j, 2j, 3j),
    (0.23 + 1.7j, 0.4 + 0.8j, 0.9 + 2.3j),
    (1.8 + 0.04j, 0.35 + 2.1j, 0.01 + 0.3j),
    (0.4 + 1.3j, 0.6 - 0.7j, 0.3 + 0.4j),
])
@pytest.mark.parametrize("fault_arm", [0j, 0.21 + 0.04j])
@pytest.mark.parametrize("emf", [1 + 0j, 3.7 - 2.1j])
def test_phase_domain_oracle_and_boundary_conditions(kind, impedances, fault_arm, emf):
    ground = 0.17 + 0.02j if kind in (FaultType.LINE_GROUND, FaultType.LINE_LINE_GROUND) else 0j
    spec = FaultSpec(kind, fault_arm, ground)
    z1, z2, z0 = impedances
    result = solve_fault(emf, z1, spec, z2_ohm=z2, z0_ohm=z0)
    expected_i, expected_v = phase_oracle(emf, z1, z2, z0, spec)
    assert result.iabc_ka == pytest.approx(expected_i, rel=1e-11, abs=1e-12)
    assert result.vabc_kv == pytest.approx(expected_v, rel=1e-11, abs=1e-12)
    assert result.residual_current_ka == pytest.approx(3 * result.i012_ka[0], abs=1e-12)
    ia, ib, ic = result.iabc_ka
    va, vb, vc = result.vabc_kv
    if kind == FaultType.THREE_PHASE:
        assert ia + ib + ic == pytest.approx(0, abs=1e-12)
        assert (va, vb, vc) == pytest.approx(tuple(fault_arm * i for i in (ia, ib, ic)), abs=1e-12)
    elif kind == FaultType.LINE_LINE:
        assert ia == pytest.approx(0, abs=1e-12)
        assert ib + ic == pytest.approx(0, abs=1e-12)
        assert vb - vc == pytest.approx(2 * fault_arm * ib, abs=1e-12)
    elif kind == FaultType.LINE_GROUND:
        assert (ib, ic) == pytest.approx((0, 0), abs=1e-12)
        assert va == pytest.approx((fault_arm + ground) * ia, abs=1e-12)
    else:
        assert ia == pytest.approx(0, abs=1e-12)
        assert vb == pytest.approx(fault_arm * ib + ground * (ib + ic), abs=1e-12)
        assert vc == pytest.approx(fault_arm * ic + ground * (ib + ic), abs=1e-12)


def test_equal_z2_special_case_and_unequal_z2_are_distinguished():
    three = solve_fault(6.1, 0.3 + 1.2j, FaultSpec("3ph"))
    two_equal = solve_fault(6.1, 0.3 + 1.2j, FaultSpec("2ph"), z2_ohm=0.3 + 1.2j)
    two_unequal = solve_fault(6.1, 0.3 + 1.2j, FaultSpec("2ph"), z2_ohm=1.1 + 2.4j)
    expected = math.sqrt(3) / 2 * abs(three.iabc_ka[0])
    assert abs(two_equal.iabc_ka[1]) == pytest.approx(expected)
    assert not math.isclose(abs(two_unequal.iabc_ka[1]), expected, rel_tol=1e-3)


@pytest.mark.parametrize("kind,kwargs,missing", [
    ("2ph", {}, ("Z2",)),
    ("1ph_g", {}, ("Z2", "Z0")),
    ("1ph_g", {"z2_ohm": 1j}, ("Z0",)),
    ("2ph_g", {"z0_ohm": 1j}, ("Z2",)),
    ("2ph_g", {"z2_ohm": 1j}, ("Z0",)),
])
def test_missing_data_are_named_and_never_guessed(kind, kwargs, missing):
    with pytest.raises(ShortCircuitStatusError) as caught:
        solve_fault(1, 1j, FaultSpec(kind), **kwargs)
    assert caught.value.code == "MISSING_SEQUENCE_DATA"
    assert all(name in str(caught.value) for name in missing)


def test_only_required_sequences_are_needed_and_supplied_values_are_preserved():
    result3 = solve_fault(1, 1j, FaultSpec("3ph"))
    result2 = solve_fault(1, 1j, FaultSpec("2ph"), z2_ohm=2j)
    assert result3.z012_ohm == (None, 1j, None)
    assert result2.z012_ohm == (None, 1j, 2j)
    assert result3.v012_kv[0] == result3.v012_kv[2] == 0j
    unused = solve_fault(1, 1j, FaultSpec("3ph"), z2_ohm=1e300j, z0_ohm=1e300j)
    assert unused.iabc_ka == result3.iabc_ka
    assert unused.z012_ohm == (1e300j, 1j, 1e300j)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), complex(1, float("nan")), True, "1.0"])
@pytest.mark.parametrize("field", ["emf", "z1", "z2", "z0", "arm", "ground"])
def test_nonfinite_and_nonnumeric_inputs_are_structured_errors(bad, field):
    values = dict(emf=1, z1=1j, z2=2j, z0=3j, arm=0j, ground=0j)
    values[field] = bad
    with pytest.raises(ShortCircuitStatusError) as caught:
        spec = FaultSpec("2ph_g", values["arm"], values["ground"])
        solve_fault(values["emf"], values["z1"], spec, z2_ohm=values["z2"], z0_ohm=values["z0"])
    assert caught.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("field", ["z1", "z2", "z0", "arm", "ground"])
def test_negative_resistance_rejected(field):
    values = dict(z1=1j, z2=2j, z0=3j, arm=0j, ground=0j)
    values[field] = -0.001 + 1j
    with pytest.raises(ShortCircuitStatusError) as caught:
        spec = FaultSpec("2ph_g", values["arm"], values["ground"])
        solve_fault(1, values["z1"], spec, z2_ohm=values["z2"], z0_ohm=values["z0"])
    assert caught.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("kind", ["3ph", "2ph"])
def test_non_ground_fault_rejects_extraneous_ground_impedance(kind):
    with pytest.raises(ShortCircuitStatusError) as caught:
        FaultSpec(kind, common_ground_impedance_ohm=0.1)
    assert caught.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("kind,z1,z2,z0,arm", [
    ("3ph", 0j, None, None, 0j),
    ("3ph", 1j, None, None, -1j),
    ("2ph", 1j, -1j, None, 0j),
    ("2ph", 1j, 1e-16 - 1j, None, 0j),
    ("1ph_g", 1j, 1j, -2j, 0j),
    ("2ph_g", 1j, 1j, -0.5j, 0j),
])
def test_singular_and_numerically_cancelled_faults_are_not_clamped(kind, z1, z2, z0, arm):
    with pytest.raises(ShortCircuitStatusError) as caught:
        solve_fault(1, z1, FaultSpec(kind, arm), z2_ohm=z2, z0_ohm=z0)
    assert caught.value.code == "NUMERIC_FAILURE"


def test_cancelled_parallel_sum_can_still_have_unique_double_ground_solution():
    spec = FaultSpec("2ph_g")
    result = solve_fault(1, 1j, spec, z2_ohm=1j, z0_ohm=-1j)
    expected_i, expected_v = phase_oracle(1, 1j, 1j, -1j, spec)
    assert result.iabc_ka == pytest.approx(expected_i, abs=1e-12)
    assert result.vabc_kv == pytest.approx(expected_v, abs=1e-12)
    assert result.i012_ka[1] == 0j


@pytest.mark.parametrize("scale", [1e-100, 1e100])
@pytest.mark.parametrize("kind", KINDS)
def test_small_physical_impedance_and_large_impedance_keep_scaling(scale, kind):
    spec = FaultSpec(kind)
    nominal = solve_fault(1, 1j, spec, z2_ohm=2j, z0_ohm=3j)
    scaled = solve_fault(1, 1j * scale, spec, z2_ohm=2j * scale, z0_ohm=3j * scale)
    assert tuple(i * scale for i in scaled.i012_ka) == pytest.approx(nominal.i012_ka, rel=1e-12, abs=1e-14)
    assert scaled.v012_kv == pytest.approx(nominal.v012_kv, abs=1e-12)


def test_overflow_becomes_structured_failure():
    with pytest.raises(ShortCircuitStatusError) as caught:
        solve_fault(1e100, 1e-300j, FaultSpec("3ph"))
    assert caught.value.code == "NUMERIC_FAILURE"


def test_finite_solution_is_not_rejected_for_large_intermediate_division():
    result = solve_fault(1e308, 1e308j, FaultSpec("3ph"))
    assert result.i012_ka == pytest.approx((0j, -1j, 0j))


def test_double_ground_mixed_impedance_scales_avoid_coefficient_overflow():
    result = solve_fault(1e200, 1e200j, FaultSpec("2ph_g"), z2_ohm=1e-110j, z0_ohm=1e-110j)
    assert result.i012_ka == pytest.approx((0.5j, -1j, 0.5j), rel=1e-12, abs=1e-14)


@pytest.mark.parametrize("kind", KINDS)
def test_zero_emf_has_zero_current_when_circuit_is_nonsingular(kind):
    result = solve_fault(0, 1j, FaultSpec(kind), z2_ohm=2j, z0_ohm=3j)
    assert result.i012_ka == result.iabc_ka == result.vabc_kv == (0j, 0j, 0j)


def test_zero_source_impedance_with_nonzero_fault_arm_is_finite():
    result = solve_fault(1, 0j, FaultSpec("3ph", 0.25))
    assert result.i012_ka == (0j, 4 + 0j, 0j)


def test_spec_and_result_are_frozen_and_trace_records_impedance_semantics():
    spec = FaultSpec("1ph_g", 0.2, 0.3)
    assert spec.kind is FaultType.LINE_GROUND
    result = solve_fault(1, 1j, spec, z2_ohm=2j, z0_ohm=3j)
    assert isinstance(result, FaultResult)
    assert result.node_id == result.mode_id == ""
    assert isinstance(result.steps, tuple) and result.steps
    assert isinstance(result.assumptions, tuple) and result.assumptions
    assert "Zф каждого плеча" in result.steps[0].given
    assert "3Zф + 3Zз" in result.steps[0].formula
    with pytest.raises(FrozenInstanceError):
        spec.kind = FaultType.THREE_PHASE
    with pytest.raises(FrozenInstanceError):
        result.iabc_ka = (0j, 0j, 0j)


@pytest.mark.parametrize("kind", ["unknown", None, True, []])
def test_invalid_fault_kind_rejected(kind):
    with pytest.raises(ShortCircuitStatusError) as caught:
        FaultSpec(kind)
    assert caught.value.code == "INVALID_INPUT"


def test_solver_requires_fault_spec():
    with pytest.raises(ShortCircuitStatusError) as caught:
        solve_fault(1, 1j, "3ph")
    assert caught.value.code == "INVALID_INPUT"
