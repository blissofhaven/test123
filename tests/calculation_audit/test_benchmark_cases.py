# -*- coding: utf-8 -*-
"""Эталоны этапа 4.4 и фиксация подтверждённых расхождений production-ядра."""
from __future__ import annotations

import math
import sys
from functools import lru_cache
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from rza_calc.core.context import Context
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID,
    GeneratorBranch,
    LineBranch,
    Mode,
    Network,
    Node,
    SourceBranch,
    TieBranch,
    TransformerBranch,
)
from rza_calc.core.short_circuit import ShortCircuitSolver

from .reference_solver import (
    GROUND,
    ReferenceBranch,
    ReferenceYBusSolver,
    per_unit_fault_current_ka,
)


STRICT = {"rel": 1e-9, "abs": 1e-12}
Z_ONE = complex(0.1, 0.4)
I_ONE = 14.7029408823
IDEAL_SOURCE_S_KZ_MVA = 1e18


def _solver(network: Network, fault_node: str) -> tuple[ShortCircuitSolver, float]:
    solver = ShortCircuitSolver(network, Mode("audit", "Контрольный режим"), Methodology.load())
    return solver, solver.at(fault_node).i3


def _add_ideal_source(network: Network, bus_id: str, source_id: str = "source") -> None:
    # Конечное большое Sкз даёт |Zс|≈1,1e-16 Ом на ступени 10 кВ. Это ниже
    # production-порога объединения 1e-12 Ом, поэтому существующий DTO выражает
    # идеальную ЭДС без использования NaN/Infinity и без отдельного дефекта ввода.
    network.add_branch(SourceBranch(
        source_id,
        "Идеальный источник",
        GRID,
        bus_id,
        s_kz_max=IDEAL_SOURCE_S_KZ_MVA,
        s_kz_min=IDEAL_SOURCE_S_KZ_MVA,
    ))


def _example_1_network() -> Network:
    network = Network("Пример 1")
    network.add_node(Node("source_bus", "Шины источника", 10))
    network.add_node(Node("fault", "Точка КЗ", 10))
    _add_ideal_source(network, "source_bus")
    network.add_branch(LineBranch(
        "z", "Контрольное Z", "source_bus", "fault",
        length_km=1.0, r0=0.1, x0=0.4,
    ))
    network.add_mode(Mode("normal", "Нормальный режим"))
    return network


def test_example_1_reference_source_and_one_impedance() -> None:
    """Пример 1: независимый Ybus даёт 14,7029408823 кА."""

    reference = ReferenceYBusSolver(
        ("fault",),
        (ReferenceBranch("z", GROUND, "fault", Z_ONE),),
    )
    assert reference.thevenin_impedance("fault") == pytest.approx(Z_ONE, **STRICT)
    assert reference.fault_current_ka("fault", 10.5) == pytest.approx(I_ONE, **STRICT)


def test_example_1_production_source_and_one_impedance() -> None:
    """Пример 1: production-ядро совпадает с независимым пересчётом."""

    network = _example_1_network()
    _, actual = _solver(network, "fault")
    assert actual == pytest.approx(I_ONE, **STRICT)


def test_example_2_reference_parallel_branches() -> None:
    """Пример 2: две ветви дают Zэкв=0,1+j0,4 Ом и делят ток поровну."""

    reference = ReferenceYBusSolver(
        ("fault",),
        (
            ReferenceBranch("a", GROUND, "fault", complex(0.2, 0.8)),
            ReferenceBranch("b", GROUND, "fault", complex(0.2, 0.8)),
        ),
    )
    total = reference.fault_current_ka("fault", 10.5)
    assert reference.thevenin_impedance("fault") == pytest.approx(Z_ONE, **STRICT)
    assert total == pytest.approx(I_ONE, **STRICT)
    for branch_id in ("a", "b"):
        factor = abs(reference.branch_transfer_factor(branch_id, "fault"))
        assert factor == pytest.approx(0.5, **STRICT)
        assert total * factor == pytest.approx(7.3514704412, **STRICT)


def _example_2_network() -> Network:
    network = Network("Пример 2")
    network.add_node(Node("source_bus", "Шины источника", 10))
    network.add_node(Node("fault", "Точка КЗ", 10))
    _add_ideal_source(network, "source_bus")
    for branch_id in ("a", "b"):
        network.add_branch(LineBranch(
            branch_id, branch_id, "source_bus", "fault",
            length_km=1.0, r0=0.2, x0=0.8,
        ))
    network.add_mode(Mode("normal", "Нормальный режим"))
    return network


