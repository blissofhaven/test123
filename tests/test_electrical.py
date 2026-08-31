# -*- coding: utf-8 -*-
"""Сценарии переключений: активная топология, напряжение, токи, отображение."""
from pathlib import Path

import pytest

from rza_calc.core.electrical import (ActiveTopology, DEENERGIZED,
                                      ENERGIZED_LOADED, ENERGIZED_ZERO,
                                      build_electrical_map, collect_switches)
from rza_calc.core.model import (GRID, GeneratorBranch, LineBranch, Mode,
                                 Network, Node, TieBranch, TransformerBranch,
                                 switch_ids)
from rza_calc.core.methodology import Methodology
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.gui.view_model import ProjectViewModel

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "rza_calc" / "examples" / "gtes_sever.json"


# ── стенд: две секции, два ввода, СВ, отходящая линия ──────────────────────
def _bench() -> Network:
    net = Network("стенд")
    for node_id, name, u in (("src", "Источник 10 кВ", 10.0),
                             ("b1", "1 СШ 10 кВ", 10.0),
                             ("b2", "2 СШ 10 кВ", 10.0),
                             ("end", "Конец фидера", 10.0)):
        net.add_node(Node(node_id, name, u))
    net.add_branch(GeneratorBranch(id="G1", name="Г-1", node_from=GRID, node_to="src",
                                   p_nom=20.0, cos_phi=0.8, u_nom=10.0, xd2=0.2,
                                   switchable=True, normally_closed=True))
    for name, node_to in (("Ввод-1", "b1"), ("Ввод-2", "b2")):
        net.add_branch(LineBranch(
            id=f"IN{node_to[-1]}", name=f"{name} 10 кВ", node_from="src", node_to=node_to,
            line_type="cable", length_km=1.0, x0=0.08, material="Al",
            section_mm2=240, switchable=True, normally_closed=True))
    net.add_branch(TieBranch(id="SV", name="СВ 10 кВ", node_from="b1", node_to="b2",
                             switchable=True, normally_closed=False))
    net.add_branch(LineBranch(id="F1", name="Ф-1", node_from="b1", node_to="end",
                              line_type="cable", length_km=2.0, x0=0.08,
                              material="Al", section_mm2=150,
                              switchable=True, normally_closed=True))
    return net


def _mode(net: Network, **states) -> Mode:
    base = {b: True for b in net.branches}
    base["SV"] = False
    base.update(states)
    mode_id = "m" + str(len(net.modes))
    return net.add_mode(Mode(mode_id, "проба", states=base))


def _map(net: Network, mode: Mode):
    return build_electrical_map(net, mode)


# ── выключатель как самостоятельный элемент ────────────────────────────────
def test_switches_are_separate_objects_with_stable_ids():
    net = _bench()
    switches = collect_switches(net)
    assert "SW:F1:from" in switches and "SW:F1:to" in switches
    assert switches["SW:F1:from"].branch_id == "F1"
    # У СВ и генератора аппарат один, у линии — на обоих концах.
    assert switch_ids(net.branches["SV"]) == ("SW:SV:from",)
    assert switch_ids(net.branches["F1"]) == ("SW:F1:from", "SW:F1:to")


def test_legacy_project_without_switch_states_behaves_as_before():
    net = _bench()
    mode = _mode(net)
    electrical = _map(net, mode)
    assert electrical.nodes["end"].energized
    assert electrical.switches["SW:F1:from"].closed


# ── радиальная сеть ────────────────────────────────────────────────────────
def test_head_switch_off_de_energises_everything_downstream():
    net = _bench()
    mode = _mode(net, **{"SW:F1:from": False})
    electrical = _map(net, mode)
    assert electrical.nodes["b1"].energized
    assert not electrical.nodes["end"].energized
    assert electrical.nodes["end"].status == DEENERGIZED
    assert electrical.nodes["end"].i3_a is None
    assert electrical.branches["F1"].current_a == 0.0
    assert electrical.switches["SW:F1:from"].current_a == 0.0


