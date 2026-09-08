"""Fault point, measured branch and CT side are independent user selections."""
from copy import copy
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest

from rza_calc.core.fault_types import FaultType
from rza_calc.core.model import Branch
from rza_calc.core.short_circuit import ShortCircuitStatusError
from rza_calc.gui.view_model import ProjectViewModel
from test_four_fault_ui_cli import make_vm


def _vm():
    vm, solver = make_vm()
    net = vm.net
    net.nodes['end'] = SimpleNamespace(id='end', name='Дальний узел', u_nom=.4)
    net.branches = {identity: Branch(identity, identity, 'bus', 'end', ct_ratio=(1000, 1), ct_node=side)
                    for identity, side in [('L1', 'bus'), ('L2', 'end')]}
    queries = []
    def network_at(node, spec):
        queries.append((node, spec.kind))
        if solver.error:
            raise solver.error
        k = list(FaultType).index(spec.kind) + 1
        def terminal(node, factor, stage):
            values = (k*factor*(1+1j), k*factor*(-1+2j), k*factor*(2-1j))
            return SimpleNamespace(node_id=node, voltage_stage_kv=stage, delta_iabc_ka=values,
                residual_current_ka=sum(values), is_physical=True, diagnostics=())
        branch = SimpleNamespace(from_terminal=terminal('bus', 1, 10.5),
                                 to_terminal=terminal('end', -10, .4))
        return SimpleNamespace(branches={key:branch for key in net.branches}, assumptions=(), diagnostics=())
    solver.fault_network_at = network_at
    return vm, solver, queries


def test_fault_network_is_lazy_and_branch_selection_reuses_one_result():
    vm, solver, queries = _vm()
    vm.fault_rows()
    assert queries == []
    original = vm.result
    for kind in FaultType:
        vm.select_fault_type(kind)
        first = vm.fault_branch_view('L1')
        second = vm.fault_branch_view('L2')
        assert first.rows[-1].node_id == 'bus' and second.rows[-1].node_id == 'end'
        assert second.rows[-1].iabc == tuple(-10*x for x in first.rows[-1].iabc)
        assert vm.selected_fault_node() == 'bus'
    assert len(queries) == 4
    vm.select_fault_type(FaultType.THREE_PHASE)
    vm.fault_branch_view('L1')
    assert len(queries) == 4 and vm.result is original
    vm.selected_id = 'end'
    vm.fault_branch_view('L2')
    assert len(queries) == 5
    vm.net.modes['min'] = SimpleNamespace(id='min', name='Минимальный', system='min')
    vm.result.ctx.solvers['min'] = solver
    vm.mode_id = 'min'
    assert 'Минимальный' in vm.fault_branch_view().title
    assert len(queries) == 6


@pytest.mark.parametrize('reason', ['stale', 'draft', 'busy'])
def test_no_measurement_escapes_freshness_draft_or_busy_guard(reason):
    vm, _, queries = _vm()
    old = vm.fault_branch_view()
    assert old.rows
    if reason == 'stale':
        vm.result.is_current_for = lambda *_: False
    elif reason == 'draft':
        vm.mode_draft = object()
    else:
        vm.calculation_busy = True
    unavailable = vm.fault_branch_view('L2')
    assert not unavailable.rows and unavailable.status
    assert len(queries) == 1 and '_fault_network_cache' not in vm.__dict__


def test_successful_result_replacement_invalidates_cached_network_and_errors():
    vm, solver, queries = _vm()
    solver.error = ShortCircuitStatusError('MISSING_SEQUENCE_DATA', 'Линия L1: нужны R0 и X0')
    failed = vm.fault_branch_view()
    assert not failed.rows and 'Нет исходных данных' in failed.status and 'R0' in failed.status
    vm.fault_branch_view()
    assert len(queries) == 1
    vm.result = copy(vm.result)
    solver.error = None
    assert vm.fault_branch_view().rows
    assert len(queries) == 2


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_qt_branch_selector_is_independent_and_displays_phasors_units_and_failure(app):
    from rza_calc.gui.main_window import BottomPanel
    vm, solver, queries = _vm()
    panel = BottomPanel(vm)
    panel.resize(1250, 530)
    panel.show()
    try:
        panel.faultTypeSelected.connect(vm.select_fault_type)
        assert not queries
        panel.fault_views.setCurrentIndex(1)
        app.processEvents()
        table = panel.fault_measurement_table
        assert table.rowCount() == 3
        assert [table.item(i, 2).text() for i in range(3)] == ['кА', 'кА', 'А']
        assert '(' in table.item(0, 3).text() and '°' in table.item(0, 3).text()
        for kind in FaultType:
            panel.fault_type_combo.setCurrentIndex(panel.fault_type_combo.findData(kind.value))
            panel.refresh(vm)
            panel.fault_branch_combo.setCurrentIndex(panel.fault_branch_combo.findData('L2'))
            assert 'end' in table.item(2, 0).text()
            assert vm.selected_fault_node() == 'bus'
            assert vm.fault_type_label() in panel.fault_branch_title.text()
            assert 'насыщение' in panel.fault_measurement_details.toPlainText()
        assert len(queries) == 4
        vm.result = copy(vm.result)
        solver.error = ShortCircuitStatusError('MISSING_SEQUENCE_DATA', 'L1: нужны R0 и X0')
        panel.refresh(vm)
        assert table.rowCount() == 0
        assert not panel.fault_branch_combo.isEnabled()
        assert 'L1: нужны R0 и X0' in panel.fault_measurement_details.toPlainText()
    finally:
        panel.close()
        app.processEvents()


def test_real_four_fault_project_measurement_reuses_snapshot_and_hides_after_edit(monkeypatch):
    from test_stage01_result_freshness import opened, change_length
    vm, controller = opened()
    original = vm.result
    solver = original.ctx.solvers[vm.mode_id]
    calls = []
    solve = solver.fault_network_at
    def counted(*args):
        calls.append(args)
        return solve(*args)
    monkeypatch.setattr(solver, 'fault_network_at', counted)
    first = vm.fault_branch_view()
    assert first.rows, first.status
    for branch, _ in first.choices:
        assert vm.fault_branch_view(branch).rows
    assert len(calls) == 1 and vm.result is original
    change_length(controller)
    assert vm.fault_branch_view().rows == ()
    assert vm.recalculate()
    assert vm.fault_branch_view().rows
    assert vm.result is not original
