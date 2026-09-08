"""One canonical mode and one physical fault point, without protection runs.

Only frozen inputs/results cross the UI/worker boundary. A solver and its
mutable numerical caches belong to one execution, never to the live project.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from .input import (CalculationCancelled, CalculationInput, ModeCalculationInput,
                    NetworkSnapshot, capture_project_input, checkpoint, validate_mode_execution)
from .projection import CalculationProjectionBuilder
from ..adapters.legacy_calculation import (CalculationTrace, adapt_to_calculation,
                                         _legacy_or_native_id)
from ..adapters.operating_parameters import network_for_mode, parallel_source_groups
from ..core.fault_types import FaultResult, FaultSpec
from ..core.short_circuit import ShortCircuitSolver, ShortCircuitStatusError
from ..domain.electrical import ElectricalNodeId, OperatingStateId, PortId
from ..domain.operating_parameters import operating_parameters
from ..topology.engine import TopologyEngine


@dataclass(frozen=True, slots=True)
class PointFaultTarget:
    """An explicit physical bus/node or connected port; never a pixel offset."""
    node_id: ElectricalNodeId | None = None
    port_id: PortId | None = None

    def __post_init__(self):
        if (self.node_id is None) == (self.port_id is None):
            raise ValueError("Для КЗ укажите ровно один электрический узел или физический вывод.")
        if self.node_id is not None and not isinstance(self.node_id, ElectricalNodeId):
            object.__setattr__(self, "node_id", ElectricalNodeId(self.node_id))
        if self.port_id is not None and not isinstance(self.port_id, PortId):
            object.__setattr__(self, "port_id", PortId(self.port_id))


@dataclass(frozen=True, slots=True)
class PointFaultRequest:
    calculation_input: CalculationInput
    state_id: OperatingStateId
    target: PointFaultTarget
    specs: tuple[FaultSpec, ...]

    def __post_init__(self):
        if not isinstance(self.calculation_input, CalculationInput) or not isinstance(self.target, PointFaultTarget):
            raise TypeError("Точечный запрос требует сохранённый CalculationInput и PointFaultTarget.")
        if not isinstance(self.state_id, OperatingStateId):
            object.__setattr__(self, "state_id", OperatingStateId(self.state_id))
        specs = tuple(dict.fromkeys(self.specs))
        if not specs or any(not isinstance(spec, FaultSpec) for spec in specs):
            raise ValueError("Выберите хотя бы один вид КЗ с явным FaultSpec.")
        object.__setattr__(self, "specs", specs)


@dataclass(frozen=True, slots=True)
class PreparedPointMode:
    calculation_input: CalculationInput
    state_id: OperatingStateId
    frame: ModeCalculationInput
    network_snapshot: NetworkSnapshot
    trace: CalculationTrace
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PointFaultOutcome:
    spec: FaultSpec
    fault: FaultResult | None = None
    code: str = ""
    message: str = ""

    @property
    def available(self):
        return self.fault is not None and not self.code


@dataclass(frozen=True, slots=True)
class PointFaultResult:
    request: PointFaultRequest
    mode_id: str
    mode_name: str
    node_id: str
    node_name: str
    outcomes: tuple[PointFaultOutcome, ...]
    prepared_mode: PreparedPointMode | None = field(default=None, repr=False)
    warnings: tuple[str, ...] = ()
    algorithm_version: str = field(default="selected-point-fault-v1", init=False)

    def is_current_for(self, project, *, state_id=None):
        return ((state_id is None or self.request.state_id == state_id)
                and self.request.calculation_input.is_current_for(project))


def make_point_fault_request(project, state_id, target, specs):
    """Capture only. Topology preparation and all numerical work are deferred."""
    return PointFaultRequest(capture_project_input(project), state_id, target, tuple(specs))


def _captured_model(request):
    captured = request.calculation_input
    if not captured.stamp.canonical or captured.model_snapshot is None:
        raise ValueError("Точечное КЗ требует канонический неизменяемый вход проекта.")
    model = captured.model_snapshot.to_model()
    method = captured.methodology.to_methodology()
    if not captured.is_current_for(SimpleNamespace(electrical_model=model, methodology=method)):
        raise ValueError("Отпечаток или версия точечного расчётного входа не совпадает.")
    if request.state_id not in model.operating_states:
        raise ValueError("Выбранный режим отсутствует в сохранённом входе.")
    return model, method


def _physical_node(model, target):
    if target.node_id is not None:
        node = model.electrical_nodes.get(target.node_id)
    else:
        if target.port_id not in model.ports:
            raise ValueError("Выбранный физический вывод отсутствует.")
        node = model.node_for_port(target.port_id)
    if node is None:
        raise ValueError("У выбранной точки нет подключённого электрического узла.")
    return node


def prepare_point_mode(request, *, cancelled=None, progress=None):
    """Compile one selected frame; retain all canonical integrity checks."""
    checkpoint(cancelled, progress, 0, 1, "Подготовка выбранного режима КЗ")
    model, method = _captured_model(request)
    state = model.operating_states[request.state_id]
    problems = method.blocking_errors()
    if problems:
        raise ValueError("Методика: " + "; ".join(problems))
    engine = TopologyEngine()
    topology = engine.compile(model, state.id)
    adaptation = adapt_to_calculation(model, topology, operating_state_ids=(state.id,))
    projection = CalculationProjectionBuilder(topology_engine=engine).build(model, topology_snapshot=topology)
    mode_id = _legacy_or_native_id("mode", state.id.value, state.extensions)
    frame = ModeCalculationInput(mode_id, topology, projection, operating_parameters(state))
    # Other modes are not executed or claimed as prepared by this result.
    net = adaptation.network
    net.modes = {mode_id: net.modes[mode_id]}
    problems = net.validate(require_source_data=False)
    if problems:
        raise ValueError("Модель: " + "; ".join(problems))
    validate_mode_execution(frame, net, adaptation.trace)
    net = network_for_mode(net, net.modes[mode_id])
    mode = net.modes[mode_id]
    problems = net.validate(mode)
    if problems:
        raise ValueError("Режим: " + "; ".join(problems))
    warnings = []
    if parallel_source_groups(net, mode):
        permission = mode.operating_parameters.get("parallel_operation")
        if permission is False:
            raise ValueError("Параллельная работа соединённых источников запрещена параметрами режима.")
        if permission is None:
            warnings.append("Параллельная работа источников присутствует в прежней схеме; явное разрешение режима ещё не записано.")
    checkpoint(cancelled, progress, 1, 1, "Выбранный режим подготовлен")
    return PreparedPointMode(request.calculation_input, state.id, frame,
                             NetworkSnapshot.capture(net), adaptation.trace, tuple(warnings))


def _execution(prepared):
    net = prepared.network_snapshot.materialize()
    validate_mode_execution(prepared.frame, net, prepared.trace)
    method = prepared.calculation_input.methodology.to_methodology()
    return net, ShortCircuitSolver(net, net.modes[prepared.frame.mode_id], method)


def run_point_faults(request, *, prepared_mode=None, cancelled=None, progress=None):
    """One solver, selected faults only. A missing sequence affects its own row."""
    checkpoint(cancelled)
    model, _ = _captured_model(request)
    state = model.operating_states[request.state_id]
    node = _physical_node(model, request.target)
    mode_id = _legacy_or_native_id("mode", state.id.value, state.extensions)
    prepared = None
    try:
        if prepared_mode is not None:
            if (prepared_mode.calculation_input.stamp != request.calculation_input.stamp
                    or prepared_mode.calculation_input.execution_versions != request.calculation_input.execution_versions
                    or prepared_mode.state_id != request.state_id):
                raise ValueError("Подготовленный режим относится к другому входу.")
            prepared = prepared_mode
        else:
            prepared = prepare_point_mode(request, cancelled=cancelled, progress=progress)
        node_id = prepared.trace.domain_node_to_legacy[node.id.value]
        net, solver = _execution(prepared)
    except CalculationCancelled:
        raise
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        outcomes = tuple(PointFaultOutcome(spec, code=str(getattr(error, "code", "MODE_UNAVAILABLE")),
                                           message=str(error)) for spec in request.specs)
        return PointFaultResult(request, mode_id, state.name, "", node.name, outcomes)
    outcomes = []
    for index, spec in enumerate(request.specs):
        checkpoint(cancelled, progress, index, len(request.specs), "КЗ " + spec.kind.value)
        try:
            outcome = PointFaultOutcome(spec, fault=solver.fault_at(node_id, spec))
        except (ValueError, KeyError, TypeError, RuntimeError) as error:
            outcome = PointFaultOutcome(spec, code=str(getattr(error, "code", "CALCULATION_ERROR")), message=str(error))
        outcomes.append(outcome)
    checkpoint(cancelled, progress, len(request.specs), len(request.specs), "Выбранные КЗ рассчитаны")
    return PointFaultResult(request, mode_id, state.name, node_id, node.name, tuple(outcomes), prepared, prepared.warnings)


def run_point_fault_network(result, spec, *, cancelled=None, progress=None):
    """Lazy branch contribution from the same frozen mode, without protections."""
    outcome = next((row for row in result.outcomes if row.spec == spec), None)
    if outcome is None:
        raise ValueError("Этот вид КЗ не входит в выполненный точечный запрос.")
    if not outcome.available or result.prepared_mode is None:
        raise ShortCircuitStatusError(outcome.code or "CALCULATION_ERROR", outcome.message)
    checkpoint(cancelled, progress, 0, 1, "Токи ветвей выбранного КЗ")
    _, solver = _execution(result.prepared_mode)
    network_result = solver.fault_network_at(result.node_id, spec)
    checkpoint(cancelled, progress, 1, 1, "Токи ветвей рассчитаны")
    return network_result
