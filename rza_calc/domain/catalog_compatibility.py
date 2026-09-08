"""Explicit scalar catalog codecs; catalog snapshots retain their original type.

The codec never converts equipment, ports, connections, protection settings or
construction segments. It only names compatible parameter values for review.
"""
from collections.abc import Mapping
from decimal import Decimal

from .electrical import DomainInvariantError, thaw_json


LINE_ALIASES = {
    "brand": "conductor_mark", "section_mm2": "cross_section_mm2",
    "n_parallel": "parallel_count", "r0": "r1_ohm_per_km",
    "x0": "x1_ohm_per_km", "ic_per_km": "capacitive_current_a_per_km",
}
TYPE_FAMILIES = {
    "builtin.external_grid": "source", "compat.rza_calc.source": "source",
    "builtin.generator": "generator", "compat.rza_calc.generator": "generator",
    "builtin.transformer_2w": "transformer_2w", "compat.rza_calc.transformer_2w": "transformer_2w",
    "builtin.transformer_3w": "transformer_3w", "compat.rza_calc.transformer_3w": "transformer_3w",
    "builtin.circuit_breaker": "switch", "compat.rza_calc.tie": "switch",
    "compat.rza_calc.branch": "switch", "builtin.load": "load", "compat.rza_calc.load": "load",
    "builtin.line": "line", "builtin.cable": "line", "compat.rza_calc.line": "line",
    "builtin.line_section.cable": "line", "builtin.line_section.overhead": "line",
}
INSTANCE_KEYS = frozenset({"name", "note", "length_mm", "length_km", "parallel_count",
    "n_parallel", "ct_node", "ct_port", "node_from", "node_to", "node", "id",
    "node_hv", "node_mv", "node_lv", "prot", "switch_with", "switchable", "normally_closed"})


def parameter_family(type_id):
    return TYPE_FAMILIES.get(str(type_id), "custom")


def _payload(type_id, properties):
    if str(type_id).startswith("compat.rza_calc."):
        data = properties.get("legacy_payload", {})
        if not isinstance(data, Mapping):
            raise DomainInvariantError("Параметры исходной марки имеют неверный формат.")
        return data
    return properties


def _line_kind(type_id, properties):
    type_id = str(type_id)
    data = _payload(type_id, properties)
    if type_id in {"builtin.cable", "builtin.line_section.cable"}:
        return "cable"
    if type_id in {"builtin.line", "builtin.line_section.overhead"}:
        return data.get("line_type", "overhead")
    return data.get("line_type")


def catalog_codec(model, equipment, entry):
    del model
    source, target = str(entry.equipment_type_id), str(equipment.type_id)
    if equipment.type_version != entry.equipment_type_version:
        raise DomainInvariantError("Версии типа марки и аппарата несовместимы.")
    family = parameter_family(target)
    if source == target:
        if family == "line":
            a, b = _line_kind(source, entry.properties), _line_kind(target, equipment.properties)
            if a is not None and b is not None and a != b:
                raise DomainInvariantError("Марку КЛ нельзя применить к ВЛ, и наоборот.")
        return None
    if family == "custom" or family != parameter_family(source):
        raise DomainInvariantError("Марка предназначена для другого типа оборудования.")
    if family == "line":
        a, b = _line_kind(source, entry.properties), _line_kind(target, equipment.properties)
        if a not in {"cable", "overhead"} or a != b:
            raise DomainInvariantError("Для переноса марки должен совпадать вид линии КЛ/ВЛ.")
    return f"rza.parameters.v1:{source}->{target}"


def catalog_values(model, equipment, entry, *, include_discriminator=False):
    catalog_codec(model, equipment, entry)
    source_type = str(entry.equipment_type_id)
    values = dict(_payload(source_type, entry.properties))
    for namespace in ("rza_calc.nameplate", "rza_calc.ct_parameters"):
        values.update(entry.extensions.get(namespace, {}))
    result = {}
    line = parameter_family(source_type) == "line"
    old_line = source_type in {"compat.rza_calc.line", "builtin.line", "builtin.cable"}
    for key, value in values.items():
        if key in INSTANCE_KEYS or (key == "line_type" and not include_discriminator):
            continue
        if key == "ct_ratio":
            if isinstance(value, (list, tuple)) and len(value) == 2:
                result.update(ct_primary_a=value[0], ct_secondary_a=value[1])
            continue
        if key == "sequence_by_system" and isinstance(value, Mapping):
            for system, fields in value.items():
                if isinstance(fields, Mapping):
                    result.update({f"sequence_by_system.{system}.{field}": thaw_json(item)
                                   for field, item in fields.items()})
            continue
        canonical = LINE_ALIASES.get(key, key) if line and old_line else key
        result[canonical] = thaw_json(value)
    if include_discriminator and line:
        result["line_type"] = _line_kind(source_type, entry.properties)
    return result


