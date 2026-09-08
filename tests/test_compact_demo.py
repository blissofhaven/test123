"""Electrical and drawing acceptance of the separate conditional training set."""
from collections import defaultdict
from dataclasses import asdict
import hashlib
import importlib.util
from itertools import combinations
import math
from pathlib import Path

import pytest

from tools import build_compact_demo as builder
from tools.autolayout import legacy_id
from rza_calc.calculation.input import capture_project_input
from rza_calc.calculation.point_fault import PointFaultRequest, PointFaultTarget, prepare_point_mode, run_point_faults
from rza_calc.core.fault_types import FaultSpec, FaultType
from rza_calc.core.model import GRID, GeneratorBranch, LineBranch, Mode, TieBranch, TransformerBranch
from rza_calc.domain import electrical_model_fingerprint
from rza_calc.domain.diagram import DiagramRouteKind, RouteAnchorKind
from rza_calc.domain.operating_parameters import operating_parameters
from rza_calc.editor.orientation import rotated_port_layout
from rza_calc.io.project import load_project, save_project

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'rza_calc/examples/compact_training.json'
OILFIELD = ROOT / 'rza_calc/examples/oilfield_gtes.json'
BUS_IDS = tuple(f'{fid}_{side}{section}' for fid in builder.FACILITY_IDS for side in ('hv', 'lv') for section in (1, 2))
SPECS = tuple(FaultSpec(kind) for kind in FaultType)


@pytest.fixture(scope='module')
def project():
    return load_project(EXAMPLE)


@pytest.fixture(scope='module')
def context(project):
    captured = capture_project_input(project)
    nodes = {legacy_id(n): n for n in project.electrical_model.electrical_nodes.values()}
    states = {legacy_id(s): s for s in project.electrical_model.operating_states.values()}
    prepared = {}
    for mid, state in states.items():
        request = PointFaultRequest(captured, state.id, PointFaultTarget(node_id=nodes[BUS_IDS[0]].id), SPECS)
        prepared[mid] = prepare_point_mode(request)
    return captured, nodes, states, prepared


def _component(net, mode, start):
    """Independent physical reachability: GRID is not a bus joining generators."""
    adjacency = defaultdict(set)
    for branch in net.branches.values():
        if not net.branch_conducting(branch, mode) or GRID in (branch.node_from, branch.node_to):
            continue
        adjacency[branch.node_from].add(branch.node_to)
        adjacency[branch.node_to].add(branch.node_from)
    seen, queue = {start}, [start]
    while queue:
        for nxt in adjacency[queue.pop()] - seen:
            seen.add(nxt)
            queue.append(nxt)
    return seen


def test_rebuild_is_deterministic_and_does_not_write_oilfield(project):
    before = hashlib.sha256(OILFIELD.read_bytes()).hexdigest()
    rebuilt = builder.build_project()
    again = builder.build_project()
    assert electrical_model_fingerprint(rebuilt.electrical_model) == electrical_model_fingerprint(project.electrical_model)
    assert rebuilt.diagram == project.diagram == again.diagram
    assert rebuilt.metadata == project.metadata
    assert hashlib.sha256(OILFIELD.read_bytes()).hexdigest() == before
    assert len(project.electrical_model.equipment) == 78
    assert project.metadata['training_demo']['counts'] == {
        'facilities': 4, 'nodes': 62, 'branches': 70, 'loads': 8,
        'generators': 2, 'transformers': 8, 'breakers': 46, 'lines': 14, 'modes': 4,
    }
    assert 'oilfield_demo' not in project.metadata
    assert 'учеб' in project.diagram.name.lower()
    assert not project.network.validate()


