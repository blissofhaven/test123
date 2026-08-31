"""Read-only editor voltage guard shared by preview and atomic commands.

Legacy ports often have no declared voltage group. Their authoritative nominal
voltage is the topology voltage zone, just as for the schematic colour. Never
infer compatibility from the display colour, calculated current, or live state.
"""
from __future__ import annotations

from dataclasses import dataclass
from weakref import WeakKeyDictionary

from rza_calc.domain.electrical import (
    DomainInvariantError, ElectricalModel, ElectricalNodeId, PortId, VoltageClassId,
)
from rza_calc.topology import TopologyEngine, TopologyError, TopologyQueryError, VoltageStatus
from .legacy_voltage import infer_legacy_voltage, stored_legacy_voltage


@dataclass(frozen=True, slots=True)
class VoltageCompatibility:
    valid: bool
    message: str
    voltage_class_id: VoltageClassId | None = None
    reason: str = ""


_ENGINE = TopologyEngine()
_SNAPSHOTS: WeakKeyDictionary = WeakKeyDictionary()


def _snapshot(model: ElectricalModel):
    revision = model.revision
    cached = _SNAPSHOTS.get(model)
    if cached is not None and cached[0] == revision:
        return cached[1]
    snapshot = _ENGINE.compile(model)
    if model.revision != revision:
        raise DomainInvariantError("Сеть изменилась во время проверки напряжения.")
    _SNAPSHOTS[model] = (revision, snapshot)
    return snapshot


def _direct_endpoint_voltage(
    model: ElectricalModel, endpoint: PortId | ElectricalNodeId | VoltageClassId,
) -> VoltageCompatibility:
    """Resolve a known declared/nominal-zone voltage, without changing the model."""
    declared = None
    if isinstance(endpoint, VoltageClassId):
        declared = endpoint
        label = "нового конца"
    elif isinstance(endpoint, PortId):
        if endpoint not in model.ports:
            return VoltageCompatibility(False, "Электрический порт не найден.")
        port = model.ports[endpoint]
        definition = model.port_definition(endpoint)
        equipment = model.equipment[port.equipment_id]
        label = f"порта «{equipment.name}: {definition.display_name}»"
        declared = model.port_voltage_class(endpoint)
    elif isinstance(endpoint, ElectricalNodeId):
        node = model.electrical_nodes.get(endpoint)
        if node is None:
            return VoltageCompatibility(False, "Электрический узел не найден.")
        label = f"узла «{node.name or node.id.value}»"
        declared = node.declared_voltage_class_id
    else:
        return VoltageCompatibility(False, "Неизвестный электрический конец соединения.")

    if not isinstance(endpoint, VoltageClassId):
        try:
            snapshot = _snapshot(model)
            try:
                zone = snapshot.voltage_zone_of(endpoint)
            except TopologyQueryError:
                # An unconnected native port can share a declared nominal
                # group with another connected terminal of that apparatus.
                if isinstance(endpoint, PortId) and definition.voltage_group:
                    zone = snapshot.voltage_zone_of((equipment.id, definition.voltage_group))
                else:
                    zone = None
            if zone is not None:
                resolution = zone.resolution
                if resolution.status is VoltageStatus.CONFLICT:
                    return VoltageCompatibility(False, f"Подключение запрещено: напряжение {label} противоречиво.")
                if resolution.status is VoltageStatus.RESOLVED:
                    resolved = resolution.voltage_class_id
                    if declared is not None and resolved != declared:
                        return VoltageCompatibility(False, f"Подключение запрещено: напряжение {label} противоречиво.")
                    declared = resolved
        except TopologyQueryError:
            pass  # No connected zone; a genuine declared voltage can still suffice.
        except (TopologyError, DomainInvariantError) as exc:
            return VoltageCompatibility(False, f"Не удалось проверить напряжение: {exc}")
    if declared is None or declared not in model.voltage_classes:
        return VoltageCompatibility(False, f"Подключение запрещено: напряжение {label} не определено. Задайте класс напряжения.", reason="unknown")
    return VoltageCompatibility(True, "Класс напряжения определён.", declared)


def endpoint_voltage(model, endpoint) -> VoltageCompatibility:
    direct = _direct_endpoint_voltage(model, endpoint)
    if not isinstance(endpoint, PortId) or endpoint not in model.ports:
        return direct
    saved = stored_legacy_voltage(model, endpoint)
    if direct.valid:
        if saved is not None and saved != direct.voltage_class_id:
            return VoltageCompatibility(False, "Подключение запрещено: сохранённый класс вывода противоречит электрической сети.")
        return direct
    if direct.reason != "unknown":
        return direct

    def connected_voltage(port_id):
        if model.connection_for_port(port_id) is None:
            return None
        result = _direct_endpoint_voltage(model, port_id)
        saved_peer = stored_legacy_voltage(model, port_id)
        if saved_peer is not None and saved_peer != result.voltage_class_id:
            return None
        return result.voltage_class_id if result.valid else None

    inferred = infer_legacy_voltage(model, endpoint, connected_voltage)
    if inferred is not None:
        return VoltageCompatibility(True, "Класс старого вывода определён по сохранённым данным.", inferred)
    return direct


def check_connection_voltage(
    model: ElectricalModel,
    first: PortId | ElectricalNodeId | VoltageClassId,
    second: PortId | ElectricalNodeId | VoltageClassId,
) -> VoltageCompatibility:
    first_value, second_value = endpoint_voltage(model, first), endpoint_voltage(model, second)
    if not first_value.valid:
        return first_value
    if not second_value.valid:
        return second_value
    if first_value.voltage_class_id != second_value.voltage_class_id:
        first_name = model.voltage_classes[first_value.voltage_class_id].display_name
        second_name = model.voltage_classes[second_value.voltage_class_id].display_name
        return VoltageCompatibility(False, f"Соединение запрещено: несовместимые классы напряжения — {first_name} и {second_name}.")
    return VoltageCompatibility(True, "Напряжения совместимы.", first_value.voltage_class_id)


__all__ = ["VoltageCompatibility", "endpoint_voltage", "check_connection_voltage"]