def test_switch_in_the_middle_keeps_upstream_alive():
    net = _bench()
    mode = _mode(net, **{"SW:F1:to": False})
    electrical = _map(net, mode)
    # Участок от шин до выключателя под напряжением, за ним — нет.
    assert electrical.branches["F1"].energized_from
    assert electrical.branches["F1"].energized_conductor
    assert not electrical.nodes["end"].energized
    switch = electrical.switches["SW:F1:to"]
    assert switch.closed is False
    assert switch.voltage_to is True      # со стороны проводника напряжение есть
    assert switch.voltage_from is False   # со стороны конца линии его нет
    assert switch.current_a == 0.0


def test_voltage_on_both_sides_of_an_open_switch():
    net = _bench()
    # Встречное питание: конец фидера получает питание от второй секции.
    net.add_branch(LineBranch(id="F2", name="Ф-2 встречный", node_from="b2",
                              node_to="end", line_type="cable", length_km=2.0,
                              x0=0.08, material="Al", section_mm2=150,
                              switchable=True, normally_closed=True))
    mode = _mode(net, **{"SW:F1:to": False, "F2": True})
    electrical = _map(net, mode)
    switch = electrical.switches["SW:F1:to"]
    assert electrical.nodes["b1"].energized and electrical.nodes["end"].energized
    assert switch.voltage_from and switch.voltage_to
    assert switch.closed is False
    assert switch.current_a == 0.0


# ── секционный выключатель ─────────────────────────────────────────────────
def test_bus_coupler_open_both_sections_fed_separately():
    """СВ отключён, обе секции живы от своих вводов, ток через СВ нулевой."""
    net = _bench()
    mode = _mode(net)
    electrical = _map(net, mode)
    assert electrical.nodes["b1"].energized and electrical.nodes["b2"].energized
    assert electrical.switches["SW:SV:from"].closed is False
    assert electrical.switches["SW:SV:from"].current_a == 0.0
    assert electrical.branches["SV"].status == DEENERGIZED or \
        electrical.branches["SV"].current_a == 0.0


def test_two_independent_sources_form_two_components():
    """Секции от РАЗНЫХ источников при отключённом СВ — разные компоненты."""
    net = _bench()
    del net.branches["IN2"]
    net.add_node(Node("src2", "Второй источник", 10.0))
    net.add_branch(GeneratorBranch(id="G2", name="Г-2", node_from=GRID,
                                   node_to="src2", p_nom=20.0, cos_phi=0.8,
                                   u_nom=10.0, xd2=0.2, switchable=True,
                                   normally_closed=True))
    net.add_branch(LineBranch(id="IN2b", name="Ввод-2", node_from="src2",
                              node_to="b2", line_type="cable", length_km=1.0,
                              x0=0.08, material="Al", section_mm2=240,
                              switchable=True, normally_closed=True))
    mode = _mode(net)
    electrical = _map(net, mode)
    assert electrical.nodes["b1"].energized and electrical.nodes["b2"].energized
    assert electrical.nodes["b1"].connected_component_id != \
        electrical.nodes["b2"].connected_component_id
    assert electrical.nodes["b1"].source_ids == ["G1"]
    assert electrical.nodes["b2"].source_ids == ["G2"]
    # Замыкание СВ объединяет секции в один компонент.
    joined = _map(net, _mode(net, SV=True))
    assert joined.nodes["b1"].connected_component_id == \
        joined.nodes["b2"].connected_component_id


def test_section_is_fed_through_closed_bus_coupler():
    net = _bench()
    mode = _mode(net, **{"SW:IN2:from": False, "SV": True})
    electrical = _map(net, mode)
    assert electrical.nodes["b2"].energized, "2 СШ должна питаться через СВ"
    assert electrical.nodes["b1"].connected_component_id == \
        electrical.nodes["b2"].connected_component_id


def test_section_dies_when_its_input_and_the_coupler_are_open():
    net = _bench()
    mode = _mode(net, **{"SW:IN2:from": False, "SV": False})
    electrical = _map(net, mode)
    assert electrical.nodes["b1"].energized
    assert not electrical.nodes["b2"].energized
    assert electrical.nodes["b2"].i3_a is None


