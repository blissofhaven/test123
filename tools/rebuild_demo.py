# -*- coding: utf-8 -*-
"""Пересборка демонстрационного проекта: резервные вводы и секционирование КТП."""
import argparse
import json, copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rza_calc.io.project import load_project, save_project


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    legacy_path = ROOT / 'rza_calc/examples/gtes_sever_v1.json'
    current_path = ROOT / 'rza_calc/examples/gtes_sever.json'
    src = json.loads(legacy_path.read_text(encoding='utf-8'))
    d = copy.deepcopy(src)

    KEEP_NODES = {'b220','G1_bus','G2_bus','G3_bus','G4_bus','b110',
                  'vost110','vost35','vost10_1','vost10_2','vost10_1_t','vost10_2_t',
                  'zap35','zap10','yuzh110','yuzh10'}
    KEEP_BR = {'G1','G1_bt','G2','G2_bt','G3','G3_bt','G4','G4_bt','AT1','AT2',
               'VL110_1','VL110_2','VV1','VV2','VSV','VL35','ZT1','VL110_3','YT1'}
    d['nodes'] = [n for n in d['nodes'] if n['id'] in KEEP_NODES]
    d['branches'] = [b for b in d['branches'] if b['id'] in KEEP_BR]
    d['loads'] = []

    def node(i, n, u, **kw):
        x = {"id": i, "name": n, "u_nom": u}; x.update(kw); return x

    def cable(i, n, a, b, km=2.5, ct=400):
        return {"id": i, "kind": "line", "name": n, "node_from": a, "node_to": b,
                "line_type": "cable", "length_km": km, "brand": "ААБл", "section_mm2": 150,
                "material": "Al", "ic_per_km": 0.9, "ct_ratio": [ct, 5],
                "breaker_t_off": 0.06, "terminal": "БМРЗ", "switchable": True,
                "normally_closed": True, "prot": {"mtz": True, "to": True, "ozz": True}}

    def overhead(i, n, a, b, km, brand, sec, ct):
        return {"id": i, "kind": "line", "name": n, "node_from": a, "node_to": b,
                "line_type": "overhead", "length_km": km, "brand": brand,
                "section_mm2": sec, "material": "Al", "x0": 0.405, "ct_ratio": [ct, 5],
                "breaker_t_off": 0.06, "terminal": "Bresler", "switchable": True,
                "normally_closed": True, "prot": {"mtz": True, "to": True, "ozz": False}}

    def tie(i, n, a, b, closed, ct=1500):
        return {"id": i, "kind": "tie", "name": n, "node_from": a, "node_to": b,
                "ct_ratio": [ct, 5], "breaker_t_off": 0.06, "terminal": "БМРЗ",
                "switchable": True, "normally_closed": closed,
                "prot": {"mtz": True, "to": False, "ozz": False}}

    def tr2(i, n, a, b, s_nom, u_hv, u_lv, uk, p_k, ct, group="Д/Ун-11", inrush=8):
        return {"id": i, "kind": "transformer", "name": n, "node_from": a, "node_to": b,
                "s_nom": s_nom, "u_hv": u_hv, "u_lv": u_lv, "uk": uk, "p_k": p_k,
                "group": group, "i_inrush_ratio": inrush, "ct_ratio": [ct, 5],
                "breaker_t_off": 0.06, "terminal": "БМРЗ", "switchable": True,
                "normally_closed": True, "prot": {"mtz": True, "to": False, "ozz": False}}

    # ── ПС Северная 110/10, две секции, два трансформатора ──────────────────────
    d['nodes'] += [
        node("sev110", "ПС Северная, ОРУ 110 кВ", 110),
        node("sev10_1", "ПС Северная, 1 СШ 10 кВ", 10, section="1 СШ"),
        node("sev10_2", "ПС Северная, 2 СШ 10 кВ", 10, section="2 СШ"),
        node("sev10_1_t", "Т1 Северная, вывод 10 кВ", 10, kind="point"),
        node("sev10_2_t", "Т2 Северная, вывод 10 кВ", 10, kind="point"),
    ]
    d['branches'] += [
        overhead("VL110_4", "ВЛ-110-4 Северная", "b110", "sev110", 31.0, "АС-240", 240, 300),
        tr2("ST1", "Т1 ПС Северная 110/10", "sev110", "sev10_1_t", 16000, 110, 10, 10.5, 85,
            200, "Ун/Д-11", 6),
        tr2("ST2", "Т2 ПС Северная 110/10", "sev110", "sev10_2_t", 16000, 110, 10, 10.5, 85,
            200, "Ун/Д-11", 6),
        tie("SVV1", "Ввод-1 10 кВ (Северная)", "sev10_1_t", "sev10_1", True),
        tie("SVV2", "Ввод-2 10 кВ (Северная)", "sev10_2_t", "sev10_2", True),
        tie("SSV", "СВ 10 кВ (Северная)", "sev10_1", "sev10_2", False),
    ]

    # ── КТП: два ввода, две секции 10 кВ и 0,4 кВ, СВ на обеих сторонах ─────────
    KTPS = [
        # (номер, префикс питающей ПС, секция рабочая, секция резервная, S, кВА, нагрузка)
        (1,  "VF", "vost10_1", "vost10_2", 630, 380),
        (2,  "VF", "vost10_1", "vost10_2", 630, 350),
        (3,  "VF", "vost10_1", "vost10_2", 400, 240),
        (4,  "VF", "vost10_2", "vost10_1", 630, 400),
        (5,  "VF", "vost10_2", "vost10_1", 400, 230),
        (6,  "VF", "vost10_2", "vost10_1", 630, 360),
        (7,  "ZF", "zap10",    "zap10",    400, 250),
        (8,  "ZF", "zap10",    "zap10",    400, 220),
        (9,  "YF", "yuzh10",   "yuzh10",   630, 390),
        (10, "YF", "yuzh10",   "yuzh10",   630, 370),
        (11, "SF", "sev10_1",  "sev10_2",  630, 340),
        (12, "SF", "sev10_2",  "sev10_1",  400, 260),
    ]
    NAMES = {"VF": "Восток", "ZF": "Запад", "YF": "Южная", "SF": "Северная"}
    counter = {}
    for number, prefix, work, reserve, s_nom, load_kw in KTPS:
        tag = f"K{number}"
        counter[prefix] = counter.get(prefix, 0) + 1
        seq = counter[prefix]
        d['nodes'] += [
            node(f"{tag}_10_1", f"КТП-{number}, 1 СШ 10 кВ", 10, section="1 СШ"),
            node(f"{tag}_10_2", f"КТП-{number}, 2 СШ 10 кВ", 10, section="2 СШ"),
            node(f"{tag}_04_1", f"КТП-{number}, 1 СШ 0,4 кВ", 0.4, section="1 СШ"),
            node(f"{tag}_04_2", f"КТП-{number}, 2 СШ 0,4 кВ", 0.4, section="2 СШ"),
        ]
        area = NAMES[prefix]
        d['branches'] += [
            cable(f"{prefix}{seq}", f"Ф-{seq} ({area})",
                  work, f"{tag}_10_1", km=2.0 + 0.4 * seq),
            cable(f"{prefix}{seq}R", f"Ф-{seq}Р ({area})",
                  reserve, f"{tag}_10_2", km=2.4 + 0.4 * seq),
            tie(f"{tag}_SV10", f"СВ 10 кВ (КТП-{number})", f"{tag}_10_1", f"{tag}_10_2",
                False, ct=400),
            tr2(f"{tag}_T1", f"КТП-{number} Т1 10/0,4", f"{tag}_10_1", f"{tag}_04_1",
                s_nom, 10, 0.4, 5.5, 11, 100),
            tr2(f"{tag}_T2", f"КТП-{number} Т2 10/0,4", f"{tag}_10_2", f"{tag}_04_2",
                s_nom, 10, 0.4, 5.5, 11, 100),
            tie(f"{tag}_SV04", f"СВ 0,4 кВ (КТП-{number})", f"{tag}_04_1", f"{tag}_04_2",
                False, ct=1000),
        ]
        d['loads'] += [
            {"id": f"L_{tag}_1", "name": f"Нагрузка КТП-{number}, 1 СШ",
             "node": f"{tag}_04_1", "p_kw": load_kw, "cos_phi": 0.9},
            {"id": f"L_{tag}_2", "name": f"Нагрузка КТП-{number}, 2 СШ",
             "node": f"{tag}_04_2", "p_kw": load_kw, "cos_phi": 0.9},
        ]

    # ── блочные трансформаторы получают выключатель со стороны 220 кВ ──────────
    for b in d['branches']:
        if b['id'].endswith('_bt'):
            b.setdefault('ct_ratio', [1000, 5])
            b.setdefault('breaker_t_off', 0.06)
            b.setdefault('terminal', 'Bresler')

    # ── у каждого присоединения с выключателем состояние хранится явно ─────────
    for b in d['branches']:
        if b.get('ct_ratio') or b.get('breaker_t_off'):
            b.setdefault('switchable', True)
            b.setdefault('normally_closed', True)

    # ── режимы ─────────────────────────────────────────────────────────────────
    all_ids = [b['id'] for b in d['branches'] if b.get('switchable')]
    normally_open = {b['id'] for b in d['branches'] if b.get('normally_closed') is False}
    for mode in d['modes']:
        states = {}
        for bid in all_ids:
            states[bid] = bid not in normally_open
        # генераторы и прежние особенности режима сохраняем
        for bid, value in mode.get('states', {}).items():
            if bid in states:
                states[bid] = value
        if mode['id'] == 't1off':
            states['VT1'] = False; states['VSV'] = True
        if mode['id'] == 't1off_min':
            states['VT1'] = False; states['VSV'] = True
        mode['states'] = states

    d['project']['name'] = "ГТЭС Север — сеть 220/110/35/10/0,4 кВ"
    legacy_path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    save_project(current_path, load_project(legacy_path))
    print("узлов:", len(d['nodes']), "| ветвей:", len(d['branches']),
          "| нагрузок:", len(d['loads']), "| режимов:", len(d['modes']))


if __name__ == "__main__":
    main()