def build_catalog_properties(specs, values, base_properties=None):
    """Encode only declared nameplate fields; no object identity or length."""
    result = thaw_json(base_properties or {})
    for spec in specs:
        if spec.key in INSTANCE_KEYS or spec.scope != "equipment" or not spec.editable:
            continue
        if spec.key not in values:
            continue
        value = getattr(values[spec.key], "value", values[spec.key])
        path = spec.storage_path or (spec.key,)
        if any(part in INSTANCE_KEYS for part in path):
            continue
        if value is not None and spec.storage_multiplier != "1":
            value = float(Decimal(str(value)) * Decimal(spec.storage_multiplier))
        _set_path(result, path, value)
    _repair_ratios(result)
    return result


def _repair_ratios(mapping):
    for key, value in list(mapping.items()):
        if key == "ct_ratio":
            if isinstance(value, dict):
                value = [value.get("0"), value.get("1")]
            if value is None or value == [None, None]:
                mapping.pop(key, None)
            elif not isinstance(value, (list, tuple)) or len(value) != 2 or any(v is None for v in value):
                raise DomainInvariantError("В марке ТТ нужны оба номинальных тока.")
            else:
                mapping[key] = list(value)
        elif isinstance(value, dict):
            _repair_ratios(value)


def _set_path(root, path, value):
    owner = root
    for index, part in enumerate(path[:-1]):
        if isinstance(owner, list):
            item = int(part)
            while len(owner) <= item:
                owner.append(None)
            if owner[item] is None:
                owner[item] = [] if path[index + 1].isdigit() else {}
            owner = owner[item]
        else:
            if owner.get(part) is None:
                owner[part] = [] if path[index + 1].isdigit() else {}
            owner = owner[part]
    if isinstance(owner, list):
        item = int(path[-1])
        while len(owner) <= item:
            owner.append(None)
        owner[item] = thaw_json(value)
    elif value is None:
        owner.pop(path[-1], None)
    else:
        owner[path[-1]] = thaw_json(value)


def build_catalog_extensions(specs, values, base_extensions=None):
    result = thaw_json(base_extensions or {})
    for spec in specs:
        if spec.instance_only or spec.key not in values or spec.scope not in {"nameplate", "ct"}:
            continue
        namespace = "rza_calc.nameplate" if spec.scope == "nameplate" else "rza_calc.ct_parameters"
        value = getattr(values[spec.key], "value", values[spec.key])
        _set_path(result.setdefault(namespace, {}), spec.storage_path or (spec.key,), value)
    _repair_ratios(result)
    return result


def _canonical_path(target_type, key):
    target_type = str(target_type)
    prefix = ("legacy_payload",) if target_type.startswith("compat.rza_calc.") else ()
    if key.startswith("sequence_by_system."):
        return prefix + tuple(key.split("."))
    if key in {"ct_primary_a", "ct_secondary_a"}:
        return prefix + ("ct_ratio", "0" if key == "ct_primary_a" else "1")
    if target_type in {"compat.rza_calc.line", "builtin.line", "builtin.cable"}:
        key = {v: k for k, v in LINE_ALIASES.items()}.get(key, key)
    return prefix + (key,)


def catalog_properties_for_type(entry, target_type):
    """A typed view of a truthful snapshot, never a fabricated source type."""
    source_type, target_type = str(entry.equipment_type_id), str(target_type)
    if source_type == target_type:
        return thaw_json(entry.properties)
    if parameter_family(source_type) == "custom" or parameter_family(source_type) != parameter_family(target_type):
        raise DomainInvariantError("Несовместимые типы каталожного снимка.")
    from types import SimpleNamespace
    target_props = {"line_type": _line_kind(source_type, entry.properties)}
    if target_type.startswith("compat.rza_calc."):
        target_props = {"legacy_payload": target_props}
    target = SimpleNamespace(type_id=target_type, type_version=entry.equipment_type_version, properties=target_props)
    values = catalog_values(None, target, entry)
    result = {}
    for key, value in values.items():
        if parameter_family(target_type) == "switch":
            continue  # switch nameplate is an extension, not a calculation DTO
        if key in entry.extensions.get("rza_calc.nameplate", {}):
            continue
        if not target_type.startswith("compat.rza_calc.") and key in {"ct_primary_a", "ct_secondary_a", "ct_accuracy", "breaker_t_off", "terminal"}:
            continue
        _set_path(result, _canonical_path(target_type, key), value)
    if parameter_family(target_type) == "line" and not target_type.startswith("builtin.line_section."):
        _set_path(result, _canonical_path(target_type, "line_type"), _line_kind(source_type, entry.properties))
    return result


def apply_catalog_parameter_overrides(properties, target_type, overrides):
    result = thaw_json(properties)
    for key, override in overrides.items():
        if key in INSTANCE_KEYS:
            continue
        _set_path(result, _canonical_path(target_type, key), override.override_value)
    return result