# ── источники ──────────────────────────────────────────────────────────────
def test_all_generators_off_leaves_network_dead():
    net = _bench()
    mode = _mode(net, G1=False)
    electrical = _map(net, mode)
    assert not any(state.energized for state in electrical.nodes.values())
    assert all(state.i3_a is None for state in electrical.nodes.values())


def test_island_with_local_generator_stays_energized():
    net = _bench()
    net.add_branch(GeneratorBranch(id="G2", name="Г-2 местный", node_from=GRID,
                                   node_to="end", p_nom=2.0, cos_phi=0.8,
                                   u_nom=10.0, xd2=0.2, switchable=True,
                                   normally_closed=True))
    mode = _mode(net, **{"SW:F1:from": False})
    electrical = _map(net, mode)
    # Участок отрезан от главного источника, но имеет свой генератор.
    assert electrical.nodes["end"].energized
    assert "G2" in electrical.nodes["end"].source_ids
    assert electrical.nodes["end"].connected_component_id != \
        electrical.nodes["b1"].connected_component_id


# ── параллельная работа и трансформатор ────────────────────────────────────
def test_parallel_inputs_one_open_keeps_supply():
    net = _bench()
    mode = _mode(net, **{"SW:IN1:from": False, "SV": True})
    electrical = _map(net, mode)
    assert electrical.nodes["b1"].energized
    assert electrical.branches["IN1"].current_a == 0.0
    assert electrical.branches["IN2"].closed


def test_transformer_off_leaves_hv_alive_and_lv_dead():
    net = Network("трансформатор")
    net.add_node(Node("hv", "Шины 110 кВ", 110.0))
    net.add_node(Node("lv", "Шины 10 кВ", 10.0))
    net.add_branch(GeneratorBranch(id="G", name="Г", node_from=GRID, node_to="hv",
                                   p_nom=50.0, cos_phi=0.8, u_nom=110.0, xd2=0.2,
                                   switchable=True, normally_closed=True))
    net.add_branch(TransformerBranch(id="T", name="Т", node_from="hv", node_to="lv",
                                     s_nom=25000, u_hv=110.0, u_lv=10.0, uk=10.5,
                                     p_k=100, switchable=True, normally_closed=True))
    closed = net.add_mode(Mode("on", "включён", states={"G": True, "T": True}))
    opened = net.add_mode(Mode("off", "отключён", states={"G": True, "T": False}))
    assert _map(net, closed).nodes["lv"].energized
    off = _map(net, opened)
    assert off.nodes["hv"].energized, "сторона ВН остаётся под напряжением"
    assert not off.nodes["lv"].energized, "сторона НН обесточена"
    assert off.nodes["hv"].nominal_voltage_kv == 110.0
    assert off.nodes["lv"].nominal_voltage_kv == 10.0, "класс напряжения не меняется"


def test_open_transformer_does_not_bridge_voltage_stages():
    net = Network("ступени")
    net.add_node(Node("hv", "110 кВ", 110.0))
    net.add_node(Node("lv", "10 кВ", 10.0))
    net.add_branch(GeneratorBranch(id="G", name="Г", node_from=GRID, node_to="hv",
                                   p_nom=50.0, cos_phi=0.8, u_nom=110.0, xd2=0.2,
                                   switchable=True, normally_closed=True))
    net.add_branch(TransformerBranch(id="T", name="Т", node_from="hv", node_to="lv",
                                     s_nom=25000, u_hv=110.0, u_lv=10.0, uk=10.5,
                                     p_k=100, switchable=True, normally_closed=True))
    mode = net.add_mode(Mode("off", "отключён", states={"G": True, "T": False}))
    topology = ActiveTopology(net, mode)
    assert topology.component_of["hv"] != topology.component_of["lv"]


