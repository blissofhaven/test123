# -*- coding: utf-8 -*-
"""
Карта электрических состояний сети.

Единственный источник истины о том, где есть напряжение и где течёт ток.
Схема ничего не выводит сама: она получает готовую карту и рисует её.

Порядок построения повторяет физику, а не картинку:

    положения выключателей → активная топология → электрически связанные
    компоненты → наличие источников → напряжение → токи → карта состояний.

Связность считается на РАСШИРЕННОМ графе: между шиной и проводником линии
стоит отдельная вершина-терминал, поэтому отключённый выключатель на одном
конце линии не обесточивает её второй конец. Матрица проводимостей при этом
остаётся прежней — ветвь входит в неё только когда замкнуты оба её
выключателя.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import (GRID, Branch, GeneratorBranch, Mode, Network, SourceBranch,
                    parse_switch_id, switch_ends, switch_ids)

# Состояния присоединения
DEENERGIZED = "deenergized"                  # напряжения нет
ENERGIZED_ZERO = "energized_zero_current"    # напряжение есть, ток нулевой
ENERGIZED_LOADED = "energized_loaded"        # есть напряжение и ток
UNKNOWN = "unknown"                          # расчёт невозможен из-за данных
ERROR = "error"                              # блокирующая ошибка
NOT_TRACKED = "not_tracked"                  # состояние аппарата не хранится


@dataclass(frozen=True)
class Switch:
    """Коммутационный аппарат как самостоятельный элемент топологии."""

    id: str
    branch_id: str
    end: str                 # 'from' | 'to'
    name: str
    tracked: bool            # состояние хранится в модели
    normally_closed: bool

    @property
    def bus_node_attr(self) -> str:
        return "node_from" if self.end == "from" else "node_to"


def collect_switches(net: Network) -> dict[str, Switch]:
    """Выключатели сети. Синтезируются из присоединений, идентификаторы
    устойчивы, поэтому режим может хранить состояние конкретного аппарата."""
    result: dict[str, Switch] = {}
    for branch in net.branches.values():
        tracked = bool(getattr(branch, "switchable", False))
        for end in switch_ends(branch):
            switch_id = f"SW:{branch.id}:{end}"
            side = "ВН" if end == "from" else "НН"
            result[switch_id] = Switch(
                id=switch_id, branch_id=branch.id, end=end,
                name=f"Выключатель {branch.name} ({side})",
                tracked=tracked,
                normally_closed=bool(getattr(branch, "normally_closed", True)),
            )
    return result


def switch_closed(switch: Switch, branch: Branch, mode: Mode) -> bool:
    """Положение аппарата. Явное состояние сильнее состояния всей ветви."""
    if switch.id in mode.states:
        return bool(mode.states[switch.id])
    return bool(mode.is_closed(branch))


# ──────────────────────────────────────────────────────────────────────────
#  Активная топология
# ──────────────────────────────────────────────────────────────────────────
def _terminal(branch_id: str, end: str) -> str:
    return f"@T:{branch_id}:{end}"


class ActiveTopology:
    """Граф связности режима с учётом положения каждого выключателя."""

    def __init__(self, net: Network, mode: Mode):
        self.net, self.mode = net, mode
        self.switches = collect_switches(net)
        self._adj: dict[str, set[str]] = {}
        self._closed: dict[str, bool] = {}
        self._build()
        self._components()

    # ---------- построение ----------
    def _link(self, a: str, b: str) -> None:
        self._adj.setdefault(a, set()).add(b)
        self._adj.setdefault(b, set()).add(a)

    def _build(self) -> None:
        net, mode = self.net, self.mode
        for node_id in net.nodes:
            self._adj.setdefault(node_id, set())
        self._adj.setdefault(GRID, set())

        for branch in net.branches.values():
            # GRID — опорная точка расчёта, а не физическая шина. Через неё
            # нельзя связывать острова: два участка со своими генераторами
            # должны оставаться разными компонентами.
            if GRID in (branch.node_from, branch.node_to):
                continue
            ends = switch_ends(branch)
            in_service = bool(mode.is_closed(branch))
            if not ends:
                # Аппарата нет: ветвь либо в работе, либо выведена целиком.
                if in_service:
                    self._link(branch.node_from, branch.node_to)
                continue

            from_t = _terminal(branch.id, "from")
            to_t = _terminal(branch.id, "to")
            # Проводник ветви существует всегда, пока оборудование не выведено.
            self._link(from_t, to_t)

            for end in ends:
                switch = self.switches[f"SW:{branch.id}:{end}"]
                closed = switch_closed(switch, branch, mode) and in_service
                self._closed[switch.id] = closed
                bus = branch.node_from if end == "from" else branch.node_to
                terminal = from_t if end == "from" else to_t
                if closed:
                    self._link(bus, terminal)
            if "to" not in ends:
                # Аппарат только с одной стороны: второй конец приварен к шине.
                self._link(to_t, branch.node_to)
            if "from" not in ends:
                self._link(from_t, branch.node_from)

    def _components(self) -> None:
        self.component_of: dict[str, int] = {}
        component = 0
        for start in self._adj:
            if start in self.component_of:
                continue
            stack, group = [start], []
            self.component_of[start] = component
            while stack:
                current = stack.pop()
                group.append(current)
                for other in self._adj.get(current, ()):
                    if other not in self.component_of:
                        self.component_of[other] = component
                        stack.append(other)
            component += 1
        self.component_count = component

        # Источники: внешняя система и работающие генераторы.
        self.sources_of: dict[int, list[str]] = {}
        for branch in self.net.branches.values():
            if not isinstance(branch, (SourceBranch, GeneratorBranch)):
                continue
            if not self.net.branch_conducting(branch, self.mode):
                continue
            node = branch.node_to if branch.node_from == GRID else branch.node_from
            group = self.component_of.get(node)
            if group is None:
                continue
            self.sources_of.setdefault(group, []).append(branch.id)
        self.energized_components = set(self.sources_of)

    # ---------- запросы ----------
    def is_energized(self, vertex: str) -> bool:
        group = self.component_of.get(vertex)
        return group is not None and group in self.energized_components

    def node_energized(self, node_id: str) -> bool:
        return self.is_energized(node_id)

    def terminal_energized(self, branch_id: str, end: str) -> bool:
        return self.is_energized(_terminal(branch_id, end))

    def conductor_energized(self, branch_id: str) -> bool:
        return (self.terminal_energized(branch_id, "from")
                or self.terminal_energized(branch_id, "to"))

    def switch_is_closed(self, switch_id: str) -> bool:
        return bool(self._closed.get(switch_id, False))

    def sources_for(self, vertex: str) -> list[str]:
        group = self.component_of.get(vertex)
        return list(self.sources_of.get(group, ())) if group is not None else []

    def energized_nodes(self) -> set[str]:
        live = {node_id for node_id in self.net.nodes if self.node_energized(node_id)}
        if self.is_energized(GRID):
            live.add(GRID)
        return live


# ──────────────────────────────────────────────────────────────────────────
#  Карта состояний
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class NodeElectricalState:
    node_id: str
    energized: bool
    nominal_voltage_kv: float
    calculated_voltage_kv: float | None
    connected_component_id: int | None
    source_ids: list[str]
    status: str
    error: str | None = None
    i3_a: float | None = None          # ток трёхфазного КЗ, А
    i2_a: float | None = None


@dataclass
class BranchElectricalState:
    branch_id: str
    available: bool                     # оборудование не выведено и данные верны
    closed: bool                        # проводит: замкнуты оба выключателя
    energized_from: bool
    energized_to: bool
    energized_conductor: bool
    current_a: float | None
    current_direction: str | None       # 'from_to' | 'to_from' | None
    power_flow: None = None             # установившийся режим ещё не считается
    status: str = UNKNOWN
    error: str | None = None


@dataclass
class SwitchElectricalState:
    switch_id: str
    branch_id: str
    end: str
    closed: bool
    tracked: bool
    voltage_from: bool                  # напряжение со стороны шин
    voltage_to: bool                    # напряжение со стороны проводника
    current_a: float | None
    status: str


@dataclass
class ElectricalMap:
    mode_id: str
    mode_name: str
    nodes: dict[str, NodeElectricalState] = field(default_factory=dict)
    branches: dict[str, BranchElectricalState] = field(default_factory=dict)
    switches: dict[str, SwitchElectricalState] = field(default_factory=dict)
    component_count: int = 0
    errors: list[str] = field(default_factory=list)

    def node_is_live(self, node_id: str) -> bool:
        state = self.nodes.get(node_id)
        return bool(state and state.energized)

    def branch_is_live(self, branch_id: str) -> bool:
        state = self.branches.get(branch_id)
        return bool(state and state.energized_conductor)


def build_electrical_map(net: Network, mode: Mode, solver: Any = None,
                         working_currents: dict[str, float] | None = None
                         ) -> ElectricalMap:
    """Собрать карту состояний для одного режима.

    solver — уже построенный ShortCircuitSolver того же режима (необязателен).
    Токи КЗ берутся только у запитанных точек: у обесточенной точки значения
    нет вообще, а не ноль.
    """
    topology = ActiveTopology(net, mode)
    currents = working_currents or {}
    result = ElectricalMap(mode_id=mode.id, mode_name=mode.name,
                           component_count=topology.component_count)

    for node_id, node in net.nodes.items():
        live = topology.node_energized(node_id)
        i3 = i2 = None
        status = ENERGIZED_ZERO if live else DEENERGIZED
        error = None
        if live and solver is not None:
            try:
                sc = solver.at(node_id)
                i3, i2 = sc.i3 * 1000.0, sc.i2 * 1000.0
            except Exception as exc:               # точка вне матрицы режима
                status = UNKNOWN
                error = str(exc)
        result.nodes[node_id] = NodeElectricalState(
            node_id=node_id,
            energized=live,
            nominal_voltage_kv=float(node.u_nom),
            calculated_voltage_kv=None,            # установившийся режим не считается
            connected_component_id=topology.component_of.get(node_id),
            source_ids=topology.sources_for(node_id),
            status=status, error=error, i3_a=i3, i2_a=i2,
        )

    for branch_id, branch in net.branches.items():
        conducting = net.branch_conducting(branch, mode)
        from_live = topology.node_energized(branch.node_from) if branch.node_from != GRID else True
        to_live = topology.node_energized(branch.node_to) if branch.node_to != GRID else True
        conductor = (topology.conductor_energized(branch_id)
                     if switch_ends(branch) else (from_live or to_live))
        current = currents.get(branch_id)
        if not conducting:
            current, status = 0.0, DEENERGIZED if not conductor else ENERGIZED_ZERO
        elif not conductor:
            current, status = 0.0, DEENERGIZED
        elif current is None:
            status = UNKNOWN
        elif abs(current) < 1e-9:
            status = ENERGIZED_ZERO
        else:
            status = ENERGIZED_LOADED
        direction = None
        if conducting and conductor and current:
            try:
                source_end, _, _ = net.orient(branch, mode)
                direction = "from_to" if source_end == branch.node_from else "to_from"
            except Exception:
                direction = None
        result.branches[branch_id] = BranchElectricalState(
            branch_id=branch_id,
            available=bool(mode.is_closed(branch)),
            closed=conducting,
            energized_from=from_live,
            energized_to=to_live,
            energized_conductor=conductor,
            current_a=current,
            current_direction=direction,
            status=status,
        )

    for switch_id, switch in topology.switches.items():
        branch = net.branches[switch.branch_id]
        closed = topology.switch_is_closed(switch_id)
        bus_node = getattr(branch, switch.bus_node_attr)
        bus_live = (topology.node_energized(bus_node)
                    if bus_node != GRID else True)
        terminal_live = topology.terminal_energized(switch.branch_id, switch.end)
        branch_state = result.branches[switch.branch_id]
        # Через разомкнутый аппарат ток равен нулю всегда.
        current = 0.0 if not closed else branch_state.current_a
        result.switches[switch_id] = SwitchElectricalState(
            switch_id=switch_id, branch_id=switch.branch_id, end=switch.end,
            closed=closed, tracked=switch.tracked,
            voltage_from=bus_live, voltage_to=terminal_live,
            current_a=current,
            status=(NOT_TRACKED if not switch.tracked
                    else (ENERGIZED_LOADED if current else
                          (ENERGIZED_ZERO if (bus_live or terminal_live) else DEENERGIZED))),
        )
    return result
