# -*- coding: utf-8 -*-
"""Four shunt faults at a point with explicitly supplied sequence impedances.

Inputs are phase EMF in kV and impedances in ohms on the SAME voltage stage;
the resulting currents are kA. Sequence tuples are ordered (0, 1, 2), phase
tuples (A, B, C), with positive phase order A-B-C. The canonical faults are
ABC, BC, AG and BCG. This module does not infer network or transformer data.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from enum import Enum
from numbers import Complex

from .short_circuit import ShortCircuitStatusError
from .trace import Step, fmt


class FaultType(str, Enum):
    THREE_PHASE = "3ph"
    LINE_LINE = "2ph"
    LINE_GROUND = "1ph_g"
    LINE_LINE_GROUND = "2ph_g"


def _number(value: complex, name: str, *, impedance: bool = False) -> complex:
    if isinstance(value, bool) or not isinstance(value, Complex):
        raise ShortCircuitStatusError("INVALID_INPUT", f"{name}: требуется число.")
    try:
        result = complex(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShortCircuitStatusError("INVALID_INPUT", f"{name}: некорректное число.") from exc
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise ShortCircuitStatusError("INVALID_INPUT", f"{name}: NaN/Infinity недопустимы.")
    if impedance and result.real < 0:
        raise ShortCircuitStatusError("INVALID_INPUT", f"{name}: активное сопротивление должно быть ≥ 0.")
    return result


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """Fault impedance is a star, not one ambiguously defined resistance.

    ``phase_arm_impedance_ohm`` is the identical impedance from EACH faulted
    phase to the common fault point. For BC, its phase-to-phase value is
    therefore twice this field. ``common_ground_impedance_ohm`` connects that
    common point to earth for AG/BCG; it must be zero for ABC/BC.
    """

    kind: FaultType
    phase_arm_impedance_ohm: complex = 0j
    common_ground_impedance_ohm: complex = 0j

    def __post_init__(self) -> None:
        try:
            kind = FaultType(self.kind)
        except (TypeError, ValueError) as exc:
            raise ShortCircuitStatusError("INVALID_INPUT", f"Неизвестный вид КЗ: {self.kind!r}.") from exc
        arm = _number(self.phase_arm_impedance_ohm, "Zф повреждения", impedance=True)
        ground = _number(self.common_ground_impedance_ohm, "Zз повреждения", impedance=True)
        if ground != 0 and kind in (FaultType.THREE_PHASE, FaultType.LINE_LINE):
            raise ShortCircuitStatusError(
                "INVALID_INPUT", "Общее сопротивление земли применимо только к КЗ AG/BCG."
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "phase_arm_impedance_ohm", arm)
        object.__setattr__(self, "common_ground_impedance_ohm", ground)


ComplexTriple = tuple[complex, complex, complex]


@dataclass(frozen=True, slots=True)
class FaultResult:
    spec: FaultSpec
    i012_ka: ComplexTriple
    iabc_ka: ComplexTriple
    v012_kv: ComplexTriple
    vabc_kv: ComplexTriple
    # None denotes an unprovided sequence that THIS fault did not require.
    z012_ohm: tuple[complex | None, complex, complex | None]
    residual_current_ka: complex
    steps: tuple[Step, ...] = ()
    assumptions: tuple[str, ...] = ()
    node_id: str = ""
    mode_id: str = ""


def _phases(values: ComplexTriple) -> ComplexTriple:
    zero, positive, negative = values
    a = complex(-0.5, math.sqrt(3.0) / 2.0)
    return (zero + positive + negative,
            zero + a.conjugate() * positive + a * negative,
            zero + a * positive + a.conjugate() * negative)


def _denominator(value: complex, terms: tuple[complex, ...]) -> complex:
    """Reject cancellation at machine precision, not small physical ohms.

    Impedances have already been scaled. A relative 64-epsilon guard leaves
    ordinary small impedances valid, while a cancelled sum cannot produce a
    precise-looking unbounded current. No denominator is clamped or replaced.
    """
    scale = sum(abs(term) for term in terms)
    if (not math.isfinite(abs(value)) or scale == 0
            or abs(value) <= 64.0 * sys.float_info.epsilon * scale):
        raise ShortCircuitStatusError(
            "NUMERIC_FAILURE", "Схема КЗ вырождена или знаменатель неотличим от нуля в пределах численной точности."
        )
    return value


def _current(e: complex, numerator: complex, denominator: complex, scale: float) -> complex:
    """Evaluate E*N/(D*scale) without overflowing a cancellable intermediate.

    Binary exponents are combined separately. For example, a huge physical
    impedance and a huge EMF can give an ordinary finite current, even when
    evaluating E/D first would overflow. No physical values are clamped.
    """
    if e == 0 or numerator == 0:
        return 0j
    e_scale = max(abs(e.real), abs(e.imag))
    n_scale = max(abs(numerator.real), abs(numerator.imag))
    d_scale = max(abs(denominator.real), abs(denominator.imag))
    unit = (e / e_scale) * (numerator / n_scale) / (denominator / d_scale)
    em, ee = math.frexp(e_scale)
    nm, ne = math.frexp(n_scale)
    dm, de = math.frexp(d_scale)
    sm, se = math.frexp(scale)
    fraction = em * nm / (dm * sm)
    exponent = ee + ne - de - se
    return complex(math.ldexp(unit.real * fraction, exponent),
                   math.ldexp(unit.imag * fraction, exponent))


def solve_fault(
    emf_phase_kv: complex,
    z1_ohm: complex,
    spec: FaultSpec,
    *,
    z2_ohm: complex | None = None,
    z0_ohm: complex | None = None,
) -> FaultResult:
    """Solve ABC, BC, AG or BCG without inventing Z2 or Z0.

    ``None`` means missing data, never an infinite physical impedance or an
    ungrounded network. A known open zero-sequence path requires a separate
    network-level limit treatment; it is not encoded here as Infinity.
    """
    if not isinstance(spec, FaultSpec):
        raise ShortCircuitStatusError("INVALID_INPUT", "Требуется типизированное описание FaultSpec.")
    e = _number(emf_phase_kv, "Фазная ЭДС")
    z1 = _number(z1_ohm, "Z1", impedance=True)
    z2 = None if z2_ohm is None else _number(z2_ohm, "Z2", impedance=True)
    z0 = None if z0_ohm is None else _number(z0_ohm, "Z0", impedance=True)
    required = []
    if spec.kind != FaultType.THREE_PHASE and z2 is None:
        required.append("Z2")
    if spec.kind in (FaultType.LINE_GROUND, FaultType.LINE_LINE_GROUND) and z0 is None:
        required.append("Z0")
    if required:
        raise ShortCircuitStatusError(
            "MISSING_SEQUENCE_DATA", "Для КЗ " + spec.kind.value + " не заданы " + ", ".join(required) + "."
        )

    zf = spec.phase_arm_impedance_ohm
    zg = spec.common_ground_impedance_ohm
    needed = [z1, zf, zg]
    if spec.kind != FaultType.THREE_PHASE:
        needed.append(z2)
    if spec.kind in (FaultType.LINE_GROUND, FaultType.LINE_LINE_GROUND):
        needed.append(z0)
    # Scale BEFORE adding or multiplying impedances; avoids overflow in the
    # double-ground denominator and underflow for physically small impedances.
    scale = max(max(abs(z.real), abs(z.imag)) for z in needed)
    if scale == 0:
        raise ShortCircuitStatusError("NUMERIC_FAILURE", "Все сопротивления схемы КЗ равны нулю.")
    n1, nf, ng = z1 / scale, zf / scale, zg / scale
    n2 = z2 / scale if spec.kind != FaultType.THREE_PHASE else 0j
    n0 = z0 / scale if spec.kind in (FaultType.LINE_GROUND, FaultType.LINE_LINE_GROUND) else 0j
    a, b, c = n1 + nf, n2 + nf, n0 + nf + 3.0 * ng
    try:
        if spec.kind == FaultType.THREE_PHASE:
            denominator = _denominator(a, (n1, nf))
            i1 = _current(e, 1 + 0j, denominator, scale)
            i0 = i2 = 0j
            formula = "I1 = E / (Z1 + Zф); I0 = I2 = 0"
        elif spec.kind == FaultType.LINE_LINE:
            denominator = _denominator(a + b, (n1, n2, 2.0 * nf))
            i1 = _current(e, 1 + 0j, denominator, scale)
            i0, i2 = 0j, -i1
            formula = "I1 = E / (Z1 + Z2 + 2Zф); I2 = -I1; I0 = 0"
        elif spec.kind == FaultType.LINE_GROUND:
            denominator = _denominator(a + b + c, (n1, n2, n0, 3.0 * nf, 3.0 * ng))
            i0 = i1 = i2 = _current(e, 1 + 0j, denominator, scale)
            formula = "I0 = I1 = I2 = E / (Z1 + Z2 + Z0 + 3Zф + 3Zз)"
        else:
            # The polynomial form remains valid when B+C=0 but the complete
            # fault equations are nonsingular; the parallel-fraction form does not.
            denominator = _denominator(a * b + a * c + b * c, (a * b, a * c, b * c))
            i1 = _current(e, b + c, denominator, scale)
            i2 = _current(e, -c, denominator, scale)
            i0 = _current(e, -b, denominator, scale)
            formula = (
                "A=Z1+Zф; B=Z2+Zф; C=Z0+Zф+3Zз; D=A·B+A·C+B·C; "
                "I1=E·(B+C)/D; I2=-E·C/D; I0=-E·B/D"
            )
        currents = (i0, i1, i2)
        voltages = (-(z0 * i0) if z0 is not None else 0j,
                    e - z1 * i1,
                    -(z2 * i2) if z2 is not None else 0j)
        phases = _phases(currents)
        phase_voltages = _phases(voltages)
        residual = sum(phases)
    except (OverflowError, ZeroDivisionError) as exc:
        raise ShortCircuitStatusError("NUMERIC_FAILURE", "Переполнение при расчёте КЗ.") from exc
    if any(not math.isfinite(value.real) or not math.isfinite(value.imag)
           for value in currents + voltages + phases + phase_voltages + (residual,)):
        raise ShortCircuitStatusError("NUMERIC_FAILURE", "Расчёт КЗ дал NaN/Infinity.")
    step = Step(
        what=f"Расчёт КЗ {spec.kind.value} методом симметричных составляющих",
        given={"Eф": f"{fmt(e)} кВ", "Z1": f"{fmt(z1)} Ом",
               "Z2": "не требуется / не задано" if z2 is None else f"{fmt(z2)} Ом",
               "Z0": "не требуется / не задано" if z0 is None else f"{fmt(z0)} Ом",
               "Zф каждого плеча": f"{fmt(zf)} Ом", "Zз общей точки": f"{fmt(zg)} Ом"},
        formula=formula,
        result="; ".join(f"I{phase} = {fmt(value)} кА" for phase, value in zip("ABC", phases)),
        source="Метод симметричных составляющих: граничные условия ABC, BC, AG, BCG.",
    )
    return FaultResult(
        spec, currents, phases, voltages, phase_voltages, (z0, z1, z2), residual,
        steps=(step,),
        assumptions=(
            "До повреждения сеть симметрична; ЭДС обратной и нулевой последовательностей равны нулю.",
            "Все сопротивления и фазная ЭДС заданы на одной расчётной ступени напряжения.",
            "Фазы повреждения: ABC, BC, AG или BCG соответственно виду КЗ; фазный порядок A-B-C.",
        ),
    )


__all__ = ["FaultType", "FaultSpec", "FaultResult", "solve_fault"]
