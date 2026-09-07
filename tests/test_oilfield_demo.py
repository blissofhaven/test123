"""Electrical acceptance of the separate synthetic oilfield example.

Switching assertions cover reachability, not load flow or N-1 capacity.
Fault values are tested only where the demonstration supplies the inputs.
"""
from dataclasses import asdict
import hashlib
import importlib
import math
from pathlib import Path

import pytest

from rza_calc.core.fault_types import FaultSpec, FaultType
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, GeneratorBranch, LineBranch, Mode, SourceBranch, TieBranch, TransformerBranch
from rza_calc.core.short_circuit import ShortCircuitSolver, ShortCircuitStatusError
from rza_calc.domain import EquipmentAvailability, SwitchPosition, electrical_model_fingerprint
from rza_calc.io.project import load_project, save_project
from rza_calc.topology import TopologyEngine
from tools import build_oilfield_demo as builder


EXAMPLE = Path(__file__).resolve().parents[1] / "rza_calc/examples/oilfield_gtes.json"
FACILITIES = ("gtes", *(f"cp{i:02d}" for i in range(1, 4)),
              *(f"ps{i:02d}" for i in range(1, 7)),
              *(f"ktp{i:02d}" for i in range(1, 25)))
SECTIONS = (1, 2)
LOAD_FEEDERS = tuple(
    f"{fid}_f{s}_{n}" for fid in FACILITIES for s in SECTIONS
    for n in range(1, 4 if fid.startswith("ktp") else 2 if fid.startswith("cp") else 3)
)


@pytest.fixture(scope="module")
def data():
    return builder.build_data()


@pytest.fixture(scope="module")
def facilities(data):
    return {row["id"]: row for row in data[1]["facilities"]}


@pytest.fixture(scope="module")
def saved_project():
    return load_project(EXAMPLE)


def _load_nodes(net):
    return {load.node for load in net.loads.values()}


def _component_without_source_reference(net, mode, start):
    # GRID is a common mathematical EMF reference, not a wire joining six
    # generator terminals. Exclude it when checking independent bus sections.
    adj = net.adjacency(mode)
    seen, queue = {start}, [start]
    while queue:
        for nxt, _ in adj[queue.pop()]:
            if nxt != GRID and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _assert_chain(net, ids, nodes):
    assert len(nodes) == len(ids) + 1
    for bid, first, last in zip(ids, nodes, nodes[1:]):
        branch = net.branches[bid]
        assert (branch.node_from, branch.node_to) == (first, last)


def test_inventory_is_large_explicit_and_deterministic(data):
    net, manifest = data
    again, again_manifest = builder.build_data()
    assert manifest == again_manifest
    assert [asdict(n) for n in net.nodes.values()] == [asdict(n) for n in again.nodes.values()]
    assert [asdict(b) for b in net.branches.values()] == [asdict(b) for b in again.branches.values()]
    assert [asdict(x) for x in net.loads.values()] == [asdict(x) for x in again.loads.values()]
    assert net.validate() == []
    assert tuple(row["id"] for row in manifest["facilities"]) == FACILITIES
    assert manifest["counts"] == {
        "facilities": 34, "nodes": 766, "branches": 838, "loads": 178,
        "generators": 6, "transformers": 68, "breakers": 520, "lines": 244, "modes": 5,
    }
    assert not any(isinstance(branch, SourceBranch) for branch in net.branches.values())
    generators = [b for b in net.branches.values() if isinstance(b, GeneratorBranch)]
    assert len(generators) == 6
    assert sum(b.p_nom for b in generators) == 36
    assert all(b.s_nom == 7500 and b.cos_phi == .8 for b in generators)
    assert sum(load.p_kw for load in net.loads.values()) == pytest.approx(21920)
    assert _load_nodes(net) <= net.energized_nodes(net.modes["normal_max"])
    assert manifest["demo_only"] is True
    assert manifest["physical_inputs_status"] == "demo_assumptions_not_confirmed"
    assert "НЕ РЕАЛЬНЫЙ ОБЪЕКТ" in manifest["warning"]
    assert any("Z2/Z0" in text for text in manifest["unresolved_inputs"])
    assert any("ТСН" in text for text in manifest["assumptions"])


@pytest.mark.parametrize("fid", FACILITIES)
def test_every_facility_has_two_electrically_independent_sections(data, facilities, fid):
    net, _ = data
    row, normal = facilities[fid], net.modes["normal_max"]
    for side in ("hv", "lv"):
        first, last = row["buses"][side]
        tie = net.branches[row["bus_ties"][side]]
        assert first != last
        assert (tie.node_from, tie.node_to) == (first, last)
        assert isinstance(tie, TieBranch) and tie.switchable
        assert tie.normally_closed is False and not net.branch_conducting(tie, normal)
        assert last not in _component_without_source_reference(net, normal, first)


