# -*- coding: utf-8 -*-
"""Boundary between the canonical electrical model and the legacy solver DTO.

The legacy :class:`~rza_calc.core.model.Network` remains useful as a calculation
input, but it is not a source of truth for the editor.  This module therefore
contains two deliberately asymmetric operations:

* ``import_legacy_network`` is a deterministic compatibility importer;
* ``adapt_to_calculation`` builds a fresh, disposable ``Network``.

Neither operation uses names, coordinates or SVG data to determine electrical
connectivity.  Legacy three-winding star nodes and legs remain implementation
details of ``core.Network`` and never become domain equipment.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid5

from ..core.model import (
    GRID,
    Branch,
    GeneratorBranch,
    LineBranch,
    Load,
    Mode,
    Network,
    Node,
    ProtectionSettings,
    SourceBranch,
    TieBranch,
    Transformer3W,
    TransformerBranch,
)
from ..domain.electrical import (
    AC_POWER,
    EQUIPMENT_AVAILABILITY_CAPABILITY,
    ConnectionId,
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentAvailability,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    OperatingState,
    OperatingStateId,
    PortDefinition,
    PortId,
    PropertyDefinition,
    SwitchPosition,
    VoltageClass,
    VoltageClassId,
    deterministic_id,
    thaw_json,
)
from ..topology import (
    DiagnosticSeverity,
    TopologySnapshot,
    electrical_model_identity,
)


LEGACY_EXTENSION = "legacy_calculation"
LEGACY_PAYLOAD = "legacy_payload"
LEGACY_SCHEMA_VERSION = 1

_CORE_ID_NAMESPACE = UUID("53b1e18a-7c18-46ad-a58a-e1a07a6ee4ee")
_SAFE_CORE_ID = re.compile(r"^[^\x00-\x20:]+$")


class LegacyCalculationAdapterError(ValueError):
    """A canonical model cannot be represented honestly by the legacy DTO."""

    def __init__(self, message: str, diagnostics: tuple["AdapterDiagnostic", ...] = ()):
        super().__init__(message)
        self.diagnostics = diagnostics


class LegacyImportError(LegacyCalculationAdapterError):
    """A legacy network cannot be imported without losing its semantics."""


@dataclass(frozen=True, slots=True)
class AdapterDiagnostic:
    severity: str
    code: str
    message: str
    object_id: str = ""


@dataclass(frozen=True, slots=True)
class CalculationTrace:
    """Stable mapping used later to attach calculation results to domain IDs."""

    domain_node_to_legacy: Mapping[str, str]
    domain_equipment_to_legacy: Mapping[str, tuple[str, ...]]
    legacy_node_to_domain: Mapping[str, str]
    legacy_object_to_domain: Mapping[str, str]
    legacy_branch_to_port: Mapping[str, str]

    @classmethod
    def create(
        cls,
        *,
        domain_node_to_legacy: Mapping[str, str],
        domain_equipment_to_legacy: Mapping[str, tuple[str, ...]],
        legacy_node_to_domain: Mapping[str, str],
        legacy_object_to_domain: Mapping[str, str],
        legacy_branch_to_port: Mapping[str, str],
    ) -> "CalculationTrace":
        return cls(
            MappingProxyType(dict(domain_node_to_legacy)),
            MappingProxyType(dict(domain_equipment_to_legacy)),
            MappingProxyType(dict(legacy_node_to_domain)),
            MappingProxyType(dict(legacy_object_to_domain)),
            MappingProxyType(dict(legacy_branch_to_port)),
        )


@dataclass(frozen=True, slots=True)
class AdaptationResult:
    network: Network
    trace: CalculationTrace
    diagnostics: tuple[AdapterDiagnostic, ...] = ()


def _stable_id(id_type, *parts: str):
    """Delimiter-safe wrapper around the domain UUIDv5 factory."""
    token = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return deterministic_id(id_type, token)


def _compat_port(role: str, display_name: str) -> PortDefinition:
    # Legacy endpoint voltage is carried by the explicit node.  Assigning one
    # voltage group here could reject old, inconsistent data during migration.
    return PortDefinition(role, display_name, AC_POWER, voltage_group=None)


def legacy_equipment_types() -> tuple[EquipmentTypeDefinition, ...]:
    """Private compatibility types serialized with imported v1/v2 projects."""
    two_terminal = (_compat_port("from", "Начало"), _compat_port("to", "Конец"))
    rows = (
        ("compat.rza_calc.source", "Legacy: источник", "legacy.source",
         (_compat_port("terminal", "Вывод"),), True),
        ("compat.rza_calc.generator", "Legacy: генератор", "legacy.generator",
         (_compat_port("terminal", "Вывод"),), True),
        ("compat.rza_calc.line", "Legacy: линия", "legacy.line", two_terminal, True),
        ("compat.rza_calc.transformer_2w", "Legacy: трансформатор 2W",
         "legacy.transformer_2w", two_terminal, True),
        ("compat.rza_calc.tie", "Legacy: нулевая связь", "legacy.tie",
         two_terminal, True),
        ("compat.rza_calc.branch", "Legacy: ветвь", "legacy.branch",
         two_terminal, True),
        ("compat.rza_calc.transformer_3w", "Legacy: трансформатор 3W",
         "legacy.transformer_3w",
         (_compat_port("hv", "ВН"), _compat_port("mv", "СН"),
          _compat_port("lv", "НН")), True),
        ("compat.rza_calc.load", "Legacy: нагрузка", "legacy.load",
         (_compat_port("terminal", "Вывод"),), False),
    )
    payload = (PropertyDefinition(LEGACY_PAYLOAD, required=True),)
    result: list[EquipmentTypeDefinition] = []
    for type_id, title, behavior, ports, has_position in rows:
        capabilities = {
            "legacy.calculation",
            EQUIPMENT_AVAILABILITY_CAPABILITY,
        }
        if has_position:
            capabilities.add("legacy.switch.position")
        result.append(EquipmentTypeDefinition(
            EquipmentTypeId(type_id),
            1,
            title,
            behavior,
            ports,
            property_definitions=payload,
            capabilities=frozenset(capabilities),
            allow_additional_properties=False,
            extensions={"compatibility_only": True},
        ))
    return tuple(result)


_LEGACY_BRANCH_TYPES: dict[type[Branch], tuple[str, tuple[str, ...]]] = {
    SourceBranch: ("compat.rza_calc.source", ("terminal",)),
    GeneratorBranch: ("compat.rza_calc.generator", ("terminal",)),
    LineBranch: ("compat.rza_calc.line", ("from", "to")),
    TransformerBranch: ("compat.rza_calc.transformer_2w", ("from", "to")),
    TieBranch: ("compat.rza_calc.tie", ("from", "to")),
    Branch: ("compat.rza_calc.branch", ("from", "to")),
}


def _legacy_marker(
    category: str,
    legacy_id: str,
    legacy_class: str,
    order: int,
    **extra: Any,
) -> dict[str, Any]:
    marker = {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "category": category,
        "legacy_id": legacy_id,
        "legacy_class": legacy_class,
        "source_order": order,
    }
    marker.update(extra)
    return {LEGACY_EXTENSION: marker}


def _marker(value: Mapping[str, Any], *, required: bool = False) -> dict[str, Any]:
    extensions = thaw_json(value)
    raw = extensions.get(LEGACY_EXTENSION)
    if raw is None:
        if required:
            raise LegacyCalculationAdapterError(
                f"Отсутствует обязательное расширение '{LEGACY_EXTENSION}'."
            )
        return {}
    if not isinstance(raw, dict):
        raise LegacyCalculationAdapterError(
            f"Расширение '{LEGACY_EXTENSION}' должно быть объектом."
        )
    return raw


def _legacy_payload(obj: Any, excluded: set[str]) -> dict[str, Any]:
    return {key: value for key, value in asdict(obj).items() if key not in excluded}


def _voltage_class_for_kv(model: ElectricalModel, value_kv: float) -> VoltageClass:
    if isinstance(value_kv, bool) or not isinstance(value_kv, (int, float)):
        raise LegacyImportError("Номинальное напряжение legacy-узла должно быть числом.")
    if not math.isfinite(float(value_kv)) or value_kv <= 0:
        raise LegacyImportError("Номинальное напряжение legacy-узла должно быть положительным.")
    exact_volts = float(value_kv) * 1000.0
    nominal_voltage_v = round(exact_volts)
    if not math.isclose(exact_volts, nominal_voltage_v, rel_tol=0.0, abs_tol=1e-7):
        raise LegacyImportError(
            f"Напряжение {value_kv!r} кВ нельзя точно представить целым числом вольт."
        )
    for voltage in model.voltage_classes.values():
        if voltage.nominal_voltage_v == nominal_voltage_v:
            return voltage
    voltage_id = _stable_id(VoltageClassId, "legacy", "voltage", str(nominal_voltage_v))
    voltage = VoltageClass(
        voltage_id,
        nominal_voltage_v,
        f"{nominal_voltage_v / 1000.0:g} кВ".replace(".", ","),
        extensions=_legacy_marker(
            "voltage_class", str(nominal_voltage_v), "VoltageClass", 0
        ),
    )
    model.register_voltage_class(voltage)
    return voltage


def _field_overrides(actual: Any, expected: Any, ignored: set[str]) -> dict[str, Any]:
    actual_data, expected_data = asdict(actual), asdict(expected)
    return {
        key: value
        for key, value in actual_data.items()
        if key not in ignored and value != expected_data.get(key)
    }


def _three_winding_generated_overrides(
    net: Network, transformer: Transformer3W
) -> dict[str, Any]:
    """Keep deliberate runtime edits without promoting generated legs to equipment."""
    missing = []
    if transformer.star_node_id not in net.nodes:
        missing.append(transformer.star_node_id)
    missing.extend(
        branch_id for branch_id in transformer.branch_ids
        if branch_id not in net.branches
    )
    if missing:
        raise LegacyImportError(
            f"Transformer3W '{transformer.id}': отсутствуют служебные объекты {missing}."
        )

    expected = Network("expected generated objects")
    for node_id in dict.fromkeys(
        (transformer.node_hv, transformer.node_mv, transformer.node_lv)
    ):
        if node_id not in net.nodes:
            raise LegacyImportError(
                f"Transformer3W '{transformer.id}': узел '{node_id}' отсутствует."
            )
        expected.add_node(deepcopy(net.nodes[node_id]))
    expected.add_transformer3w(deepcopy(transformer))

    actual_star = net.nodes.get(transformer.star_node_id)
    expected_star = expected.nodes[transformer.star_node_id]
    if actual_star is None:
        raise LegacyImportError(
            f"Transformer3W '{transformer.id}': служебный узел звезды отсутствует."
        )
    star_overrides = _field_overrides(actual_star, expected_star, {"id"})
    branch_overrides: dict[str, dict[str, Any]] = {}
    for role, branch_id in zip(("hv", "mv", "lv"), transformer.branch_ids):
        actual = net.branches.get(branch_id)
        generated = expected.branches[branch_id]
        if actual is None:
            raise LegacyImportError(
                f"Transformer3W '{transformer.id}': луч '{branch_id}' отсутствует."
            )
        if (actual.node_from, actual.node_to) != (generated.node_from, generated.node_to):
            raise LegacyImportError(
                f"Transformer3W '{transformer.id}': топология служебного луча "
                f"'{branch_id}' изменена и не может быть импортирована без потерь."
            )
        changes = _field_overrides(actual, generated, {"id", "node_from", "node_to"})
        if changes:
            branch_overrides[role] = changes
    result: dict[str, Any] = {}
    if star_overrides:
        result["star_node"] = star_overrides
    if branch_overrides:
        result["branches"] = branch_overrides
    return result


def _add_legacy_equipment(
    model: ElectricalModel,
    *,
    category: str,
    legacy_id: str,
    obj: Any,
    type_id: str,
    roles_to_nodes: Mapping[str, str],
    order: int,
    excluded_payload_fields: set[str],
    marker_extra: Mapping[str, Any] | None = None,
) -> EquipmentInstance:
    equipment_id = _stable_id(EquipmentId, "legacy", "equipment", category, legacy_id)
    port_ids = {
        role: _stable_id(PortId, "legacy", "port", category, legacy_id, role)
        for role in roles_to_nodes
    }
    extra = dict(marker_extra or {})
    grid_roles = [role for role, node_id in roles_to_nodes.items() if node_id == GRID]
    if grid_roles:
        extra["grid_roles"] = grid_roles
    marker = _legacy_marker(
        category, legacy_id, type(obj).__name__, order, **extra
    )
    switchable = bool(getattr(obj, "switchable", False))
    normal_position = None
    if switchable:
        normal_position = (
            SwitchPosition.CLOSED
            if bool(getattr(obj, "normally_closed", True))
            else SwitchPosition.OPEN
        )
    equipment, _ = model.create_equipment(
        type_id,
        obj.name,
        properties={LEGACY_PAYLOAD: _legacy_payload(obj, excluded_payload_fields)},
        normal_position=normal_position,
        note=getattr(obj, "note", ""),
        extensions=marker,
        equipment_id=equipment_id,
        port_ids_by_role=port_ids,
    )
    for role, legacy_node_id in roles_to_nodes.items():
        if legacy_node_id == GRID:
            continue
        node_id = _stable_id(ElectricalNodeId, "legacy", "node", legacy_node_id)
        if node_id not in model.electrical_nodes:
            raise LegacyImportError(
                f"Объект '{legacy_id}' ссылается на отсутствующий узел "
                f"'{legacy_node_id}'."
            )
        model.connect_port(
            port_ids[role],
            node_id,
            connection_id=_stable_id(
                ConnectionId, "legacy", "connection", category, legacy_id, role
            ),
            extensions=_legacy_marker(
                "connection", f"{category}:{legacy_id}:{role}", "Connection", order
            ),
        )
    return equipment


def import_legacy_network(net: Network) -> ElectricalModel:
    """Deterministically import a calculation DTO into the canonical model.

    Composite legacy branches intentionally remain compatibility equipment.
    Splitting them into fictitious QF/CT/line objects would invent physical
    facts that are absent from v1/v2 files.
    """
    if not isinstance(net, Network):
        raise TypeError("import_legacy_network ожидает core.model.Network.")

    generated_node_ids = {item.star_node_id for item in net.transformers3w.values()}
    generated_branch_ids = {
        branch_id
        for item in net.transformers3w.values()
        for branch_id in item.branch_ids
    }
    model = ElectricalModel.with_builtins(
        net.name,
        neutral=deepcopy(net.neutral),
        extensions={
            LEGACY_EXTENSION: {
                "schema_version": LEGACY_SCHEMA_VERSION,
                "source": "core.Network",
                "generated_node_ids": sorted(generated_node_ids),
                "generated_branch_ids": sorted(generated_branch_ids),
            }
        },
    )
    for definition in legacy_equipment_types():
        model.register_equipment_type(definition)

    for order, node in enumerate(net.nodes.values()):
        if node.id in generated_node_ids:
            continue
        voltage = _voltage_class_for_kv(model, node.u_nom)
        node_id = _stable_id(ElectricalNodeId, "legacy", "node", node.id)
        model.add_node(ElectricalNode(
            node_id,
            node.name,
            AC_POWER,
            voltage.id,
            node.note,
            _legacy_marker(
                "node",
                node.id,
                type(node).__name__,
                order,
                payload={
                    "kind": node.kind,
                    "section": node.section,
                    "calculation_base_kv": node.calculation_base_kv,
                    "prefault_voltage_kv": node.prefault_voltage_kv,
                },
            ),
        ))

    state_targets: dict[str, EquipmentId] = {}
    availability_targets: dict[str, tuple[EquipmentId, str | None]] = {}
    for order, transformer in enumerate(net.transformers3w.values()):
        generated = _three_winding_generated_overrides(net, transformer)
        equipment = _add_legacy_equipment(
            model,
            category="transformer3w",
            legacy_id=transformer.id,
            obj=transformer,
            type_id="compat.rza_calc.transformer_3w",
            roles_to_nodes={
                "hv": transformer.node_hv,
                "mv": transformer.node_mv,
                "lv": transformer.node_lv,
            },
            order=order,
            excluded_payload_fields={
                "id", "name", "node_hv", "node_mv", "node_lv", "note"
            },
            marker_extra={"generated_overrides": generated},
        )
        state_targets[transformer.id] = equipment.id
        availability_targets[transformer.id] = (equipment.id, "hv")
        availability_targets[f"{transformer.id}_mv"] = (equipment.id, "mv")
        availability_targets[f"{transformer.id}_lv"] = (equipment.id, "lv")

    branch_order = 0
    for branch in net.branches.values():
        if branch.id in generated_branch_ids:
            continue
        specification = _LEGACY_BRANCH_TYPES.get(type(branch))
        if specification is None:
            raise LegacyImportError(
                f"Ветвь '{branch.id}' имеет неподдерживаемый класс "
                f"'{type(branch).__name__}'; тип нельзя угадывать по kind или имени."
            )
        type_id, roles = specification
        if isinstance(branch, (SourceBranch, GeneratorBranch)):
            if branch.node_from != GRID or branch.node_to == GRID:
                raise LegacyImportError(
                    f"{type(branch).__name__} '{branch.id}' должен иметь явную "
                    "ориентацию GRID → electrical node."
                )
            role_nodes = {"terminal": branch.node_to}
        else:
            role_nodes = {"from": branch.node_from, "to": branch.node_to}
        equipment = _add_legacy_equipment(
            model,
            category="branch",
            legacy_id=branch.id,
            obj=branch,
            type_id=type_id,
            roles_to_nodes=role_nodes,
            order=branch_order,
            excluded_payload_fields={
                "id", "name", "node_from", "node_to", "note"
            },
        )
        state_targets[branch.id] = equipment.id
        availability_targets[branch.id] = (equipment.id, None)
        branch_order += 1

    for order, load in enumerate(net.loads.values()):
        equipment = _add_legacy_equipment(
            model,
            category="load",
            legacy_id=load.id,
            obj=load,
            type_id="compat.rza_calc.load",
            roles_to_nodes={"terminal": load.node},
            order=order,
            excluded_payload_fields={"id", "name", "node"},
        )
        availability_targets[load.id] = (equipment.id, None)

    for order, mode in enumerate(net.modes.values()):
        positions: dict[EquipmentId, SwitchPosition] = {}
        for target_id, closed in mode.states.items():
            equipment_id = state_targets.get(target_id)
            if equipment_id is not None and isinstance(closed, bool):
                positions[equipment_id] = (
                    SwitchPosition.CLOSED if closed else SwitchPosition.OPEN
                )
        availability: dict[EquipmentId, EquipmentAvailability] = {}
        unavailable_transformer_roles: dict[EquipmentId, set[str]] = {}
        for target_id, available in mode.availability.items():
            if not isinstance(available, bool):
                raise LegacyImportError(
                    f"Режим '{mode.id}': доступность '{target_id}' должна быть bool."
                )
            target = availability_targets.get(target_id)
            if target is None:
                raise LegacyImportError(
                    f"Режим '{mode.id}': доступность ссылается на неизвестный "
                    f"объект '{target_id}'."
                )
            if available:
                continue
            equipment_id, transformer_role = target
            if transformer_role is None:
                availability[equipment_id] = EquipmentAvailability.OUT_OF_SERVICE
            else:
                unavailable_transformer_roles.setdefault(equipment_id, set()).add(
                    transformer_role
                )
        for equipment_id, roles in unavailable_transformer_roles.items():
            if roles == {"hv", "mv", "lv"}:
                availability[equipment_id] = EquipmentAvailability.OUT_OF_SERVICE
        state = OperatingState(
            _stable_id(OperatingStateId, "legacy", "state", mode.id),
            mode.name,
            positions,
            mode.system,
            mode.description,
            _legacy_marker(
                "mode",
                mode.id,
                type(mode).__name__,
                order,
                # The complete sparse dictionary is authoritative for legacy
                # compatibility, including SW:* and Transformer3W leg keys.
                states=deepcopy(mode.states),
                # Per-leg legacy availability cannot always be represented by
                # one equipment-level enum.  Preserve it authoritatively and
                # also map every lossless whole-equipment case above.
                availability=deepcopy(mode.availability),
            ),
            availability,
        )
        model.add_operating_state(state)

    issues = model.validate_integrity()
    if issues:
        raise LegacyImportError(
            "Импорт создал некорректную Domain Model:\n- "
            + "\n- ".join(issue.message for issue in issues)
        )
    return model


@dataclass(frozen=True, slots=True)
class _Behavior:
    category: str
    core_class: type | None
    roles: tuple[str, ...]
    compatibility: bool = False


_BEHAVIORS: dict[str, _Behavior] = {
    "source": _Behavior("branch", SourceBranch, ("terminal",)),
    "generator": _Behavior("branch", GeneratorBranch, ("terminal",)),
    "bus": _Behavior("none", None, ("terminal",)),
    "line": _Behavior("branch", LineBranch, ("from", "to")),
    "line_section": _Behavior("branch", LineBranch, ("from", "to")),
    "switch": _Behavior("branch", TieBranch, ("a", "b")),
    "recloser": _Behavior("branch", TieBranch, ("a", "b")),
    "transformer_2w": _Behavior("branch", TransformerBranch, ("hv", "lv")),
    "transformer_3w": _Behavior("transformer3w", Transformer3W, ("hv", "mv", "lv")),
    "load": _Behavior("load", Load, ("terminal",)),
    "legacy.source": _Behavior("branch", SourceBranch, ("terminal",), True),
    "legacy.generator": _Behavior("branch", GeneratorBranch, ("terminal",), True),
    "legacy.line": _Behavior("branch", LineBranch, ("from", "to"), True),
    "legacy.transformer_2w": _Behavior(
        "branch", TransformerBranch, ("from", "to"), True
    ),
    "legacy.tie": _Behavior("branch", TieBranch, ("from", "to"), True),
    "legacy.branch": _Behavior("branch", Branch, ("from", "to"), True),
    "legacy.transformer_3w": _Behavior(
        "transformer3w", Transformer3W, ("hv", "mv", "lv"), True
    ),
    "legacy.load": _Behavior("load", Load, ("terminal",), True),
}


def _behavior_port_diagnostics(
    model: ElectricalModel,
    equipment: EquipmentInstance,
    definition: EquipmentTypeDefinition,
    behavior: _Behavior,
) -> tuple[AdapterDiagnostic, ...]:
    """Validate the exact instance boundary consumed by a behavior handler.

    Optional definitions which are not instantiated are harmless and remain
    supported.  Once a port exists on an EquipmentInstance, however, the
    adapter must consume it explicitly; otherwise its connection would vanish
    from the calculation graph without a diagnostic.
    """
    issues: list[AdapterDiagnostic] = []
    expected_roles = set(behavior.roles)
    definitions_by_role = {
        port_definition.role: port_definition
        for port_definition in definition.port_definitions
    }
    actual_ports = model.ports_of(equipment.id)
    actual_roles = [port.role for port in actual_ports]
    actual_role_set = set(actual_roles)

    missing_definitions = expected_roles - set(definitions_by_role)
    if missing_definitions:
        issues.append(AdapterDiagnostic(
            "error",
            "behavior_port_definition_missing",
            f"Behavior '{definition.behavior_key}' требует определения портов "
            f"{sorted(missing_definitions)}.",
            equipment.id.value,
        ))

    wrong_kind = sorted(
        role for role in expected_roles & set(definitions_by_role)
        if definitions_by_role[role].kind_id != AC_POWER
    )
    if wrong_kind:
        issues.append(AdapterDiagnostic(
            "error",
            "behavior_port_kind_mismatch",
            f"Behavior '{definition.behavior_key}' поддерживает только AC power "
            f"порты; несовместимые роли: {wrong_kind}.",
            equipment.id.value,
        ))

    missing_instances = expected_roles - actual_role_set
    if missing_instances:
        issues.append(AdapterDiagnostic(
            "error",
            "behavior_port_instance_missing",
            f"EquipmentInstance для behavior '{definition.behavior_key}' не имеет "
            f"обязательных для адаптера портов {sorted(missing_instances)}.",
            equipment.id.value,
        ))

    extra_instances = actual_role_set - expected_roles
    if extra_instances:
        issues.append(AdapterDiagnostic(
            "error",
            "behavior_port_instance_extra",
            f"EquipmentInstance для behavior '{definition.behavior_key}' содержит "
            f"неподдерживаемые порты {sorted(extra_instances)}; adapter не может "
            "молча отбросить их connections.",
            equipment.id.value,
        ))

    if len(actual_roles) != len(actual_role_set):
        issues.append(AdapterDiagnostic(
            "error",
            "behavior_port_instance_duplicate",
            f"EquipmentInstance для behavior '{definition.behavior_key}' содержит "
            "повторяющиеся роли портов.",
            equipment.id.value,
        ))
    return tuple(issues)


def _source_order(extensions: Mapping[str, Any], fallback: int) -> tuple[int, int]:
    marker = _marker(extensions)
    raw = marker.get("source_order")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return 0, raw
    return 1, fallback


def _safe_native_core_id(category: str, domain_id: str) -> str:
    token = uuid5(_CORE_ID_NAMESPACE, f"{category}/{domain_id}").hex
    prefix = {"node": "N", "branch": "B", "transformer3w": "T3", "load": "L",
              "mode": "M"}.get(category, "O")
    return f"{prefix}_{token}"


def _legacy_or_native_id(
    category: str, domain_id: str, extensions: Mapping[str, Any]
) -> str:
    marker = _marker(extensions)
    legacy_id = marker.get("legacy_id")
    if legacy_id is None:
        return _safe_native_core_id(category, domain_id)
    if not isinstance(legacy_id, str) or not legacy_id.strip():
        raise LegacyCalculationAdapterError(
            f"Объект '{domain_id}': legacy_id должен быть непустой строкой."
        )
    return legacy_id


def _plain_payload(
    model: ElectricalModel,
    equipment: EquipmentInstance,
    behavior: _Behavior,
) -> dict[str, Any]:
    properties = thaw_json(equipment.properties)
    if not isinstance(properties, dict):
        raise LegacyCalculationAdapterError(
            f"Оборудование '{equipment.id}': properties должен быть объектом."
        )
    if behavior.compatibility:
        if set(properties) != {LEGACY_PAYLOAD}:
            raise LegacyCalculationAdapterError(
                f"Compatibility equipment '{equipment.id}' должно содержать только "
                f"свойство '{LEGACY_PAYLOAD}'."
            )
        payload = properties[LEGACY_PAYLOAD]
        if not isinstance(payload, dict):
            raise LegacyCalculationAdapterError(
                f"Оборудование '{equipment.id}': legacy_payload должен быть объектом."
            )
        marker = _marker(equipment.extensions, required=True)
        expected = behavior.core_class.__name__ if behavior.core_class is not None else ""
        if marker.get("legacy_class") != expected:
            raise LegacyCalculationAdapterError(
                f"Оборудование '{equipment.id}': legacy_class "
                f"'{marker.get('legacy_class')}' не соответствует behavior."
            )
        return dict(payload)
    # Native types receive catalog/type defaults through the Domain query.
    # Compatibility equipment must keep using the exact embedded payload
    # above so v1/v2 round-trip remains byte-semantically faithful.
    return _effective_properties(model, equipment)


_MISSING = object()

# These maps are deliberately narrow.  A canonical equipment type may carry
# catalog, nameplate and future calculation properties which are not fields of
# the legacy DTO.  Such data remains in ElectricalModel and must never leak
# through ``**properties`` into a dataclass constructor by coincidence.
_RECLOSER_PROPERTY_MAP = {
    "full_opening_time_s": "breaker_t_off",
}
_RECLOSER_LEGACY_FALLBACKS = {
    "full_opening_time_s": "breaker_t_off",
}
_LINE_SECTION_PROPERTY_MAP = {
    "conductor_mark": "brand",
    "cross_section_mm2": "section_mm2",
    "material": "material",
    "parallel_count": "n_parallel",
    # Historical core names r0/x0 mean the per-kilometre impedance used by
    # its positive-sequence short-circuit calculation.  They must therefore
    # receive canonical r1/x1, never canonical zero-sequence values.
    "r1_ohm_per_km": "r0",
    "x1_ohm_per_km": "x0",
    "capacitive_current_a_per_km": "ic_per_km",
}
_LINE_SECTION_LEGACY_FALLBACKS = {
    "conductor_mark": "brand",
    "cross_section_mm2": "section_mm2",
    "parallel_count": "n_parallel",
    "r1_ohm_per_km": "r0",
    "x1_ohm_per_km": "x0",
    "capacitive_current_a_per_km": "ic_per_km",
}


def _optional_model_api(model: ElectricalModel, name: str):
    """Return an optional Domain query without masking a broken implementation."""
    value = getattr(model, name, None)
    if value is None:
        return None
    if not callable(value):
        raise LegacyCalculationAdapterError(
            f"ElectricalModel.{name} должен быть вызываемым методом."
        )
    return value


def _effective_properties(
    model: ElectricalModel,
    equipment: EquipmentInstance,
) -> dict[str, Any]:
    """Resolve catalog-overlaid properties, with a pre-catalog safe fallback.

    Contract of the optional Domain API::

        effective_equipment_properties(EquipmentId) -> Mapping[str, JSON]

    When the API exists it is authoritative: exceptions and invalid return
    values are adapter errors, not reasons to fall back to raw instance data.
    """
    resolver = _optional_model_api(model, "effective_equipment_properties")
    raw = resolver(equipment.id) if resolver is not None else equipment.properties
    if not isinstance(raw, Mapping):
        raise LegacyCalculationAdapterError(
            f"Оборудование '{equipment.id}': effective properties должны быть объектом."
        )
    properties = thaw_json(raw)
    if not isinstance(properties, dict):
        raise LegacyCalculationAdapterError(
            f"Оборудование '{equipment.id}': effective properties должны быть объектом."
        )
    return properties


def _canonical_or_legacy_value(
    properties: Mapping[str, Any],
    canonical_key: str,
    legacy_key: str | None,
    *,
    context: str,
) -> tuple[Any, frozenset[str]]:
    """Read one explicitly supported value and reject conflicting aliases."""
    canonical = properties.get(canonical_key, _MISSING)
    legacy = (
        properties.get(legacy_key, _MISSING)
        if legacy_key is not None and legacy_key != canonical_key
        else _MISSING
    )
    if canonical is not _MISSING and legacy is not _MISSING and canonical != legacy:
        raise LegacyCalculationAdapterError(
            f"{context}: свойства '{canonical_key}' и '{legacy_key}' конфликтуют."
        )
    if canonical is not _MISSING:
        keys = {canonical_key}
        if legacy is not _MISSING and legacy_key is not None:
            keys.add(legacy_key)
        return canonical, frozenset(keys)
    if legacy is not _MISSING:
        return legacy, frozenset({legacy_key})
    return _MISSING, frozenset()


def _finite_number(value: Any, *, context: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LegacyCalculationAdapterError(f"{context} должно быть числом.")
    result = float(value)
    if not math.isfinite(result):
        raise LegacyCalculationAdapterError(f"{context} должно быть конечным числом.")
    if positive and result <= 0:
        raise LegacyCalculationAdapterError(f"{context} должно быть больше нуля.")
    if not positive and result < 0:
        raise LegacyCalculationAdapterError(f"{context} не может быть отрицательным.")
    return result


def _positive_length_mm(value: Any, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LegacyCalculationAdapterError(
            f"{context} должно быть целым числом миллиметров."
        )
    if value <= 0:
        raise LegacyCalculationAdapterError(f"{context} должно быть больше нуля.")
    return value


def _recloser_payload(
    model: ElectricalModel,
    equipment: EquipmentInstance,
) -> tuple[dict[str, Any], frozenset[str]]:
    properties = _effective_properties(model, equipment)
    payload: dict[str, Any] = {}
    consumed: set[str] = set()
    for canonical_key, dto_key in _RECLOSER_PROPERTY_MAP.items():
        value, keys = _canonical_or_legacy_value(
            properties,
            canonical_key,
            _RECLOSER_LEGACY_FALLBACKS.get(canonical_key),
            context=f"Реклоузер '{equipment.id}'",
        )
        consumed.update(keys)
        if value is _MISSING or value is None:
            continue
        if canonical_key == "full_opening_time_s":
            value = _finite_number(
                value,
                context=f"Реклоузер '{equipment.id}': full_opening_time_s",
            )
        payload[dto_key] = value
    return payload, frozenset(set(properties) - consumed)


def _line_section_geometry(
    model: ElectricalModel,
    equipment: EquipmentInstance,
    properties: Mapping[str, Any],
) -> tuple[float, str, frozenset[str]]:
    """Resolve authoritative section length and logical-line kind.

    Preferred Domain API contract::

        line_section_for_equipment(EquipmentId) -> LineSection
        LineSection.length_mm
        logical_line_for_section(EquipmentId) -> LogicalLine
        LogicalLine.line_kind.value in {"cable", "overhead"}

    Until those queries exist, explicit ``length_mm`` (or compatibility
    ``length_km``) and ``line_type`` effective properties form the fallback.
    """
    consumed: set[str] = set()
    section_resolver = _optional_model_api(model, "line_section_for_equipment")
    logical_line_resolver = _optional_model_api(model, "logical_line_for_section")

    if section_resolver is not None:
        section = section_resolver(equipment.id)
        if section is None:
            raise LegacyCalculationAdapterError(
                f"Оборудование '{equipment.id}': Domain API не вернул LineSection."
            )
        length_mm = getattr(section, "length_mm", _MISSING)
        if length_mm is _MISSING:
            raise LegacyCalculationAdapterError(
                f"LineSection оборудования '{equipment.id}' не имеет length_mm."
            )
        length_km = _positive_length_mm(
            length_mm,
            context=f"LineSection оборудования '{equipment.id}': length_mm",
        ) / 1_000_000.0

        if logical_line_resolver is not None:
            logical_line = logical_line_resolver(equipment.id)
            if logical_line is None:
                raise LegacyCalculationAdapterError(
                    f"LineSection оборудования '{equipment.id}' не связан с LogicalLine."
                )
            line_kind = getattr(logical_line, "line_kind", _MISSING)
            if line_kind is _MISSING:
                raise LegacyCalculationAdapterError(
                    f"LogicalLine для LineSection '{equipment.id}' не имеет line_kind."
                )
            line_type = getattr(line_kind, "value", line_kind)
        else:
            line_type = properties.get("line_type", _MISSING)
            if line_type is not _MISSING:
                consumed.add("line_type")
    else:
        length_mm, length_mm_keys = _canonical_or_legacy_value(
            properties,
            "length_mm",
            None,
            context=f"LineSection оборудования '{equipment.id}'",
        )
        length_km_raw = properties.get("length_km", _MISSING)
        if length_mm is not _MISSING and length_km_raw is not _MISSING:
            converted = _positive_length_mm(
                length_mm,
                context=f"LineSection оборудования '{equipment.id}': length_mm",
            ) / 1_000_000.0
            legacy_length = _finite_number(
                length_km_raw,
                context=f"LineSection оборудования '{equipment.id}': length_km",
                positive=True,
            )
            if not math.isclose(converted, legacy_length, rel_tol=0.0, abs_tol=1e-12):
                raise LegacyCalculationAdapterError(
                    f"LineSection оборудования '{equipment.id}': length_mm и "
                    "length_km конфликтуют."
                )
            length_km = converted
            consumed.update(length_mm_keys)
            consumed.add("length_km")
        elif length_mm is not _MISSING:
            length_km = _positive_length_mm(
                length_mm,
                context=f"LineSection оборудования '{equipment.id}': length_mm",
            ) / 1_000_000.0
            consumed.update(length_mm_keys)
        elif length_km_raw is not _MISSING:
            length_km = _finite_number(
                length_km_raw,
                context=f"LineSection оборудования '{equipment.id}': length_km",
                positive=True,
            )
            consumed.add("length_km")
        else:
            raise LegacyCalculationAdapterError(
                f"LineSection оборудования '{equipment.id}' не имеет длины; "
                "нужен Domain API или effective property length_mm."
            )
        line_type = properties.get("line_type", _MISSING)
        if line_type is not _MISSING:
            consumed.add("line_type")

    if line_type not in {"cable", "overhead"}:
        raise LegacyCalculationAdapterError(
            f"LineSection оборудования '{equipment.id}': line kind должен быть "
            "'cable' или 'overhead'."
        )
    return length_km, str(line_type), frozenset(consumed)


def _line_sequence_fields(
    rows: list[tuple[Any, float, str, dict[str, Any]]],
    payload: dict[str, Any],
    consumed: set[str],
) -> None:
    """Carry explicit sequences without mistaking legacy r0/x0 for Z0.

    Every construction segment must supply both components.  A partial
    sequence stays absent in the DTO; the original values remain in the
    canonical project and in the adapter's unused-property diagnostic.
    The DTO's parallel count is retained for a simple segment, or accounted
    for while producing a series equivalent for a composite section.
    """
    properties = [row[3] for row in rows]
    for sequence in (2, 0):
        keys = (f"r{sequence}_ohm_per_km", f"x{sequence}_ohm_per_km")
        if not all(all(item.get(key) is not None for key in keys) for item in properties):
            continue
        if len(rows) == 1:
            for key in keys:
                payload[key] = properties[0][key]
        else:
            total_length = sum(row[1] for row in rows)
            # An unknown segment length is not a zero-length conductor.
            if total_length <= 0 or any(row[1] <= 0 for row in rows):
                continue
            target_parallel = payload.get("n_parallel", 1)
            for key in keys:
                total = 0.0
                for _, length_km, _, item in rows:
                    parallel = item.get("parallel_count", 1)
                    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
                        raise LegacyCalculationAdapterError(
                            "LineSection: parallel_count должен быть положительным целым."
                        )
                    total += _finite_number(item[key], context=f"LineSection: {key}") * length_km / parallel
                payload[key] = total * target_parallel / total_length
        consumed.update(keys)

    equal_key = "negative_sequence_equal_positive"
    if any(equal_key in item for item in properties):
        consumed.add(equal_key)
        if all(item.get(equal_key) is True for item in properties):
            payload[equal_key] = True

    connection_key = "zero_sequence_connection"
    connections = [item.get(connection_key) for item in properties]
    if len(rows) == 1:
        if connections[0] is not None:
            payload[connection_key] = connections[0]
            consumed.add(connection_key)
    elif any(value is not None for value in connections):
        if all(value in (None, "series") for value in connections):
            payload[connection_key] = "series"
        elif all(value == "blocked" for value in connections):
            payload[connection_key] = "blocked"
        else:
            # A lost zero-sequence barrier must not become the conventional
            # default series line.  Segment shunts and mixed topologies need
            # a richer equivalent; this marker is rejected ONLY by the new
            # zero-sequence calculation, without blocking legacy 3ph.
            payload[connection_key] = "unsupported_composite"
        consumed.add(connection_key)


def _line_section_payload(
    model: ElectricalModel,
    equipment: EquipmentInstance,
) -> tuple[dict[str, Any], frozenset[str], tuple[AdapterDiagnostic, ...]]:
    """Create a compatibility line DTO from one canonical line branch.

    The v6 Domain may keep several ordered construction segments between the
    same two electrical nodes.  They remain one calculation branch.  When
    every segment carries explicit r1/x1, the legacy DTO receives an exact
    series equivalent.  Otherwise the DTO remains available for the old UI,
    but a visible diagnostic states that its single nameplate row is only a
    compatibility approximation.  The canonical projection never loses the
    segment records.
    """
    diagnostics: list[AdapterDiagnostic] = []
    section_resolver = _optional_model_api(model, "line_section_for_equipment")
    segment_resolver = _optional_model_api(
        model, "effective_line_construction_segment_properties"
    )
    section = section_resolver(equipment.id) if section_resolver is not None else None
    raw_segments = getattr(section, "construction_segments", ()) if section is not None else ()
    segment_rows: list[tuple[Any, float, str, dict[str, Any]]] = []
    if raw_segments:
        for segment in raw_segments:
            raw_length_mm = getattr(segment, "length_mm", None)
            length_km = (
                0.0
                if raw_length_mm is None
                else _positive_length_mm(
                    raw_length_mm,
                    context=(
                        f"Конструктивный участок "
                        f"'{getattr(segment, 'id', '?')}': length_mm"
                    ),
                ) / 1_000_000.0
            )
            kind = getattr(getattr(segment, "line_kind", None), "value", None)
            if kind not in {"overhead", "cable", "busduct"}:
                raise LegacyCalculationAdapterError(
                    f"Конструктивный участок '{getattr(segment, 'id', '?')}' "
                    "имеет неподдерживаемый вид."
                )
            if segment_resolver is not None:
                effective = segment_resolver(equipment.id, segment.id)
                if not isinstance(effective, Mapping):
                    raise LegacyCalculationAdapterError(
                        f"Конструктивный участок '{segment.id}': effective properties "
                        "должны быть объектом."
                    )
                properties = thaw_json(effective)
            else:
                properties = _effective_properties(model, equipment)
                properties.update(thaw_json(getattr(segment, "properties", {})))
            segment_rows.append((segment, length_km, kind, properties))

    if not segment_rows:
        properties = _effective_properties(model, equipment)
        length_km, line_type, geometry_keys = _line_section_geometry(
            model, equipment, properties
        )
        segment_rows = [(None, length_km, line_type, properties)]
        consumed = set(geometry_keys)
    else:
        consumed = set()

    total_length_km = sum(item[1] for item in segment_rows)
    kinds = {item[2] for item in segment_rows}
    line_type = (
        next(iter(kinds))
        if len(kinds) == 1 and "busduct" not in kinds
        else "cable"
        if {"cable", "busduct"} & kinds
        else "overhead"
    )
    payload: dict[str, Any] = {
        "length_km": total_length_km,
        "line_type": line_type,
    }
    length_unconfirmed_reasons: list[str] = []
    impedance_unconfirmed_reasons: list[str] = []
    if raw_segments:
        for segment, _, _, properties in segment_rows:
            segment_name = getattr(getattr(segment, "id", None), "value", "?")
            if (
                getattr(segment, "length_mm", None) is None
                or getattr(
                    segment,
                    "length_confirmation",
                    DataConfirmation.UNCONFIRMED,
                ) is not DataConfirmation.CONFIRMED
            ):
                length_unconfirmed_reasons.append(
                    f"у конструктивного участка '{segment_name}' не подтверждена длина"
                )
            if (
                getattr(
                    segment,
                    "impedance_confirmation",
                    DataConfirmation.UNCONFIRMED,
                ) is not DataConfirmation.CONFIRMED
                or properties.get("r1_ohm_per_km") is None
                or properties.get("x1_ohm_per_km") is None
            ):
                impedance_unconfirmed_reasons.append(
                    f"у конструктивного участка '{segment_name}' не подтверждены r1/x1"
                )
    # Неизвестная/неподтверждённая физическая длина всегда получает новый
    # точный blocker. Для одиночной обычной ВЛ/КЛ такой же blocker нужен при
    # неподтверждённом сопротивлении. Составные ветви и шинопроводы сохраняют
    # свои более конкретные диагностические коды ниже, но тоже проверяют typed
    # confirmation, а не только наличие чисел.
    generic_unconfirmed_reasons = list(length_unconfirmed_reasons)
    if len(segment_rows) == 1 and segment_rows[0][2] != "busduct":
        generic_unconfirmed_reasons.extend(impedance_unconfirmed_reasons)
    if generic_unconfirmed_reasons:
        # Ноль здесь является только невозможным для расчёта значением старого
        # производного DTO. В канонической модели неизвестная длина остаётся
        # именно None и никогда не подменяется фиктивными миллиметрами.
        if any(getattr(item[0], "length_mm", None) is None for item in segment_rows):
            payload["length_km"] = 0.0
        first_properties = segment_rows[0][3]
        for canonical_key, dto_key in _LINE_SECTION_PROPERTY_MAP.items():
            legacy_key = _LINE_SECTION_LEGACY_FALLBACKS.get(canonical_key)
            value, keys = _canonical_or_legacy_value(
                first_properties,
                canonical_key,
                legacy_key,
                context=f"LineSection оборудования '{equipment.id}'",
            )
            consumed.update(keys)
            if value is not _MISSING:
                payload[dto_key] = value
        block_reason = "; ".join(dict.fromkeys(generic_unconfirmed_reasons))
        payload["calculation_block_reason"] = block_reason
        diagnostics.append(AdapterDiagnostic(
            "error",
            "line_data_unconfirmed",
            f"Расчёт линии '{equipment.name}' заблокирован: {block_reason}.",
            equipment.id.value,
        ))
        _line_sequence_fields(segment_rows, payload, consumed)
        unused = set().union(
            *(set(item[3]) for item in segment_rows)
        ) - consumed
        return payload, frozenset(unused), tuple(diagnostics)
    if len(segment_rows) == 1:
        properties = segment_rows[0][3]
        for canonical_key, dto_key in _LINE_SECTION_PROPERTY_MAP.items():
            legacy_key = _LINE_SECTION_LEGACY_FALLBACKS.get(canonical_key)
            value, keys = _canonical_or_legacy_value(
                properties,
                canonical_key,
                legacy_key,
                context=f"LineSection оборудования '{equipment.id}'",
            )
            consumed.update(keys)
            if value is not _MISSING:
                payload[dto_key] = value
        if segment_rows[0][2] == "busduct":
            busduct_has_explicit_impedance = (
                properties.get("r1_ohm_per_km") is not None
                and properties.get("x1_ohm_per_km") is not None
                and getattr(
                    segment_rows[0][0],
                    "impedance_confirmation",
                    DataConfirmation.UNCONFIRMED,
                ) is DataConfirmation.CONFIRMED
            )
            if busduct_has_explicit_impedance:
                diagnostics.append(AdapterDiagnostic(
                    "info",
                    "busduct_legacy_line_type",
                    "Шинопровод сохранён в канонической модели; старый расчётный DTO "
                    "показывает его как кабельную ветвь с явно заданными r1/x1.",
                    equipment.id.value,
                ))
            else:
                block_reason = (
                    "для шинопровода не заданы подтверждённые удельные "
                    "сопротивления r1 и x1"
                )
                payload["calculation_block_reason"] = block_reason
                diagnostics.append(AdapterDiagnostic(
                    "error",
                    "busduct_legacy_calculation_blocked",
                    "Старое расчётное ядро не подменяет шинопровод кабелем: "
                    f"{block_reason}.",
                    equipment.id.value,
                ))
        _line_sequence_fields(segment_rows, payload, consumed)
        unused = set(properties) - consumed
        return payload, frozenset(unused), tuple(diagnostics)

    # An exact series equivalent is possible only from explicit per-kilometre
    # values.  ``parallel_count`` is included before the equivalent is reduced
    # to one legacy circuit.
    explicit = all(
        row[3].get("r1_ohm_per_km") is not None
        and row[3].get("x1_ohm_per_km") is not None
        and getattr(
            row[0],
            "impedance_confirmation",
            DataConfirmation.UNCONFIRMED,
        ) is DataConfirmation.CONFIRMED
        for row in segment_rows
    )
    all_properties = [row[3] for row in segment_rows]
    if explicit:
        total_r = 0.0
        total_x = 0.0
        total_ic = 0.0
        has_all_ic = all(
            properties.get("capacitive_current_a_per_km") is not None
            for properties in all_properties
        )
        for _, length_km, _, properties in segment_rows:
            parallel_raw = properties.get("parallel_count", 1)
            if (
                isinstance(parallel_raw, bool)
                or not isinstance(parallel_raw, int)
                or parallel_raw < 1
            ):
                raise LegacyCalculationAdapterError(
                    f"LineSection оборудования '{equipment.id}': parallel_count "
                    "должен быть положительным целым."
                )
            r1 = _finite_number(
                properties["r1_ohm_per_km"],
                context=f"LineSection оборудования '{equipment.id}': r1_ohm_per_km",
            )
            x1 = _finite_number(
                properties["x1_ohm_per_km"],
                context=f"LineSection оборудования '{equipment.id}': x1_ohm_per_km",
            )
            total_r += r1 * length_km / parallel_raw
            total_x += x1 * length_km / parallel_raw
            if has_all_ic:
                ic = _finite_number(
                    properties["capacitive_current_a_per_km"],
                    context=(
                        f"LineSection оборудования '{equipment.id}': "
                        "capacitive_current_a_per_km"
                    ),
                )
                total_ic += ic * length_km * parallel_raw
        payload.update({
            "r0": total_r / total_length_km,
            "x0": total_x / total_length_km,
            "n_parallel": 1,
            "brand": f"Составная ветвь ({len(segment_rows)} участка)",
        })
        if has_all_ic:
            payload["ic_per_km"] = total_ic / total_length_km
        consumed.update({
            "r1_ohm_per_km", "x1_ohm_per_km", "parallel_count",
        })
        if has_all_ic:
            consumed.add("capacitive_current_a_per_km")
        diagnostics.append(AdapterDiagnostic(
            "info",
            "composite_line_legacy_equivalent",
            "Составная электрическая ветвь сведена для старого DTO к точному "
            "последовательному эквиваленту r1/x1; исходные участки сохранены.",
            equipment.id.value,
        ))
    else:
        # Preserve old screens without inventing missing impedances.  The first
        # nameplate row remains a compatibility-only view, while the explicit
        # DTO marker prevents the old engine from calculating with it.
        first_properties = all_properties[0]
        for canonical_key, dto_key in _LINE_SECTION_PROPERTY_MAP.items():
            legacy_key = _LINE_SECTION_LEGACY_FALLBACKS.get(canonical_key)
            value, keys = _canonical_or_legacy_value(
                first_properties,
                canonical_key,
                legacy_key,
                context=f"LineSection оборудования '{equipment.id}'",
            )
            consumed.update(keys)
            if value is not _MISSING:
                payload[dto_key] = value
        block_reason = (
            "не на всех конструктивных участках составной ветви заданы "
            "подтверждённые удельные сопротивления r1 и x1"
        )
        payload["calculation_block_reason"] = block_reason
        diagnostics.append(AdapterDiagnostic(
            "error",
            "composite_line_legacy_calculation_blocked",
            "Старый DTO сохранён только для просмотра первой марки; "
            f"расчёт заблокирован: {block_reason}.",
            equipment.id.value,
        ))
    _line_sequence_fields(segment_rows, payload, consumed)
    unused = set().union(*(set(item) for item in all_properties)) - consumed
    return payload, frozenset(unused), tuple(diagnostics)


def _coerce_special_fields(cls: type, values: dict[str, Any]) -> None:
    if "ct_ratio" in values and isinstance(values["ct_ratio"], list):
        values["ct_ratio"] = tuple(values["ct_ratio"])
    if "prot" in values and isinstance(values["prot"], dict):
        allowed = {item.name for item in fields(ProtectionSettings)}
        unknown = set(values["prot"]) - allowed
        if unknown:
            raise LegacyCalculationAdapterError(
                f"ProtectionSettings: неизвестные поля {sorted(unknown)}."
            )
        values["prot"] = ProtectionSettings(**values["prot"])


def _construct(cls: type, values: Mapping[str, Any], context: str):
    data = dict(values)
    allowed = {item.name for item in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise LegacyCalculationAdapterError(
            f"{context}: свойства {sorted(unknown)} не поддерживаются legacy DTO "
            f"{cls.__name__}."
        )
    _coerce_special_fields(cls, data)
    try:
        return cls(**data)
    except (TypeError, ValueError) as exc:
        raise LegacyCalculationAdapterError(
            f"{context}: не удалось создать {cls.__name__}: {exc}"
        ) from exc


def _apply_overrides(target: Any, raw: Mapping[str, Any], context: str) -> None:
    values = dict(raw)
    allowed = {item.name for item in fields(type(target))} - {"id", "node_from", "node_to"}
    unknown = set(values) - allowed
    if unknown:
        raise LegacyCalculationAdapterError(
            f"{context}: недопустимые override-поля {sorted(unknown)}."
        )
    _coerce_special_fields(type(target), values)
    for key, value in values.items():
        setattr(target, key, value)


def _node_for_role(
    model: ElectricalModel,
    equipment: EquipmentInstance,
    role: str,
    node_ids: Mapping[str, str],
    *,
    allow_grid_marker: bool = False,
) -> str:
    port = model.port_by_role(equipment.id, role)
    node = model.node_for_port(port.id)
    if node is not None:
        try:
            return node_ids[node.id.value]
        except KeyError as exc:
            raise LegacyCalculationAdapterError(
                f"Порт '{port.id}' ссылается на узел без calculation ID."
            ) from exc
    if allow_grid_marker:
        marker = _marker(equipment.extensions)
        grid_roles = marker.get("grid_roles", [])
        if isinstance(grid_roles, list) and role in grid_roles:
            return GRID
    raise LegacyCalculationAdapterError(
        f"Оборудование '{equipment.id}': обязательный порт '{role}' не подключён."
    )


def _native_defaults(
    equipment: EquipmentInstance,
    behavior_key: str,
    payload: dict[str, Any],
) -> None:
    # Canonical equipment uses explicit switch objects.  Conductors and sources
    # must not silently synthesize additional legacy endpoint breakers.
    if behavior_key in {
        "source", "generator", "line", "line_section",
        "transformer_2w", "transformer_3w"
    }:
        payload["switchable"] = False
        payload["normally_closed"] = True
    if behavior_key in {"switch", "recloser"}:
        payload["switchable"] = True
        if equipment.normal_position is not None:
            payload["normally_closed"] = equipment.normal_position == SwitchPosition.CLOSED
    if behavior_key == "line" and "line_type" not in payload:
        payload["line_type"] = (
            "cable" if equipment.type_id.value == "builtin.cable" else "overhead"
        )


def _parameter_inputs(model, equipment, behavior, payload, node_ids):
    """Project explicit provenance and the physical CT terminal into a fresh DTO."""
    if behavior.category not in {"branch", "transformer3w", "load"}:
        return
    provenance = equipment.extensions.get("rza_calc.parameter_provenance", {})
    if not isinstance(provenance, Mapping):
        raise LegacyCalculationAdapterError(f"«{equipment.name}»: неверный формат источников параметров.")
    provenance = thaw_json(provenance)
    section = model.line_sections.get(equipment.id)
    if section is not None:
        # Each segment supplies its own equivalent. An overridden segment
        # cannot borrow its parent's confirmation for another numeric value.
        keys = {"r2_ohm_per_km", "x2_ohm_per_km", "r0_ohm_per_km", "x0_ohm_per_km",
                "negative_sequence_equal_positive", "zero_sequence_connection", "capacitive_current_a_per_km"}
        for key in keys:
            records = []
            for segment in section.construction_segments:
                local = segment.extensions.get("rza_calc.parameter_provenance", {})
                if not isinstance(local, Mapping):
                    raise LegacyCalculationAdapterError(f"«{equipment.name}»: неверные источники данных участка.")
                if key in local:
                    records.append(local[key])
                elif key not in segment.properties and key in provenance:
                    records.append(provenance[key])
            provenance.pop(key, None)
            if records:
                chosen = records[0]
                for record in records:
                    if (not isinstance(record, Mapping) or record.get("confirmation") != "confirmed"
                            or not isinstance(record.get("source"), str) or not record["source"].strip()):
                        chosen = record
                        break
                provenance[key] = thaw_json(chosen)
    if provenance:
        payload["parameter_provenance"] = provenance
    if behavior.category == "load":
        return
    ct = equipment.extensions.get("rza_calc.ct_parameters", {})
    allowed_ct = {"ct_ratio", "ct_accuracy", "breaker_t_off", "terminal"}
    if not isinstance(ct, Mapping) or set(ct) - allowed_ct:
        raise LegacyCalculationAdapterError(f"«{equipment.name}»: неверный формат параметров ТТ.")
    supported = {item.name for item in fields(behavior.core_class)}
    for key, value in ct.items():
        if key not in supported:
            raise LegacyCalculationAdapterError(f"«{equipment.name}»: параметр {key} пока не поддерживается этим присоединением.")
        payload[key] = thaw_json(value)
    if "rza_calc.ct_port_id" in equipment.extensions:
        raw = equipment.extensions["rza_calc.ct_port_id"]
        if raw is None:
            payload["ct_node"] = None
        else:
            port = next((p for p in model.ports_of(equipment.id) if p.id.value == raw), None)
            node = model.node_for_port(port.id) if port is not None else None
            if node is None or node.id.value not in node_ids:
                raise LegacyCalculationAdapterError(f"«{equipment.name}»: сторона установки ТТ не принадлежит подключённому выводу аппарата.")
            payload["ct_node"] = node_ids[node.id.value]


def adapt_to_calculation(
    model: ElectricalModel,
    topology_snapshot: TopologySnapshot | None = None,
) -> AdaptationResult:
    """Build a fresh legacy ``Network`` plus stable result trace.

    Unsupported electrical behavior is a blocking error.  The adapter never
    guesses a class from an equipment name or silently omits an unknown object.
    """
    if not isinstance(model, ElectricalModel):
        raise TypeError("adapt_to_calculation ожидает ElectricalModel.")
    if topology_snapshot is not None:
        if not isinstance(topology_snapshot, TopologySnapshot):
            raise TypeError("topology_snapshot должен быть TopologySnapshot.")
        try:
            topology_snapshot.assert_compatible(
                model_identity=electrical_model_identity(model),
                model_revision=model.revision,
            )
        except Exception as exc:
            diagnostic = AdapterDiagnostic(
                "error",
                "topology_snapshot_incompatible",
                str(exc),
            )
            raise LegacyCalculationAdapterError(
                "Topology snapshot относится к другой модели или ревизии.",
                (diagnostic,),
            ) from exc
        blockers = tuple(
            AdapterDiagnostic(
                "error",
                f"topology.{item.code}",
                item.message,
                (
                    item.equipment_id.value if item.equipment_id is not None
                    else item.port_id.value if item.port_id is not None
                    else item.electrical_node_id.value
                    if item.electrical_node_id is not None
                    else item.connection_id.value
                    if item.connection_id is not None
                    else ""
                ),
            )
            for item in topology_snapshot.diagnostics
            if item.severity == DiagnosticSeverity.ERROR
        )
        if blockers:
            raise LegacyCalculationAdapterError(
                "Topology snapshot содержит блокирующие ошибки.", blockers
            )
    integrity = model.validate_integrity()
    if integrity:
        diagnostics = tuple(AdapterDiagnostic(
            issue.severity, issue.code, issue.message, issue.object_id
        ) for issue in integrity)
        raise LegacyCalculationAdapterError(
            "Domain Model нарушает инварианты целостности.", diagnostics
        )

    diagnostics: list[AdapterDiagnostic] = []
    node_rows = list(model.electrical_nodes.values())
    node_index = {item.id: index for index, item in enumerate(node_rows)}
    node_rows.sort(key=lambda item: _source_order(
        item.extensions, node_index[item.id]
    ))
    node_ids: dict[str, str] = {}
    used_node_ids: dict[str, str] = {}
    for node in node_rows:
        output_id = _legacy_or_native_id("node", node.id.value, node.extensions)
        if output_id == GRID:
            raise LegacyCalculationAdapterError(
                f"Domain node '{node.id}' не может адаптироваться в зарезервированный GRID."
            )
        previous = used_node_ids.get(output_id)
        if previous is not None:
            raise LegacyCalculationAdapterError(
                f"Calculation node ID '{output_id}' конфликтует у '{previous}' и '{node.id}'."
            )
        used_node_ids[output_id] = node.id.value
        node_ids[node.id.value] = output_id

    equipment_rows = list(model.equipment.values())
    equipment_index = {item.id: index for index, item in enumerate(equipment_rows)}
    equipment_rows.sort(key=lambda item: _source_order(
        item.extensions, equipment_index[item.id]
    ))
    plans: dict[str, tuple[_Behavior, str | None, str]] = {}
    planning_errors: list[AdapterDiagnostic] = []
    for equipment in equipment_rows:
        definition = model.equipment_type(equipment.type_id, equipment.type_version)
        behavior = _BEHAVIORS.get(definition.behavior_key)
        if behavior is None:
            planning_errors.append(AdapterDiagnostic(
                "error",
                "unsupported_behavior",
                f"Behavior '{definition.behavior_key}' не поддержан calculation adapter.",
                equipment.id.value,
            ))
            continue
        planning_errors.extend(
            _behavior_port_diagnostics(model, equipment, definition, behavior)
        )
        if definition.behavior_key == "recloser":
            if "switch.position" not in definition.capabilities:
                planning_errors.append(AdapterDiagnostic(
                    "error",
                    "recloser_switch_capability_missing",
                    "Canonical recloser должен иметь capability 'switch.position'.",
                    equipment.id.value,
                ))
            if equipment.normal_position is None:
                planning_errors.append(AdapterDiagnostic(
                    "error",
                    "recloser_normal_position_missing",
                    "Canonical recloser должен иметь явное normal_position OPEN/CLOSED.",
                    equipment.id.value,
                ))
        output_id = None
        if behavior.category != "none":
            output_id = _legacy_or_native_id(
                behavior.category, equipment.id.value, equipment.extensions
            )
        plans[equipment.id.value] = (behavior, output_id, definition.behavior_key)
    if planning_errors:
        raise LegacyCalculationAdapterError(
            "Схема портов оборудования несовместима с calculation behavior.",
            tuple(planning_errors),
        )

    direct_branch_ids: dict[str, str] = {}
    transformer_ids: dict[str, str] = {}
    generated_branch_ids: dict[str, str] = {}
    generated_node_ids: dict[str, str] = {}
    load_ids: dict[str, str] = {}
    for equipment in equipment_rows:
        behavior, output_id, _ = plans[equipment.id.value]
        if output_id is None:
            continue
        if behavior.category == "branch":
            if output_id in direct_branch_ids:
                raise LegacyCalculationAdapterError(
                    f"Calculation branch ID '{output_id}' указан повторно."
                )
            direct_branch_ids[output_id] = equipment.id.value
        elif behavior.category == "transformer3w":
            if output_id in transformer_ids:
                raise LegacyCalculationAdapterError(
                    f"Transformer3W ID '{output_id}' указан повторно."
                )
            transformer_ids[output_id] = equipment.id.value
            for branch_id in (output_id, f"{output_id}_mv", f"{output_id}_lv"):
                previous = generated_branch_ids.get(branch_id)
                if previous is not None:
                    raise LegacyCalculationAdapterError(
                        f"Служебный branch ID '{branch_id}' конфликтует у "
                        f"'{previous}' и '{equipment.id}'."
                    )
                generated_branch_ids[branch_id] = equipment.id.value
            star_id = f"{output_id}__star"
            if star_id in generated_node_ids:
                raise LegacyCalculationAdapterError(
                    f"Служебный node ID '{star_id}' указан повторно."
                )
            generated_node_ids[star_id] = equipment.id.value
        elif behavior.category == "load":
            if output_id in load_ids:
                raise LegacyCalculationAdapterError(
                    f"Calculation load ID '{output_id}' указан повторно."
                )
            load_ids[output_id] = equipment.id.value
    overlap = set(direct_branch_ids) & set(generated_branch_ids)
    if overlap:
        value = sorted(overlap)[0]
        raise LegacyCalculationAdapterError(
            f"Branch ID '{value}' конфликтует со служебным лучом Transformer3W."
        )
    node_overlap = set(used_node_ids) & set(generated_node_ids)
    if node_overlap:
        value = sorted(node_overlap)[0]
        raise LegacyCalculationAdapterError(
            f"Node ID '{value}' конфликтует со служебной звездой Transformer3W."
        )

    net = Network(model.name)
    net.neutral = thaw_json(model.neutral)
    domain_node_to_legacy: dict[str, str] = {}
    legacy_node_to_domain: dict[str, str] = {}
    domain_equipment_to_legacy: dict[str, tuple[str, ...]] = {}
    legacy_object_to_domain: dict[str, str] = {}
    legacy_branch_to_port: dict[str, str] = {}

    for node in node_rows:
        output_id = node_ids[node.id.value]
        effective_voltage_id = node.declared_voltage_class_id
        if effective_voltage_id is None and topology_snapshot is not None:
            effective_voltage_id = topology_snapshot.voltage_by_node[
                node.id
            ].voltage_class_id
        if effective_voltage_id is None:
            raise LegacyCalculationAdapterError(
                f"Узел '{node.id}' не имеет явного класса напряжения; "
                "передайте валидный TopologySnapshot для voltage propagation."
            )
        voltage = model.voltage_classes.get(effective_voltage_id)
        if voltage is None:
            raise LegacyCalculationAdapterError(
                f"Узел '{node.id}' ссылается на отсутствующий класс напряжения."
            )
        marker = _marker(node.extensions)
        payload = marker.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise LegacyCalculationAdapterError(
                f"Узел '{node.id}': legacy payload должен быть объектом."
            )
        unknown_node_fields = set(payload) - {
            "kind",
            "section",
            "calculation_base_kv",
            "prefault_voltage_kv",
        }
        if unknown_node_fields:
            raise LegacyCalculationAdapterError(
                f"Узел '{node.id}': неизвестные legacy-поля "
                f"{sorted(unknown_node_fields)}."
            )
        net.add_node(Node(
            output_id,
            node.name or output_id,
            voltage.nominal_voltage_v / 1000.0,
            kind=payload.get("kind", "bus"),
            section=payload.get("section"),
            note=node.note,
            calculation_base_kv=payload.get("calculation_base_kv"),
            prefault_voltage_kv=payload.get("prefault_voltage_kv"),
        ))
        domain_node_to_legacy[node.id.value] = output_id
        legacy_node_to_domain[output_id] = node.id.value

    # Three-winding equipment must be added before ordinary branches, matching
    # the legacy loader and reserving all generated IDs up front.
    categories = ("transformer3w", "branch", "load", "none")
    ordered_equipment = sorted(
        equipment_rows,
        key=lambda item: categories.index(plans[item.id.value][0].category),
    )
    compatibility_count = 0
    for equipment in ordered_equipment:
        behavior, output_id, behavior_key = plans[equipment.id.value]
        if behavior.compatibility:
            compatibility_count += 1
        if behavior.category == "none":
            domain_equipment_to_legacy[equipment.id.value] = ()
            continue
        assert output_id is not None
        unused_properties: frozenset[str] = frozenset()
        if behavior_key == "recloser":
            payload, unused_properties = _recloser_payload(model, equipment)
        elif behavior_key == "line_section":
            payload, unused_properties, line_diagnostics = _line_section_payload(
                model, equipment
            )
            diagnostics.extend(line_diagnostics)
        else:
            payload = _plain_payload(model, equipment, behavior)
        if (behavior_key == "legacy.line"
                and "rza_calc.protection_zone_review" in equipment.extensions):
            # A split of a protected compatibility branch changes load_end.
            # Preserve CT/protection inputs, but never silently reinterpret its
            # original main protection zone as a less strict reserve zone.
            # Presence of the marker blocks even a malformed/false flag; there
            # is no checkbox that can establish the missing protection contract.
            review = equipment.extensions["rza_calc.protection_zone_review"]
            reason = (review.get("reason") if isinstance(review, Mapping) else None)
            if not isinstance(reason, str) or not reason.strip():
                reason = "После создания отпайки требуется подтвердить зону защиты исходной линии."
            existing_reason = payload.get("calculation_block_reason")
            if isinstance(existing_reason, str) and existing_reason.strip():
                reason = existing_reason + "; " + reason
            payload["calculation_block_reason"] = reason
            diagnostics.append(AdapterDiagnostic(
                "error", "line_protection_zone_review_required", reason,
                equipment.id.value,
            ))
        if not behavior.compatibility:
            _native_defaults(equipment, behavior_key, payload)
        _parameter_inputs(model, equipment, behavior, payload, node_ids)
        if unused_properties:
            diagnostics.append(AdapterDiagnostic(
                "info",
                f"{behavior_key}_properties_not_used",
                f"Calculation DTO не использует properties "
                f"{sorted(unused_properties)}; значения сохранены в Domain Model.",
                equipment.id.value,
            ))
        identity = {"id": output_id, "name": equipment.name}
        if behavior.category == "branch":
            if behavior_key in {"source", "generator", "legacy.source", "legacy.generator"}:
                identity.update({
                    "node_from": GRID,
                    "node_to": _node_for_role(
                        model, equipment, "terminal", node_ids
                    ),
                })
            else:
                from_role, to_role = behavior.roles
                identity.update({
                    "node_from": _node_for_role(
                        model, equipment, from_role, node_ids,
                        allow_grid_marker=behavior.compatibility,
                    ),
                    "node_to": _node_for_role(
                        model, equipment, to_role, node_ids,
                        allow_grid_marker=behavior.compatibility,
                    ),
                })
            if "note" in {item.name for item in fields(behavior.core_class)}:
                identity["note"] = equipment.note
            overlap_keys = set(identity) & set(payload)
            if overlap_keys:
                raise LegacyCalculationAdapterError(
                    f"Оборудование '{equipment.id}': legacy_payload дублирует "
                    f"канонические поля {sorted(overlap_keys)}."
                )
            branch = _construct(
                behavior.core_class,
                identity | payload,
                f"Оборудование '{equipment.id}'",
            )
            net.add_branch(branch)
            domain_equipment_to_legacy[equipment.id.value] = (branch.id,)
            legacy_object_to_domain[f"branch:{branch.id}"] = equipment.id.value
        elif behavior.category == "transformer3w":
            identity.update({
                "node_hv": _node_for_role(model, equipment, "hv", node_ids),
                "node_mv": _node_for_role(model, equipment, "mv", node_ids),
                "node_lv": _node_for_role(model, equipment, "lv", node_ids),
                "note": equipment.note,
            })
            overlap_keys = set(identity) & set(payload)
            if overlap_keys:
                raise LegacyCalculationAdapterError(
                    f"Оборудование '{equipment.id}': legacy_payload дублирует "
                    f"канонические поля {sorted(overlap_keys)}."
                )
            transformer = _construct(
                Transformer3W,
                identity | payload,
                f"Оборудование '{equipment.id}'",
            )
            net.add_transformer3w(transformer)
            marker = _marker(equipment.extensions)
            generated_overrides = marker.get("generated_overrides", {})
            if not isinstance(generated_overrides, dict):
                raise LegacyCalculationAdapterError(
                    f"Transformer3W '{equipment.id}': generated_overrides должен быть объектом."
                )
            star_overrides = generated_overrides.get("star_node", {})
            branch_overrides = generated_overrides.get("branches", {})
            if not isinstance(star_overrides, dict) or not isinstance(branch_overrides, dict):
                raise LegacyCalculationAdapterError(
                    f"Transformer3W '{equipment.id}': повреждены generated overrides."
                )
            _apply_overrides(
                net.nodes[transformer.star_node_id],
                star_overrides,
                f"Transformer3W '{equipment.id}', star node",
            )
            for role, branch_id in zip(("hv", "mv", "lv"), transformer.branch_ids):
                raw = branch_overrides.get(role, {})
                if not isinstance(raw, dict):
                    raise LegacyCalculationAdapterError(
                        f"Transformer3W '{equipment.id}', role '{role}': override должен быть объектом."
                    )
                _apply_overrides(
                    net.branches[branch_id],
                    raw,
                    f"Transformer3W '{equipment.id}', role '{role}'",
                )
                provenance = equipment.extensions.get("rza_calc.parameter_provenance", {})
                if provenance:
                    net.branches[branch_id].parameter_provenance = thaw_json(provenance)
                port = model.port_by_role(equipment.id, role)
                legacy_branch_to_port[branch_id] = port.id.value
                legacy_object_to_domain[f"branch:{branch_id}"] = equipment.id.value
            domain_equipment_to_legacy[equipment.id.value] = transformer.branch_ids
            legacy_object_to_domain[
                f"transformer3w:{transformer.id}"
            ] = equipment.id.value
        elif behavior.category == "load":
            identity.update({
                "node": _node_for_role(model, equipment, "terminal", node_ids)
            })
            overlap_keys = set(identity) & set(payload)
            if overlap_keys:
                raise LegacyCalculationAdapterError(
                    f"Оборудование '{equipment.id}': legacy_payload дублирует "
                    f"канонические поля {sorted(overlap_keys)}."
                )
            load = _construct(
                Load,
                identity | payload,
                f"Оборудование '{equipment.id}'",
            )
            net.add_load(load)
            domain_equipment_to_legacy[equipment.id.value] = (load.id,)
            legacy_object_to_domain[f"load:{load.id}"] = equipment.id.value

    state_rows = list(model.operating_states.values())
    state_index = {item.id: index for index, item in enumerate(state_rows)}
    state_rows.sort(key=lambda item: _source_order(
        item.extensions, state_index[item.id]
    ))
    used_mode_ids: set[str] = set()
    for state in state_rows:
        mode_id = _legacy_or_native_id("mode", state.id.value, state.extensions)
        if mode_id in used_mode_ids:
            raise LegacyCalculationAdapterError(
                f"Calculation mode ID '{mode_id}' указан повторно."
            )
        used_mode_ids.add(mode_id)
        marker = _marker(state.extensions)
        raw_states = marker.get("states", {})
        if not isinstance(raw_states, dict):
            raise LegacyCalculationAdapterError(
                f"Режим '{state.id}': legacy states должен быть объектом."
            )
        states = dict(raw_states)
        raw_availability = marker.get("availability", {})
        if not isinstance(raw_availability, dict) or any(
            not isinstance(key, str) or not isinstance(value, bool)
            for key, value in raw_availability.items()
        ):
            raise LegacyCalculationAdapterError(
                f"Режим '{state.id}': legacy availability должен быть "
                "объектом string → bool."
            )
        availability_states: dict[str, bool] = dict(raw_availability)
        for equipment_id, position in state.positions.items():
            plan = plans.get(equipment_id.value)
            if plan is None or plan[1] is None or plan[0].category not in {
                "branch", "transformer3w"
            }:
                raise LegacyCalculationAdapterError(
                    f"Режим '{state.id}': положение '{equipment_id}' нельзя "
                    "представить в legacy Mode."
                )
            states[plan[1]] = position == SwitchPosition.CLOSED
        for equipment_id, availability in state.availability.items():
            plan = plans.get(equipment_id.value)
            if plan is None or plan[1] is None:
                raise LegacyCalculationAdapterError(
                    f"Режим '{state.id}': доступность '{equipment_id}' нельзя "
                    "представить в legacy Mode."
                )
            if plan[0].category not in {"branch", "transformer3w", "load"}:
                raise LegacyCalculationAdapterError(
                    f"Режим '{state.id}': вывод из работы '{equipment_id}' "
                    "пока не представим в legacy calculation DTO без потери смысла."
                )
            target_ids = (
                (plan[1], f"{plan[1]}_mv", f"{plan[1]}_lv")
                if plan[0].category == "transformer3w"
                else (plan[1],)
            )
            for target_id in target_ids:
                availability_states[target_id] = (
                    availability == EquipmentAvailability.IN_SERVICE
                )
        net.add_mode(Mode(
            mode_id,
            state.name,
            states=states,
            system=state.system,
            description=state.description,
            availability=availability_states,
        ))

    if compatibility_count:
        diagnostics.append(AdapterDiagnostic(
            "info",
            "legacy_compatibility_equipment",
            f"Адаптировано compatibility equipment: {compatibility_count}.",
        ))
    unsafe_ids = [
        branch_id for branch_id in net.branches
        if not _SAFE_CORE_ID.match(branch_id)
    ]
    if unsafe_ids:
        diagnostics.append(AdapterDiagnostic(
            "warning",
            "legacy_switch_id_unsafe",
            "Некоторые сохранённые legacy branch IDs несовместимы с форматом "
            "SW:<branch>:<end>: " + ", ".join(sorted(unsafe_ids)),
        ))

    trace = CalculationTrace.create(
        domain_node_to_legacy=domain_node_to_legacy,
        domain_equipment_to_legacy=domain_equipment_to_legacy,
        legacy_node_to_domain=legacy_node_to_domain,
        legacy_object_to_domain=legacy_object_to_domain,
        legacy_branch_to_port=legacy_branch_to_port,
    )
    from .operating_parameters import attach_operating_parameters
    attach_operating_parameters(model, net, trace)
    return AdaptationResult(net, trace, tuple(diagnostics))


__all__ = [
    "AdaptationResult",
    "AdapterDiagnostic",
    "CalculationTrace",
    "LegacyCalculationAdapterError",
    "LegacyImportError",
    "adapt_to_calculation",
    "import_legacy_network",
    "legacy_equipment_types",
]
