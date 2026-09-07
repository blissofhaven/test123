"""Stage 1: independently specified TO mode and transformer-inrush boundaries."""
from __future__ import annotations

import math

import pytest

from rza_calc.core.context import Context
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID, LineBranch, Mode, Network, Node, SourceBranch, TieBranch,
    TransformerBranch,
)
from rza_calc.core.protections.to import calc_to
from rza_calc.core.result import FAIL, OK, UNRESOLVED


def _calculate(net, branch_id, methodology=None):
    ctx = Context(net, methodology or Methodology.load())
    assert not ctx.errors
    return calc_to(ctx, net.branches[branch_id])


def _coverage(result, text):
    return next(row for row in result.coverage if text in row.name)


def _inrush_steps(result):
    return [step for step in result.steps if "броска тока намагничивания" in step.what]


def _line_modes():
    net = Network("A source labelled min can be the strongest topology")
    for node_id in ("a", "b"):
        net.add_node(Node(node_id, node_id, 10))
    net.add_branch(SourceBranch("weak", "weak", GRID, "a", s_kz_max=100, s_kz_min=80))
    net.add_branch(SourceBranch("strong", "strong", GRID, "a", s_kz_max=500,
                                s_kz_min=400, switchable=True, normally_closed=False))
    line = LineBranch("F", "F", "a", "b", length_km=11, r0=.1, x0=.1,
                      ct_ratio=(500, 5), ct_node="a")
    line.prot.mtz = False
    line.prot.to = True
    net.add_branch(line)
    net.add_mode(Mode("weak_max", "weak max", {"strong": False}, system="max"))
    net.add_mode(Mode("strong_min", "strong min", {"strong": True}, system="min"))
    return net


def test_external_selection_includes_stronger_min_topology_with_manual_oracle():
    result = _calculate(_line_modes(), "F")
    # Hand series/parallel circuit: the min topology has S=80+400 MVA.
    # Default min conductor resistance factor is 1.2; U=10.5 kV.
    z = complex(11 * .1 * 1.2, 11 * .1 + 10.5**2 / 480)
    expected = 1.2 * 10500 / (math.sqrt(3) * abs(z))
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert result.i_primary == 4000
    assert result.governing_mode == "strong min"
    assert result.status == FAIL  # Raising the setting exposes inadequate sensitivity.
    assert _coverage(result, "внешнего КЗ").checked == 2
    assert _coverage(result, "внешнего КЗ").complete


def test_mode_order_and_names_do_not_change_the_current_or_equal_mode_set():
    net = _line_modes()
    net.modes.pop("weak_max")
    net.add_mode(Mode("duplicate", "equal topology", {"strong": True}, system="min"))
    first = _calculate(net, "F")
    net.modes = dict(reversed(list(net.modes.items())))
    net.modes["duplicate"].name = "renamed"
    second = _calculate(net, "F")
    assert first.i_calc == pytest.approx(second.i_calc, rel=1e-12)
    assert first.governing_mode == "strong min"
    assert second.governing_mode == "renamed"
    for result in (first, second):
        step = next(s for s in result.steps if s.what == "Отстройка ТО от внешнего КЗ")
        assert "strong_min" in step.note and "duplicate" in step.note
        assert "первый по порядку объявления" in step.note


def test_missing_solver_is_a_required_mode_and_keeps_its_reason():
    net = _line_modes()
    ctx = Context(net, Methodology.load())
    del ctx.solvers["strong_min"]
    ctx.errors["strong_min"] = "deliberate solver assembly failure"
    result = calc_to(ctx, net.branches["F"])
    assert result.i_primary is not None
    assert not result.is_complete
    assert result.status == UNRESOLVED
    for text in ("внешнего КЗ", "чувствительности"):
        coverage = _coverage(result, text)
        assert (coverage.checked, coverage.required) == (1, 2)
        assert "deliberate solver assembly failure" in " ".join(coverage.problems)


