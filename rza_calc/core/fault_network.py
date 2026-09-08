"""Superimposed fault contributions; no load current or invented prefault phasors."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Mapping

from .fault_types import ComplexTriple, FaultResult, FaultType, _phases
from .impedance import stage_voltage
from .model import GRID
from .short_circuit import ShortCircuitStatusError

PartialComplexTriple = tuple[complex | None, complex | None, complex | None]


@dataclass(frozen=True, slots=True)
class FaultNetworkDiagnostic:
    code: str
    message: str
    sequence: int | None = None
    branch_id: str = ""
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class NodeFaultResult:
    node_id: str
    voltage_stage_kv: float | None
    delta_v012_kv: PartialComplexTriple
    delta_vabc_kv: ComplexTriple | None
    v012_kv: ComplexTriple | None = None
    vabc_kv: ComplexTriple | None = None
    diagnostics: tuple[FaultNetworkDiagnostic, ...] = ()
    is_physical: bool = True

    @property
    def is_complete(self) -> bool:
        return self.delta_vabc_kv is not None


@dataclass(frozen=True, slots=True)
class BranchTerminalResult:
    node_id: str
    voltage_stage_kv: float | None
    delta_i012_ka: PartialComplexTriple
    delta_iabc_ka: ComplexTriple | None
    residual_current_ka: complex | None
    is_physical: bool = True
    diagnostics: tuple[FaultNetworkDiagnostic, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.delta_iabc_ka is not None


@dataclass(frozen=True, slots=True)
class BranchFaultResult:
    branch_id: str
    from_terminal: BranchTerminalResult
    to_terminal: BranchTerminalResult
    active_in_mode: bool
    diagnostics: tuple[FaultNetworkDiagnostic, ...] = ()

    @property
    def is_complete(self) -> bool:
        return all(terminal.is_complete for terminal in (self.from_terminal, self.to_terminal)
                   if terminal.is_physical)


@dataclass(frozen=True, slots=True)
class FaultNetworkResult:
    fault: FaultResult
    nodes: Mapping[str, NodeFaultResult]
    branches: Mapping[str, BranchFaultResult]
    base_voltage_kv: float
    quantity_kind: str = field(default="superimposed_fault_contribution", init=False)
    algorithm_version: str = field(default="sequence-branch-contribution-v1", init=False)
    assumptions: tuple[str, ...] = ()
    diagnostics: tuple[FaultNetworkDiagnostic, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "nodes", MappingProxyType(dict(self.nodes)))
        object.__setattr__(self, "branches", MappingProxyType(dict(self.branches)))


def _finite(value):
    if value is not None and not (math.isfinite(value.real) and math.isfinite(value.imag)):
        raise ShortCircuitStatusError("NUMERIC_FAILURE",
            "Получена неконечная составляющая напряжения или тока от КЗ.")
    return value


def _abc(values):
    if any(value is None for value in values):
        return None
    return tuple(_finite(value) for value in _phases(tuple(values)))


def _bridges(edges):
    """Multigraph bridges, iterative Tarjan; parallel ideal ties remain ambiguous."""
    adjacency = {}
    for index, stamp in enumerate(edges):
        adjacency.setdefault(stamp.first, []).append((stamp.second, index))
        adjacency.setdefault(stamp.second, []).append((stamp.first, index))
    discovered, low, bridges, components = {}, {}, set(), []
    clock = 0
    for root in adjacency:
        if root in discovered:
            continue
        component = {root}
        discovered[root] = low[root] = clock
        clock += 1
        parent = {root: (None, None)}
        stack = [(root, iter(adjacency[root]))]
        while stack:
            node, iterator = stack[-1]
            try:
                other, edge_id = next(iterator)
            except StopIteration:
                stack.pop()
                previous, incoming = parent[node]
                if previous is not None:
                    low[previous] = min(low[previous], low[node])
                    if low[node] > discovered[previous]:
                        bridges.add(incoming)
                continue
            if edge_id == parent[node][1]:
                continue
            if other not in discovered:
                discovered[other] = low[other] = clock
                clock += 1
                component.add(other)
                parent[other] = (node, edge_id)
                stack.append((other, iter(adjacency[other])))
            else:
                low[node] = min(low[node], discovered[other])
        components.append(component)
    return adjacency, bridges, components


def _ideal_currents(edges, finite_outgoing, fault_node, fault_current, sequence):
    """Recover uniquely determined ideal-edge currents by cut-set KCL."""
    adjacency, bridges, components = _bridges(edges)
    rhs = {node: -finite_outgoing.get(node, 0j)
           - (fault_current if node == fault_node else 0j) for node in adjacency}
    for component in components:
        if GRID in component:
            # The reference holds zero incremental voltage and supplies the missing injection.
            rhs[GRID] = -sum(rhs[node] for node in component if node != GRID)
        else:
            residual = _finite(sum(rhs[node] for node in component))
            scale = max(1.0, sum(abs(rhs[node]) for node in component))
            if abs(residual) > 1e-8 * scale:
                raise ShortCircuitStatusError("NUMERIC_FAILURE",
                    f"Идеальная группа: не выполнен баланс токов последовательности {sequence}.")
    currents = {}
    for index, stamp in enumerate(edges):
        if index not in bridges:
            currents[stamp.branch_id] = None
            continue
        side, pending = {stamp.first}, [stamp.first]
        while pending:
            for other, edge_id in adjacency[pending.pop()]:
                if edge_id != index and other not in side:
                    side.add(other)
                    pending.append(other)
        currents[stamp.branch_id] = _finite(sum(rhs[node] for node in side))
    return currents


def build_fault_network(solver, node_id, spec):
    """Reuse the point-fault equations, then solve the negative current injections.

    Terminal signs are FROM each physical bus INTO the equipment. Returned
    currents are changes produced by the fault with fixed source EMFs.
    Positive-sequence prefault load/circulating currents are not available here.
    """
    fault = solver.fault_at(node_id, spec)
    net, base = solver.net, solver.base
    nodes, raw_branches = solver._component(node_id)
    branches = ([solver._resolved_sequence_branch(branch) for branch in raw_branches]
                if spec.kind is not FaultType.THREE_PHASE else raw_branches)
    # The historical balanced path uses a strict ideal-impedance threshold.
    # Match its actual network, without changing the existing point-fault API.
    strict_threshold = (spec.kind is FaultType.THREE_PHASE and not any(
        getattr(branch, "sequence_phase_shift_deg", None) not in (None, 0) for branch in branches))
    actual_stages, stage_diags = {}, {}
    for key, node in net.nodes.items():
        if key == GRID:
            continue
        try:
            actual_stages[key] = stage_voltage(node, solver.methodology)
        except (KeyError, ValueError, TypeError) as error:
            if key in nodes:
                raise
            actual_stages[key] = None
            stage_diags[key] = FaultNetworkDiagnostic("UNAVAILABLE_UNRELATED_VOLTAGE_STAGE",
                f"Ступень узла вне аварийного острова не определена: {error}", node_id=key)
    internal_nodes = {transformer.star_node_id for transformer in net.transformers3w.values()}
    fault_stage = actual_stages[node_id]
    node_delta = {key: [0j, 0j, 0j] for key in actual_stages}
    terminal_delta = {key: [[0j, 0j, 0j], [0j, 0j, 0j]] for key in net.branches}
    branch_diags = {key: [] for key in net.branches}
    node_diags = {key: [stage_diags[key]] if key in stage_diags else [] for key in actual_stages}
    diagnostics = list(stage_diags.values())
    active_ids = {branch.id for branch in net.active_branches(solver.mode)}

    for sequence in (0, 1, 2):
        current = fault.i012_ka[sequence]
        if current == 0:
            # A balanced unused sequence needs neither guessed impedances nor a matrix.
            continue
        sequence_branches = (solver._zero_component_branches(branches, node_id)
                             if sequence == 0 else branches)
        stamps = solver._stamps(sequence_branches, sequence,
                                asymmetric=spec.kind is not FaultType.THREE_PHASE)
        response = {}
        solver._driving_impedance(nodes, stamps, node_id, sequence, response=response,
                                  strict_threshold=strict_threshold)
        injection = _finite(current * fault_stage / base)
        delta = {key: None if value is None else _finite(-value * injection)
                 for key, value in response["column"].items()}
        for key in nodes:
            node_delta[key][sequence] = (None if delta[key] is None else
                                         _finite(delta[key] * actual_stages[key] / base))
            if delta[key] is None:
                issue = FaultNetworkDiagnostic("UNREFERENCED_SEQUENCE_VOLTAGE",
                    "Изменение напряжения относительно земли в отделённой схеме последовательности не определено.",
                    sequence, node_id=key)
                node_diags[key].append(issue)
                diagnostics.append(issue)
        finite_outgoing = {}
        ideal = []
        stamp_currents = {}
        for stamp in stamps:
            ideal_stamp = (stamp.impedance == 0 or abs(stamp.impedance) < solver.threshold
                           or (not strict_threshold and abs(stamp.impedance) == solver.threshold))
            if ideal_stamp:
                ideal.append(stamp)
                continue
            first, second = delta[stamp.first], delta[stamp.second]
            if first is None or second is None:
                stamp_currents[stamp.branch_id] = (None, None)
                issue = FaultNetworkDiagnostic("UNREFERENCED_SEQUENCE_CURRENT",
                    "Ток ветви не определён из доступной схемы последовательности.",
                    sequence, stamp.branch_id)
                branch_diags[stamp.branch_id].append(issue)
                diagnostics.append(issue)
                continue
            y, tap = 1 / stamp.impedance, stamp.tap
            first_current = _finite(y * first / abs(tap)**2 - y * second / tap.conjugate())
            second_current = _finite(y * second - y * first / tap)
            stamp_currents[stamp.branch_id] = (first_current, second_current)
            finite_outgoing[stamp.first] = finite_outgoing.get(stamp.first, 0j) + first_current
            finite_outgoing[stamp.second] = finite_outgoing.get(stamp.second, 0j) + second_current
        ideal_values = _ideal_currents(ideal, finite_outgoing, node_id, injection, sequence)
        for stamp in ideal:
            value = ideal_values[stamp.branch_id]
            stamp_currents[stamp.branch_id] = (value, None if value is None else -value)
            if value is None:
                issue = FaultNetworkDiagnostic("AMBIGUOUS_IDEAL_CURRENT",
                    "Ток идеальной ветви в замкнутом контуре неоднозначен; сопротивление и деление тока не выдумываются.",
                    sequence, stamp.branch_id)
                branch_diags[stamp.branch_id].append(issue)
                diagnostics.append(issue)
        # Check physical KCL wherever every incident current is determined.
        balance, uncertain = {}, set()
        for stamp in stamps:
            values = stamp_currents[stamp.branch_id]
            for key, value in zip((stamp.first, stamp.second), values):
                if value is None:
                    uncertain.add(key)
                else:
                    balance[key] = balance.get(key, 0j) + value
        for key in nodes - uncertain:
            residual = _finite(balance.get(key, 0j) + (injection if key == node_id else 0j))
            if abs(residual) > 1e-8 * max(1.0, abs(injection)):
                raise ShortCircuitStatusError("NUMERIC_FAILURE",
                    f"Узел {key}: не выполнен баланс токов последовательности {sequence}.")
        for branch in sequence_branches:
            values = stamp_currents.get(branch.id)
            if values is None:  # Explicit blocked zero-sequence transmission: terminal I0=0.
                continue
            connection = branch.zero_sequence_connection if sequence == 0 else "series"
            if connection == "from_ground":
                values = (values[0], 0j)
            elif connection == "to_ground":
                values = (0j, values[0])
            for side, value in enumerate(values):
                terminal_delta[branch.id][side][sequence] = value

    # Both quantities are independently available at the fault itself. A
    # different contraction or inconsistent scale must not escape as a result.
    prefault = net.nodes[node_id].prefault_voltage_kv
    emf = (fault_stage if prefault is None else prefault) / math.sqrt(3)
    for sequence, delta_value in enumerate(node_delta[node_id]):
        expected = fault.v012_kv[sequence] - (emf if sequence == 1 else 0j)
        if delta_value is None or abs(delta_value - expected) > 1e-8 * max(1, abs(emf), abs(expected)):
            raise ShortCircuitStatusError("NUMERIC_FAILURE",
                f"В точке КЗ не согласованы напряжение и его изменение последовательности {sequence}.")

    node_results = {}
    for key, values in node_delta.items():
        vector = tuple(values)
        node_results[key] = NodeFaultResult(key, actual_stages[key], vector, _abc(vector),
            fault.v012_kv if key == node_id else None,
            fault.vabc_kv if key == node_id else None, tuple(node_diags[key]), key not in internal_nodes)

    branch_results = {}
    for key, branch in net.branches.items():
        terminals = []
        for side, terminal_node in enumerate((branch.node_from, branch.node_to)):
            if terminal_node == GRID or terminal_node in internal_nodes:
                issue = FaultNetworkDiagnostic("INTERNAL_REFERENCE_TERMINAL" if terminal_node == GRID
                    else "INTERNAL_TRANSFORMER_TERMINAL",
                    "Внутренний расчётный конец не является физическим местом установки ТТ.",
                    branch_id=key, node_id=terminal_node)
                terminals.append(BranchTerminalResult(terminal_node, None, (None, None, None),
                    None, None, False, (issue,)))
                continue
            stage = actual_stages[terminal_node]
            vector = tuple(None if value is None else 0j if value == 0 else _finite(value * base / stage)
                           for value in terminal_delta[key][side])
            terminal_diags = tuple(branch_diags[key]) + ((stage_diags[terminal_node],)
                if terminal_node in stage_diags else ())
            terminals.append(BranchTerminalResult(terminal_node, stage, vector, _abc(vector),
                None if vector[0] is None else _finite(3 * vector[0]), diagnostics=terminal_diags))
        branch_results[key] = BranchFaultResult(key, *terminals, key in active_ids,
                                                tuple(branch_diags[key]))
    return FaultNetworkResult(fault, node_results, branch_results, base,
        assumptions=(
            "Составляющая от КЗ; доаварийный ток нагрузки и уравнительные токи не добавлены.",
            "Терминальные токи направлены из соответствующего узла внутрь оборудования.",
            "Изменения напряжений и токов рассчитаны при фиксированных внутренних ЭДС источников.",
            "Абсолютные напряжения возвращаются только в точке КЗ; доаварийные фазоры остальных узлов не выдумываются.",
            "Ветви вне затронутого физического острова и отключённые ветви имеют нулевую составляющую от этого КЗ.",
        ), diagnostics=tuple(diagnostics))


__all__ = ["FaultNetworkDiagnostic", "NodeFaultResult", "BranchTerminalResult",
           "BranchFaultResult", "FaultNetworkResult"]
