"""Independent completeness checks: one failed required case must remain visible."""
from dataclasses import replace

import pytest

from rza_calc.core.context import Context
from rza_calc.core.engine import STALE, run, summary_table
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, LineBranch, Load, Mode, Network, Node, SourceBranch, TieBranch, ProtectionSettings
from rza_calc.core.protections.mtz import calc_mtz
from rza_calc.core.result import Check, FAIL, OK, UNRESOLVED, ProtectionResult


def network(*, load_kw=50):
    net = Network('Stage 01 completeness')
    for node in ('a', 'b', 'c'):
        net.add_node(Node(node, node, 10))
    net.add_branch(SourceBranch('S', 'S', GRID, 'a', s_kz_max=200, s_kz_min=150))
    net.add_branch(TieBranch('Q', 'Q', 'a', 'b', normally_closed=True,
                            ct_ratio=(100, 5), ct_node='a',
                            prot=ProtectionSettings(mtz=True, to=False)))
    net.add_branch(TieBranch('Q2', 'Q2', 'a', 'b', normally_closed=False))
    net.add_branch(LineBranch('F', 'F', 'b', 'c', length_km=1, r0=.1, x0=.1))
    net.add_load(Load('L', 'L', 'c', p_kw=load_kw, cos_phi=.9))
    net.add_mode(Mode('radial', 'radial', states={'Q2': False}, system='max'))
    net.add_mode(Mode('ring', 'ring', states={'Q2': True}, system='min'))
    return net


def test_ideal_ring_cannot_hide_behind_successful_radial_mode():
    from rza_calc.core.load_current import working_current
    net = network()
    meth = Methodology.load()
    successful = working_current(net, net.branches['Q'], net.modes['radial'], meth)
    result = run(net, meth).get('Q', 'МТЗ')
    assert result.i_primary is None and result.i_secondary is None
    assert successful.step in result.steps
    assert result.status == UNRESOLVED
    assert not result.is_complete
    # A setting cannot be chosen before every required working current exists;
    # sensitivity against a partial setting would be premature.
    row = next(c for c in result.coverage if c.name == 'Рабочий ток')
    assert (row.checked, row.required) == (1, 2)
    assert 'ring' in '\n'.join(row.problems)
    assert 'радиальная сумма нагрузок' in '\n'.join(row.problems)
    assert 'ПОЛНОТА ПРОВЕРКИ' in result.explain()
    assert '1 из 2' in result.explain()


def test_mode_with_failed_solver_is_still_required():
    net = network()
    ctx = Context(net, Methodology.load())
    ctx.solvers.pop('ring')
    ctx.errors['ring'] = 'test: matrix cannot be assembled'
    result = calc_mtz(ctx, net.branches['Q'])
    assert result.status == UNRESOLVED
    assert 'matrix cannot be assembled' in result.explain()
    assert any(row.required == 2 and row.checked == 1 for row in result.coverage)


def test_inactive_failed_mode_is_excluded_with_reason():
    net = network()
    net.modes['ring'].states['Q'] = False
    ctx = Context(net, Methodology.load())
    ctx.solvers.pop('ring')
    ctx.errors['ring'] = 'unused solver problem'
    result = calc_mtz(ctx, net.branches['Q'])
    assert result.status == OK
    assert result.is_complete
    assert any('ring' in message and 'отключено' in message for message in result.messages)


def test_empty_main_zone_in_one_mode_is_incomplete(monkeypatch):
    net = network()
    net.modes['ring'].states['Q2'] = False
    ctx = Context(net, Methodology.load())
    original = ctx.zone_points
    monkeypatch.setattr(ctx, 'zone_points', lambda br, mode: ([], []) if mode.id == 'ring' else original(br, mode))
    result = calc_mtz(ctx, net.branches['Q'])
    assert result.status == UNRESOLVED
    assert any('ring' in p and 'нет точек' in p for c in result.coverage for p in c.problems)


def test_backup_error_does_not_disappear_when_main_zone_passes(monkeypatch):
    net = network()
    net.modes['ring'].states['Q2'] = False
    ctx = Context(net, Methodology.load())
    original = ctx.current_through
    def current(mode, branch, node, which):
        if mode.id == 'ring' and node == 'c':
            raise ValueError('test: backup current unavailable')
        return original(mode, branch, node, which)
    monkeypatch.setattr(ctx, 'current_through', current)
    result = calc_mtz(ctx, net.branches['Q'])
    assert result.status == UNRESOLVED
    row = next(c for c in result.coverage if 'зоне резервирования' in c.name)
    assert (row.checked, row.required) == (1, 2)
    assert 'backup current unavailable' in result.explain()


