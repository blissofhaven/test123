# -*- coding: utf-8 -*-
"""Сборка демонстрационной сети «Энергорайон»: четыре ПС и 24 отходящих фидера.

Скрипт строит проект в исходном формате v1 и сохраняет его через штатный
загрузчик, поэтому результат — обычный проект текущего формата, ничем не
отличимый от созданного пользователем. Запускать из корня проекта:

    python tools/build_demo_network.py

Состав сети описан в docs/roadmap/stages/B1-demo-and-analysis.md.
Параметры оборудования правдоподобны и взяты из типовых справочных рядов, но
к реальному объекту не относятся.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rza_calc.io.project import load_project, save_project

V1_PATH = ROOT / "rza_calc/examples/energoraion_v1.json"
V7_PATH = ROOT / "rza_calc/examples/energoraion.json"

PROT_FULL = {"mtz": True, "to": True, "ozz": True}      # линия с ОЗЗ
PROT_LINE = {"mtz": True, "to": True, "ozz": False}     # линия без ОЗЗ
PROT_MTZ = {"mtz": True, "to": False, "ozz": False}     # только МТЗ
#  Короткий кабельный фидер у мощных шин: ток КЗ в конце линии почти равен
#  току на шинах, зоны у отсечки нет — в проекте ТО не применяется.
PROT_SHORT_CABLE = {"mtz": True, "to": False, "ozz": True}

# ── Удельные параметры прямой последовательности, Ом/км ───────────────────
#  ВЛ (сталеалюминиевый провод), x зависит от среднегеометрического расстояния
OVERHEAD = {
    (110, 120): (0.249, 0.427),
    (110, 150): (0.198, 0.420),
    (35, 95): (0.301, 0.400),
    (35, 120): (0.249, 0.391),
    (10, 50): (0.603, 0.392),
    (10, 70): (0.428, 0.375),
    (10, 95): (0.315, 0.363),
}
#  Кабель 10 кВ с алюминиевыми жилами: (r0, x0, ёмкостный ток А/км)
CABLE = {
    95: (0.326, 0.083, 0.80),
    120: (0.258, 0.081, 0.90),
    150: (0.206, 0.079, 1.00),
    240: (0.129, 0.075, 1.30),
}


def node(nid: str, name: str, u_nom: float, kind: str | None = None) -> dict:
    row = {"id": nid, "name": name, "u_nom": u_nom}
    if kind:
        row["kind"] = kind
    return row


def source(nid, name, to, s_max, s_min) -> dict:
    return {"id": nid, "kind": "source", "name": name, "node_from": "GRID",
            "node_to": to, "s_kz_max": s_max, "s_kz_min": s_min,
            "input_mode_max": "power", "input_mode_min": "power",
            "x_r_ratio": 12.0}


def transformer(nid, name, hv, lv, s_nom, u_hv, u_lv, uk, p_k, ct,
                group="Д/Ун-11", inrush=5.0, prot=None, terminal="БМРЗ-100") -> dict:
    return {"id": nid, "kind": "transformer", "name": name,
            "node_from": hv, "node_to": lv, "s_nom": s_nom,
            "u_hv": u_hv, "u_lv": u_lv, "uk": uk, "p_k": p_k, "group": group,
            "i_inrush_ratio": inrush, "ct_ratio": [ct, 5],
            "breaker_t_off": 0.06, "terminal": terminal,
            "switchable": True, "normally_closed": True,
            "prot": dict(prot or PROT_MTZ)}


def cable(nid, name, a, b, km, section, ct, brand="АПвПу", closed=True,
          prot=None, terminal="БМРЗ-100") -> dict:
    r0, x0, ic = CABLE[section]
    return {"id": nid, "kind": "line", "name": name, "node_from": a, "node_to": b,
            "line_type": "cable", "length_km": km, "brand": f"{brand}-10",
            "section_mm2": section, "material": "Al",
            "r0": r0, "x0": x0, "ic_per_km": ic,
            "ct_ratio": [ct, 5], "breaker_t_off": 0.06, "terminal": terminal,
            "switchable": True, "normally_closed": closed,
            "prot": dict(prot or PROT_FULL)}


def overhead(nid, name, a, b, km, u_class, section, ct, closed=True,
             prot=None, terminal="Бреслер ТОР-300") -> dict:
    r0, x0 = OVERHEAD[(u_class, section)]
    row = {"id": nid, "kind": "line", "name": name, "node_from": a, "node_to": b,
           "line_type": "overhead", "length_km": km, "brand": f"АС-{section}",
           "section_mm2": section, "material": "Al", "r0": r0, "x0": x0,
           "ct_ratio": [ct, 5], "breaker_t_off": 0.06, "terminal": terminal,
           "switchable": True, "normally_closed": closed,
           "prot": dict(prot or PROT_LINE)}
    if u_class == 10:
        # Ёмкостный ток ВЛ 10 кВ мал, но должен быть задан: иначе ёмкостный
        # ток сети занижается и уставка ОЗЗ кабельных фидеров недостоверна.
        row["ic_per_km"] = 0.02
    return row


def tie(nid, name, a, b, closed, ct=1000, prot=None) -> dict:
    return {"id": nid, "kind": "tie", "name": name, "node_from": a, "node_to": b,
            "ct_ratio": [ct, 5], "breaker_t_off": 0.06, "terminal": "БМРЗ-100",
            "switchable": True, "normally_closed": closed,
            "prot": dict(prot or PROT_MTZ)}


def load(lid, name, node_id, p_kw, cos_phi=0.9, k_use=1.0) -> dict:
    return {"id": lid, "name": name, "node": node_id, "p_kw": p_kw,
            "cos_phi": cos_phi, "k_use": k_use}


nodes: list[dict] = []
branches: list[dict] = []
tr3w: list[dict] = []
loads: list[dict] = []

# ──────────────────────────────────────────────────────────────────────────
#  ПС «Центральная» 110/35/10 — питающая, трёхобмоточный трансформатор
# ──────────────────────────────────────────────────────────────────────────
nodes += [
    node("c110", "ЦЕНТРАЛЬНАЯ · ОРУ 110 кВ", 110),
    node("c35", "ЦЕНТРАЛЬНАЯ · ОРУ 35 кВ", 35),
    node("c10_t1", "ЦЕНТРАЛЬНАЯ · вывод НН Т1", 10, "point"),
    node("c10_t2", "ЦЕНТРАЛЬНАЯ · вывод НН Т2", 10, "point"),
    node("c10_1", "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ", 10),
    node("c10_2", "ЦЕНТРАЛЬНАЯ · 2 СШ 10 кВ", 10),
]
branches += [source("SC", "Энергосистема 110 кВ", "c110", 2500, 1700)]
tr3w += [
    {"id": "CT1", "name": "Т1 Центральная 110/35/10", "node_hv": "c110",
     "node_mv": "c35", "node_lv": "c10_t1", "s_nom": 25000,
     "u_hv": 110, "u_mv": 35, "u_lv": 10,
     "uk_hm": 10.5, "uk_hl": 17.5, "uk_ml": 6.5, "p_k": 140,
     "group": "Ун/Ун/Д-0-11", "i_inrush_ratio": 5, "ct_ratio": [200, 5],
     "breaker_t_off": 0.06, "terminal": "Бреслер ТОР-300", "switchable": True,
     "normally_closed": True, "prot": dict(PROT_MTZ)},
]
branches += [
    transformer("CT2", "Т2 Центральная 110/10", "c110", "c10_t2",
                16000, 110, 10, 10.5, 85, 150, terminal="Бреслер ТОР-300"),
    tie("CV1", "Ввод-1 10 кВ (Центральная)", "c10_t1", "c10_1", True, 1500),
    tie("CV2", "Ввод-2 10 кВ (Центральная)", "c10_t2", "c10_2", True, 1500),
    tie("CSV", "СВ 10 кВ (Центральная)", "c10_1", "c10_2", False, 1500),
]

# ──────────────────────────────────────────────────────────────────────────
#  ПС «Северная» 110/10 — два двухобмоточных
# ──────────────────────────────────────────────────────────────────────────
nodes += [
    node("n110", "СЕВЕРНАЯ · ОРУ 110 кВ", 110),
    node("n10_t1", "СЕВЕРНАЯ · вывод НН Т1", 10, "point"),
    node("n10_t2", "СЕВЕРНАЯ · вывод НН Т2", 10, "point"),
    node("n10_1", "СЕВЕРНАЯ · 1 СШ 10 кВ", 10),
    node("n10_2", "СЕВЕРНАЯ · 2 СШ 10 кВ", 10),
]
branches += [
    overhead("VL110_1", "ВЛ-110 Центральная — Северная", "c110", "n110",
             18.5, 110, 120, 300),
    transformer("NT1", "Т1 Северная 110/10", "n110", "n10_t1",
                16000, 110, 10, 10.5, 85, 150),
    transformer("NT2", "Т2 Северная 110/10", "n110", "n10_t2",
                16000, 110, 10, 10.5, 85, 150),
    tie("NV1", "Ввод-1 10 кВ (Северная)", "n10_t1", "n10_1", True, 1500),
    tie("NV2", "Ввод-2 10 кВ (Северная)", "n10_t2", "n10_2", True, 1500),
    tie("NSV", "СВ 10 кВ (Северная)", "n10_1", "n10_2", False, 1500),
]

# ──────────────────────────────────────────────────────────────────────────
#  ПС «Западная» 35/10 — питается по ВЛ 35 кВ от «Центральной»
# ──────────────────────────────────────────────────────────────────────────
nodes += [
    node("w35", "ЗАПАДНАЯ · ОРУ 35 кВ", 35),
    node("w10_t1", "ЗАПАДНАЯ · вывод НН Т1", 10, "point"),
    node("w10_t2", "ЗАПАДНАЯ · вывод НН Т2", 10, "point"),
    node("w10_1", "ЗАПАДНАЯ · 1 СШ 10 кВ", 10),
    node("w10_2", "ЗАПАДНАЯ · 2 СШ 10 кВ", 10),
]
branches += [
    overhead("VL35", "ВЛ-35 Центральная — Западная", "c35", "w35",
             15.5, 35, 95, 200),
    transformer("WT1", "Т1 Западная 35/10", "w35", "w10_t1",
                6300, 35, 10, 7.5, 46.5, 100),
    transformer("WT2", "Т2 Западная 35/10", "w35", "w10_t2",
                6300, 35, 10, 7.5, 46.5, 100),
    tie("WV1", "Ввод-1 10 кВ (Западная)", "w10_t1", "w10_1", True, 1000),
    tie("WV2", "Ввод-2 10 кВ (Западная)", "w10_t2", "w10_2", True, 1000),
    tie("WSV", "СВ 10 кВ (Западная)", "w10_1", "w10_2", False, 1000),
]

# ──────────────────────────────────────────────────────────────────────────
#  ПС «Южная» 110/10 — один трансформатор, одна секция, резервное питание
# ──────────────────────────────────────────────────────────────────────────
nodes += [
    node("s110", "ЮЖНАЯ · ОРУ 110 кВ", 110),
    node("s10_t1", "ЮЖНАЯ · вывод НН Т1", 10, "point"),
    node("s10_1", "ЮЖНАЯ · 1 СШ 10 кВ", 10),
]
branches += [
    overhead("VL110_2", "ВЛ-110 Центральная — Южная", "c110", "s110",
             24.0, 110, 120, 300),
    overhead("VL110_3", "ВЛ-110 Северная — Южная (резерв)", "n110", "s110",
             21.0, 110, 120, 300, closed=False),
    transformer("ST1", "Т1 Южная 110/10", "s110", "s10_t1",
                10000, 110, 10, 10.5, 60, 100),
    tie("SV1", "Ввод-1 10 кВ (Южная)", "s10_t1", "s10_1", True, 1000),
]

# ──────────────────────────────────────────────────────────────────────────
#  Отходящие фидеры 10 кВ — по шесть на подстанцию
# ──────────────────────────────────────────────────────────────────────────
#  Исполнение фидера:
#    (вид, длина км, сечение мм², ТТ, кВА КТП|None, кВт нагрузки, состав защит)
#  Вид: "cable" · "overhead" · "recloser" (ВЛ с реклоузером и участком за ним)
#
#  «Центральная» — смешанный узел: кабельные вводы в промзону и воздушные
#  фидеры в пригород. Кабельная сеть каждой секции содержит короткий и
#  длинный фидер: короткий проходит по чувствительности ОЗЗ, длинный — нет,
#  и это видно в протоколе.
CENTRAL_FEEDERS = (
    ("cable", 1.2, 150, 300, 630, 420, PROT_SHORT_CABLE),
    ("overhead", 6.4, 70, 200, None, 620, PROT_LINE),
    ("cable", 5.6, 120, 300, None, 950, PROT_FULL),
    ("cable", 1.1, 150, 300, 400, 260, PROT_SHORT_CABLE),
    ("recloser", 8.2, 70, 200, 250, 170, PROT_LINE),
    ("cable", 6.1, 95, 200, 400, 300, PROT_FULL),
)
#  «Северная» и «Западная» — сельские воздушные сети, ОЗЗ по току не
#  применяется (ёмкостный ток ВЛ мал, защита выполняется по напряжению).
RURAL_FEEDERS = (
    ("overhead", 7.4, 70, 200, 400, 290, PROT_LINE),
    ("overhead", 5.1, 70, 200, None, 520, PROT_LINE),
    ("overhead", 6.2, 70, 200, 250, 165, PROT_LINE),
    ("recloser", 8.8, 70, 200, 250, 175, PROT_LINE),
    ("overhead", 4.6, 95, 300, 630, 430, PROT_LINE),
    ("overhead", 6.8, 50, 150, None, 380, PROT_LINE),
)
#  «Южная» — городская ПС с одной секцией и шестью кабельными фидерами:
#  ёмкостный ток сети достаточен, ОЗЗ проходит по чувствительности.
CITY_FEEDERS = (
    ("cable", 7.8, 150, 300, 630, 450, PROT_FULL),
    ("cable", 6.9, 120, 300, 400, 280, PROT_FULL),
    ("cable", 8.4, 150, 300, None, 900, PROT_FULL),
    ("cable", 7.2, 120, 300, 400, 310, PROT_FULL),
    ("cable", 7.5, 150, 300, 630, 470, PROT_FULL),
    ("cable", 5.8, 95, 200, None, 640, PROT_FULL),
)

FEEDER_GROUPS = (
    ("C", "Центральная", ("c10_1",) * 3 + ("c10_2",) * 3, CENTRAL_FEEDERS),
    ("N", "Северная", ("n10_1",) * 3 + ("n10_2",) * 3, RURAL_FEEDERS),
    ("W", "Западная", ("w10_1",) * 3 + ("w10_2",) * 3, RURAL_FEEDERS),
    ("S", "Южная", ("s10_1",) * 6, CITY_FEEDERS),
)

for prefix, station, buses, kinds in FEEDER_GROUPS:
    for index, (bus, spec) in enumerate(zip(buses, kinds), start=1):
        shape, km, section, ct, ktp_kva, load_kw, prot = spec
        fid = f"{prefix}F{index}"
        name = f"Ф-{index} 10 кВ ({station})"
        end = f"{fid}_end"
        nodes.append(node(end, f"{station} · конец Ф-{index}", 10, "point"))

        if shape == "cable":
            branches.append(cable(fid, name, bus, end, km, section, ct,
                                  prot=prot))
        else:
            branches.append(overhead(fid, name, bus, end, km, 10, section, ct,
                                     prot=prot))

        tail = end
        if shape == "recloser":
            # Реклоузер в середине магистрали и участок за ним. ТО за
            # реклоузером не применяется: ток КЗ в конце участка сравним с
            # током в месте установки, зоны у отсечки нет (см. отчёт B1).
            mid = f"{fid}_rec"
            nodes.append(node(mid, f"{station} · за реклоузером Ф-{index}", 10, "point"))
            branches.append({
                "id": f"{fid}_R", "kind": "tie",
                "name": f"Реклоузер Ф-{index} ({station})",
                "node_from": end, "node_to": mid,
                "ct_ratio": [400, 5], "breaker_t_off": 0.06,
                "terminal": "PBA/TEL", "switchable": True,
                "normally_closed": True, "prot": dict(PROT_MTZ),
            })
            tail = f"{fid}_tail"
            nodes.append(node(tail, f"{station} · хвост Ф-{index}", 10, "point"))
            branches.append(overhead(
                f"{fid}_2", f"Ф-{index} за реклоузером ({station})",
                mid, tail, 1.6, 10, 70, 200, prot=PROT_MTZ,
                terminal="PBA/TEL"))

        if ktp_kva:
            lv = f"{fid}_lv"
            nodes.append(node(lv, f"{station} · КТП Ф-{index}, щит 0,4 кВ", 0.4))
            branches.append(transformer(
                f"{fid}_T", f"КТП Ф-{index} {ktp_kva} кВ·А ({station})",
                tail, lv, float(ktp_kva), 10, 0.4, 5.5, 7.6, 100,
                inrush=8.0, prot=PROT_LINE, terminal="БМРЗ-100",
            ))
            loads.append(load(f"{fid}_L", f"Нагрузка КТП Ф-{index} ({station})",
                              lv, load_kw))
        else:
            loads.append(load(f"{fid}_L", f"Нагрузка Ф-{index} ({station})",
                              tail, load_kw))

# ──────────────────────────────────────────────────────────────────────────
#  Режимы сети
# ──────────────────────────────────────────────────────────────────────────
BASE_STATES = {
    "CT1": True, "CT2": True, "CV1": True, "CV2": True, "CSV": False,
    "NT1": True, "NT2": True, "NV1": True, "NV2": True, "NSV": False,
    "WT1": True, "WT2": True, "WV1": True, "WV2": True, "WSV": False,
    "ST1": True, "SV1": True,
    "VL110_1": True, "VL110_2": True, "VL110_3": False, "VL35": True,
}


def switch_states(**overrides) -> dict:
    base = dict(BASE_STATES)
    base.update(overrides)
    return base


modes = [
    {"id": "normal", "name": "Нормальный режим", "system": "max",
     "description": "Все трансформаторы в работе, секции 10 кВ разделены, "
                    "секционные выключатели отключены, резервная ВЛ-110 "
                    "Северная — Южная отключена. Энергосистема в максимальном "
                    "режиме.",
     "states": switch_states()},
    {"id": "min", "name": "Минимальный режим системы", "system": "min",
     "description": "Нормальная схема при минимальной мощности КЗ "
                    "энергосистемы. По этому режиму проверяется "
                    "чувствительность защит.",
     "states": switch_states()},
    {"id": "c_repair", "name": "Ремонт Т2 Центральной, СВ включён",
     "system": "max",
     "description": "Т2 ПС Центральная выведен в ремонт, обе секции 10 кВ "
                    "питаются от Т1 через секционный выключатель.",
     "states": switch_states(CT2=False, CV2=False, CSV=True)},
    {"id": "n_repair", "name": "Ремонт Т1 Северной, СВ включён", "system": "max",
     "description": "Т1 ПС Северная выведен в ремонт, обе секции 10 кВ "
                    "питаются от Т2 через секционный выключатель.",
     "states": switch_states(NT1=False, NV1=False, NSV=True)},
    {"id": "w_repair", "name": "Ремонт Т1 Западной, СВ включён", "system": "max",
     "description": "Т1 ПС Западная выведен в ремонт, обе секции 10 кВ "
                    "питаются от Т2 через секционный выключатель.",
     "states": switch_states(WT1=False, WV1=False, WSV=True)},
    {"id": "s_reserve", "name": "Южная на резервном питании", "system": "max",
     "description": "ВЛ-110 Центральная — Южная отключена, ПС Южная питается "
                    "по резервной ВЛ-110 от ПС Северная.",
     "states": switch_states(VL110_2=False, VL110_3=True)},
]

project = {
    "format_version": 1,
    "project": {
        "name": "Энергорайон — демонстрационная сеть",
        "note": "Четыре подстанции и 24 отходящих фидера 10 кВ. "
                "Демонстрационный проект: параметры оборудования взяты из "
                "типовых справочных рядов и правдоподобны, но к реальному "
                "объекту не относятся. Коэффициенты методики — заглушка и "
                "требуют замены на принятые в проекте.",
    },
    "methodology": {"file": "../data/methodology_default.json"},
    "neutral": {"110": "earthed", "35": "isolated", "10": "isolated",
                "0.4": "earthed"},
    "nodes": nodes,
    "transformers3w": tr3w,
    "branches": branches,
    "loads": loads,
    "modes": modes,
}

def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    V1_PATH.write_text(
        json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    save_project(V7_PATH, load_project(V1_PATH))
    print(f"узлов: {len(nodes)}, ветвей: {len(branches)}, 3W: {len(tr3w)}, "
          f"нагрузок: {len(loads)}, режимов: {len(modes)}")
    print(f"сохранено: {V1_PATH} и {V7_PATH}")


if __name__ == "__main__":
    main()
