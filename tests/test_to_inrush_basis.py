# -*- coding: utf-8 -*-
"""Напряжение, на котором считается Iном в отстройке ТО от броска намагничивания.

Этап A2, пункт 5 ТЗ. Условие отстройки от броска — единственное место расчёта,
где паспортная величина трансформатора (Sном) превращалась в ток по СРЕДНЕМУ
напряжению ступени. Разница между 10 и 10,5 кВ — около 5 %, и она направлена в
опасную сторону: чем выше принятое напряжение, тем НИЖЕ Iном и тем хуже
отсечка отстроена от броска.

Первоначально демо-проекты этот путь не проходили. Этап 1 добавил собственный
бросок трансформаторов, но в демо это условие не определяет уставку. Поэтому
независимые синтетические проверки выбора напряжения остаются необходимыми.
"""
from __future__ import annotations

import math

import pytest

from rza_calc.core.engine import run
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
                                 SourceBranch, TransformerBranch)

#  Паспортные величины КТП, на которых считается ожидаемый ответ вручную.
S_NOM_KVA = 630.0
U_HV_KV = 10.0
INRUSH_RATIO = 5.0


def _network_with_downstream_transformer() -> Network:
    """Линия 10 кВ с ТО «за трансформатором» и КТП 10/0,4 кВ в конце."""
    net = Network("Отстройка ТО от броска намагничивания")
    net.add_node(Node("bus10", "Шины 10 кВ", 10.0))
    net.add_node(Node("ktp_hv", "КТП, ввод 10 кВ", 10.0))
    net.add_node(Node("ktp_lv", "КТП, шины 0,4 кВ", 0.4))
    net.add_branch(SourceBranch(
        id="source", name="Система 10 кВ", node_from=GRID, node_to="bus10",
        s_kz_max=250.0e3, s_kz_min=150.0e3,
    ))
    feeder = LineBranch(
        id="feeder", name="Фидер 10 кВ", node_from="bus10", node_to="ktp_hv",
        length_km=2.0, r0=0.428, x0=0.375, ct_ratio=(200.0, 5.0),
    )
    feeder.prot.mtz = True
    feeder.prot.to = True
    feeder.prot.ozz = False
    feeder.prot.to_reach = "behind_transformer"
    net.add_branch(feeder)
    net.add_branch(TransformerBranch(
        id="ktp", name="КТП 630 кВ·А", node_from="ktp_hv", node_to="ktp_lv",
        s_nom=S_NOM_KVA, u_hv=U_HV_KV, u_lv=0.4, uk=5.5, p_k=7.6,
        i_inrush_ratio=INRUSH_RATIO,
    ))
    net.add_load(Load("load", "Нагрузка 0,4 кВ", "ktp_lv", p_kw=350.0))
    net.add_mode(Mode("normal", "Нормальный режим", system="max"))
    net.add_mode(Mode("min", "Минимальный режим", system="min"))
    return net


def _inrush_step(methodology: Methodology):
    net = _network_with_downstream_transformer()
    result = run(net, methodology)
    protection = result.get("feeder", "ТО")
    assert protection is not None
    for step in protection.steps:
        if "броска тока намагничивания" in step.what:
            return step
    pytest.fail(
        "условие отстройки от броска намагничивания не посчиталось — "
        "тест перестал проверять то, ради чего написан"
    )


def _i_nom_from(step) -> float:
    raw = step.given["Iном (на стороне защиты)"].split()[0]
    return float(raw.replace(" ", "").replace(" ", "").replace(",", "."))


def test_demo_own_transformers_cover_inrush_without_inventing_missing_ratios():
    """Этап 1 добавил собственный бросок 17 КТП с известным Kбр.

    Числовые проверки ниже остаются независимыми синтетическими примерами:
    в демо добавленное условие не определяет уставку. Для четырёх блочных
    трансформаторов ГТЭС без Kбр число не выдумывается.
    """
    from pathlib import Path

    from rza_calc.io.project import load

    root = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "legacy_projects"
    covered = {}
    for name in ("energoraion.json", "gtes_sever.json", "ps_severnaya.json"):
        net, methodology, _ = load(root / name)
        result = run(net, methodology)
        covered[name] = []
        for protection in result.all_results():
            if protection.kind != "ТО":
                continue
            if any("броска тока намагничивания" in s.what for s in protection.steps):
                covered[name].append(protection.branch_id)
        if name == "gtes_sever.json":
            for number in range(1, 5):
                protection = result.get(f"G{number}_bt", "ТО")
                assert not protection.is_complete
                assert net.branches[f"G{number}_bt"].i_inrush_ratio is None
                assert any("при питании с двух сторон" in problem
                           for row in protection.coverage for problem in row.problems)
    assert {name: len(rows) for name, rows in covered.items()} == {
        "energoraion.json": 16, "gtes_sever.json": 0, "ps_severnaya.json": 1,
    }


