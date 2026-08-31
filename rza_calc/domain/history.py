# -*- coding: utf-8 -*-
"""Атомарная история команд канонической электрической модели.

Команда всегда выполняется на изолированной копии. Реальная модель получает
готовое и проверенное состояние одним commit, поэтому исключение не оставляет
частично созданных портов, узлов или участков линии. Undo/redo восстанавливают
данные, но никогда не уменьшают ``ElectricalModel.revision``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar

from .electrical import (
    ChangeSet,
    DomainInvariantError,
    ElectricalModel,
    StableId,
    thaw_json,
)


T = TypeVar("T")


class CommandHistoryError(RuntimeError):
    """Нарушен порядок истории либо команда дала некорректную модель."""


@dataclass(frozen=True, slots=True)
class ElectricalModelMemento:
    """Неизменяемый снимок canonical stores без производного Network."""

    revision: int
    name: str
    neutral: Mapping[str, str]
    extensions: Mapping[str, Any]
    voltage_classes: Mapping[Any, Any]
    equipment_types: Mapping[Any, Any]
    equipment: Mapping[Any, Any]
    ports: Mapping[Any, Any]
    electrical_nodes: Mapping[Any, Any]
    connections: Mapping[Any, Any]
    operating_states: Mapping[Any, Any]
    logical_lines: Mapping[Any, Any]
    line_sections: Mapping[Any, Any]

    @classmethod
    def capture(cls, model: ElectricalModel) -> "ElectricalModelMemento":
        if not isinstance(model, ElectricalModel):
            raise TypeError("Снимок можно создать только для ElectricalModel.")
        revision = model.revision
        result = cls(
            revision,
            model.name,
            MappingProxyType(dict(model.neutral)),
            MappingProxyType(dict(model.extensions)),
            MappingProxyType(dict(model.voltage_classes)),
            MappingProxyType(dict(model.equipment_types)),
            MappingProxyType(dict(model.equipment)),
            MappingProxyType(dict(model.ports)),
            MappingProxyType(dict(model.electrical_nodes)),
            MappingProxyType(dict(model.connections)),
            MappingProxyType(dict(model.operating_states)),
            MappingProxyType(dict(model.logical_lines)),
            MappingProxyType(dict(model.line_sections)),
        )
        if model.revision != revision:
            raise CommandHistoryError(
                "Электрическая модель изменилась во время создания снимка."
            )
        return result

    def to_model(self) -> ElectricalModel:
        """Create a private mutable aggregate carrying the captured records."""
        model = ElectricalModel(
            self.name,
            neutral=thaw_json(self.neutral),
            extensions=thaw_json(self.extensions),
        )
        model._revision = self.revision
        model._voltage_classes = dict(self.voltage_classes)
        model._equipment_types = dict(self.equipment_types)
        model._equipment = dict(self.equipment)
        model._ports = dict(self.ports)
        model._electrical_nodes = dict(self.electrical_nodes)
        model._connections = dict(self.connections)
        model._operating_states = dict(self.operating_states)
        model._logical_lines = dict(self.logical_lines)
        model._line_sections = dict(self.line_sections)
        return model


@dataclass(frozen=True, slots=True)
class CommandExecution(Generic[T]):
    """Пользовательский результат команды и реальный общий ChangeSet."""

    result: T
    change: ChangeSet


@dataclass(frozen=True, slots=True)
class CommandHistoryEntry:
    sequence: int
    action: str
    description: str
    revision_before: int
    revision_after: int
    occurred_at_utc: datetime
    message: str


@dataclass(frozen=True, slots=True)
class _UndoRecord:
    description: str
    before: ElectricalModelMemento
    after: ElectricalModelMemento
    expected_revision: int


def _same_content(
    first: ElectricalModelMemento, second: ElectricalModelMemento
) -> bool:
    return (
        first.name == second.name
        and first.neutral == second.neutral
        and first.extensions == second.extensions
        and first.voltage_classes == second.voltage_classes
        and first.equipment_types == second.equipment_types
        and first.equipment == second.equipment
        and first.ports == second.ports
        and first.electrical_nodes == second.electrical_nodes
        and first.connections == second.connections
        and first.operating_states == second.operating_states
        and first.logical_lines == second.logical_lines
        and first.line_sections == second.line_sections
    )


def _records_by_id(model: ElectricalModel) -> dict[str, tuple[Any, ...]]:
    """Collect every record sharing one public stable ID for ChangeSet diff."""
    rows: dict[str, list[Any]] = {}

    def add(value: str, category: str, record: Any) -> None:
        rows.setdefault(value, []).append((category, record))

    for item in model.voltage_classes.values():
        add(item.id.value, "voltage_class", item)
    type_groups: dict[str, list[Any]] = {}
    for item in model.equipment_types.values():
        type_groups.setdefault(item.id.value, []).append(item)
    for value, definitions in type_groups.items():
        add(
            value,
            "equipment_type",
            tuple(sorted(definitions, key=lambda item: item.schema_version)),
        )
    for category, store in (
        ("equipment", model.equipment),
        ("port", model.ports),
        ("node", model.electrical_nodes),
        ("connection", model.connections),
        ("state", model.operating_states),
        ("logical_line", model.logical_lines),
    ):
        for object_id, record in store.items():
            add(object_id.value, category, record)
    for section in model.line_sections.values():
        add(section.equipment_id.value, "line_section", section)
        for segment in section.construction_segments:
            add(segment.id.value, "line_construction_segment", segment)
    return {value: tuple(records) for value, records in rows.items()}


def _diff(
    current: ElectricalModel, target: ElectricalModel
) -> tuple[tuple[StableId, ...], tuple[StableId, ...], tuple[StableId, ...]]:
    current_rows = _records_by_id(current)
    target_rows = _records_by_id(target)
    current_ids = set(current_rows)
    target_ids = set(target_rows)
    added = tuple(StableId(value) for value in sorted(target_ids - current_ids))
    removed = tuple(StableId(value) for value in sorted(current_ids - target_ids))
    changed = tuple(
        StableId(value)
        for value in sorted(current_ids & target_ids)
        if current_rows[value] != target_rows[value]
    )
    return added, changed, removed


class CommandHistory:
    """Последовательная history одного ``ElectricalModel``."""

    def __init__(self, model: ElectricalModel):
        if not isinstance(model, ElectricalModel):
            raise TypeError("CommandHistory требует ElectricalModel.")
        self._model = model
        self._undo: list[_UndoRecord] = []
        self._redo: list[_UndoRecord] = []
        self._journal: list[CommandHistoryEntry] = []
        self._last_change = ChangeSet(model.revision)
        self._sequence = 0
        self._lock = RLock()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def journal(self) -> tuple[CommandHistoryEntry, ...]:
        return tuple(self._journal)

    @property
    def last_change(self) -> ChangeSet:
        return self._last_change

    def _append_journal(
        self,
        action: str,
        description: str,
        revision_before: int,
        revision_after: int,
    ) -> None:
        self._sequence += 1
        messages = {
            "Выполнено": f"Выполнено: {description}",
            "Отменено": f"Отменено: {description}",
            "Повторено": f"Повторено: {description}",
        }
        self._journal.append(CommandHistoryEntry(
            self._sequence,
            action,
            description,
            revision_before,
            revision_after,
            datetime.now(timezone.utc),
            messages[action],
        ))

    def _apply_memento(
        self,
        memento: ElectricalModelMemento,
        *,
        expected_revision: int,
    ) -> ChangeSet:
        if self._model.revision != expected_revision:
            raise CommandHistoryError(
                "Модель была изменена вне истории; операция не выполнена."
            )
        staged = memento.to_model()
        staged._revision = expected_revision
        issues = [
            issue for issue in staged.validate_integrity()
            if issue.severity == "error"
        ]
        if issues:
            raise CommandHistoryError(
                "Команда создала некорректную электрическую модель:\n- "
                + "\n- ".join(issue.message for issue in issues)
            )
        added, changed, removed = _diff(self._model, staged)
        change = self._model._commit_transaction(
            staged,
            added=added,
            changed=changed,
            removed=removed,
        )
        self._last_change = change
        return change

    def execute(
        self,
        description: str,
        command: Callable[[ElectricalModel], T],
    ) -> CommandExecution[T]:
        """Validate and commit a command once, or leave the model untouched."""
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Описание команды должно быть непустой строкой.")
        if not callable(command):
            raise TypeError("command должен быть вызываемым объектом.")
        with self._lock:
            revision_before = self._model.revision
            before = ElectricalModelMemento.capture(self._model)
            staged = before.to_model()
            result = command(staged)
            if self._model.revision != revision_before:
                raise CommandHistoryError(
                    "Модель изменилась во время выполнения команды."
                )
            issues = [
                issue for issue in staged.validate_integrity()
                if issue.severity == "error"
            ]
            if issues:
                raise CommandHistoryError(
                    "Команда создала некорректную электрическую модель:\n- "
                    + "\n- ".join(issue.message for issue in issues)
                )
            staged_after = ElectricalModelMemento.capture(staged)
            if _same_content(before, staged_after):
                change = ChangeSet(self._model.revision)
                self._last_change = change
                return CommandExecution(result, change)
            change = self._apply_memento(
                staged_after,
                expected_revision=revision_before,
            )
            after = ElectricalModelMemento.capture(self._model)
            self._undo.append(_UndoRecord(
                description.strip(), before, after, self._model.revision
            ))
            self._redo.clear()
            self._append_journal(
                "Выполнено",
                description.strip(),
                revision_before,
                self._model.revision,
            )
            return CommandExecution(result, change)

    def undo(self) -> ChangeSet:
        with self._lock:
            if not self._undo:
                raise CommandHistoryError("Нет команды для отмены.")
            record = self._undo[-1]
            revision_before = self._model.revision
            change = self._apply_memento(
                record.before,
                expected_revision=record.expected_revision,
            )
            self._undo.pop()
            self._redo.append(replace(
                record, expected_revision=self._model.revision
            ))
            self._append_journal(
                "Отменено",
                record.description,
                revision_before,
                self._model.revision,
            )
            return change

    def redo(self) -> ChangeSet:
        with self._lock:
            if not self._redo:
                raise CommandHistoryError("Нет команды для повтора.")
            record = self._redo[-1]
            revision_before = self._model.revision
            change = self._apply_memento(
                record.after,
                expected_revision=record.expected_revision,
            )
            self._redo.pop()
            self._undo.append(replace(
                record, expected_revision=self._model.revision
            ))
            self._append_journal(
                "Повторено",
                record.description,
                revision_before,
                self._model.revision,
            )
            return change

    def clear(self) -> None:
        """Forget undo/redo stacks; the audit journal remains available."""
        with self._lock:
            self._undo.clear()
            self._redo.clear()


__all__ = [
    "CommandExecution",
    "CommandHistory",
    "CommandHistoryEntry",
    "CommandHistoryError",
    "ElectricalModelMemento",
]