def test_partial_sensitivity_failure_cannot_be_ok_and_does_not_hide_known_fail(monkeypatch):
    net = _line_modes()
    ctx = Context(net, Methodology.load())
    original = ctx.current_at

    def failed(mode, point, voltage, which="i3"):
        if mode.id == "strong_min" and which == "i2":
            raise ValueError("independent sensitivity failure")
        return original(mode, point, voltage, which)

    monkeypatch.setattr(ctx, "current_at", failed)
    result = calc_to(ctx, net.branches["F"])
    assert not result.is_complete
    assert result.status == FAIL
    assert any(check.status == FAIL for check in result.checks)
    assert any(check.status == UNRESOLVED for check in result.checks)
    coverage = _coverage(result, "чувствительности")
    assert (coverage.checked, coverage.required) == (1, 2)


def _own_transformer(*, ratio=12, source_side="hv", ct_side="hv", reversed_ends=False,
                     explicit_base=False):
    net = Network("Own transformer magnetizing current")
    net.add_node(Node("hv", "HV", 35))
    net.add_node(Node("lv", "LV", 10))
    if explicit_base:
        net.nodes["hv"].calculation_base_kv = 36.5
        net.nodes["lv"].calculation_base_kv = 10.2
    net.add_branch(SourceBranch("S", "S", GRID, source_side, s_kz_max=1000, s_kz_min=800))
    ends = ("lv", "hv") if reversed_ends else ("hv", "lv")
    transformer = TransformerBranch("T", "T", *ends, s_nom=10000, u_hv=35,
                                    u_lv=10, uk=10, p_k=50, i_inrush_ratio=ratio,
                                    ct_ratio=(300, 5), ct_node=ct_side)
    transformer.prot.mtz = False
    transformer.prot.to = True
    # The magnetizing-current condition is independent of the external-KZ zone.
    transformer.prot.to_reach = "branch_end"
    transformer.prot.i_scale = [1, 10, 30, 50, 100, 200, 500]
    net.add_branch(transformer)
    net.add_mode(Mode("max", "max", system="max"))
    net.add_mode(Mode("min", "min", system="min"))
    return net


@pytest.mark.parametrize("reversed_ends", [False, True])
@pytest.mark.parametrize("explicit_base", [False, True])
def test_own_hv_inrush_uses_nameplate_and_not_fault_calculation_base(reversed_ends, explicit_base):
    net = _own_transformer(reversed_ends=reversed_ends, explicit_base=explicit_base)
    result = _calculate(net, "T")
    expected = 1.3 * 12 * 10000 / (math.sqrt(3) * 35)
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert result.i_primary == 3000
    assert len(_inrush_steps(result)) == 1
    coverage = _coverage(result, "броска")
    assert (coverage.checked, coverage.required) == (2, 2)
    assert coverage.complete


def test_inrush_ratio_changes_own_transformer_setting():
    low = _calculate(_own_transformer(ratio=2), "T")
    high = _calculate(_own_transformer(ratio=12), "T")
    assert high.i_calc > low.i_calc
    assert high.i_primary > low.i_primary


@pytest.mark.parametrize("ratio", [None, 0, -1, math.nan, math.inf])
def test_missing_or_invalid_inrush_does_not_substitute_one_and_claim_complete(ratio):
    result = _calculate(_own_transformer(ratio=ratio), "T")
    assert not result.is_complete
    assert result.status != OK
    assert not _inrush_steps(result)
    coverage = _coverage(result, "броска")
    assert (coverage.checked, coverage.required) == (0, 2)
    assert "i_inrush_ratio" in " ".join(coverage.problems)


def test_lv_ct_does_not_measure_own_primary_magnetizing_current():
    result = _calculate(_own_transformer(ct_side="lv"), "T")
    assert not _inrush_steps(result)
    assert "не проходит через ТТ" in result.explain()
    assert _coverage(result, "броска").complete


def test_reverse_lv_energization_uses_lv_nominal_current():
    result = _calculate(_own_transformer(source_side="lv", ct_side="lv"), "T")
    expected = 1.3 * 12 * 10000 / (math.sqrt(3) * 10)
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert len(_inrush_steps(result)) == 1


