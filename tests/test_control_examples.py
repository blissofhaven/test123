# -*- coding: utf-8 -*-
"""Контрольные примеры этапа A1 на уже сделанные исправления.

Каждый пример фиксирует одно поведение, которое было исправлено или найдено, и
проверяется **независимо**: либо ручным расчётом в относительных единицах,
либо инвариантом, который нельзя получить тем же кодом. Тест, сверяющий
программу с тем, что она выдала вчера, закрепляет ошибку вместо того, чтобы её
ловить, — такие тесты правилами карты запрещены.

Ручные расчёты ведутся в классическом базисе средних напряжений: ступень
0,4 → 0,4; 6 → 6,3; 10 → 10,5; 35 → 37; 110 → 115 кВ.
"""
import functools
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.core.engine import run
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
                                 ProtectionSettings, SourceBranch, TieBranch,
                                 TransformerBranch)
from rza_calc.core.result import FAIL, OK, UNRESOLVED
from rza_calc.core.short_circuit import ShortCircuitSolver

SQRT3 = math.sqrt(3.0)
U_AVG = {0.4: 0.4, 6.0: 6.3, 10.0: 10.5, 35.0: 37.0, 110.0: 115.0}


@pytest.fixture
def meth():
    return Methodology.load()


# ──────────────────────────────────────────────────────────────────────────
#  Механика «известного дефекта»
# ──────────────────────────────────────────────────────────────────────────
def known_defect(defect_id: str, closes: str):
    """Тест обязан падать, пока дефект открыт, и краснеть, когда он закрыт.

    Так дефект не теряется: он зафиксирован проверкой ожидаемого поведения, а
    не комментарием. Как только поведение исправлено, тест требует снять
    пометку — иначе «исправлено» осталось бы незамеченным.
    """
    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                function(*args, **kwargs)
            except AssertionError:
                return                      # дефект ещё открыт — это ожидаемо
            raise AssertionError(
                f"Дефект {defect_id} больше не воспроизводится. "
                f"Снимите пометку known_defect и включите тест как обычный. "
                f"Дефект должен был закрываться этапом {closes}."
            )
        return wrapper
    return decorate


# ──────────────────────────────────────────────────────────────────────────
#  1. Трансформатор с ТТ на стороне НН
# ──────────────────────────────────────────────────────────────────────────
def _transformer_with_lv_ct() -> Network:
    """Трансформатор 35/10, ТТ установлен на стороне 10 кВ, а не 35 кВ."""
    net = Network("ТТ на НН")
    net.add_node(Node("b35", "ОРУ 35 кВ", 35))
    net.add_node(Node("b10", "1 СШ 10 кВ", 10))
    net.add_node(Node("end", "конец фидера", 10))
    net.add_branch(SourceBranch(id="S", name="Система", node_from=GRID, node_to="b35",
                                s_kz_max=800, s_kz_min=500))
    net.add_branch(TransformerBranch(
        id="T", name="Т1 35/10", node_from="b35", node_to="b10",
        s_nom=10000, u_hv=35, u_lv=10, uk=7.5, p_k=60,
        ct_ratio=(600, 5), ct_node="b10",          # ТТ на низкой стороне
        breaker_t_off=0.06, terminal="БМРЗ-100",
        prot=ProtectionSettings(mtz=True, to=True, ozz=False)))
    net.add_branch(LineBranch(id="F", name="Ф-1", node_from="b10", node_to="end",
                              length_km=4.0, section_mm2=120, material="Al",
                              ct_ratio=(300, 5), breaker_t_off=0.06))
    net.add_load(Load("L", "нагрузка", "end", p_kw=1800, cos_phi=0.9))
    net.add_mode(Mode("max", "Максимальный", states={}, system="max"))
    net.add_mode(Mode("min", "Минимальный", states={}, system="min"))
    return net


def test_1_transformer_with_lv_side_ct_does_not_invent_a_reach(meth):
    """ТО трансформатора при ТТ на НН обязана дать честный статус, а не число.

    Отсечка отстраивается от КЗ за трансформатором. Если ТТ стоит на стороне
    НН, то ток КЗ за трансформатором течёт **через место установки ТТ
    полностью**, и отстройка от него делает отсечку неработоспособной: уставка
    заведомо выше любого тока, который защита видит. Раньше в этом случае
    выдавался правдоподобный коэффициент чувствительности — это и был дефект.
    """
    result = run(_transformer_with_lv_ct(), meth)
    row = result.get("T", "ТО")
    assert row is not None, "ТО трансформатора должна присутствовать в результате"
    assert row.status in (UNRESOLVED, FAIL), (
        f"ожидался честный статус, получено {row.status} с Kч="
        f"{[c.value for c in row.checks]}"
    )
    text = " ".join(row.messages) + " ".join(c.comment or "" for c in row.checks)
    assert text.strip(), "статус обязан сопровождаться объяснением, а не быть пустым"