@pytest.mark.parametrize('fid', builder.FACILITY_IDS)
def test_buses_and_both_transformer_breakers_are_real_independent_circuits(project, fid):
    net = project.network
    facility = next(f for f in project.metadata['training_demo']['facilities'] if f['id'] == fid)
    normal = net.modes['normal_max']
    for side in ('hv', 'lv'):
        first, second = facility['buses'][side]
        tie = net.branches[facility['bus_ties'][side]]
        assert isinstance(tie, TieBranch) and tie.switchable and not tie.normally_closed
        assert (tie.node_from, tie.node_to) == (first, second)
        assert second not in _component(net, normal, first)
    for transformer in facility['transformers']:
        assert len(set(transformer['branch_ids'])) == 3
        for bid, first, last in zip(transformer['branch_ids'], transformer['node_ids'], transformer['node_ids'][1:]):
            branch = net.branches[bid]
            assert (branch.node_from, branch.node_to) == (first, last)
        for key in ('hv_breaker', 'lv_breaker'):
            q = net.branches[transformer[key]]
            assert isinstance(q, TieBranch) and q.switchable


@pytest.mark.parametrize('fid', builder.FACILITY_IDS[1:])
def test_two_inputs_really_reach_distinct_upstream_sections_and_open_at_each_end(project, fid):
    net = project.network
    byid = {f['id']: f for f in project.metadata['training_demo']['facilities']}
    row = byid[fid]
    upstream = byid[row['upstream_id']]
    side = 'hv' if upstream['kind'] == 'gtes' else 'lv'
    assert {r['upstream_node_id'] for r in row['incoming']} == set(upstream['buses'][side])
    assert {r['target_node_id'] for r in row['incoming']} == set(row['buses']['hv'])
    for entry in row['incoming']:
        assert entry in upstream['outgoing']
        for bid, first, last in zip(entry['branch_ids'], entry['node_ids'], entry['node_ids'][1:]):
            assert (net.branches[bid].node_from, net.branches[bid].node_to) == (first, last)
        for key in ('outgoing_breaker', 'incoming_breaker'):
            mode = Mode('cut', 'Cut', states={entry[key]: False})
            assert entry['upstream_node_id'] not in _component(net, mode, entry['target_node_id'])
            assert row['buses']['lv'][entry['section']-1] not in net.energized_nodes(mode)


def test_saved_modes_keep_all_bus_sections_energized_and_only_requested_changes(project):
    net = project.network
    for mode in net.modes.values():
        assert set(BUS_IDS) <= net.energized_nodes(mode)
    minimum = net.modes['single_generator_min']
    assert minimum.states == {'train_g2_q': False, 'train_gtes_sv_lv': True}
    assert minimum.availability == {'train_g2': False}
    assert not net.branch_conducting(net.branches['train_g2'], minimum)
    assert all(nid in _component(net, minimum, 'train_g1_terminal') for nid in BUS_IDS)
    repair = net.modes['repair_ps_t1']
    assert repair.states == {'train_ps_t1_q_hv': False, 'train_ps_t1_q_lv': False, 'train_ps_sv_lv': True}
    assert repair.availability == {'train_ps_t1': False}
    assert all(not net.branch_conducting(net.branches[bid], repair)
               for bid in ('train_ps_t1', 'train_ps_t1_q_hv', 'train_ps_t1_q_lv'))
    parallel = net.modes['parallel_cp35']
    assert parallel.states == {'train_cp_sv_lv': True} and not parallel.availability
    assert 'train_g2_terminal' in _component(net, parallel, 'train_g1_terminal')
    assert 'train_g2_terminal' not in _component(net, net.modes['normal_max'], 'train_g1_terminal')
    for state in project.electrical_model.operating_states.values():
        assert operating_parameters(state).parallel_operation is True