def test_example_2_production_parallel_branches() -> None:
    """Пример 2: production Ybus учитывает обе независимые ветви."""

    network = _example_2_network()
    solver, total = _solver(network, "fault")
    assert total == pytest.approx(I_ONE, **STRICT)
    for branch_id in ("a", "b"):
        factor = solver.distribution_factor(network.branches[branch_id], "fault")
        assert factor == pytest.approx(0.5, **STRICT)
        assert total * factor == pytest.approx(7.3514704412, **STRICT)


def test_example_3_reference_infinite_source_and_transformer() -> None:
    """Пример 3: трансформатор 1 МВА, 0,4 кВ, Uk=6 % даёт 24,0562612162 кА."""

    transformer_z_lv = complex(0.0, 0.06 * 0.4**2 / 1.0)
    reference = ReferenceYBusSolver(
        ("fault",),
        (ReferenceBranch("transformer", GROUND, "fault", transformer_z_lv),),
    )
    assert reference.fault_current_ka("fault", 0.4) == pytest.approx(
        24.0562612162, **STRICT
    )


def _example_3_network() -> Network:
    network = Network("Пример 3")
    network.add_node(Node("hv", "ВН", 10))
    network.add_node(Node("lv", "НН", 0.4))
    _add_ideal_source(network, "hv")
    network.add_branch(TransformerBranch(
        "transformer", "Трансформатор", "hv", "lv",
        s_nom=1000.0, u_hv=10.0, u_lv=0.4, uk=6.0,
    ))
    network.add_mode(Mode("normal", "Нормальный режим"))
    return network


def test_example_3_production_infinite_source_and_transformer() -> None:
    """Пример 3: production-формула Uk% проходит аналитический эталон."""

    network = _example_3_network()
    _, actual = _solver(network, "lv")
    assert actual == pytest.approx(24.0562612162, **STRICT)


def test_example_4_reference_three_serial_sections() -> None:
    """Пример 4: до каждой точки входит только предшествующая часть линии."""

    section_z = complex(0.05, 0.2)
    reference = ReferenceYBusSolver(
        ("j1", "j2", "j3"),
        (
            ReferenceBranch("s1", GROUND, "j1", section_z),
            ReferenceBranch("s2", "j1", "j2", section_z),
            ReferenceBranch("s3", "j2", "j3", section_z),
        ),
    )
    expected = {"j1": 29.4058817646, "j2": 14.7029408823, "j3": 9.8019605882}
    for node_id, current in expected.items():
        assert reference.fault_current_ka(node_id, 10.5) == pytest.approx(current, **STRICT)


def _example_4_network() -> Network:
    network = Network("Пример 4")
    for node_id in ("source_bus", "j1", "j2", "j3"):
        network.add_node(Node(node_id, node_id, 10))
    _add_ideal_source(network, "source_bus")
    for index, (node_from, node_to) in enumerate(
        (("source_bus", "j1"), ("j1", "j2"), ("j2", "j3")), start=1
    ):
        network.add_branch(LineBranch(
            f"s{index}", f"Участок {index}", node_from, node_to,
            length_km=1.0, r0=0.05, x0=0.2,
        ))
    network.add_mode(Mode("normal", "Нормальный режим"))
    return network


def test_example_4_production_three_serial_sections() -> None:
    """Пример 4: production-ядро сохраняет электрический порядок трёх участков."""

    network = _example_4_network()
    solver = ShortCircuitSolver(network, Mode("audit", "Контрольный режим"), Methodology.load())
    expected = {"j1": 29.4058817646, "j2": 14.7029408823, "j3": 9.8019605882}
    for node_id, current in expected.items():
        assert solver.at(node_id).i3 == pytest.approx(current, **STRICT)


def _example_5_network() -> Network:
    network = Network("Пример 5")
    for node_id in ("source_1", "source_2", "before_1", "before_2", "fault"):
        network.add_node(Node(node_id, node_id, 10))
    _add_ideal_source(network, "source_1", "ideal_1")
    _add_ideal_source(network, "source_2", "ideal_2")
    network.add_branch(LineBranch(
        "path_1", "Путь 1", "source_1", "before_1",
        length_km=1.0, r0=0.1, x0=0.4,
    ))
    network.add_branch(LineBranch(
        "path_2", "Путь 2", "source_2", "before_2",
        length_km=1.0, r0=0.1, x0=0.4,
    ))
    network.add_branch(TieBranch(
        "recloser_1", "Реклоузер 1", "before_1", "fault",
        switchable=True, normally_closed=True,
    ))
    network.add_branch(TieBranch(
        "recloser_2", "Реклоузер 2", "before_2", "fault",
        switchable=True, normally_closed=True,
    ))
    network.add_mode(Mode(
        "both", "Оба пути", states={"recloser_1": True, "recloser_2": True}
    ))
    network.add_mode(Mode(
        "one", "Первый путь отключён",
        states={"recloser_1": False, "recloser_2": True},
    ))
    network.add_mode(Mode(
        "none", "Оба пути отключены",
        states={"recloser_1": False, "recloser_2": False},
    ))
    return network