def test_working_current_failure_keeps_successful_trace_without_partial_setting(monkeypatch):
    from rza_calc.core.protections import mtz
    net = network()
    net.modes['ring'].states['Q2'] = False
    original = mtz.working_current
    meth = Methodology.load()
    successful = original(net, net.branches['Q'], net.modes['radial'], meth)
    def current(net, br, mode, meth):
        if mode.id == 'ring':
            raise ValueError('test: working current unavailable')
        return original(net, br, mode, meth)
    monkeypatch.setattr(mtz, 'working_current', current)
    result = calc_mtz(Context(net, meth), net.branches['Q'])
    assert result.i_primary is None and result.i_secondary is None
    assert successful.step in result.steps
    assert result.status == UNRESOLVED
    assert not result.is_complete
    row = next(c for c in result.coverage if c.name == 'Рабочий ток')
    assert (row.checked, row.required) == (1, 2)
    assert 'ring' in '\n'.join(row.problems)
    assert 'working current unavailable' in result.explain()


@pytest.mark.parametrize('bad_value', [float('nan'), float('inf'), -1.0])
def test_invalid_fault_current_counts_as_unchecked(monkeypatch, bad_value):
    net = network()
    net.modes['ring'].states['Q2'] = False
    ctx = Context(net, Methodology.load())
    original = ctx.current_through
    def current(mode, branch, node, which):
        value, sc, share = original(mode, branch, node, which)
        return (bad_value if mode.id == 'ring' else value), sc, share
    monkeypatch.setattr(ctx, 'current_through', current)
    result = calc_mtz(ctx, net.branches['Q'])
    assert result.status == UNRESOLVED


def test_known_failure_and_incomplete_evidence_are_independent():
    result = ProtectionResult('Q', 'Q', 'МТЗ', i_primary=100)
    result.checks.append(Check('Kч', .5, 1.5, FAIL))
    result.record_coverage('sensitivity', 1, 2, ['second mode unavailable'])
    result.recompute_status()
    assert result.status == FAIL
    assert not result.is_complete
    assert any(check.status == UNRESOLVED for check in result.checks)


def test_coverage_counts_do_not_become_sensitivity_values():
    net = network()
    net.modes.pop('ring')
    project = run(net, Methodology.load())
    result = project.get('Q', 'МТЗ')
    result.record_coverage('many scenarios', 1, 200, ['test'])
    result.recompute_status()
    assert all(c.value is None for c in result.checks if c.name.startswith('Полнота:'))
    row = next(row for row in summary_table(project)[1:] if row[0] == 'Q' and row[1] == 'МТЗ')
    assert row[-1] == '?'
    assert row[-2] not in ('1', '200')


def test_result_from_another_algorithm_is_not_current():
    net = network()
    result = run(net, Methodology.load())
    result.calculation_case = replace(result.calculation_case, algorithm_version='previous-algorithm')
    assert result.freshness_for(net, Methodology.load()) == STALE


def test_reordering_modes_does_not_change_completeness_or_setting():
    net = network()
    before = run(net, Methodology.load()).get('Q', 'МТЗ')
    net.modes = dict(reversed(list(net.modes.items())))
    after = run(net, Methodology.load()).get('Q', 'МТЗ')
    assert before.i_primary == after.i_primary
    assert before.status == after.status == UNRESOLVED
    assert [(c.checked, c.required) for c in before.coverage] == [(c.checked, c.required) for c in after.coverage]


def test_project_and_cli_do_not_hide_a_failed_solver_without_protection_rows():
    from rza_calc.core.engine import ProjectResult
    from rza_calc.cli import exit_code, EXIT_UNRESOLVED
    from rza_calc.core.selectivity import report
    net = network()
    ctx = Context(net, Methodology.load())
    ctx.solvers.pop('ring')
    ctx.errors['ring'] = 'test: missing mode'
    result = ProjectResult(ctx)
    assert not result.is_complete
    assert exit_code(result, 'table') == EXIT_UNRESOLVED
    text = report(ctx, [])
    assert 'НЕПОЛНАЯ' in text and 'ring' in text
    assert 'во всех заданных режимах' not in text


def test_cli_does_not_hide_an_unresolved_selectivity_pair():
    from rza_calc.core.engine import ProjectResult
    from rza_calc.cli import exit_code, EXIT_UNRESOLVED
    from rza_calc.core.selectivity import Pair
    result = ProjectResult(Context(network(), Methodology.load()))
    result.pairs = [Pair('low', 'Low', 'high', 'High', 'radial', 'radial',
                         None, None, None, .4, UNRESOLVED)]
    assert not result.is_complete
    assert exit_code(result, 'table') == EXIT_UNRESOLVED