def test_nameplate_basis_uses_the_transformer_winding_voltage():
    """Iном = Sном/(√3·Uном обмотки), а не по среднему напряжению ступени."""
    methodology = Methodology.load()
    assert methodology.text("to.i_nom_basis") == "nameplate"
    step = _inrush_step(methodology)
    expected = S_NOM_KVA / (math.sqrt(3.0) * U_HV_KV)

    assert _i_nom_from(step) == pytest.approx(expected, rel=1e-3)
    assert "паспортное напряжение обмотки" in step.given["Напряжение для Iном"]
    assert "10" in step.given["Напряжение для Iном"]


def test_stage_average_basis_reproduces_the_previous_behaviour():
    """Прежний способ остаётся доступным — иначе старый расчёт не повторить."""
    methodology = Methodology.load()
    methodology.data["to"]["i_nom_basis"]["value"] = "stage_average"
    step = _inrush_step(methodology)
    expected = S_NOM_KVA / (math.sqrt(3.0) * methodology.u_avg(U_HV_KV))

    assert _i_nom_from(step) == pytest.approx(expected, rel=1e-3)
    assert "среднее расчётное напряжение" in step.given["Напряжение для Iном"]


def test_the_change_raises_the_setting_and_that_direction_is_the_safe_one():
    """Новый способ даёт БОЛЬШУЮ уставку — то есть лучшую отстройку от броска.

    Проверяется именно знак разницы. Если бы правка снижала уставку, она
    ухудшала бы отстройку, и «уточнение методики» на деле означало бы рост
    вероятности ложного отключения при включении трансформатора.
    """
    nameplate = Methodology.load()
    average = Methodology.load()
    average.data["to"]["i_nom_basis"]["value"] = "stage_average"

    i_nameplate = _i_nom_from(_inrush_step(nameplate))
    i_average = _i_nom_from(_inrush_step(average))

    assert i_nameplate > i_average
    ratio = nameplate.u_avg(U_HV_KV) / U_HV_KV
    assert i_nameplate / i_average == pytest.approx(ratio, rel=1e-3)


def test_basis_choice_is_recorded_in_the_protocol_and_in_the_snapshot():
    """Решение обязано быть видно в протоколе и попасть в паспорт расчёта."""
    methodology = Methodology.load()
    net = _network_with_downstream_transformer()
    result = run(net, methodology)

    step = _inrush_step(methodology)
    assert "Напряжение для Iном" in step.given, (
        "выбор основания не показан в протоколе: два разных ответа выглядели "
        "бы одинаково обоснованными"
    )
    snapshot = result.calculation_case.methodology_snapshot
    assert snapshot.value("to.i_nom_basis") == "nameplate"


def test_nameplate_from_another_stage_falls_back_to_the_node_class():
    """Паспорт не с той ступени не используется, и подмены средним нет.

    ГРАНИЦА ПРОВЕРКИ. Через `run()` этот случай для обычного двухобмоточного
    трансформатора недостижим: `Network.validate()` блокирует расчёт раньше,
    сообщая, что паспортные напряжения не соответствуют классам узлов. Поэтому
    ветвь проверяется прямым вызовом, и это не выдаётся за сквозную проверку.
    Ветвь не мёртвая: у внутренних лучей трёхобмоточного трансформатора
    (`internal_star_leg`) паспортные напряжения этой проверки не проходят и до
    неё не доходят.
    """
    from rza_calc.core.protections.to import _inrush_voltage

    methodology = Methodology.load()
    transformer = TransformerBranch(
        id="leg", name="Луч звезды", node_from="a", node_to="b",
        s_nom=S_NOM_KVA, u_hv=6.0, u_lv=0.0, uk=5.5, p_k=7.6,
    )
    voltage, description = _inrush_voltage(
        methodology, transformer, U_HV_KV, "nameplate"
    )

    assert voltage == U_HV_KV
    assert "номинальный класс стороны защиты" in description
    assert voltage != methodology.u_avg(U_HV_KV), (
        "запасной вариант обязан оставаться номинальным напряжением, иначе он "
        "молча возвращает прежнее поведение там, где данных меньше всего"
    )


def test_nameplate_mismatch_is_blocked_before_the_protection_is_calculated():
    """Парная проверка к предыдущей: расхождение паспорта не доходит до защиты.

    Она и объясняет, почему предыдущая проверка вызывает функцию напрямую.
    """
    net = _network_with_downstream_transformer()
    net.branches["ktp"].u_hv = 6.0
    from rza_calc.core.engine import CalculationInputError

    with pytest.raises(CalculationInputError, match="не соответствуют классам"):
        run(net, Methodology.load())
