# -*- coding: utf-8 -*-
"""Детерминированный электрический отпечаток canonical Domain Model.

Отпечаток включает только данные, влияющие на электрическую/расчётную модель.
Номер ревизии и графическое представление намеренно не входят: две разные
ветви истории с одинаковым номером ревизии не должны попадать в один cache,
а перемещение символа не должно делать электрический результат устаревшим.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from .electrical import ElectricalModel
from .history import ElectricalModelMemento


def _key(value: Any) -> str:
    raw = getattr(value, "value", value)
    return f"{type(value).__name__}:{raw}"


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return {"$type": type(value).__name__, "value": value.value}
    if is_dataclass(value):
        return {
            field.name: _normalize(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            _key(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: _key(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalize(item) for item in value), key=repr)
    return repr(value)


def electrical_model_fingerprint(model: ElectricalModel) -> str:
    """Return SHA-256 of the canonical electrical content of ``model``."""
    if not isinstance(model, ElectricalModel):
        raise TypeError("electrical_model_fingerprint ожидает ElectricalModel.")
    snapshot = ElectricalModelMemento.capture(model)
    payload = {
        "name": snapshot.name,
        "neutral": _normalize(snapshot.neutral),
        "extensions": _normalize(snapshot.extensions),
        "voltage_classes": _normalize(snapshot.voltage_classes),
        "equipment_types": _normalize(snapshot.equipment_types),
        "equipment": _normalize(snapshot.equipment),
        "ports": _normalize(snapshot.ports),
        "electrical_nodes": _normalize(snapshot.electrical_nodes),
        "connections": _normalize(snapshot.connections),
        "operating_states": _normalize(snapshot.operating_states),
        "logical_lines": _normalize(snapshot.logical_lines),
        "line_sections": _normalize(snapshot.line_sections),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
