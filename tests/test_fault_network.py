"""Independent phase-boundary, terminal-sign and ideal cut-set checks."""
from dataclasses import FrozenInstanceError
import math

import numpy as np
import pytest

from rza_calc.core.fault_types import FaultSpec, FaultType
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Mode, Network, Node, SourceBranch,
                                 TieBranch, TransformerBranch, Transformer3W)
from rza_calc.core.short_circuit import ShortCircuitSolver, ShortCircuitStatusError

U = 10.5
E = U / math.sqrt(3)


def _node(net, key, voltage=U):
    net.add_node(Node(key, key, voltage, calculation_base_kv=voltage))


def _source(net, key, node, z1=1+2j, z2=1.2+2.4j, z0=2+4j):
    return net.add_branch(SourceBranch(key, key, GRID, node,
        s_kz_max=U**2/abs(z1), s_kz_min=U**2/abs(z1), x_r_ratio=z1.imag/z1.real,
        r2_ohm=z2.real, x2_ohm=z2.imag, r0_ohm=z0.real, x0_ohm=z0.imag,
        sequence_reference_kv=U, zero_sequence_connection="series"))


def _solver(net):
    mode = net.modes.setdefault("max", Mode("max", "max"))
    return ShortCircuitSolver(net, mode, Methodology.load())


def _phases_matrix():
    a = np.exp(2j*np.pi/3)
    return np.array(((1,1,1),(1,a*a,a),(1,a,a*a)), dtype=complex)


def _phase_boundary(z012, spec):
    """Solve a phase-domain Thevenin circuit, without production fault formulas."""
    t = _phases_matrix()
    z = t @ np.diag(z012) @ np.linalg.inv(t)
    e = t @ np.array((0,E,0), dtype=complex)
    f, g = spec.phase_arm_impedance_ohm, spec.common_ground_impedance_ohm
    if spec.kind == FaultType.THREE_PHASE:
        lhs, rhs = z + f*np.eye(3), e
    elif spec.kind == FaultType.LINE_LINE:
        lhs = np.array(((1,0,0),(0,1,1),z[1]-z[2]),dtype=complex)
        lhs[2,1] += 2*f
        rhs = np.array((0,0,e[1]-e[2]))
    elif spec.kind == FaultType.LINE_GROUND:
        lhs = np.array(((0,1,0),(0,0,1),z[0]),dtype=complex)
        lhs[2,0] += f+g
        rhs = np.array((0,0,e[0]))
    else:
        lhs = np.array(((1,0,0),z[1],z[2]),dtype=complex)
        lhs[1,1] += f+g
        lhs[1,2] += g
        lhs[2,1] += g
        lhs[2,2] += f+g
        rhs = np.array((0,e[1],e[2]))
    return np.linalg.solve(lhs,rhs), z


@pytest.mark.parametrize("kind", list(FaultType))
@pytest.mark.parametrize("with_impedance", [False,True])
def test_radial_terminals_and_node_voltage_changes_match_independent_phase_boundary(kind, with_impedance):
    net=Network()
    _node(net,"a")
    _node(net,"k")
    zg=(2+4j,1+2j,1.2+2.4j)
    zl=(.7+1j,.3+.4j,.5+.6j)
    _source(net,"g","a",zg[1],zg[2],zg[0])
    net.add_branch(LineBranch("l","l","a","k",length_km=1,r0=zl[1].real,x0=zl[1].imag,
        r2_ohm_per_km=zl[2].real,x2_ohm_per_km=zl[2].imag,
        r0_ohm_per_km=zl[0].real,x0_ohm_per_km=zl[0].imag))
    spec=FaultSpec(kind, .21+.04j if with_impedance else 0j,
        .17+.02j if with_impedance and kind in (FaultType.LINE_GROUND,FaultType.LINE_LINE_GROUND) else 0j)
    expected,zabc=_phase_boundary(tuple(a+b for a,b in zip(zg,zl)),spec)
    solver=_solver(net)
    point_before=solver.fault_at("k",spec)
    result=solver.fault_network_at("k",spec)
    assert result.fault == point_before
    assert result.quantity_kind == "superimposed_fault_contribution"
    assert result.algorithm_version == "sequence-branch-contribution-v1"
    assert result.branches["l"].from_terminal.delta_iabc_ka == pytest.approx(expected)
    assert result.branches["l"].to_terminal.delta_iabc_ka == pytest.approx(-expected)
    assert result.branches["g"].to_terminal.delta_iabc_ka == pytest.approx(-expected)
    assert not result.branches["g"].from_terminal.is_physical
    assert result.branches["g"].from_terminal.delta_iabc_ka is None
    t=_phases_matrix()
    zgabc=t@np.diag(zg)@np.linalg.inv(t)
    assert result.nodes["a"].delta_vabc_kv == pytest.approx(-zgabc@expected)
    assert result.nodes["k"].delta_vabc_kv == pytest.approx(-zabc@expected)
    assert result.nodes["a"].vabc_kv is None
    assert result.nodes["k"].vabc_kv == point_before.vabc_kv
    assert result.branches["l"].from_terminal.residual_current_ka == pytest.approx(sum(expected))


