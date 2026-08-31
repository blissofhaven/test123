# -*- coding: utf-8 -*-
"""
Верификация ядра.

Главная идея тестов: результаты ядра сверяются с НЕЗАВИСИМЫМ расчётом,
записанным здесь же формулами, а не с «тем, что программа выдала вчера».
Иначе тест закрепляет ошибку вместо того, чтобы её ловить.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.core.engine import run
from rza_calc.core.impedance import line_impedance, source_impedance, transformer_impedance
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (GRID, LineBranch, Load, Mode, Network, Node,
                                 SourceBranch, TieBranch, TransformerBranch)
from rza_calc.core.result import FAIL, OK
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.io.project import load, save

SQRT3 = math.sqrt(3.0)
EX = Path(__file__).resolve().parent.parent / "rza_calc" / "examples" / "ps_severnaya.json"


@pytest.fixture
def meth():
    return Methodology.load()


# ── сопротивления: сверка с формулами вручную ────────────────────────────
def test_source_impedance(meth):
    br = SourceBranch(id="S", name="С", node_from=GRID, node_to="b", s_kz_max=400)
    z, _ = source_impedance(br, meth, 35, "max")
    assert z.real == 0
    assert abs(z.imag - 37.0 ** 2 / 400) < 1e-9


def test_source_from_current_matches_power(meth):
    """Задание через Sкз и через Iкз должно давать одно и то же."""
    i_kz = 400 / (SQRT3 * 37.0)
    a, _ = source_impedance(SourceBranch(id="1", name="a", node_from=GRID, node_to="b",
                                         s_kz_max=400), meth, 35, "max")
    b, _ = source_impedance(SourceBranch(id="2", name="b", node_from=GRID, node_to="b",
                                         i_kz_max=i_kz), meth, 35, "max")
    assert abs(abs(a) - abs(b)) < 1e-9


def test_transformer_impedance(meth):
    br = TransformerBranch(id="T", name="Т", node_from="a", node_to="b",
                           s_nom=10000, u_hv=35, u_lv=10, uk=7.5, p_k=60)
    z, _ = transformer_impedance(br, meth)
    z_exp = 10 * 7.5 * 35.0 ** 2 / 10000
    r_exp = 1000 * 60 * 35.0 ** 2 / 10000 ** 2
    assert abs(abs(z) - z_exp) < 1e-9
    assert abs(z.real - r_exp) < 1e-9
    # проверка через определение Uк: Zт = Uк · Uном / (√3 · Iном)
    i_nom = 10000 / (SQRT3 * 35.0)
    assert abs(abs(z) - 0.075 * 35000 / (SQRT3 * i_nom)) < 1e-6


def test_transformer_inconsistent_data_raises(meth):
    br = TransformerBranch(id="T", name="Т", node_from="a", node_to="b",
                           s_nom=1000, u_hv=10, u_lv=0.4, uk=0.5, p_k=200)
    with pytest.raises(ValueError):
        transformer_impedance(br, meth)


def test_line_impedance_and_temperature(meth):
    br = LineBranch(id="L", name="Л", node_from="a", node_to="b",
                    length_km=3.2, section_mm2=150, material="Al")
    z_max, _ = line_impedance(br, meth, "max")
    assert abs(z_max.real - 31.5 / 150 * 3.2) < 1e-9
    z_min, _ = line_impedance(br, meth, "min")
    kt = meth.k("short_circuit.temp_factor_min")
    assert abs(z_min.real - z_max.real * kt) < 1e-9
    assert abs(z_min.imag - z_max.imag) < 1e-12   # реактивное от нагрева не меняется


def test_parallel_lines_halve_impedance(meth):
    a = LineBranch(id="1", name="1", node_from="a", node_to="b",
                   length_km=2, section_mm2=95, material="Al")
    b = LineBranch(id="2", name="2", node_from="a", node_to="b",
                   length_km=2, section_mm2=95, material="Al", n_parallel=2)
    za, _ = line_impedance(a, meth, "max")
    zb, _ = line_impedance(b, meth, "max")
    assert abs(za / 2 - zb) < 1e-12


# ── токи КЗ ──────────────────────────────────────────────────────────────
def _two_transformer_net():
    net = Network("тест")
    for n in (Node("b35", "ОРУ 35", 35), Node("b1", "1 СШ", 10), Node("b2", "2 СШ", 10),
              Node("end", "конец Ф-1", 10)):
        net.add_node(n)
    net.add_branch(SourceBranch(id="S", name="С", node_from=GRID, node_to="b35",
                                s_kz_max=400, s_kz_min=250))
    for i, dst in ((1, "b1"), (2, "b2")):
        net.add_branch(TransformerBranch(id=f"T{i}", name=f"Т{i}", node_from="b35", node_to=dst,
                                         s_nom=10000, u_hv=35, u_lv=10, uk=7.5, p_k=60))
    net.add_branch(TieBranch(id="SV", name="СВ", node_from="b1", node_to="b2"))
    net.add_branch(LineBranch(id="F1", name="Ф-1", node_from="b1", node_to="end",
                              length_km=3.2, section_mm2=150, material="Al", ct_ratio=(400, 5)))
    net.add_load(Load("L", "нагр", "end", p_kw=2500, cos_phi=0.9))
    return net


# Весь ручной расчёт ведётся в классическом базисе средних напряжений:
# ступень 35 кВ — это 37 кВ, ступень 10 кВ — 10,5 кВ. Тем же базисом
# пользуется и программа, поэтому сверка проверяет расчёт, а не совпадение
# двух одинаковых допущений.
U_AVG_35, U_AVG_10 = 37.0, 10.5


def _hand_i3(z_ohm_at_35, u_prefault=U_AVG_10):
    # Приведение между ступенями — по отношению средних напряжений.
    z = z_ohm_at_35 * (U_AVG_10 / U_AVG_35) ** 2
    return u_prefault / (SQRT3 * abs(z))


def test_sc_on_hv_bus(meth):
    net = _two_transformer_net()
    mode = net.add_mode(Mode("m", "норм", states={"SV": False}))
    s = ShortCircuitSolver(net, mode, meth)
    assert abs(s.at("b35").i3 - 37.0 / (SQRT3 * 37.0 ** 2 / 400)) < 1e-9


def test_sc_single_transformer(meth):
    net = _two_transformer_net()
    mode = net.add_mode(Mode("m", "СВ откл", states={"SV": False}))
    s = ShortCircuitSolver(net, mode, meth)
    zc = complex(0, U_AVG_35 ** 2 / 400)
    zt, _ = transformer_impedance(net.branches["T1"], meth, U_AVG_35)
    assert abs(s.at("b1").i3 - _hand_i3(zc + zt)) < 1e-9


def test_sc_parallel_transformers_via_tie(meth):
    """Ключевая проверка: включённый СВ должен давать параллельную работу Т1 и Т2."""
    net = _two_transformer_net()
    mode = net.add_mode(Mode("m", "СВ вкл", states={"SV": True}))
    s = ShortCircuitSolver(net, mode, meth)
    zc = complex(0, U_AVG_35 ** 2 / 400)
    zt, _ = transformer_impedance(net.branches["T1"], meth, U_AVG_35)
    assert abs(s.at("b1").i3 - _hand_i3(zc + zt / 2)) < 1e-9
    # обе секции объединены — токи равны
    assert abs(s.at("b1").i3 - s.at("b2").i3) < 1e-12
    # и ток заметно больше, чем при разделённых секциях
    s_split = ShortCircuitSolver(net, Mode("x", "откл", states={"SV": False}), meth)
    assert s.at("b1").i3 > s_split.at("b1").i3 * 1.5


def test_sc_along_line(meth):
    net = _two_transformer_net()
    mode = net.add_mode(Mode("m", "норм", states={"SV": False}))
    s = ShortCircuitSolver(net, mode, meth)
    zc = complex(0, U_AVG_35 ** 2 / 400)
    zt, _ = transformer_impedance(net.branches["T1"], meth, U_AVG_35)
    zl, _ = line_impedance(net.branches["F1"], meth, "max")
    z10 = (zc + zt) * (U_AVG_10 / U_AVG_35) ** 2 + zl
    assert abs(s.at("end").i3 - U_AVG_10 / (SQRT3 * abs(z10))) < 1e-9
    assert s.at("end").i3 < s.at("b1").i3       # ток вдоль линии падает


def test_min_regime_lower_than_max(meth):
    net = _two_transformer_net()
    a = ShortCircuitSolver(net, Mode("a", "max", states={"SV": False}, system="max"), meth)
    b = ShortCircuitSolver(net, Mode("b", "min", states={"SV": False}, system="min"), meth)
    assert b.at("b1").i3 < a.at("b1").i3
    assert b.at("end").i3 < a.at("end").i3


def test_two_phase_is_explicit_approximation(meth):
    net = _two_transformer_net()
    s = ShortCircuitSolver(net, Mode("m", "н", states={"SV": False}), meth)
    r = s.at("b1")
    assert abs(r.i2 / r.i3 - SQRT3 / 2.0) < 1e-12
    assert r.i2_is_approximation
    assert "Z2 = Z1" in r.i2_note


def test_deenergized_node_raises(meth):
    net = _two_transformer_net()
    mode = Mode("m", "Т1 откл", states={"T1": False, "SV": False})
    s = ShortCircuitSolver(net, mode, meth)
    with pytest.raises(KeyError):
        s.at("b1")


# ── топология ────────────────────────────────────────────────────────────
def test_orientation_reverses_when_fed_backwards():
    net = _two_transformer_net()
    net.add_mode(Mode("m", "Т1 откл, СВ вкл", states={"T1": False, "SV": True}))
    mode = net.modes["m"]
    sv = net.branches["SV"]
    src, load_end, ring = net.orient(sv, mode)
    assert (src, load_end) == ("b2", "b1")      # питание идёт справа налево
    assert not ring
    assert "b1" in net.downstream_nodes(sv, mode)
    assert "end" in net.downstream_nodes(sv, mode)


def test_no_zone_when_ring():
    net = _two_transformer_net()
    mode = net.add_mode(Mode("m", "оба Т, СВ вкл", states={"T1": True, "T2": True, "SV": True}))
    # при включённом СВ и двух трансформаторах Т1 работает параллельно — зоны нет
    assert net.downstream_nodes(net.branches["T1"], mode) == set()


def test_downstream_loads_sum():
    net = _two_transformer_net()
    mode = net.add_mode(Mode("m", "н", states={"SV": False}))
    loads = net.downstream_loads(net.branches["F1"], mode)
    assert [l.id for l in loads] == ["L"]


# ── сквозной прогон примера ──────────────────────────────────────────────
def test_example_project_runs():
    net, meth, _ = load(EX)
    assert net.validate() == []
    pr = run(net, meth)
    assert len(pr.ctx.solvers) == len(net.modes)
    assert pr.results


def test_example_mtz_setting_matches_hand_calc():
    net, meth, _ = load(EX)
    pr = run(net, meth)
    r = pr.get("F1", "МТЗ")
    # ручной пересчёт
    s = 1800 / 0.90 + 700 / 0.90
    i_rab = s / (SQRT3 * 10)
    i_calc = meth.k("mtz.k_ots") * meth.k("mtz.k_szp") / meth.k("mtz.k_v") * i_rab
    assert abs(r.i_calc - i_calc) < 1e-6
    assert r.i_secondary in meth.ct_scale()
    assert r.i_primary >= r.i_calc                      # округление только вверх
    assert abs(r.i_primary - r.i_secondary * 400 / 5) < 1e-9


def test_example_selectivity_chain():
    """КТП → фидер → ввод → трансформатор: ступень выдержана во всех режимах."""
    net, meth, _ = load(EX)
    pr = run(net, meth)
    dt = meth.k("selectivity.dt")
    assert pr.get("TK1", "МТЗ").t == 0.5               # задано вручную — не изменено
    assert abs(pr.get("F1", "МТЗ").t - 0.8) < 1e-9
    assert abs(pr.get("V1", "МТЗ").t - 1.1) < 1e-9
    assert abs(pr.get("T1", "МТЗ").t - 1.4) < 1e-9
    for p in pr.pairs:
        assert p.dt is None or p.dt >= dt - 1e-9, p.line()


def test_selectivity_violation_is_detected():
    """Если принудительно занизить выдержку ввода — программа обязана это увидеть."""
    from rza_calc.core import selectivity as sel
    net, meth, _ = load(EX)
    net.branches["V1"].prot.t_mtz = 0.9               # вместо 1.1
    pr = run(net, meth)
    bad = sel.violations(pr.pairs)
    assert bad, "нарушение селективности не обнаружено"
    assert any(p.lower_id == "F1" and p.upper_id == "V1" for p in bad)


def test_settings_are_checked_in_all_modes():
    """Уставка должна выбираться по наихудшему режиму, а не по первому попавшемуся."""
    net, meth, _ = load(EX)
    pr = run(net, meth)
    r = pr.get("F3", "МТЗ")
    kch = [c for c in r.checks if "основной" in c.name][0]
    assert "Минимальный" in kch.comment or "минимальный" in kch.comment


def test_no_ct_means_no_settings():
    net, meth, _ = load(EX)
    net.branches["F2"].ct_ratio = None
    pr = run(net, meth)
    assert "F2" not in pr.results


def test_roundtrip_save_load(tmp_path):
    net, meth, meta = load(EX)
    out = tmp_path / "p.json"
    save(out, net, meta)
    net2, _, _ = load(out)
    assert set(net2.branches) == set(net.branches)
    assert set(net2.nodes) == set(net.nodes)
    assert net2.neutral == net.neutral
    pr1, pr2 = run(net, meth), run(net2, meth)
    for bid in pr1.results:
        for kind in pr1.results[bid]:
            a, b = pr1.get(bid, kind), pr2.get(bid, kind)
            assert (a.i_primary, a.t) == (b.i_primary, b.t)


def test_unknown_field_rejected(tmp_path):
    import json
    raw = json.loads(EX.read_text(encoding="utf-8"))
    raw["electrical_model"]["electrical_nodes"][0]["u_nomm"] = 35
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)


# ── протокол расчёта ─────────────────────────────────────────────────────
def test_every_number_has_a_protocol():
    """Ни одна принятая уставка не должна появляться без объяснения."""
    net, meth, _ = load(EX)
    pr = run(net, meth)
    for r in pr.all_results():
        if r.i_primary is None:
            continue
        assert r.steps, f"{r.branch_name}/{r.kind}: нет протокола"
        text = r.explain()
        for word in ("ЧТО СЧИТАЕМ", "ФОРМУЛА", "ПОДСТАНОВКА", "РЕЗУЛЬТАТ"):
            assert word in text, f"{r.branch_name}/{r.kind}: в протоколе нет раздела {word}"


def test_placeholder_methodology_is_flagged():
    """Профиль-заглушка обязан дать видимое предупреждение.

    Профиль по умолчанию перестал быть заглушкой на этапе A2, поэтому
    заглушка здесь строится явно: проверяется механизм, а не то, каким
    оказался профиль в поставке. Иначе тест тихо перестал бы что-либо
    проверять ровно в тот момент, когда профиль заменили.
    """
    net, meth, _ = load(EX)
    meth.data["status"] = "ЗАГЛУШКА"
    pr = run(net, meth)
    assert any("ЗАГЛУШКА" in w for w in pr.warnings)


def test_shipped_profile_is_no_longer_a_placeholder():
    """Профиль в поставке заполнен и заявляет статус, который подтверждён.

    Парный тест к предыдущему: механизм заглушки жив, но пользователь
    получает не заглушку.
    """
    net, meth, _ = load(EX)
    assert meth.status == "ТИПОВОЙ"
    assert meth.status_claim_errors() == []
    pr = run(net, meth)
    assert not any("ЗАГЛУШКА" in w for w in pr.warnings)
    assert any("не утверждён для конкретного объекта" in w for w in pr.warnings), (
        "типовой профиль обязан прямо сообщать, что он не утверждён: без этого "
        "исчезновение слова ЗАГЛУШКА выглядело бы как согласованная методика"
    )


def test_methodology_missing_key_raises(meth):
    from rza_calc.core.methodology import MethodologyError
    with pytest.raises(MethodologyError):
        meth.k("mtz.nonexistent")


# ── генераторы и трёхобмоточные трансформаторы ───────────────────────────
GTES = Path(__file__).resolve().parent.parent / "rza_calc" / "examples" / "gtes_sever.json"


def test_generator_impedance(meth):
    from rza_calc.core.impedance import generator_impedance
    from rza_calc.core.model import GeneratorBranch
    g = GeneratorBranch(id="G", name="ГТГ", node_from=GRID, node_to="b",
                        p_nom=14.0, cos_phi=0.8, u_nom=10, xd2=0.15)
    z, _ = generator_impedance(g, meth)
    s = 14.0 * 1000 / 0.8
    assert abs(abs(z) - 1000 * 0.15 * 10.0 ** 2 / s) < 1e-9
    # Сопротивление вычислено по паспортным 10 кВ; ток может использовать
    # отдельное предаварийное напряжение 10,5 кВ.
    i_sc = 10.5 * 1000 / (SQRT3 * abs(z))
    i_hand = 10.5 * s / (SQRT3 * 0.15 * 10.0 ** 2)
    assert abs(i_sc - i_hand) < 1e-6


def test_three_winding_star_is_exact():
    from rza_calc.core.model import Transformer3W
    t = Transformer3W(id="T", name="Т", node_hv="a", node_mv="b", node_lv="c",
                      s_nom=25000, u_hv=110, u_mv=35, u_lv=10,
                      uk_hm=10.5, uk_hl=17.0, uk_ml=6.0)
    h, mm, l = t.leg_uk()
    assert abs(h + mm - 10.5) < 1e-12
    assert abs(h + l - 17.0) < 1e-12
    assert abs(mm + l - 6.0) < 1e-12
    assert mm < 0                       # средний луч отрицательный — это нормально


def test_gtes_example_runs():
    net, meth, _ = load(GTES)
    assert net.validate() == []
    pr = run(net, meth)
    assert not pr.ctx.errors
    assert len(pr.ctx.solvers) == len(net.modes)
    assert len(pr.all_results()) > 40


def test_sc_scales_with_generating_fleet():
    """Меньше машин в работе — меньше ток КЗ. На всех ступенях."""
    net, meth, _ = load(GTES)
    pr = run(net, meth)
    a = pr.ctx.solvers["max"]
    b = pr.ctx.solvers["min"]
    c = pr.ctx.solvers["one"]
    for node in ("b220", "b110", "vost35", "vost10_1"):
        assert a.at(node).i3 > b.at(node).i3 > c.at(node).i3, node


def test_gtes_hand_check_220kv(meth):
    """Сверка тока КЗ на 220 кВ с ручным расчётом по четырём машинам."""
    net, _, _ = load(GTES)
    pr = run(net, meth)
    # Классический базис: ступень 220 кВ — 230 кВ, ступень 10 кВ — 10,5 кВ.
    # И машины, и блочные трансформаторы считаются на среднем напряжении своей
    # ступени, приведение к 220 кВ — по отношению средних напряжений.
    u_avg_220, u_avg_10 = 230.0, 10.5
    k = (u_avg_220 / u_avg_10) ** 2
    z = []
    for gid, s_gen, s_block, uk in (("G1", 17500, 25000, 11.5), ("G2", 17500, 25000, 11.5),
                                    ("G3", 7500, 10000, 10.5), ("G4", 7500, 10000, 10.5)):
        xg = 1000 * 0.15 * u_avg_10 ** 2 / s_gen
        xb = 10 * uk * u_avg_10 ** 2 / s_block
        z.append((xg + xb) * k)
    z_eq = 1 / sum(1 / x for x in z)
    i_hand = u_avg_220 / (SQRT3 * z_eq)
    assert abs(pr.ctx.solvers["max"].at("b220").i3 - i_hand) < 1e-6


def test_all_generators_off_is_an_error():
    net, meth, _ = load(GTES)
    net.add_mode(Mode("dead", "все ГТГ отключены",
                      states={"G1": False, "G2": False, "G3": False, "G4": False}))
    pr = run(net, meth)
    assert "dead" in pr.ctx.errors


def test_backup_zone_stops_at_transformer():
    """По умолчанию зона резервирования не заходит за трансформатор."""
    net, meth, _ = load(EX)
    pr = run(net, meth)
    r = pr.get("T1", "МТЗ")
    for c in r.checks:
        assert "0,4 кВ" not in (c.comment or ""), c.line()
    # а с включённым флагом — заходит
    net2, meth2, _ = load(EX)
    net2.branches["T1"].prot.backup_through_transformer = True
    pr2 = run(net2, meth2)
    r2 = pr2.get("T1", "МТЗ")
    assert any("0,4 кВ" in (c.comment or "") for c in r2.checks)


def test_switch_with_follows_parent():
    """Обмотки трёхобмоточного отключаются вместе с ним."""
    net, meth, _ = load(GTES)
    mode = net.modes["t1off"]
    assert not mode.is_closed(net.branches["VT1"])
    assert not mode.is_closed(net.branches["VT1_mv"])
    assert not mode.is_closed(net.branches["VT1_lv"])
    assert mode.is_closed(net.branches["VT2_lv"])
