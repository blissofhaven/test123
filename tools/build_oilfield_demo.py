# -*- coding: utf-8 -*-
"""Build the separate synthetic oilfield demo; importing this module writes nothing.

All ratings, distances, impedances and loads are invented demonstration inputs.
They are neither surveyed data nor a protection-setting design for a real site.
The public build_data() and build_project() functions have no disk side effects.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from rza_calc.adapters import import_legacy_network
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID, GeneratorBranch, LineBranch, Load, Mode, Network, Node,
    ProtectionSettings, TieBranch, TransformerBranch,
)
from rza_calc.domain import ProjectStructure
from rza_calc.domain.diagram import DiagramDocument, DiagramDocumentId
from rza_calc.io.project import FORMAT_VERSION, ProjectData, save_project


NAME = "Нефтепромысел «Таёжный» — учебная ГТЭС и 33 двухвводных объекта"
WARNING = (
    "ДЕМОНСТРАЦИОННЫЙ ПРОЕКТ, НЕ РЕАЛЬНЫЙ ОБЪЕКТ. Все мощности, длины, "
    "сопротивления, коэффициенты ТТ, нагрузки и времена отключения придуманы "
    "для проверки программы. Паспортные и натурные данные не подтверждены; "
    "результаты не являются проектными уставками."
)


def _protection(*, mtz=False):
    return ProtectionSettings(mtz=mtz, to=False, ozz=False, backup_through_transformer=False)


class _Builder:
    def __init__(self):
        self.net = Network(NAME, neutral={"110": "earthed", "35": "isolated",
                                         "10": "isolated", "0.4": "earthed"})
        self.facilities = []
        self.by_id = {}

    def node(self, facility, nid, name, voltage, *, bus=False, section=None):
        self.net.add_node(Node(nid, name, voltage, kind="bus" if bus else "point",
                               section=section, note="Учебный узел: " + facility["name"]))
        facility["node_ids"].append(nid)
        return nid

    def branch(self, facility, branch):
        branch.note = "ДЕМО: параметры не подтверждены паспортом или измерениями."
        self.net.add_branch(branch)
        facility["branch_ids"].append(branch.id)
        return branch.id

    def breaker(self, facility, bid, name, first, last, *, closed=True):
        return self.branch(facility, TieBranch(
            bid, name, first, last, normally_closed=closed, switchable=True,
            breaker_t_off=.06, prot=_protection(),
        ))

    def facility(self, fid, name, kind, hv, lv, upstream=None):
        row = {"id": fid, "name": name, "kind": kind, "hv_kv": hv, "lv_kv": lv,
               "upstream_id": upstream, "buses": {"hv": [], "lv": []},
               "bus_ties": {}, "incoming": [], "outgoing": [], "transformers": [],
               "generators": [], "loads": [], "node_ids": [], "branch_ids": [],
               "load_ids": [], "section_names": ["I СШ", "II СШ"]}
        self.facilities.append(row)
        self.by_id[fid] = row
        for side, voltage in (("hv", hv), ("lv", lv)):
            for section in (1, 2):
                row["buses"][side].append(self.node(
                    row, f"{fid}_{side}{section}", f"{name}, {voltage:g} кВ, {section} СШ",
                    voltage, bus=True, section=f"{section} СШ",
                ))
            row["bus_ties"][side] = self.breaker(
                row, f"{fid}_sv_{side}", f"СВ {name} {voltage:g} кВ",
                *row["buses"][side], closed=False,
            )
        return row

    def transformer_pair(self, row, *, kva, uk, loss, ratio=6):
        hv, lv = row["hv_kv"], row["lv_kv"]
        for section in (1, 2):
            tid = f"{row['id']}_t{section}"
            hv_bus, lv_bus = row["buses"]["hv"][section - 1], row["buses"]["lv"][section - 1]
            hv_terminal = self.node(row, tid + "_hv", tid + " ВН", hv)
            lv_terminal = self.node(row, tid + "_lv", tid + " НН", lv)
            qhv = self.breaker(row, tid + "_q_hv", f"QF ВН Т{section} {row['name']}", hv_bus, hv_terminal)
            qlv = self.breaker(row, tid + "_q_lv", f"QF НН Т{section} {row['name']}", lv_terminal, lv_bus)
            # These CT values and MTZ examples are instructional assumptions.
            ct_node = lv_terminal if row["kind"] == "gtes" else hv_terminal
            ct_voltage = lv if row["kind"] == "gtes" else hv
            ct_primary = 3000 if ct_voltage == 10 and kva >= 40000 else (
                600 if kva >= 16000 else 100)
            self.branch(row, TransformerBranch(
                tid, f"Т{section} {row['name']} {kva / 1000:g} МВА", hv_terminal, lv_terminal,
                s_nom=kva, u_hv=hv, u_lv=lv, uk=uk, p_k=loss,
                i_inrush_ratio=ratio, ct_ratio=(ct_primary, 5), ct_node=ct_node,
                breaker_t_off=.06, switchable=False, prot=_protection(mtz=True),
                group="Д/Ун-11 (демо)" if lv == .4 else "схема соединения не подтверждена",
            ))
            row["transformers"].append({
                "id": tid, "section": section, "hv_bus": hv_bus, "lv_bus": lv_bus,
                "hv_breaker": qhv, "lv_breaker": qlv,
                "node_ids": [hv_bus, hv_terminal, lv_terminal, lv_bus],
                "branch_ids": [qhv, tid, qlv],
            })

    def incoming_pair(self, row, *, km):
        upstream = self.by_id[row["upstream_id"]]
        voltage = row["hv_kv"]
        upstream_side = "hv" if upstream["kind"] == "gtes" else "lv"
        r, x = (.198, .42) if voltage == 110 else (.301, .4) if voltage == 35 else (.326, .083)
        for section in (1, 2):
            iid = f"{row['id']}_in{section}"
            first = upstream["buses"][upstream_side][section - 1]
            last = row["buses"]["hv"][section - 1]
            head = self.node(upstream, iid + "_head", iid + " начало линии", voltage)
            tail = self.node(row, iid + "_tail", iid + " конец линии", voltage)
            qout = self.breaker(upstream, iid + "_q_out", f"QF линии {row['name']} ввод {section}", first, head)
            qin = self.breaker(row, iid + "_q_in", f"QF ввода {section} {row['name']}", tail, last)
            line_id = iid + "_line"
            self.branch(row, LineBranch(
                line_id, f"Линия {upstream['name']} — {row['name']}, ввод {section}", head, tail,
                line_type="overhead" if voltage >= 35 else "cable",
                length_km=km + .15 * (section - 1), r0=r, x0=x,
                section_mm2=150 if voltage == 110 else 95, material="Al",
                ct_ratio=(600 if voltage >= 35 else 300, 5), ct_node=head,
                prot=_protection(mtz=True), switchable=False,
            ))
            chain = {"id": iid, "section": section, "upstream_id": upstream["id"],
                     "target_facility_id": row["id"], "upstream_node_id": first,
                     "target_node_id": last, "outgoing_breaker": qout, "line": line_id,
                     "incoming_breaker": qin, "node_ids": [first, head, tail, last],
                     "branch_ids": [qout, line_id, qin]}
            row["incoming"].append(chain)
            upstream["outgoing"].append(dict(chain))

    def load_feeder(self, row, section, number, name, power, *, motor_share=0):
        fid = f"{row['id']}_f{section}_{number}"
        voltage = row["lv_kv"]
        bus = row["buses"]["lv"][section - 1]
        head = self.node(row, fid + "_head", name + " после QF", voltage)
        end = self.node(row, fid + "_end", name + " шины потребителя", voltage)
        breaker = self.breaker(row, fid + "_q", f"QF {name}", bus, head)
        length = .12 + .04 * number if voltage == .4 else .6 + .2 * number
        line_id = self.branch(row, LineBranch(
            fid + "_line", f"КЛ {name}", head, end, line_type="cable", length_km=length,
            r0=.193 if voltage == .4 else .206, x0=.075 if voltage == .4 else .079,
            section_mm2=150, material="Al", prot=_protection(), switchable=False,
        ))
        load_id = fid + "_load"
        self.net.add_load(Load(load_id, name, end, p_kw=power, cos_phi=.85 if motor_share else .92,
                               k_use=1, k_szp=1.5 if motor_share else 1.1, motor_share=motor_share))
        row["load_ids"].append(load_id)
        row["loads"].append({"id": fid, "name": name, "section": section, "bus": bus,
                             "breaker": breaker, "line": line_id, "load_id": load_id,
                             "load_node_id": end, "node_ids": [bus, head, end],
                             "branch_ids": [breaker, line_id]})


def build_data() -> tuple[Network, dict]:
    """Return deterministic calculation data and a data-only layout/test manifest."""
    builder = _Builder()
    plant = builder.facility("gtes", "ГТЭС Таёжная", "gtes", 110, 10)
    builder.transformer_pair(plant, kva=40000, uk=10.5, loss=180)
    for number in range(1, 7):
        section = 1 if number <= 3 else 2
        gid = f"gtes_g{number}"
        terminal = builder.node(plant, gid + "_terminal", f"Г{number}, вывод 10 кВ", 10)
        bus = plant["buses"]["lv"][section - 1]
        builder.branch(plant, GeneratorBranch(
            gid, f"ГТГ-{number} 6 МВт", GRID, terminal, s_nom=7500, p_nom=6,
            cos_phi=.8, u_nom=10, xd2=.15, r_pu=.012,
            ct_ratio=(600, 5), ct_node=terminal, breaker_t_off=.06,
            prot=_protection(mtz=True), switchable=False,
        ))
        breaker = builder.breaker(plant, gid + "_q", f"QF ГТГ-{number}", terminal, bus)
        plant["generators"].append({"id": gid, "section": section, "breaker": breaker,
                                    "bus": bus, "node_ids": [terminal, bus],
                                    "branch_ids": [gid, breaker]})
    for section in (1, 2):
        builder.load_feeder(plant, section, 1, f"ГТЭС СН-{section}: маслосистема и вентиляция", 200, motor_share=.8)
        builder.load_feeder(plant, section, 2, f"ГТЭС СН-{section}: автоматика и освещение", 80)

    for number, zone in enumerate(("Север", "Центр", "Юг"), 1):
        row = builder.facility(f"cp{number:02d}", f"ЦП-{number} {zone} 110/35 кВ", "central", 110, 35, "gtes")
        builder.transformer_pair(row, kva=40000, uk=10.5, loss=180)
        builder.incoming_pair(row, km=12 + 5 * number)
        for section in (1, 2):
            builder.load_feeder(row, section, 1, f"ЦП-{number} СН-{section} (эквивалент на 35 кВ)", 80)

    process_names = ("ДНС Север", "КНС Север", "УПН Центр", "КНС Центр", "ДНС Юг", "КНС Юг")
    for number, name in enumerate(process_names, 1):
        row = builder.facility(f"ps{number:02d}", f"ПС-{number} {name} 35/10 кВ", "substation",
                               35, 10, f"cp{(number - 1) // 2 + 1:02d}")
        builder.transformer_pair(row, kva=16000, uk=8, loss=90)
        builder.incoming_pair(row, km=4 + number)
        for section in (1, 2):
            builder.load_feeder(row, section, 1, f"ПС-{number}, насос Н-{section}", 500, motor_share=.95)
            builder.load_feeder(row, section, 2, f"ПС-{number}, компрессор К-{section}", 400, motor_share=.9)

    for number in range(1, 25):
        row = builder.facility(f"ktp{number:02d}", f"КТП-{number:02d} куст скважин {number:02d}",
                               "ktp", 10, .4, f"ps{(number - 1) // 4 + 1:02d}")
        builder.transformer_pair(row, kva=1000, uk=6, loss=10.5)
        builder.incoming_pair(row, km=1.2 + .35 * ((number - 1) % 4))
        for section in (1, 2):
            builder.load_feeder(row, section, 1, f"КТП-{number:02d} ЭЦН-{section}", 110, motor_share=.95)
            builder.load_feeder(row, section, 2, f"КТП-{number:02d} технологические насосы-{section}", 65, motor_share=.8)
            builder.load_feeder(row, section, 3, f"КТП-{number:02d} обогрев и связь-{section}", 35)

    net = builder.net
    net.add_mode(Mode("normal_max", "Нормальная схема — максимум", system="max",
                      description="Все шесть ГТГ включены. Все СВ разомкнуты; два ввода каждого объекта работают на разные секции."))
    net.add_mode(Mode("normal_min", "Нормальная схема — минимум", system="min",
                      description="Та же топология; минимальный расчётный режим. Мощность ГТГ остаётся заданной сверхпереходной моделью."))
    for fid in ("cp01", "ps01", "ktp01"):
        row = builder.by_id[fid]
        transformer = row["transformers"][0]
        net.add_mode(Mode(
            f"repair_{fid}_t1", f"Ремонт Т1 — {row['name']}",
            states={transformer["hv_breaker"]: False, transformer["lv_breaker"]: False,
                    row["bus_ties"]["lv"]: True}, system="min",
            availability={transformer["id"]: False},
            description="Т1 выведен из работы, QF с обеих сторон открыты. Замкнут только СВ НН. Это топологический пример, не расчёт допустимой перегрузки.",
        ))
    manifest = {
        "schema_version": 1, "name": NAME, "demo_only": True, "warning": WARNING,
        "facilities": builder.facilities, "normal_mode_id": "normal_max",
        "mode_ids": list(net.modes), "generator_power_mw": 36,
        "physical_inputs_status": "demo_assumptions_not_confirmed",
        "unresolved_inputs": [
            "Паспортные сопротивления, длины, нагрузки, ТТ и времена отключения реального объекта отсутствуют.",
            "Данные Z2/Z0, группы соединения и заземление нейтралей для расчёта четырёх видов КЗ не подтверждены.",
            "Не проверены потоки мощности, пуск двигателей, динамика ГТЭС, перегрузка после резервирования и допустимые напряжения.",
        ],
        "assumptions": [
            "ГТЭС — единственный источник: 6 ГТГ по 6 МВт / 7,5 МВА, по три на секцию 10 кВ.",
            "ГТЭС: 2×40 МВА 10/110; ЦП: 3×(2×40 МВА) 110/35; ПС: 6×(2×16 МВА) 35/10; КТП: 24×(2×1 МВА) 10/0,4.",
            "СВ обеих сторон каждого объекта нормально разомкнуты; в нормальной схеме нет параллели двух трансформаторов через СВ.",
            "Выключатели смоделированы отдельными управляемыми нулевыми ветвями, а не графическими значками.",
            "Генераторы представлены ЭДС за сверхпереходным сопротивлением, двигатели — нагрузками без отдельной подпитки КЗ.",
            "Собственные нужды ЦП и ГТЭС представлены эквивалентной нагрузкой на питающих шинах; отдельные ТСН и их низковольтная сеть не моделируются.",
        ],
        "counts": {"facilities": len(builder.facilities), "nodes": len(net.nodes),
                   "branches": len(net.branches), "loads": len(net.loads),
                   "generators": sum(isinstance(b, GeneratorBranch) for b in net.branches.values()),
                   "transformers": sum(isinstance(b, TransformerBranch) for b in net.branches.values()),
                   "breakers": sum(isinstance(b, TieBranch) for b in net.branches.values()),
                   "lines": sum(isinstance(b, LineBranch) for b in net.branches.values()),
                   "modes": len(net.modes)},
    }
    return net, manifest


def build_project(*, with_layout: bool = True) -> ProjectData:
    """Build canonical project data in memory, optionally with the facility sheets."""
    net, manifest = build_data()
    model = import_legacy_network(net)
    methodology = Methodology.load()
    project = ProjectData(
        net, methodology, {"name": NAME, "object": "Вымышленный нефтепромысел",
                           "note": WARNING, "_warning": WARNING, "oilfield_demo": manifest},
        ProjectStructure(), FORMAT_VERSION, model,
        methodology_ref=methodology.data,
        diagram=DiagramDocument(DiagramDocumentId("diagram.oilfield-demo"), NAME, {}),
    )
    if with_layout:
        from tools.oilfield_layout import build_layout
        project.diagram = build_layout(project, manifest)
    return project


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Explicit path for the NEW demo JSON")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly permit replacing the named output")
    parser.add_argument("--without-layout", action="store_true", help="Build electrical data only")
    parser.add_argument("--manifest", type=Path, help="Optional separate topology manifest")
    args = parser.parse_args(argv)
    paths = [args.output, *([args.manifest] if args.manifest else [])]
    if len({p.resolve() for p in paths}) != len(paths):
        parser.error("Project and manifest outputs must be different files.")
    for path in paths:
        if path.exists() and not args.overwrite:
            parser.error(f"Output already exists; use --overwrite explicitly: {path}")
    project = build_project(with_layout=not args.without_layout)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    save_project(args.output, project)
    if args.manifest:
        args.manifest.write_text(json.dumps(project.metadata["oilfield_demo"], ensure_ascii=False,
                                             indent=2) + "\n", encoding="utf-8")
    print(json.dumps(project.metadata["oilfield_demo"]["counts"], ensure_ascii=False))
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