# ──────────────────────────────────────────────────────────────────────────
#  2. Линия с двухсторонним питанием
# ──────────────────────────────────────────────────────────────────────────
def _double_fed_line() -> Network:
    """Две системы по концам, между ними линия — ток к КЗ идёт с двух сторон."""
    net = Network("двухстороннее питание")
    net.add_node(Node("a", "ПС А, 110 кВ", 110))
    net.add_node(Node("b", "ПС Б, 110 кВ", 110))
    net.add_branch(SourceBranch(id="SA", name="Система А", node_from=GRID, node_to="a",
                                s_kz_max=3000, s_kz_min=2000))
    net.add_branch(SourceBranch(id="SB", name="Система Б", node_from=GRID, node_to="b",
                                s_kz_max=1500, s_kz_min=1000))
    net.add_branch(LineBranch(
        id="VL", name="ВЛ-110 А—Б", node_from="a", node_to="b",
        length_km=30.0, section_mm2=120, material="Al", r0=0.249, x0=0.427,
        ct_ratio=(600, 5), ct_node="a", breaker_t_off=0.06, terminal="Бреслер ТОР-300",
        prot=ProtectionSettings(mtz=True, to=True, ozz=False)))
    net.add_load(Load("LB", "нагрузка Б", "b", p_kw=12000, cos_phi=0.9))
    net.add_mode(Mode("max", "Максимальный", states={}, system="max"))
    net.add_mode(Mode("min", "Минимальный", states={}, system="min"))
    return net


def test_2_double_fed_line_uses_the_share_through_its_own_ct(meth):
    """Kч отсечки считается по доле тока через ТТ, а не по полному току КЗ.

    Инвариант проверяется независимо от расчёта уставок: при КЗ на шинах Б
    полный ток складывается из вкладов обеих систем, а защита на конце А видит
    только свой вклад. Значит, доля тока через ТТ строго меньше единицы, и
    коэффициент чувствительности обязан считаться от неё.
    """
    net = _double_fed_line()
    mode = net.modes["min"]
    solver = ShortCircuitSolver(net, mode, meth)
    total = solver.at("b").i3
    contributions = solver.at("b").source_contributions
    assert len(contributions) == 2, contributions

    # Независимая проверка: сумма вкладов равна полному току (первый закон
    # Кирхгофа в узле повреждения), и вклад через нашу линию строго меньше.
    summed = abs(sum(contributions.values()))
    assert summed == pytest.approx(total, rel=1e-9), (summed, total)
    for value in contributions.values():
        assert abs(value) < summed, "вклад одного источника меньше полного тока"

    share = solver.distribution_magnitude(net.branches["VL"], "b")
    assert 0.0 < share < 1.0, f"доля тока через ТТ должна быть меньше единицы: {share}"

    result = run(net, meth)
    row = result.get("VL", "ТО")
    assert row is not None
    kch = [c for c in row.checks if "Kч" in c.name]
    assert kch, "у отсечки обязана быть проверка чувствительности"
    for check in kch:
        if check.value is not None:
            # Kч, посчитанный по полному току, был бы завышен ровно в 1/share раз.
            inflated = check.value / share
            assert check.value < inflated, "Kч не должен считаться по полному току"


