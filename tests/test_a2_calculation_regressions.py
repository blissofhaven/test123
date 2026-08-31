"""A2.1: transformer boundaries and nameplate inrush-current regressions.

Synthetic data are local to these tests. No demo or calculation baseline is
changed. Numeric comparisons below concern scalar arithmetic, not an empirical
fit to solver output: relative tolerance 1e-12 allows ordinary binary64 rounding
in S / (sqrt(3) * U) and multiplication by the two declared coefficients.
Topology sets and accepted discrete settings are compared exactly.
"""
from __future__ import annotations

import math

import pytest

from rza_calc.core.engine import run
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID, LineBranch, Load, Mode, Network, Node, SourceBranch,
    Transformer3W, TransformerBranch,
)
from rza_calc.core.protections.to import _inrush_voltage


ARITHMETIC_REL_TOL = 1e-12


def _backup_network(*, transformer_first: bool, with_bypass: bool = True) -> Network:
    """The bypass is a permitted route even when a transformer is also present."""
    net = Network("A2.1 backup order")
    for node_id in ("src", "bus", "b", "end"):
        net.add_node(Node(node_id, node_id, 10))
    net.add_branch(SourceBranch(
        id="S", name="S", node_from=GRID, node_to="src",
        s_kz_max=250, s_kz_min=170,
    ))
    net.add_branch(LineBranch(
        id="F", name="F", node_from="src", node_to="bus",
        length_km=2, r0=.249, x0=.427, ct_ratio=(300, 5),
    ))
    transformer = TransformerBranch(
        id="T", name="T 10/10", node_from="bus", node_to="b",
        s_nom=630, u_hv=10, u_lv=10, uk=5.5,
    )
    bypass = LineBranch(
        id="B", name="B", node_from="bus", node_to="b",
        length_km=1, r0=.249, x0=.427,
    )
    ordered = [transformer, bypass] if transformer_first else [bypass, transformer]
    for branch in ordered:
        if branch is transformer or with_bypass:
            net.add_branch(branch)
    net.add_branch(LineBranch(
        id="E", name="E", node_from="b", node_to="end",
        length_km=4, r0=.249, x0=.427,
    ))
    net.add_load(Load("L", "L", "end", p_kw=300))
    net.add_mode(Mode("max", "max", system="max"))
    return net


@pytest.mark.parametrize("transformer_first", [True, False])
def test_backup_keeps_permitted_path_in_either_declaration_order(transformer_first):
    net = _backup_network(transformer_first=transformer_first)
    assert net.validate() == []
    result = run(net, Methodology.load())
    assert result.ctx.errors == {}
    backup = result.ctx.zone_points(net.branches["F"], net.modes["max"])[1]

    assert {point.node_id for point in backup} == {"b", "end"}
    assert not result.ctx.backup_zone_stops_at_transformer(
        net.branches["F"], net.modes["max"])
    assert any(check.name == "Kч в зоне резервирования"
               for check in result.get("F", "МТЗ").checks)


@pytest.mark.parametrize("deep", [False, True])
def test_backup_without_bypass_crosses_transformer_only_with_explicit_opt_in(deep):
    net = _backup_network(transformer_first=True, with_bypass=False)
    net.branches["F"].prot.backup_through_transformer = deep
    assert net.validate() == []
    result = run(net, Methodology.load())
    assert result.ctx.errors == {}
    backup = result.ctx.zone_points(net.branches["F"], net.modes["max"])[1]

    assert {point.node_id for point in backup} == ({"b", "end"} if deep else set())
    assert result.ctx.backup_zone_stops_at_transformer(
        net.branches["F"], net.modes["max"]) is (not deep)


def _three_winding_network() -> Network:
    net = Network("A2.1 generated HV leg")
    for node_id, voltage in (("s", 110), ("h", 110), ("m", 35), ("l", 10)):
        net.add_node(Node(node_id, node_id, voltage))
    net.add_branch(SourceBranch(
        id="S", name="S", node_from=GRID, node_to="s",
        s_kz_max=100, s_kz_min=70,
    ))
    feeder = LineBranch(
        id="F", name="F", node_from="s", node_to="h",
        length_km=20, r0=.249, x0=.427, ct_ratio=(300, 5),
    )
    feeder.prot.to_reach = "behind_transformer"
    feeder.prot.i_scale = [20, 30, 40]
    net.add_branch(feeder)
    net.add_transformer3w(Transformer3W(
        id="T", name="T 115/38.5/11", node_hv="h", node_mv="m", node_lv="l",
        s_nom=32000, u_hv=115, u_mv=38.5, u_lv=11,
        uk_hm=10.5, uk_hl=17, uk_ml=7, i_inrush_ratio=8,
    ))
    # The generated star uses its own explicit calculation base. This isolates
    # the winding-nameplate check from the unrelated missing u_avg[115] entry.
    net.nodes["T__star"].calculation_base_kv = 115
    net.add_load(Load("L", "L", "l", p_kw=5000))
    net.add_mode(Mode("max", "max", system="max"))
    return net


