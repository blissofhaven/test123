# -*- coding: utf-8 -*-
"""ПС 110/10 кВ «Промышленная» — образцовая схема с полной ошиновкой.

Отличие от «Энергорайона»: здесь у каждого силового элемента есть свои
выключатели — с ОБЕИХ сторон трансформаторов, на вводах 10 кВ, на секционном
и на каждом отходящем фидере. Именно так подстанция выглядит на проектной
однолинейке, и именно так её можно выводить в ремонт по частям.

Выключатель здесь — настоящий электрический объект (ветвь нулевого
сопротивления с положением), а не картинка: его можно отключить в режиме, и
схема при этом действительно меняется. Рисовать выключатели, которые ничего
не коммутируют, было бы враньём на чертеже.

Запускать из корня проекта:

    python tools_build_substation_demo.py
    python tools_build_substation_demo.py --layout

Параметры оборудования правдоподобны и взяты из типовых справочных рядов, но
к реальному объекту не относятся.
"""
from __future__ import annotations

import json
from pathlib import Path

from rza_calc.io.project import load_project, save_project

V1_PATH = Path("rza_calc/examples/ps_promyshlennaya_v1.json")
V7_PATH = Path("rza_calc/examples/ps_promyshlennaya.json")

PROT_FULL = {"mtz": True, "to": True, "ozz": True}
PROT_LINE = {"mtz": True, "to": True, "ozz": False}
PROT_MTZ = {"mtz": True, "to": False, "ozz": False}
PROT_NONE = {"mtz": False, "to": False, "ozz": False}
#  Короткий кабельный фидер у мощных шин: ток КЗ в конце линии почти равен
#  току на шинах, зоны у отсечки нет — ТО в проекте не применяется.
PROT_SHORT_CABLE = {"mtz": True, "to": False, "ozz": True}

OVERHEAD = {(110, 120): (0.249, 0.427), (10, 70): (0.428, 0.375),
            (10, 95): (0.315, 0.363)}
CABLE = {120: (0.258, 0.081, 0.90), 150: (0.206, 0.079, 1.00),
         240: (0.129, 0.075, 1.30)}


def node(nid, name, u_nom, kind=None):
    row = {"id": nid, "name": name, "u_nom": u_nom}
    if kind:
        row["kind"] = kind
    return row


def source(nid, name, to, s_max, s_min):
    return {"id": nid, "kind": "source", "name": name, "node_from": "GRID",
            "node_to": to, "s_kz_max": s_max, "s_kz_min": s_min,
            "input_mode_max": "power", "input_mode_min": "power",
            "x_r_ratio": 12.0}


def breaker(nid, name, a, b, closed=True, ct=600):
    """Выключатель: коммутирующая ветвь без собственного сопротивления.

    Защита на самом выключателе не рассчитывается — она принадлежит
    присоединению, в рассечку которого он включён, и стоит на его ТТ.
    Иначе в таблице уставок появились бы строки-двойники.
    """
    return {"id": nid, "kind": "tie", "name": name, "node_from": a, "node_to": b,
            "ct_ratio": [ct, 5], "breaker_t_off": 0.06, "terminal": "БМРЗ-100",
            "switchable": True, "normally_closed": closed,
            "prot": dict(PROT_NONE)}


def transformer(nid, name, hv, lv, s_nom, u_hv, u_lv, uk, p_k, ct, t_mtz=None):
    row = {"id": nid, "kind": "transformer", "name": name,
            "node_from": hv, "node_to": lv, "s_nom": s_nom,
            "u_hv": u_hv, "u_lv": u_lv, "uk": uk, "p_k": p_k, "group": "Д/Ун-11",
            "i_inrush_ratio": 5.0, "ct_ratio": [ct, 5],
            "breaker_t_off": 0.06, "terminal": "Бреслер ТОР-300",
            "switchable": False, "normally_closed": True,
            "prot": dict(PROT_MTZ)}
    if t_mtz is not None:
        row["prot"]["t_mtz"] = t_mtz
    return row


def cable(nid, name, a, b, km, section, ct, prot=None, t_mtz=None):
    r0, x0, ic = CABLE[section]
    row = {"id": nid, "kind": "line", "name": name, "node_from": a, "node_to": b,
            "line_type": "cable", "length_km": km, "brand": "АПвПу-10",
            "section_mm2": section, "material": "Al",
            "r0": r0, "x0": x0, "ic_per_km": ic,
            "ct_ratio": [ct, 5], "breaker_t_off": 0.06, "terminal": "БМРЗ-100",
            "switchable": False, "normally_closed": True,
            "prot": dict(prot or PROT_FULL)}
    if t_mtz is not None:
        row["prot"]["t_mtz"] = t_mtz
    return row


