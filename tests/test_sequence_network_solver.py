"""Network integration checks independent of production fault formulas."""
import math
import cmath
from pathlib import Path

import pytest

from rza_calc.core.fault_types import FaultSpec, FaultType, solve_fault
from rza_calc.core.impedance import branch_impedance, stage_voltage
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, LineBranch, Mode, Network, Node, SourceBranch, TieBranch, TransformerBranch
from rza_calc.core.short_circuit import ShortCircuitSolver, ShortCircuitStatusError
from rza_calc.io.project import load_project


def _source_network():
    net = Network("Проверка четырёх КЗ")
    net.add_node(Node("a", "A", 10))
    source = net.add_branch(SourceBranch(
        "s", "Система", GRID, "a", s_kz_max=110.25, s_kz_min=88.2,
        r2_ohm=0, x2_ohm=2, r0_ohm=0, x0_ohm=3,
        sequence_reference_kv=10.5, zero_sequence_connection="series"))
    net.add_mode(Mode("max", "Максимум"))
    net.add_mode(Mode("min", "Минимум", system="min"))
    return net, source


def _solver(net, mode="max"):
    return ShortCircuitSolver(net, net.modes[mode], Methodology.load())


@pytest.mark.parametrize("kind", tuple(FaultType))
def test_source_network_unequal_sequences_matches_manual_complex_values(kind):
    net, _ = _source_network()
    result = _solver(net).fault_at("a", FaultSpec(kind))
    e = 10.5 / math.sqrt(3)
    expected = {
        FaultType.THREE_PHASE: (0j, -1j, 0j),
        FaultType.LINE_LINE: (0j, -1j / 3, 1j / 3),
        FaultType.LINE_GROUND: (-1j / 6, -1j / 6, -1j / 6),
        FaultType.LINE_LINE_GROUND: (2j / 11, -5j / 11, 3j / 11),
    }[kind]
    assert result.i012_ka == pytest.approx(tuple(value * e for value in expected))
    assert result.node_id == "a" and result.mode_id == "max"


def test_ground_fault_does_not_double_count_a_neutral_already_in_z0():
    net, source = _source_network()
    source.r0_ohm = 3 * 0.5
    result = _solver(net).fault_at("a", FaultSpec(FaultType.LINE_GROUND))
    assert result.z012_ohm[0] == pytest.approx(1.5 + 3j)
    expected_ia = (10.5 * math.sqrt(3)) / (1.5 + 6j)
    assert result.iabc_ka[0] == pytest.approx(expected_ia)


def test_complete_line_data_is_converted_from_per_km_in_all_sequences():
    net, _ = _source_network()
    net.add_node(Node("b", "B", 10))
    net.add_branch(LineBranch(
        "line", "Линия", "a", "b", length_km=4, n_parallel=2,
        r0=0.1, x0=0.2, r2_ohm_per_km=0.3, x2_ohm_per_km=0.4,
        r0_ohm_per_km=0.5, x0_ohm_per_km=0.6))
    result = _solver(net).fault_at("b", FaultSpec(FaultType.LINE_GROUND))
    assert result.z012_ohm == pytest.approx((1 + 4.2j, 0.2 + 1.4j, 0.6 + 2.8j))
    assert result.iabc_ka[0] == pytest.approx(10.5 * math.sqrt(3) / (1.8 + 8.4j))


def test_minimum_regime_applies_line_temperature_and_keeps_explicit_source_data():
    net, _ = _source_network()
    net.add_node(Node("b", "B", 10))
    net.add_branch(LineBranch("l", "Линия", "a", "b", length_km=1,
        r0=0.1, x0=0.2, r2_ohm_per_km=0.3, x2_ohm_per_km=0.4,
        r0_ohm_per_km=0.5, x0_ohm_per_km=0.6))
    method = Methodology.load()
    factor = method.k("short_circuit.temp_factor_min")
    result = _solver(net, "min").fault_at("b", FaultSpec(FaultType.LINE_GROUND))
    assert result.z012_ohm == pytest.approx((0.5 * factor + 3.6j,
        0.1 * factor + 1.45j, 0.3 * factor + 2.4j))


def test_missing_sequence_data_blocks_only_the_requested_asymmetric_fault():
    net, source = _source_network()
    source.r2_ohm = source.x2_ohm = source.r0_ohm = source.x0_ohm = None
    solver = _solver(net)
    assert abs(solver.fault_at("a", FaultSpec("3ph")).iabc_ka[0]) == pytest.approx(solver.at("a").i3)
    with pytest.raises(ShortCircuitStatusError) as error:
        solver.fault_at("a", FaultSpec("2ph"))
    assert error.value.code == "MISSING_SEQUENCE_DATA" and "Система" in str(error.value)
    source.negative_sequence_equal_positive = True
    ll = _solver(net).fault_at("a", FaultSpec("2ph"))
    assert abs(ll.iabc_ka[1]) == pytest.approx(solver.at("a").i3 * math.sqrt(3) / 2)
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert error.value.code == "MISSING_SEQUENCE_DATA"


def test_separate_energized_island_missing_z0_does_not_block_this_island():
    net, _ = _source_network()
    before = _solver(net).fault_at("a", FaultSpec("1ph_g"))
    net.add_node(Node("island", "Другой остров", 10))
    net.add_branch(SourceBranch("other", "Другая система", GRID, "island", s_kz_max=100, s_kz_min=80))
    after = _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert after.iabc_ka == pytest.approx(before.iabc_ka)