def test_explicit_sequence_and_provenance_are_conditional_not_nameplate_certification(project):
    net = project.network
    assert net.neutral == {'110': 'earthed', '35': 'earthed', '10': 'earthed', '0.4': 'earthed'}
    assert all(n.calculation_base_kv == n.prefault_voltage_kv == n.u_nom for n in net.nodes.values())
    for branch in net.branches.values():
        assert not any((branch.prot.mtz, branch.prot.to, branch.prot.ozz))
        assert 'НЕ РЕАЛЬНЫЙ ОБЪЕКТ' in branch.note
        if isinstance(branch, TieBranch):
            continue
        for key, row in branch.parameter_provenance.items():
            assert row['source'] == builder.SOURCE
            assert row['origin'] == 'assumption'
            if row['confirmation'] == 'confirmed':
                stored_key = {'length_mm': 'length_km', 'parallel_count': 'n_parallel',
                              'r1_ohm_per_km': 'r0', 'x1_ohm_per_km': 'x0'}.get(key, key)
                assert getattr(branch, stored_key) is not None
        if branch.ct_ratio is not None:
            assert branch.parameter_provenance['ct_primary_a']['confirmation'] == 'unconfirmed'
            assert branch.parameter_provenance['ct_secondary_a']['confirmation'] == 'unconfirmed'
        if isinstance(branch, LineBranch):
            assert (branch.r2_ohm_per_km, branch.x2_ohm_per_km) == (branch.r0, branch.x0)
            assert (branch.r0_ohm_per_km, branch.x0_ohm_per_km) == (3*branch.r0, 3*branch.x0)
            assert branch.r0_ohm is branch.r2_ohm is None
        elif isinstance(branch, GeneratorBranch):
            assert branch.sequence_reference_kv == 10
            assert complex(branch.r0_ohm, branch.x0_ohm) == pytest.approx(complex(.1,.5)+3*complex(.2,.05))
        else:
            assert isinstance(branch, TransformerBranch)
            if branch.id.startswith('train_gtes'):
                assert (branch.group, branch.zero_sequence_connection, branch.sequence_phase_shift_deg) == ('YNd11','from_ground',30.)
            elif branch.id.startswith('train_ktp'):
                assert (branch.group, branch.zero_sequence_connection, branch.sequence_reference_kv) == ('Dyn11','to_ground',.4)
            else:
                assert (branch.group, branch.zero_sequence_connection, branch.sequence_phase_shift_deg) == ('YNyn0','series',0.)


def test_equipment_card_sees_canonical_line_provenance_without_a_legacy_alias_gap(project):
    from rza_calc.editor.controller import ProjectEditorController
    from rza_calc.domain.electrical import DataConfirmation
    controller = ProjectEditorController(project)
    line = next(item for item in project.electrical_model.equipment.values() if legacy_id(item) == 'train_cp_in1_line')
    snapshot = controller.equipment_parameter_snapshot(line.id)
    for key in ('length_mm', 'parallel_count', 'r1_ohm_per_km', 'x1_ohm_per_km',
                'r2_ohm_per_km', 'x2_ohm_per_km', 'r0_ohm_per_km', 'x0_ohm_per_km'):
        assert snapshot.fields[key].confirmation is DataConfirmation.CONFIRMED
        assert snapshot.fields[key].source == builder.SOURCE
        assert snapshot.fields[key].value is not None
    assert snapshot.fields['length_mm'].value == 8000000
    assert snapshot.fields['ct_primary_a'].confirmation is DataConfirmation.UNCONFIRMED
    assert snapshot.fields['ct_secondary_a'].confirmation is DataConfirmation.UNCONFIRMED


@pytest.mark.parametrize('mode_id', builder.MODE_IDS)
@pytest.mark.parametrize('bus_id', BUS_IDS)
def test_four_faults_are_available_on_every_bus_in_each_saved_mode(project, context, mode_id, bus_id):
    captured, nodes, states, prepared = context
    request = PointFaultRequest(captured, states[mode_id].id, PointFaultTarget(node_id=nodes[bus_id].id), SPECS)
    result = run_point_faults(request, prepared_mode=prepared[mode_id])
    assert result.mode_id == mode_id and result.node_id == bus_id
    assert result.is_current_for(project)
    assert len(result.outcomes) == 4
    for row in result.outcomes:
        assert row.available, (mode_id, bus_id, row.spec.kind, row.code, row.message)
        assert all(math.isfinite(abs(i)) for i in row.fault.iabc_ka)
        assert max(map(abs, row.fault.iabc_ka)) > 0


