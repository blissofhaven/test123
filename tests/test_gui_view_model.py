# -*- coding: utf-8 -*-
"""GUI получает реальные данные ядра; тесты не требуют установленного PySide6."""
from pathlib import Path

from rza_calc.gui.view_model import ProjectViewModel, iter_tree


EXAMPLE = Path(__file__).resolve().parent.parent / "rza_calc" / "examples" / "gtes_sever.json"


def make_vm() -> ProjectViewModel:
    return ProjectViewModel.open(EXAMPLE)


def test_gui_view_model_loads_real_gtes_project():
    vm = make_vm()
    assert vm.net.name.startswith("ГТЭС Север")
    assert len(vm.generators()) == 4
    assert vm.total_generation_mw() == 40.0
    assert vm.result is not None


def test_gui_mode_changes_generator_cards_and_power():
    vm = make_vm()
    vm.select_mode("min")
    statuses = {item.branch_id: item.enabled for item in vm.generators()}
    assert statuses == {"G1": False, "G2": False, "G3": True, "G4": True}
    assert vm.total_generation_mw() == 12.0


def test_gui_custom_mode_can_change_generator_composition_and_recalculate():
    vm = make_vm()
    vm.ensure_custom_mode()
    vm.set_generator_enabled("G1", False)
    vm.set_generator_enabled("G4", False)
    assert vm.mode_id == "gui_custom"
    assert vm.total_generation_mw() == 20.0
    assert vm.recalculate()
    assert "gui_custom" in vm.result.ctx.solvers


def test_gui_builds_honest_fallback_tree_for_legacy_project():
    vm = make_vm()
    entries = list(iter_tree(vm.tree()))
    assert any(entry.key == "branch:VF2" for entry in entries)
    assert any(entry.key == "node:vost10_1" for entry in entries)
    assert any(entry.key == "group:generators" for entry in entries)
    assert vm.tree()[0].subtitle == "Расчётная схема v1"


def test_gui_selection_exposes_properties_faults_and_settings():
    vm = make_vm()
    vm.select("branch", "VF2")
    assert vm.selection_title().startswith("Ф-2")
    assert any(row.label == "Терминал РЗА" for row in vm.properties())
    assert len(vm.fault_rows()) == len(vm.net.modes)
    assert {row.kind for row in vm.setting_rows()} == {"МТЗ", "ТО", "ОЗЗ"}

