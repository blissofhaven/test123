"""Independent identities, immutable execution, and explicit mode currents."""
from dataclasses import FrozenInstanceError, replace
import json
import math
from types import SimpleNamespace

import pytest

from rza_calc.adapters.legacy_calculation import import_legacy_network
from rza_calc.calculation.input import (CalculationCancelled, NetworkSnapshot,
    capture_project_input, capture_network_input, load_calculation_input,
    prepare_calculation_input, save_calculation_input)
from rza_calc.core.engine import run, run_input
from rza_calc.core.fault_types import FaultSpec
from rza_calc.core.fingerprint import network_fingerprint
from rza_calc.core.load_current import working_current
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
    ProtectionSettings, SourceBranch)
from rza_calc.domain.electrical import DataConfirmation
from rza_calc.domain.operating_parameters import (ModeValue, OperatingParameters,
    with_operating_parameters)


def _network(*, parallel=False):
    net = Network("Independent stage04 circuit")
    for key in ("a", "b"):
        net.add_node(Node(key, key, 10.0))
    net.add_branch(SourceBranch("S", "S", GRID, "a", s_kz_max=200, s_kz_min=100,
        r2_ohm=0, x2_ohm=1, r0_ohm=0, x0_ohm=2, sequence_reference_kv=10.5,
        zero_sequence_connection="series"))
    for key in (("F", "F2") if parallel else ("F",)):
        net.add_branch(LineBranch(key, key, "a", "b", length_km=1,
            r0=.1, x0=.1, r2_ohm_per_km=.1, x2_ohm_per_km=.1,
            r0_ohm_per_km=.2, x0_ohm_per_km=.2, ct_ratio=(100, 5), ct_node="a",
            prot=ProtectionSettings(mtz=True, to=False, ozz=False)))
    net.add_load(Load("L", "L", "b", p_kw=90, cos_phi=.9))
    net.add_mode(Mode("z-first", "First", system="max"))
    net.add_mode(Mode("a-second", "Second", system="min"))
    return net


def _project(*, parallel=False):
    return SimpleNamespace(electrical_model=import_legacy_network(_network(parallel=parallel)),
                           methodology=Methodology.load())


def _equipment(project, name):
    return next(row for row in project.electrical_model.equipment.values() if row.name == name)


def _set_params(project, params, index=0):
    model = project.electrical_model
    state = list(model.operating_states.values())[index]
    model._operating_states[state.id] = replace(state,
        extensions=with_operating_parameters(state.extensions, params))
    model._revision += 1


def _confirmed(value):
    return ModeValue(value, "Independent operating study", DataConfirmation.CONFIRMED)


def _faults(result, node="b"):
    return {mid: tuple(tuple(solver.fault_at(node, FaultSpec(kind)).iabc_ka)
        for kind in ("3ph", "2ph", "1ph_g", "2ph_g"))
        for mid, solver in result.ctx.solvers.items()}


def test_network_snapshot_preserves_all_values_order_and_does_not_assign_missing_ct():
    net = _network()
    net.branches["F"].ct_node = None  # Exact DTO capture must not run add_branch defaults.
    snapshot = NetworkSnapshot.capture(net)
    materialized = snapshot.materialize()
    assert materialized.branches["F"].ct_node is None
    assert network_fingerprint(materialized) == network_fingerprint(net)
    assert tuple(materialized.modes) == ("z-first", "a-second")
    materialized.branches["F"].r0 = 500
    assert snapshot.materialize().branches["F"].r0 == .1


def test_capture_is_cheap_no_topology_and_deeply_immutable(monkeypatch):
    project = _project()
    from rza_calc.topology.engine import TopologyEngine
    monkeypatch.setattr(TopologyEngine, "compile", lambda *a, **kw: pytest.fail("capture compiled topology"))
    captured = capture_project_input(project)
    with pytest.raises(FrozenInstanceError):
        captured.prepared = True
    with pytest.raises(TypeError):
        captured.model_snapshot.equipment["x"] = None
    _set_params(project, OperatingParameters(load_factor=_confirmed(2)))
    assert not captured.is_current_for(project)
    assert captured.model_snapshot.operating_states != project.electrical_model.operating_states


