"""A table refresh reads one fresh network, regardless of its row count."""
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from rza_calc.core.fault_types import FaultType
from rza_calc.domain.electrical import thaw_json
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.main_window import BottomPanel
from rza_calc.gui.view_model import ProjectViewModel


class TableViewModel:
    """Keep the table's own network reads visible, outside result consumers."""

    selected_fault_type = FaultType.THREE_PHASE
    mode_id = ""

    def __init__(self, count):
        self.reads = 0
        self.network = SimpleNamespace(
            name="Сеть", branches={}, modes={},
            nodes={"n": SimpleNamespace(name="Узел")},
            loads={str(i): SimpleNamespace(name=f"Нагрузка {i}", node="n", p_kw=i, cos_phi=0.9)
                   for i in range(count)},
        )

    @property
    def net(self):
        self.reads += 1
        return self.network

    def status_counts(self): return {}
    def warnings(self): return []
    def selected_fault_node(self): return None
    def fault_rows(self): return []
    def fault_details_text(self, rows): return ""
    def setting_rows(self): return []
    def selectivity_pairs(self): return []
    def report_text(self): return ""


@pytest.mark.parametrize("count", (1, 1000))
def test_table_refresh_reads_one_network_and_next_refresh_gets_new_snapshot(count):
    qt = QApplication.instance() or QApplication([])
    vm = TableViewModel(count)
    panel = BottomPanel(vm)
    try:
        assert vm.reads == 1
        assert panel.loads_table.rowCount() == count
        assert panel.loads_table.item(count - 1, 1).text() == "Узел"
        vm.network = TableViewModel(2).network
        vm.network.nodes["n"].name = "Новый узел"
        vm.reads = 0
        panel.refresh(vm)
        assert vm.reads == 1
        assert panel.loads_table.rowCount() == 2
        assert panel.loads_table.item(1, 1).text() == "Новый узел"
    finally:
        panel.close()
        panel.deleteLater()
        qt.processEvents()


def test_real_electrical_edit_refreshes_table_and_preserves_stale_result_guard():
    qt = QApplication.instance() or QApplication([])
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/ps_severnaya.json"
    vm = ProjectViewModel.open(path)
    before = vm.net
    load = next(iter(before.loads.values()))
    vm.select("load", load.id)
    controller = ProjectEditorController(vm.project)
    equipment = next(item for item in controller.model.equipment.values()
                     if item.extensions.get("legacy_calculation", {}).get("legacy_id") == load.id)
    panel = BottomPanel(vm)
    try:
        payload = thaw_json(equipment.properties["legacy_payload"])
        payload["p_kw"] = load.p_kw + 123.0
        controller.set_equipment_property(equipment.id, "legacy_payload", payload)
        panel.refresh(vm)
        assert vm.net is not before
        row = next(i for i in range(panel.loads_table.rowCount())
                   if panel.loads_table.item(i, 0).text() == load.name)
        assert panel.loads_table.item(row, 2).text() == f"{load.p_kw + 123.0:g}"
        assert vm.current_result is None
        assert "устар" in panel.fault_details.toPlainText().lower()
        assert "устар" in panel.report_text.toPlainText().lower()
    finally:
        panel.close()
        panel.deleteLater()
        qt.processEvents()