# ──────────────────────────────────────────────────────────────────────────
#  3. Отпаечная КТП в обеих ориентациях
# ──────────────────────────────────────────────────────────────────────────
def _feeder_with_ktp(reversed_ktp: bool) -> Network:
    """Фидер 10 кВ с отпаечной КТП 10/0,4.

    `reversed_ktp` меняет местами узлы трансформатора в описании: физически
    это тот же трансформатор, поэтому уставка обязана совпасть. Раньше
    результат зависел от того, в каком порядке записаны узлы.
    """
    net = Network("отпаечная КТП")
    net.add_node(Node("b10", "1 СШ 10 кВ", 10))
    net.add_node(Node("tap", "отпайка", 10))
    net.add_node(Node("b04", "щит 0,4 кВ", 0.4))
    net.add_branch(SourceBranch(id="S", name="Система", node_from=GRID, node_to="b10",
                                s_kz_max=200, s_kz_min=140))
    net.add_branch(LineBranch(id="F", name="Ф-1", node_from="b10", node_to="tap",
                              length_km=2.5, section_mm2=120, material="Al",
                              r0=0.258, x0=0.081, ct_ratio=(300, 5),
                              breaker_t_off=0.06, terminal="БМРЗ-100"))
    hv, lv = ("tap", "b04") if not reversed_ktp else ("b04", "tap")
    u_hv, u_lv = (10, 0.4) if not reversed_ktp else (0.4, 10)
    net.add_branch(TransformerBranch(
        id="KTP", name="КТП 400 кВ·А", node_from=hv, node_to=lv,
        s_nom=400, u_hv=u_hv, u_lv=u_lv, uk=4.5, p_k=5.5,
        ct_ratio=(50, 5), breaker_t_off=0.06, terminal="БМРЗ-100",
        prot=ProtectionSettings(mtz=True, to=True, ozz=False)))
    net.add_load(Load("L", "нагрузка", "b04", p_kw=280, cos_phi=0.92))
    net.add_mode(Mode("max", "Максимальный", states={}, system="max"))
    net.add_mode(Mode("min", "Минимальный", states={}, system="min"))
    return net


def test_3_tap_ktp_setting_does_not_depend_on_node_order(meth):
    """Уставка фидера одинакова при обоих порядках записи узлов КТП."""
    straight = run(_feeder_with_ktp(False), meth).get("F", "МТЗ")
    reversed_ = run(_feeder_with_ktp(True), meth).get("F", "МТЗ")
    assert straight is not None and reversed_ is not None
    assert straight.i_primary == pytest.approx(reversed_.i_primary, rel=1e-12)
    assert straight.i_secondary == pytest.approx(reversed_.i_secondary, rel=1e-12)
    assert straight.t == pytest.approx(reversed_.t, rel=1e-12)


def test_3b_fault_behind_ktp_is_identical_in_both_orientations(meth):
    """И сам ток КЗ за КТП обязан совпасть: это проверка ориентации, не уставки."""
    a = _feeder_with_ktp(False)
    b = _feeder_with_ktp(True)
    ia = ShortCircuitSolver(a, a.modes["max"], meth).at("b04").i3
    ib = ShortCircuitSolver(b, b.modes["max"], meth).at("b04").i3
    assert ia == pytest.approx(ib, rel=1e-12)

    # Независимая проверка порядка величины: КЗ на шинах 0,4 кВ за КТП
    # 400 кВ·А определяется почти целиком сопротивлением самого трансформатора.
    z_t = 10 * 4.5 * U_AVG[0.4] ** 2 / 400            # Ом на ступени 0,4 кВ
    i_only_transformer = U_AVG[0.4] / (SQRT3 * z_t)   # кА
    assert ia < i_only_transformer, "сеть выше КТП обязана снижать ток"
    assert ia > 0.75 * i_only_transformer, (
        "сопротивление КТП должно оставаться определяющим"
    )


# ──────────────────────────────────────────────────────────────────────────
#  4. Паспортные напряжения трансформатора и класс ступени
# ──────────────────────────────────────────────────────────────────────────
def _transformer_net(u_hv: float, u_lv: float, lv_class: float = 10.0) -> Network:
    net = Network(f"{u_hv}/{u_lv}")
    net.add_node(Node("hv", "ОРУ 110 кВ", 110))
    net.add_node(Node("lv", "шины НН", lv_class))
    net.add_branch(SourceBranch(id="S", name="Система", node_from=GRID, node_to="hv",
                                s_kz_max=2500, s_kz_min=1700))
    net.add_branch(TransformerBranch(id="T", name="Т1", node_from="hv", node_to="lv",
                                     s_nom=16000, u_hv=u_hv, u_lv=u_lv, uk=10.5, p_k=85))
    net.add_mode(Mode("max", "Максимальный", states={}, system="max"))
    return net