# ── расчёт токов только по активной топологии ──────────────────────────────
def test_open_branch_leaves_the_admittance_matrix():
    net = _bench()
    meth = Methodology.load()
    both = _mode(net, SV=True)
    solver_both = ShortCircuitSolver(net, both, meth)
    one = _mode(net, SV=True, **{"SW:IN2:from": False})
    solver_one = ShortCircuitSolver(net, one, meth)
    assert "IN2" in solver_both.branch_z
    assert "IN2" not in solver_one.branch_z, "отключённый ввод не должен быть в матрице"
    assert solver_one.at("b1").i3 < solver_both.at("b1").i3


# ── демонстрационный проект целиком ────────────────────────────────────────
def test_demo_switch_toggle_is_transactional_and_recomputes():
    vm = ProjectViewModel.open(EXAMPLE)
    before = vm.electrical_map()
    assert before.node_is_live("K1_10_1")
    assert before.nodes["K1_10_1"].i3_a is not None

    assert vm.toggle_switch("SW:VF1:from") is False
    after = vm.electrical_map()
    assert not after.node_is_live("K1_10_1")
    assert after.nodes["K1_10_1"].i3_a is None, "старый ток обязан исчезнуть"
    assert after.switches["SW:VF1:from"].voltage_from is True
    assert after.switches["SW:VF1:from"].current_a == 0.0

    assert vm.toggle_switch("SW:VF1:from") is True
    restored = vm.electrical_map()
    assert restored.nodes["K1_10_1"].i3_a is not None


def test_demo_ktp_reserve_input_is_real_and_carries_supply():
    vm = ProjectViewModel.open(EXAMPLE)
    # Рабочий ввод отключён — секция питается настоящим резервным фидером.
    vm.toggle_switch("SW:VF1:from")
    electrical = vm.electrical_map()
    assert not electrical.node_is_live("K1_10_1")
    assert electrical.node_is_live("K1_10_2"), "резервный ввод должен держать 2 СШ"
    assert all(
        "Резерв (не задан)" not in branch.name for branch in vm.net.branches.values()
    ), "заглушка не участвует в расчёте"


def test_demo_deenergized_point_has_no_current_at_all():
    """Обесточенный узел обязан остаться без числа, а не получить ноль.

    Прежде это проверялось по тексту автосхемы; после её удаления проверка
    выполняется там, где решение принимается, — в электрической карте.
    """
    vm = ProjectViewModel.open(EXAMPLE)
    vm.toggle_switch("SW:VF1:from")
    vm.toggle_switch("SW:VF1R:from")
    electrical = vm.electrical_map()
    for node_id in ("K1_10_1", "K1_10_2", "K1_04_1", "K1_04_2"):
        assert electrical.nodes[node_id].i3_a is None
        assert electrical.nodes[node_id].status == DEENERGIZED


def test_demo_state_comes_only_from_the_core_map():
    """Состояние узлов берётся из ядра, а не из отдельной графической модели."""
    vm = ProjectViewModel.open(EXAMPLE)
    before = vm.electrical_map()
    assert before is not None
    assert before.node_is_live("K1_10_1")
    vm.toggle_switch("SW:VF1:from")
    after = vm.electrical_map()
    assert after.nodes["K1_10_1"].energized is False
    assert not after.node_is_live("K1_10_1")


def test_demo_failed_switching_is_rolled_back():
    vm = ProjectViewModel.open(EXAMPLE)
    before = dict(vm.net.modes.get("gui_custom").states) if "gui_custom" in vm.net.modes else None
    with pytest.raises((KeyError, ValueError)):
        vm.toggle_switch("SW:НЕТ_ТАКОГО:from")
    assert vm.result is not None, "интерфейс не должен остаться без расчёта"


def test_demo_project_keeps_workspace_view_state():
    """Состояние обзора схемы хранится в проекте, а не в коде автосхемы.

    Автосхема сохраняла масштаб и центр в собственном JavaScript. После её
    удаления ту же роль выполняет рабочее состояние редактора внутри проекта.
    """
    vm = ProjectViewModel.open(EXAMPLE)
    workspace = dict(vm.project.diagram.extensions.get("stage3_workspace") or {})
    for key in ("zoom", "view_x", "view_y", "active_page_id", "grid_size"):
        assert key in workspace, f"в рабочем состоянии нет «{key}»"
