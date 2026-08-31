# -*- coding: utf-8 -*-
"""Explicit electrical behavior registry for the topology compiler.

The registry is deliberately declarative.  A type name, symbol name or port
count is never used to guess conductivity: every supported ``behavior_key``
must have an exact entry here (or in a caller-provided registry).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping


class BehaviorKind(StrEnum):
    SOURCE = "source"
    GENERATOR = "generator"
    PASSIVE = "passive"
    CONDUCTOR = "conductor"
    SWITCH = "switch"
    TRANSFORMER_2W = "transformer_2w"
    TRANSFORMER_3W = "transformer_3w"
    LOAD = "load"


@dataclass(frozen=True, slots=True)
class TopologyBehaviorHandler:
    """Topology law associated with one exact domain ``behavior_key``."""

    behavior_key: str
    kind: BehaviorKind
    roles: tuple[str, ...]
    same_voltage: bool = False
    compatibility: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.behavior_key, str) or not self.behavior_key.strip():
            raise ValueError("behavior_key must be a non-empty string")
        if not isinstance(self.kind, BehaviorKind):
            object.__setattr__(self, "kind", BehaviorKind(self.kind))
        roles = tuple(self.roles)
        if not roles or any(not isinstance(role, str) or not role for role in roles):
            raise ValueError("handler roles must be non-empty strings")
        if len(roles) != len(set(roles)):
            raise ValueError("handler roles must be unique")
        object.__setattr__(self, "roles", roles)

    @property
    def is_source(self) -> bool:
        return self.kind in {BehaviorKind.SOURCE, BehaviorKind.GENERATOR}

    @property
    def is_switch(self) -> bool:
        return self.kind == BehaviorKind.SWITCH

    @property
    def is_transformer(self) -> bool:
        return self.kind in {
            BehaviorKind.TRANSFORMER_2W,
            BehaviorKind.TRANSFORMER_3W,
        }

    @property
    def creates_link(self) -> bool:
        return self.kind in {
            BehaviorKind.CONDUCTOR,
            BehaviorKind.SWITCH,
            BehaviorKind.TRANSFORMER_2W,
            BehaviorKind.TRANSFORMER_3W,
        }


class TopologyBehaviorRegistry:
    """Immutable, fingerprinted registry used at one compiler boundary."""

    def __init__(self, handlers: Iterable[TopologyBehaviorHandler]):
        values: dict[str, TopologyBehaviorHandler] = {}
        for handler in handlers:
            if not isinstance(handler, TopologyBehaviorHandler):
                raise TypeError("registry accepts TopologyBehaviorHandler records")
            if handler.behavior_key in values:
                raise ValueError(
                    f"duplicate topology behavior '{handler.behavior_key}'"
                )
            values[handler.behavior_key] = handler
        self._handlers: Mapping[str, TopologyBehaviorHandler] = MappingProxyType(
            dict(sorted(values.items()))
        )
        payload = [
            {
                "behavior_key": item.behavior_key,
                "kind": item.kind.value,
                "roles": item.roles,
                "same_voltage": item.same_voltage,
                "compatibility": item.compatibility,
            }
            for item in self._handlers.values()
        ]
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self._fingerprint = hashlib.sha256(encoded).hexdigest()

    @property
    def handlers(self) -> Mapping[str, TopologyBehaviorHandler]:
        return self._handlers

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def get(self, behavior_key: str) -> TopologyBehaviorHandler | None:
        return self._handlers.get(behavior_key)

    def __iter__(self) -> Iterator[TopologyBehaviorHandler]:
        return iter(self._handlers.values())

    def extended(
        self, handlers: Iterable[TopologyBehaviorHandler]
    ) -> "TopologyBehaviorRegistry":
        additions = {item.behavior_key: item for item in handlers}
        if set(additions) & set(self._handlers):
            duplicate = sorted(set(additions) & set(self._handlers))[0]
            raise ValueError(f"topology behavior '{duplicate}' is already registered")
        return TopologyBehaviorRegistry((*self._handlers.values(), *additions.values()))


def builtin_topology_handlers() -> TopologyBehaviorRegistry:
    """Return all canonical and migration-compatibility behavior handlers."""

    rows = (
        TopologyBehaviorHandler("source", BehaviorKind.SOURCE, ("terminal",)),
        TopologyBehaviorHandler("generator", BehaviorKind.GENERATOR, ("terminal",)),
        TopologyBehaviorHandler("bus", BehaviorKind.PASSIVE, ("terminal",)),
        TopologyBehaviorHandler(
            "line", BehaviorKind.CONDUCTOR, ("from", "to"), same_voltage=True
        ),
        TopologyBehaviorHandler(
            "line_section",
            BehaviorKind.CONDUCTOR,
            ("from", "to"),
            same_voltage=True,
        ),
        TopologyBehaviorHandler(
            "switch", BehaviorKind.SWITCH, ("a", "b"), same_voltage=True
        ),
        TopologyBehaviorHandler(
            "recloser", BehaviorKind.SWITCH, ("a", "b"), same_voltage=True
        ),
        TopologyBehaviorHandler(
            "transformer_2w", BehaviorKind.TRANSFORMER_2W, ("hv", "lv")
        ),
        TopologyBehaviorHandler(
            "transformer_3w",
            BehaviorKind.TRANSFORMER_3W,
            ("hv", "mv", "lv"),
        ),
        TopologyBehaviorHandler("load", BehaviorKind.LOAD, ("terminal",)),
        TopologyBehaviorHandler(
            "legacy.source",
            BehaviorKind.SOURCE,
            ("terminal",),
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.generator",
            BehaviorKind.GENERATOR,
            ("terminal",),
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.line",
            BehaviorKind.CONDUCTOR,
            ("from", "to"),
            same_voltage=True,
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.transformer_2w",
            BehaviorKind.TRANSFORMER_2W,
            ("from", "to"),
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.tie",
            BehaviorKind.CONDUCTOR,
            ("from", "to"),
            same_voltage=True,
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.branch",
            BehaviorKind.CONDUCTOR,
            ("from", "to"),
            same_voltage=True,
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.transformer_3w",
            BehaviorKind.TRANSFORMER_3W,
            ("hv", "mv", "lv"),
            compatibility=True,
        ),
        TopologyBehaviorHandler(
            "legacy.load",
            BehaviorKind.LOAD,
            ("terminal",),
            compatibility=True,
        ),
    )
    return TopologyBehaviorRegistry(rows)


__all__ = [
    "BehaviorKind",
    "TopologyBehaviorHandler",
    "TopologyBehaviorRegistry",
    "builtin_topology_handlers",
]
