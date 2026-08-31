"""Keep imported CTs on their persistent physical ports during explicit edits.

Compatibility payloads name calculation nodes, not ports. Remember the proven
port before disconnecting it; never infer the CT side from symbol orientation,
voltage, flow, or whichever endpoint happens to remain connected.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from rza_calc.adapters import adapt_to_calculation
from rza_calc.core.model import Transformer3W
from rza_calc.domain.electrical import DomainInvariantError, ElectricalModel, PortId, thaw_json
from rza_calc.domain.history import ElectricalModelMemento

CT_BINDINGS_KEY = "editor_legacy_ct_ports"
_LEGACY = "legacy_calculation"


def _failure(equipment, detail):
    return DomainInvariantError(
        f"Оборудование «{equipment.name}»: не удалось сохранить привязку ТТ "
        f"к прежнему выводу ({detail}). Соединение не изменено."
    )


def _node_identity(model, node_id, cache):
    """Ask the public adapter for identity without adapting an unfinished graph.

    This disposable one-node projection keeps the exact node ID/extensions.
    Its voltage is either declared or already resolved by the actual model;
    no arbitrary voltage, copied UUID formula, or persisted projection is used.
    """
    key = (id(model), node_id)
    if key in cache:
        return cache[key]
    from .connection_voltage import endpoint_voltage
    node = model.electrical_nodes[node_id]
    voltage = endpoint_voltage(model, node_id)
    if not voltage.valid:
        raise DomainInvariantError("Невозможно определить узел привязки ТТ: " + voltage.message)
    projection = ElectricalModel("Идентификатор узла ТТ")
    projection.register_voltage_class(model.voltage_classes[voltage.voltage_class_id])
    projection.add_node(replace(node, declared_voltage_class_id=voltage.voltage_class_id))
    try:
        value = adapt_to_calculation(projection).trace.domain_node_to_legacy[node.id.value]
    except (ValueError, KeyError, RuntimeError) as exc:
        raise DomainInvariantError("Невозможно получить расчётный ID узла ТТ: " + str(exc)) from exc
    cache[key] = value
    return value


def _node_identity_or_none(model, node_id, cache):
    """Расчётный ID узла или ``None``, если его нельзя определить.

    Нужна там, где мы лишь ПЕРЕБИРАЕМ выводы в поисках стороны ТТ. Вывод,
    висящий на узле без класса напряжения, заведомо не тот, который ищем:
    искомый ct_node — непустая строка из совместимого проекта. Раньше такой
    вывод обрушивал всю команду, и пользователь не мог подключить выключатель
    к шине из-за постороннего висящего конца — при том что к самому ТТ этот
    конец отношения не имел.

    Там, где определить узел ОБЯЗАТЕЛЬНО (сторона ТТ действительно
    переезжает), по-прежнему вызывается ``_node_identity`` и отказ остаётся
    отказом: записать ct_node наугад хуже, чем не выполнить операцию.
    """
    try:
        return _node_identity(model, node_id, cache)
    except DomainInvariantError:
        return None


def _slots(equipment):
    payload = equipment.properties.get("legacy_payload", {})
    if not isinstance(payload, Mapping):
        raise _failure(equipment, "повреждён legacy_payload")
    result = {}
    if payload.get("ct_node") is not None:
        result["ct_node"] = (payload["ct_node"], None)
    marker = equipment.extensions.get(_LEGACY, {})
    generated = marker.get("generated_overrides", {}) if isinstance(marker, Mapping) else {}
    branches = generated.get("branches", {}) if isinstance(generated, Mapping) else {}
    if isinstance(branches, Mapping):
        for role in ("hv", "mv", "lv"):
            override = branches.get(role, {})
            if isinstance(override, Mapping) and override.get("ct_node") is not None:
                result["generated." + role] = (override["ct_node"], role)
    return result


def _star_identity(model, equipment, cache):
    """Use the real 3W DTO's public identity, not a guessed internal node ID."""
    marker = equipment.extensions.get(_LEGACY, {})
    if not isinstance(marker, Mapping):
        raise _failure(equipment, "повреждён идентификатор 3W")
    if marker.get("legacy_class") != "Transformer3W":
        return None
    if not isinstance(marker.get("legacy_id"), str) or not marker["legacy_id"].strip():
        raise _failure(equipment, "отсутствует идентификатор 3W")
    try:
        values = thaw_json(equipment.properties["legacy_payload"])
        values.update(id=marker["legacy_id"], name=equipment.name, note=equipment.note)
        for role in ("hv", "mv", "lv"):
            connection = model.connection_for_port(model.port_by_role(equipment.id, role).id)
            # Disconnection is real missing data, not a fabricated endpoint.
            # This DTO is used for identity only, never added to a Network.
            values["node_" + role] = (
                _node_identity(model, connection.electrical_node_id, cache) if connection else None
            )
        return Transformer3W(**values).star_node_id
    except (TypeError, ValueError, KeyError) as exc:
        raise _failure(equipment, "повреждено описание 3W") from exc


