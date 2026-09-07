# -*- coding: utf-8 -*-
"""Идеальный выключатель не создаёт второй точки той же зоны (AUD-PROT-011).

Ветвь нулевого сопротивления — включённый выключатель, разъединитель, луч
звезды с нулевым Uк — соединяет два узла в ОДНУ электрическую точку: ток КЗ в
них совпадает до последнего бита. Матрица проводимостей объединяет их ещё до
сборки; зона защиты обязана считать так же.

Пока `zone_points()` различала точки по ID узла, каждый нарисованный
выключатель добавлял «проверку Kч в зоне резервирования» на месте основной
зоны — с требованием 1,2 вместо 1,5. Это опаснее лишней строки в таблице:
защита, не прошедшая основную зону, показывала рядом пройденную проверку на
том же физическом месте.

Дефект найден Sol в опыте с 64 выключателями (35 ложных проверок),
воспроизведён и закрыт при сборке демо-подстанции.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rza_calc.core.engine import run
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
                                 SourceBranch, TieBranch)
from rza_calc.core.result import OK
from rza_calc.io.project import load

DEMO = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/ps_promyshlennaya.json"


def _network(*, with_breaker: bool) -> Network:
    net = Network("Идеальный выключатель после фидера")
    for node_id, name, voltage in (("bus", "Шины 10 кВ", 10.0),
                                   ("end", "Конец фидера", 10.0),
                                   ("cb", "За выключателем", 10.0)):
        net.add_node(Node(node_id, name, voltage))
    net.add_branch(SourceBranch(id="src", name="Система", node_from=GRID,
                                node_to="bus", s_kz_max=250e3, s_kz_min=150e3))
    feeder = LineBranch(id="feeder", name="Фидер", node_from="bus", node_to="end",
                        length_km=3.0, r0=0.428, x0=0.375, ct_ratio=(200.0, 5.0))
    feeder.prot.mtz, feeder.prot.to, feeder.prot.ozz = True, False, False
    net.add_branch(feeder)
    if with_breaker:
        net.add_branch(TieBranch(id="q1", name="Выключатель Q1", node_from="end",
                                 node_to="cb", normally_closed=True))
        net.add_load(Load("l", "Нагрузка", "cb", p_kw=800.0))
    else:
        net.add_load(Load("l", "Нагрузка", "end", p_kw=800.0))
    net.add_mode(Mode("normal", "Нормальный", system="max"))
    net.add_mode(Mode("min", "Минимальный", system="min"))
    return net


def _zone(with_breaker: bool):
    result = run(_network(with_breaker=with_breaker), Methodology.load())
    context = result.ctx
    branch = context.net.branches["feeder"]
    main, backup = context.zone_points(branch, context.net.modes["normal"])
    return result, main, backup


def test_ideal_breaker_does_not_add_a_backup_point_on_the_main_zone():
    """Ядро проверки: результат обязан совпасть с точностью до строки."""
    plain, plain_main, plain_backup = _zone(False)
    switched, switched_main, switched_backup = _zone(True)

    assert [point.node_id for point in plain_backup] == []
    assert [point.node_id for point in switched_backup] == [], (
        "идеальный выключатель добавил точку зоны резервирования на месте "
        "основной зоны — это дефект AUD-PROT-011"
    )
    assert [point.node_id for point in plain_main] == [point.node_id for point in switched_main]


def test_the_added_node_carries_the_same_current_to_the_last_digit():
    """Обоснование правила: это не «почти одинаково», а одно и то же число."""
    result, _, _ = _zone(True)
    solver = result.ctx.solvers["min"]
    assert solver.at("end").i3 == solver.at("cb").i3
    assert solver.electrical_point("end") == solver.electrical_point("cb")


def test_checks_and_settings_are_identical_with_and_without_the_breaker():
    plain, _, _ = _zone(False)
    switched, _, _ = _zone(True)

    def rows(result):
        return {
            f"{row.branch_id}|{row.kind}": (
                row.i_primary, row.t, str(row.status),
                tuple((check.name, check.value, check.required) for check in row.checks),
            )
            for row in result.all_results()
        }

    assert rows(plain) == rows(switched)


def test_an_open_breaker_still_cuts_the_zone():
    """Послабление не должно превратиться в «выключателя как будто нет»."""
    net = _network(with_breaker=True)
    net.branches["q1"].normally_closed = False
    net.modes["normal"].states["q1"] = False
    result = run(net, Methodology.load())
    solver = result.ctx.solvers["normal"]
    with pytest.raises(Exception):
        solver.at("cb")          # узел за отключённым выключателем не запитан


def test_electrical_point_of_an_unknown_node_is_the_node_itself():
    """Без решателя объединение неизвестно, и выдумывать его нельзя."""
    result, _, _ = _zone(True)
    solver = result.ctx.solvers["normal"]
    assert solver.electrical_point("несуществующий-узел") == "несуществующий-узел"


# ── та же проверка на настоящей демо-подстанции ───────────────────────────
def test_demo_substation_has_no_duplicate_zone_points():
    """13 выключателей и ни одной проверки-двойника."""
    net, methodology, _ = load(DEMO)
    result = run(net, methodology)
    context = result.ctx
    problems = []
    for branch in net.protection_points():
        for mode in context.modes_where_active(branch):
            main, backup = context.zone_points(branch, mode)
            point = context._electrical_point(mode)
            main_points = {point(row.node_id) for row in main}
            for row in backup:
                if point(row.node_id) in main_points:
                    problems.append(f"{branch.name}/{mode.name}: {row.name}")
    assert not problems, "точки резервирования совпали с основной зоной:\n  " + "\n  ".join(problems)


def test_demo_substation_switches_are_real_and_switchable():
    """Выключатели обязаны коммутировать, иначе это картинки."""
    net, _, _ = load(DEMO)
    switches = [branch for branch in net.branches.values()
                if type(branch).__name__ == "TieBranch"]
    assert len(switches) == 13
    assert all(branch.switchable for branch in switches)
    repair = net.modes["t1_repair"]
    assert repair.states.get("Q1") is False and repair.states.get("Q3") is False, (
        "ремонт трансформатора обязан выводить его выключателями с ОБЕИХ сторон"
    )
    assert repair.states.get("QB") is True


def test_demo_substation_result_is_stable_and_explained():
    net, methodology, _ = load(DEMO)
    result = run(net, methodology)
    rows = result.all_results()
    assert len(rows) == 23      # 11 МТЗ + 6 ТО + 6 ОЗЗ
    assert sum(1 for row in rows if row.status is OK) == 19
    assert not [row for row in rows if str(row.status) == "unresolved"], (
        "неопределённых результатов в образцовой схеме быть не должно"
    )
    failed = [row for row in rows if row.status is not OK]
    assert {row.kind for row in failed} == {"ОЗЗ"}, (
        "все оставшиеся нарушения обязаны быть одним понятным случаем"
    )
    for row in failed:
        assert any("ёмкостн" in message for message in row.messages), (
            f"{row.branch_name}: нарушение не объяснено в протоколе"
        )
    assert all(str(pair.status) == "ok" for pair in result.pairs)
