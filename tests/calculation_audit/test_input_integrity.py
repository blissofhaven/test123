# -*- coding: utf-8 -*-
"""Регрессионные проверки входных данных и повторяемости расчёта.

Проверки используют публичный производственный путь ``core.Network`` и
подтверждают закрытие опасных входных дефектов после аудита 4.4.
"""
from __future__ import annotations

import math

import pytest

from rza_calc.core.engine import CalculationInputError, run
from rza_calc.core.impedance import line_impedance
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID,
    LineBranch,
    Load,
    Mode,
    Network,
    Node,
    SourceBranch,
    TransformerBranch,
)
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.io.project import load, save


def _radial_network(*, line_r: float = 0.1, line_x: float = 0.4) -> Network:
    net = Network("Аудиторская радиальная схема")
    net.add_node(Node("bus", "Шины 10 кВ", 10.0))
    net.add_node(Node("fault", "Точка КЗ", 10.0, kind="point"))
    net.add_branch(SourceBranch(
        id="source",
        name="Система",
        node_from=GRID,
        node_to="bus",
        s_kz_max=1.0e9,
        s_kz_min=1.0e9,
    ))
    net.add_branch(LineBranch(
        id="line",
        name="Линия",
        node_from="bus",
        node_to="fault",
        length_km=1.0,
        r0=line_r,
        x0=line_x,
    ))
    net.add_mode(Mode("normal", "Нормальный режим"))
    return net


def test_repeated_calculation_is_bitwise_repeatable() -> None:
    """Одинаковые входы дают одинаковые complex128/float результаты."""
    net = _radial_network()
    meth = Methodology.load()
    first = ShortCircuitSolver(net, net.modes["normal"], meth).at("fault")
    second = ShortCircuitSolver(net, net.modes["normal"], meth).at("fault")

    assert first.z_th == second.z_th
    assert first.i3 == second.i3
    assert first.i2 == second.i2


def test_save_reload_preserves_short_circuit_result(tmp_path) -> None:
    """Сохранение и новая загрузка не меняют ток при неизменных данных."""
    net = _radial_network()
    meth = Methodology.load()
    expected = ShortCircuitSolver(net, net.modes["normal"], meth).at("fault").i3
    project_path = tmp_path / "audit-roundtrip.json"

    save(project_path, net, {"name": "Аудиторская радиальная схема"})
    loaded, loaded_meth, _ = load(project_path)
    actual = ShortCircuitSolver(
        loaded, loaded.modes["normal"], loaded_meth
    ).at("fault").i3

    assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), (
        f"ожидалось {expected:.12g} кА, получено {actual:.12g} кА; "
        "допуск 1e-12"
    )


def test_missing_line_length_blocks_engine() -> None:
    """Нулевая физическая длина блокирует производственный ``run``."""
    net = _radial_network()
    net.branches["line"].length_km = 0.0

    with pytest.raises(CalculationInputError, match="не задана длина"):
        run(net, Methodology.load())


def test_placeholder_methodology_is_explicitly_reported() -> None:
    """Заглушка пока не блокирует расчёт, но обязана дать видимое предупреждение.

    Заглушка строится явно: с этапа A2 профиль в поставке заявляет статус
    ТИПОВОЙ, и проверка «профиль по умолчанию — заглушка» проверяла бы уже не
    механизм предупреждения, а комплектацию.
    """
    methodology = Methodology.load()
    methodology.data["status"] = "ЗАГЛУШКА"
    result = run(_radial_network(), methodology)

    assert any("ЗАГЛУШКА" in warning for warning in result.warnings)


def test_typical_methodology_is_reported_as_not_approved() -> None:
    """Типовой профиль обязан сообщать, что он не утверждён для объекта.

    Молчание здесь опаснее заглушки: числа выглядят согласованными, хотя
    источники не сверены и никто их не утверждал.
    """
    result = run(_radial_network(), Methodology.load())

    assert not any("ЗАГЛУШКА" in warning for warning in result.warnings)
    assert any(
        "ТИПОВОЙ" in warning and "не утверждён" in warning
        for warning in result.warnings
    )


