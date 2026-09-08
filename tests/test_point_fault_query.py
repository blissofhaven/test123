"""One selected mode/point: numerical oracles, no hidden protection calculation."""
from dataclasses import FrozenInstanceError, replace
import math
from types import SimpleNamespace
from pathlib import Path

import pytest

from rza_calc.adapters.legacy_calculation import import_legacy_network, adapt_to_calculation
from rza_calc.adapters.operating_parameters import attach_operating_parameters
from rza_calc.calculation.input import CalculationCancelled
from rza_calc.calculation.point_fault import (PointFaultTarget, make_point_fault_request,
    prepare_point_mode, run_point_faults, run_point_fault_network)
from rza_calc.core.fault_types import FaultSpec, FaultType
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, LineBranch, Mode, Network, Node, SourceBranch
from rza_calc.domain.electrical import DataConfirmation, ElectricalNodeId, EquipmentAvailability, PortId
from rza_calc.domain.operating_parameters import ModeValue, OperatingParameters, with_operating_parameters


def _project():
    net = Network("Point query")
    for key in ("a", "k"):
        net.add_node(Node(key, key, 10, calculation_base_kv=10))
    net.add_branch(SourceBranch("s", "s", GRID, "a", s_kz_max=100/abs(.2+.6j),
        s_kz_min=100/abs(.4+1.2j), x_r_ratio=3,
        r2_ohm=.3, x2_ohm=.7, r0_ohm=.6, x0_ohm=1.5,
        sequence_reference_kv=10, zero_sequence_connection="series"))
    net.add_branch(LineBranch("l", "l", "a", "k", length_km=1, r0=.1, x0=.2,
        r2_ohm_per_km=.12, x2_ohm_per_km=.24,
        r0_ohm_per_km=.3, x0_ohm_per_km=.8, ct_ratio=(100,5), ct_node="a"))
    net.add_mode(Mode("max", "Maximum"))
    net.add_mode(Mode("min", "Minimum", system="min"))
    return SimpleNamespace(electrical_model=import_legacy_network(net), methodology=Methodology.load())


def _state(project, index=0):
    return list(project.electrical_model.operating_states.values())[index]


def _node(project, name="k"):
    return next(row.id for row in project.electrical_model.electrical_nodes.values() if row.name == name)


def _equipment(project, name):
    return next(row for row in project.electrical_model.equipment.values() if row.name == name)


def _request(project, kinds=tuple(FaultType), index=0, target=None):
    return make_point_fault_request(project, _state(project,index).id,
        target or PointFaultTarget(node_id=_node(project)), tuple(FaultSpec(kind) for kind in kinds))


def _params(project, params, index=0):
    state = _state(project, index)
    project.electrical_model._operating_states[state.id] = replace(state,
        extensions=with_operating_parameters(state.extensions, params))
    project.electrical_model._revision += 1


def _confirmed(value):
    return ModeValue(value, "Independent mode input", DataConfirmation.CONFIRMED)