def test_old_and_new_entrypoints_preserve_four_faults_and_numeric_passport():
    net = _network()
    method = Methodology.load()
    legacy = run(net, method)
    project = SimpleNamespace(electrical_model=import_legacy_network(net), methodology=method)
    unified = run_input(capture_project_input(project))
    assert _faults(unified) == _faults(legacy)
    assert unified.calculation_case.model_fingerprint == network_fingerprint(net)
    assert unified.calculation_case.declaration_order == legacy.calculation_case.declaration_order
    assert tuple(frame.mode_id for frame in unified.calculation_input.modes) == tuple(net.modes)
    assert unified.get("F", "МТЗ").i_primary == legacy.get("F", "МТЗ").i_primary
    for frame in unified.calculation_input.modes:
        assert frame.projection.topology_fingerprint == frame.topology.topology_fingerprint


def test_late_caller_mutations_cannot_change_lazily_built_fault_sequences():
    net = _network()
    result = run(net, Methodology.load())
    net.branches["S"].r0_ohm = 1000
    net.modes["z-first"].states["S"] = False
    reference = run(_network(), Methodology.load())
    assert _faults(result) == _faults(reference)
    assert result.ctx.net is not net
    assert not result.is_current_for(net, Methodology.load())


@pytest.mark.parametrize("canonical", [True, False])
def test_archive_replays_exact_inputs_without_external_files_or_geometry(tmp_path, canonical):
    project = _project()
    request = capture_project_input(project) if canonical else capture_network_input(_network(), project.methodology)
    before = run_input(request)
    path = tmp_path / "input.json"
    save_calculation_input(before.calculation_input, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"schema", "stamp", "model", "network", "methodology", "execution_versions"}
    restored = load_calculation_input(path)
    after = run_input(restored)
    assert restored.stamp == request.stamp
    assert _faults(after) == _faults(before)
    assert after.calculation_case.model_fingerprint == before.calculation_case.model_fingerprint
    with pytest.raises(FileExistsError):
        save_calculation_input(request, path)


def test_archive_tampering_is_rejected(tmp_path):
    path = tmp_path / "input.json"
    save_calculation_input(capture_network_input(_network(), Methodology.load()), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    network = json.loads(raw["network"]["payload_json"])
    network["branches"][0][2]["s_kz_max"] *= 2
    raw["network"]["payload_json"] = json.dumps(network)
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="Отпечаток"):
        load_calculation_input(path)


def test_cancel_before_preparation_and_between_solver_modes_publishes_no_result():
    project = _project()
    request = capture_project_input(project)
    with pytest.raises(CalculationCancelled):
        run_input(request, cancelled=lambda: True)
    cancel = False
    labels = []
    def progress(done, total, label):
        nonlocal cancel
        labels.append(label)
        if label == "КЗ: First":
            cancel = True
    with pytest.raises(CalculationCancelled):
        run_input(request, cancelled=lambda: cancel, progress=progress)
    assert "КЗ: Second" not in labels
    assert request.is_current_for(project)


def test_per_mode_source_strength_changes_only_its_solver_with_known_bus_ratio():
    project = _project()
    before = run_input(capture_project_input(project))
    source = _equipment(project, "S")
    _set_params(project, OperatingParameters(sources={source.id: {"s_kz_max": _confirmed(400)}}))
    after = run_input(capture_project_input(project))
    def current(result, mid):
        return abs(result.ctx.solvers[mid].fault_at("a", FaultSpec("3ph")).iabc_ka[0])
    assert current(after, "z-first") == pytest.approx(2 * current(before, "z-first"))
    assert current(after, "a-second") == current(before, "a-second")
    assert _equipment(project, "S").properties["legacy_payload"]["s_kz_max"] == 200
    assert after.calculation_input.is_current_for(project)


def test_explicit_missing_mode_z0_does_not_inherit_common_or_disable_three_phase():
    project = _project()
    source = _equipment(project, "S")
    _set_params(project, OperatingParameters(sources={source.id: {
        "sequence_by_system.max.r0_ohm": ModeValue(None, "Not provided")}}))
    result = run_input(capture_project_input(project))
    assert result.ctx.solvers["z-first"].fault_at("a", FaultSpec("3ph"))
    assert result.ctx.solvers["z-first"].fault_at("a", FaultSpec("2ph"))
    with pytest.raises(ValueError):
        result.ctx.solvers["z-first"].fault_at("a", FaultSpec("1ph_g"))
    assert result.ctx.solvers["a-second"].fault_at("a", FaultSpec("1ph_g"))


