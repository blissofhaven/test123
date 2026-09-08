# -*- coding: utf-8 -*-
"""Детерминированные отпечатки legacy-расчётной сети и методики.

Этот модуль нужен не для признания legacy-модели источником истины, а для
проверки актуальности переходного расчётного результата до полного удаления
``core.Network`` из нового расчётного пути.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from .methodology import Methodology
from .model import Branch, Load, Network, Transformer3W


# Adding absent optional inputs must not invalidate previously saved passports.
# Supplied sequence values, including numeric zero, DO affect the fingerprint.
_OPTIONAL_SEQUENCE_FIELDS = frozenset({
    "r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv",
    "zero_sequence_connection", "sequence_phase_shift_deg",
    "r2_ohm_per_km", "x2_ohm_per_km", "r0_ohm_per_km", "x0_ohm_per_km",
})


def _absent_sequence_field(value, name):
    if isinstance(value, (Load, Transformer3W)) and name == "parameter_provenance":
        return getattr(value, name) == {}
    if not isinstance(value, Branch):
        return False
    field_value = getattr(value, name)
    if name in {"sequence_by_system", "parameter_provenance"} and field_value == {}:
        return True
    return (name in _OPTIONAL_SEQUENCE_FIELDS and field_value is None) or (
        name == "negative_sequence_equal_positive" and field_value is False)


def _key(value: Any) -> str:
    raw = getattr(value, "value", value)
    return f"{type(value).__name__}:{raw}"


def _normalise(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Отпечаток расчёта нельзя построить для NaN/Infinity.")
        # repr(float) стабилен для round-trip в поддерживаемых версиях Python.
        return {"$float": repr(value)}
    if isinstance(value, complex):
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise ValueError("Отпечаток расчёта нельзя построить для NaN/Infinity.")
        return {"$complex": [repr(value.real), repr(value.imag)]}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _normalise(getattr(value, item.name))
            for item in fields(value)
            if not _absent_sequence_field(value, item.name)
        }
    if isinstance(value, Mapping):
        return {
            _key(key): _normalise(item)
            for key, item in sorted(value.items(), key=lambda pair: _key(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_normalise(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalise(item) for item in value), key=repr)
    return repr(value)


def _sha256(payload: Any) -> str:
    raw = json.dumps(
        _normalise(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def network_fingerprint(network: Network) -> str:
    """Отпечаток всего электрически значимого содержимого ``Network``."""
    if not isinstance(network, Network):
        raise TypeError("network_fingerprint ожидает Network.")
    return _sha256({
        "name": network.name,
        "nodes": network.nodes,
        "branches": network.branches,
        "transformers3w": network.transformers3w,
        "loads": network.loads,
        "modes": network.modes,
        "neutral": network.neutral,
    })


def methodology_fingerprint(methodology: Methodology) -> str:
    """Отпечаток фактически применённого снимка методики."""
    if not isinstance(methodology, Methodology):
        raise TypeError("methodology_fingerprint ожидает Methodology.")
    return _sha256(methodology.data)


__all__ = ["network_fingerprint", "methodology_fingerprint"]