def test_generated_three_winding_hv_leg_uses_its_known_nameplate_voltage():
    net = _three_winding_network()
    meth = Methodology.load()
    hv_leg = net.branches["T"]
    assert hv_leg.internal_star_leg
    assert hv_leg.u_hv == hv_leg.u_lv == 115
    voltage, description = _inrush_voltage(meth, hv_leg, 110, "nameplate")
    assert voltage == 115
    assert "паспортное напряжение обмотки" in description

    assert net.validate() == []
    result = run(net, meth)
    assert result.ctx.errors == {}
    protection = result.get("F", "ТО")
    expected_threshold = 1.3 * 8 * 32000 / (math.sqrt(3) * 115)
    # The source-alone external-fault bound is below the inrush threshold;
    # all added series R/X are positive in this synthetic radial network.
    assert 1.2 * 100 * 1000 / (math.sqrt(3) * 115) < expected_threshold
    assert protection.i_calc == pytest.approx(
        expected_threshold, rel=ARITHMETIC_REL_TOL)
    assert protection.i_primary == 1800
    assert protection.i_secondary == 30
    step = next(step for step in protection.steps if "броска" in step.what)
    assert step.given["Напряжение для Iном"] == description


def test_different_nameplate_voltages_are_not_merged_as_duplicate_candidates():
    transformer = TransformerBranch(
        id="T", name="T", node_from="a", node_to="b",
        s_nom=630, u_hv=10.4, u_lv=10.8, uk=5.5,
    )
    voltage, description = _inrush_voltage(
        Methodology.load(), transformer, 10, "nameplate")
    assert voltage == 10
    assert "не определено однозначно" in description


def _inrush_governs_network() -> Network:
    """Choose the source strength and scale boundary analytically, before run().

    A 2 MVA source bounds k_ots * I3 at 1.2*2000/(sqrt(3)*10.5) < 132 A.
    The two inrush conditions are 1.3*5*630/(sqrt(3)*U): about 225 A for
    U=10.5 and 236 A for U=10. Thus inrush governs both calculations. With
    CT 200/5, scale point 5.75 A means 230 A primary, strictly between them;
    the next point 6 A means 240 A. No solver-dependent tolerance or scale
    adjustment is used to obtain the different accepted settings.
    """
    net = Network("A2.1 inrush governs accepted setting")
    for node_id, voltage in (("bus", 10), ("hv", 10), ("lv", .4)):
        net.add_node(Node(node_id, node_id, voltage))
    net.add_branch(SourceBranch(
        id="S", name="S", node_from=GRID, node_to="bus",
        s_kz_max=2, s_kz_min=1.5,
    ))
    feeder = LineBranch(
        id="F", name="F", node_from="bus", node_to="hv",
        length_km=2, r0=.428, x0=.375, ct_ratio=(200, 5),
    )
    feeder.prot.to_reach = "behind_transformer"
    feeder.prot.i_scale = [5, 5.75, 6, 10]
    net.add_branch(feeder)
    net.add_branch(TransformerBranch(
        id="T", name="T 630 kVA", node_from="hv", node_to="lv",
        s_nom=630, u_hv=10, u_lv=.4, uk=5.5, p_k=7.6, i_inrush_ratio=5,
    ))
    net.add_load(Load("L", "L", "lv", p_kw=350))
    net.add_mode(Mode("max", "max", system="max"))
    return net


def test_nameplate_basis_changes_the_accepted_setting_when_inrush_governs():
    expected_nameplate = 1.3 * 5 * 630 / (math.sqrt(3) * 10)
    expected_average = 1.3 * 5 * 630 / (math.sqrt(3) * 10.5)
    assert 1.2 * 2000 / (math.sqrt(3) * 10.5) < expected_average
    assert 200 < expected_average < 230 < expected_nameplate < 240

    accepted = {}
    for basis, expected, primary, secondary in (
        ("nameplate", expected_nameplate, 240, 6),
        ("stage_average", expected_average, 230, 5.75),
    ):
        net = _inrush_governs_network()
        meth = Methodology.load()
        meth.data["to"]["i_nom_basis"]["value"] = basis
        assert net.validate() == []
        result = run(net, meth)
        assert result.ctx.errors == {}
        protection = result.get("F", "ТО")
        assert protection.i_calc == pytest.approx(expected, rel=ARITHMETIC_REL_TOL)
        assert protection.i_primary == primary
        assert protection.i_secondary == secondary
        assert any("Определяющее условие: отстройка от броска намагничивания" in message
                   for message in protection.messages)
        accepted[basis] = protection.i_primary

    assert accepted["nameplate"] > accepted["stage_average"]