def test_selected_mode_prepared_once_and_only_requested_point_faults_run(monkeypatch):
    project = _project()
    from rza_calc.core import engine
    from rza_calc.core.context import Context
    from rza_calc.core.short_circuit import ShortCircuitSolver
    from rza_calc.topology.engine import TopologyEngine
    fail = lambda *a, **kw: pytest.fail("Full/legacy calculation must not run")
    monkeypatch.setattr(engine,"run",fail)
    monkeypatch.setattr(engine,"run_input",fail)
    monkeypatch.setattr(Context,"__init__",fail)
    monkeypatch.setattr(ShortCircuitSolver,"at",fail)
    compile_calls, build_calls, fault_calls = [], [], []
    original_compile, original_init, original_fault = TopologyEngine.compile, ShortCircuitSolver.__init__, ShortCircuitSolver.fault_at
    def compile(self, model, state=None, *args, **kwargs):
        compile_calls.append(state)
        return original_compile(self,model,state,*args,**kwargs)
    def init(self,*args,**kwargs):
        build_calls.append(True)
        return original_init(self,*args,**kwargs)
    def fault(self,node,spec):
        fault_calls.append((node,spec.kind))
        return original_fault(self,node,spec)
    monkeypatch.setattr(TopologyEngine,"compile",compile)
    monkeypatch.setattr(ShortCircuitSolver,"__init__",init)
    monkeypatch.setattr(ShortCircuitSolver,"fault_at",fault)
    request = _request(project)
    assert compile_calls == build_calls == fault_calls == []
    result = run_point_faults(request)
    assert all(row.available for row in result.outcomes)
    assert compile_calls == [request.state_id]
    assert len(build_calls) == 1
    assert fault_calls == [("k",kind) for kind in FaultType]
    assert tuple(result.prepared_mode.network_snapshot.materialize().modes) == ("max",)
    # Independent manual equivalent: source(.2+j.6)+line(.1+j.2).
    i1 = 10/math.sqrt(3)/(.3+.8j)
    assert result.outcomes[0].fault.i012_ka[1] == pytest.approx(i1)
    i1_ll = 10/math.sqrt(3)/(.3+.8j+.42+.94j)
    assert result.outcomes[1].fault.i012_ka == pytest.approx((0j,i1_ll,-i1_ll))
    i0_ag = 10/math.sqrt(3)/(.3+.8j+.42+.94j+.9+2.3j)
    assert result.outcomes[2].fault.i012_ka == pytest.approx((i0_ag,)*3)


def test_missing_zero_sequence_affects_only_earth_rows():
    project = _project()
    source = _equipment(project,"s")
    _params(project,OperatingParameters(sources={source.id:{"r0_ohm":ModeValue(None)}}))
    result = run_point_faults(_request(project))
    assert [row.available for row in result.outcomes] == [True,True,False,False]
    assert all(row.code in {"UNCONFIRMED_INPUT","MISSING_SEQUENCE_DATA"} for row in result.outcomes[2:])
    assert all(row.fault is None and row.message for row in result.outcomes[2:])


def test_unselected_mode_missing_source_does_not_enter_the_solver():
    project = _project()
    source = _equipment(project,"s")
    _params(project, OperatingParameters(sources={source.id:{"s_kz_min":ModeValue(None)}}), index=1)
    good = run_point_faults(_request(project,kinds=("3ph",)))
    bad = run_point_faults(_request(project,kinds=("3ph",),index=1))
    assert good.outcomes[0].available
    assert not bad.outcomes[0].available and bad.outcomes[0].message


def test_attach_default_still_populates_all_modes_and_subset_is_explicit():
    project = _project()
    source = _equipment(project,"s")
    for index in (0,1):
        _params(project,OperatingParameters(sources={source.id:{"r2_ohm":_confirmed(index+.5)}}),index)
    model = project.electrical_model
    all_modes = adapt_to_calculation(model)
    attach_operating_parameters(model,all_modes.network,all_modes.trace)
    assert all(mode.operating_parameters for mode in all_modes.network.modes.values())
    selected = adapt_to_calculation(model, operating_state_ids=())
    attach_operating_parameters(model,selected.network,selected.trace,state_ids=(_state(project).id,))
    assert selected.network.modes["max"].operating_parameters
    assert not selected.network.modes["min"].operating_parameters


def test_port_target_is_exact_and_branch_view_needs_no_full_result():
    project = _project()
    line = _equipment(project,"l")
    port = project.electrical_model.port_by_role(line.id,"from")
    result = run_point_faults(_request(project,kinds=("3ph",),target=PointFaultTarget(port_id=port.id)))
    assert result.node_id == "a"  # No orient()/automatic far-end selection.
    assert result.outcomes[0].fault.i012_ka[1] == pytest.approx(10/math.sqrt(3)/(.2+.6j))
    network = run_point_fault_network(result,FaultSpec("3ph"))
    assert network.fault == result.outcomes[0].fault
    assert network.branches["s"].to_terminal.delta_iabc_ka == pytest.approx(tuple(-i for i in network.fault.iabc_ka))