@pytest.mark.parametrize("kind",list(FaultType))
def test_two_unequal_parallel_sources_share_by_complex_admittance(kind):
    net=Network()
    _node(net,"k")
    zfirst=(2+4j,1+2j,1.2+2.4j)
    zsecond=(3+5j,2+3j,2.5+4j)
    _source(net,"g1","k",zfirst[1],zfirst[2],zfirst[0])
    _source(net,"g2","k",zsecond[1],zsecond[2],zsecond[0])
    result=_solver(net).fault_network_at("k",FaultSpec(kind))
    for index in range(3):
        fault_current=result.fault.i012_ka[index]
        first=-fault_current*zsecond[index]/(zfirst[index]+zsecond[index])
        second=-fault_current*zfirst[index]/(zfirst[index]+zsecond[index])
        assert result.branches["g1"].to_terminal.delta_i012_ka[index] == pytest.approx(first,abs=1e-12)
        assert result.branches["g2"].to_terminal.delta_i012_ka[index] == pytest.approx(second,abs=1e-12)
        assert first+second+fault_current == pytest.approx(0,abs=1e-12)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("base", [.4, U, 110])
@pytest.mark.parametrize("ideal_ground", [False, True])
def test_delta_wye_grounded_transformer_has_distinct_physical_terminal_currents(reverse, base, ideal_ground):
    net=Network()
    _node(net,"hv")
    _node(net,"lv",.4)
    _source(net,"g","hv")
    net.branches["g"].zero_sequence_connection="blocked"
    ends = ("lv", "hv") if reverse else ("hv", "lv")
    net.add_branch(TransformerBranch("t","t",*ends,s_nom=1000,u_hv=U,u_lv=.4,uk=5.5,p_k=12.2,
        negative_sequence_equal_positive=True,sequence_phase_shift_deg=-30 if reverse else 30,
        r0_ohm=0 if ideal_ground else .001952,
        x0_ohm=0 if ideal_ground else math.sqrt(.0088**2-.001952**2),
        sequence_reference_kv=.4,zero_sequence_connection="from_ground" if reverse else "to_ground"))
    result=ShortCircuitSolver(net,Mode("max","max"),Methodology.load(),u_base=base).fault_network_at("lv",FaultSpec("1ph_g"))
    tr=result.branches["t"]
    high, low = (tr.to_terminal, tr.from_terminal) if reverse else (tr.from_terminal, tr.to_terminal)
    fault=result.fault.i012_ka
    ratio=.4/U
    assert high.voltage_stage_kv == U
    assert low.voltage_stage_kv == .4
    assert high.delta_i012_ka[0] == 0j
    assert low.delta_i012_ka[0] == pytest.approx(-fault[0])
    for sequence, angle in ((1,-math.pi/6),(2,math.pi/6)):
        expected=fault[sequence]*ratio*np.exp(1j*angle)
        assert high.delta_i012_ka[sequence] == pytest.approx(expected)
        assert low.delta_i012_ka[sequence] == pytest.approx(-fault[sequence])
        assert result.branches["g"].to_terminal.delta_i012_ka[sequence] == pytest.approx(-expected)
    assert result.nodes["hv"].delta_v012_kv[0] is None
    assert result.nodes["hv"].delta_vabc_kv is None
    assert result.nodes["hv"].vabc_kv is None


