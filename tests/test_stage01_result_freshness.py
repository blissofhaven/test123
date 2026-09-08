"""Stage 1A: a frozen answer must not impersonate the edited network."""
from __future__ import annotations

import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from rza_calc.core.engine import run_input
from rza_calc.core.result import Check, FAIL, OK, UNRESOLVED
from rza_calc.domain.electrical import thaw_json
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.view_model import ProjectViewModel

ROOT = Path(__file__).resolve().parents[1]


def opened(example="four_fault_types"):
    vm = ProjectViewModel.open(ROOT / f"tests/fixtures/legacy_projects/{example}.json")
    line = next(b for b in vm.net.branches.values() if b.kind == "line")
    vm.select("node", line.node_to)
    vm.select_mode(next(iter(vm.net.modes)))
    return vm, vm.mode_controller


def edit_payload(controller, predicate, **changes):
    equipment = next(e for e in controller.model.equipment.values()
                     if predicate(thaw_json(e.properties.get("legacy_payload", {}))))
    payload = thaw_json(equipment.properties["legacy_payload"])
    payload.update(changes)
    controller.set_equipment_property(equipment.id, "legacy_payload", payload)
    return equipment.id


def change_length(controller):
    return edit_payload(controller, lambda p: p.get("kind") == "line", length_km=20.0)


@pytest.mark.parametrize("example", ["ps_severnaya", "four_fault_types"])
def test_electrical_edit_hides_old_faults_and_current_report_until_recalculated(example):
    vm, controller = opened(example)
    before = vm.fault_rows()[0].i3_ka
    assert before is not None and before > 0
    snapshot = vm.result
    change_length(controller)
    assert not snapshot.is_current_for(vm.net, vm.project.methodology)
    rows = vm.fault_rows()
    assert rows and all(row.i3_ka is None and row.iabc_ka is None for row in rows)
    assert vm.current_result is None
    assert "устар" in vm.report_text().lower()
    with pytest.raises(ValueError, match="устар"):
        vm.require_current_result()
    assert vm.recalculate()
    assert vm.current_result is vm.result
    assert vm.fault_rows()[0].i3_ka != pytest.approx(before)


@pytest.mark.parametrize("change", ["source", "ct", "methodology", "switch"])
def test_every_electrical_input_invalidates_all_result_consumers(change):
    vm, controller = opened("ps_severnaya")
    protected = next(b for b in vm.net.branches.values() if b.has_protection_point)
    vm.select("branch", protected.id)
    if change == "source":
        edit_payload(controller, lambda p: p.get("kind") == "source", s_kz_max=900.0)
    elif change == "ct":
        edit_payload(controller, lambda p: bool(p.get("ct_ratio")), ct_ratio=[2000, 5])
    elif change == "methodology":
        vm.project.methodology.data["mtz"]["k_ots"]["value"] = 1.2
    else:
        # Старый проект управляет выключателями через пользовательский
        # расчётный режим, а не через native switch.position аппарата.
        vm.set_branch_enabled(protected.id, False)
    assert vm.current_result is None
    assert vm.setting_rows() == []
    assert vm.selected_checks() == []
    assert vm.selectivity_pairs() == []
    assert vm.status_counts()[OK] == 0
    assert vm.status_counts()[UNRESOLVED] > 0
    assert all(node.i3_a is None and node.i2_a is None for node in vm.electrical_map().nodes.values())
    assert ("черновик" if change == "switch" else "устар") in " ".join(vm.warnings()).lower()


def test_geometry_selection_mode_and_fault_type_reuse_current_snapshot(monkeypatch):
    vm, controller = opened()
    snapshot = vm.result
    monkeypatch.setattr("rza_calc.gui.view_model.run_input", lambda *_: pytest.fail("No recalculation for presentation choices"))
    representation = next(iter(controller.diagram.representations))
    controller.set_label(representation, text="Подпись для проверки")
    vm.select_mode("min")
    vm.select_fault_type("2ph_g")
    assert vm.current_result is snapshot
    assert vm.fault_rows()[0].iabc_ka is not None
    controller.undo()
    controller.redo()
    assert vm.current_result is snapshot


def test_undo_redo_restore_and_invalidate_electrical_snapshot():
    vm, controller = opened()
    snapshot = vm.result
    change_length(controller)
    assert vm.current_result is None
    controller.undo()
    assert vm.current_result is snapshot
    controller.redo()
    assert vm.current_result is None