def overhead(nid, name, a, b, km, u_class, section, ct, prot=None, t_mtz=None):
    r0, x0 = OVERHEAD[(u_class, section)]
    row = {"id": nid, "kind": "line", "name": name, "node_from": a, "node_to": b,
           "line_type": "overhead", "length_km": km, "brand": f"АС-{section}",
           "section_mm2": section, "material": "Al", "r0": r0, "x0": x0,
           "ct_ratio": [ct, 5], "breaker_t_off": 0.06,
           "terminal": "Бреслер ТОР-300", "switchable": False,
           "normally_closed": True, "prot": dict(prot or PROT_LINE)}
    if u_class == 10:
        row["ic_per_km"] = 0.02
    if t_mtz is not None:
        row["prot"]["t_mtz"] = t_mtz
    return row


def load(lid, name, node_id, p_kw, cos_phi=0.9):
    return {"id": lid, "name": name, "node": node_id, "p_kw": p_kw,
            "cos_phi": cos_phi, "k_use": 1.0}


nodes: list[dict] = []
branches: list[dict] = []
loads: list[dict] = []

# ── Питание и ОРУ 110 кВ ──────────────────────────────────────────────────
nodes += [
    node("sys110", "Система 110 кВ", 110, "point"),
    node("ou110", "ОРУ 110 кВ", 110),
    node("t1_hv", "Вывод ВН Т1", 110, "point"),
    node("t2_hv", "Вывод ВН Т2", 110, "point"),
]
branches += [
    source("SYS", "Система 110 кВ", "sys110", 2500e3, 1400e3),
    #  ТО на питающей ВЛ-110 не применяется: её зона мгновенного действия
    #  должна была бы доходить до шин 10 кВ за трансформатором, а между
    #  линией и трансформатором стоит выключатель — отдельный объект, за
    #  который поиск трансформатора не заглядывает (дефект AUD-PROT-012).
    #  Ставить ТО, отстроенную от КЗ на своих же шинах 110 кВ, бессмысленно:
    #  она не сработает никогда. Защита линии — МТЗ.
    overhead("VL110", "ВЛ-110 питающая", "sys110", "ou110", 12.0, 110, 120, 300,
             prot=PROT_MTZ, t_mtz=1.3),
    #  Выключатели ВН обоих трансформаторов
    breaker("Q1", "Q1 · выключатель ВН Т1", "ou110", "t1_hv", True, 200),
    breaker("Q2", "Q2 · выключатель ВН Т2", "ou110", "t2_hv", True, 200),
]

# ── Трансформаторы и вводы 10 кВ ──────────────────────────────────────────
nodes += [
    node("t1_lv", "Вывод НН Т1", 10, "point"),
    node("t2_lv", "Вывод НН Т2", 10, "point"),
    node("sh1", "1 СШ 10 кВ", 10),
    node("sh2", "2 СШ 10 кВ", 10),
]
branches += [
    transformer("T1", "Т1 · 16 МВ·А 110/10", "t1_hv", "t1_lv",
                16000, 115.0, 11.0, 10.5, 85.0, 100, t_mtz=0.9),
    transformer("T2", "Т2 · 16 МВ·А 110/10", "t2_hv", "t2_lv",
                16000, 115.0, 11.0, 10.5, 85.0, 100, t_mtz=0.9),
    #  Выключатели НН тех же трансформаторов — они же вводы 10 кВ
    breaker("Q3", "Q3 · ввод-1 10 кВ", "t1_lv", "sh1", True, 1000),
    breaker("Q4", "Q4 · ввод-2 10 кВ", "t2_lv", "sh2", True, 1000),
    #  Секционный: в нормальном режиме отключён
    breaker("QB", "QB · секционный 10 кВ", "sh1", "sh2", False, 1000),
]