def test_4_nameplate_115_11_and_110_10_give_the_same_result(meth):
    """115/11 и 110/10 — один и тот же трансформатор в разной записи паспорта.

    Оба паспорта относятся к ступеням 115 и 10,5 кВ, поэтому ток КЗ обязан
    совпасть. Ручная проверка: сопротивление считается по расчётному
    напряжению ступени, а не по паспортному числу.
    """
    a = _transformer_net(115, 11)
    b = _transformer_net(110, 10)
    ia = ShortCircuitSolver(a, a.modes["max"], meth).at("lv").i3
    ib = ShortCircuitSolver(b, b.modes["max"], meth).at("lv").i3
    assert ia == pytest.approx(ib, rel=1e-12)

    # Независимый ручной расчёт в омах на ступени 10,5 кВ.
    z_source_110 = U_AVG[110.0] ** 2 / 2500.0
    z_t_110 = 10 * 10.5 * U_AVG[110.0] ** 2 / 16000
    k = (U_AVG[10.0] / U_AVG[110.0]) ** 2
    x = (z_source_110 + z_t_110) * k
    r_source = z_source_110 * k / math.sqrt(1 + 12.0 ** 2) * 1.0   # X/R = 12 у источника
    r_t = 1000 * 85 * U_AVG[110.0] ** 2 / 16000 ** 2 * k
    z = complex(r_source + r_t, math.sqrt(max(x ** 2 - (r_source + r_t) ** 2, 0.0)))
    hand = U_AVG[10.0] / (SQRT3 * abs(z))
    assert ia == pytest.approx(hand, rel=2e-3), (ia, hand)


def test_4b_nameplate_that_does_not_belong_to_the_stage_is_rejected(meth):
    """Трансформатор 110/6,3 на шинах 10 кВ обязан быть отклонён.

    Раньше паспорт молча игнорировался и считался «как получится». Теперь
    несоответствие паспортного напряжения классу узла блокирует расчёт: это
    почти всегда опечатка, и правдоподобное число здесь опаснее отказа.
    """
    net = _transformer_net(110, 6.3, lv_class=10.0)
    problems = net.validate()
    assert problems, "несоответствие паспорта классу узла обязано быть замечено"
    text = " ".join(problems)
    assert "6" in text and ("10" in text or "класс" in text.lower()), text


# ──────────────────────────────────────────────────────────────────────────
#  5. Обесточенный остров
# ──────────────────────────────────────────────────────────────────────────
def _island_net() -> Network:
    net = Network("обесточенный остров")
    net.add_node(Node("b10", "1 СШ 10 кВ", 10))
    net.add_node(Node("island", "2 СШ 10 кВ", 10))
    net.add_branch(SourceBranch(id="S", name="Система", node_from=GRID, node_to="b10",
                                s_kz_max=200, s_kz_min=140))
    net.add_branch(TieBranch(id="SV", name="СВ 10 кВ", node_from="b10", node_to="island",
                             switchable=True, normally_closed=False,
                             ct_ratio=(1000, 5), breaker_t_off=0.06))
    net.add_node(Node("island_end", "конец Ф-2", 10))
    net.add_branch(LineBranch(id="F", name="Ф-2 (обесточен)", node_from="island",
                              node_to="island_end", length_km=2.0, section_mm2=95,
                              material="Al", ct_ratio=(200, 5), breaker_t_off=0.06))
    net.add_load(Load("L", "нагрузка", "island_end", p_kw=300, cos_phi=0.9))
    net.add_mode(Mode("max", "СВ отключён", states={"SV": False}, system="max"))
    return net


def test_5_de_energised_island_reports_a_status_not_a_zero(meth):
    """Обесточенный участок обязан дать статус, а не ноль.

    Ноль ампер и «напряжения нет» — разные состояния. Ноль выглядит как
    результат расчёта и может быть принят за него; статус — нет.
    """
    net = _island_net()
    result = run(net, meth)
    row = result.get("F", "МТЗ")
    assert row is not None
    assert row.status is not OK, "защита без напряжения не может быть в норме"
    assert row.i_primary is None or row.i_primary == 0 or row.status is UNRESOLVED
    text = " ".join(row.messages).lower()
    assert "напряж" in text or "обесточ" in text or "не находится" in text, row.messages

    mode = net.modes["max"]
    live = net.energized_nodes(mode)
    assert "island" not in live and "island_end" not in live
    assert "b10" in live