def _downstream_with_ideal_switches():
    net = Network("Downstream transformer through ideal apparatus")
    for node_id, voltage in (("s", 10), ("end", 10), ("mid", 10), ("hv", 10), ("lv", .4)):
        net.add_node(Node(node_id, node_id, voltage))
    net.add_branch(SourceBranch("S", "S", GRID, "s", s_kz_max=2, s_kz_min=1.5))
    line = LineBranch("F", "F", "s", "end", length_km=2, r0=.428, x0=.375,
                      ct_ratio=(200, 5), ct_node="s")
    line.prot.to_reach = "behind_transformer"
    line.prot.i_scale = [1, 5, 6, 10, 1000]
    net.add_branch(line)
    net.add_branch(TieBranch("Q1", "Q1", "end", "mid", normally_closed=True))
    net.add_branch(TieBranch("Q2", "Q2", "mid", "hv", normally_closed=True))
    net.add_branch(TransformerBranch("T", "T", "hv", "lv", s_nom=630, u_hv=10,
                                    u_lv=.4, uk=5.5, p_k=7.6, i_inrush_ratio=5))
    net.add_mode(Mode("max", "max", system="max"))
    net.add_mode(Mode("min", "min", system="min"))
    return net


def test_ideal_apparatus_does_not_hide_downstream_inrush_or_duplicate_it():
    result = _calculate(_downstream_with_ideal_switches(), "F")
    expected = 1.3 * 5 * 630 / (math.sqrt(3) * 10)
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert result.i_primary == 240
    assert len(_inrush_steps(result)) == 1
    assert _coverage(result, "броска").complete


def test_transformer_search_tracks_switch_state_in_each_mode():
    net = _downstream_with_ideal_switches()
    # No transformer is available behind the feeder in the first declared mode.
    net.modes["max"].states["Q2"] = False
    result = _calculate(net, "F")
    assert len(_inrush_steps(result)) == 1
    assert not result.is_complete  # The requested behind-transformer zone is absent in max.
    assert any("max" in p for row in result.coverage for p in row.problems)


def test_ideal_ring_failure_is_not_silently_removed_from_required_modes():
    net = _downstream_with_ideal_switches()
    protected = net.branches["Q1"]
    protected.ct_ratio = (200, 5)
    protected.ct_node = "end"
    protected.prot.to_reach = "branch_end"
    net.add_branch(TieBranch("parallel", "parallel", "end", "mid", normally_closed=False))
    net.modes["min"].states["parallel"] = True
    result = _calculate(net, "Q1")
    assert not result.is_complete
    assert any(check.status == UNRESOLVED for check in result.checks)
    coverage = _coverage(result, "внешнего КЗ")
    assert (coverage.checked, coverage.required) == (1, 2)


def test_external_fault_terminal_follows_each_modes_actual_supply_direction():
    net = _line_modes()
    net.branches["strong"].node_to = "b"
    net.branches["weak"].switchable = True
    net.branches["F"].length_km = 20
    net.modes = {
        "forward": Mode("forward", "forward", {"weak": True, "strong": False}, system="max"),
        "reverse": Mode("reverse", "reverse", {"weak": False, "strong": True}, system="max"),
    }
    result = _calculate(net, "F")
    # The stronger reverse-fed case faults node a, at the end of the line.
    expected = 1.2 * 10500 / (math.sqrt(3) * abs(complex(2, 2 + 10.5**2 / 500)))
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert result.governing_mode == "reverse"
    assert _coverage(result, "внешнего КЗ").complete


def test_no_available_solver_keeps_full_external_and_sensitivity_coverage():
    net = _line_modes()
    ctx = Context(net, Methodology.load())
    ctx.solvers.clear()
    ctx.errors.update({mode_id: "unavailable " + mode_id for mode_id in net.modes})
    result = calc_to(ctx, net.branches["F"])
    assert result.status == UNRESOLVED
    assert result.i_primary is None
    for text in ("внешнего КЗ", "чувствительности"):
        coverage = _coverage(result, text)
        assert (coverage.checked, coverage.required) == (0, 2)


def test_open_branch_mode_is_explicitly_excluded():
    net = _line_modes()
    net.branches["F"].switchable = True
    net.modes["strong_min"].states["F"] = False
    result = _calculate(net, "F")
    coverage = _coverage(result, "внешнего КЗ")
    assert (coverage.checked, coverage.required) == (1, 1)
    assert coverage.complete
    assert "strong_min" in result.explain() and "присоединение отключено" in result.explain()