def test_example_5_two_sources_and_recloser() -> None:
    """Пример 5: отключение одного пути не удаляет питание от второго источника."""

    network = _example_5_network()
    both = network.modes["both"]
    one = network.modes["one"]
    none = network.modes["none"]

    both_solver = ShortCircuitSolver(network, both, Methodology.load())
    total = both_solver.at("fault").i3
    assert total == pytest.approx(29.4058817646, **STRICT)
    for branch_id in ("path_1", "path_2"):
        factor = both_solver.distribution_factor(network.branches[branch_id], "fault")
        assert total * factor == pytest.approx(I_ONE, **STRICT)

    assert ShortCircuitSolver(network, one, Methodology.load()).at("fault").i3 == pytest.approx(
        I_ONE, **STRICT
    )
    with pytest.raises(KeyError, match="не запитан"):
        ShortCircuitSolver(network, none, Methodology.load()).at("fault")


def _reference_example_6() -> dict[str, float]:
    base_power_mva = 100.0
    generator = complex(0.0, 0.20 * base_power_mva / 25.0)

    def transformer(uk_percent: float, p_k_mw: float, s_nom_mva: float) -> complex:
        z_abs = uk_percent / 100.0 * base_power_mva / s_nom_mva
        resistance = p_k_mw * base_power_mva / s_nom_mva**2
        return complex(resistance, math.sqrt(z_abs**2 - resistance**2))

    def line(r_ohm: float, x_ohm: float, base_voltage_kv: float) -> complex:
        z_base = base_voltage_kv**2 / base_power_mva
        return complex(r_ohm, x_ohm) / z_base

    total = sum((
        generator,
        transformer(10.5, 0.120, 25.0),
        line(0.12 * 30.0, 0.40 * 30.0, 110.0),
        transformer(10.5, 0.085, 16.0),
        line(0.125 * 2.0, 0.080 * 2.0, 10.0),
        transformer(6.0, 0.0108, 1.0),
    ), start=0.0j)
    fault_current = per_unit_fault_current_ka(
        base_power_mva=base_power_mva,
        fault_voltage_kv=0.4,
        total_impedance_pu=total,
    )
    return {
        "r_pu": total.real,
        "x_pu": total.imag,
        "z_pu": abs(total),
        "i_0_4_ka": fault_current,
        "i_10_ka": fault_current * 0.4 / 10.0,
        "i_110_ka": fault_current * 0.4 / 110.0,
        "i_10_5_ka": fault_current * 0.4 / 10.5,
    }


def test_example_6_independent_per_unit_reference() -> None:
    """Пример 6: независимый расчёт в о.е. воспроизводит контрольные числа ТЗ."""

    actual = _reference_example_6()
    expected = {
        "r_pu": 1.4121551911,
        "x_pu": 8.0361436299,
        "z_pu": 8.1592761152,
        "i_0_4_ka": 17.6899966687,
        "i_10_ka": 0.7075998667,
        "i_110_ka": 0.0643272606,
        "i_10_5_ka": 0.6739046350,
    }
    for key, value in expected.items():
        assert actual[key] == pytest.approx(value, **STRICT)


