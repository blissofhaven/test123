"""Read-only physical-terminal and ideal-CT views of a fault contribution.

No relay setting or CT saturation model is applied here. The input terminal
phasors already belong to the physical voltage stage; only a declared CT
ratio converts kA primary to A secondary. Historical metadata absence is
reported, while an explicitly unconfirmed input prevents secondary values.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from numbers import Real
from typing import TYPE_CHECKING

from ..core.model import Branch

if TYPE_CHECKING:
    from ..core.fault_network import BranchFaultResult


MEASUREMENT_ASSUMPTIONS = (
    'ΔI — составляющая тока от КЗ, без доаварийного нагрузочного тока. '
    'Направление каждого конца: из узла в оборудование; фазы A, B, C.',
    'ТТ: идеальное масштабирование по номинальным токам. Полярность вторичных '
    'цепей, класс точности, насыщение и схема соединения ТТ не рассчитываются.',
    'Эти токи не заменяют автоматически исходные данные действующих расчётов уставок и защит.',
)


@dataclass(frozen=True)
class FaultMeasurementRow:
    kind: str
    label: str
    node_id: str | None
    voltage_stage_kv: float | None
    unit: str
    iabc: tuple[complex, complex, complex] | None
    residual_current: complex | None
    status: str = ''


def _ct_input_problem(branch: Branch) -> str:
    metadata = branch.parameter_provenance
    if not isinstance(metadata, Mapping):
        return 'Повреждены сведения о подтверждении параметров ТТ.'
    labels = {'ct_primary_a': 'Первичный ток ТТ', 'ct_secondary_a': 'Вторичный ток ТТ',
              'ct_port': 'Сторона установки ТТ'}
    problems = []
    for key, label in labels.items():
        if key not in metadata:
            continue
        row = metadata[key]
        if (not isinstance(row, Mapping) or row.get('confirmation') != 'confirmed'
                or not isinstance(row.get('source'), str) or not row['source'].strip()):
            problems.append(f'{label} не подтверждён: укажите источник в карточке оборудования.')
    return ' '.join(problems)


def branch_fault_measurements(branch: Branch, result: BranchFaultResult) -> tuple[FaultMeasurementRow, ...]:
    """Show both physical ends and the explicitly specified CT installation.

    A missing/ambiguous installation never inherits the apparent power-flow
    direction. A known residual remains available when other sequences are
    indeterminate, but incomplete phase currents remain None.
    """
    rows = []
    terminals = []
    for label, terminal in (('Начало', result.from_terminal), ('Конец', result.to_terminal)):
        if not terminal.is_physical:
            continue
        terminals.append(terminal)
        messages = ' '.join(item.message for item in terminal.diagnostics)
        rows.append(FaultMeasurementRow('terminal', f'{label} — {terminal.node_id}',
            terminal.node_id, terminal.voltage_stage_kv, 'кА', terminal.delta_iabc_ka,
            terminal.residual_current_ka, messages or ('Рассчитано' if terminal.delta_iabc_ka is not None
                                                     else 'Фазные токи не определены')))

    def unavailable(message):
        return tuple(rows) + (FaultMeasurementRow('ct', 'ТТ', branch.ct_node, None, 'А', None, None, message),)

    ratio = branch.ct_ratio
    if ratio is None and branch.ct_node is None:
        return unavailable('ТТ не задан: укажите номинальные токи и сторону в карточке оборудования.')
    if (not isinstance(ratio, (tuple, list)) or len(ratio) != 2
            or any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) or v <= 0 for v in ratio)):
        return unavailable('Для ТТ нужны конечные положительные первичный и вторичный номинальные токи.')
    if not branch.ct_node:
        return unavailable('Не задана сторона установки ТТ.')
    matching = [terminal for terminal in terminals if terminal.node_id == branch.ct_node]
    if len(matching) != 1:
        return unavailable('Сторона ТТ не совпадает с единственным физическим концом этой ветви.')
    if problem := _ct_input_problem(branch):
        return unavailable(problem)
    terminal = matching[0]
    factor = (float(ratio[1]) / float(ratio[0])) * 1000.0
    if not math.isfinite(factor) or factor <= 0:
        return unavailable('Масштабирование ТТ выходит за численный диапазон.')
    currents = None if terminal.delta_iabc_ka is None else tuple(value * factor for value in terminal.delta_iabc_ka)
    residual = None if terminal.residual_current_ka is None else terminal.residual_current_ka * factor
    if any(not math.isfinite(abs(v)) for v in (*(() if currents is None else currents),
                                             *(() if residual is None else (residual,)))):
        return unavailable('Масштабирование ТТ выходит за численный диапазон.')
    status = ['Идеальное масштабирование']
    if not all(key in branch.parameter_provenance for key in ('ct_primary_a', 'ct_secondary_a', 'ct_port')):
        status.append('Подтверждение ТТ не записано для всех параметров')
    status.extend(item.message for item in terminal.diagnostics)
    if currents is None and not terminal.diagnostics:
        status.append('Фазные токи не определены')
    rows.append(FaultMeasurementRow('ct', f'ТТ {ratio[0]:g}/{ratio[1]:g} А — {branch.ct_node}',
        branch.ct_node, terminal.voltage_stage_kv, 'А', currents, residual, '. '.join(status)))
    return tuple(rows)
