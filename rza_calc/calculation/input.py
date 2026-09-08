"""Immutable calculation requests and their prepared, mode-specific views.

Capture is deliberately cheap and does not compile topology on the GUI thread.
Preparation and all numerical work use only captured records. ``Network`` is a
private compatibility materialisation, never a mutable member of this input.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from ..core import model as dto
from ..core.fingerprint import methodology_fingerprint, network_fingerprint
from ..core.methodology import Methodology, MethodologySnapshot
from ..domain.electrical import ElectricalModel
from ..domain.fingerprint import electrical_model_fingerprint
from ..domain.history import ElectricalModelMemento
from ..version import APPLICATION_VERSION, KERNEL_VERSION, ALGORITHM_VERSION


class CalculationCancelled(Exception):
    """An explicitly cancelled attempt has no publishable result."""


def checkpoint(cancelled=None, progress=None, done=0, total=1, label=""):
    if cancelled is not None and cancelled():
        raise CalculationCancelled("Расчёт отменён.")
    if progress is not None:
        progress(done, total, label)
    if cancelled is not None and cancelled():
        raise CalculationCancelled("Расчёт отменён.")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


_COLLECTIONS = ("nodes", "branches", "transformers3w", "loads", "modes")
_DTO_TYPES = {name: value for name, value in vars(dto).items()
              if isinstance(value, type) and hasattr(value, "__dataclass_fields__")}


@dataclass(frozen=True, slots=True)
class NetworkSnapshot:
    """Exact DTO values in declaration order; decoding has no migration defaults."""
    payload_json: str
    fingerprint: str

    @classmethod
    def capture(cls, net: dto.Network):
        payload = {"name": net.name, "neutral": net.neutral}
        for name in _COLLECTIONS:
            payload[name] = [[key, type(value).__name__, asdict(value)]
                             for key, value in getattr(net, name).items()]
        return cls(_json(payload), network_fingerprint(net))

    def materialize(self) -> dto.Network:
        raw = json.loads(self.payload_json)
        if set(raw) != {"name", "neutral", *_COLLECTIONS}:
            raise ValueError("Повреждён состав снимка расчётной сети.")
        net = dto.Network(name=raw["name"], neutral=raw["neutral"])
        for name in _COLLECTIONS:
            store = {}
            for key, type_name, values in raw[name]:
                kind = _DTO_TYPES.get(type_name)
                expected = {"nodes": dto.Node, "branches": dto.Branch,
                            "transformers3w": dto.Transformer3W,
                            "loads": dto.Load, "modes": dto.Mode}[name]
                if kind is None or not issubclass(kind, expected):
                    raise ValueError("Недопустимый тип в снимке расчётной сети.")
                if set(values) != {item.name for item in fields(kind)}:
                    raise ValueError("Повреждены поля снимка расчётной сети.")
                if "prot" in values and isinstance(values["prot"], dict):
                    values["prot"] = dto.ProtectionSettings(**values["prot"])
                if values.get("ct_ratio") is not None:
                    values["ct_ratio"] = tuple(values["ct_ratio"])
                value = kind(**values)
                if key != value.id or key in store:
                    raise ValueError("Повреждены ID снимка расчётной сети.")
                store[key] = value
            setattr(net, name, store)
        if network_fingerprint(net) != self.fingerprint:
            raise ValueError("Отпечаток снимка расчётной сети не совпадает.")
        return net


@dataclass(frozen=True, slots=True)
class CalculationInputStamp:
    model_fingerprint: str
    methodology_fingerprint: str
    mode_order: tuple[str, ...] = ()
    canonical: bool = True

    @property
    def fingerprint(self):
        return hashlib.sha256(_json(asdict(self)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModeCalculationInput:
    mode_id: str
    topology: Any
    projection: Any
    operating_parameters: Any


@dataclass(frozen=True, slots=True)
class ExecutionVersions:
    application: str = APPLICATION_VERSION
    kernel: str = KERNEL_VERSION
    algorithm: str = ALGORITHM_VERSION


@dataclass(frozen=True, slots=True)
class CalculationInput:
    stamp: CalculationInputStamp
    methodology: MethodologySnapshot
    model_snapshot: ElectricalModelMemento | None = field(default=None, repr=False)
    compatibility_network: NetworkSnapshot | None = field(default=None, repr=False)
    modes: tuple[ModeCalculationInput, ...] = ()
    trace: Any = None
    prepared: bool = False
    execution_versions: ExecutionVersions = field(default_factory=ExecutionVersions)

    def is_current_for(self, project) -> bool:
        try:
            if (self.execution_versions.kernel != KERNEL_VERSION
                    or self.execution_versions.algorithm != ALGORITHM_VERSION):
                return False
            if self.stamp.canonical:
                model = project.electrical_model
                return self.stamp == CalculationInputStamp(
                    electrical_model_fingerprint(model),
                    methodology_fingerprint(project.methodology),
                    tuple(item.value for item in model.operating_states))
            return (network_fingerprint(project.network) == self.stamp.model_fingerprint
                    and methodology_fingerprint(project.methodology) == self.stamp.methodology_fingerprint)
        except (AttributeError, TypeError, ValueError):
            return False


def capture_project_input(project) -> CalculationInput:
    """Freeze canonical inputs only; do not read the derived Network property."""
    model = project.electrical_model
    snapshot = ElectricalModelMemento.capture(model)
    method = project.methodology.snapshot()
    # Fingerprint the captured records, not a second live read of their values.
    captured = snapshot.to_model()
    stamp = CalculationInputStamp(electrical_model_fingerprint(captured),
        methodology_fingerprint(method.to_methodology()),
        tuple(item.value for item in captured.operating_states))
    if model.revision != snapshot.revision:
        raise ValueError("Модель изменилась во время фиксации расчётного входа.")
    return CalculationInput(stamp, method, snapshot)


def capture_network_input(net: dto.Network, meth: Methodology) -> CalculationInput:
    snapshot = NetworkSnapshot.capture(net)
    method = meth.snapshot()
    return CalculationInput(CalculationInputStamp(snapshot.fingerprint,
        methodology_fingerprint(method.to_methodology()),
        tuple(row[0] for row in json.loads(snapshot.payload_json)["modes"]), False),
        method, compatibility_network=snapshot, prepared=True)


def prepare_calculation_input(request: CalculationInput, *, cancelled=None, progress=None) -> CalculationInput:
    if not isinstance(request, CalculationInput):
        raise TypeError("Ожидается неизменяемый CalculationInput.")
    if (request.execution_versions.kernel != KERNEL_VERSION
            or request.execution_versions.algorithm != ALGORITHM_VERSION):
        raise ValueError("Версия ядра или алгоритма расчётного входа не поддерживается.")
    checkpoint(cancelled, progress, 0, 1, "Подготовка расчётного входа")
    if request.prepared:
        return request
    from ..adapters.legacy_calculation import adapt_to_calculation, _legacy_or_native_id
    from ..adapters.operating_parameters import attach_operating_parameters
    from ..domain.operating_parameters import operating_parameters
    from ..topology.engine import TopologyEngine
    from .projection import CalculationProjectionBuilder
    if request.model_snapshot is None:
        raise ValueError("В расчётном входе отсутствует каноническая модель.")
    model = request.model_snapshot.to_model()
    engine = TopologyEngine()
    builder = CalculationProjectionBuilder(topology_engine=engine)
    states = list(model.operating_states.values())
    frames = []
    adaptation = None
    for index, state in enumerate(states or [None]):
        checkpoint(cancelled, progress, index, len(states) or 1,
                   "Топология и проекция: " + (state.name if state else "нормальная схема"))
        topology = engine.compile(model, state.id if state else None)
        if adaptation is None or not topology.is_valid:
            # The adapter supplies the established typed error/confirmation guards.
            current = adapt_to_calculation(model, topology)
            if adaptation is None:
                adaptation = current
        projection = builder.build(model, topology_snapshot=topology)
        frames.append(ModeCalculationInput(
            _legacy_or_native_id("mode", state.id.value, state.extensions) if state else "",
            topology, projection, operating_parameters(state) if state else None))
    assert adaptation is not None
    attach_operating_parameters(model, adaptation.network, adaptation.trace)
    # Numerical iteration and tie-breaking retain adapter declaration order.
    order = {mid: index for index, mid in enumerate(adaptation.network.modes)}
    frames.sort(key=lambda frame: order.get(frame.mode_id, len(order)))
    checkpoint(cancelled, progress, len(states) or 1, len(states) or 1, "Расчётный вход подготовлен")
    return replace(request, compatibility_network=NetworkSnapshot.capture(adaptation.network),
                   modes=tuple(frames), trace=adaptation.trace, prepared=True)


def validate_mode_execution(frame: ModeCalculationInput, net, trace):
    """A compatibility matrix may not contradict its authoritative mode frame."""
    topology, projection = frame.topology, frame.projection
    if (projection.model_fingerprint != topology.model_fingerprint
            or projection.state_id != topology.operating_state.state_id
            or projection.topology_fingerprint != topology.topology_fingerprint):
        raise ValueError("Режим, топология и расчётная проекция относятся к разным входам.")
    mode = net.modes[frame.mode_id]
    energized = net.energized_nodes(mode)
    if set(projection.electrical_node_map) != set(topology.node_ids):
        raise ValueError("Проекция потеряла канонические электрические узлы.")
    for node_id in topology.node_ids:
        legacy_id = trace.domain_node_to_legacy[node_id.value]
        if topology.is_energized(node_id) != (legacy_id in energized):
            raise ValueError("Питание узла не совпадает с каноническим режимом: " + legacy_id)
    for branch in projection.branches.values():
        ids = trace.domain_equipment_to_legacy[branch.equipment_id.value]
        candidates = [net.branches[bid] for bid in ids if bid in net.branches]
        if len(ids) > 1 and len(branch.port_ids) == 1:
            candidates = [item for item in candidates if trace.legacy_branch_to_port.get(item.id) == branch.port_ids[0].value]
        if not candidates:
            continue  # A load has no conducting series branch in the DTO.
        if any(net.branch_conducting(item, mode) != branch.active for item in candidates):
            raise ValueError("Положение расчётной ветви не совпадает с проекцией режима: " + ", ".join(item.id for item in candidates))
        expected = {trace.domain_node_to_legacy[topology.port_to_node[pid].value]
                    for pid in branch.port_ids}
        actual = {node for item in candidates for node in (item.node_from, item.node_to)}
        if not expected <= actual:
            raise ValueError("Физические концы ветви не совпадают с проекцией режима.")


def save_calculation_input(request: CalculationInput, path: str | Path, *, overwrite=False):
    from ..io.electrical_model import electrical_model_to_dict
    payload = {"schema": "rza-calculation-input/1", "stamp": asdict(request.stamp),
               "execution_versions": asdict(request.execution_versions),
               "methodology": request.methodology.as_dict(), "model": None, "network": None}
    if request.model_snapshot is not None:
        payload["model"] = electrical_model_to_dict(request.model_snapshot.to_model())
    elif request.compatibility_network is not None:
        payload["network"] = asdict(request.compatibility_network)
    else:
        raise ValueError("В расчётном входе нет исходных данных.")
    text = _json(payload)
    destination = Path(path)
    if not overwrite:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                prefix="." + destination.name + ".", suffix=".tmp",
                dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_calculation_input(path: str | Path) -> CalculationInput:
    from ..io.electrical_model import electrical_model_from_dict
    def object_pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("Повторное поле в архиве расчётного входа: " + key)
            out[key] = value
        return out
    def invalid_constant(value):
        raise ValueError("Неконечное число в архиве расчётного входа.")
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                     object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    if (not isinstance(raw, dict) or set(raw) != {"schema", "stamp", "methodology", "model", "network", "execution_versions"}
            or raw["schema"] != "rza-calculation-input/1"):
        raise ValueError("Неизвестный формат расчётного входа.")
    versions_raw = raw["execution_versions"]
    if (not isinstance(versions_raw, dict) or set(versions_raw) != {"application", "kernel", "algorithm"}
            or any(not isinstance(value, str) or not value.strip() for value in versions_raw.values())):
        raise ValueError("Повреждены версии расчётного входа.")
    versions = ExecutionVersions(**versions_raw)
    if versions.kernel != KERNEL_VERSION or versions.algorithm != ALGORITHM_VERSION:
        raise ValueError("Версия ядра или алгоритма расчётного входа не поддерживается.")
    stamp_raw = dict(raw["stamp"])
    stamp_raw["mode_order"] = tuple(stamp_raw["mode_order"])
    stamp = CalculationInputStamp(**stamp_raw)
    method = MethodologySnapshot.from_dict(raw["methodology"])
    if methodology_fingerprint(method.to_methodology()) != stamp.methodology_fingerprint:
        raise ValueError("Отпечаток методики архива не совпадает.")
    if raw["model"] is not None and raw["network"] is None and stamp.canonical:
        model = electrical_model_from_dict(raw["model"])
        if (electrical_model_fingerprint(model) != stamp.model_fingerprint
                or set(item.value for item in model.operating_states) != set(stamp.mode_order)
                or len(stamp.mode_order) != len(model.operating_states)):
            raise ValueError("Отпечаток модели архива не совпадает.")
        by_id = {item.value: item for item in model.operating_states}
        model._operating_states = {by_id[key]: model.operating_states[by_id[key]]
                                   for key in stamp.mode_order}
        return CalculationInput(stamp, method, ElectricalModelMemento.capture(model), execution_versions=versions)
    if raw["model"] is None and raw["network"] is not None and not stamp.canonical:
        network = NetworkSnapshot(**raw["network"])
        net = network.materialize()
        if network.fingerprint != stamp.model_fingerprint or tuple(net.modes) != stamp.mode_order:
            raise ValueError("Отпечаток сети архива не совпадает.")
        return CalculationInput(stamp, method, compatibility_network=network, prepared=True, execution_versions=versions)
    raise ValueError("Архив должен содержать один исходный электрический вход.")
