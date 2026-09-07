"""Absent extension data preserves old passports; supplied data invalidates them."""
import pytest

from rza_calc.core.fingerprint import network_fingerprint
from rza_calc.core.model import GRID, Network, Node, SourceBranch, LineBranch, Mode


def _legacy_witness():
    net = Network("Legacy fingerprint witness")
    net.add_node(Node("a", "A", 10))
    net.add_node(Node("b", "B", 10))
    net.add_branch(SourceBranch("s", "S", GRID, "a", s_kz_max=200, s_kz_min=100))
    net.add_branch(LineBranch("l", "L", "a", "b", length_km=2, r0=.1, x0=.2))
    net.add_mode(Mode("max", "Max"))
    return net


def test_no_sequence_data_matches_hash_measured_before_the_extension():
    # Recorded by the unchanged original application on 2026-09-07.
    assert network_fingerprint(_legacy_witness()) == "670d5a10f24629616f264d55387688a44c7971762dd8403dee4d7f9654e78a03"


@pytest.mark.parametrize("field,value", [
    ("r2_ohm", 0.0), ("x2_ohm", .2), ("r0_ohm", 0.0), ("x0_ohm", .3),
    ("sequence_reference_kv", 10.5), ("sequence_phase_shift_deg", 0.0),
    ("zero_sequence_connection", "blocked"), ("negative_sequence_equal_positive", True),
    ("r2_ohm_per_km", .1), ("x2_ohm_per_km", .1),
    ("r0_ohm_per_km", .3), ("x0_ohm_per_km", .3),
])
def test_each_supplied_sequence_input_changes_the_model_fingerprint(field, value):
    net = _legacy_witness()
    before = network_fingerprint(net)
    setattr(net.branches["l"], field, value)
    assert network_fingerprint(net) != before
