"""Explicit, transactional splitting of simple imported line conductors.

The original equipment and CT remain at the declared ``from`` end. A protected
line acquires a persistent review blocker: splitting a conductor must not
silently shorten the existing protection's main zone.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
import math

from ..adapters.legacy_calculation import _legacy_or_native_id, _safe_native_core_id
from ..core.model import LineBranch, ProtectionSettings
from ..domain.electrical import (
    ChangeSet, ConnectionId, DomainInvariantError, ElectricalModel,
    ElectricalNodeId, EquipmentId, PortId, SwitchPosition, thaw_json,
)
from .connection_voltage import check_connection_voltage


REVIEW_MARKER = "rza_calc.protection_zone_review"
SPLIT_MARKER = "rza_calc.legacy_line_split"
REVIEW_REASON = "После создания отпайки требуется подтвердить зону защиты исходной линии."
_PAYLOAD_FIELDS = {item.name for item in fields(LineBranch)} - {"id", "name", "node_from", "node_to", "note"}


@dataclass(frozen=True, slots=True)
class LegacyLineSplitInfo:
    length_mm: int
    protected: bool
    from_node_id: ElectricalNodeId
    to_node_id: ElectricalNodeId
    name: str


@dataclass(frozen=True, slots=True)
class LegacyLineSplitResult:
    first_section_id: EquipmentId
    second_section_id: EquipmentId
    tap_node_id: ElectricalNodeId
    original_from_node_id: ElectricalNodeId
    original_to_node_id: ElectricalNodeId
    first_from_port_id: PortId
    first_to_port_id: PortId
    second_from_port_id: PortId
    second_to_port_id: PortId
    offset_mm: int
    total_length_mm: int
    change: ChangeSet


def _number(value, field, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DomainInvariantError(f"Линия: {field} должно быть заданным конечным числом.")
    if value < 0 or (positive and value == 0):
        raise DomainInvariantError(f"Линия: недопустимое значение {field}.")
    return float(value)


def _source(model, equipment_id):
    equipment = model.equipment.get(equipment_id)
    if equipment is None:
        raise DomainInvariantError("Разбиваемая линия не найдена.")
    definition = model.equipment_type(equipment.type_id, equipment.type_version)
    if definition.behavior_key != "legacy.line" or equipment_id in model.line_sections:
        raise DomainInvariantError("Это не простая импортированная физическая линия.")
    if set(equipment.properties) != {"legacy_payload"}:
        raise DomainInvariantError("У линии неоднозначный набор исходных свойств.")
    payload = equipment.properties["legacy_payload"]
    marker = equipment.extensions.get("legacy_calculation", {})
    if not isinstance(payload, Mapping) or set(payload) - _PAYLOAD_FIELDS:
        raise DomainInvariantError("В параметрах линии есть неподдерживаемые поля.")
    if not isinstance(marker, Mapping) or marker.get("legacy_class") != "LineBranch" or marker.get("category") != "branch":
        raise DomainInvariantError("Не подтверждён исходный тип импортированной линии.")
    if payload.get("kind") != "line" or payload.get("line_type") not in ("cable", "overhead"):
        raise DomainInvariantError("Вид импортированной линии не определён.")
    if set(model.port_definition(pid).role for pid in equipment.port_ids) != {"from", "to"} or len(equipment.port_ids) != 2:
        raise DomainInvariantError("Линия должна иметь ровно два вывода from/to.")
    first = model.port_by_role(equipment_id, "from")
    last = model.port_by_role(equipment_id, "to")
    first_connection = model.connection_for_port(first.id)
    last_connection = model.connection_for_port(last.id)
    if first_connection is None or last_connection is None:
        raise DomainInvariantError("Оба конца разбиваемой линии должны быть подключены.")
    if first_connection.electrical_node_id == last_connection.electrical_node_id:
        raise DomainInvariantError("Концы линии должны относиться к разным узлам.")
    return equipment, payload, first, last, first_connection, last_connection


def legacy_line_split_info(model: ElectricalModel, equipment_id: EquipmentId) -> LegacyLineSplitInfo:
    """Read-only eligibility and physical length; screen distance is never input."""
    equipment, payload, first, last, first_connection, last_connection = _source(model, equipment_id)
    length = _number(payload.get("length_km"), "длина", positive=True)
    millimeters = length * 1_000_000
    if not math.isfinite(millimeters) or millimeters < 2:
        raise DomainInvariantError("Линия слишком короткая или слишком длинная для разбиения в миллиметрах.")
    rounded = round(millimeters)
    if abs(millimeters - rounded) > max(1e-7, 2 * math.ulp(millimeters)):
        raise DomainInvariantError("Длина линии не представима с точностью до миллиметра.")
    _number(payload.get("r0"), "R1, Ом/км")
    _number(payload.get("x0"), "X1, Ом/км")
    count = payload.get("n_parallel", 1)
    if type(count) is not int or count < 1:
        raise DomainInvariantError("Число параллельных цепей должно быть целым и положительным.")
    if payload.get("switchable", False) is not False or payload.get("normally_closed", True) is not True:
        raise DomainInvariantError("Сначала требуется отдельная модель коммутации этой линии.")
    if equipment.normal_position not in (None, SwitchPosition.CLOSED) or payload.get("switch_with") is not None:
        raise DomainInvariantError("Зависимое или неоднозначное переключение линии не поддержано.")
    if payload.get("calculation_block_reason") is not None:
        raise DomainInvariantError("Исходные расчётные данные линии уже заблокированы; сначала устраните причину.")
    if any(payload.get(key) is not None for key in (
        "r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv", "sequence_phase_shift_deg",
    )):
        raise DomainInvariantError("Разбиение полного сопротивления последовательности без удельных данных не поддержано.")
    for sequence in (2, 0):
        for component in ("r", "x"):
            value = payload.get(f"{component}{sequence}_ohm_per_km")
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value) or (component == "r" and value < 0):
                    raise DomainInvariantError("Некорректное удельное сопротивление последовательности.")
    protection = payload.get("prot")
    if not isinstance(protection, Mapping) or set(protection) - {item.name for item in fields(ProtectionSettings)}:
        raise DomainInvariantError("Настройки защиты исходной линии неоднозначны.")
    if protection.get("to", True) is not False:
        raise DomainInvariantError("Разбиение линии с действующей ТО требует отдельного уточнения зоны отсечки.")
    voltage = check_connection_voltage(model, first.id, last.id)
    if not voltage.valid:
        raise DomainInvariantError(voltage.message)
    first_node = model.electrical_nodes[first_connection.electrical_node_id]
    last_node = model.electrical_nodes[last_connection.electrical_node_id]
    # A new series segment must use the same impedance calculation base.
    def explicit_base(node):
        return node.extensions.get("legacy_calculation", {}).get("payload", {}).get("calculation_base_kv")
    if explicit_base(first_node) != explicit_base(last_node):
        raise DomainInvariantError("Расчётные базисы на концах линии различаются; разбиение неоднозначно.")
    ct = payload.get("ct_ratio")
    if ct is not None:
        if not isinstance(ct, (tuple, list)) or len(ct) != 2:
            raise DomainInvariantError("Параметры ТТ линии повреждены.")
        for value in ct:
            _number(value, "коэффициент ТТ", positive=True)
        start_core_id = _legacy_or_native_id("node", first_node.id.value, first_node.extensions)
        if payload.get("ct_node") != start_core_id:
            raise DomainInvariantError("ТТ должен быть явно указан на стороне начала исходной линии.")
    elif payload.get("ct_node") is not None:
        raise DomainInvariantError("Место ТТ указано без его коэффициента трансформации.")
    legacy_id = _legacy_or_native_id("branch", equipment.id.value, equipment.extensions)
    state_keys = {legacy_id, f"SW:{legacy_id}:from", f"SW:{legacy_id}:to"}
    for state in model.operating_states.values():
        raw = state.extensions.get("legacy_calculation", {})
        if equipment.id in state.positions or state_keys.intersection(raw.get("states", {})):
            raise DomainInvariantError("Режим задаёт отдельную коммутацию линии; требуется явная схема аппаратов.")
        availability = raw.get("availability", {})
        if state_keys.intersection(availability) - {legacy_id}:
            raise DomainInvariantError("Доступность отдельных концов линии неоднозначна.")
        if legacy_id in availability and type(availability[legacy_id]) is not bool:
            raise DomainInvariantError("Некорректная доступность исходной линии в режиме.")
    for peer in model.equipment.values():
        peer_payload = peer.properties.get("legacy_payload", {})
        if isinstance(peer_payload, Mapping) and peer_payload.get("switch_with") in state_keys:
            raise DomainInvariantError("Другая ветвь связана с переключением этой линии.")
    protected = ct is not None or any(protection.get(key, False) for key in ("mtz", "to", "ozz")) or REVIEW_MARKER in equipment.extensions
    return LegacyLineSplitInfo(rounded, protected, first_node.id, last_node.id, equipment.name)


def split_legacy_line(model: ElectricalModel, equipment_id: EquipmentId, offset_mm: int) -> LegacyLineSplitResult:
    """Split a simple legacy conductor on a private model, then commit once."""
    info = legacy_line_split_info(model, equipment_id)
    if type(offset_mm) is not int or not 0 < offset_mm < info.length_mm:
        raise DomainInvariantError("Место отпайки задаётся в миллиметрах строго внутри линии.")
    equipment, payload, first, last, first_connection, last_connection = _source(model, equipment_id)
    staged = model._transaction_copy()
    tap_id, second_id = ElectricalNodeId.new(), EquipmentId.new()
    new_ports = {"from": PortId.new(), "to": PortId.new()}
    new_connections = (ConnectionId.new(), ConnectionId.new())
    legacy_id = _legacy_or_native_id("branch", equipment.id.value, equipment.extensions)
    second_core_id = _safe_native_core_id("branch", second_id.value)
    first_payload = thaw_json(payload)
    first_payload["length_km"] = offset_mm / 1_000_000
    second_payload = thaw_json(payload)
    # Keep the original float total; the display's integer-mm length is not a
    # license to replace old source data by a rounded engineering measurement.
    second_payload["length_km"] = payload["length_km"] - first_payload["length_km"]
    if not (0 < first_payload["length_km"] < payload["length_km"]
            and 0 < second_payload["length_km"] < payload["length_km"]):
        raise DomainInvariantError("Заданное место отпайки не представимо с точностью исходной длины.")
    second_payload.update(ct_ratio=None, ct_node=None, ct_accuracy="", terminal="", breaker_t_off=None,
                          switchable=False, normally_closed=True, switch_with=None,
                          prot=asdict(ProtectionSettings(mtz=False, to=False, ozz=False)))
    original_extensions = thaw_json(equipment.extensions)
    origin = original_extensions.setdefault(SPLIT_MARKER, {
        "original_equipment_id": equipment.id.value,
        "original_legacy_id": legacy_id,
        "original_from_node_id": info.from_node_id.value,
        "original_to_node_id": info.to_node_id.value,
        "original_length_km": payload["length_km"],
        "original_properties": thaw_json(equipment.properties),
    })
    if not isinstance(origin, dict):
        raise DomainInvariantError("Повреждены сведения о предыдущем разбиении линии.")
    if info.protected:
        review = original_extensions.setdefault(REVIEW_MARKER, {
            "required": True, "reason": REVIEW_REASON,
            "original_to_node_id": origin.get("original_to_node_id", info.to_node_id.value),
            "original_length_km": origin.get("original_length_km", payload["length_km"]),
            "split_node_ids": [],
        })
        if not isinstance(review, dict) or not isinstance(review.get("split_node_ids", []), list):
            raise DomainInvariantError("Повреждена запись о требуемой проверке зоны защиты.")
        review["required"] = True
        review.setdefault("split_node_ids", []).append(tap_id.value)
    second_extensions = thaw_json(original_extensions)
    second_extensions.pop(REVIEW_MARKER, None)  # The original protection stays on the first equipment.
    second_extensions["legacy_calculation"]["legacy_id"] = second_core_id
    source_node = model.electrical_nodes[info.from_node_id]
    node_payload = thaw_json(source_node.extensions.get("legacy_calculation", {}).get("payload", {}))
    node_payload.update(kind="point", section=None)
    staged.add_node(replace(source_node, id=tap_id, name="Отпайка: " + equipment.name,
        note="Положение отпайки задано пользователем от начала исходного участка.", extensions={
            "junction_kind": "line_tap", "creation_origin": "explicit_legacy_line_split",
            "physical_offset_mm": offset_mm,
            "legacy_calculation": {"schema_version": 1, "category": "node", "legacy_class": "Node", "payload": node_payload},
        }))
    staged._equipment[equipment.id] = replace(equipment, properties={"legacy_payload": first_payload}, extensions=original_extensions)
    staged.remove_connection(last_connection.id)
    staged.connect_port(last.id, tap_id, connection_id=last_connection.id, extensions=thaw_json(last_connection.extensions))
    second, _ = staged.create_equipment(
        equipment.type_id, equipment.name + " — продолжение", type_version=equipment.type_version,
        equipment_id=second_id, port_ids_by_role=new_ports, properties={"legacy_payload": second_payload},
        voltage_class_by_group=dict(equipment.voltage_class_by_group),
        normal_position=SwitchPosition.CLOSED, note=equipment.note, extensions=second_extensions,
    )
    staged.connect_port(new_ports["from"], tap_id, connection_id=new_connections[0])
    staged.connect_port(new_ports["to"], info.to_node_id, connection_id=new_connections[1])
    changed_states = []
    for state_id, state in staged.operating_states.items():
        availability = dict(state.availability)
        extensions = thaw_json(state.extensions)
        raw_availability = extensions.get("legacy_calculation", {}).get("availability", {})
        changed = False
        if equipment.id in availability:
            availability[second.id] = availability[equipment.id]
            changed = True
        if legacy_id in raw_availability:
            raw_availability[second_core_id] = raw_availability[legacy_id]
            changed = True
        if changed:
            staged._operating_states[state_id] = replace(state, availability=availability, extensions=extensions)
            changed_states.append(state_id)
    errors = [issue.message for issue in staged.validate_integrity() if issue.severity == "error"]
    if errors:
        raise DomainInvariantError("Разбиение линии не прошло проверку: " + "; ".join(errors))
    change = model._commit_transaction(staged,
        added=(tap_id, second.id, *second.port_ids, *new_connections),
        changed=(equipment.id, last_connection.id, *changed_states))
    return LegacyLineSplitResult(equipment.id, second.id, tap_id, info.from_node_id, info.to_node_id,
        first.id, last.id, new_ports["from"], new_ports["to"], offset_mm, info.length_mm, change)
