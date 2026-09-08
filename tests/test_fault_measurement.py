"""Independent ideal-CT scaling and physical terminal selection oracles."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rza_calc.calculation.fault_measurement import branch_fault_measurements
from rza_calc.core.model import Branch


def _terminal(node, values, *, stage=10.5, residual=None, physical=True):
    return SimpleNamespace(node_id=node, voltage_stage_kv=stage,
        delta_iabc_ka=values, residual_current_ka=(sum(values) if residual is None and values else residual),
        is_physical=physical, diagnostics=())


def _network_branch(first=None, second=None):
    return SimpleNamespace(from_terminal=first or _terminal('H', (1+2j, -2+3j, 4-1j)),
        to_terminal=second or _terminal('L', (-10-20j, 20-30j, -40+10j), stage=.4))


@pytest.mark.parametrize('ratio,factor', [((600, 5), 25/3), ((1000, 1), 1), ((50, 5), 100)])
@pytest.mark.parametrize('side,index', [('H', 0), ('L', 1)])
def test_ct_uses_its_physical_terminal_and_exact_complex_ratio(ratio, factor, side, index):
    branch = Branch('T', 'T', 'H', 'L', ct_ratio=ratio, ct_node=side)
    rows = branch_fault_measurements(branch, _network_branch())
    primary, ct = rows[index], rows[-1]
    assert ct.kind == 'ct' and ct.unit == 'А'
    assert ct.node_id == side and ct.voltage_stage_kv == primary.voltage_stage_kv
    assert ct.iabc == pytest.approx(tuple(value*factor for value in primary.iabc))
    assert ct.residual_current == pytest.approx(sum(primary.iabc)*factor)
    assert 'Подтверждение ТТ не записано' in ct.status


def test_reversing_branch_does_not_move_ct_or_change_current_sign():
    branch = Branch('T', 'T', 'H', 'L', ct_ratio=(600, 5), ct_node='H')
    result = _network_branch()
    old = branch_fault_measurements(branch, result)[-1]
    reverse = replace(branch, node_from='L', node_to='H')
    swapped = SimpleNamespace(from_terminal=result.to_terminal, to_terminal=result.from_terminal)
    assert branch_fault_measurements(reverse, swapped)[-1] == old


@pytest.mark.parametrize('ratio,side,reason', [
    (None, None, 'ТТ не задан'), ((600, 5), None, 'Не задана сторона'),
    ((600, 5), 'foreign', 'не совпадает'), ((0, 5), 'H', 'положительные'),
    ((600, float('nan')), 'H', 'положительные'), ((True, 5), 'H', 'положительные'),
])
def test_missing_or_invalid_ct_never_defaults_to_unit_ratio(ratio, side, reason):
    branch = Branch('T', 'T', 'H', 'L', ct_ratio=ratio, ct_node=side)
    rows = branch_fault_measurements(branch, _network_branch())
    assert rows[0].iabc is not None
    assert rows[-1].iabc is None and rows[-1].residual_current is None
    assert reason in rows[-1].status


@pytest.mark.parametrize('key', ['ct_primary_a', 'ct_secondary_a', 'ct_port'])
def test_explicit_unconfirmed_ct_blocks_secondary_only(key):
    branch = Branch('T', 'T', 'H', 'L', ct_ratio=(600, 5), ct_node='H',
        parameter_provenance={key: {'confirmation':'unconfirmed', 'source':'Паспорт'}})
    rows = branch_fault_measurements(branch, _network_branch())
    assert rows[0].iabc is not None and rows[-1].iabc is None
    assert 'не подтвержд' in rows[-1].status
    branch.parameter_provenance[key]['confirmation'] = 'confirmed'
    assert branch_fault_measurements(branch, _network_branch())[-1].iabc is not None
    branch.parameter_provenance[key]['source'] = ' '
    assert branch_fault_measurements(branch, _network_branch())[-1].iabc is None


def test_partial_sequence_preserves_known_residual_without_inventing_phases():
    terminal = _terminal('H', None, residual=3+2j)
    terminal.diagnostics = (SimpleNamespace(message='Не определён ток идеального контура'),)
    rows = branch_fault_measurements(Branch('L', 'L', 'H', 'L', ct_ratio=(1000, 1), ct_node='H'),
                                     _network_branch(first=terminal))
    assert rows[0].iabc is None and rows[-1].iabc is None
    assert rows[-1].residual_current == 3+2j
    assert 'идеального контура' in rows[-1].status


def test_internal_grid_terminal_is_not_a_physical_ct_location():
    first = _terminal('GRID', None, physical=False, stage=None)
    result = _network_branch(first=first)
    rows = branch_fault_measurements(Branch('S', 'S', 'GRID', 'L', ct_ratio=(100, 5), ct_node='GRID'), result)
    assert len(rows) == 2 and rows[0].node_id == 'L'
    assert rows[-1].iabc is None and 'не совпадает' in rows[-1].status


def test_large_equal_ct_nominals_scale_without_intermediate_overflow():
    branch = Branch('T', 'T', 'H', 'L', ct_ratio=(1e308, 1e308), ct_node='H')
    rows = branch_fault_measurements(branch, _network_branch())
    assert rows[-1].iabc == tuple(value*1000 for value in rows[0].iabc)


def test_unrepresentable_ct_factor_is_unavailable_instead_of_fake_zero():
    branch = Branch('T', 'T', 'H', 'L', ct_ratio=(1e308, 1e-308), ct_node='H')
    row = branch_fault_measurements(branch, _network_branch())[-1]
    assert row.iabc is None and row.residual_current is None
    assert 'численный диапазон' in row.status
