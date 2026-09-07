"""Sequence networks for an explicitly selected fault, in a common kV base.

The legacy positive-sequence solver remains a compatibility API for existing
protection calculations. This module does not reuse its scalar distribution
factors for unbalanced faults. Zero impedances supplied here include 3*Zn.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, replace

import numpy as np

from .impedance import branch_impedance, refer, stage_voltage
from .model import GRID, LineBranch, TieBranch, TransformerBranch
from .short_circuit import NodeNotEnergizedError, ShortCircuitStatusError, _Union


@dataclass(frozen=True)
class _Stamp:
    branch_id: str
    first: str
    second: str
    impedance: complex
    tap: complex = 1 + 0j


def _number(value, label, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShortCircuitStatusError("INVALID_INPUT", f"{label}: требуется число.")
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise ShortCircuitStatusError("INVALID_INPUT", f"{label}: недопустимое значение.")
    return float(value)


class SequenceFaultSolver:
    """Compile only the energized physical component containing the fault.

    A blocked zero-sequence component is distinct from missing parameters and
    from a singular numerical matrix. A capacitive earth-fault model is not
    fabricated for a component without a grounded zero-sequence return path.
    """

    def __init__(self, positive_solver):
        self.positive = positive_solver
        self.net = positive_solver.net
        self.mode = positive_solver.mode
        self.methodology = positive_solver.m
        self.base = positive_solver.u_base
        self.threshold = positive_solver.zero_threshold
        self.assumptions = []

    def _missing(self, branch, text):
        raise ShortCircuitStatusError(
            "MISSING_SEQUENCE_DATA",
            f"«{branch.name}», режим «{self.mode.name}»: {text}",
        )

    def _component(self, node_id):
        if node_id not in self.positive._rep:
            raise NodeNotEnergizedError(node_id, self.mode.name)
        active = list(self.net.active_branches(self.mode))
        adjacency = {}
        for branch in active:
            if GRID in (branch.node_from, branch.node_to):
                continue
            adjacency.setdefault(branch.node_from, set()).add(branch.node_to)
            adjacency.setdefault(branch.node_to, set()).add(branch.node_from)
        nodes, todo = {node_id}, [node_id]
        while todo:
            for other in adjacency.get(todo.pop(), ()):
                if other not in nodes:
                    nodes.add(other)
                    todo.append(other)
        return nodes, [b for b in active if
                       (b.node_from in nodes or b.node_from == GRID) and
                       (b.node_to in nodes or b.node_to == GRID) and
                       (b.node_from in nodes or b.node_to in nodes)]

    def _impedance(self, branch, sequence):
        if sequence == 1:
            value, _ = branch_impedance(self.net, branch, self.methodology,
                                        self.base, self.mode.system)
            return 0j if value is None else value
        if sequence == 2 and branch.negative_sequence_equal_positive:
            if not isinstance(branch.negative_sequence_equal_positive, bool):
                raise ShortCircuitStatusError("INVALID_INPUT", "Признак Z2=Z1 должен быть логическим.")
            if any(getattr(branch, key, None) is not None for key in
                   ("r2_ohm", "x2_ohm", "r2_ohm_per_km", "x2_ohm_per_km")):
                raise ShortCircuitStatusError(
                    "INVALID_INPUT", f"«{branch.name}»: одновременно заданы Z2 и допущение Z2=Z1.")
            self.assumptions.append(f"«{branch.name}»: явно принято Z2=Z1.")
            return self._impedance(branch, 1)
        if not isinstance(branch.negative_sequence_equal_positive, bool):
            raise ShortCircuitStatusError("INVALID_INPUT", "Признак Z2=Z1 должен быть логическим.")
        r_key, x_key = f"r{sequence}_ohm", f"x{sequence}_ohm"
        r, x = getattr(branch, r_key), getattr(branch, x_key)
        if isinstance(branch, LineBranch):
            rp, xp = getattr(branch, r_key + "_per_km"), getattr(branch, x_key + "_per_km")
            if rp is not None or xp is not None:
                if r is not None or x is not None:
                    raise ShortCircuitStatusError("INVALID_INPUT", f"«{branch.name}»: одновременно заданы удельное и полное Z{sequence}.")
                if rp is None or xp is None:
                    self._missing(branch, f"нужна полная пара R{sequence}/X{sequence} в Ом/км.")
                rp = _number(rp, f"«{branch.name}», R{sequence}", nonnegative=True)
                xp = _number(xp, f"«{branch.name}», X{sequence}")
                length = _number(branch.length_km, f"«{branch.name}», длина", positive=True)
                if isinstance(branch.n_parallel, bool) or not isinstance(branch.n_parallel, int) or branch.n_parallel < 1:
                    raise ShortCircuitStatusError("INVALID_INPUT", "Число параллельных цепей должно быть целым и > 0.")
                if self.mode.system == "min":
                    rp *= self.methodology.k("short_circuit.temp_factor_min")
                return refer(complex(rp, xp) * length / branch.n_parallel,
                             stage_voltage(self.net.node(branch.node_from), self.methodology), self.base)
        if r is None or x is None:
            self._missing(branch, f"не заданы {r_key}, {x_key} и опорное напряжение sequence_reference_kv.")
        r = _number(r, f"«{branch.name}», R{sequence}", nonnegative=True)
        x = _number(x, f"«{branch.name}», X{sequence}")
        if branch.sequence_reference_kv is None:
            self._missing(branch, "задайте опорное напряжение сопротивлений sequence_reference_kv в кВ.")
        reference = _number(branch.sequence_reference_kv, "Опорное напряжение", positive=True)
        self.assumptions.append(f"«{branch.name}»: явно заданное Z{sequence} общее для режимов max/min.")
        return refer(complex(r, x), reference, self.base)

    def _stamps(self, branches, sequence, *, asymmetric):
        stamps = []
        for branch in branches:
            first, second = branch.node_from, branch.node_to
            if isinstance(branch, TieBranch):
                stamps.append(_Stamp(branch.id, first, second, 0j))
                continue
            connection = "series"
            if sequence == 0:
                connection = branch.zero_sequence_connection
                if connection is None and isinstance(branch, LineBranch):
                    connection = "series"
                if connection is None:
                    self._missing(branch, "задайте схему нулевой последовательности zero_sequence_connection; Z0 включает 3Zn.")
                if connection not in ("series", "from_ground", "to_ground", "blocked"):
                    raise ShortCircuitStatusError("INVALID_INPUT", f"«{branch.name}»: неизвестная схема нулевой последовательности.")
                if connection == "blocked":
                    continue
                if connection == "from_ground":
                    second = GRID
                elif connection == "to_ground":
                    first, second = branch.node_to, GRID
                if first == second:
                    raise ShortCircuitStatusError("INVALID_INPUT", f"«{branch.name}»: шунт нулевой последовательности не имеет внешнего узла.")
                self.assumptions.append(f"«{branch.name}»: Z0 включает нейтральное сопротивление 3Zn; схема {connection}.")
            phase = branch.sequence_phase_shift_deg
            tap = 1 + 0j
            if isinstance(branch, TransformerBranch):
                if asymmetric and phase is None:
                    self._missing(branch, "задайте сдвиг положительной последовательности sequence_phase_shift_deg, включая явный 0°.")
                if phase is not None:
                    phase = _number(phase, "Сдвиг фаз трансформатора")
                    # Tap = V_from/V_to; the negative-sequence shift is opposite.
                    if sequence != 0:
                        tap = cmath.exp(-1j * math.radians(phase) * (1 if sequence == 1 else -1))
            elif phase not in (None, 0):
                raise ShortCircuitStatusError("INVALID_INPUT", f"«{branch.name}»: сдвиг фаз допустим только у трансформатора.")
            impedance = self._impedance(branch, sequence)
            if not math.isfinite(impedance.real) or not math.isfinite(impedance.imag):
                raise ShortCircuitStatusError("NUMERIC_FAILURE", f"«{branch.name}»: Z{sequence} содержит NaN/Infinity.")
            stamps.append(_Stamp(branch.id, first, second, impedance, tap))
        return stamps

    def _zero_component_branches(self, branches, node_id):
        """Find relevant Z0 terminals before requesting their impedances.

        Ground is a reference, not a connection through which a fault can
        reach unrelated grounded islands. Unknown models are conservatively
        considered through paths here, then diagnosed by _stamps if relevant.
        """
        adjacency, terminals = {}, []
        for branch in branches:
            first, second = branch.node_from, branch.node_to
            connection = "series" if isinstance(branch, TieBranch) else branch.zero_sequence_connection
            if connection == "blocked":
                continue
            if connection == "from_ground":
                second = GRID
            elif connection == "to_ground":
                first, second = branch.node_to, GRID
            terminals.append((branch, first, second))
            if GRID not in (first, second):
                adjacency.setdefault(first, set()).add(second)
                adjacency.setdefault(second, set()).add(first)
        reached, todo = {node_id}, [node_id]
        while todo:
            for other in adjacency.get(todo.pop(), ()):
                if other not in reached:
                    reached.add(other)
                    todo.append(other)
        return [branch for branch, a, b in terminals if a in reached or b in reached]

    @staticmethod
    def _passive_driving_impedance(value):
        # Solving a passive, lossless complex matrix can leave a negative real
        # roundoff residue. Only remove a residue relative to the impedance;
        # significant negative resistance remains an explicit numerical error.
        if value.real < 0:
            if -value.real > abs(value) * 1e-12:
                raise ShortCircuitStatusError("NUMERIC_FAILURE", "Эквивалент пассивной сети имеет отрицательное активное сопротивление.")
            return complex(0, value.imag)
        return value

    def _driving_impedance(self, nodes, stamps, node_id, sequence):
        union = _Union(list(nodes | {GRID}))
        for stamp in stamps:
            if abs(stamp.impedance) <= self.threshold:
                if abs(stamp.tap - 1) > 1e-12:
                    raise ShortCircuitStatusError("UNSUPPORTED_IDEAL_PHASE_SHIFT", "Идеальный трансформатор со сдвигом фаз требует отдельного ограничения напряжений.")
                union.union(stamp.first, stamp.second)
        target, ground = union.find(node_id), union.find(GRID)
        if target == ground:
            return 0j
        adjacency = {}
        for stamp in stamps:
            a, b = union.find(stamp.first), union.find(stamp.second)
            if a != b:
                adjacency.setdefault(a, set()).add(b)
                adjacency.setdefault(b, set()).add(a)
        connected, todo = {target}, [target]
        while todo:
            for other in adjacency.get(todo.pop(), ()):
                if other not in connected:
                    connected.add(other)
                    todo.append(other)
        if ground not in connected:
            code = "NO_ZERO_SEQUENCE_RETURN_PATH" if sequence == 0 else "NO_SEQUENCE_RETURN_PATH"
            raise ShortCircuitStatusError(
                code,
                f"У точки КЗ нет замкнутого контура последовательности {sequence}. "
                "Для изолированной/компенсированной сети нужен отдельный учёт ёмкостей и заземления; нулевой ток не подставлен.")
        ids = sorted(connected - {ground})
        index = {node: i for i, node in enumerate(ids)}
        ybus = np.zeros((len(ids), len(ids)), dtype=complex)
        for stamp in stamps:
            a, b = union.find(stamp.first), union.find(stamp.second)
            if a not in connected or b not in connected:
                continue
            if a == b and (abs(stamp.tap - 1) <= 1e-12 or a == ground):
                continue
            admittance = 1 / stamp.impedance
            tap = stamp.tap
            if a != ground:
                ybus[index[a], index[a]] += admittance / abs(tap) ** 2
            if b != ground:
                ybus[index[b], index[b]] += admittance
            if a != ground and b != ground:
                ybus[index[a], index[b]] -= admittance / tap.conjugate()
                ybus[index[b], index[a]] -= admittance / tap
        if not np.isfinite(ybus).all():
            raise ShortCircuitStatusError("NUMERIC_FAILURE", "Матрица последовательности содержит NaN/Infinity.")
        current = np.zeros(len(ids), dtype=complex)
        current[index[target]] = 1
        try:
            voltage = np.linalg.solve(ybus, current)
        except np.linalg.LinAlgError as error:
            raise ShortCircuitStatusError("NUMERIC_FAILURE", f"Сеть последовательности {sequence} вырождена.") from error
        if not np.isfinite(voltage).all() or not np.allclose(ybus @ voltage, current, rtol=1e-8, atol=1e-8):
            raise ShortCircuitStatusError("NUMERIC_FAILURE", f"Сеть последовательности {sequence}: не выполнен баланс токов.")
        return complex(voltage[index[target]])

    def fault_at(self, node_id, spec):
        from .fault_types import FaultSpec, FaultType, solve_fault
        if not isinstance(spec, FaultSpec):
            raise ShortCircuitStatusError("INVALID_INPUT", "Нужен явно заданный FaultSpec.")
        nodes, branches = self._component(node_id)
        node = self.net.node(node_id)
        stage = stage_voltage(node, self.methodology)
        prefault = node.prefault_voltage_kv if node.prefault_voltage_kv is not None else stage
        _number(prefault, "Напряжение до КЗ", positive=True)
        asymmetric = spec.kind is not FaultType.THREE_PHASE
        # Preserve exact legacy Z1 in the existing no-phase-shift convention.
        has_shift = any(getattr(b, "sequence_phase_shift_deg", None) not in (None, 0) for b in branches)
        if not asymmetric and not has_shift:
            z1 = self.positive.z_th(node_id)
        else:
            z1 = self._driving_impedance(nodes, self._stamps(branches, 1, asymmetric=asymmetric), node_id, 1)
        z2 = z0 = None
        if asymmetric:
            z2 = self._driving_impedance(nodes, self._stamps(branches, 2, asymmetric=True), node_id, 2)
        if spec.kind in (FaultType.LINE_GROUND, FaultType.LINE_LINE_GROUND):
            zero_branches = self._zero_component_branches(branches, node_id)
            z0 = self._driving_impedance(nodes, self._stamps(zero_branches, 0, asymmetric=True), node_id, 0)
            self.assumptions.append("Продольные эквиваленты Z0: ёмкости, взаимная индукция линий и динамические составляющие не добавляются автоматически.")
        scale = (stage / self.base) ** 2
        z1 = self._passive_driving_impedance(z1)
        z2 = None if z2 is None else self._passive_driving_impedance(z2)
        z0 = None if z0 is None else self._passive_driving_impedance(z0)
        result = solve_fault(prefault / math.sqrt(3), z1 * scale, spec,
                             z2_ohm=None if z2 is None else z2 * scale,
                             z0_ohm=None if z0 is None else z0 * scale)
        notes = tuple(dict.fromkeys(self.assumptions + [
            "Расчёт в точке КЗ при заданном симметричном напряжении до повреждения; подпитка двигателей не учтена.",
            "Токи через ТТ и действующие расчёты защит этим результатом автоматически не заменяются.",
        ]))
        return replace(result, node_id=node_id, mode_id=self.mode.id,
                       assumptions=result.assumptions + notes)