def test_confirmed_global_and_load_factors_multiply_per_mode_without_changing_faults():
    project = _project()
    before = run_input(capture_project_input(project))
    load = _equipment(project, "L")
    _set_params(project, OperatingParameters(load_factor=_confirmed(2), load_factors={load.id: _confirmed(3)}))
    after = run_input(capture_project_input(project))
    ctx = after.ctx
    expected = 90 / .9 * 6 / (math.sqrt(3) * 10)
    assert working_current(ctx.net, ctx.net.branches["F"], ctx.net.modes["z-first"], ctx.meth).value == pytest.approx(expected)
    assert working_current(ctx.net, ctx.net.branches["F"], ctx.net.modes["a-second"], ctx.meth).value == pytest.approx(expected / 6)
    assert _faults(after) == _faults(before)


@pytest.mark.parametrize("kind", ["missing", "unconfirmed"])
def test_draft_load_factor_blocks_working_current_without_disabling_faults(kind):
    project = _project()
    _set_params(project, OperatingParameters(load_factor=ModeValue(None if kind == "missing" else 2, "Review")))
    result = run_input(capture_project_input(project))
    assert result.ctx.solvers["z-first"].fault_at("b", FaultSpec("3ph"))
    assert not result.get("F", "МТЗ").is_complete
    assert "Коэффициент нагрузки" in result.get("F", "МТЗ").explain()


def test_parallel_group_requires_external_current_even_when_all_load_is_known():
    project = _project(parallel=True)
    result = run_input(capture_project_input(project))
    for bid in ("F", "F2"):
        assert result.get(bid, "МТЗ").i_primary is None
        assert "параллельное питание" in result.get(bid, "МТЗ").explain()
    assert result.ctx.solvers["z-first"].fault_at("b", FaultSpec("3ph"))


def test_external_current_is_tied_to_exact_physical_ct_port_and_each_mode():
    project = _project(parallel=True)
    model = project.electrical_model
    feeder = _equipment(project, "F")
    right = model.port_by_role(feeder.id, "from").id
    wrong = model.port_by_role(feeder.id, "to").id
    _set_params(project, OperatingParameters(working_currents={right: _confirmed(17), wrong: _confirmed(999)}))
    _set_params(project, OperatingParameters(working_currents={right: _confirmed(23)}), 1)
    result = run_input(capture_project_input(project))
    ctx = result.ctx
    assert working_current(ctx.net, ctx.net.branches["F"], ctx.net.modes["z-first"], ctx.meth).value == 17
    assert working_current(ctx.net, ctx.net.branches["F"], ctx.net.modes["a-second"], ctx.meth).value == 23
    assert result.get("F", "МТЗ").i_primary is not None
    assert result.get("F2", "МТЗ").i_primary is None


def test_wrong_side_current_cannot_make_parallel_protection_complete():
    project = _project(parallel=True)
    feeder = _equipment(project, "F")
    wrong = project.electrical_model.port_by_role(feeder.id, "to").id
    _set_params(project, OperatingParameters(working_currents={wrong: _confirmed(999)}))
    result = run_input(capture_project_input(project))
    assert result.get("F", "МТЗ").i_primary is None


def test_parallel_sources_require_explicit_permission_when_forbidden():
    net = _network()
    net.add_branch(SourceBranch("S2", "S2", GRID, "a", s_kz_max=100, s_kz_min=80))
    project = SimpleNamespace(electrical_model=import_legacy_network(net), methodology=Methodology.load())
    historical = run_input(capture_project_input(project))
    assert "z-first" in historical.ctx.solvers
    assert any("разрешение" in text for text in historical.warnings)
    _set_params(project, OperatingParameters(parallel_operation=False))
    result = run_input(capture_project_input(project))
    assert "z-first" not in result.ctx.solvers
    assert "запрещена" in result.ctx.errors["z-first"]
    assert "a-second" in result.ctx.solvers


def test_partial_working_current_never_selects_a_setting_from_radial_subset():
    net = _network(parallel=True)
    net.modes["a-second"].availability["F2"] = False
    project = SimpleNamespace(electrical_model=import_legacy_network(net), methodology=Methodology.load())
    partial = run_input(capture_project_input(project))
    result = partial.get("F", "МТЗ")
    assert result.i_primary is None and result.i_secondary is None
    assert any("Максимальный рабочий ток" in step.what for step in result.steps)
    feeder = _equipment(project, "F")
    port = project.electrical_model.port_by_role(feeder.id, "from").id
    _set_params(project, OperatingParameters(working_currents={port: _confirmed(25)}))
    complete = run_input(capture_project_input(project))
    assert complete.get("F", "МТЗ").i_primary is not None
    assert complete.get("F", "МТЗ").governing_mode == "First"


