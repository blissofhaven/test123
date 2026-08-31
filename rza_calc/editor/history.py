# -*- coding: utf-8 -*-
"""Единая project-level история электрической и графической моделей."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Generic, Protocol, TypeVar

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument
from rza_calc.domain.electrical import ChangeSet, ElectricalModel, thaw_json
from rza_calc.domain.history import ElectricalModelMemento

from .state import WORKSPACE_EXTENSION_KEY
from .strings import tr


T = TypeVar("T")


class EditableProject(Protocol):
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


class ProjectHistoryError(RuntimeError):
    """Общая история проекта не может безопасно выполнить операцию."""


@dataclass(slots=True)
class ProjectDraft:
    """Изолированное рабочее состояние одной атомарной команды."""

    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


@dataclass(frozen=True, slots=True)
class ProjectMemento:
    electrical: ElectricalModelMemento
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots

    @classmethod
    def capture(cls, project: EditableProject) -> "ProjectMemento":
        return cls(
            ElectricalModelMemento.capture(project.electrical_model),
            project.diagram,
            project.catalog_snapshots,
        )

    @classmethod
    def capture_draft(cls, draft: ProjectDraft) -> "ProjectMemento":
        return cls(
            ElectricalModelMemento.capture(draft.electrical_model),
            draft.diagram,
            draft.catalog_snapshots,
        )


@dataclass(frozen=True, slots=True)
class ProjectChangeSet:
    project_revision: int
    electrical_change: ChangeSet
    diagram_revision: int
    electrical_changed: bool = False
    diagram_changed: bool = False
    catalog_changed: bool = False


@dataclass(frozen=True, slots=True)
class ProjectCommandExecution(Generic[T]):
    result: T
    change: ProjectChangeSet


@dataclass(frozen=True, slots=True)
class ProjectHistoryEntry:
    sequence: int
    action: str
    description: str
    occurred_at_utc: datetime
    project_revision: int
    message: str
    electrical_fingerprint_before: str = ""
    electrical_fingerprint_after: str = ""


@dataclass(frozen=True, slots=True)
class ProjectHistoryEvent:
    action: str
    description: str
    change: ProjectChangeSet


@dataclass(frozen=True, slots=True)
class _UndoRecord:
    description: str
    before: ProjectMemento
    after: ProjectMemento


def _same_electrical(
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


def _same_diagram(first: DiagramDocument, second: DiagramDocument) -> bool:
    return (
        first.id == second.id
        and first.name == second.name
        and first.pages == second.pages
        and first.representations == second.representations
        and first.routes == second.routes
        and first.extensions == second.extensions
    )


def _with_current_workspace(
    target: DiagramDocument, current: DiagramDocument
) -> DiagramDocument:
    """Сохранить transient UI-настройки, но откатить выбранный режим сети.

    Масштаб, панорамирование и открытые панели не являются частью команды
    электрического редактирования. Выбранный режим, напротив, может быть создан
    самой командой переключения. Поэтому при undo/redo он обязан следовать за
    целевым снимком, иначе после удаления режима в workspace остаётся битая
    ссылка.
    """
    target_extensions = thaw_json(target.extensions)
    current_extensions = thaw_json(current.extensions)
    if WORKSPACE_EXTENSION_KEY in current_extensions:
        current_workspace = thaw_json(
            current_extensions[WORKSPACE_EXTENSION_KEY]
        )
        target_workspace = target_extensions.get(WORKSPACE_EXTENSION_KEY, {})
        if isinstance(current_workspace, dict) and isinstance(
            target_workspace, dict
        ):
            if "active_operating_state_id" in target_workspace:
                current_workspace["active_operating_state_id"] = (
                    target_workspace["active_operating_state_id"]
                )
            else:
                current_workspace.pop("active_operating_state_id", None)
        target_extensions[WORKSPACE_EXTENSION_KEY] = current_workspace
    else:
        target_extensions.pop(WORKSPACE_EXTENSION_KEY, None)
    from dataclasses import replace

    return replace(target, extensions=target_extensions)


class ProjectCommandHistory:
    """Одна undo/redo-цепочка для всей редактируемой части проекта.

    Команда получает private draft. Реальный ``ProjectData`` меняется только
    после полной проверки ElectricalModel, DiagramDocument и каталожных ссылок.
    """

    def __init__(self, project: EditableProject):
        if not isinstance(project.electrical_model, ElectricalModel):
            raise TypeError("История проекта требует ElectricalModel.")
        if not isinstance(project.diagram, DiagramDocument):
            raise TypeError("История проекта требует DiagramDocument.")
        if not isinstance(project.catalog_snapshots, ProjectCatalogSnapshots):
            raise TypeError("История проекта требует ProjectCatalogSnapshots.")
        self._project = project
        self._undo: list[_UndoRecord] = []
        self._redo: list[_UndoRecord] = []
        self._journal: list[ProjectHistoryEntry] = []
        self._listeners: list[Callable[[ProjectHistoryEvent], None]] = []
        self._sequence = 0
        self._project_revision = 0
        self._lock = RLock()
        # Кэш содержит именно полный электрический снимок, который уже прошёл
        # validate_integrity как часть полностью успешной проверки проекта.
        # Диаграмма и каталожные ссылки намеренно не кэшируются: они должны
        # проверяться для каждой команды, включая чисто графическую.
        self._validated_electrical: ElectricalModelMemento | None = None
        self._remember_current()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def project_revision(self) -> int:
        return self._project_revision

    @property
    def journal(self) -> tuple[ProjectHistoryEntry, ...]:
        return tuple(self._journal)

    def subscribe(
        self, listener: Callable[[ProjectHistoryEvent], None]
    ) -> Callable[[], None]:
        """Подписать GUI без зависимости ядра редактора от Qt."""
        if not callable(listener):
            raise TypeError("Обработчик события должен быть вызываемым объектом.")
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, event: ProjectHistoryEvent) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                # Сбой необязательной панели не должен отменять уже
                # зафиксированную электрическую транзакцию.
                continue

    def _remember_current(self) -> None:
        self._known_electrical_revision = self._project.electrical_model.revision
        self._known_diagram = self._project.diagram
        self._known_catalog_snapshots = self._project.catalog_snapshots

    def _require_known_current(self) -> None:
        if (
            self._project.electrical_model.revision
            != self._known_electrical_revision
            or self._project.diagram != self._known_diagram
            or self._project.catalog_snapshots != self._known_catalog_snapshots
        ):
            raise ProjectHistoryError(tr("error.external_change"))

    def _validate(self, memento: ProjectMemento) -> None:
        model = memento.electrical.to_model()
        electrical_already_validated = (
            self._validated_electrical is not None
            and _same_electrical(
                self._validated_electrical,
                memento.electrical,
            )
        )
        if not electrical_already_validated:
            issues = [
                issue for issue in model.validate_integrity()
                if issue.severity == "error"
            ]
            if issues:
                raise ProjectHistoryError(
                    "Команда создала некорректную электрическую модель:\n- "
                    + "\n- ".join(issue.message for issue in issues)
                )
        memento.diagram.require_valid_targets(model)
        catalog_problems = memento.catalog_snapshots.validate_targets(model)
        if catalog_problems:
            raise ProjectHistoryError(
                "Команда создала повреждённые каталожные ссылки:\n- "
                + "\n- ".join(catalog_problems)
            )
        # Обновляем кэш только после успешной проверки всех трёх частей.
        # Ошибка диаграммы/каталога не должна пометить электрическую модель
        # проверенной в составе повреждённого ProjectMemento.
        self._validated_electrical = memento.electrical

    @staticmethod
    def _same(first: ProjectMemento, second: ProjectMemento) -> bool:
        return (
            _same_electrical(first.electrical, second.electrical)
            and _same_diagram(first.diagram, second.diagram)
            and first.catalog_snapshots == second.catalog_snapshots
        )

    def _apply(
        self,
        target: ProjectMemento,
        *,
        preserve_workspace: bool,
        restore_revisions: bool = False,
        target_already_validated: bool = False,
    ) -> ProjectChangeSet:
        self._require_known_current()
        current = ProjectMemento.capture(self._project)
        target_diagram = (
            _with_current_workspace(target.diagram, current.diagram)
            if preserve_workspace else target.diagram
        )
        normalized = ProjectMemento(
            target.electrical, target_diagram, target.catalog_snapshots
        )
        if target_already_validated and preserve_workspace:
            raise ProjectHistoryError(
                "Нельзя пропустить проверку снимка после переноса настроек "
                "рабочего поля."
            )
        if not target_already_validated:
            self._validate(normalized)

        electrical_changed = not _same_electrical(
            current.electrical, normalized.electrical
        )
        diagram_changed = not _same_diagram(current.diagram, target_diagram)
        catalog_changed = current.catalog_snapshots != target.catalog_snapshots

        if electrical_changed:
            staged = normalized.electrical.to_model()
            staged._revision = self._project.electrical_model.revision
            electrical_change = self._project.electrical_model._commit_transaction(
                staged
            )
            if restore_revisions:
                self._project.electrical_model._revision = (
                    normalized.electrical.revision
                )
                electrical_change = ChangeSet(
                    normalized.electrical.revision,
                    electrical_change.added_ids,
                    electrical_change.changed_ids,
                    electrical_change.removed_ids,
                )
        else:
            electrical_change = ChangeSet(self._project.electrical_model.revision)

        if diagram_changed:
            from dataclasses import replace

            self._project.diagram = replace(
                target_diagram,
                revision=(
                    target_diagram.revision
                    if restore_revisions
                    else current.diagram.revision + 1
                ),
            )
        if catalog_changed:
            self._project.catalog_snapshots = target.catalog_snapshots

        if electrical_changed or diagram_changed or catalog_changed:
            self._project_revision += 1
        self._remember_current()
        return ProjectChangeSet(
            self._project_revision,
            electrical_change,
            self._project.diagram.revision,
            electrical_changed,
            diagram_changed,
            catalog_changed,
        )

    def _append_journal(
        self,
        action: str,
        description: str,
        change: ProjectChangeSet,
        *,
        fingerprint_before: str = "",
        fingerprint_after: str = "",
    ) -> None:
        self._sequence += 1
        key = {
            "Выполнено": "journal.done",
            "Отменено": "journal.undo",
            "Повторено": "journal.redo",
        }[action]
        self._journal.append(ProjectHistoryEntry(
            self._sequence,
            action,
            description,
            datetime.now(timezone.utc),
            change.project_revision,
            tr(key, description=description),
            fingerprint_before,
            fingerprint_after,
        ))
        self._notify(ProjectHistoryEvent(action, description, change))

    @staticmethod
    def _electrical_fingerprint(memento: ProjectMemento) -> str:
        # Локальный импорт не создаёт цикл
        # domain.fingerprint -> domain.history -> editor.history.
        from rza_calc.domain.fingerprint import electrical_model_fingerprint

        return electrical_model_fingerprint(memento.electrical.to_model())

    def execute(
        self,
        description: str,
        command: Callable[[ProjectDraft], T],
    ) -> ProjectCommandExecution[T]:
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Описание команды должно быть непустой строкой.")
        if not callable(command):
            raise TypeError("Команда должна быть вызываемым объектом.")
        with self._lock:
            self._require_known_current()
            before = ProjectMemento.capture(self._project)
            fingerprint_before = self._electrical_fingerprint(before)
            draft = ProjectDraft(
                before.electrical.to_model(),
                before.diagram,
                before.catalog_snapshots,
            )
            result = command(draft)
            self._require_known_current()
            target = ProjectMemento.capture_draft(draft)
            self._validate(target)
            if self._same(before, target):
                return ProjectCommandExecution(
                    result,
                    ProjectChangeSet(
                        self._project_revision,
                        ChangeSet(self._project.electrical_model.revision),
                        self._project.diagram.revision,
                    ),
                )
            change = self._apply(
                target,
                preserve_workspace=False,
                target_already_validated=True,
            )
            after = ProjectMemento.capture(self._project)
            fingerprint_after = (
                self._electrical_fingerprint(after)
                if change.electrical_changed
                else fingerprint_before
            )
            self._undo.append(_UndoRecord(description.strip(), before, after))
            self._redo.clear()
            self._append_journal(
                "Выполнено",
                description.strip(),
                change,
                fingerprint_before=fingerprint_before,
                fingerprint_after=fingerprint_after,
            )
            return ProjectCommandExecution(result, change)

    def undo(self) -> ProjectChangeSet:
        with self._lock:
            self._require_known_current()
            if not self._undo:
                raise ProjectHistoryError(tr("error.undo_empty"))
            record = self._undo[-1]
            current = ProjectMemento.capture(self._project)
            fingerprint_before = self._electrical_fingerprint(current)
            change = self._apply(
                record.before,
                preserve_workspace=True,
                restore_revisions=True,
            )
            self._undo.pop()
            self._redo.append(record)
            fingerprint_after = (
                self._electrical_fingerprint(
                    ProjectMemento.capture(self._project)
                )
                if change.electrical_changed
                else fingerprint_before
            )
            self._append_journal(
                "Отменено",
                record.description,
                change,
                fingerprint_before=fingerprint_before,
                fingerprint_after=fingerprint_after,
            )
            return change

    def redo(self) -> ProjectChangeSet:
        with self._lock:
            self._require_known_current()
            if not self._redo:
                raise ProjectHistoryError(tr("error.redo_empty"))
            record = self._redo[-1]
            current = ProjectMemento.capture(self._project)
            fingerprint_before = self._electrical_fingerprint(current)
            change = self._apply(
                record.after,
                preserve_workspace=True,
                restore_revisions=True,
            )
            self._redo.pop()
            self._undo.append(record)
            fingerprint_after = (
                self._electrical_fingerprint(
                    ProjectMemento.capture(self._project)
                )
                if change.electrical_changed
                else fingerprint_before
            )
            self._append_journal(
                "Повторено",
                record.description,
                change,
                fingerprint_before=fingerprint_before,
                fingerprint_after=fingerprint_after,
            )
            return change

    def apply_workspace_diagram(self, diagram: DiagramDocument) -> None:
        """Сохранить zoom/grid/pan без засорения пользовательского undo."""
        with self._lock:
            self._require_known_current()
            current = self._project.diagram
            if (
                diagram.id != current.id
                or diagram.name != current.name
                or diagram.pages != current.pages
                or diagram.representations != current.representations
                or diagram.routes != current.routes
            ):
                raise ProjectHistoryError(
                    "Через настройку рабочего поля нельзя изменять объекты схемы."
                )
            old_extensions = thaw_json(current.extensions)
            new_extensions = thaw_json(diagram.extensions)
            old_workspace = old_extensions.pop(WORKSPACE_EXTENSION_KEY, None)
            new_workspace = new_extensions.pop(WORKSPACE_EXTENSION_KEY, None)
            if old_extensions != new_extensions:
                raise ProjectHistoryError(
                    "Через настройку рабочего поля нельзя изменять данные проекта."
                )
            if old_workspace == new_workspace:
                return
            from dataclasses import replace

            self._project.diagram = replace(
                diagram, revision=current.revision + 1
            )
            self._project_revision += 1
            self._remember_current()

    def clear(self) -> None:
        with self._lock:
            self._undo.clear()
            self._redo.clear()


__all__ = [
    "EditableProject",
    "ProjectChangeSet",
    "ProjectCommandExecution",
    "ProjectCommandHistory",
    "ProjectDraft",
    "ProjectHistoryEntry",
    "ProjectHistoryError",
    "ProjectHistoryEvent",
    "ProjectMemento",
]