def test_nominal_inrush_basis_setting_preserves_explicit_legacy_average_option():
    meth = Methodology.load()
    meth.data["to"]["i_nom_basis"]["value"] = "stage_average"
    result = _calculate(_own_transformer(), "T", meth)
    expected = 1.3 * 12 * 10000 / (math.sqrt(3) * 37)
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert "среднее расчётное напряжение" in _inrush_steps(result)[0].given["Напряжение для Iном"]


def test_double_sided_own_transformer_has_no_invented_inrush_current_split():
    net = _own_transformer()
    net.add_branch(SourceBranch("LVsource", "LVsource", GRID, "lv", s_kz_max=100,
                                s_kz_min=80))
    result = _calculate(net, "T")
    coverage = _coverage(result, "броска")
    assert (coverage.checked, coverage.required) == (0, 2)
    assert not result.is_complete
    assert not _inrush_steps(result)
    assert "питании с двух сторон" in result.explain()


def test_two_ideal_routes_to_one_transformer_produce_one_physical_inrush_condition():
    net = _downstream_with_ideal_switches()
    net.add_branch(TieBranch("bypass", "bypass", "end", "hv", normally_closed=True))
    result = _calculate(net, "F")
    expected = 1.3 * 5 * 630 / (math.sqrt(3) * 10)
    assert result.i_calc == pytest.approx(expected, rel=1e-12)
    assert len(_inrush_steps(result)) == 1
    coverage = _coverage(result, "броска")
    assert (coverage.checked, coverage.required) == (2, 2)


def test_setting_range_failure_retains_unchecked_sensitivity_cases():
    net = _own_transformer()
    net.branches["T"].prot.i_scale = [1]
    result = _calculate(net, "T")
    assert result.status == FAIL
    assert not result.is_complete
    coverage = _coverage(result, "чувствительности")
    assert (coverage.checked, coverage.required) == (0, 2)


@pytest.mark.parametrize("mode_count", [1, 2])
def test_equal_fault_targets_do_not_inflate_the_number_of_equal_modes(mode_count):
    net = _downstream_with_ideal_switches()
    net.modes.pop("min")
    if mode_count == 2:
        net.add_mode(Mode("duplicate", "duplicate", system="max"))
    net.add_node(Node("other_lv", "other LV", .4))
    net.add_branch(TransformerBranch("other_T", "other T", "hv", "other_lv",
                                    s_nom=630, u_hv=10, u_lv=.4, uk=5.5,
                                    p_k=7.6, i_inrush_ratio=5))
    result = _calculate(net, "F")
    coverage = _coverage(result, "внешнего КЗ")
    assert (coverage.checked, coverage.required) == (2 * mode_count, 2 * mode_count)
    step = next(s for s in result.steps if s.what == "Отстройка ТО от внешнего КЗ")
    if mode_count == 1:
        assert step.note is None
    else:
        assert "2 режимах" in step.note
        assert "4 режимах" not in step.note
        assert "duplicate" in step.note


def test_governing_mode_is_the_inrush_mode_when_inrush_controls_the_setting():
    net = _own_transformer()
    # External faults are strongest in max. The equal inrush condition is
    # represented by min, because the user has declared that mode first.
    net.modes = dict(reversed(list(net.modes.items())))
    result = _calculate(net, "T")
    assert result.i_calc == pytest.approx(1.3 * 12 * 10000 / (math.sqrt(3) * 35), rel=1e-12)
    assert result.governing_mode == "min"
    step = next(s for s in result.steps if s.what == "Отстройка ТО от внешнего КЗ")
    assert "«max»" in step.given["Расчётная точка"]
    assert _inrush_steps(result)[0].given["Применимые режимы"].startswith("«min»")


def test_mixed_radial_and_double_fed_modes_do_not_claim_a_complete_line_reach():
    net = _line_modes()
    net.branches["strong"].node_to = "b"
    result = _calculate(net, "F")
    assert _coverage(result, "внешнего КЗ").complete
    assert _coverage(result, "чувствительности").complete
    assert not any(s.what == "Зона действия ТО по длине линии" for s in result.steps)
    diagnostic = next(message for message in result.messages
                      if "Зона действия ТО по длине линии не определена" in message)
    assert "strong_min" in diagnostic
    assert "питании с двух сторон" in diagnostic
