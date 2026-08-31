# -*- coding: utf-8 -*-
"""Этап R1: единый базис средних напряжений и коэффициент трансформации.

Тесты закрепляют методическое решение: весь расчёт ведётся в средних
напряжениях ступеней. Каждое ожидаемое число получено независимым ручным
расчётом по формулам классической методики, а не снято с программы.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.core.engine import run
from rza_calc.core.impedance import (
    belongs_to_stage,
    stage_voltage,
    transformer_stage_node,
)
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
                                 SourceBranch, TransformerBranch)
from rza_calc.core.short_circuit import ShortCircuitSolver

SQRT3 = math.sqrt(3.0)

# Средние напряжения ступеней из профиля методики.
U_AVG = {0.4: 0.4, 6.0: 6.3, 10.0: 10.5, 35.0: 37.0, 110.0: 115.0, 220.0: 230.0}


@pytest.fixture
def meth() -> Methodology:
    return Methodology.load()


def _substation(u_hv: float = 110.0, u_lv: float = 10.0,
                lv_class: float = 10.0, reverse: bool = False) -> Network:
    """Система 3000 МВ·А на 110 кВ и трансформатор 25 МВ·А, Uк = 10,5 %."""
    net = Network("подстанция")
    net.add_node(Node("hv", "ШИНЫ 110", 110.0))
    net.add_node(Node("lv", "ШИНЫ НН", lv_class))
    net.add_branch(SourceBranch(
        id="S", name="Система", node_from=GRID, node_to="hv",
        s_kz_max=3000.0, s_kz_min=3000.0,
        input_mode_max="power", input_mode_min="power",
    ))
    first, second = ("hv", "lv") if not reverse else ("lv", "hv")
    hv, lv = (u_hv, u_lv) if not reverse else (u_lv, u_hv)
    net.add_branch(TransformerBranch(
        id="T", name="Т-1", node_from=first, node_to=second,
        s_nom=25000.0, u_hv=hv, u_lv=lv, uk=10.5,
    ))
    net.add_mode(Mode("max", "Максимальный", system="max"))
    return net


def _solver(net: Network, meth: Methodology) -> ShortCircuitSolver:
    return ShortCircuitSolver(net, net.modes["max"], meth)


# ── базис ────────────────────────────────────────────────────────────────
def test_stage_voltage_is_the_average_of_the_class(meth: Methodology) -> None:
    for u_class, u_avg in U_AVG.items():
        node = Node("n", "узел", u_class)
        assert stage_voltage(node, meth) == pytest.approx(u_avg)


def test_explicit_calculation_base_overrides_the_table(meth: Methodology) -> None:
    node = Node("n", "узел", 10.0, calculation_base_kv=10.2)
    assert stage_voltage(node, meth) == pytest.approx(10.2)


def test_solver_base_is_a_stage_voltage(meth: Methodology) -> None:
    solver = _solver(_substation(), meth)
    assert solver.u_base == pytest.approx(115.0)


def test_hand_check_two_stage_substation(meth: Methodology) -> None:
    """Ручной расчёт: Zс = Uср²/Sкз, Zт = 10·Uк·Uср²/Sном, приведение по Uср."""
    solver = _solver(_substation(), meth)

    z_source_hv = 115.0 ** 2 / 3000.0
    z_source_lv = z_source_hv * (10.5 / 115.0) ** 2
    z_transformer_lv = 10.0 * 10.5 * 10.5 ** 2 / 25000.0
    z_total = z_source_lv + z_transformer_lv
    i_hand = (10.5 / SQRT3) / z_total

    result = solver.at("lv")
    assert abs(result.z_th) == pytest.approx(z_total, rel=1e-12)
    assert result.i3 == pytest.approx(i_hand, rel=1e-12)
    assert result.u_stage == pytest.approx(10.5)


def test_source_stage_current_matches_short_circuit_power(meth: Methodology) -> None:
    """На ступени источника ток обязан в точности соответствовать Sкз."""
    solver = _solver(_substation(), meth)
    assert solver.at("hv").i3 == pytest.approx(3000.0 / (SQRT3 * 115.0), rel=1e-12)


# ── коэффициент трансформации ────────────────────────────────────────────
@pytest.mark.parametrize("u_hv,u_lv", [(115, 11), (110, 10), (115, 10), (110, 11)])
def test_same_stage_spelled_differently_gives_the_same_result(
    meth: Methodology, u_hv: float, u_lv: float
) -> None:
    """115/11 и 110/10 — один и тот же аппарат на тех же ступенях."""
    reference = _solver(_substation(110.0, 10.0), meth).at("lv").i3
    assert _solver(_substation(u_hv, u_lv), meth).at("lv").i3 == pytest.approx(
        reference, rel=1e-12
    )


def test_drawing_direction_does_not_change_the_result(meth: Methodology) -> None:
    direct = _solver(_substation(), meth).at("lv").i3
    reverse = _solver(_substation(reverse=True), meth).at("lv").i3
    assert reverse == pytest.approx(direct, rel=1e-12)


def test_nameplate_that_contradicts_the_node_class_is_rejected(
    meth: Methodology
) -> None:
    """Трансформатор 110/6,3 на шинах 10 кВ — ошибка данных, а не 110/10."""
    net = _substation(110.0, 6.3, lv_class=10.0)
    problems = net.validate()
    assert any("паспортные напряжения" in item for item in problems)
    with pytest.raises(ValueError, match="не соответствуют классам узлов"):
        _solver(net, meth)


def test_real_six_kilovolt_transformer_is_computed_on_its_own_stage(
    meth: Methodology
) -> None:
    solver = _solver(_substation(110.0, 6.3, lv_class=6.0), meth)
    z_source = 115.0 ** 2 / 3000.0 * (6.3 / 115.0) ** 2
    z_transformer = 10.0 * 10.5 * 6.3 ** 2 / 25000.0
    i_hand = (6.3 / SQRT3) / (z_source + z_transformer)
    assert solver.at("lv").i3 == pytest.approx(i_hand, rel=1e-12)


def test_stage_node_is_found_from_nameplate_not_from_record_order() -> None:
    """Блочный трансформатор записан как 10/220 — сторона Uк определяется по классам."""
    net = Network("блок")
    net.add_node(Node("g", "Шины генератора", 10.0))
    net.add_node(Node("hv", "ОРУ 220", 220.0))
    branch = TransformerBranch(
        id="BT", name="Блочный Т", node_from="g", node_to="hv",
        s_nom=25000.0, u_hv=10.0, u_lv=220.0, uk=11.5,
    )
    net.add_branch(branch)
    assert transformer_stage_node(net, branch).id == "g"


def test_belongs_to_stage_accepts_average_voltages_and_rejects_other_classes() -> None:
    assert belongs_to_stage(115.0, 110.0)
    assert belongs_to_stage(11.0, 10.0)
    assert belongs_to_stage(6.3, 6.0)
    assert not belongs_to_stage(6.3, 10.0)
    assert not belongs_to_stage(35.0, 10.0)


# ── ток между ступенями ──────────────────────────────────────────────────
def test_current_referral_between_stages_uses_the_same_basis(
    meth: Methodology
) -> None:
    """Ток, приведённый со ступени НН на ВН, совпадает с ручным пересчётом."""
    net = _substation()
    net.add_load(Load(id="LD", name="Нагрузка", node="lv", p_kw=1000.0))
    net.branches["T"].ct_ratio = (200.0, 5.0)
    net.branches["T"].ct_node = "hv"
    result = run(net, meth)
    context = result.ctx
    current, sc = context.current_at(
        net.modes["max"], "lv", context.protection_side_stage(net.branches["T"]), "i3"
    )
    assert current == pytest.approx(sc.i3 * 1000.0 * (10.5 / 115.0), rel=1e-12)


# ── порог нулевого сопротивления ─────────────────────────────────────────
def test_zero_impedance_threshold_comes_from_the_methodology(
    meth: Methodology
) -> None:
    solver = _solver(_substation(), meth)
    assert solver.zero_threshold == pytest.approx(
        meth.k("short_circuit.zero_impedance_ohm")
    )


# ── вклады источников ────────────────────────────────────────────────────
def test_source_contributions_sum_to_the_total_current(meth: Methodology) -> None:
    """Сумма вкладов источников равна полному току точки.

    Это независимая проверка матрицы: расхождение означало бы ошибку
    токораспределения, а не округление.
    """
    net = Network("два источника")
    net.add_node(Node("a", "A", 10.0))
    net.add_node(Node("b", "B", 10.0))
    for name, node, power in (("S1", "a", 300.0), ("S2", "b", 150.0)):
        net.add_branch(SourceBranch(
            id=name, name=f"Система {node.upper()}", node_from=GRID, node_to=node,
            s_kz_max=power, s_kz_min=power,
            input_mode_max="power", input_mode_min="power",
        ))
    net.add_branch(LineBranch(
        id="L", name="Связь", node_from="a", node_to="b",
        length_km=4.0, section_mm2=120, material="Al",
    ))
    net.add_mode(Mode("max", "Максимальный", system="max"))
    result = _solver(net, meth).at("a")

    assert set(result.source_contributions) == {"S1", "S2"}
    total = sum(result.source_contributions.values())
    assert abs(total - result.i3_complex) < 1e-12
    assert abs(result.source_contributions["S1"]) > abs(
        result.source_contributions["S2"]
    )


# ── протокол ─────────────────────────────────────────────────────────────
def test_protocol_lists_only_branches_that_carry_current(meth: Methodology) -> None:
    """В цепочку сопротивлений попадают только влияющие элементы."""
    net = _substation()
    net.add_node(Node("other", "Чужая секция", 10.0))
    net.add_branch(TransformerBranch(
        id="T2", name="Т-2", node_from="hv", node_to="other",
        s_nom=25000.0, u_hv=110.0, u_lv=10.0, uk=10.5,
    ))
    solver = _solver(net, meth)
    names = " ".join(step.what for step in solver.at("other").steps[0].children)
    assert "Т-2" in names
    assert "Т-1" not in names, "в протокол попал трансформатор соседней секции"
