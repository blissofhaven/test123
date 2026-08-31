# -*- coding: utf-8 -*-
"""Минимальный независимый Ybus-решатель для контрольных примеров этапа 4.4.

Модуль намеренно не импортирует ``rza_calc.core``, production-адаптер или
объектную модель проекта. Источники в расчёте собственного сопротивления
деактивированы, поэтому их идеальные ЭДС представлены узлом ``GROUND``.
Направление тока ветви считается положительным от ``node_from`` к ``node_to``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np


GROUND = "__reference_ground__"
SQRT3 = math.sqrt(3.0)


@dataclass(frozen=True, slots=True)
class ReferenceBranch:
    """Одна комплексная последовательная ветвь в именованных единицах."""

    id: str
    node_from: str
    node_to: str
    impedance_ohm: complex

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ID эталонной ветви не должен быть пустым.")
        if self.node_from == self.node_to:
            raise ValueError("Эталонная ветвь должна соединять разные узлы.")
        z = complex(self.impedance_ohm)
        if not all(math.isfinite(value) for value in (z.real, z.imag)):
            raise ValueError("Сопротивление эталонной ветви должно быть конечным.")
        if abs(z) == 0.0:
            raise ValueError("Нулевые ветви в эталоне объединяются до сборки Ybus.")


class ReferenceYBusSolver:
    """Независимое решение ``Y · U = I`` с помощью ``numpy.linalg.solve``."""

    def __init__(
        self,
        nodes: Iterable[str],
        branches: Iterable[ReferenceBranch],
    ) -> None:
        ordered = tuple(nodes)
        if GROUND in ordered:
            raise ValueError("GROUND является служебным узлом и не входит в nodes.")
        if len(ordered) != len(set(ordered)):
            raise ValueError("Эталонные узлы должны иметь уникальные ID.")
        self.nodes = ordered
        self.branches = tuple(branches)
        self._index = {node_id: index for index, node_id in enumerate(ordered)}
        self.ybus = self._build_ybus()

    def _build_ybus(self) -> np.ndarray:
        ybus = np.zeros((len(self.nodes), len(self.nodes)), dtype=complex)
        known = set(self.nodes) | {GROUND}
        for branch in self.branches:
            if branch.node_from not in known or branch.node_to not in known:
                raise KeyError(f"Ветвь '{branch.id}' ссылается на неизвестный узел.")
            admittance = 1.0 / branch.impedance_ohm
            a = self._index.get(branch.node_from)
            b = self._index.get(branch.node_to)
            if a is not None:
                ybus[a, a] += admittance
            if b is not None:
                ybus[b, b] += admittance
            if a is not None and b is not None:
                ybus[a, b] -= admittance
                ybus[b, a] -= admittance
        return ybus

    @property
    def condition_number(self) -> float:
        """Двухнормовая оценка обусловленности собранной матрицы."""

        return float(np.linalg.cond(self.ybus))

    def transfer_impedances(self, fault_node: str) -> dict[str, complex]:
        """Столбец Zbus для единичного тока, введённого в точке КЗ."""

        try:
            fault_index = self._index[fault_node]
        except KeyError as exc:
            raise KeyError(f"Неизвестная эталонная точка КЗ '{fault_node}'.") from exc
        injection = np.zeros(len(self.nodes), dtype=complex)
        injection[fault_index] = 1.0 + 0.0j
        try:
            voltages = np.linalg.solve(self.ybus, injection)
        except np.linalg.LinAlgError as exc:
            raise ValueError("Эталонная матрица Ybus вырождена.") from exc
        if not np.all(np.isfinite(voltages)):
            raise ValueError("Эталонный решатель получил NaN или бесконечность.")
        return {
            node_id: complex(voltages[index])
            for node_id, index in self._index.items()
        }

    def thevenin_impedance(self, fault_node: str) -> complex:
        return self.transfer_impedances(fault_node)[fault_node]

    def fault_current_ka(self, fault_node: str, line_voltage_kv: float) -> float:
        """Действующее значение трёхфазного металлического КЗ, кА."""

        if not math.isfinite(line_voltage_kv) or line_voltage_kv <= 0.0:
            raise ValueError("Линейное напряжение должно быть конечным и положительным.")
        z_th = self.thevenin_impedance(fault_node)
        if abs(z_th) == 0.0:
            raise ValueError("Эквивалентное сопротивление равно нулю.")
        return line_voltage_kv / (SQRT3 * abs(z_th))

    def branch_transfer_factor(self, branch_id: str, fault_node: str) -> complex:
        """Комплексный ток ветви при единичном токе в точке КЗ."""

        branch = next((item for item in self.branches if item.id == branch_id), None)
        if branch is None:
            raise KeyError(f"Неизвестная эталонная ветвь '{branch_id}'.")
        voltages = self.transfer_impedances(fault_node)
        voltage_from = 0.0j if branch.node_from == GROUND else voltages[branch.node_from]
        voltage_to = 0.0j if branch.node_to == GROUND else voltages[branch.node_to]
        return (voltage_from - voltage_to) / branch.impedance_ohm


def per_unit_fault_current_ka(
    *, base_power_mva: float, fault_voltage_kv: float, total_impedance_pu: complex
) -> float:
    """Независимый пересчёт тока из относительных единиц."""

    values = (base_power_mva, fault_voltage_kv)
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise ValueError("Базисные мощность и напряжение должны быть положительными.")
    if not (
        math.isfinite(total_impedance_pu.real)
        and math.isfinite(total_impedance_pu.imag)
    ):
        raise ValueError("Суммарное сопротивление в о.е. должно быть конечным.")
    if abs(total_impedance_pu) == 0.0:
        raise ValueError("Суммарное сопротивление в о.е. равно нулю.")
    base_current_ka = base_power_mva / (SQRT3 * fault_voltage_kv)
    return base_current_ka / abs(total_impedance_pu)