# ── Восемь фидеров 10 кВ, у каждого свой выключатель ──────────────────────
FEEDERS = [
    # (номер, секция, вид, длина км, сечение, ТТ, мощность кВт, имя, защиты)
    (1, "sh1", "cable", 1.1, 120, 400, 1800, "Ф-1 · КЛ на РП-1", "short"),
    (2, "sh1", "cable", 5.2, 120, 400, 1250, "Ф-2 · КЛ на ЦРП", None),
    (3, "sh1", "cable", 6.4, 120, 300, 1100, "Ф-3 · КЛ на ЦТП-4", None),
    (4, "sh1", "overhead", 4.2, 95, 300, 780, "Ф-4 · ВЛ на насосную", None),
    (5, "sh2", "cable", 1.3, 240, 400, 2400, "Ф-5 · КЛ на компрессорную", "short"),
    (6, "sh2", "cable", 5.6, 120, 400, 1600, "Ф-6 · КЛ на РП-2", None),
    (7, "sh2", "cable", 6.8, 120, 300, 1050, "Ф-7 · КЛ на очистные", None),
    (8, "sh2", "overhead", 4.6, 95, 300, 690, "Ф-8 · ВЛ на карьер", None),
]
for number, section, kind, km, mm2, ct, power, title, style in FEEDERS:
    cell, end = f"f{number}_cell", f"f{number}_end"
    nodes += [
        node(cell, f"Ячейка Ф-{number}", 10, "point"),
        node(end, f"Конец {title.split(' · ')[0]}", 10, "point"),
    ]
    branches.append(breaker(f"QF{number}", f"QF{number} · выключатель Ф-{number}",
                            section, cell, True, ct))
    if kind == "cable":
        branches.append(cable(f"F{number}", title, cell, end, km, mm2, ct,
                              prot=PROT_SHORT_CABLE if style == "short" else None,
                              t_mtz=0.5))
    else:
        branches.append(overhead(f"F{number}", title, cell, end, km, 10, mm2, ct,
                                 prot=PROT_LINE, t_mtz=0.5))
    loads.append(load(f"L{number}", f"Нагрузка {title.split(' · ')[0]}", end, power))


def states(**flags):
    return {key: bool(value) for key, value in flags.items()}


modes = [
    {"id": "normal", "name": "Нормальный режим", "system": "max",
     "description": "Оба трансформатора в работе, секционный выключатель "
                    "отключён, секции работают раздельно.",
     "states": states()},
    {"id": "minimum", "name": "Минимальный режим системы", "system": "min",
     "description": "Та же схема при наименьшей мощности питающей системы. "
                    "Используется для проверки чувствительности защит.",
     "states": states()},
    {"id": "t1_repair", "name": "Ремонт Т1, СВ включён", "system": "max",
     "description": "Т1 выведен выключателями с обеих сторон; обе секции "
                    "10 кВ питаются от Т2 через секционный выключатель.",
     "states": states(Q1=False, Q3=False, QB=True)},
    {"id": "t2_repair", "name": "Ремонт Т2, СВ включён", "system": "max",
     "description": "Т2 выведен выключателями с обеих сторон; обе секции "
                    "10 кВ питаются от Т1 через секционный выключатель.",
     "states": states(Q2=False, Q4=False, QB=True)},
    {"id": "t1_repair_min", "name": "Ремонт Т1 при минимуме системы",
     "system": "min",
     "description": "Самый тяжёлый режим для чувствительности: одна питающая "
                    "цепь и наименьшая мощность системы.",
     "states": states(Q1=False, Q3=False, QB=True)},
]

project = {
    "format_version": 1,
    "project": {
        "name": "ПС 110/10 кВ «Промышленная» — образцовая схема",
        "note": "Два трансформатора 16 МВ·А, две секции 10 кВ, восемь фидеров. "
                "Выключатели стоят с обеих сторон каждого трансформатора, на "
                "вводах 10 кВ, на секционном и на каждом фидере — их можно "
                "отключать в режимах. Демонстрационный проект: параметры "
                "правдоподобны, но к реальному объекту не относятся. "
                "Коэффициенты методики — типовые и требуют утверждения.",
    },
    "methodology": {"file": "../data/methodology_default.json"},
    "neutral": {"110": "earthed", "10": "isolated"},
    "nodes": nodes,
    "transformers3w": [],
    "branches": branches,
    "loads": loads,
    "modes": modes,
}

V1_PATH.write_text(json.dumps(project, ensure_ascii=False, indent=1),
                   encoding="utf-8")
data = load_project(V1_PATH)

#  Линии рисуются ТРАССАМИ, а не объектами в рамке: ровно так их теперь
#  создаёт протяжка от вывода, и на схеме не должно быть двух разных видов
#  одной и той же линии.
import tools_autolayout

data.diagram = tools_autolayout.build_layout(data, lines_as_routes=True)
problems = data.diagram.validate_targets(data.electrical_model)
if problems:
    raise SystemExit("Ошибки ссылок схемы:\n- " + "\n- ".join(problems))
save_project(V7_PATH, data)
switches = sum(1 for row in branches if row["kind"] == "tie")
print(f"узлов: {len(nodes)}, ветвей: {len(branches)} (из них выключателей: "
      f"{switches}), нагрузок: {len(loads)}, режимов: {len(modes)}")
print(f"сохранено: {V1_PATH} и {V7_PATH}")