def test_frozen_capture_freshness_reuse_and_cancel(monkeypatch):
    project = _project()
    request = _request(project,kinds=("3ph",))
    prepared = prepare_point_mode(request)
    source = _equipment(project,"s")
    _params(project,OperatingParameters(sources={source.id:{"s_kz_max":_confirmed(500)}}))
    result = run_point_faults(request,prepared_mode=prepared)
    assert result.outcomes[0].available
    assert not result.is_current_for(project)
    assert result.outcomes[0].fault.i012_ka[1] == pytest.approx(10/math.sqrt(3)/(.3+.8j))
    with pytest.raises(FrozenInstanceError):
        result.node_id = "changed"
    fresh = run_point_faults(_request(project,kinds=("3ph",)))
    assert fresh.is_current_for(project)
    project.diagram = object()  # Geometry is deliberately outside the stamp.
    assert fresh.is_current_for(project)
    with pytest.raises(CalculationCancelled):
        run_point_faults(request,cancelled=lambda:True)
    with pytest.raises(CalculationCancelled):
        run_point_fault_network(fresh,FaultSpec("3ph"),cancelled=lambda:True)


@pytest.mark.parametrize("target", [PointFaultTarget(node_id=ElectricalNodeId("missing")),
                                    PointFaultTarget(port_id=PortId("missing"))])
def test_nonphysical_or_missing_target_is_rejected(target):
    with pytest.raises(ValueError):
        run_point_faults(_request(_project(),target=target))


def _deferred_vm(monkeypatch):
    from rza_calc.gui import view_model
    def fail(*args,**kwargs):
        pytest.fail("Deferred opening/point query must not execute a full run")
    monkeypatch.setattr(view_model,"run",fail)
    monkeypatch.setattr(view_model,"run_input",fail)
    path = Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json"
    return view_model.ProjectViewModel.open(path,calculate=False)


def _vm_target(vm):
    node = next(iter(vm.project.electrical_model.electrical_nodes.values()))
    return PointFaultTarget(node_id=node.id)


def test_deferred_open_and_point_receipt_do_not_populate_full_result(monkeypatch):
    vm = _deferred_vm(monkeypatch)
    assert vm.result is None and vm.current_result is None
    assert 'ПКМ' in vm.result_unavailable_reason()
    vm.selected_kind, vm.selected_id = 'node', next(iter(vm.net.nodes))
    assert all(row.status_code == 'NOT_CALCULATED' for row in vm.fault_rows())
    generation, request = vm.begin_point_fault_query(_vm_target(vm),(FaultSpec("3ph"),))
    assert vm.point_query_busy and vm.current_point_fault_result is None
    result = run_point_faults(request)
    assert result.outcomes[0].available
    assert vm.publish_point_fault_query(generation,request,result)
    assert vm.current_point_fault_result is result
    assert vm.result is None and vm.current_result is None
    assert vm.point_fault_branch_view(FaultSpec("3ph")).rows
    assert vm.cached_point_fault_query(request) is result
    assert vm.current_point_fault_results() == (result,)
    assert vm.point_fault_prepared_mode(request) is result.prepared_mode