def test_cached_fault_details_cannot_bypass_freshness_check():
    vm, controller = opened()
    cached_rows = vm.fault_rows()
    assert "Фаза A" in vm.fault_details_text(cached_rows)
    change_length(controller)
    details = vm.fault_details_text(cached_rows)
    assert "устар" in details.lower()
    assert "Фаза A" not in details


@pytest.mark.parametrize("change", ["recalculate", "fault_type", "node", "mode"])
def test_cached_fault_details_follow_the_current_result_and_selection(change):
    vm, controller = opened()
    cached_rows = vm.fault_rows()
    old_row = next(row for row in cached_rows if row.mode_id == vm.mode_id)
    if change == "recalculate":
        change_length(controller)
        assert vm.recalculate()
    elif change == "fault_type":
        vm.select_fault_type("2ph_g")
    elif change == "node":
        source = next(b for b in vm.net.branches.values() if b.kind == "source")
        vm.select("node", source.node_to)
    else:
        vm.select_mode("min")
    assert vm.current_result is not None
    current_row = next(row for row in vm.fault_rows() if row.mode_id == vm.mode_id)
    assert abs(current_row.iabc_ka[0]) != pytest.approx(abs(old_row.iabc_ka[0]))
    details = vm.fault_details_text(cached_rows)
    value = f"{abs(current_row.iabc_ka[0]):.4f}".rstrip("0").rstrip(".").replace(".", ",")
    assert f"Фаза A: |I| = {value} кА" in details
    assert details == vm.fault_details_text()


def test_editing_operating_state_invalidates_snapshot_even_after_mode_selection():
    from dataclasses import replace
    from rza_calc.domain.electrical import EquipmentAvailability
    vm, controller = opened()
    line = next(b for b in vm.net.branches.values() if b.kind == "line")
    mode = next(row for row in vm.operating_modes() if row.calculation_mode_id == 'min')
    draft = controller.operating_mode_draft(mode.state_id)
    eid = controller.resolve_operating_mode_equipment(line.id)
    draft = replace(draft, availability={**draft.availability, eid: EquipmentAvailability.OUT_OF_SERVICE})
    controller.apply_operating_mode_preview(controller.preview_operating_mode(draft))
    vm.select_mode("max")
    vm.select_fault_type("1ph_g")
    assert vm.current_result is None
    assert all(row.iabc_ka is None for row in vm.fault_rows())


def test_inplace_edits_leave_the_historical_solver_network_and_modes_unchanged():
    from rza_calc.core.fingerprint import network_fingerprint

    vm, controller = opened()
    result = vm.result
    frozen_fingerprint = network_fingerprint(result.ctx.net)
    before = result.ctx.solvers["min"].at("FAULT").i3
    live_line = next(b for b in vm.net.branches.values() if b.kind == "line")
    # Stage 4 makes the canonical input authoritative; editing the derived
    # Network is no longer a supported project mutation.
    change_length(controller)
    assert vm.current_result is None
    assert result.ctx.net is not vm.net
    assert network_fingerprint(result.ctx.net) == frozen_fingerprint
    assert result.ctx.solvers["min"].at("FAULT").i3 == pytest.approx(before)


def test_incomplete_protection_remains_visible_when_another_check_failed():
    vm, _ = opened("ps_severnaya")
    protected = next(b for b in vm.net.branches.values() if b.has_protection_point)
    vm.select("branch", protected.id)
    protection = vm.result.results[protected.id]["МТЗ"]
    protection.checks.append(Check("Контрольная проверка чувствительности", .5, 1.5, FAIL))
    protection.record_coverage("резервирование", 1, 2, ["Вторая точка не рассчитана"])
    protection.recompute_status()
    assert protection.status == FAIL
    assert not vm.current_result.is_complete
    assert any("Полнота:" in name and value == "—" and status == UNRESOLVED
               for name, value, status in vm.selected_checks())
    assert "проверка не завершена" in vm.report_text()


