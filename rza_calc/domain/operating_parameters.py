"""Typed scenario inputs stored in the existing v7 OperatingState extensions.

Missing entries inherit equipment data. An explicit value of None remains
unknown. Values are immutable and use the same canonical units as stage 3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Any, Mapping

from .electrical import DataConfirmation, EquipmentId, PortId, _freeze_json, thaw_json

OPERATING_PARAMETERS_KEY = "rza_calc.operating_parameters"
SOURCE_KEYS = frozenset({
    "s_kz_max", "s_kz_min", "i_kz_max", "i_kz_min", "input_mode_max", "input_mode_min",
    "voltage_kv", "x_r_ratio", "s_nom", "p_nom", "cos_phi", "u_nom", "xd2", "r_pu",
    "r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv",
    "negative_sequence_equal_positive", "zero_sequence_connection",
    *(f"sequence_by_system.{system}.{key}" for system in ("max", "min")
      for key in ("r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv")),
})


@dataclass(frozen=True, slots=True)
class ModeValue:
    value: Any
    source: str = ""
    confirmation: DataConfirmation = DataConfirmation.UNCONFIRMED

    def __post_init__(self):
        if not isinstance(self.source, str):
            raise ValueError("Источник режимного значения должен быть строкой.")
        object.__setattr__(self, "confirmation", DataConfirmation(self.confirmation))
        object.__setattr__(self, "value", _freeze_json(self.value))
        if self.confirmation == DataConfirmation.CONFIRMED and (self.value is None or not self.source.strip()):
            raise ValueError("Подтверждённое режимное значение требует значения и источника.")


def _values(values, id_type, title):
    if not isinstance(values, Mapping):
        raise ValueError(f"{title}: требуется отображение постоянных ID.")
    result = {}
    for key, value in values.items():
        if not isinstance(key, id_type) or not isinstance(value, ModeValue):
            raise ValueError(f"{title}: некорректный ID или ModeValue.")
        result[key] = value
    return MappingProxyType(result)


def _nonnegative(value, title):
    raw = value.value
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, (float, int)) or not math.isfinite(raw) or raw < 0:
        raise ValueError(f"{title}: требуется конечное число ≥ 0.")


@dataclass(frozen=True, slots=True)
class OperatingParameters:
    sources: Mapping[EquipmentId, Mapping[str, ModeValue]] = field(default_factory=dict)
    load_factor: ModeValue | None = None
    load_factors: Mapping[EquipmentId, ModeValue] = field(default_factory=dict)
    working_currents: Mapping[PortId, ModeValue] = field(default_factory=dict)
    # None is historical/unrecorded consent, never an implicit positive answer.
    parallel_operation: bool | None = None

    def __post_init__(self):
        if not isinstance(self.sources, Mapping):
            raise ValueError("Эквиваленты источников должны быть отображением.")
        sources = {}
        for eid, values in self.sources.items():
            if not isinstance(eid, EquipmentId) or not isinstance(values, Mapping):
                raise ValueError("Источник режима адресуется EquipmentId.")
            row = {}
            for key, value in values.items():
                if key not in SOURCE_KEYS or not isinstance(value, ModeValue):
                    raise ValueError(f"Неизвестное режимное поле источника: {key}.")
                row[key] = value
            sources[eid] = MappingProxyType(row)
        object.__setattr__(self, "sources", MappingProxyType(sources))
        object.__setattr__(self, "load_factors", _values(self.load_factors, EquipmentId, "Нагрузка"))
        object.__setattr__(self, "working_currents", _values(self.working_currents, PortId, "Рабочий ток"))
        if self.load_factor is not None:
            if not isinstance(self.load_factor, ModeValue):
                raise ValueError("Общий коэффициент нагрузки требует ModeValue.")
            _nonnegative(self.load_factor, "Коэффициент нагрузки")
        for value in self.load_factors.values():
            _nonnegative(value, "Коэффициент нагрузки")
        for value in self.working_currents.values():
            _nonnegative(value, "Первичный рабочий ток, А")
        if self.parallel_operation is not None and type(self.parallel_operation) is not bool:
            raise ValueError("Разрешение параллельной работы должно быть bool или None.")


def _value_to_dict(value):
    return {"value": thaw_json(value.value), "source": value.source, "confirmation": value.confirmation.value}


def _value_from_dict(raw):
    if not isinstance(raw, Mapping) or set(raw) != {"value", "source", "confirmation"}:
        raise ValueError("Режимное значение требует value/source/confirmation без неизвестных полей.")
    return ModeValue(raw["value"], raw["source"], raw["confirmation"])


def operating_parameters_to_dict(parameters):
    if not isinstance(parameters, OperatingParameters):
        raise TypeError("Ожидается OperatingParameters.")
    return {
        "version": 1,
        "sources": {eid.value: {key: _value_to_dict(value) for key, value in row.items()} for eid, row in parameters.sources.items()},
        "load_factor": _value_to_dict(parameters.load_factor) if parameters.load_factor is not None else None,
        "load_factors": {eid.value: _value_to_dict(value) for eid, value in parameters.load_factors.items()},
        "working_currents": {pid.value: _value_to_dict(value) for pid, value in parameters.working_currents.items()},
        "parallel_operation": parameters.parallel_operation,
    }


def operating_parameters_from_dict(raw):
    fields = {"version", "sources", "load_factor", "load_factors", "working_currents", "parallel_operation"}
    if not isinstance(raw, Mapping) or set(raw) != fields or type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("Неизвестная структура или версия параметров режима.")
    for key in ("sources", "load_factors", "working_currents"):
        if not isinstance(raw[key], Mapping):
            raise ValueError(f"{key}: требуется объект.")
    sources = {}
    for key, row in raw["sources"].items():
        if not isinstance(row, Mapping):
            raise ValueError("Параметры источника должны быть объектом.")
        sources[EquipmentId(key)] = {name: _value_from_dict(value) for name, value in row.items()}
    return OperatingParameters(
        sources, _value_from_dict(raw["load_factor"]) if raw["load_factor"] is not None else None,
        {EquipmentId(key): _value_from_dict(value) for key, value in raw["load_factors"].items()},
        {PortId(key): _value_from_dict(value) for key, value in raw["working_currents"].items()},
        raw["parallel_operation"],
    )


def operating_parameters(state):
    raw = state.extensions.get(OPERATING_PARAMETERS_KEY)
    return OperatingParameters() if raw is None and OPERATING_PARAMETERS_KEY not in state.extensions else operating_parameters_from_dict(raw)


read_operating_parameters = operating_parameters


def parallel_operation_required(topology):
    return any(len(component.source_equipment_ids) > 1 for component in topology.components.values())


def with_operating_parameters(extensions, parameters):
    result = thaw_json(extensions)
    result[OPERATING_PARAMETERS_KEY] = operating_parameters_to_dict(parameters)
    return result


def validate_operating_parameters(model, state):
    """Reject malformed and dangling canonical references, including at IO."""
    parameters = operating_parameters(state)
    from .catalog_compatibility import parameter_family
    for eid in parameters.sources:
        equipment = model.equipment.get(eid)
        if equipment is None or parameter_family(equipment.type_id) not in {"source", "generator"}:
            raise ValueError(f"Источник режима '{eid}' отсутствует или имеет неподходящий тип.")
        family = parameter_family(equipment.type_id)
        positive_keys = ({"s_kz_max", "s_kz_min", "i_kz_max", "i_kz_min", "input_mode_max", "input_mode_min", "voltage_kv", "x_r_ratio"}
                         if family == "source" else {"s_nom", "p_nom", "cos_phi", "u_nom", "xd2", "r_pu"})
        for key, value in parameters.sources[eid].items():
            if key not in positive_keys and key not in SOURCE_KEYS - {
                "s_kz_max", "s_kz_min", "i_kz_max", "i_kz_min", "input_mode_max", "input_mode_min", "voltage_kv", "x_r_ratio",
                "s_nom", "p_nom", "cos_phi", "u_nom", "xd2", "r_pu"}:
                raise ValueError(f"Поле '{key}' не применимо к источнику '{eid}'.")
            raw = value.value
            if raw is None:
                continue
            if key.startswith("input_mode_"):
                valid = raw in {"power", "current"} if isinstance(raw, str) else False
            elif key == "negative_sequence_equal_positive":
                valid = type(raw) is bool
            elif key == "zero_sequence_connection":
                valid = isinstance(raw, str) and raw in {"series", "from_ground", "to_ground", "blocked"}
            else:
                valid = not isinstance(raw, bool) and isinstance(raw, (int, float)) and math.isfinite(raw)
                leaf = key.rsplit(".", 1)[-1]
                if valid and not leaf.startswith("x"):
                    valid = raw >= 0
                if valid and key in {"s_kz_max", "s_kz_min", "i_kz_max", "i_kz_min", "voltage_kv", "s_nom", "p_nom", "cos_phi", "u_nom", "xd2"}:
                    valid = raw > 0
                if valid and leaf == "sequence_reference_kv":
                    valid = raw > 0
                if valid and key == "x_r_ratio":
                    valid = raw >= 0
                if valid and key == "cos_phi":
                    valid = raw <= 1
            if not valid:
                raise ValueError(f"Недопустимое режимное значение '{key}' для '{eid}'.")
    for eid in parameters.load_factors:
        equipment = model.equipment.get(eid)
        if equipment is None or parameter_family(equipment.type_id) != "load":
            raise ValueError(f"Нагрузка режима '{eid}' отсутствует или имеет неподходящий тип.")
    for pid in parameters.working_currents:
        port = model.ports.get(pid)
        if port is None or port.equipment_id not in model.equipment:
            raise ValueError(f"Физический вывод рабочего тока '{pid}' отсутствует.")
        equipment = model.equipment[port.equipment_id]
        if parameter_family(equipment.type_id) not in {"source", "generator", "transformer_2w", "transformer_3w", "switch", "line"}:
            raise ValueError(f"Рабочий ток '{pid}' требует физического вывода ветви или ТТ.")
    return parameters


def without_operating_parameter_references(extensions, equipment_id, port_ids):
    if OPERATING_PARAMETERS_KEY not in extensions:
        return extensions
    parameters = operating_parameters_from_dict(extensions[OPERATING_PARAMETERS_KEY])
    removed_ports = frozenset(port_ids)
    updated = OperatingParameters(
        {eid: row for eid, row in parameters.sources.items() if eid != equipment_id},
        parameters.load_factor,
        {eid: value for eid, value in parameters.load_factors.items() if eid != equipment_id},
        {pid: value for pid, value in parameters.working_currents.items() if pid not in removed_ports},
        parameters.parallel_operation,
    )
    return extensions if updated == parameters else with_operating_parameters(extensions, updated)
