"""Four-fault query integration: selection, phase results and honest errors."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from rza_calc import cli
from rza_calc.core.fault_types import FaultType
from rza_calc.core.short_circuit import ShortCircuitStatusError
from rza_calc.gui.view_model import ProjectViewModel


class QuerySolver:
    def __init__(self):
        self.queries = []
        self.error = None

    def at(self, node_id):
        return SimpleNamespace(i3=9.0, i2=7.8)

    def fault_at(self, node_id, spec):
        self.queries.append((node_id, spec.kind))
        if self.error is not None:
            raise self.error
        magnitude = list(FaultType).index(spec.kind) + 1.0
        return SimpleNamespace(
            iabc_ka=(complex(magnitude), -magnitude * 1j, -magnitude),
            vabc_kv=(0j, 4 + 3j, -4 + 3j),
            residual_current_ka=-magnitude * 1j,
            assumptions=("Проверенный тестовый источник фазных результатов",),
        )


def make_vm():
    solver = QuerySolver()
    node = SimpleNamespace(id="bus", name="Контрольная шина", u_nom=10.0)
    mode = SimpleNamespace(id="max", name="Максимальный", system="max")
    net = SimpleNamespace(name="Тестовая сеть", nodes={node.id: node},
                          modes={mode.id: mode}, branches={}, loads={})
    result = SimpleNamespace(ctx=SimpleNamespace(net=net, solvers={"max": solver}, errors={}),
                             results={}, pairs=[], warnings=[], all_results=lambda: [], is_complete=True)
    methodology = object()
    result.is_current_for = lambda current_net, current_method: (
        current_net is net and current_method is methodology
    )
    vm = ProjectViewModel.__new__(ProjectViewModel)
    vm.project = SimpleNamespace(network=net, methodology=methodology, calculation_blockers=[])
    vm._applied_network = net
    vm.result = result
    vm.path = Path("synthetic.json")
    vm.selected_kind, vm.selected_id = "node", "bus"
    vm.selected_fault_type = FaultType.THREE_PHASE
    vm.mode_id = "max"
    vm.calculation_error = ""
    return vm, solver


@pytest.mark.parametrize("kind", list(FaultType))
def test_view_model_queries_selected_type_and_retains_legacy_fields(kind):
    vm, solver = make_vm()
    vm.select_fault_type(kind.value)
    row, = vm.fault_rows()
    assert solver.queries == [("bus", kind)]
    assert (row.i3_ka, row.i2_ka) == (9.0, 7.8)
    assert row.fault_type is kind
    assert row.vabc_kv == (0j, 4 + 3j, -4 + 3j)
    assert row.iabc_ka[0] == list(FaultType).index(kind) + 1.0
    detail = vm.fault_details_text([row])
    assert "Фаза A" in detail and "Фаза C" in detail and "|3I0|" in detail
    assert "остаточное" in detail


def test_view_model_preserves_missing_sequence_diagnostic_without_fake_phase_values():
    vm, solver = make_vm()
    vm.select_fault_type("1ph_g")
    solver.error = ShortCircuitStatusError("MISSING_SEQUENCE_DATA", "Источник S1: требуется z0_ohm")
    row, = vm.fault_rows()
    assert row.iabc_ka is None and row.vabc_kv is None
    assert row.status_code == "MISSING_SEQUENCE_DATA"
    details = vm.fault_details_text([row])
    assert "Нет исходных данных: Источник S1" in details
    assert "MISSING_SEQUENCE_DATA" not in details
    assert "z0_ohm" in vm.report_text()


@pytest.mark.parametrize("code,label", [
    ("MISSING_SEQUENCE_DATA", "Нет исходных данных"),
    ("NOT_ENERGIZED", "Нет питания"),
    ("NO_ZERO_SEQUENCE_RETURN_PATH", "Нет контура нулевой последовательности"),
    ("NO_SEQUENCE_RETURN_PATH", "Нет контура последовательности"),
    ("NUMERIC_FAILURE", "Численная ошибка"),
    ("INVALID_INPUT", "Ошибка данных"),
    ("MODE_UNAVAILABLE", "Режим не рассчитан"),
    ("UNEXPECTED_CODE", "Расчёт не выполнен"),
])
def test_gui_fault_status_is_human_readable(code, label):
    vm, solver = make_vm()
    solver.error = ShortCircuitStatusError(code, "Пояснение с именем аппарата T1")
    row, = vm.fault_rows()
    text = vm.fault_details_text([row])
    assert label in text and "аппарата T1" in text
    assert code not in text


@pytest.mark.parametrize("kind", list(FaultType))
def test_cli_selected_query_outputs_requested_phase_quantities(kind):
    vm, solver = make_vm()
    text, unresolved = cli.cmd_selected_sc(vm.result, fault_type=kind.value,
                                           node_id="bus", mode_id="max")
    assert not unresolved
    assert solver.queries == [("bus", kind)]
    assert all(label in text for label in ("IA:", "IB:", "IC:", "UA:", "UB:", "UC:", "3I0:"))
    assert "кА" in text and "кВ" in text and kind.value in text


def test_cli_does_not_mislabel_missing_sequence_as_not_energized():
    vm, solver = make_vm()
    solver.error = ShortCircuitStatusError("MISSING_SEQUENCE_DATA", "Линия L1: требуется x2")
    text, unresolved = cli.cmd_selected_sc(vm.result, fault_type="2ph")
    assert unresolved and "MISSING_SEQUENCE_DATA" in text and "Линия L1" in text
    assert "не запитан" not in text


@pytest.mark.parametrize("args", [
    ["--bad"], ["--fault-type", "4ph"], ["--node"], ["--mode="],
    ["--node", "bus", "--node", "bus"], ["unexpected"],
])
def test_cli_rejects_invalid_options_before_calculation(monkeypatch, capsys, args):
    monkeypatch.setattr(cli, "run", lambda *_: pytest.fail("Invalid input must not start calculation"))
    assert cli.main(["example", "sc", *args]) == cli.EXIT_INPUT_ERROR
    assert "Ошибка запроса" in capsys.readouterr().out


def test_cli_plain_sc_keeps_legacy_output_and_selected_errors_have_exit_three(monkeypatch, capsys):
    vm, solver = make_vm()
    monkeypatch.setattr(cli, "run", lambda *_: vm.result)
    monkeypatch.setattr(cli, "cmd_sc", lambda result: "legacy-sc-output")
    monkeypatch.setattr(cli, "exit_code", lambda *_: cli.EXIT_OK)
    assert cli.main(["example", "sc"]) == cli.EXIT_OK
    assert "legacy-sc-output" in capsys.readouterr().out
    assert not solver.queries
    solver.error = ShortCircuitStatusError("MISSING_SEQUENCE_DATA", "Нет Z0 источника S1")
    assert cli.main(["example", "sc", "--fault-type", "1ph_g", "--node", "bus", "--mode", "max"]) == cli.EXIT_UNRESOLVED
    assert "Нет Z0 источника S1" in capsys.readouterr().out


@pytest.mark.parametrize("option,value", [("node_id", "absent"), ("mode_id", "absent")])
def test_cli_rejects_unknown_requested_objects(option, value):
    vm, _ = make_vm()
    with pytest.raises(cli.CliInputError, match="не найден"):
        cli.cmd_selected_sc(vm.result, **{option: value})


@pytest.mark.parametrize("kind", list(FaultType))
def test_real_solver_is_connected_to_view_model_and_cli(kind):
    from rza_calc.core.engine import run
    from rza_calc.core.methodology import Methodology
    from rza_calc.core.model import GRID, Mode, Network, Node, SourceBranch

    net = Network("Явные сети последовательностей")
    net.add_node(Node("bus", "Контрольная шина", 10.0))
    mode = Mode("max", "Максимальный")
    net.add_mode(mode)
    net.add_branch(SourceBranch(
        id="source", name="Питающая система", node_from=GRID, node_to="bus",
        s_kz_max=300, s_kz_min=200, x_r_ratio=10, r2_ohm=.2, x2_ohm=1.2,
        r0_ohm=.3, x0_ohm=2.1, sequence_reference_kv=10.5,
        zero_sequence_connection="series",
    ))
    vm, _ = make_vm()
    vm.project.network = net
    vm.project.methodology = Methodology.load()
    vm._applied_network = net
    vm.result = run(net, vm.project.methodology)
    vm.select_fault_type(kind)
    row, = vm.fault_rows()
    assert not row.error, row.error
    assert row.iabc_ka is not None and max(abs(value) for value in row.iabc_ka) > 0
    if kind is FaultType.LINE_GROUND:
        assert abs(row.iabc_ka[1]) < 1e-12 and abs(row.iabc_ka[2]) < 1e-12
    if kind is FaultType.LINE_LINE:
        assert abs(row.iabc_ka[0]) < 1e-12 and abs(row.residual_current_ka) < 1e-12
    text, unresolved = cli.cmd_selected_sc(vm.result, fault_type=kind.value)
    assert not unresolved and "IA:" in text and "UA:" in text


def test_actual_type_combo_updates_both_result_panels_and_error_text():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from rza_calc.gui.main_window import BottomPanel, InspectorPanel, MainWindow

    app = QApplication.instance() or QApplication([])
    vm, solver = make_vm()
    bottom, inspector = BottomPanel(vm), InspectorPanel(vm)
    owner = SimpleNamespace(vm=vm, bottom=bottom, inspector=inspector,
                            statusBar=lambda: SimpleNamespace(showMessage=lambda *_: None))
    bottom.faultTypeSelected.connect(lambda value: MainWindow._select_fault_type(owner, value))
    try:
        assert bottom.fault_type_combo.count() == 4
        for kind in FaultType:
            bottom.fault_type_combo.setCurrentIndex(bottom.fault_type_combo.findData(kind.value))
            app.processEvents()
            assert vm.selected_fault_type is kind
            assert vm.fault_type_label() in inspector.fault_title.text()
            assert vm.fault_type_label() in bottom.fault_details.toPlainText()
            assert inspector.fault_table.item(0, 1).text() == bottom.fault_table.item(0, 2).text()
        solver.error = ShortCircuitStatusError("MISSING_SEQUENCE_DATA", "Трансформатор T1: требуется схема нейтрали")
        bottom.refresh(vm)
        inspector.refresh(vm)
        assert "Трансформатор T1" in bottom.fault_table.item(0, 4).text()
        assert "Нет исходных данных" in bottom.fault_table.item(0, 4).text()
        assert "MISSING_SEQUENCE_DATA" not in bottom.fault_table.item(0, 4).text()
        assert "схема нейтрали" in inspector.fault_table.item(0, 3).text()
        assert "MISSING_SEQUENCE_DATA" not in bottom.fault_details.toPlainText()
        assert bottom.fault_table.item(0, 2).text() == "—"
    finally:
        bottom.close()
        inspector.close()
        app.processEvents()