@pytest.mark.parametrize("fid", FACILITIES[1:])
def test_two_inputs_come_from_distinct_upstream_sections_with_real_breakers(data, facilities, fid):
    net, _ = data
    row = facilities[fid]
    upstream = facilities[row["upstream_id"]]
    side = "hv" if upstream["kind"] == "gtes" else "lv"
    assert len(row["incoming"]) == 2
    assert {c["upstream_node_id"] for c in row["incoming"]} == set(upstream["buses"][side])
    assert {c["target_node_id"] for c in row["incoming"]} == set(row["buses"]["hv"])
    for chain in row["incoming"]:
        section = chain["section"] - 1
        assert chain["upstream_node_id"] == upstream["buses"][side][section]
        assert chain["target_node_id"] == row["buses"]["hv"][section]
        assert chain in upstream["outgoing"]
        _assert_chain(net, chain["branch_ids"], chain["node_ids"])
        assert isinstance(net.branches[chain["line"]], LineBranch)
        for key in ("outgoing_breaker", "incoming_breaker"):
            q = net.branches[chain[key]]
            assert isinstance(q, TieBranch) and q.switchable and q.normally_closed
        assert {net.nodes[n].u_nom for n in chain["node_ids"]} == {row["hv_kv"]}


@pytest.mark.parametrize("fid", FACILITIES)
@pytest.mark.parametrize("section", SECTIONS)
def test_both_transformer_sides_have_distinct_real_breakers(data, facilities, fid, section):
    net, _ = data
    row = facilities[fid]
    tr = row["transformers"][section - 1]
    branch = net.branches[tr["id"]]
    assert isinstance(branch, TransformerBranch) and not branch.switchable
    assert tr["hv_bus"] == row["buses"]["hv"][section - 1]
    assert tr["lv_bus"] == row["buses"]["lv"][section - 1]
    assert tr["hv_breaker"] != tr["lv_breaker"]
    _assert_chain(net, tr["branch_ids"], tr["node_ids"])
    for key in ("hv_breaker", "lv_breaker"):
        q = net.branches[tr[key]]
        assert isinstance(q, TieBranch) and q.switchable and q.normally_closed
    assert (branch.u_hv, branch.u_lv) == (row["hv_kv"], row["lv_kv"])
    expected_ct = tr["node_ids"][2 if fid == "gtes" else 1]
    assert branch.ct_node == expected_ct


@pytest.mark.parametrize("fid", FACILITIES)
@pytest.mark.parametrize("section", SECTIONS)
def test_isolated_transformer_and_correct_bus_tie_restore_all_load_reachability(data, facilities, fid, section):
    net, _ = data
    row = facilities[fid]
    tr = row["transformers"][section - 1]
    opened = {tr["hv_breaker"]: False, tr["lv_breaker"]: False}
    unavailable = {tr["id"]: False}
    outage = Mode(f"test_{fid}_t{section}_out", "test", states=opened, availability=unavailable)
    assert _load_nodes(net) - net.energized_nodes(outage)
    # The GTES transformer supplies 110 kV from 10 kV. For downstream sites
    # it is the LV bus coupler that transfers their interrupted demand.
    side = "hv" if fid == "gtes" else "lv"
    repair = Mode(f"test_{fid}_t{section}_repair", "test",
                  states={**opened, row["bus_ties"][side]: True}, availability=unavailable)
    assert _load_nodes(net) <= net.energized_nodes(repair)
    assert not net.branch_conducting(net.branches[tr["id"]], repair)
    assert not net.branch_conducting(net.branches[tr["hv_breaker"]], repair)
    assert not net.branch_conducting(net.branches[tr["lv_breaker"]], repair)
    other_side = "lv" if side == "hv" else "hv"
    assert not net.branch_conducting(net.branches[row["bus_ties"][other_side]], repair)
    assert len(net.modes) == 5  # Exhaustive tests must not multiply saved modes.


@pytest.mark.parametrize("fid", FACILITIES[1:])
@pytest.mark.parametrize("section", SECTIONS)
def test_input_breakers_interrupt_supply_and_hv_tie_restores_it(data, facilities, fid, section):
    net, _ = data
    row = facilities[fid]
    chain = row["incoming"][section - 1]
    for key in ("outgoing_breaker", "incoming_breaker"):
        outage = Mode(f"test_{chain['id']}_{key}", "test", states={chain[key]: False})
        assert _load_nodes(net) - net.energized_nodes(outage)
    repair = Mode(f"test_{chain['id']}_repair", "test",
                  states={chain["outgoing_breaker"]: False, chain["incoming_breaker"]: False,
                          row["bus_ties"]["hv"]: True}, availability={chain["line"]: False})
    assert _load_nodes(net) <= net.energized_nodes(repair)
    assert not net.branch_conducting(net.branches[row["bus_ties"]["lv"]], repair)
    assert all(net.branch_conducting(net.branches[t["id"]], repair) for t in row["transformers"])