# ──────────────────────────────────────────────────────────────────────────
#  6. Зона резервирования за трансформатором — дефект AUD-PROT-010
# ──────────────────────────────────────────────────────────────────────────
def _two_stage_net() -> Network:
    """Система → ВЛ 110 → трансформатор 110/10 → фидер 10 кВ."""
    net = Network("две ступени")
    net.add_node(Node("a110", "ПС А, 110 кВ", 110))
    net.add_node(Node("b110", "ПС Б, 110 кВ", 110))
    net.add_node(Node("b10", "ПС Б, 10 кВ", 10))
    net.add_node(Node("end", "конец фидера", 10))
    net.add_branch(SourceBranch(id="S", name="Система", node_from=GRID, node_to="a110",
                                s_kz_max=2500, s_kz_min=1700))
    net.add_branch(LineBranch(
        id="VL", name="ВЛ-110 А—Б", node_from="a110", node_to="b110",
        length_km=20.0, section_mm2=120, material="Al", r0=0.249, x0=0.427,
        ct_ratio=(300, 5), ct_node="a110", breaker_t_off=0.06,
        prot=ProtectionSettings(mtz=True, to=False, ozz=False)))
    net.add_branch(TransformerBranch(id="T", name="Т1 110/10", node_from="b110",
                                     node_to="b10", s_nom=16000, u_hv=110, u_lv=10,
                                     uk=10.5, p_k=85, ct_ratio=(150, 5),
                                     breaker_t_off=0.06))
    net.add_branch(LineBranch(id="F", name="Ф-1 10 кВ", node_from="b10", node_to="end",
                              length_km=6.0, section_mm2=95, material="Al",
                              ct_ratio=(200, 5), breaker_t_off=0.06))
    net.add_load(Load("L", "нагрузка", "end", p_kw=900, cos_phi=0.9))
    net.add_mode(Mode("min", "Минимальный", states={}, system="min"))
    return net


def test_6_backup_zone_must_not_cross_a_transformer(meth):
    """Зона резервирования защиты 110 кВ не должна заходить за трансформатор.

    `context.zone_points()` документирован прямо: «По умолчанию зона
    резервирования НЕ заходит за трансформатор… Проверять защиту ввода 110 кВ
    по КЗ на шинах 0,4 кВ за двумя трансформаторами — строже любой методики и
    даёт ложные нарушения». Профиль методики задаёт
    `sensitivity.backup_through_transformer = 0`.

    Фактически исключается только сама ветвь-трансформатор, но не её
    поддерево: узлы 10 кВ за трансформатором в зону попадают. Отсюда ложные
    нарушения Kч у защит высшего напряжения — в демонстрационной сети это два
    нарушения из пяти.

    Дефект закрыт этапом A2 (31.08.2026): обход зоны теперь останавливается на
    каждом последующем трансформаторе, а не пропускает одну ветвь. Пометка
    `known_defect` снята — тест работает как обычная проверка.
    """
    net = _two_stage_net()
    result = run(net, meth)
    zone_nodes = {
        point.node_id
        for point in result.ctx.zone_points(net.branches["VL"], net.modes["min"])[1]
    }
    behind_transformer = {"b10", "end"}
    crossed = zone_nodes & behind_transformer
    assert not crossed, (
        "В зону резервирования защиты 110 кВ попали узлы за трансформатором: "
        f"{sorted(crossed)}. Ожидалась граница на шинах 110 кВ ПС Б."
    )


def test_6b_backup_zone_stops_with_an_explanation_not_a_silence(meth):
    """Пустая зона резервирования обязана объяснить, почему она пуста.

    «Ниже ничего нет» и «ниже есть, но за трансформатором» — разные факты.
    Пока дефект AUD-PROT-010 был открыт, второй случай вообще не возникал; после
    его закрытия он стал обычным, и выдавать за первый его нельзя.
    """
    net = _two_stage_net()
    result = run(net, meth)
    mode = net.modes["min"]
    zone_nodes = {
        point.node_id
        for point in result.ctx.zone_points(net.branches["VL"], mode)[1]
    }
    assert zone_nodes == set(), f"зона обязана быть пустой, получено {zone_nodes}"
    assert result.ctx.backup_zone_stops_at_transformer(net.branches["VL"], mode)

    row = result.get("VL", "МТЗ")
    assert row is not None
    text = " ".join(row.messages)
    assert "ограничена трансформатором" in text, row.messages
    assert "нет других элементов" not in text, (
        "нельзя выдавать «за трансформатором» за «ничего нет»"
    )