def test_example_6_independent_named_ohms_matches_per_unit() -> None:
    """Один пример независимо сверен в именованных единицах и в о.е.

    Здесь не вызываются production ``impedance``/``refer``: каждое паспортное
    сопротивление строится прямо в омах своей ступени и приводится к 0,4 кВ
    по согласованным паспортным отношениям 10,5/110/10/0,4.
    """

    def refer_to_fault(z_ohm: complex, source_voltage_kv: float) -> complex:
        return z_ohm * (0.4 / source_voltage_kv) ** 2

    def transformer_ohm(
        *, uk_percent: float, p_k_mw: float, s_nom_mva: float, voltage_kv: float
    ) -> complex:
        z_abs = uk_percent / 100.0 * voltage_kv**2 / s_nom_mva
        resistance = p_k_mw * voltage_kv**2 / s_nom_mva**2
        return complex(resistance, math.sqrt(z_abs**2 - resistance**2))

    generator_ohm_10_5 = complex(0.0, 0.20 * 10.5**2 / 25.0)
    t1_ohm_110 = transformer_ohm(
        uk_percent=10.5, p_k_mw=0.120, s_nom_mva=25.0, voltage_kv=110.0
    )
    line_110_ohm = complex(0.12 * 30.0, 0.40 * 30.0)
    t2_ohm_110 = transformer_ohm(
        uk_percent=10.5, p_k_mw=0.085, s_nom_mva=16.0, voltage_kv=110.0
    )
    line_10_ohm = complex(0.125 * 2.0, 0.080 * 2.0)
    t3_ohm_10 = transformer_ohm(
        uk_percent=6.0, p_k_mw=0.0108, s_nom_mva=1.0, voltage_kv=10.0
    )
    total_ohm_0_4 = sum((
        refer_to_fault(generator_ohm_10_5, 10.5),
        refer_to_fault(t1_ohm_110, 110.0),
        refer_to_fault(line_110_ohm, 110.0),
        refer_to_fault(t2_ohm_110, 110.0),
        refer_to_fault(line_10_ohm, 10.0),
        refer_to_fault(t3_ohm_10, 10.0),
    ), start=0.0j)
    current_from_ohms = 0.4 / (math.sqrt(3.0) * abs(total_ohm_0_4))
    per_unit = _reference_example_6()

    assert current_from_ohms == pytest.approx(per_unit["i_0_4_ka"], **STRICT)
    assert current_from_ohms == pytest.approx(17.6899966687, **STRICT)


def _example_6_network() -> Network:
    """Сеть контрольного примера 6 с ЯВНО заданными расчётными напряжениями.

    Эталон примера построен в базисе 10,5 / 110 / 10 / 0,4 кВ — эти напряжения
    являются частью условия задачи, а не следствием таблицы средних
    напряжений. После перевода ядра на классический базис Uср (этап R1)
    условие задаётся тем механизмом, который для этого и предназначен:
    ``Node.calculation_base_kv`` переопределяет расчётное напряжение ступени.
    Так контрольные числа ТЗ сохраняются, а заодно проверяется сам механизм
    переопределения.
    """
    network = Network("Пример 6")
    for node in (
        # Электрический узел относится к классу сети 10 кВ. Паспортное
        # напряжение генератора 10,5 кВ хранится отдельно в GeneratorBranch.
        Node("g", "Шины генератора", 10.0, calculation_base_kv=10.5),
        Node("n110a", "110 кВ A", 110, calculation_base_kv=110.0),
        Node("n110b", "110 кВ B", 110, calculation_base_kv=110.0),
        Node("n10a", "10 кВ A", 10, calculation_base_kv=10.0),
        Node("n10b", "10 кВ B", 10, calculation_base_kv=10.0),
        Node("n04", "0,4 кВ", 0.4, calculation_base_kv=0.4),
    ):
        network.add_node(node)
    branches = (
        GeneratorBranch(
            "g1", "G1", GRID, "g", s_nom=25000.0, u_nom=10.5, xd2=0.20,
            ct_ratio=(1.0, 1.0), ct_node="g",
        ),
        TransformerBranch(
            "t1", "T1", "g", "n110a", s_nom=25000.0,
            u_hv=110.0, u_lv=10.5, uk=10.5, p_k=120.0,
        ),
        LineBranch(
            "l110", "ВЛ-110", "n110a", "n110b", line_type="overhead",
            length_km=30.0, r0=0.12, x0=0.40,
            ct_ratio=(1.0, 1.0), ct_node="n110a",
        ),
        TransformerBranch(
            "t2", "T2", "n110b", "n10a", s_nom=16000.0,
            u_hv=110.0, u_lv=10.0, uk=10.5, p_k=85.0,
        ),
        LineBranch(
            "l10", "КЛ-10", "n10a", "n10b", line_type="cable",
            length_km=2.0, r0=0.125, x0=0.080,
            ct_ratio=(1.0, 1.0), ct_node="n10a",
        ),
        TransformerBranch(
            "t3", "T3", "n10b", "n04", s_nom=1000.0,
            u_hv=10.0, u_lv=0.4, uk=6.0, p_k=10.8,
        ),
    )
    for branch in branches:
        network.add_branch(branch)
    network.add_mode(Mode("audit", "Контрольный режим"))
    return network