@pytest.mark.parametrize("feeder_id", LOAD_FEEDERS)
def test_each_load_feeder_breaker_interrupts_only_its_own_load(data, facilities, feeder_id):
    net, _ = data
    fid = feeder_id.split("_", 1)[0]
    chain = next(x for x in facilities[fid]["loads"] if x["id"] == feeder_id)
    _assert_chain(net, chain["branch_ids"], chain["node_ids"])
    assert isinstance(net.branches[chain["breaker"]], TieBranch)
    assert isinstance(net.branches[chain["line"]], LineBranch)
    outage = Mode(f"test_{feeder_id}", "test", states={chain["breaker"]: False})
    assert _load_nodes(net) - net.energized_nodes(outage) == {chain["load_node_id"]}


def test_generator_breakers_are_independent_and_section_reserve_is_electrical(data, facilities):
    net, _ = data
    plant = facilities["gtes"]
    assert len(plant["generators"]) == 6
    for generator in plant["generators"]:
        q = net.branches[generator["breaker"]]
        assert isinstance(q, TieBranch) and q.normally_closed
        assert (q.node_from, q.node_to) == tuple(generator["node_ids"])
        assert generator["bus"] == plant["buses"]["lv"][generator["section"] - 1]
    for section in SECTIONS:
        opened = {g["breaker"]: False for g in plant["generators"] if g["section"] == section}
        outage = Mode(f"test_generators_section{section}", "test", states=opened)
        assert _load_nodes(net) - net.energized_nodes(outage)
        restored = Mode(outage.id + "_reserve", "test", states={**opened, plant["bus_ties"]["lv"]: True})
        assert _load_nodes(net) <= net.energized_nodes(restored)
        assert not net.branch_conducting(net.branches[plant["bus_ties"]["hv"]], restored)


def test_saved_repair_modes_isolate_both_sides_and_use_only_local_lv_coupler(data, facilities):
    net, _ = data
    for fid in ("cp01", "ps01", "ktp01"):
        mode = net.modes[f"repair_{fid}_t1"]
        row, tr = facilities[fid], facilities[fid]["transformers"][0]
        assert mode.states == {tr["hv_breaker"]: False, tr["lv_breaker"]: False,
                               row["bus_ties"]["lv"]: True}
        assert mode.availability == {tr["id"]: False}
        assert _load_nodes(net) <= net.energized_nodes(mode)


@pytest.fixture(scope="module")
def fault_solver(data):
    net, _ = data
    return ShortCircuitSolver(net, net.modes["normal_max"], Methodology.load())


def test_gtes_three_phase_fault_matches_three_generators_in_parallel(fault_solver):
    result = fault_solver.fault_at("gtes_lv1", FaultSpec(FaultType.THREE_PHASE))
    # No bus ties are closed. Passive downstream feeders draw no current in
    # the unloaded fault model, so only three local 7.5 MVA generators feed
    # this 10 kV bus. Manual base: Uavg=10.5 kV, Rpu=.012, Xpu=.15.
    z_manual = (.012 + .15j) * 10.5**2 / 7.5 / 3
    current_manual = 10.5 / math.sqrt(3) / z_manual
    assert result.z012_ohm[1] == pytest.approx(z_manual, rel=1e-8)
    assert result.iabc_ka[0] == pytest.approx(current_manual, rel=1e-8)
    assert abs(result.residual_current_ka) < 1e-9
    assert all(math.isfinite(abs(i)) for i in result.iabc_ka)


@pytest.mark.parametrize("kind", (FaultType.LINE_LINE, FaultType.LINE_GROUND, FaultType.LINE_LINE_GROUND))
@pytest.mark.parametrize("node", ("gtes_lv1", "ktp24_lv2"))
def test_asymmetric_faults_refuse_unconfirmed_sequence_data(fault_solver, kind, node):
    with pytest.raises(ShortCircuitStatusError) as error:
        fault_solver.fault_at(node, FaultSpec(kind))
    assert error.value.code == "MISSING_SEQUENCE_DATA"
    assert str(error.value)