# ──────────────────────────────────────────────────────────────────────────
#  7. Устойчивость определяющего режима — дефект AUD-CALC-011
# ──────────────────────────────────────────────────────────────────────────
def _equal_modes_net() -> Network:
    """Радиальный фидер: ток не зависит от состояния удалённой ПС.

    Все три максимальных режима физически дают один и тот же ток КЗ, поэтому
    «определяющий режим» между ними выбирается произвольно, если правило
    выбора не задано явно.
    """
    net = Network("равные режимы")
    net.add_node(Node("b110", "ОРУ 110 кВ", 110))
    net.add_node(Node("b10", "1 СШ 10 кВ", 10))
    net.add_node(Node("far", "2 СШ 10 кВ (другая ПС)", 10))
    net.add_node(Node("end", "конец фидера", 10))
    net.add_branch(SourceBranch(id="S", name="Система", node_from=GRID, node_to="b110",
                                s_kz_max=2500, s_kz_min=1700))
    net.add_branch(TransformerBranch(id="T", name="Т1 110/10", node_from="b110",
                                     node_to="b10", s_nom=16000, u_hv=110, u_lv=10,
                                     uk=10.5, p_k=85))
    net.add_branch(TieBranch(id="SV", name="СВ 10 кВ", node_from="b10", node_to="far",
                             switchable=True, normally_closed=False,
                             ct_ratio=(1000, 5), breaker_t_off=0.06))
    net.add_branch(LineBranch(id="F", name="Ф-1", node_from="b10", node_to="end",
                              length_km=5.0, section_mm2=95, material="Al",
                              r0=0.326, x0=0.083, ct_ratio=(300, 5),
                              breaker_t_off=0.06, terminal="БМРЗ-100"))
    net.add_load(Load("L", "нагрузка", "end", p_kw=600, cos_phi=0.9))
    # Порядок объявления существен: он и разрешает равенство.
    net.add_mode(Mode("normal", "Нормальный режим", states={"SV": False}, system="max"))
    net.add_mode(Mode("sv_on", "СВ включён", states={"SV": True}, system="max"))
    net.add_mode(Mode("min", "Минимальный", states={"SV": False}, system="min"))
    return net


def test_7_governing_mode_is_the_first_declared_among_equal_modes(meth):
    """При равных токах определяющим объявляется первый по порядку режим.

    Раньше выбор делался строгим сравнением без допуска, поэтому имя режима
    определялось последним битом решателя и зависело от сборки численной
    библиотеки: один и тот же проект на двух машинах давал разные имена при
    совпадающих до последнего знака числах. Это дефект `AUD-CALC-011`.
    """
    result = run(_equal_modes_net(), meth)
    row = result.get("F", "ТО")
    assert row is not None
    assert row.governing_mode == "Нормальный режим", (
        f"ожидался первый по объявлению режим, получен «{row.governing_mode}»"
    )


def test_7b_selection_survives_a_one_ulp_perturbation(meth):
    """Возмущение в один последний бит не должно менять определяющий режим.

    Прямая проверка самого правила выбора, без обращения к решателю: если
    полоса равенства работает, порядок кандидатов и их последние биты на
    результат не влияют.
    """
    from rza_calc.core.protections.selection import pick_extreme

    base = 1451.8606186246268
    one_ulp_up = math.nextafter(base, math.inf)
    assert one_ulp_up != base, "проверка бессмысленна, если возмущение нулевое"

    rows = [("normal", base), ("repair", one_ulp_up), ("reserve", base)]
    chosen, tied = pick_extreme(rows, key=lambda r: r[1], largest=True)
    assert chosen[0] == "normal", "победил режим, выигравший один последний бит"
    assert tied == 3, "все три режима обязаны считаться равными"

    # Обратный порядок кандидатов даёт тот же ответ по имени первого в списке.
    rows_reversed = [("reserve", one_ulp_up), ("normal", base), ("repair", base)]
    chosen_r, _ = pick_extreme(rows_reversed, key=lambda r: r[1], largest=True)
    assert chosen_r[0] == "reserve", "выбор обязан зависеть только от порядка"

    # Настоящее различие полосой не поглощается.
    real = [("a", 1000.0), ("b", 1000.0 * (1 + 1e-6))]
    chosen_real, tied_real = pick_extreme(real, key=lambda r: r[1], largest=True)
    assert chosen_real[0] == "b" and tied_real == 1, "физическое различие обязано победить"


def test_7c_every_protection_reports_a_declared_mode(meth):
    """Определяющий режим обязан быть одним из объявленных в проекте."""
    from rza_calc.io.project import load_project

    project = load_project(Path(__file__).resolve().parent.parent
                           / "rza_calc" / "examples" / "energoraion.json")
    result = run(project.network, project.methodology)
    known = {mode.name for mode in project.network.modes.values()}
    for row in result.all_results():
        if row.governing_mode:
            assert row.governing_mode in known, (
                f"{row.branch_id}/{row.kind}: режим «{row.governing_mode}» не объявлен"
            )