def test_an_unrelated_island_with_missing_sequences_has_zero_contribution():
    net=Network()
    _node(net,"k")
    _node(net,"other")
    _source(net,"g","k")
    net.add_branch(SourceBranch("other_g","other_g",GRID,"other",s_kz_max=50))
    result=_solver(net).fault_network_at("k",FaultSpec("1ph_g"))
    assert result.nodes["other"].delta_vabc_kv == (0j,0j,0j)
    assert result.nodes["other"].vabc_kv is None
    assert result.branches["other_g"].to_terminal.delta_iabc_ka == (0j,0j,0j)


@pytest.mark.parametrize("reverse",[False,True])
@pytest.mark.parametrize("kind",list(FaultType))
def test_ideal_forest_kirchhoff_currents_respect_actual_terminal_orientation(reverse,kind):
    net=Network()
    _node(net,"a")
    _node(net,"k")
    _source(net,"g","a")
    ends=("k","a") if reverse else ("a","k")
    net.add_branch(TieBranch("q","q",*ends,normally_closed=True))
    result=_solver(net).fault_network_at("k",FaultSpec(kind))
    direction=-1 if reverse else 1
    assert result.branches["q"].from_terminal.delta_iabc_ka == pytest.approx(
        tuple(direction*i for i in result.fault.iabc_ka))
    assert result.branches["q"].to_terminal.delta_iabc_ka == pytest.approx(
        tuple(-direction*i for i in result.fault.iabc_ka))
    assert result.branches["q"].is_complete


def test_parallel_ideal_cycle_is_ambiguous_but_its_tail_bridge_and_fault_are_available():
    net=Network()
    for key in ("a","b","k"):
        _node(net,key)
    _source(net,"g","a")
    net.add_branch(TieBranch("q1","q1","a","b",normally_closed=True))
    net.add_branch(TieBranch("q2","q2","a","b",normally_closed=True))
    net.add_branch(TieBranch("tail","tail","b","k",normally_closed=True))
    result=_solver(net).fault_network_at("k",FaultSpec("3ph"))
    for key in ("q1","q2"):
        assert result.branches[key].from_terminal.delta_i012_ka[1] is None
        assert result.branches[key].from_terminal.delta_iabc_ka is None
        assert not result.branches[key].is_complete
        assert "AMBIGUOUS_IDEAL_CURRENT" in {issue.code for issue in result.branches[key].diagnostics}
    assert result.branches["tail"].from_terminal.delta_iabc_ka == pytest.approx(result.fault.iabc_ka)
    assert result.branches["g"].to_terminal.delta_iabc_ka == pytest.approx(tuple(-i for i in result.fault.iabc_ka))
    net.modes["max"].states["q2"]=False
    radial=_solver(net).fault_network_at("k",FaultSpec("3ph"))
    assert radial.branches["q1"].is_complete
    assert radial.branches["q2"].from_terminal.delta_iabc_ka == (0j,0j,0j)


def test_result_is_immutable_and_does_not_guess_missing_ground_data():
    net=Network()
    _node(net,"k")
    _source(net,"g","k")
    result=_solver(net).fault_network_at("k",FaultSpec("3ph"))
    with pytest.raises(TypeError):
        result.branches["other"]=result.branches["g"]
    with pytest.raises(FrozenInstanceError):
        result.base_voltage_kv=1
    net.branches["g"].r0_ohm=None
    with pytest.raises(ShortCircuitStatusError) as caught:
        _solver(net).fault_network_at("k",FaultSpec("1ph_g"))
    assert caught.value.code == "MISSING_SEQUENCE_DATA"


def test_missing_stage_in_disconnected_island_does_not_block_available_fault():
    net = Network()
    _node(net, "k")
    _source(net, "g", "k")
    net.add_node(Node("other", "other", 13))  # No mapping to a calculation stage.
    net.add_branch(TieBranch("open", "open", "k", "other", normally_closed=False))
    solver = _solver(net)
    point = solver.fault_at("k", FaultSpec("1ph_g"))
    result = solver.fault_network_at("k", FaultSpec("1ph_g"))
    assert result.fault == point
    assert result.nodes["other"].voltage_stage_kv is None
    assert result.nodes["other"].delta_vabc_kv == (0j, 0j, 0j)
    assert result.branches["open"].to_terminal.voltage_stage_kv is None
    assert result.branches["open"].to_terminal.delta_iabc_ka == (0j, 0j, 0j)
    assert {d.code for d in result.nodes["other"].diagnostics} == {"UNAVAILABLE_UNRELATED_VOLTAGE_STAGE"}