def test_mode_specific_sequence_precedence_is_independent_of_mapping_order():
    values = {"x2_ohm": _confirmed(.4), "sequence_by_system.max.x2_ohm": _confirmed(.8)}
    currents = []
    for order in (values, dict(reversed(tuple(values.items())))):
        project = _project()
        source = _equipment(project, "S")
        _set_params(project, OperatingParameters(sources={source.id: order}))
        result = run_input(capture_project_input(project))
        assert result.ctx.mode_networks["z-first"].branches["S"].x2_ohm == .8
        currents.append(result.ctx.solvers["z-first"].fault_at("a", FaultSpec("2ph")).iabc_ka)
    assert currents[0] == currents[1]


def test_empty_common_source_is_filled_per_mode_without_borrowing_from_another_mode():
    net = _network()
    net.branches["S"].s_kz_max = net.branches["S"].s_kz_min = None
    project = SimpleNamespace(electrical_model=import_legacy_network(net), methodology=Methodology.load())
    source = _equipment(project, "S")
    _set_params(project, OperatingParameters(sources={source.id: {"s_kz_max": _confirmed(200)}}))
    result = run_input(capture_project_input(project))
    assert "z-first" in result.ctx.solvers
    assert "a-second" in result.ctx.errors
    measured = abs(result.ctx.solvers["z-first"].fault_at("a", FaultSpec("3ph")).iabc_ka[0])
    # Source Z=Ubase²/Ssc, E=Ubase/sqrt(3), hence I=Ssc/(sqrt(3)*Ubase).
    assert measured == pytest.approx(200 / (math.sqrt(3) * 10.5))
    assert net.validate()  # Unchanged strict standalone API still requires both systems.


def test_unavailable_empty_source_does_not_energize_or_block_another_island():
    net = _network()
    net.add_node(Node("dead", "Unpowered island", 10.0))
    net.add_branch(SourceBranch("Bad", "Unavailable empty source", GRID, "dead"))
    for mode in net.modes.values():
        mode.availability["Bad"] = False
    project = SimpleNamespace(electrical_model=import_legacy_network(net), methodology=Methodology.load())
    result = run_input(capture_project_input(project))
    assert set(result.ctx.solvers) == set(net.modes)
    for solver in result.ctx.solvers.values():
        assert solver.fault_at("a", FaultSpec("3ph"))
        with pytest.raises(KeyError):
            solver.fault_at("dead", FaultSpec("3ph"))


@pytest.mark.parametrize("field,value", [("s_kz_min", float("nan")), ("s_kz_min", -2)])
def test_inactive_or_nonselected_source_cannot_hide_invalid_numeric_values(field, value):
    net = _network()
    setattr(net.branches["S"], field, value)
    net.modes["a-second"].availability["S"] = False
    assert net.validate(net.modes["z-first"], require_source_data=False)


def test_failed_atomic_archive_overwrite_preserves_existing_bytes(tmp_path, monkeypatch):
    request = capture_project_input(_project())
    path = tmp_path / "input.json"
    path.write_bytes(b"previous complete snapshot")
    def fail_replace(*args):
        raise OSError("Simulated replace failure")
    monkeypatch.setattr("rza_calc.calculation.input.os.replace", fail_replace)
    with pytest.raises(OSError):
        save_calculation_input(request, path, overwrite=True)
    assert path.read_bytes() == b"previous complete snapshot"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("field", ["kernel", "algorithm", "missing"])
def test_archive_rejects_incompatible_or_incomplete_execution_versions(tmp_path, field):
    path = tmp_path / "input.json"
    save_calculation_input(capture_project_input(_project()), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if field == "missing":
        del raw["execution_versions"]
    else:
        raw["execution_versions"][field] = "future-incompatible-version"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_calculation_input(path)


def test_corrupted_prepared_topology_cannot_be_ignored_by_execution():
    prepared = prepare_calculation_input(capture_project_input(_project()))
    frame = prepared.modes[0]
    broken = replace(frame, projection=replace(frame.projection, topology_fingerprint="wrong"))
    result = run_input(replace(prepared, modes=(broken, *prepared.modes[1:])))
    assert frame.mode_id in result.ctx.errors
    assert "разным входам" in result.ctx.errors[frame.mode_id]
    assert prepared.modes[1].mode_id in result.ctx.solvers