@lru_cache(maxsize=1)
def _production_example_6() -> dict[str, float]:
    network = _example_6_network()
    mode = network.modes["audit"]
    context = Context(network, Methodology.load())
    fault = context.solvers[mode.id].at("n04")
    z_base_ohm = 0.4**2 / 100.0

    def through(branch_id: str) -> float:
        current_a, _, _ = context.current_through(
            mode, network.branches[branch_id], "n04"
        )
        return current_a / 1000.0

    return {
        "r_pu": fault.z_th.real / z_base_ohm,
        "x_pu": fault.z_th.imag / z_base_ohm,
        "z_pu": abs(fault.z_th) / z_base_ohm,
        "i_0_4_ka": fault.i3,
        "i_10_ka": through("l10"),
        "i_110_ka": through("l110"),
        "i_10_5_ka": through("g1"),
    }


def test_example_6_production_impedance_components_match_reference() -> None:
    """После исправления модели напряжений RΣ*, XΣ* и |ZΣ* совпадают с эталоном."""

    actual = _production_example_6()
    independent_reference = _reference_example_6()
    for key in ("r_pu", "x_pu", "z_pu"):
        assert actual[key] == pytest.approx(independent_reference[key], **STRICT)


@pytest.mark.parametrize(
    ("key", "expected"),
    (
        ("i_0_4_ka", 17.6899966687),
        ("i_10_ka", 0.7075998667),
        ("i_110_ka", 0.0643272606),
        ("i_10_5_ka", 0.6739046350),
    ),
    ids=("КЗ 0,4 кВ", "ветвь 10 кВ", "ВЛ 110 кВ", "сторона генератора 10,5 кВ"),
)
def test_example_6_production_voltage_model_matches(key: str, expected: float) -> None:
    """Пример 6 совпадает с независимым расчётом при явных физических ступенях."""

    assert _production_example_6()[key] == pytest.approx(expected, **STRICT)


def test_two_phase_factor_uses_exact_equal_sequence_value() -> None:
    """При Z2=Z1 контрольный коэффициент равен sqrt(3)/2, не 0,866."""

    network = Network("Проверка Iк2")
    network.add_node(Node("source_bus", "Шины источника", 10))
    network.add_node(Node("fault", "Точка КЗ", 10))
    _add_ideal_source(network, "source_bus")
    network.add_branch(LineBranch(
        "z", "Контрольное Z", "source_bus", "fault",
        length_km=1.0, r0=0.1, x0=0.4,
    ))
    solver, _ = _solver(network, "fault")
    result = solver.at("fault")
    assert result.i2 == pytest.approx(result.i3 * math.sqrt(3.0) / 2.0, **STRICT)
    assert result.i2_is_approximation
    assert "Z2 = Z1" in result.i2_note


def test_branch_current_sign_changes_when_branch_orientation_is_reversed() -> None:
    """Ориентированный комплексный ток обязан менять знак при развороте ветви."""

    def factor(node_from: str, node_to: str) -> complex:
        network = Network("Направление тока")
        network.add_node(Node("source_bus", "Шины источника", 10))
        network.add_node(Node("fault", "Точка КЗ", 10))
        _add_ideal_source(network, "source_bus")
        network.add_branch(LineBranch(
            "z", "Контрольное Z", node_from, node_to,
            length_km=1.0, r0=0.1, x0=0.4,
        ))
        solver, _ = _solver(network, "fault")
        return solver.distribution_factor_complex(network.branches["z"], "fault")

    forward = factor("source_bus", "fault")
    reversed_orientation = factor("fault", "source_bus")
    assert reversed_orientation == pytest.approx(-forward, **STRICT)


def test_legacy_core_validation_rejects_nan_source_data() -> None:
    """NaN не должен доходить до матрицы и результата КЗ."""

    network = Network("NaN")
    network.add_node(Node("bus", "Шины", 10))
    network.add_branch(SourceBranch(
        "source", "Источник", GRID, "bus",
        s_kz_max=float("nan"), s_kz_min=float("nan"),
    ))
    network.add_mode(Mode("audit", "Контрольный режим"))
    assert any("NaN" in problem for problem in network.validate())


def test_legacy_core_validation_rejects_negative_short_circuit_power() -> None:
    """Отрицательная мощность КЗ не должна давать обычный положительный ток."""

    network = Network("Отрицательная Sкз")
    network.add_node(Node("bus", "Шины", 10))
    network.add_branch(SourceBranch(
        "source", "Источник", GRID, "bus",
        s_kz_max=-100.0, s_kz_min=-100.0,
    ))
    network.add_mode(Mode("audit", "Контрольный режим"))
    assert network.validate()
