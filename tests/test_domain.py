# -*- coding: utf-8 -*-
"""Проверки физической структуры, отдельной от расчётного графа."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rza_calc.core.model import GRID, LineBranch, Mode, Network, Node, SourceBranch
from rza_calc.domain import (
    Bay,
    BusSection,
    CalculationRef,
    Equipment,
    EquipmentPlacement,
    Facility,
    ProjectStructure,
    VoltageLevel,
)


def _network() -> Network:
    net = Network("тест")
    net.add_node(Node("g10", "Шины ГТЭС 10 кВ", 10))
    net.add_node(Node("ps35", "Шины ПС 35 кВ", 35))
    net.add_branch(SourceBranch(
        id="grid", name="Система", node_from=GRID, node_to="ps35",
        s_kz_max=500, s_kz_min=300,
    ))
    net.add_branch(LineBranch(
        id="line_1", name="ВЛ ГТЭС — ПС", node_from="g10", node_to="ps35",
        length_km=12.0, r0=0.25, x0=0.4,
    ))
    net.add_mode(Mode("normal", "Нормальный"))
    return net


def _structure() -> ProjectStructure:
    s = ProjectStructure()
    s.add_facility(Facility("gtes", "ГТЭС Север", "power_plant"))
    s.add_facility(Facility("ps", "ПС Северная", "substation"))
    s.add_voltage_level(VoltageLevel("gtes_10", "gtes", "РУ-10 кВ", 10.0))
    s.add_voltage_level(VoltageLevel("ps_35", "ps", "ОРУ-35 кВ", 35.0))
    s.add_bus_section(BusSection("gtes_10_bus", "gtes_10", "1 СШ", "g10"))
    s.add_bus_section(BusSection("ps_35_bus", "ps_35", "1 СШ", "ps35"))
    s.add_bay(Bay(
        "gtes_line_bay", "gtes_10", "ВЛ на ПС Северная", "outgoing",
        bus_section_id="gtes_10_bus",
    ))
    s.add_bay(Bay(
        "ps_line_bay", "ps_35", "ВЛ от ГТЭС", "incoming",
        bus_section_id="ps_35_bus",
    ))
    s.add_equipment(Equipment(
        "line", "ВЛ ГТЭС — ПС Северная", "line",
        [CalculationRef("branch", "line_1", "impedance")],
    ))
    s.place_equipment(EquipmentPlacement(
        "line_at_gtes", "line", "gtes_line_bay", terminal="from",
    ))
    s.place_equipment(EquipmentPlacement(
        "line_at_ps", "line", "ps_line_bay", terminal="to",
    ))
    return s


def test_structure_builds_navigation_chain():
    s = _structure()
    assert [f.id for f in s.root_facilities()] == ["gtes", "ps"]
    assert [v.id for v in s.levels_of("ps")] == ["ps_35"]
    assert [section.id for section in s.sections_of("ps_35")] == ["ps_35_bus"]
    assert [b.id for b in s.bays_of("ps_35")] == ["ps_line_bay"]
    assert [e.id for e in s.equipment_in_bay("ps_line_bay")] == ["line"]


def test_one_equipment_can_connect_two_facilities():
    s = _structure()
    placements = s.placements_of("line")
    assert {p.bay_id for p in placements} == {"gtes_line_bay", "ps_line_bay"}
    assert {p.terminal for p in placements} == {"from", "to"}


def test_structure_is_separate_but_validates_calculation_refs():
    s = _structure()
    net = _network()
    assert s.validate(net) == []
    # Физическая структура не меняет расчётный граф.
    assert set(net.branches) == {"grid", "line_1"}
    assert not hasattr(net, "facilities")


def test_missing_calculation_object_is_reported():
    s = _structure()
    s.equipment["line"].calculation_refs.append(
        CalculationRef("branch", "missing", "breaker")
    )
    problems = s.validate(_network())
    assert any("missing" in text and "не найден" in text for text in problems)


def test_duplicate_ids_are_rejected_across_hierarchy():
    s = ProjectStructure()
    s.add_facility(Facility("same", "ГТЭС", "power_plant"))
    with pytest.raises(ValueError):
        s.add_equipment(Equipment("same", "Генератор", "generator"))


def test_transformer_can_have_hv_mv_lv_placements():
    s = ProjectStructure()
    s.add_facility(Facility("ps", "ПС 110/35/10", "substation"))
    for suffix, voltage in (("110", 110.0), ("35", 35.0), ("10", 10.0)):
        s.add_voltage_level(VoltageLevel(f"vl_{suffix}", "ps", f"РУ-{suffix} кВ", voltage))
        s.add_bus_section(BusSection(f"bus_{suffix}", f"vl_{suffix}", "1 СШ"))
        s.add_bay(Bay(
            f"bay_{suffix}", f"vl_{suffix}", f"АТ, сторона {suffix} кВ",
            "transformer", bus_section_id=f"bus_{suffix}",
        ))
    s.add_equipment(Equipment(
        "at1", "АТ-1", "autotransformer",
        [
            CalculationRef("branch", "at1_hv", "hv_leg"),
            CalculationRef("branch", "at1_mv", "mv_leg"),
            CalculationRef("branch", "at1_lv", "lv_leg"),
        ],
    ))
    for suffix, terminal in (("110", "hv"), ("35", "mv"), ("10", "lv")):
        s.place_equipment(EquipmentPlacement(
            f"at1_{terminal}", "at1", f"bay_{suffix}", terminal=terminal,
        ))
    assert {p.terminal for p in s.placements_of("at1")} == {"hv", "mv", "lv"}
    assert s.validate() == []


def test_duplicate_equipment_terminal_is_reported():
    s = _structure()
    s.place_equipment(EquipmentPlacement(
        "line_second_from", "line", "ps_line_bay", terminal="from",
    ))
    assert any("сторона 'from' размещена дважды" in text for text in s.validate())