def test_generator_bus_matches_independent_six_complex_phase_boundary_oracle(project, context):
    path = ROOT / 'tests/fixtures/control_examples/tkz_reference_01/four_fault_oracle.py'
    spec = importlib.util.spec_from_file_location('compact_phase_oracle', path)
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    captured, nodes, states, prepared = context
    req = PointFaultRequest(captured, states['normal_max'].id, PointFaultTarget(node_id=nodes['train_gtes_lv1'].id), SPECS)
    result = run_point_faults(req, prepared_mode=prepared['normal_max'])
    names = dict(zip(FaultType, oracle.FAULTS))
    # One isolated 10kV section has exactly G1 behind its impedance; no load
    # infeed and GSU's delta side blocks zero sequence. Ohms are derived here
    # from the written synthetic 7.5MVA/.012/.15 inputs, not production helpers.
    z1 = complex(.012, .15) * 10**2 / 7.5
    for outcome in result.outcomes:
        expected = oracle.phase_solve(z1, complex(.16,1.8), complex(.7,.65), 10000/math.sqrt(3),
                                      names[outcome.spec.kind], zero_path='finite_impedance')
        assert expected['checks']['passed']
        phases = tuple(complex(*pair) for pair in expected['Iabc_kA'])
        assert outcome.fault.iabc_ka == pytest.approx(phases, rel=1e-10, abs=1e-11)


def test_roundtrip_preserves_inputs_geometry_modes_and_fault_numbers(project, tmp_path, context):
    target = tmp_path / 'roundtrip.json'
    before = electrical_model_fingerprint(project.electrical_model)
    save_project(target, project)
    reopened = load_project(target)
    assert electrical_model_fingerprint(reopened.electrical_model) == before
    assert reopened.diagram == project.diagram
    assert reopened.metadata == project.metadata
    assert [asdict(m) for m in reopened.network.modes.values()] == [asdict(m) for m in project.network.modes.values()]
    captured, nodes, states, prepared = context
    old = run_point_faults(PointFaultRequest(captured, states['parallel_cp35'].id,
        PointFaultTarget(node_id=nodes['train_ktp_lv2'].id), SPECS), prepared_mode=prepared['parallel_cp35'])
    request = PointFaultRequest(capture_project_input(reopened), states['parallel_cp35'].id,
        PointFaultTarget(node_id=nodes['train_ktp_lv2'].id), SPECS)
    new = run_point_faults(request)
    assert [r.fault.iabc_ka for r in new.outcomes] == [r.fault.iabc_ka for r in old.outcomes]


def test_every_object_is_placed_and_repeated_circuits_keep_same_electrical_ends(project):
    diagram = project.diagram
    model = project.electrical_model
    assert len(diagram.pages) == 4 and not diagram.validate_targets(model)
    placed = {r.equipment_id for r in diagram.representations.values() if r.equipment_id}
    placed |= {r.equipment_id for r in diagram.routes.values() if r.equipment_id}
    assert placed == set(model.equipment)
    assert {r.electrical_node_id for r in diagram.representations.values() if r.electrical_node_id} == set(model.electrical_nodes)
    equipment = {legacy_id(e): e for e in model.equipment.values()}
    for facility in project.metadata['training_demo']['facilities']:
        for incoming in facility['incoming']:
            views = [r for r in diagram.routes.values() if r.equipment_id == equipment[incoming['line']].id]
            assert len(views) == len({r.page_id for r in views}) == 2
            assert len({(r.start_anchor.branch_port_id,r.end_anchor.branch_port_id) for r in views}) == 1
            assert len({(r.start_anchor.electrical_node_id,r.end_anchor.electrical_node_id) for r in views}) == 1
    for rep in diagram.representations.values():
        target = rep.extensions.get('linked_page_id')
        if target:
            assert any(other.page_id.value == target and other.target_id == rep.target_id
                       for other in diagram.representations.values())


