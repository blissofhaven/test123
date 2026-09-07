# -*- coding: utf-8 -*-
"""Регрессии для критических расчётных ошибок пункта 4."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.core.context import Context
from rza_calc.core.engine import CalculationInputError, run
from rza_calc.core.load_current import working_current
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
                                 ProtectionSettings, SourceBranch, TieBranch,
                                 TransformerBranch)
from rza_calc.core.result import FAIL, UNRESOLVED
from rza_calc.core.short_circuit import CurrentDistributionError, ShortCircuitSolver
from rza_calc.io.project import load, load_project

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya.json"


def _two_section_network() -> tuple[Network, TransformerBranch, TransformerBranch, TieBranch]:
    net = Network("две секции")
    for node in (
        Node("b35", "ОРУ-35 кВ", 35),
        Node("b1", "1 СШ 10 кВ", 10),
        Node("b2", "2 СШ 10 кВ", 10),
        Node("end", "Конец фидера", 10),
    ):
        net.add_node(node)
    net.add_branch(SourceBranch(
        id="S", name="Система", node_from=GRID, node_to="b35",
        s_kz_max=500, s_kz_min=300,
    ))
    t1 = net.add_branch(TransformerBranch(
        id="T1", name="Т1", node_from="b35", node_to="b1",
        s_nom=10000, u_hv=35, u_lv=10, uk=7.5,
        ct_ratio=(200, 5), ct_node="b35",
    ))
    t2 = net.add_branch(TransformerBranch(
        id="T2", name="Т2", node_from="b35", node_to="b2",
        s_nom=10000, u_hv=35, u_lv=10, uk=7.5,
        ct_ratio=(200, 5), ct_node="b35",
    ))
    tie = net.add_branch(TieBranch(
        id="SV", name="СВ-10 кВ", node_from="b1", node_to="b2",
        ct_ratio=(1000, 5), ct_node="b2",
    ))
    net.add_branch(LineBranch(
        id="F", name="Фидер", node_from="b1", node_to="end",
        length_km=2, r0=0.25, x0=0.08,
    ))
    net.add_load(Load("L1", "Нагрузка 1 СШ", "end", p_kw=1800, cos_phi=0.9))
    net.add_load(Load("L2", "Нагрузка 2 СШ", "b2", p_kw=900, cos_phi=0.9))
    return net, t1, t2, tie


def test_ct_voltage_does_not_move_when_power_reverses():
    meth = Methodology.load()
    net = Network("реверс")
    net.add_node(Node("hv", "Шины 35 кВ", 35))
    net.add_node(Node("lv", "Шины 10 кВ", 10))
    net.add_branch(SourceBranch(
        id="S", name="Источник 10 кВ", node_from=GRID, node_to="lv",
        s_kz_max=300, s_kz_min=200,
    ))
    transformer = net.add_branch(TransformerBranch(
        id="T", name="Т", node_from="hv", node_to="lv",
        s_nom=10000, u_hv=35, u_lv=10, uk=7.5,
        ct_ratio=(200, 5), ct_node="hv",
    ))
    mode = net.add_mode(Mode("reverse", "Обратное питание"))
    assert net.orient(transformer, mode)[:2] == ("lv", "hv")
    assert Context(net, meth).protection_side_u(transformer, mode) == 35


def test_transformer_fallback_current_uses_ct_side():
    meth = Methodology.load()
    net, t1, _, _ = _two_section_network()
    mode = net.add_mode(Mode("split", "СВ отключён", states={"SV": False}))
    # У Т1 нет нагрузки непосредственно в его радиальной зоне после удаления
    # нагрузки 1 СШ — используется Iном, но на стороне ТТ 35 кВ.
    del net.loads["L1"]
    value = working_current(net, t1, mode, meth)
    expected = t1.s_nom / (math.sqrt(3.0) * 35.0)
    assert abs(value.value - expected) < 1e-9
    assert "35" in value.step.given["U стороны ТТ"]


def test_parallel_lines_use_full_group_load_for_n_minus_one_setting():
    meth = Methodology.load()
    net = Network("две параллельные линии")
    net.add_node(Node("bus", "Шины 10 кВ", 10))
    net.add_node(Node("load_bus", "Шины нагрузки", 10))
    net.add_branch(SourceBranch(
        id="S", name="Система", node_from=GRID, node_to="bus",
        s_kz_max=300, s_kz_min=200,
    ))
    lines = []
    for branch_id in ("L1", "L2"):
        lines.append(net.add_branch(LineBranch(
            id=branch_id, name=branch_id, node_from="bus", node_to="load_bus",
            length_km=2, r0=0.25, x0=0.08,
            ct_ratio=(400, 5), ct_node="bus",
        )))
    net.add_load(Load("L", "Нагрузка", "load_bus", p_kw=1800, cos_phi=0.9))
    mode = net.add_mode(Mode("parallel", "Обе линии"))
    expected = (1800 / 0.9) / (math.sqrt(3.0) * 10.0)
    for line in lines:
        value = working_current(net, line, mode, meth)
        assert abs(value.value - expected) < 1e-9
        assert "параллельно" in (value.step.note or "")


def test_section_breaker_carries_transferred_section_in_transfer_mode():
    meth = Methodology.load()
    net, _, _, tie = _two_section_network()
    mode = net.add_mode(Mode(
        "transfer", "Т1 отключён, СВ включён",
        states={"T1": False, "T2": True, "SV": True},
    ))
    value = working_current(net, tie, mode, meth)
    # Через СВ проходит только нагрузка обесточенного первого ввода; нагрузка
    # второй секции питается от Т2 напрямую и через СВ не течёт.
    expected_s = 1800 / 0.9
    expected_i = expected_s / (math.sqrt(3.0) * 10.0)
    assert abs(value.value - expected_i) < 1e-9


def test_zero_impedance_tie_distribution_is_one_only_when_radial():
    meth = Methodology.load()
    net, _, _, tie = _two_section_network()
    radial = Mode(
        "transfer", "Т1 отключён, СВ включён",
        states={"T1": False, "T2": True, "SV": True},
    )
    solver = ShortCircuitSolver(net, radial, meth)
    assert solver.distribution_factor(tie, "end") == 1.0
    assert solver.distribution_factor(tie, "b2") == 0.0


def test_zero_impedance_tie_in_ring_is_not_silently_100_percent():
    meth = Methodology.load()
    net, _, _, tie = _two_section_network()
    ring = Mode(
        "parallel", "Оба трансформатора и СВ включены",
        states={"T1": True, "T2": True, "SV": True},
    )
    solver = ShortCircuitSolver(net, ring, meth)
    with pytest.raises(CurrentDistributionError):
        solver.distribution_factor(tie, "end")


def test_disabled_mtz_is_absent_from_selectivity():
    net, meth, _ = load(EX)
    net.branches["F1"].prot.mtz = False
    result = run(net, meth)
    assert "МТЗ" not in result.results["F1"]
    assert all(pair.lower_id != "F1" and pair.upper_id != "F1"
               for pair in result.pairs)


def test_terminal_scale_overflow_is_failure_not_accepted_setting():
    net, meth, _ = load(EX)
    net.branches["F1"].prot.i_scale = [0.1]
    result = run(net, meth)
    mtz = result.get("F1", "МТЗ")
    assert mtz.status == FAIL
    assert mtz.i_primary is None
    assert any(check.status == FAIL and "терминала" in check.name
               for check in mtz.checks)


def test_missing_methodology_file_blocks_loading(tmp_path):
    raw = json.loads(EX.read_text(encoding="utf-8"))
    raw["methodology"] = {"file": "missing-methodology.json"}
    path = tmp_path / "project.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_project(path)


def test_missing_required_methodology_value_blocks_calculation():
    net, meth, _ = load(EX)
    del meth.data["sensitivity"]["backup_through_transformer"]
    with pytest.raises(CalculationInputError):
        run(net, meth)


def test_invalid_network_blocks_calculation():
    net = Network("без источника")
    net.add_node(Node("b", "Шины", 10))
    net.add_mode(Mode("m", "Режим"))
    with pytest.raises(CalculationInputError):
        run(net, Methodology.load())


def test_duplicate_electrical_id_is_rejected_immediately():
    net = Network("дубликат")
    net.add_node(Node("bus", "Шины 1", 10))
    with pytest.raises(ValueError):
        net.add_node(Node("bus", "Шины 2", 10))


def test_unknown_mode_branch_blocks_calculation():
    net, meth, _ = load(EX)
    next(iter(net.modes.values())).states["missing-breaker"] = True
    with pytest.raises(CalculationInputError):
        run(net, meth)


def test_reversed_selectivity_between_modes_returns_engineering_result():
    net = Network("двухстороннее питание")
    for node in (Node("left", "Левые шины", 10), Node("middle", "Нагрузка", 10),
                 Node("right", "Правые шины", 10)):
        net.add_node(node)
    for branch_id, node in (("SL", "left"), ("SR", "right")):
        net.add_branch(SourceBranch(
            id=branch_id, name=branch_id, node_from=GRID, node_to=node,
            s_kz_max=300, s_kz_min=200, switchable=True,
        ))
    protection = ProtectionSettings(mtz=True, to=False)
    net.add_branch(LineBranch(
        id="A", name="Защита A", node_from="left", node_to="middle",
        length_km=1, r0=0.2, x0=0.08, ct_ratio=(400, 5), ct_node="left",
        prot=protection,
    ))
    net.add_branch(LineBranch(
        id="B", name="Защита B", node_from="middle", node_to="right",
        length_km=1, r0=0.2, x0=0.08, ct_ratio=(400, 5), ct_node="right",
        prot=ProtectionSettings(mtz=True, to=False),
    ))
    net.add_load(Load("L", "Нагрузка", "middle", p_kw=1000, cos_phi=0.9))
    net.add_mode(Mode("left_feed", "Питание слева", states={"SL": True, "SR": False}))
    net.add_mode(Mode("right_feed", "Питание справа", states={"SL": False, "SR": True}))

    result = run(net, Methodology.load())
    for branch_id in ("A", "B"):
        mtz = result.get(branch_id, "МТЗ")
        assert mtz.status == UNRESOLVED
        assert any("Направление селективности" in check.name for check in mtz.checks)