def test_three_winding_star_is_not_a_physical_ct_terminal():
    net = Network()
    for name, voltage in (("hv", U), ("mv", 6.3), ("lv", .4)):
        _node(net, name, voltage)
    _source(net, "g", "hv")
    transformer = Transformer3W("t", "t", "hv", "mv", "lv", 1000, U, 6.3, .4, 10, 12, 6)
    net.add_transformer3w(transformer)
    net.nodes[transformer.star_node_id].calculation_base_kv = U
    result = _solver(net).fault_network_at("lv", FaultSpec("3ph"))
    assert not result.nodes[transformer.star_node_id].is_physical
    for branch_id in transformer.branch_ids:
        branch = result.branches[branch_id]
        for terminal in (branch.from_terminal, branch.to_terminal):
            if terminal.node_id == transformer.star_node_id:
                assert not terminal.is_physical
                assert terminal.delta_iabc_ka is None
                assert terminal.delta_i012_ka == (None, None, None)
                assert terminal.diagnostics[0].code == "INTERNAL_TRANSFORMER_TERMINAL"
            else:
                assert terminal.is_physical
                assert terminal.is_complete
    assert result.branches["t_lv"].to_terminal.delta_iabc_ka == pytest.approx(
        tuple(-i for i in result.fault.iabc_ka))


def test_nonfinite_derived_voltage_is_structured_failure(monkeypatch):
    from rza_calc.core.sequence_network import SequenceFaultSolver
    net = Network()
    _node(net, "k")
    _source(net, "g", "k", z1=.01+.02j)
    original = SequenceFaultSolver._driving_impedance

    def extreme_response(self, *args, **kwargs):
        impedance = original(self, *args, **kwargs)
        if kwargs.get("response") is not None:
            kwargs["response"]["column"]["k"] = 1e308+1e308j
        return impedance

    monkeypatch.setattr(SequenceFaultSolver, "_driving_impedance", extreme_response)
    solver = _solver(net)
    assert all(math.isfinite(abs(i)) for i in solver.fault_at("k", FaultSpec("3ph")).iabc_ka)
    with pytest.raises(ShortCircuitStatusError) as caught:
        solver.fault_network_at("k", FaultSpec("3ph"))
    assert caught.value.code == "NUMERIC_FAILURE"


@pytest.mark.parametrize("kind", list(FaultType))
def test_exact_ideal_threshold_matches_existing_point_voltage(kind):
    net = Network()
    _node(net, "a")
    _node(net, "k")
    _source(net, "g", "a")
    net.add_branch(LineBranch("l", "l", "a", "k", length_km=1, r0=.1, x0=0,
        r2_ohm_per_km=.1, x2_ohm_per_km=0, r0_ohm_per_km=.1, x0_ohm_per_km=0))
    methodology = Methodology.load()
    methodology.data["short_circuit"]["zero_impedance_ohm"]["value"] = .1
    solver = ShortCircuitSolver(net, Mode("max", "max"), methodology)
    spec = FaultSpec(kind, .02)
    point = solver.fault_at("k", spec)
    result = solver.fault_network_at("k", spec)
    assert result.fault == point
    assert result.nodes["k"].delta_v012_kv == pytest.approx(
        tuple(v - (E if s == 1 else 0j) for s, v in enumerate(point.v012_kv)), abs=1e-10)
    assert result.branches["l"].from_terminal.delta_iabc_ka == pytest.approx(point.iabc_ka)
    # Retain each existing point algorithm's threshold policy; no hidden change.
    if kind is FaultType.THREE_PHASE:
        assert result.nodes["a"].delta_v012_kv[1] - result.nodes["k"].delta_v012_kv[1] == pytest.approx(
            .1 * point.i012_ka[1])
    else:
        assert result.nodes["a"].delta_v012_kv == pytest.approx(result.nodes["k"].delta_v012_kv)
