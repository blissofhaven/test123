"""Nominal voltages of imported ports, independent of their screen positions.

Queries never persist inferred data. A disconnect command may remember the
previously resolved voltage of its surviving port in the same undo transaction.
Legacy 2W ``from/to`` is NOT an HV/LV contract: both orientations are valid.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from rza_calc.domain.electrical import (
    DomainInvariantError, ElectricalModel, PortId, VoltageClassId,
)
from rza_calc.domain.history import ElectricalModelMemento

NOMINALS_KEY = "editor_port_nominals"


def is_legacy_port(model: ElectricalModel, port_id: PortId) -> bool:
    port = model.ports.get(port_id)
    if port is None or model.port_definition(port_id).voltage_group is not None:
        return False
    equipment = model.equipment[port.equipment_id]
    return model.equipment_type(equipment.type_id, equipment.type_version).behavior_key.startswith("legacy.")


def stored_legacy_voltage(model: ElectricalModel, port_id: PortId) -> VoltageClassId | None:
    if not is_legacy_port(model, port_id):
        return None
    port = model.ports[port_id]
    entries = model.equipment[port.equipment_id].extensions.get(NOMINALS_KEY, {})
    entry = entries.get(port.role) if isinstance(entries, Mapping) else None
    if (not isinstance(entry, Mapping) or not isinstance(entry.get("source"), str)
            or entry.get("source") not in {"explicit", "connected_nominal_zone"}):
        return None
    raw = entry.get("voltage_class_id")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = VoltageClassId(raw)
    except DomainInvariantError:
        return None
    return value if value in model.voltage_classes else None


def _nameplate_volts(raw):
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = Decimal(str(raw)) * 1000
        return value if value.is_finite() and value > 0 else None
    except InvalidOperation:
        return None


def infer_legacy_voltage(
    model: ElectricalModel, port_id: PortId,
    connected_voltage: Callable[[PortId], VoltageClassId | None],
) -> VoltageClassId | None:
    """Use exact, unambiguous declarations only; no nameplate-stage tolerance."""
    if not is_legacy_port(model, port_id):
        return None
    saved = stored_legacy_voltage(model, port_id)
    if saved is not None:
        return saved
    port = model.ports[port_id]
    equipment = model.equipment[port.equipment_id]
    behavior = model.equipment_type(equipment.type_id, equipment.type_version).behavior_key
    others = [pid for pid in equipment.port_ids if pid != port_id]
    known = [connected_voltage(pid) for pid in others]
    if behavior in {"legacy.line", "legacy.tie", "legacy.branch"} and len(known) == 1:
        return known[0]
    payload = equipment.properties.get("legacy_payload", {})
    if not isinstance(payload, Mapping):
        return None
    wanted = None
    if behavior == "legacy.transformer_2w" and len(known) == 1 and known[0] is not None:
        first, second = (_nameplate_volts(payload.get(key)) for key in ("u_hv", "u_lv"))
        if first is None or second is None or first == second:
            return None
        peer = Decimal(str(model.voltage_classes[known[0]].nominal_voltage_v))
        # Neither array order nor diagram orientation assigns from/to to HV/LV.
        wanted = second if peer == first else first if peer == second else None
    elif behavior == "legacy.transformer_3w" and port.role in {"hv", "mv", "lv"}:
        wanted = _nameplate_volts(payload.get("u_" + port.role))
    if wanted is None:
        return None
    candidates = [value.id for value in model.voltage_classes.values()
                  if value.system_kind == "ac"
                  and Decimal(str(value.nominal_voltage_v)) == wanted]
    return candidates[0] if len(candidates) == 1 else None


def _replace_nominals(model, assignments, source):
    equipment_rows = dict(model.equipment)
    changed = False
    for port_id, voltage_class_id in assignments.items():
        port = model.ports[port_id]
        equipment = equipment_rows[port.equipment_id]
        existing = equipment.extensions.get(NOMINALS_KEY, {})
        entries = dict(existing) if isinstance(existing, Mapping) else {}
        entry = {"voltage_class_id": voltage_class_id.value, "source": source}
        if entries.get(port.role) == entry:
            continue
        entries[port.role] = entry
        equipment_rows[equipment.id] = replace(
            equipment, extensions={**equipment.extensions, NOMINALS_KEY: entries})
        changed = True
    if not changed:
        return model
    saved = ElectricalModelMemento.capture(model)
    return replace(saved, revision=saved.revision + 1, equipment=equipment_rows).to_model()


def set_legacy_port_voltage(model, port_id, voltage_class_id, source="explicit"):
    if source != "explicit":
        raise DomainInvariantError("Источник ручного назначения напряжения должен быть явным.")
    if not is_legacy_port(model, port_id):
        raise DomainInvariantError("Выберите вывод старого оборудования без группы напряжения.")
    if model.connection_for_port(port_id) is not None:
        raise DomainInvariantError("Напряжение подключённого вывода нельзя менять; сначала отсоедините его.")
    if voltage_class_id not in model.voltage_classes:
        raise DomainInvariantError("Класс напряжения не зарегистрирован в проекте.")
    return _replace_nominals(model, {port_id: voltage_class_id}, "explicit")


def preserve_disconnected_legacy_voltages(before, after):
    """Called ONLY inside an explicit command, never during load or inspection."""
    if before is after or before.revision == after.revision:
        return after
    from .connection_voltage import endpoint_voltage
    assignments = {}
    for connection in before.connections.values():
        port_id = connection.port_id
        if port_id not in after.ports or not is_legacy_port(before, port_id):
            continue
        current = after.connection_for_port(port_id)
        if current is not None and current.electrical_node_id == connection.electrical_node_id:
            continue
        known = endpoint_voltage(before, port_id)
        if known.valid:
            assignments[port_id] = known.voltage_class_id
    return _replace_nominals(after, assignments, "connected_nominal_zone")