def test_unverified_line_fallback_is_visible_in_trace() -> None:
    """Подстановка табличного X для линии маркируется как ориентировочная."""
    branch = LineBranch(
        id="fallback",
        name="Линия без подтверждённых r1/x1",
        node_from="a",
        node_to="b",
        length_km=1.0,
        section_mm2=95.0,
        material="Al",
        line_type="cable",
    )

    _, step = line_impedance(branch, Methodology.load(), "max")

    assert step.note is not None
    assert "ориентировоч" in step.note.lower()


def test_unenergized_island_is_not_reported_as_zero_current() -> None:
    """Точка без связи с источником даёт диагностический статус, а не 0 кА."""
    net = _radial_network()
    net.add_node(Node("island", "Остров", 10.0, kind="point"))
    solver = ShortCircuitSolver(net, net.modes["normal"], Methodology.load())

    with pytest.raises(KeyError, match="не запитан"):
        solver.at("island")


def test_non_finite_line_impedance_must_be_rejected() -> None:
    """Ожидание аудита: NaN должен блокироваться до построения Ybus."""
    net = _radial_network(line_r=math.nan)

    problems = net.validate()

    assert any("конеч" in problem.lower() or "nan" in problem.lower() for problem in problems), (
        "ожидалась ошибка конечности r1; фактически Network.validate() вернул "
        f"{problems!r}"
    )


def test_conflicting_source_power_and_current_must_be_rejected() -> None:
    """Два взаимоисключающих первичных задания источника требуют сверки."""
    net = _radial_network()
    source = net.branches["source"]
    assert isinstance(source, SourceBranch)
    source.s_kz_max = 100.0
    source.i_kz_max = 1.0  # при 10,5 кВ это не соответствует 100 МВА

    problems = net.validate()

    assert any("противореч" in problem.lower() for problem in problems), (
        "ожидалась блокировка противоречивых Sкз/Iкз; фактически ошибок нет"
    )


def test_motor_contribution_omission_must_be_reported() -> None:
    """Неполная модель КЗ не должна выглядеть как полный результат."""
    net = _radial_network()
    net.add_load(Load(
        "motor-load",
        "Двигательная нагрузка",
        "fault",
        p_kw=1000.0,
        cos_phi=0.9,
        motor_share=0.8,
    ))

    result = run(net, Methodology.load())

    assert any("двигател" in warning.lower() for warning in result.warnings), (
        "ожидалось предупреждение об исключённой подпитке двигателей; "
        f"фактические предупреждения: {result.warnings!r}"
    )


def test_negative_line_impedance_must_be_rejected() -> None:
    """Отрицательное последовательное сопротивление требует явной спецмодели."""
    net = _radial_network(line_r=-0.1, line_x=0.4)

    problems = net.validate()

    assert any("отриц" in problem.lower() for problem in problems), (
        "ожидалась блокировка отрицательного r1; фактически ошибок нет"
    )


def test_negative_two_winding_transformer_uk_must_be_rejected() -> None:
    """Исключение для луча звезды 3W нельзя переносить на обычный аппарат 2W."""
    net = Network("Отрицательный Uk двухобмоточного трансформатора")
    net.add_node(Node("hv", "ВН", 10.0))
    net.add_node(Node("lv", "НН", 0.4))
    net.add_branch(SourceBranch(
        id="source",
        name="Система",
        node_from=GRID,
        node_to="hv",
        s_kz_max=1.0e18,
        s_kz_min=1.0e18,
    ))
    net.add_branch(TransformerBranch(
        id="transformer",
        name="Т 10/0,4 кВ",
        node_from="hv",
        node_to="lv",
        s_nom=1000.0,
        u_hv=10.0,
        u_lv=0.4,
        uk=-6.0,
    ))
    net.add_mode(Mode("normal", "Нормальный режим"))

    problems = net.validate()

    assert any("uk" in problem.lower() or "uк" in problem.lower() for problem in problems), (
        "ожидалась блокировка Uk=-6% для 2W; фактически ошибок нет"
    )