def test_vm_rejects_cancelled_stale_wrong_mode_and_wrong_generation(monkeypatch):
    vm = _deferred_vm(monkeypatch)
    generation, request = vm.begin_point_fault_query(_vm_target(vm),(FaultSpec("3ph"),))
    with pytest.raises(ValueError):
        vm.begin_point_fault_query(_vm_target(vm),(FaultSpec("3ph"),))
    with pytest.raises(ValueError):
        vm.begin_background_calculation()
    assert vm.recalculate() is False
    vm.calculation_error = ''
    with pytest.raises(ValueError):
        vm.begin_mode_draft()
    result = run_point_faults(request)
    assert not vm.publish_point_fault_query(generation-1,request,result)
    assert vm.point_query_busy
    vm.cancel_point_fault_query()
    assert not vm.publish_point_fault_query(generation,request,result)
    assert vm.current_point_fault_result is None and vm.mode_draft is None
    assert vm.current_point_fault_results() == ()
    generation, request = vm.begin_point_fault_query(_vm_target(vm),(FaultSpec("3ph"),))
    result = run_point_faults(request)
    vm.mode_id = "another-mode"
    assert not vm.publish_point_fault_query(generation,request,result)
    assert vm.result is None
    vm.mode_id = result.mode_id
    generation, request = vm.begin_point_fault_query(_vm_target(vm),(FaultSpec("3ph"),))
    result = run_point_faults(request)
    vm.project.methodology.data["short_circuit"]["temp_factor_min"]["value"] *= 1.1
    assert not vm.publish_point_fault_query(generation,request,result)
    assert vm.current_point_fault_result is None and vm.result is None


def test_default_vm_construction_still_requests_full_calculation(monkeypatch):
    from rza_calc.gui.view_model import ProjectViewModel
    calls = []
    monkeypatch.setattr(ProjectViewModel,"recalculate",lambda self:calls.append(self))
    path = Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json"
    eager = ProjectViewModel.open(path)
    deferred = ProjectViewModel.open(path,calculate=False)
    assert calls == [eager] and deferred.result is None


def test_revision_only_restore_reuses_prepared_mode_and_cached_result(monkeypatch):
    vm = _deferred_vm(monkeypatch)
    generation, first = vm.begin_point_fault_query(_vm_target(vm), (FaultSpec('3ph'),))
    result = run_point_faults(first)
    assert vm.publish_point_fault_query(generation, first, result)
    # Undo restores equal records but never rewinds the model revision.
    vm.project.electrical_model._revision += 2
    generation, restored = vm.begin_point_fault_query(_vm_target(vm), first.specs)
    assert vm.current_point_fault_results() == ()
    assert restored.calculation_input != first.calculation_input
    assert restored.calculation_input.stamp == first.calculation_input.stamp
    prepared = vm.point_fault_prepared_mode(restored)
    assert prepared is result.prepared_mode
    reused = run_point_faults(restored, prepared_mode=prepared)
    assert reused.outcomes == result.outcomes
    assert vm.cached_point_fault_query(restored) is result
    assert vm.publish_point_fault_query(generation, restored, result)
    assert vm.current_point_fault_result is result


def test_prepared_projection_mismatch_cannot_produce_point_numbers():
    request = _request(_project())
    prepared = prepare_point_mode(request)
    projection = replace(prepared.frame.projection, topology_fingerprint='incorrect')
    tampered = replace(prepared, frame=replace(prepared.frame, projection=projection))
    result = run_point_faults(request, prepared_mode=tampered)
    assert all(not row.available and row.fault is None for row in result.outcomes)
    assert all('разным входам' in row.message for row in result.outcomes)


def test_selected_disconnected_point_is_not_energized_for_every_fault():
    project = _project()
    state, line = _state(project), _equipment(project, 'l')
    project.electrical_model._operating_states[state.id] = replace(state,
        availability={line.id: EquipmentAvailability.OUT_OF_SERVICE})
    result = run_point_faults(_request(project))
    assert all(row.code == 'NOT_ENERGIZED' and row.fault is None for row in result.outcomes)


def test_point_query_cannot_bypass_existing_protection_zone_review_blocker():
    project = _project()
    line = _equipment(project, 'l')
    project.electrical_model._equipment[line.id] = replace(line,
        extensions={**line.extensions, 'rza_calc.protection_zone_review': {'required': True}})
    result = run_point_faults(_request(project))
    assert all(not row.available and row.fault is None for row in result.outcomes)
    assert all('зону защиты' in row.message for row in result.outcomes)