def test_checked_example_matches_inventory_and_is_portable(saved_project, data, tmp_path):
    net, manifest = data
    project = saved_project
    saved_net = project.network  # One fingerprint/adapter boundary, not per item.
    assert project.metadata["oilfield_demo"]["counts"] == manifest["counts"]
    assert project.metadata["oilfield_demo"]["demo_only"] is True
    assert set(saved_net.nodes) == set(net.nodes)
    assert set(saved_net.branches) == set(net.branches)
    assert set(saved_net.loads) == set(net.loads)
    assert saved_net.validate() == []
    assert project.diagram.validate_targets(project.electrical_model) == ()
    assert len(project.diagram.pages) == 34
    assert not project.calculation_blockers
    before = electrical_model_fingerprint(project.electrical_model)
    destination = tmp_path / "portable-oilfield.json"
    save_project(destination, project)
    again = load_project(destination)
    assert electrical_model_fingerprint(again.electrical_model) == before
    assert set(again.electrical_model.equipment) == set(project.electrical_model.equipment)
    assert set(again.electrical_model.connections) == set(project.electrical_model.connections)
    assert again.diagram == project.diagram
    assert again.metadata["oilfield_demo"] == project.metadata["oilfield_demo"]


@pytest.mark.parametrize("field", ("nodes", "branches", "loads", "modes"))
def test_saved_calculation_view_preserves_every_input_field(saved_project, data, field):
    expected = getattr(data[0], field)
    actual = getattr(saved_project.network, field)
    assert set(actual) == set(expected)
    # No migration exceptions: names, ratings, impedances, CT side, protection
    # flags, normal/sparse switching states and availability all survive exactly.
    for object_id, value in expected.items():
        assert asdict(actual[object_id]) == asdict(value), object_id


@pytest.mark.parametrize("mode_id", ("normal_max", "normal_min", "repair_cp01_t1",
                                    "repair_ps01_t1", "repair_ktp01_t1"))
def test_saved_canonical_topology_matches_switching_and_load_energization(saved_project, data, mode_id):
    model = saved_project.electrical_model
    expected_net = data[0]
    expected_mode = expected_net.modes[mode_id]
    by_legacy = lambda values: {
        item.extensions["legacy_calculation"]["legacy_id"]: item for item in values
    }
    nodes = by_legacy(model.electrical_nodes.values())
    equipment = by_legacy(model.equipment.values())
    states = by_legacy(model.operating_states.values())
    topology = TopologyEngine().compile(model, states[mode_id].id)
    assert not [d for d in topology.diagnostics if d.is_blocking]
    assert len(topology.sources) == 6
    assert all(source.active for source in topology.sources.values())
    # Check actual canonical load terminals, not merely legacy node reachability.
    for load in expected_net.loads.values():
        canonical = equipment[load.id]
        terminals = [p for p in model.ports.values() if p.equipment_id == canonical.id]
        assert len(terminals) == 1
        node_id = topology.port_to_node[terminals[0].id]
        assert node_id == nodes[load.node].id
        assert topology.is_energized(node_id)
    expected_live = expected_net.energized_nodes(expected_mode)
    for legacy_id, node in nodes.items():
        assert topology.is_energized(node.id) == (legacy_id in expected_live), legacy_id
    # Every breaker, including all 68 bus couplers, resolves to the saved mode.
    for branch in expected_net.branches.values():
        canonical_id = equipment[branch.id].id
        links = [topology.links[lid] for lid in topology.link_ids_by_equipment.get(canonical_id, ())]
        if isinstance(branch, TieBranch):
            closed = expected_net.branch_conducting(branch, expected_mode)
            position = topology.operating_state.positions[canonical_id].position
            assert position == (SwitchPosition.CLOSED if closed else SwitchPosition.OPEN)
            assert links and all(link.active == closed for link in links), branch.id
        if isinstance(branch, TransformerBranch):
            available = expected_mode.is_available(branch.id)
            expected_availability = (EquipmentAvailability.IN_SERVICE if available
                                     else EquipmentAvailability.OUT_OF_SERVICE)
            assert topology.operating_state.availability[canonical_id] == expected_availability
            assert links and all(link.active == available for link in links), branch.id


def test_import_writes_no_example_and_cli_requires_explicit_new_path(tmp_path, monkeypatch):
    before = hashlib.sha256(EXAMPLE.read_bytes()).digest()
    monkeypatch.chdir(tmp_path)
    importlib.reload(builder)
    assert list(tmp_path.iterdir()) == []
    assert hashlib.sha256(EXAMPLE.read_bytes()).digest() == before
    with pytest.raises(SystemExit) as missing:
        builder.main([])
    assert missing.value.code == 2
    destination = tmp_path / "keep.json"
    destination.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(builder, "build_project", lambda **kw: pytest.fail("Refusal must precede any build"))
    with pytest.raises(SystemExit) as existing:
        builder.main(["--output", str(destination)])
    assert existing.value.code == 2
    assert destination.read_text(encoding="utf-8") == "keep"