def test_unfinished_new_equipment_blocks_old_answer_even_if_projection_unchanged():
    vm, controller = opened()
    snapshot = vm.result
    controller.add_equipment("builtin.transformer_2w", "Новый незаполненный Т-2", x=800, y=800)
    assert vm.project.calculation_blockers
    # Unfinished native equipment is excluded from the legacy projection.
    assert snapshot.is_current_for(vm.net, vm.project.methodology)
    assert vm.current_result is None
    assert "заблокирован" in vm.report_text().lower()
    assert all(row.iabc_ka is None for row in vm.fault_rows())
    assert not vm.recalculate()
    assert vm.result is None


def test_failed_recalculation_cannot_leave_old_ok_or_currents(monkeypatch):
    vm, _ = opened("ps_severnaya")
    def fail(*_):
        raise ValueError("Контрольная ошибка нового расчёта")
    monkeypatch.setattr("rza_calc.gui.view_model.run_input", fail)
    assert not vm.recalculate()
    assert vm.current_result is None
    assert vm.result is None
    assert vm.status_counts()[OK] == 0
    assert not any(row.i3_ka is not None for row in vm.fault_rows())
    assert "Контрольная ошибка нового расчёта" in vm.report_text()


@pytest.mark.parametrize("change", ["network", "methodology"])
def test_snapshot_changed_during_calculation_is_rejected_at_publication(monkeypatch, change):
    vm, controller = opened()
    def delayed(captured):
        candidate = run_input(captured)
        if change == "network":
            change_length(controller)
        else:
            vm.project.methodology.data["mtz"]["k_ots"]["value"] = 1.2
        return candidate
    monkeypatch.setattr("rza_calc.gui.view_model.run_input", delayed)
    assert not vm.recalculate()
    assert vm.current_result is None and vm.result is None
    assert "измен" in vm.calculation_error.lower()


def test_late_older_calculation_cannot_erase_a_newer_result(monkeypatch):
    vm, controller = opened()
    calls = []
    latest = []
    def delayed(captured):
        candidate = run_input(captured)
        calls.append(candidate)
        if len(calls) == 1:
            change_length(controller)
            assert vm.recalculate()
            latest.append(vm.result)
        return candidate
    monkeypatch.setattr("rza_calc.gui.view_model.run_input", delayed)
    assert not vm.recalculate()
    assert vm.current_result is latest[0]
    assert not vm.calculation_error


def test_late_older_failure_cannot_erase_a_newer_result(monkeypatch):
    vm, controller = opened()
    calls = []
    latest = []
    def delayed(captured):
        calls.append(None)
        if len(calls) == 1:
            change_length(controller)
            assert vm.recalculate()
            latest.append(vm.result)
            raise ValueError("Ошибка предыдущей попытки")
        return run_input(captured)
    monkeypatch.setattr("rza_calc.gui.view_model.run_input", delayed)
    assert not vm.recalculate()
    assert vm.current_result is latest[0]
    assert not vm.calculation_error


def test_real_window_updates_after_controller_undo_redo_and_tab_type_mode_changes(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from rza_calc.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, tb: errors.append(str(error)))
    vm, _ = opened()
    vm.select_fault_type("2ph_g")
    window = MainWindow(vm)
    window.show()
    app.processEvents()
    try:
        change_length(window.editor_controller)
        app.processEvents()
        assert "устар" in window.bottom.fault_table.item(0, 4).text().lower()
        assert window.bottom.fault_table.item(0, 4).text().count("Результаты устарели") == 1
        window.workspace_tabs.setCurrentIndex(1)
        window.bottom.fault_type_combo.setCurrentIndex(window.bottom.fault_type_combo.findData("2ph"))
        window.header.mode_buttons["min"].click()
        app.processEvents()
        assert "устар" in window.inspector.fault_table.item(0, 3).text().lower()
        assert "устар" in window.bottom.report_text.toPlainText().lower()
        assert window.bottom.selectivity_table.rowCount() == 0
        window.editor_controller.undo()
        app.processEvents()
        assert window.bottom.fault_table.item(0, 2).text() != "—"
        window.editor_controller.redo()
        app.processEvents()
        assert window.bottom.fault_table.item(0, 2).text() == "—"
        window.header.recalcRequested.emit()
        from PySide6.QtTest import QTest
        import time
        deadline = time.monotonic() + 5
        while window._calculation_worker is not None and time.monotonic() < deadline:
            QTest.qWait(5)
        assert window._calculation_worker is None
        assert window.bottom.fault_table.item(0, 2).text() != "—"
    finally:
        window.close()
        app.processEvents()
    assert not errors