def preserve_legacy_ct_bindings(before, after):
    """Complete a topology command atomically; load/inspection never call this."""
    if before is after or before.revision == after.revision:
        return after
    old_connections = {row.port_id: row.electrical_node_id for row in before.connections.values()}
    new_connections = {row.port_id: row.electrical_node_id for row in after.connections.values()}
    changed_ports = {pid for pid in old_connections.keys() | new_connections.keys()
                     if pid in before.ports and pid in after.ports
                     and old_connections.get(pid) != new_connections.get(pid)}
    if not changed_ports:
        return after
    equipment_rows = dict(after.equipment)
    changed = False
    cache = {}
    for equipment_id in {before.ports[pid].equipment_id for pid in changed_ports}:
        previous = before.equipment[equipment_id]
        equipment = equipment_rows.get(equipment_id)
        if equipment is None or not before.equipment_type(
                previous.type_id, previous.type_version).behavior_key.startswith("legacy."):
            continue
        slots = _slots(previous)
        if not slots:
            continue
        current_slots = _slots(equipment)
        bindings = thaw_json(previous.extensions.get(CT_BINDINGS_KEY, {}))
        if not isinstance(bindings, dict):
            raise _failure(previous, "повреждена сохранённая привязка")
        properties = thaw_json(equipment.properties)
        extensions = thaw_json(equipment.extensions)
        touched = False
        for key, (ct_node, generated_role) in slots.items():
            if not isinstance(ct_node, str) or not ct_node:
                raise _failure(previous, "пустой или некорректный узел ТТ")
            if current_slots.get(key) != (ct_node, generated_role):
                raise _failure(previous, "узел ТТ изменён одновременно с подключением")
            candidates = [pid for pid in previous.port_ids
                          if pid in old_connections
                          and _node_identity_or_none(before, old_connections[pid], cache) == ct_node
                          and (generated_role is None or before.ports[pid].role == generated_role)]
            saved = bindings.get(key)
            if saved is not None:
                if not isinstance(saved, dict) or saved.get("node_id") != ct_node:
                    raise _failure(previous, "сохранённая привязка не соответствует узлу ТТ")
                try:
                    port_id = PortId(saved["port_id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise _failure(previous, "повреждён ID вывода ТТ") from exc
                if (port_id not in previous.port_ids or port_id not in equipment.port_ids
                        or before.ports[port_id].role != saved.get("role")
                        or (generated_role is not None and saved["role"] != generated_role)
                        or (port_id in old_connections and port_id not in candidates)):
                    raise _failure(previous, "изменён смысловой вывод ТТ")
            elif len(candidates) == 1:
                port_id = candidates[0]
            elif not candidates and generated_role is not None and ct_node == _star_identity(before, previous, cache):
                continue  # The internal star did not move with a physical port.
            else:
                raise _failure(previous, "сторона ТТ не определена однозначно")
            if port_id not in changed_ports:
                continue  # Moving the other terminal must not relocate the CT.
            new_node = new_connections.get(port_id)
            if new_node is None:
                new_identity = ct_node
            else:
                # Здесь узел определить ОБЯЗАТЕЛЬНО: переехала сама сторона ТТ.
                # Отказ оставляет проект нетронутым; записать ct_node наугад
                # значило бы привязать измерение к неизвестно какой точке.
                try:
                    new_identity = _node_identity(after, new_node, cache)
                except DomainInvariantError as exc:
                    raise _failure(
                        previous,
                        f"сторона ТТ переезжает на узел, класс напряжения "
                        f"которого не определён: {exc}"
                    ) from exc
            bindings[key] = {"port_id": port_id.value, "role": before.ports[port_id].role,
                             "node_id": new_identity}
            if new_identity != ct_node:
                if generated_role is None:
                    properties["legacy_payload"]["ct_node"] = new_identity
                else:
                    extensions[_LEGACY]["generated_overrides"]["branches"][generated_role]["ct_node"] = new_identity
            touched = True
        if touched:
            extensions[CT_BINDINGS_KEY] = bindings
            equipment_rows[equipment_id] = replace(equipment, properties=properties, extensions=extensions)
            changed = True
    if not changed:
        return after
    saved = ElectricalModelMemento.capture(after)
    return replace(saved, revision=saved.revision + 1, equipment=equipment_rows).to_model()
