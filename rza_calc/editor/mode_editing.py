"""One local mode draft, immutable preview and one project-history command."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping
from copy import deepcopy

from ..domain.electrical import (
    OperatingState, OperatingStateId, EquipmentId, PortId, SwitchPosition,
    EquipmentAvailability, EQUIPMENT_AVAILABILITY_CAPABILITY, thaw_json,
    _legacy_mode_keys_for_equipment,
)
from ..domain.history import ElectricalModelMemento
from ..domain.catalog_compatibility import parameter_family
from ..domain.operating_parameters import (
    ModeValue, OperatingParameters, SOURCE_KEYS, operating_parameters,
    with_operating_parameters, validate_operating_parameters,
    parallel_operation_required,
)
from ..adapters.legacy_calculation import _legacy_or_native_id
from ..topology.engine import TopologyEngine
from .history import ProjectMemento, ProjectDraft
from .state import EditorWorkspaceState, WORKSPACE_EXTENSION_KEY
from .parameter_schema import FieldSpec, schemas_for, validate_field_value

MODE_SOURCE_FIELDS = SOURCE_KEYS


@dataclass(frozen=True, slots=True)
class ModeTarget:
    target_id: str
    equipment_id: EquipmentId
    label: str
    section: str
    port_id: PortId | None = None
    side: str = ""
    specs: tuple[FieldSpec, ...] = ()
    legacy_key: str | None = None
    calculation_switch_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModeStamp:
    owner: Any = field(repr=False, compare=False)
    state: ProjectMemento = field(repr=False)
    project: Any = field(repr=False, compare=False)
    methodology: Any = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ModeDraft:
    state_id: OperatingStateId | None = None
    name: str = "Новый режим"
    description: str = ""
    system: str = "max"
    positions: Mapping[EquipmentId, SwitchPosition] = field(default_factory=dict)
    availability: Mapping[EquipmentId, EquipmentAvailability] = field(default_factory=dict)
    parameters: OperatingParameters = field(default_factory=OperatingParameters)
    extra_positions: Mapping[str, bool] = field(default_factory=dict)
    extra_availability: Mapping[str, bool] = field(default_factory=dict)
    stamp: ModeStamp | None = field(default=None, repr=False)
    clone_from: OperatingStateId | None = None

    def __post_init__(self):
        for name in ("positions", "availability", "extra_positions", "extra_availability"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class ModeChoice:
    state_id: OperatingStateId
    calculation_mode_id: str
    name: str
    description: str
    system: str


@dataclass(frozen=True, slots=True)
class ModeSnapshot:
    state_id: OperatingStateId
    calculation_mode_id: str
    name: str
    description: str
    system: str
    positions: Mapping
    availability: Mapping
    parameters: OperatingParameters
    effective_positions: Mapping
    effective_availability: Mapping
    targets: tuple[ModeTarget, ...]
    stamp: ModeStamp


@dataclass(frozen=True, slots=True)
class ModeChange:
    section: str
    target_id: str
    key: str
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class ModeIssue:
    code: str
    message: str
    section: str = ""
    target_id: str = ""
    key: str = ""
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class ModePreview:
    valid: bool
    state_id: OperatingStateId | None = None
    changes: tuple[ModeChange, ...] = ()
    diagnostics: tuple[ModeIssue, ...] = ()
    stamp: ModeStamp | None = field(default=None, repr=False)
    _candidate: ElectricalModelMemento | None = field(default=None, repr=False)

    @property
    def errors(self):
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def can_display(self):
        return self._candidate is not None and self.state_id is not None


def _stamp(controller):
    return ModeStamp(controller, ProjectMemento.capture(controller._project), controller._project,
                     deepcopy(getattr(controller._project, "methodology", None)))


def _require_current(controller, stamp):
    from .controller import EditorCommandError
    if not isinstance(stamp, ModeStamp) or stamp.owner is not controller or stamp.project is not controller._project:
        raise EditorCommandError("Проект изменился после начала редактирования режима. Откройте свежий черновик.")
    current = ProjectMemento.capture(controller._project)
    if (stamp.state.electrical != current.electrical or stamp.state.catalog_snapshots != current.catalog_snapshots
        or stamp.state.user_catalog != current.user_catalog or stamp.methodology != getattr(controller._project, "methodology", None)):
        raise EditorCommandError("Проект изменился после начала редактирования режима. Откройте свежий черновик.")


def _legacy_maps(state):
    marker = state.extensions.get("legacy_calculation", {}) if state else {}
    return marker.get("states", {}), marker.get("availability", {})


def operating_mode_choices(controller) -> tuple[ModeChoice, ...]:
    """Selector metadata only; no electrical snapshot or topology compilation."""
    return tuple(ModeChoice(state.id,
        _legacy_or_native_id("mode", state.id.value, state.extensions),
        state.name, state.description, state.system)
        for state in controller.model.operating_states.values())


def operating_mode_targets(controller):
    model = controller.model
    targets = []
    for equipment in sorted(model.equipment.values(), key=lambda row: (row.name, row.id.value)):
        family = parameter_family(equipment.type_id)
        definition = model.equipment_type(equipment.type_id, equipment.type_version)
        eid = equipment.id
        caps = definition.capabilities
        if {"switch.position", "legacy.switch.position"} & caps:
            targets.append(ModeTarget(eid.value, eid, equipment.name, "positions"))
        if EQUIPMENT_AVAILABILITY_CAPABILITY in caps:
            targets.append(ModeTarget(eid.value, eid, equipment.name, "availability"))
        marker = equipment.extensions.get("legacy_calculation", {})
        payload = equipment.properties.get("legacy_payload", equipment.properties)
        if marker and payload.get("switchable", False):
            legacy_id = marker.get("legacy_id")
            if marker.get("category") == "transformer3w":
                branches = ((legacy_id, "ВН"), (f"{legacy_id}_mv", "СН"), (f"{legacy_id}_lv", "НН"))
                endpoints = ((bid, "from", side) for bid, side in branches)
            else:
                ends = ("from", "to") if family in {"line", "transformer_2w"} else ("from",)
                endpoints = ((legacy_id, end, "Начало" if end == "from" else "Конец") for end in ends)
            for branch_id, end, side in endpoints:
                key = f"SW:{branch_id}:{end}"
                targets.append(ModeTarget(key, eid, f"{equipment.name} — {side}", "positions", side=side,
                                          legacy_key=key, calculation_switch_id=key))
        if family in {"source", "generator"}:
            specs = tuple(spec for spec in schemas_for(model, equipment) if spec.key in SOURCE_KEYS and spec.editable)
            targets.append(ModeTarget(eid.value, eid, equipment.name, "source", specs=specs))
        if family == "load":
            targets.append(ModeTarget(eid.value, eid, equipment.name, "load",
                specs=(FieldSpec("load_factor", "Коэффициент нагрузки", "number", minimum=0),)))
        if family in {"source", "generator", "transformer_2w", "transformer_3w", "switch", "line"}:
            for port in model.ports_of(eid):
                if model.node_for_port(port.id) is None:
                    continue
                targets.append(ModeTarget(port.id.value, eid, f"{equipment.name} — {port.role}", "working_current",
                    port.id, port.role, (FieldSpec("working_current", "Первичный рабочий ток", "number", "A",
                    minimum=0, display_units=("A", "kA")),)))
    return tuple(targets)


def operating_mode_snapshots(controller):
    stamp = _stamp(controller)
    targets = operating_mode_targets(controller)
    rows = []
    for state in controller.model.operating_states.values():
        resolved = TopologyEngine().compile(controller.model, state).operating_state
        effective = {eid.value: row.position for eid, row in resolved.positions.items()}
        raw, _ = _legacy_maps(state)
        for target in targets:
            if target.legacy_key:
                value = raw.get(target.legacy_key)
                effective[target.target_id] = (SwitchPosition.CLOSED if value else SwitchPosition.OPEN) if value is not None else effective.get(target.equipment_id.value)
        rows.append(ModeSnapshot(state.id, _legacy_or_native_id("mode", state.id.value, state.extensions),
            state.name, state.description, state.system, state.positions, state.availability,
            operating_parameters(state), MappingProxyType(effective), resolved.availability, targets, stamp))
    return tuple(rows)


def operating_mode_draft(controller, state_id=None, clone_from=None):
    if state_id is not None and clone_from is not None:
        raise ValueError("Нельзя одновременно редактировать и клонировать режим.")
    selected = state_id or clone_from
    stamp = _stamp(controller)
    if selected is None:
        return ModeDraft(stamp=stamp)
    selected = selected if isinstance(selected, OperatingStateId) else OperatingStateId(selected)
    state = controller.model.operating_states.get(selected)
    if state is None:
        raise ValueError("Режим не найден.")
    positions, availability = _legacy_maps(state)
    return ModeDraft(state.id if state_id is not None else None,
        state.name if state_id is not None else state.name + " — копия", state.description, state.system,
        state.positions, state.availability, operating_parameters(state), positions, availability, stamp,
        selected if clone_from is not None else None)


def _changes(before, after):
    changes = []
    for key in ("name", "description", "system"):
        old = getattr(before, key) if before else None
        if old != getattr(after, key):
            changes.append(ModeChange("metadata", after.id.value, key, old, getattr(after, key)))
    def diff(section, a, b):
        for key in sorted(set(a) | set(b), key=str):
            if a.get(key) != b.get(key):
                changes.append(ModeChange(section, str(key), section, a.get(key), b.get(key)))
    diff("positions", before.positions if before else {}, after.positions)
    diff("availability", before.availability if before else {}, after.availability)
    oldraw, oldavail = _legacy_maps(before)
    newraw, newavail = _legacy_maps(after)
    diff("extra_positions", oldraw, newraw)
    diff("extra_availability", oldavail, newavail)
    oldp = operating_parameters(before) if before else OperatingParameters()
    newp = operating_parameters(after)
    for eid in sorted(set(oldp.sources) | set(newp.sources), key=str):
        a, b = oldp.sources.get(eid, {}), newp.sources.get(eid, {})
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                changes.append(ModeChange("source", eid.value, key, a.get(key), b.get(key)))
    diff("load_factors", oldp.load_factors, newp.load_factors)
    diff("working_currents", oldp.working_currents, newp.working_currents)
    for key in ("load_factor", "parallel_operation"):
        if getattr(oldp, key) != getattr(newp, key):
            changes.append(ModeChange("parameters", after.id.value, key, getattr(oldp,key), getattr(newp,key)))
    return tuple(changes)


def preview_operating_mode(controller, mode_draft):
    if not isinstance(mode_draft, ModeDraft):
        raise TypeError("Ожидается ModeDraft.")
    _require_current(controller, mode_draft.stamp)
    try:
        if not isinstance(mode_draft.parameters, OperatingParameters):
            raise ValueError("Требуются типизированные параметры режима.")
        if not mode_draft.name.strip():
            raise ValueError("Введите название режима.")
        model = mode_draft.stamp.state.electrical.to_model()
        original = model.operating_states.get(mode_draft.state_id or mode_draft.clone_from)
        if (mode_draft.state_id or mode_draft.clone_from) is not None and original is None:
            raise ValueError("Исходный режим удалён.")
        state_id = mode_draft.state_id or OperatingStateId.new()
        if any(row.id != state_id and row.name.strip().casefold() == mode_draft.name.strip().casefold() for row in model.operating_states.values()):
            raise ValueError("Режим с таким названием уже существует.")
        extensions = thaw_json(original.extensions) if original else {}
        raw = dict(mode_draft.extra_positions)
        avail = dict(mode_draft.extra_availability)
        known_keys = set()
        for equipment in model.equipment.values():
            known_keys.update(_legacy_mode_keys_for_equipment(equipment))
            marker = equipment.extensions.get("legacy_calculation", {})
            if marker.get("legacy_id"):
                known_keys.add(marker["legacy_id"])
        for rows in (raw, avail):
            if any(key not in known_keys or type(value) is not bool for key, value in rows.items()):
                raise ValueError("Режим содержит неизвестный адрес или некорректное legacy-состояние.")
        # Normalized changes must agree with the authoritative legacy main key;
        # unrelated endpoint and 3W leg records are retained verbatim.
        for eid in set(mode_draft.positions) | (set(original.positions) if original else set()):
            previous = original.positions.get(eid) if original else None
            selected = mode_draft.positions.get(eid)
            if previous == selected:
                continue
            equipment = model.equipment.get(eid)
            marker = equipment.extensions.get("legacy_calculation", {}) if equipment else {}
            if marker.get("legacy_id"):
                if selected is None:
                    raw.pop(marker["legacy_id"], None)
                else:
                    raw[marker["legacy_id"]] = SwitchPosition(selected) == SwitchPosition.CLOSED
        if "legacy_calculation" in extensions or raw or avail:
            marker = extensions.setdefault("legacy_calculation", {})
            marker["states"], marker["availability"] = raw, avail
            if mode_draft.state_id is None:
                marker.pop("legacy_id", None)
                marker.pop("source_order", None)
        if mode_draft.parameters != OperatingParameters() or "rza_calc.operating_parameters" in extensions:
            extensions = with_operating_parameters(extensions, mode_draft.parameters)
        candidate = OperatingState(state_id, mode_draft.name.strip(), mode_draft.positions,
            mode_draft.system, mode_draft.description, extensions, mode_draft.availability)
        validate_operating_parameters(model, candidate)
        for eid, values in mode_draft.parameters.sources.items():
            specs = {spec.key: spec for spec in schemas_for(model, model.equipment[eid]) if spec.key in SOURCE_KEYS and spec.editable}
            for key, value in values.items():
                if key not in specs:
                    raise ValueError(f"Поле источника '{key}' не поддерживается этим аппаратом.")
                issue = validate_field_value(specs[key], value.value)
                if issue:
                    raise ValueError(issue)
        if state_id in model.operating_states:
            model._operating_states[state_id] = candidate
            model._revision += 1
        else:
            model.add_operating_state(candidate)
        problems = model.validate_integrity()
        if problems:
            raise ValueError("; ".join(item.message for item in problems))
        changes = _changes(original if mode_draft.state_id else None, candidate)
        topology = TopologyEngine().compile(model, candidate)
        if changes and parallel_operation_required(topology) and candidate.extensions.get("rza_calc.operating_parameters", {}).get("parallel_operation") is not True:
            return ModePreview(False, state_id, changes, (ModeIssue("parallel_permission_required",
                "Обнаружено параллельное питание. Для нового или изменяемого режима нужно явно разрешить параллельную работу.",
                "parameters", state_id.value, "parallel_operation"),), mode_draft.stamp,
                ElectricalModelMemento.capture(model))
        return ModePreview(True, state_id, changes, stamp=mode_draft.stamp,
            _candidate=ElectricalModelMemento.capture(model))
    except (ValueError, TypeError, KeyError) as exc:
        return ModePreview(False, diagnostics=(ModeIssue("invalid_mode", str(exc)),), stamp=mode_draft.stamp)


def apply_operating_mode_preview(controller, preview):
    from .controller import EditorCommandError
    if not isinstance(preview, ModePreview) or not preview.valid or preview._candidate is None:
        raise EditorCommandError("Режим содержит ошибки и не применён.")
    _require_current(controller, preview.stamp)
    if not preview.changes:
        return preview.state_id
    def command(draft):
        _require_current(controller, preview.stamp)
        draft.electrical_model = preview._candidate.to_model()
        draft.diagram = _with_active_mode(draft.diagram, preview.state_id)
        return preview.state_id
    return controller._execute("Изменить режим сети", command)


def operating_mode_preview_model(controller, preview):
    _require_current(controller, preview.stamp)
    if not preview.can_display:
        raise ValueError("Предпросмотр режима содержит структурные ошибки.")
    return preview._candidate.to_model(), preview.state_id


def select_operating_mode(controller, state_id):
    state_id = state_id if isinstance(state_id, OperatingStateId) else OperatingStateId(state_id)
    if state_id not in controller.model.operating_states:
        raise ValueError("Режим не найден.")
    diagram = _with_active_mode(controller.diagram, state_id)
    controller._history.apply_workspace_diagram(diagram)
    return EditorWorkspaceState.from_diagram(diagram)


def _with_active_mode(diagram, state_id):
    extensions = thaw_json(diagram.extensions)
    extensions.setdefault(WORKSPACE_EXTENSION_KEY, {})["active_operating_state_id"] = state_id.value
    return replace(diagram, extensions=extensions)


def compare_operating_modes(controller, first_state_id, second_state_id):
    first = controller.model.operating_states.get(first_state_id)
    second = controller.model.operating_states.get(second_state_id)
    if first is None or second is None:
        raise ValueError("Режим для сравнения не найден.")
    return _changes(first, second)


def resolve_operating_mode_equipment(controller, calculation_object_id):
    matches = []
    for equipment in controller.model.equipment.values():
        family = parameter_family(equipment.type_id)
        category = "load" if family == "load" else ("transformer3w" if family == "transformer_3w" else "branch")
        object_id = _legacy_or_native_id(category, equipment.id.value, equipment.extensions)
        keys = {equipment.id.value, object_id}
        if family == "transformer_3w":
            keys.update((f"{object_id}_mv", f"{object_id}_lv"))
        if calculation_object_id in keys:
            matches.append(equipment.id)
    if len(matches) != 1:
        raise ValueError("Расчётный объект отсутствует или неоднозначен.")
    return matches[0]


def resolve_operating_mode_switch(controller, switch_id):
    matches = []
    for target in operating_mode_targets(controller):
        if target.section != "positions":
            continue
        if switch_id == target.target_id or switch_id == target.calculation_switch_id:
            matches.append(target)
            continue
        if not target.legacy_key:
            equipment = controller.model.equipment[target.equipment_id]
            branch_id = _legacy_or_native_id("branch", equipment.id.value, equipment.extensions)
            if switch_id in {f"SW:{branch_id}:from", f"SW:{branch_id}:to", f"SW_{branch_id}_from", f"SW_{branch_id}_to"}:
                matches.append(target)
        elif switch_id == target.calculation_switch_id.replace("SW:", "SW_", 1).rsplit(":",1)[0] + "_" + target.calculation_switch_id.rsplit(":",1)[1]:
            matches.append(target)
    exact = [item for item in matches if item.legacy_key]
    matches = exact or matches
    if len(matches) != 1:
        raise ValueError("Коммутационный аппарат не найден или его адрес неоднозначен.")
    return matches[0]


def operating_mode_template(controller, kind, *, base_state_id=None, equipment_id=None):
    """Suggest a concrete local draft; never pick a random object or commit."""
    if kind not in {"normal", "reserve", "repair", "generator_loss"}:
        raise ValueError("Неизвестный шаблон режима.")
    draft = operating_mode_draft(controller, clone_from=base_state_id)
    titles = {"normal":"Нормальный режим", "reserve":"Резервное питание", "repair":"Ремонт оборудования", "generator_loss":"Потеря генератора"}
    draft = replace(draft, name=titles[kind])
    if kind == "normal":
        return draft
    if equipment_id is None:
        raise ValueError("Выберите конкретный аппарат для предложения режима.")
    equipment = controller.model.equipment.get(equipment_id)
    if equipment is None:
        raise ValueError("Оборудование шаблона не найдено.")
    family = parameter_family(equipment.type_id)
    if kind == "generator_loss" and family != "generator":
        raise ValueError("Для потери генератора выберите генератор.")
    if kind == "reserve":
        definition = controller.model.equipment_type(equipment.type_id, equipment.type_version)
        if "switch.position" not in definition.capabilities:
            raise ValueError("Для резервного питания выберите реальный выключатель.")
        return replace(draft, positions={**draft.positions, equipment_id: SwitchPosition.CLOSED})
    return replace(draft, availability={**draft.availability, equipment_id: EquipmentAvailability.OUT_OF_SERVICE})