def test_all_route_endpoints_contacts_and_crossings_are_honest(project):
    model, diagram = project.electrical_model, project.diagram
    contacts, segments = defaultdict(list), defaultdict(list)
    buses = [rep for rep in diagram.representations.values() if rep.symbol_key == 'busbar']
    for route in diagram.routes.values():
        for anchor, point in ((route.start_anchor, route.waypoints[0]), (route.end_anchor,route.waypoints[-1])):
            rep = diagram.representations[anchor.representation_id]
            if anchor.kind is RouteAnchorKind.EQUIPMENT_PORT:
                equipment = model.equipment[rep.equipment_id]
                definition = model.equipment_types[(equipment.type_id,equipment.type_version)]
                graphics = rep.extensions['stage3_graphics']
                port = next(p for p in rotated_port_layout(equipment,definition,width=graphics['width'],height=graphics['height'],
                    rotation=rep.rotation_deg,center_x=rep.x,center_y=rep.y) if p.port_id == anchor.target_port_id)
                assert (point.x,point.y) == pytest.approx((port.x,port.y))
            elif anchor.kind is RouteAnchorKind.BUS:
                width = rep.extensions['stage3_graphics']['width']
                assert point.y == pytest.approx(rep.y)
                assert point.x == pytest.approx(rep.x-width/2+float(anchor.anchor_key)*width)
                contacts[rep.id].append(point.x)
            else:
                assert (point.x,point.y) == pytest.approx((rep.x,rep.y))
        for a,b in zip(route.waypoints,route.waypoints[1:]):
            assert a.x == b.x or a.y == b.y
            horizontal = a.y == b.y
            lo,hi = sorted((a.x,b.x) if horizontal else (a.y,b.y))
            segments[(route.page_id,horizontal,a.y if horizontal else a.x)].append((lo,hi,route.id))
            for bus in buses:
                if bus.page_id != route.page_id or bus.electrical_node_id in {route.start_anchor.electrical_node_id,route.end_anchor.electrical_node_id}:
                    continue
                half = bus.extensions['stage3_graphics']['width']/2
                if a.x == b.x:
                    assert not (bus.x-half <= a.x <= bus.x+half and min(a.y,b.y) <= bus.y <= max(a.y,b.y))
                elif a.y == bus.y:
                    assert max(min(a.x,b.x),bus.x-half) > min(max(a.x,b.x),bus.x+half)
    assert all(abs(a-b) >= 20-1e-7 for rows in contacts.values() for a,b in combinations(rows,2))
    assert all(a[2] == b[2] or min(a[1],b[1])-max(a[0],b[0]) <= 1e-7
               for rows in segments.values() for a,b in combinations(rows,2))


def test_cli_requires_new_explicit_output_and_never_writes_existing(monkeypatch, tmp_path):
    target = tmp_path / 'existing.json'
    target.write_bytes(b'user project')
    monkeypatch.setattr(builder, 'build_project', lambda: pytest.fail('Must reject before building'))
    with pytest.raises(SystemExit) as absent:
        builder.main([])
    assert absent.value.code == 2
    with pytest.raises(SystemExit) as existing:
        builder.main(['--output', str(target)])
    assert existing.value.code == 2
    assert target.read_bytes() == b'user project'


def test_cli_can_write_only_a_new_temporary_project(tmp_path):
    before = hashlib.sha256(OILFIELD.read_bytes()).hexdigest()
    target = tmp_path / 'new.json'
    assert builder.main(['--output',str(target)]) == 0
    assert len(load_project(target).electrical_model.equipment) == 78
    assert hashlib.sha256(OILFIELD.read_bytes()).hexdigest() == before