def test_known_blocked_zero_return_is_not_reported_as_missing_impedance():
    net, source = _source_network()
    source.zero_sequence_connection = "blocked"
    source.r0_ohm = source.x0_ohm = None
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert error.value.code == "NO_ZERO_SEQUENCE_RETURN_PATH"
    assert abs(_solver(net).fault_at("a", FaultSpec("2ph")).iabc_ka[1]) > 0


def _transformer_network():
    net = Network("Шунт нулевой последовательности трансформатора")
    net.add_node(Node("hv", "ВН", 35))
    net.add_node(Node("lv", "НН", 10))
    net.add_branch(SourceBranch("s", "Система", GRID, "hv", s_kz_max=1000, s_kz_min=800,
        negative_sequence_equal_positive=True, zero_sequence_connection="blocked"))
    transformer = net.add_branch(TransformerBranch("t", "Трансформатор", "hv", "lv",
        s_nom=10000, u_hv=35, u_lv=10, uk=10, p_k=50,
        negative_sequence_equal_positive=True, sequence_phase_shift_deg=30,
        r0_ohm=0.2, x0_ohm=2, sequence_reference_kv=10.5, zero_sequence_connection="to_ground"))
    net.add_mode(Mode("max", "Максимум"))
    return net, transformer


def test_grounded_low_side_delta_equivalent_is_a_shunt_and_blocks_hv_transfer():
    net, transformer = _transformer_network()
    result = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert result.z012_ohm[0] == pytest.approx(0.2 + 2j)
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("hv", FaultSpec("1ph_g"))
    assert error.value.code == "NO_ZERO_SEQUENCE_RETURN_PATH"
    transformer.zero_sequence_connection = "blocked"
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert error.value.code == "NO_ZERO_SEQUENCE_RETURN_PATH"


def test_zero_sequence_data_behind_a_transformer_barrier_is_not_required():
    net, transformer = _transformer_network()
    source = net.branches["s"]
    source.zero_sequence_connection = "series"
    source.r0_ohm = source.x0_ohm = None
    result = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert result.z012_ohm[0] == pytest.approx(0.2 + 2j)
    source.r0_ohm, source.x0_ohm, source.sequence_reference_kv = 1, 999, 37
    changed = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert changed.iabc_ka == pytest.approx(result.iabc_ka)


def test_lossless_phase_shifting_transformer_removes_only_negative_roundoff():
    net, transformer = _transformer_network()
    transformer.p_k = None
    transformer.r0_ohm = 0
    result = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert all(z.real >= 0 for z in result.z012_ohm)


def test_finite_phase_shifter_parallel_to_ideal_tie_retains_diagonal_stamp():
    from rza_calc.core.sequence_network import SequenceFaultSolver, _Stamp
    net, _ = _source_network()
    model = SequenceFaultSolver(_solver(net))
    stamps = [_Stamp("s", GRID, "a", 1j), _Stamp("q", "a", "b", 0j),
              _Stamp("t", "a", "b", 1j, cmath.exp(-1j * math.pi / 6))]
    z = model._driving_impedance({"a", "b"}, stamps, "a", 1)
    assert z == pytest.approx(1j / (3 - math.sqrt(3)))


def test_grounded_transformer_series_model_includes_the_source_zero_impedance():
    net, transformer = _transformer_network()
    transformer.zero_sequence_connection = "series"
    source = net.branches["s"]
    source.zero_sequence_connection = "series"
    source.r0_ohm, source.x0_ohm, source.sequence_reference_kv = 1, 3, 37
    result = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert result.z012_ohm[0] == pytest.approx(0.2 + 2j + (1 + 3j) * (10.5 / 37) ** 2)


def test_asymmetric_transformer_requires_an_explicit_phase_shift_including_zero():
    net, transformer = _transformer_network()
    transformer.sequence_phase_shift_deg = None
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("lv", FaultSpec("2ph"))
    assert error.value.code == "MISSING_SEQUENCE_DATA"
    assert "sequence_phase_shift_deg" in str(error.value)
    assert abs(_solver(net).fault_at("lv", FaultSpec("3ph")).iabc_ka[0]) > 0


def test_ideal_switch_connectivity_is_applied_before_the_sequence_matrix():
    net, _ = _source_network()
    net.add_node(Node("b", "B", 10))
    net.add_branch(TieBranch("q", "Q", "a", "b", normally_closed=True))
    solver = _solver(net)
    assert solver.fault_at("a", FaultSpec("1ph_g")).iabc_ka == pytest.approx(solver.fault_at("b", FaultSpec("1ph_g")).iabc_ka)
    net.modes["max"].states["q"] = False
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("b", FaultSpec("1ph_g"))
    assert error.value.code == "NOT_ENERGIZED"


@pytest.mark.parametrize("field,value", [("r2_ohm", float("nan")), ("sequence_reference_kv", 0),
    ("negative_sequence_equal_positive", "yes"), ("r0_ohm", -1)])
def test_invalid_supplied_sequence_parameters_are_never_silently_used(field, value):
    net, source = _source_network()
    setattr(source, field, value)
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert error.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("filename", ["energoraion.json", "ps_promyshlennaya.json", "ps_severnaya.json"])
def test_existing_demo_three_phase_stays_identical(filename):
    path = Path(__file__).resolve().parents[1] / "rza_calc" / "examples" / filename
    project = load_project(path)
    network = project.network
    method = project.methodology
    for mode in network.modes.values():
        solver = ShortCircuitSolver(network, mode, method)
        for node_id in sorted(network.energized_nodes(mode) - {GRID})[::5]:
            old = solver.at(node_id)
            new = solver.fault_at(node_id, FaultSpec("3ph"))
            assert new.iabc_ka[0] == pytest.approx(old.i3_complex, rel=1e-12, abs=1e-12)
