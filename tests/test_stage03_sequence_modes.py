"""Mode-specific sequence inputs choose data, without changing fault equations."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from rza_calc.core.fault_types import FaultSpec
from rza_calc.core.fingerprint import network_fingerprint
from rza_calc.core.model import GRID, GeneratorBranch, LineBranch
from rza_calc.core.short_circuit import ShortCircuitStatusError
from test_sequence_network_solver import _source_network, _solver, _transformer_network
from test_sequence_fingerprint_compatibility import _legacy_witness


def test_selected_system_uses_its_impedances_and_reference_without_mutating_network():
    net, source = _source_network()
    source.sequence_by_system = {
        "max": {"r2_ohm": .2, "x2_ohm": 4, "r0_ohm": .3, "x0_ohm": 5, "sequence_reference_kv": 10.5},
        "min": {"r2_ohm": .8, "x2_ohm": 24, "r0_ohm": 1.2, "x0_ohm": 28, "sequence_reference_kv": 21},
    }
    before = deepcopy(asdict(source))
    maximum = _solver(net, "max").fault_at("a", FaultSpec("1ph_g"))
    minimum = _solver(net, "min").fault_at("a", FaultSpec("1ph_g"))
    assert maximum.z012_ohm == pytest.approx((.3+5j, 1j, .2+4j))
    assert minimum.z012_ohm == pytest.approx((.3+7j, 1.25j, .2+6j))
    assert maximum.iabc_ka != minimum.iabc_ka
    assert asdict(source) == before


def test_partial_mode_input_and_null_fall_back_per_field_to_common_values():
    net, source = _source_network()
    source.sequence_by_system = {"min": {"x2_ohm": 7, "r2_ohm": None, "sequence_reference_kv": None}}
    maximum = _solver(net).fault_at("a", FaultSpec("2ph"))
    minimum = _solver(net, "min").fault_at("a", FaultSpec("2ph"))
    assert maximum.z012_ohm[2] == pytest.approx(2j)
    assert minimum.z012_ohm[2] == pytest.approx(7j)


def test_empty_new_inputs_preserve_legacy_fingerprint_nonempty_inputs_change_it():
    net = _legacy_witness()
    assert network_fingerprint(net) == "670d5a10f24629616f264d55387688a44c7971762dd8403dee4d7f9654e78a03"
    branch = next(iter(net.branches.values()))
    branch.sequence_by_system = {"max": {"r2_ohm": 0}}
    assert network_fingerprint(net) != "670d5a10f24629616f264d55387688a44c7971762dd8403dee4d7f9654e78a03"


def test_unconfirmed_selected_sequence_field_blocks_only_faults_using_it():
    net, source = _source_network()
    source.sequence_by_system = {"min": {"x2_ohm": 7}}
    source.parameter_provenance = {
        "sequence_by_system.min.x2_ohm": {"source": "Паспорт", "confirmation": "unconfirmed"}}
    assert _solver(net).fault_at("a", FaultSpec("2ph"))
    assert _solver(net, "min").fault_at("a", FaultSpec("3ph"))
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net, "min").fault_at("a", FaultSpec("2ph"))
    assert error.value.code == "UNCONFIRMED_INPUT"


@pytest.mark.parametrize("kind", ("3ph", "2ph", "1ph_g", "2ph_g"))
@pytest.mark.parametrize("mode", ("max", "min"))
def test_generator_mode_input_matches_equivalent_explicit_common_network(kind, mode):
    net, _ = _source_network()
    generator = GeneratorBranch("s", "Генератор", GRID, "a", s_nom=10000, u_nom=10.5, xd2=.2,
        r2_ohm=0, x2_ohm=2, r0_ohm=0, x0_ohm=3, sequence_reference_kv=10.5,
        zero_sequence_connection="series",
        sequence_by_system={"max": {"r2_ohm": .2, "x2_ohm": 4},
                            "min": {"r2_ohm": .3, "x2_ohm": 6, "r0_ohm": .5, "x0_ohm": 7}})
    net.branches["s"] = generator
    reference = deepcopy(net)
    expected_branch = reference.branches["s"]
    for key, value in expected_branch.sequence_by_system[mode].items():
        setattr(expected_branch, key, value)
    expected_branch.sequence_by_system = {}
    expected = _solver(reference, mode).fault_at("a", FaultSpec(kind))
    got = _solver(net, mode).fault_at("a", FaultSpec(kind))
    assert got.iabc_ka == pytest.approx(expected.iabc_ka)
    assert got.vabc_kv == pytest.approx(expected.vabc_kv)
    assert got.z012_ohm == pytest.approx(expected.z012_ohm)


@pytest.mark.parametrize("info", ({"source": "", "confirmation": "confirmed"},
                                  {"source": "Паспорт", "confirmation": "unconfirmed"}))
def test_unconfirmed_common_zero_input_does_not_block_three_phase_or_line_line(info):
    net, source = _source_network()
    source.parameter_provenance = {"r0_ohm": info}
    assert _solver(net).fault_at("a", FaultSpec("3ph"))
    assert _solver(net).fault_at("a", FaultSpec("2ph"))
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert error.value.code == "UNCONFIRMED_INPUT"
    assert "R0" in str(error.value)


def test_selected_mode_metadata_is_used_instead_of_common_or_other_mode_metadata():
    net, source = _source_network()
    source.sequence_by_system = {"min": {"x2_ohm": 7}}
    source.parameter_provenance = {
        "x2_ohm": {"source": "Общий паспорт", "confirmation": "unconfirmed"},
        "sequence_by_system.min.x2_ohm": {"source": "Минимальная схема", "confirmation": "confirmed"},
    }
    assert _solver(net, "min").fault_at("a", FaultSpec("2ph")).z012_ohm[2] == pytest.approx(7j)
    with pytest.raises(ShortCircuitStatusError, match="X2"):
        _solver(net, "max").fault_at("a", FaultSpec("2ph"))
    source.sequence_by_system["min"]["x2_ohm"] = None
    with pytest.raises(ShortCircuitStatusError, match="X2"):
        _solver(net, "min").fault_at("a", FaultSpec("2ph"))


def test_unconfirmed_zero_barrier_is_reported_before_it_can_hide_the_return_path():
    net, source = _source_network()
    source.zero_sequence_connection = "blocked"
    source.parameter_provenance = {"zero_sequence_connection": {
        "source": "Схема нейтрали", "confirmation": "unconfirmed"}}
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert error.value.code == "UNCONFIRMED_INPUT"
    source.parameter_provenance["zero_sequence_connection"]["confirmation"] = "confirmed"
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("1ph_g"))
    assert error.value.code == "NO_ZERO_SEQUENCE_RETURN_PATH"


def test_unconfirmed_zero_data_behind_confirmed_transformer_barrier_are_not_used():
    net, transformer = _transformer_network()
    source = net.branches["s"]
    before = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    source.zero_sequence_connection = "blocked"
    source.parameter_provenance = {key: {"source": "", "confirmation": "unconfirmed"}
                                   for key in ("zero_sequence_connection", "r0_ohm", "x0_ohm")}
    after = _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert after.iabc_ka == pytest.approx(before.iabc_ka)
    transformer.parameter_provenance = {"zero_sequence_connection": {
        "source": "Схема", "confirmation": "unconfirmed"}}
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("lv", FaultSpec("1ph_g"))
    assert error.value.code == "UNCONFIRMED_INPUT"


def test_phase_shift_and_equal_sequence_assumption_need_confirmation_only_when_used():
    net, transformer = _transformer_network()
    transformer.parameter_provenance = {"sequence_phase_shift_deg": {
        "source": "Схема", "confirmation": "unconfirmed"}}
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("lv", FaultSpec("2ph"))
    assert error.value.code == "UNCONFIRMED_INPUT"
    net, source = _source_network()
    source.parameter_provenance = {"negative_sequence_equal_positive": {
        "source": "", "confirmation": "unconfirmed"}}
    assert _solver(net).fault_at("a", FaultSpec("2ph"))  # false is unused
    source.negative_sequence_equal_positive = True
    source.r2_ohm = source.x2_ohm = None
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net).fault_at("a", FaultSpec("2ph"))
    assert error.value.code == "UNCONFIRMED_INPUT"


def test_provenance_nonempty_changes_fingerprint_and_nested_maps_are_independent():
    first, source = _source_network()
    before = network_fingerprint(first)
    source.parameter_provenance["r2_ohm"] = {"source": "Паспорт", "confirmation": "confirmed"}
    assert network_fingerprint(first) != before
    copied = deepcopy(first)
    copied.branches["s"].parameter_provenance["r2_ohm"]["confirmation"] = "unconfirmed"
    assert source.parameter_provenance["r2_ohm"]["confirmation"] == "confirmed"
    assert network_fingerprint(copied) != network_fingerprint(first)


@pytest.mark.parametrize("value", (True, float("nan"), -1))
def test_invalid_selected_mode_values_are_not_replaced_with_common_values(value):
    net, source = _source_network()
    source.sequence_by_system = {"min": {"r2_ohm": value}}
    assert _solver(net).fault_at("a", FaultSpec("2ph"))
    with pytest.raises(ShortCircuitStatusError) as error:
        _solver(net, "min").fault_at("a", FaultSpec("2ph"))
    assert error.value.code == "INVALID_INPUT"
