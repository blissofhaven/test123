# -*- coding: utf-8 -*-
"""Чтение, миграция и сохранение версионированного JSON-проекта."""
from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from ..core.methodology import Methodology
from ..core.model import (Branch, GeneratorBranch, LineBranch, Load, Mode,
                          Network, Node, ProtectionSettings, SourceBranch,
                          TieBranch, Transformer3W, TransformerBranch)
from ..calculation import (
    CalculationProjection,
    CalculationProjectionBuilder,
)
from ..domain import (Bay, BusSection, CalculationRef, Equipment,
                       DomainInvariantError, ElectricalModel, EquipmentPlacement, Facility, ProjectStructure,
                       UserCatalog, VoltageLevel, builtin_equipment_types)
from ..domain.catalog_snapshot import ProjectCatalogSnapshots
from ..domain.catalog import (
    CatalogId,
    DEFAULT_PROJECT_CATALOG_ID,
    DEFAULT_USER_CATALOG_ID,
)
from ..domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramRouteId,
    RouteWaypointId,
)
from ..domain.electrical import (
    LineConstructionSegmentId,
    EquipmentId,
    StableId,
    deterministic_id,
)
from ..domain.history import ElectricalModelMemento
from ..domain.fingerprint import electrical_model_fingerprint
from ..adapters import (
    AdaptationResult,
    AdapterDiagnostic,
    CalculationTrace,
    LegacyCalculationAdapterError,
    adapt_to_calculation,
    import_legacy_network,
)
from ..topology import TopologyEngine
from .electrical_model import electrical_model_from_dict, electrical_model_to_dict
from .catalog import user_catalog_from_dict, user_catalog_to_dict
from .catalog_snapshot import (
    catalog_snapshots_from_dict,
    catalog_snapshots_to_dict,
)
from .diagram import diagram_from_dict, diagram_to_dict

BRANCH_CLASSES = {
    "source": SourceBranch,
    "transformer": TransformerBranch,
    "line": LineBranch,
    "tie": TieBranch,
    "generator": GeneratorBranch,
    "branch": Branch,
}

FORMAT_VERSION = 7
SUPPORTED_FORMAT_VERSIONS = {1, 2, 3, 4, 5, 6, 7}
TOP_LEVEL_FIELDS = {
    1: {
        "format_version", "project", "methodology", "neutral", "nodes",
        "transformers3w", "branches", "loads", "modes",
    },
    2: {
        "format_version", "project", "methodology", "neutral", "structure",
        "nodes", "transformers3w", "branches", "loads", "modes",
    },
    3: {
        "format_version", "project", "methodology", "structure",
        "electrical_model",
    },
    4: {
        "format_version", "project", "methodology", "structure",
        "electrical_model", "catalogs",
    },
    5: {
        "format_version", "project", "methodology", "structure",
        "electrical_model", "catalogs",
    },
    6: {
        "format_version", "project", "methodology", "structure",
        "electrical_model", "catalogs", "diagram", "catalog_snapshots",
        "migration_journal",
    },
    7: {
        "format_version", "project", "methodology", "structure",
        "electrical_model", "catalogs", "diagram", "catalog_snapshots",
        "migration_journal",
    },
}


_V5_AVAILABILITY_NATIVE_TYPES: dict[str, str] = {
    "builtin.external_grid": "source",
    "builtin.generator": "generator",
    "builtin.line": "line",
    "builtin.cable": "line",
    "builtin.circuit_breaker": "switch",
    "builtin.transformer_2w": "transformer_2w",
    "builtin.transformer_3w": "transformer_3w",
    "builtin.load": "load",
    "builtin.line_section.overhead": "line_section",
    "builtin.line_section.cable": "line_section",
    "builtin.recloser": "recloser",
}

_V5_AVAILABILITY_COMPAT_TYPES: dict[str, str] = {
    "compat.rza_calc.source": "legacy.source",
    "compat.rza_calc.generator": "legacy.generator",
    "compat.rza_calc.line": "legacy.line",
    "compat.rza_calc.transformer_2w": "legacy.transformer_2w",
    "compat.rza_calc.tie": "legacy.tie",
    "compat.rza_calc.branch": "legacy.branch",
    "compat.rza_calc.transformer_3w": "legacy.transformer_3w",
    "compat.rza_calc.load": "legacy.load",
}


class ProjectFormatError(ValueError):
    """Project JSON cannot be loaded or saved without losing validity."""


_PROJECT_TOPOLOGY_ENGINE = TopologyEngine()

_STAGE4_MIGRATION_REVIEW_MARKER = "rza_calc.stage4_migration_review"
_MIGRATION_REVIEW_BLOCKER_CODE = "migration.electrical_route_review_required"

_PERSISTABLE_CALCULATION_BLOCKER_CODES = frozenset({
    "topology.required_port_unconnected",
    "line_length_missing",
    "line_length_unconfirmed",
    "line_impedance_unconfirmed",
    "line_data_unconfirmed",
    "line_protection_zone_review_required",
    "composite_line_legacy_calculation_blocked",
    "busduct_legacy_calculation_blocked",
    _MIGRATION_REVIEW_BLOCKER_CODE,
})


def _adapt_project_model(electrical_model: ElectricalModel):
    """Build a calculation view with topology-derived nominal voltages.

    Nominal voltage zones do not depend on switch position.  Every registered
    operating state is nevertheless validated, so one valid mode cannot hide
    a second electrically ambiguous mode.  The normal state is used only when
    the project has no registered modes.
    """
    state_ids = sorted(
        electrical_model.operating_states,
        key=lambda item: item.value,
    )
    if state_ids:
        snapshots = tuple(
            _PROJECT_TOPOLOGY_ENGINE.compile(electrical_model, state_id)
            for state_id in state_ids
        )
        for snapshot in snapshots:
            if not snapshot.is_valid:
                # Convert typed topology diagnostics to the established
                # calculation-adapter error contract.  A single good mode must
                # never hide an electrically ambiguous registered mode.
                return adapt_to_calculation(electrical_model, snapshot)
        return adapt_to_calculation(electrical_model, snapshots[0])

    snapshot = _PROJECT_TOPOLOGY_ENGINE.compile(electrical_model, None)
    return adapt_to_calculation(electrical_model, snapshot)


def _adapt_project_model_or_blocked(
    electrical_model: ElectricalModel,
) -> AdaptationResult:
    """Вернуть расчётный view, сохранив диагностику явного черновика.

    Канонический проект обязан открываться и сохраняться во время поэтапного
    редактирования. На Этапе 3 новое оборудование уже существует вместе с
    портами, но до Этапа 4 пользователь ещё не может подключить эти порты на
    холсте. Это корректный незавершённый проект, а не повреждение файла.

    Блокирующая диагностика остаётся видна и запрещает расчёт полного графа.
    Для прежнего расчётного экрана строится одноразовый derived view без явно
    помеченных неподключённых черновиков; сами объекты остаются в канонической
    модели. Неоднозначные режимы, конфликты напряжения и повреждения всё так же
    отклоняются.
    """
    try:
        return _adapt_project_model(electrical_model)
    except LegacyCalculationAdapterError as exc:
        if not _is_persistable_calculation_blockers(
            electrical_model, exc.diagnostics
        ):
            raise
        # Preserve the established legacy navigation view only for the exact
        # Stage-3 case of wholly unconnected, explicitly marked symbols.  A
        # connected physical line with unconfirmed data is never removed from
        # a derived graph: it receives an empty, calculation-blocked legacy
        # placeholder instead.
        if _is_persistable_editor_draft(electrical_model, exc.diagnostics):
            calculation_model = _model_without_editor_drafts(
                electrical_model, exc.diagnostics
            )
            adapted = _adapt_project_model(calculation_model)
            return AdaptationResult(
                adapted.network,
                adapted.trace,
                tuple((*exc.diagnostics, *adapted.diagnostics)),
            )
        return AdaptationResult(
            Network(electrical_model.name),
            CalculationTrace.create(
                domain_node_to_legacy={},
                domain_equipment_to_legacy={},
                legacy_node_to_domain={},
                legacy_object_to_domain={},
                legacy_branch_to_port={},
            ),
            exc.diagnostics,
        )


def _is_persistable_calculation_blockers(
    electrical_model: ElectricalModel,
    diagnostics: tuple[AdapterDiagnostic, ...],
) -> bool:
    """Separate editable incompleteness from corruption and ambiguity."""
    if electrical_model.validate_integrity() or not diagnostics:
        return False
    blockers = tuple(item for item in diagnostics if item.severity == "error")
    return bool(blockers) and all(
        item.code in _PERSISTABLE_CALCULATION_BLOCKER_CODES
        for item in blockers
    )


def _calculation_view_may_be_incomplete(
    diagnostics: tuple[AdapterDiagnostic, ...],
) -> bool:
    """A zone review forbids calculation without removing graph objects.

    Other blockers retain the conservative incomplete-view contract. They
    may defer calculation references, but never the structure's own checks.
    """
    return any(
        item.severity == "error"
        and item.code != "line_protection_zone_review_required"
        for item in diagnostics
    )


def _is_persistable_editor_draft(
    electrical_model: ElectricalModel,
    diagnostics: tuple[AdapterDiagnostic, ...],
) -> bool:
    """Разрешить только явно помеченные неподключённые объекты Этапа 3."""
    if electrical_model.validate_integrity() or not diagnostics:
        return False
    blockers = tuple(
        item for item in diagnostics if item.severity == "error"
    )
    if not blockers:
        return False
    for diagnostic in blockers:
        if diagnostic.code != "topology.required_port_unconnected":
            return False
        try:
            equipment = electrical_model.equipment[
                EquipmentId(diagnostic.object_id)
            ]
        except (KeyError, TypeError, ValueError):
            return False
        marker = equipment.extensions.get("stage3_editor")
        if not isinstance(marker, Mapping) or marker.get("draft") is not True:
            return False
    return True


def _model_without_editor_drafts(
    electrical_model: ElectricalModel,
    diagnostics: tuple[AdapterDiagnostic, ...],
) -> ElectricalModel:
    """Создать одноразовую расчётную копию без неподключённых черновиков."""
    if not _is_persistable_editor_draft(electrical_model, diagnostics):
        raise LegacyCalculationAdapterError(
            "Расчётную копию можно строить только для явного черновика редактора.",
            diagnostics,
        )
    result = ElectricalModelMemento.capture(electrical_model).to_model()
    equipment_ids = sorted(
        {
            EquipmentId(item.object_id)
            for item in diagnostics
            if item.severity == "error"
            and item.code == "topology.required_port_unconnected"
        },
        key=lambda item: item.value,
    )
    for equipment_id in equipment_ids:
        result.remove_equipment(equipment_id, cascade=True)
    return result


def _build_project_projection(
    electrical_model: ElectricalModel,
) -> CalculationProjection:
    """Построить детерминированную read-only проекцию для проверки ID."""
    state_ids = sorted(
        electrical_model.operating_states,
        key=lambda item: item.value,
    )
    return CalculationProjectionBuilder().build(
        electrical_model,
        state_ids[0] if state_ids else None,
    )


def _build_project_projection_or_none(
    electrical_model: ElectricalModel,
    diagnostics: tuple[AdapterDiagnostic, ...] = (),
) -> CalculationProjection | None:
    """Проверить расчётные ID полного графа либо честного draft-view."""
    if _is_persistable_editor_draft(electrical_model, diagnostics):
        electrical_model = _model_without_editor_drafts(
            electrical_model, diagnostics
        )
    elif (
        _is_persistable_calculation_blockers(electrical_model, diagnostics)
        and _calculation_view_may_be_incomplete(diagnostics)
    ):
        return None
    return _build_project_projection(electrical_model)


def _validate_user_catalog(
    electrical_model: ElectricalModel,
    user_catalog: UserCatalog,
) -> None:
    """Validate persisted templates through the canonical instance contract."""
    for entry in user_catalog.entries.values():
        try:
            parameters = entry.instance_parameters()
            electrical_model.validate_equipment_parameters(
                parameters.type_id,
                type_version=parameters.type_version,
                properties=parameters.properties,
                voltage_class_by_group=parameters.voltage_class_by_group,
                normal_position=parameters.normal_position,
            )
        except DomainInvariantError as exc:
            raise ValueError(
                f"Запись каталога '{entry.id}' не может быть материализована: {exc}"
            ) from exc


def _validate_project_stable_ids(
    electrical_model: ElectricalModel,
    user_catalog: UserCatalog,
    diagram: DiagramDocument,
    catalog_snapshots: ProjectCatalogSnapshots,
    projection: CalculationProjection | None,
) -> None:
    """Проверить межслойную уникальность собственных постоянных ID.

    Ссылки намеренно не регистрируются повторно: target графического
    представления, equipment_id каталожной привязки и ID копии каталожной
    записи ссылаются на уже существующую сущность либо сохраняют её
    происхождение. Собственные ID страниц, представлений, каталогов и
    производных расчётных объектов обязаны быть уникальны во всём проекте.
    """
    owners: dict[str, str] = {}

    def add(value: Any, owner: str) -> None:
        raw = getattr(value, "value", None)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"{owner}: отсутствует корректный постоянный ID.")
        previous = owners.get(raw)
        if previous is not None:
            raise ValueError(
                f"Постоянный ID '{raw}' используется одновременно как "
                f"{previous} и {owner}."
            )
        owners[raw] = owner

    for value in electrical_model.voltage_classes:
        add(value, "класс напряжения")
    for value in {key[0] for key in electrical_model.equipment_types}:
        add(value, "тип оборудования")
    for label, store in (
        ("оборудование", electrical_model.equipment),
        ("порт", electrical_model.ports),
        ("электрический узел", electrical_model.electrical_nodes),
        ("электрическое соединение", electrical_model.connections),
        ("режим", electrical_model.operating_states),
        ("логическая линия", electrical_model.logical_lines),
    ):
        for value in store:
            add(value, label)
    for section in electrical_model.line_sections.values():
        for segment in section.construction_segments:
            add(segment.id, "конструктивный участок")
    for feeder_id in {
        line.feeder_id
        for line in electrical_model.logical_lines.values()
        if line.feeder_id is not None
    }:
        add(feeder_id, "фидер")

    catalog_id = getattr(user_catalog, "id", None)
    if catalog_id is not None:
        add(catalog_id, "пользовательский каталог")
    for entry in user_catalog.entries.values():
        add(entry.id, "запись пользовательского каталога")

    snapshots_id = getattr(catalog_snapshots, "id", None)
    if snapshots_id is not None:
        add(snapshots_id, "снимок каталога проекта")

    add(diagram.id, "документ схемы")
    for page in diagram.pages.values():
        add(page.id, "страница схемы")
    for representation in diagram.representations.values():
        add(representation.id, "графическое представление")
    for route in diagram.routes.values():
        add(route.id, "графическая трасса")
        for waypoint in route.waypoints:
            add(waypoint.id, "точка графической трассы")

    if projection is not None:
        for node in projection.nodes.values():
            add(node.id, "расчётный узел")
        for branch in projection.branches.values():
            add(branch.id, "расчётная ветвь")


def _empty_diagram_document(
    document_id: DiagramDocumentId | None = None,
) -> DiagramDocument:
    """Создать data-only документ без обращения к графическому движку."""
    return diagram_from_dict({
        "format_version": 2,
        "id": (document_id or DiagramDocumentId("diagram.main")).value,
        "name": "Однолинейная схема",
        "revision": 0,
        "pages": [],
        "representations": [],
        "routes": [],
        "extensions": {},
    })


def _migration_review_diagnostics(
    diagram: DiagramDocument,
    electrical_model: ElectricalModel,
) -> tuple[AdapterDiagnostic, ...]:
    """Вывести блокировки расчёта из сохраняемых marker миграции v6.

    Marker остаётся источником истины для этого решения: чисто графическая
    неоднозначность сохраняется для ручной проверки, но не блокирует
    производный старый расчёт. Блокирующая диагностика создаётся заново после
    load/refresh и поэтому не является второй сохраняемой моделью состояния.
    """
    diagnostics: list[AdapterDiagnostic] = []
    for representation in sorted(
        diagram.representations.values(),
        key=lambda item: item.id.value,
    ):
        marker = representation.extensions.get(
            _STAGE4_MIGRATION_REVIEW_MARKER
        )
        if (
            not isinstance(marker, Mapping)
            or marker.get("required") is not True
            or marker.get("blocks_legacy_calculation") is not True
        ):
            continue

        object_id = representation.target_id.value
        subject = f"электрического объекта {object_id}"
        if representation.equipment_id is not None:
            equipment = electrical_model.equipment.get(
                representation.equipment_id
            )
            if equipment is not None:
                subject = f"оборудования «{equipment.name}»"

        reason = marker.get("reason")
        reason_text = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else "не удалось однозначно восстановить электрический смысл старой линии"
        )
        diagnostics.append(AdapterDiagnostic(
            severity="error",
            code=_MIGRATION_REVIEW_BLOCKER_CODE,
            message=(
                "Требуется проверка после миграции v6 для "
                f"{subject}: {reason_text}"
            ),
            object_id=object_id,
        ))
    return tuple(diagnostics)


@dataclass
class ProjectData:
    """Полный проект: canonical domain плюс производный расчётный view."""

    network: Network
    methodology: Methodology
    metadata: dict[str, Any]
    structure: ProjectStructure
    source_format_version: int
    electrical_model: ElectricalModel
    methodology_ref: dict[str, Any] | None = None
    adapter_diagnostics: tuple[AdapterDiagnostic, ...] = ()
    source_path: Path | None = None
    user_catalog: UserCatalog = field(default_factory=UserCatalog)
    diagram: DiagramDocument = field(default_factory=_empty_diagram_document)
    catalog_snapshots: ProjectCatalogSnapshots = field(
        default_factory=ProjectCatalogSnapshots
    )
    migration_journal: tuple[dict[str, Any], ...] = ()
    _calculation_view_revision: int = field(init=False, repr=False, compare=False)
    _calculation_view_fingerprint: str = field(
        init=False, repr=False, compare=False
    )
    _calculation_view_lock: Any = field(
        default_factory=RLock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.diagram, DiagramDocument):
            raise TypeError("ProjectData.diagram должен быть DiagramDocument.")
        if not isinstance(self.catalog_snapshots, ProjectCatalogSnapshots):
            raise TypeError(
                "ProjectData.catalog_snapshots должен быть ProjectCatalogSnapshots."
            )
        self.migration_journal = tuple(deepcopy(self.migration_journal))
        self._calculation_view_revision = self.electrical_model.revision
        self._calculation_view_fingerprint = electrical_model_fingerprint(
            self.electrical_model
        )

    def __getattribute__(self, name: str):
        if name in {"network", "adapter_diagnostics"}:
            values = object.__getattribute__(self, "__dict__")
            model = values.get("electrical_model")
            compiled_revision = values.get("_calculation_view_revision")
            compiled_fingerprint = values.get("_calculation_view_fingerprint")
            if (
                isinstance(model, ElectricalModel)
                and compiled_revision is not None
                and (
                    model.revision != compiled_revision
                    or electrical_model_fingerprint(model) != compiled_fingerprint
                )
            ):
                object.__getattribute__(self, "refresh_calculation_view")()
        return object.__getattribute__(self, name)

    def refresh_calculation_view(self, *, force: bool = False) -> Network:
        """Return a calculation Network synchronized to the canonical model."""
        lock = object.__getattribute__(self, "_calculation_view_lock")
        with lock:
            model = object.__getattribute__(self, "electrical_model")
            current_revision = model.revision
            current_fingerprint = electrical_model_fingerprint(model)
            compiled_revision = object.__getattribute__(
                self, "_calculation_view_revision"
            )
            compiled_fingerprint = object.__getattribute__(
                self, "_calculation_view_fingerprint"
            )
            if (
                not force
                and current_revision == compiled_revision
                and current_fingerprint == compiled_fingerprint
            ):
                return object.__getattribute__(self, "network")
            adaptation = _adapt_project_model_or_blocked(model)
            migration_diagnostics = _migration_review_diagnostics(
                object.__getattribute__(self, "diagram"),
                model,
            )
            if model.revision != current_revision:
                raise RuntimeError(
                    "ElectricalModel changed while the calculation view was refreshed."
                )
            if electrical_model_fingerprint(model) != current_fingerprint:
                raise RuntimeError(
                    "ElectricalModel changed while the calculation view was refreshed."
                )
            object.__setattr__(self, "network", adaptation.network)
            object.__setattr__(
                self,
                "adapter_diagnostics",
                tuple((*adaptation.diagnostics, *migration_diagnostics)),
            )
            object.__setattr__(
                self, "_calculation_view_revision", current_revision
            )
            object.__setattr__(
                self, "_calculation_view_fingerprint", current_fingerprint
            )
            return adaptation.network

    @property
    def calculation_blockers(self) -> tuple[AdapterDiagnostic, ...]:
        """Вернуть ошибки, при которых расчёт канонической модели запрещён."""
        return tuple(
            item for item in self.adapter_diagnostics if item.severity == "error"
        )

    @staticmethod
    def calculation_blocker_message(diagnostic: AdapterDiagnostic) -> str:
        """Сформировать русское пользовательское описание блокировки."""
        messages = {
            "topology.required_port_unconnected": (
                "Не подключён обязательный электрический порт оборудования."
            ),
            "topology.ambiguous_switch_position": (
                "Не задано однозначное положение коммутационного аппарата."
            ),
            "topology.voltage_conflict": (
                "Обнаружен конфликт классов напряжения."
            ),
            "line_data_unconfirmed": (
                diagnostic.message
                if diagnostic.message
                else "Не подтверждены физические данные участка линии."
            ),
            "line_protection_zone_review_required": (
                diagnostic.message
                if diagnostic.message
                else "После создания отпайки требуется подтвердить зону защиты исходной линии."
            ),
            _MIGRATION_REVIEW_BLOCKER_CODE: (
                diagnostic.message
                if diagnostic.message
                else "Требуется проверка электрического смысла линии после миграции."
            ),
        }
        message = messages.get(
            diagnostic.code,
            "Электрическая модель содержит блокирующую ошибку.",
        )
        if diagnostic.object_id:
            message += f" Объект: {diagnostic.object_id}."
        return message

    def require_calculation_ready(self) -> None:
        """Запретить расчёт производного графа при ошибках исходной модели."""
        blockers = self.calculation_blockers
        if not blockers:
            return
        raise ValueError(
            "Расчёт заблокирован диагностикой электрической модели:\n- "
            + "\n- ".join(
                self.calculation_blocker_message(item) for item in blockers
            )
        )


def _reject_json_constant(token: str) -> None:
    raise ProjectFormatError(
        f"JSON проекта содержит недопустимое неограниченное число '{token}'."
    )


def _validate_json_tree(
    value: Any,
    context: str = "project",
    active_containers: set[int] | None = None,
) -> None:
    """Reject non-JSON values and non-finite floats before persistence."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectFormatError(
                f"{context}: NaN и Infinity недопустимы в JSON проекта."
            )
        return

    if active_containers is None:
        active_containers = set()
    if isinstance(value, dict):
        identity = id(value)
        if identity in active_containers:
            raise ProjectFormatError(f"{context}: обнаружена циклическая JSON-структура.")
        active_containers.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ProjectFormatError(
                        f"{context}: ключ JSON-объекта должен быть строкой."
                    )
                _validate_json_tree(
                    item,
                    f"{context}.{key}" if key else context + ".<empty-key>",
                    active_containers,
                )
        finally:
            active_containers.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ProjectFormatError(f"{context}: обнаружена циклическая JSON-структура.")
        active_containers.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_json_tree(item, f"{context}[{index}]", active_containers)
        finally:
            active_containers.remove(identity)
        return
    raise ProjectFormatError(
        f"{context}: значение типа {type(value).__name__} нельзя сохранить в JSON проекта."
    )


def _write_json(path: str | Path, raw: dict[str, Any]) -> None:
    """Атомарно заменить JSON-файл, не оставляя обрезанный проект.

    Временный файл создаётся рядом с целевым, поэтому ``os.replace`` остаётся
    атомарной операцией одного файлового тома. При любой ошибке исходный файл
    не изменяется, а служебный файл удаляется.
    """
    _validate_json_tree(raw)
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as stream:
            json.dump(raw, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except (TypeError, ValueError) as exc:
        raise ProjectFormatError(f"Проект не удалось сериализовать в строгий JSON: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Не скрываем исходную ошибку записи из-за уборки временного файла.
            pass


def _filtered(cls, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__}: ожидался объект JSON.")
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"{cls.__name__}: неизвестные поля {sorted(unknown)}")
    return {key: value for key, value in data.items() if key in names}


def _list(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"Раздел '{key}' должен быть списком.")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Раздел '{key}' должен содержать только объекты JSON.")
    return value


def _unique_ids(rows: list[dict[str, Any]], section: str) -> None:
    seen: set[str] = set()
    for row in rows:
        object_id = row.get("id")
        if not isinstance(object_id, str) or not object_id.strip():
            raise ValueError(f"Раздел '{section}': у каждого объекта должен быть непустой ID.")
        if object_id in seen:
            raise ValueError(f"Раздел '{section}': ID '{object_id}' указан повторно.")
        seen.add(object_id)


def _read_version(raw: dict[str, Any]) -> int:
    version = raw.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("В проекте должен быть указан целочисленный format_version.")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        if version > FORMAT_VERSION:
            raise ValueError(
                f"Формат проекта v{version} новее поддерживаемого v{FORMAT_VERSION}. "
                "Обновите программу."
            )
        raise ValueError(f"Формат проекта v{version} не поддерживается.")
    unknown = set(raw) - TOP_LEVEL_FIELDS[version]
    if unknown:
        raise ValueError(f"Неизвестные разделы проекта: {sorted(unknown)}")
    return version


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_migration_record(
    from_version: int,
    to_version: int,
    steps: list[str] | tuple[str, ...],
    *,
    backup_file: str = "",
) -> dict[str, Any]:
    if from_version >= to_version:
        raise ValueError("Версия-источник миграции должна быть меньше целевой.")
    if not steps or any(not isinstance(item, str) or not item.strip() for item in steps):
        raise ValueError("Журнал миграции должен содержать непустой список действий.")
    return {
        "id": f"migration_{uuid4().hex}",
        "from_version": from_version,
        "to_version": to_version,
        "applied_at": _utc_timestamp(),
        "steps": list(steps),
        "backup_file": backup_file,
    }


def _load_migration_journal(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("migration_journal должен быть массивом JSON.")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected = {
        "id", "from_version", "to_version", "applied_at", "steps",
        "backup_file",
    }
    for index, item in enumerate(value):
        context = f"migration_journal[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{context} должен быть объектом JSON.")
        unknown = set(item) - expected
        missing = expected - set(item)
        if unknown or missing:
            raise ValueError(
                f"{context}: неизвестные {sorted(unknown)}, отсутствуют {sorted(missing)}."
            )
        record_id = item["id"]
        applied_at = item["applied_at"]
        backup_file = item["backup_file"]
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError(f"{context}.id должен быть непустой строкой.")
        if record_id in seen_ids:
            raise ValueError(f"{context}.id '{record_id}' указан повторно.")
        seen_ids.add(record_id)
        for key in ("from_version", "to_version"):
            number = item[key]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError(f"{context}.{key} должен быть положительным целым.")
        if item["from_version"] >= item["to_version"]:
            raise ValueError(f"{context}: направление миграции некорректно.")
        if item["to_version"] > FORMAT_VERSION:
            raise ValueError(
                f"{context}.to_version не может быть новее формата проекта "
                f"v{FORMAT_VERSION}."
            )
        if not isinstance(applied_at, str) or not applied_at.strip():
            raise ValueError(f"{context}.applied_at должен быть непустой строкой.")
        try:
            parsed_at = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"{context}.applied_at должен быть датой ISO 8601."
            ) from exc
        if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
            raise ValueError(
                f"{context}.applied_at должен содержать часовой пояс."
            )
        if not isinstance(backup_file, str):
            raise ValueError(f"{context}.backup_file должен быть строкой.")
        steps = item["steps"]
        if (
            not isinstance(steps, list)
            or not steps
            or any(not isinstance(step, str) or not step.strip() for step in steps)
        ):
            raise ValueError(f"{context}.steps должен быть непустым массивом строк.")
        result.append(deepcopy(item))
    return tuple(result)


def _migrate_line_sections_v5_to_v6(
    electrical_model: dict[str, Any],
    used_ids: set[str] | None = None,
) -> bool:
    """Добавить один конструктивный участок каждой старой ветви линии."""
    ids_adjusted = False
    raw_lines = electrical_model.get("logical_lines")
    raw_sections = electrical_model.get("line_sections")
    raw_equipment = electrical_model.get("equipment")
    if not isinstance(raw_lines, list) or not isinstance(raw_sections, list):
        raise ValueError(
            "В формате v5 logical_lines и line_sections должны быть массивами."
        )
    if not isinstance(raw_equipment, list):
        raise ValueError("В формате v5 electrical_model.equipment должен быть массивом.")
    line_kind_by_id: dict[str, str] = {}
    for index, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            raise ValueError(f"electrical_model.logical_lines[{index}] должен быть объектом.")
        line_id = line.get("id")
        line_kind = line.get("line_kind")
        if not isinstance(line_id, str) or not isinstance(line_kind, str):
            raise ValueError(
                f"electrical_model.logical_lines[{index}] содержит неверный id/line_kind."
            )
        line_kind_by_id[line_id] = line_kind
    equipment_by_id = {
        row.get("id"): row
        for row in raw_equipment
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    for index, section in enumerate(raw_sections):
        context = f"electrical_model.line_sections[{index}]"
        if not isinstance(section, dict):
            raise ValueError(f"{context} должен быть объектом.")
        if "construction_segments" in section:
            raise ValueError(
                f"Формат v5 содержит поле следующей версии: "
                f"{context}.construction_segments."
            )
        equipment_id = section.get("equipment_id")
        logical_line_id = section.get("logical_line_id")
        length_mm = section.get("length_mm")
        if not isinstance(equipment_id, str) or not isinstance(logical_line_id, str):
            raise ValueError(f"{context}: equipment_id/logical_line_id повреждены.")
        if isinstance(length_mm, bool) or not isinstance(length_mm, int) or length_mm < 1:
            raise ValueError(f"{context}.length_mm должен быть положительным целым.")
        line_kind = line_kind_by_id.get(logical_line_id)
        if line_kind not in {"overhead", "cable"}:
            raise ValueError(f"{context}: вид старой логической линии не поддерживается.")
        equipment = equipment_by_id.get(equipment_id)
        properties = equipment.get("properties", {}) if equipment is not None else {}
        if not isinstance(properties, dict):
            raise ValueError(f"Оборудование '{equipment_id}': properties должен быть объектом.")
        preferred_segment_id = deterministic_id(
            LineConstructionSegmentId,
            "legacy-line-section",
            equipment_id,
        )
        segment_id = LineConstructionSegmentId(
            _allocate_migration_id(
                LineConstructionSegmentId,
                preferred_segment_id.value,
                used_ids,
                f"line-construction-segment:{equipment_id}",
            )
            if used_ids is not None
            else preferred_segment_id.value
        )
        ids_adjusted = ids_adjusted or segment_id != preferred_segment_id
        section["construction_segments"] = [{
            "id": segment_id.value,
            "line_kind": line_kind,
            "length_mm": length_mm,
            # В v5 эти значения принадлежали всей электрической ветви и
            # остаются в EquipmentInstance.properties. Копия в сегменте
            # незаметно меняла бы их владельца: segment override намеренно
            # сильнее branch override, поэтому set_section_override() после
            # миграции переставал влиять на effective-параметр.
            "properties": {},
            "extensions": {
                "migrated_from_format": 5,
                "source_equipment_id": equipment_id,
                "parameter_source": "electrical_branch",
            },
        }]
        del section["length_mm"]
    return ids_adjusted


def _raw_owned_id_values(
    electrical_model: dict[str, Any],
    user_catalog: dict[str, Any],
) -> set[str]:
    """Собрать собственные ID старого проекта до добавления v6-контейнеров."""
    result: set[str] = set()
    for key in (
        "voltage_classes",
        "equipment_types",
        "equipment",
        "ports",
        "electrical_nodes",
        "connections",
        "operating_states",
        "logical_lines",
    ):
        rows = electrical_model.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    result.add(row["id"])
    sections = electrical_model.get("line_sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            segments = section.get("construction_segments")
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if (
                    isinstance(segment, dict)
                    and isinstance(segment.get("id"), str)
                ):
                    result.add(segment["id"])
    entries = user_catalog.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                result.add(entry["id"])
    return result


def _allocate_migration_id(
    id_type: type[StableId],
    preferred: str,
    used: set[str],
    purpose: str,
) -> str:
    """Выбрать воспроизводимый ID, не конфликтующий со старым проектом."""
    if preferred not in used:
        used.add(preferred)
        return preferred
    fingerprint = "\x1f".join(sorted(used))
    attempt = 0
    while True:
        candidate = deterministic_id(
            id_type,
            "project-v6-auxiliary",
            purpose,
            fingerprint,
            str(attempt),
        ).value
        if candidate not in used:
            used.add(candidate)
            return candidate
        attempt += 1


def _migration_exact_fields(
    value: Any,
    expected: set[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} должен быть объектом JSON.")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        raise ValueError(
            f"{context}: неизвестные поля {sorted(unknown)}, "
            f"отсутствуют {sorted(missing)}."
        )
    return value


def _raw_project_id_values(raw: Mapping[str, Any]) -> set[str]:
    """Collect ID-shaped values before deterministic v7 allocation.

    References are intentionally included.  This is stricter than the final
    owned-ID validator and guarantees that a migration-generated route ID can
    never be confused with an existing reference even in a damaged project.
    """
    result: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if isinstance(value, str) and (
            key == "id" or key.endswith("_id") or key.endswith("_ids")
        ):
            result.add(value)

    visit(raw)
    return result


def _is_orthogonal_legacy_route(points: list[dict[str, Any]]) -> bool:
    if len(points) < 2:
        return False
    coordinates: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        row = _migration_exact_fields(
            point, {"x", "y"}, f"diagram.representations.route_points[{index}]"
        )
        x, y = row["x"], row["y"]
        if (
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or isinstance(y, bool)
            or not isinstance(y, (int, float))
            or not math.isfinite(float(x))
            or not math.isfinite(float(y))
        ):
            raise ValueError("Точки старой графической трассы должны быть конечными числами.")
        coordinates.append((float(x), float(y)))
    return all(
        (first[0] == second[0]) != (first[1] == second[1])
        for first, second in zip(coordinates, coordinates[1:])
    )


def _migration_review_representation(
    representation: dict[str, Any],
    reason: str,
    *,
    blocks_legacy_calculation: bool,
) -> None:
    extensions = representation.get("extensions")
    if not isinstance(extensions, dict):
        raise ValueError("diagram.representations.extensions должен быть объектом.")
    marker_key = _STAGE4_MIGRATION_REVIEW_MARKER
    if marker_key in extensions:
        raise ValueError(
            "Формат v6 содержит служебный marker следующей версии: "
            f"{marker_key}."
        )
    extensions[marker_key] = {
        "required": True,
        "reason": reason,
        "legacy_route_points_preserved": True,
        "blocks_legacy_calculation": blocks_legacy_calculation,
    }


def _migrate_diagram_v1_to_v2(
    diagram: dict[str, Any],
    electrical_model: dict[str, Any],
    used_ids: set[str],
) -> tuple[int, int]:
    """Convert only routes whose electrical target and endpoints are certain."""
    _migration_exact_fields(
        diagram,
        {
            "format_version",
            "id",
            "name",
            "revision",
            "pages",
            "representations",
            "extensions",
        },
        "diagram v1",
    )
    if diagram.get("format_version") != 1:
        raise ValueError("В проекте v6 раздел diagram должен иметь format_version=1.")
    pages = diagram.get("pages")
    representations = diagram.get("representations")
    if not isinstance(pages, list) or not isinstance(representations, list):
        raise ValueError("diagram.pages и diagram.representations должны быть массивами.")
    for index, page in enumerate(pages):
        _migration_exact_fields(
            page,
            {"id", "name", "parent_id", "order", "extensions"},
            f"diagram.pages[{index}]",
        )

    raw_equipment = electrical_model.get("equipment")
    raw_ports = electrical_model.get("ports")
    raw_connections = electrical_model.get("connections")
    raw_nodes = electrical_model.get("electrical_nodes")
    raw_sections = electrical_model.get("line_sections")
    if not all(
        isinstance(value, list)
        for value in (
            raw_equipment,
            raw_ports,
            raw_connections,
            raw_nodes,
            raw_sections,
        )
    ):
        raise ValueError("electrical_model v6 содержит повреждённые коллекции.")
    equipment_by_id = {
        row.get("id"): row for row in raw_equipment if isinstance(row, dict)
    }
    ports_by_equipment: dict[str, list[dict[str, Any]]] = {}
    for row in raw_ports:
        if isinstance(row, dict) and isinstance(row.get("equipment_id"), str):
            ports_by_equipment.setdefault(row["equipment_id"], []).append(row)
    connection_by_port: dict[str, list[dict[str, Any]]] = {}
    for row in raw_connections:
        if isinstance(row, dict) and isinstance(row.get("port_id"), str):
            connection_by_port.setdefault(row["port_id"], []).append(row)
    node_ids = {
        row.get("id") for row in raw_nodes
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    line_section_ids = {
        row.get("equipment_id") for row in raw_sections
        if isinstance(row, dict) and isinstance(row.get("equipment_id"), str)
    }

    routes: list[dict[str, Any]] = []
    converted = 0
    review_required = 0
    representation_fields = {
        "id",
        "page_id",
        "target_kind",
        "equipment_id",
        "electrical_node_id",
        "x",
        "y",
        "rotation_deg",
        "z_index",
        "symbol_key",
        "label",
        "route_points",
        "extensions",
    }
    for index, raw_representation in enumerate(representations):
        representation = _migration_exact_fields(
            raw_representation,
            representation_fields,
            f"diagram.representations[{index}]",
        )
        representation_id = representation.get("id")
        page_id = representation.get("page_id")
        points = representation.get("route_points")
        if (
            not isinstance(representation_id, str)
            or not isinstance(page_id, str)
            or not isinstance(points, list)
        ):
            raise ValueError(
                f"diagram.representations[{index}] содержит повреждённые ID/route_points."
            )
        if not points:
            continue
        if not _is_orthogonal_legacy_route(points):
            _migration_review_representation(
                representation,
                "Старая графическая линия не является строгой ортогональной трассой.",
                blocks_legacy_calculation=False,
            )
            review_required += 1
            continue

        route_kind: str | None = None
        equipment_id: str | None = None
        electrical_node_id: str | None = None
        start_anchor: dict[str, Any] | None = None
        end_anchor: dict[str, Any] | None = None
        target_kind = representation.get("target_kind")
        if target_kind == "electrical_node":
            node_id = representation.get("electrical_node_id")
            if isinstance(node_id, str) and node_id in node_ids:
                route_kind = "node_connection"
                electrical_node_id = node_id
                anchor = {
                    "kind": "electrical_node",
                    "representation_id": representation_id,
                    "electrical_node_id": node_id,
                    "branch_port_id": None,
                    "target_port_id": None,
                    "anchor_key": "node",
                }
                start_anchor = deepcopy(anchor)
                end_anchor = deepcopy(anchor)
        elif target_kind == "equipment":
            candidate_id = representation.get("equipment_id")
            equipment = equipment_by_id.get(candidate_id)
            if isinstance(candidate_id, str) and candidate_id in line_section_ids:
                port_rows = ports_by_equipment.get(candidate_id, [])
                connected: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for port in port_rows:
                    port_id = port.get("id")
                    rows = connection_by_port.get(port_id, []) if isinstance(port_id, str) else []
                    if len(rows) == 1:
                        connected.append((port, rows[0]))
                if len(port_rows) == 2 and len(connected) == 2:
                    by_role = {row[0].get("role"): row for row in connected}
                    ordered = (
                        [by_role["from"], by_role["to"]]
                        if set(by_role) == {"from", "to"}
                        else connected
                    )
                    first_node = ordered[0][1].get("electrical_node_id")
                    second_node = ordered[1][1].get("electrical_node_id")
                    if (
                        isinstance(first_node, str)
                        and isinstance(second_node, str)
                        and first_node in node_ids
                        and second_node in node_ids
                        and first_node != second_node
                    ):
                        route_kind = "equipment_branch"
                        equipment_id = candidate_id
                        anchors: list[dict[str, Any]] = []
                        for port, connection in ordered:
                            port_id = port["id"]
                            anchors.append({
                                "kind": "equipment_port",
                                "representation_id": representation_id,
                                "electrical_node_id": connection["electrical_node_id"],
                                "branch_port_id": port_id,
                                "target_port_id": port_id,
                                "anchor_key": str(port.get("role", "")),
                            })
                        start_anchor, end_anchor = anchors

        if route_kind is None or start_anchor is None or end_anchor is None:
            _migration_review_representation(
                representation,
                "Нельзя однозначно определить электрический смысл и привязки старой линии.",
                blocks_legacy_calculation=True,
            )
            review_required += 1
            continue

        route_id = _allocate_migration_id(
            DiagramRouteId,
            deterministic_id(
                DiagramRouteId,
                "diagram-v1-route",
                representation_id,
            ).value,
            used_ids,
            f"diagram-route:{representation_id}",
        )
        waypoints: list[dict[str, Any]] = []
        for point_index, point in enumerate(points):
            waypoint_id = _allocate_migration_id(
                RouteWaypointId,
                deterministic_id(
                    RouteWaypointId,
                    "diagram-v1-waypoint",
                    representation_id,
                    str(point_index),
                ).value,
                used_ids,
                f"diagram-waypoint:{representation_id}:{point_index}",
            )
            waypoints.append({
                "id": waypoint_id,
                "x": point["x"],
                "y": point["y"],
                "source": "user",
                "pinned": True,
            })
        routes.append({
            "id": route_id,
            "page_id": page_id,
            "kind": route_kind,
            "equipment_id": equipment_id,
            "electrical_node_id": electrical_node_id,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "routing_algorithm_version": 1,
            "extensions": {
                "migrated_from_diagram_format": 1,
                "source_representation_id": representation_id,
            },
        })
        representation["route_points"] = []
        converted += 1

    diagram["format_version"] = 2
    diagram["routes"] = routes
    return converted, review_required


def _migrate_electrical_model_v6_to_v7(
    electrical_model: dict[str, Any],
) -> tuple[int, int]:
    """Add explicit data-confirmation state without deriving it from pixels."""
    logical_lines = electrical_model.get("logical_lines")
    line_sections = electrical_model.get("line_sections")
    equipment = electrical_model.get("equipment")
    if not all(isinstance(value, list) for value in (logical_lines, line_sections, equipment)):
        raise ValueError("electrical_model v6 содержит повреждённую модель линии.")
    line_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_line in enumerate(logical_lines):
        if not isinstance(raw_line, dict):
            raise ValueError(f"electrical_model.logical_lines[{index}] должен быть объектом.")
        if "feeder_id" in raw_line:
            raise ValueError(
                f"Формат v6 содержит поле следующей версии: "
                f"electrical_model.logical_lines[{index}].feeder_id."
            )
        line_id = raw_line.get("id")
        inherited = raw_line.get("inherited_properties")
        if not isinstance(line_id, str) or not isinstance(inherited, dict):
            raise ValueError(
                f"electrical_model.logical_lines[{index}] содержит повреждённые данные."
            )
        raw_line["feeder_id"] = None
        line_by_id[line_id] = raw_line
    equipment_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(equipment):
        if not isinstance(row, dict):
            raise ValueError(f"electrical_model.equipment[{index}] должен быть объектом.")
        equipment_id = row.get("id")
        properties = row.get("properties")
        if isinstance(equipment_id, str) and isinstance(properties, dict):
            equipment_by_id[equipment_id] = row

    confirmed_impedance = 0
    unconfirmed_impedance = 0
    for section_index, raw_section in enumerate(line_sections):
        if not isinstance(raw_section, dict):
            raise ValueError(
                f"electrical_model.line_sections[{section_index}] должен быть объектом."
            )
        equipment_id = raw_section.get("equipment_id")
        line_id = raw_section.get("logical_line_id")
        segments = raw_section.get("construction_segments")
        if (
            not isinstance(equipment_id, str)
            or not isinstance(line_id, str)
            or not isinstance(segments, list)
            or line_id not in line_by_id
            or equipment_id not in equipment_by_id
        ):
            raise ValueError(
                f"electrical_model.line_sections[{section_index}] повреждён."
            )
        inherited = line_by_id[line_id]["inherited_properties"]
        branch_properties = equipment_by_id[equipment_id]["properties"]
        for segment_index, raw_segment in enumerate(segments):
            context = (
                f"electrical_model.line_sections[{section_index}]"
                f".construction_segments[{segment_index}]"
            )
            if not isinstance(raw_segment, dict):
                raise ValueError(f"{context} должен быть объектом.")
            premature = {
                "length_confirmation",
                "impedance_confirmation",
            } & set(raw_segment)
            if premature:
                raise ValueError(
                    f"Формат v6 содержит поля следующей версии: "
                    f"{context}.{sorted(premature)}."
                )
            length = raw_segment.get("length_mm")
            properties = raw_segment.get("properties")
            if (
                isinstance(length, bool)
                or not isinstance(length, int)
                or length < 1
                or not isinstance(properties, dict)
            ):
                raise ValueError(f"{context} содержит повреждённую длину/параметры.")
            effective = dict(inherited)
            effective.update(branch_properties)
            effective.update(properties)
            impedance_is_explicit = (
                effective.get("r1_ohm_per_km") is not None
                and effective.get("x1_ohm_per_km") is not None
            )
            raw_segment["length_confirmation"] = "confirmed"
            raw_segment["impedance_confirmation"] = (
                "confirmed" if impedance_is_explicit else "unconfirmed"
            )
            if impedance_is_explicit:
                confirmed_impedance += 1
            else:
                unconfirmed_impedance += 1
    return confirmed_impedance, unconfirmed_impedance


def _migrate_v6_to_v7(raw: dict[str, Any]) -> tuple[int, int, int, int]:
    electrical_model = raw.get("electrical_model")
    diagram = raw.get("diagram")
    journal = raw.get("migration_journal")
    if not isinstance(electrical_model, dict):
        raise ValueError("В формате v6 electrical_model должен быть объектом.")
    if not isinstance(diagram, dict):
        raise ValueError("В формате v6 diagram должен быть объектом.")
    if not isinstance(journal, list):
        raise ValueError("В формате v6 migration_journal должен быть массивом.")
    used_ids = _raw_project_id_values(raw)
    confirmed, unconfirmed = _migrate_electrical_model_v6_to_v7(
        electrical_model
    )
    converted, review = _migrate_diagram_v1_to_v2(
        diagram, electrical_model, used_ids
    )
    raw["format_version"] = 7
    steps = [
        "Добавлена отдельная модель ортогональных графических трасс v2.",
        "Добавлены явные признаки подтверждённости физических данных линии.",
    ]
    if converted:
        steps.append(
            f"Однозначно преобразованы старые графические трассы: {converted}."
        )
    if review:
        steps.append(
            f"Сохранены без догадок и помечены для проверки старые линии: {review}."
        )
    journal.append(_new_migration_record(6, 7, steps))
    return converted, review, confirmed, unconfirmed


def _migrate(raw: dict[str, Any], version: int) -> dict[str, Any]:
    """Последовательные миграции без изменения исходного словаря."""
    migrated = deepcopy(raw)
    original_version = version
    migration_steps: list[str] = []
    if version == 1:
        # В v1 физическая структура отсутствовала. Электрические данные не
        # угадываем по названиям — создаём пустой, но корректный новый слой.
        migrated["structure"] = {
            "facilities": [],
            "voltage_levels": [],
            "bus_sections": [],
            "bays": [],
            "equipment": [],
            "placements": [],
        }
        migrated["format_version"] = 2
        migration_steps.append("Добавлена навигационная структура проекта v2.")
    if version == 3:
        electrical_model = migrated.get("electrical_model")
        if not isinstance(electrical_model, dict):
            raise ValueError(
                "В формате v3 раздел 'electrical_model' обязателен и должен быть объектом."
            )
        premature = {"logical_lines", "line_sections"} & set(electrical_model)
        if premature:
            raise ValueError(
                "Формат v3 содержит поля следующей версии: "
                f"{sorted(premature)}."
            )
        electrical_model["logical_lines"] = []
        electrical_model["line_sections"] = []
        migrated["catalogs"] = {
            "format_version": 1,
            "catalog_kind": "user",
            "entries": [],
        }
        migrated["format_version"] = 4
        migration_steps.append(
            "Добавлены логические линии, физические ветви и пользовательский каталог v4."
        )
    if migrated.get("format_version") == 4:
        electrical_model = migrated.get("electrical_model")
        if not isinstance(electrical_model, dict):
            raise ValueError(
                "В формате v4 раздел 'electrical_model' обязателен и должен быть объектом."
            )
        raw_states = electrical_model.get("operating_states")
        if not isinstance(raw_states, list):
            raise ValueError(
                "В формате v4 electrical_model.operating_states должен быть массивом."
            )
        for index, state in enumerate(raw_states):
            if not isinstance(state, dict):
                raise ValueError(
                    "В формате v4 electrical_model.operating_states"
                    f"[{index}] должен быть объектом."
                )
            if "availability" in state:
                raise ValueError(
                    "Формат v4 содержит поле следующей версии: "
                    f"electrical_model.operating_states[{index}].availability."
                )
            # There is no safe basis for inferring an outage from an old
            # project.  Sparse absence means IN_SERVICE in the v5 domain.
            state["availability"] = {}

        raw_types = electrical_model.get("equipment_types")
        if not isinstance(raw_types, list):
            raise ValueError(
                "В формате v4 electrical_model.equipment_types должен быть массивом."
            )
        for index, definition in enumerate(raw_types):
            if not isinstance(definition, dict):
                raise ValueError(
                    "В формате v4 electrical_model.equipment_types"
                    f"[{index}] должен быть объектом."
                )
            definition_id = definition.get("id")
            behavior_key = definition.get("behavior_key")
            expected_native_behavior = _V5_AVAILABILITY_NATIVE_TYPES.get(
                definition_id
            )
            expected_compat_behavior = _V5_AVAILABILITY_COMPAT_TYPES.get(
                definition_id
            )
            extensions = definition.get("extensions")
            capabilities = definition.get("capabilities")
            is_known_native = (
                expected_native_behavior is not None
                and behavior_key == expected_native_behavior
            )
            is_known_compat = (
                expected_compat_behavior is not None
                and behavior_key == expected_compat_behavior
                and isinstance(extensions, dict)
                and extensions.get("compatibility_only") is True
                and isinstance(capabilities, list)
                and "legacy.calculation" in capabilities
            )
            if (
                definition.get("schema_version") != 1
                or not (is_known_native or is_known_compat)
            ):
                continue
            if not isinstance(capabilities, list) or any(
                not isinstance(item, str) for item in capabilities
            ):
                # Leave malformed rows intact for the strict electrical-model
                # codec to reject with its ordinary field context.
                continue
            if "equipment.availability" not in capabilities:
                capabilities.append("equipment.availability")
                capabilities.sort()
        migrated["format_version"] = 5
        migration_steps.append(
            "Добавлена раздельная эксплуатационная доступность оборудования v5."
        )
    if migrated.get("format_version") == 5:
        electrical_model = migrated.get("electrical_model")
        if not isinstance(electrical_model, dict):
            raise ValueError(
                "В формате v5 раздел 'electrical_model' обязателен и должен быть объектом."
            )
        user_catalog = migrated.get("catalogs")
        if not isinstance(user_catalog, dict):
            raise ValueError(
                "В формате v5 раздел 'catalogs' обязателен и должен быть объектом."
            )
        used_ids = _raw_owned_id_values(electrical_model, user_catalog)
        segment_ids_adjusted = _migrate_line_sections_v5_to_v6(
            electrical_model, used_ids
        )
        requested_catalog_id = user_catalog.get("id")
        if not isinstance(requested_catalog_id, str) or not requested_catalog_id:
            requested_catalog_id = DEFAULT_USER_CATALOG_ID.value
        selected_catalog_id = _allocate_migration_id(
            CatalogId,
            requested_catalog_id,
            used_ids,
            "user-catalog",
        )
        user_catalog["id"] = selected_catalog_id
        diagram_id = _allocate_migration_id(
            DiagramDocumentId,
            "diagram.main",
            used_ids,
            "diagram",
        )
        snapshots_id = _allocate_migration_id(
            CatalogId,
            DEFAULT_PROJECT_CATALOG_ID.value,
            used_ids,
            "project-catalog-snapshots",
        )
        migrated["diagram"] = {
            "format_version": 1,
            "id": diagram_id,
            "name": "Однолинейная схема",
            "revision": 0,
            "pages": [],
            "representations": [],
            "extensions": {},
        }
        migrated["catalog_snapshots"] = {
            "format_version": 1,
            "id": snapshots_id,
            "bindings": [],
        }
        migration_steps.append(
            "Разделены электрические ветви и конструктивные участки линии."
        )
        migration_steps.append(
            "Добавлены data-only страницы схемы и проектные снимки каталогов."
        )
        if (
            segment_ids_adjusted
            or selected_catalog_id != requested_catalog_id
            or diagram_id != "diagram.main"
            or snapshots_id != DEFAULT_PROJECT_CATALOG_ID.value
        ):
            migration_steps.append(
                "Для новых сущностей v6 выбраны детерминированные ID без "
                "коллизий с исходным проектом."
            )
        migrated["format_version"] = 6
        migrated["migration_journal"] = [
            _new_migration_record(original_version, 6, migration_steps)
        ]
    if migrated.get("format_version") == 6:
        _migrate_v6_to_v7(migrated)
    return migrated


def _load_structure(raw: dict[str, Any]) -> ProjectStructure:
    data = raw.get("structure")
    if not isinstance(data, dict):
        raise ValueError(
            "В форматах v2/v3/v4/v5/v6/v7 раздел 'structure' обязателен и должен быть объектом."
        )
    allowed = {
        "facilities", "voltage_levels", "bus_sections", "bays", "equipment",
        "placements",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"Структура проекта: неизвестные разделы {sorted(unknown)}")

    for key in allowed:
        _unique_ids(_list(data, key), f"structure.{key}")

    structure = ProjectStructure()
    # Порядок объектов в JSON не должен иметь значения: сначала разрешаем
    # корневые объекты, затем их вложенные КТП/РП.
    pending = [Facility(**_filtered(Facility, row))
               for row in _list(data, "facilities")]
    while pending:
        ready = [item for item in pending
                 if item.parent_id is None or item.parent_id in structure.facilities]
        if not ready:
            unresolved = ", ".join(f"{item.id}->{item.parent_id}" for item in pending)
            raise ValueError(
                "Структура проекта: не найдены родители или обнаружен цикл: "
                + unresolved
            )
        for item in ready:
            structure.add_facility(item)
            pending.remove(item)
    for row in _list(data, "voltage_levels"):
        structure.add_voltage_level(VoltageLevel(**_filtered(VoltageLevel, row)))
    for row in _list(data, "bus_sections"):
        structure.add_bus_section(BusSection(**_filtered(BusSection, row)))
    for row in _list(data, "bays"):
        structure.add_bay(Bay(**_filtered(Bay, row)))
    for source in _list(data, "equipment"):
        row = dict(source)
        refs = row.get("calculation_refs", [])
        if not isinstance(refs, list):
            raise ValueError(
                f"Equipment '{row.get('id')}': calculation_refs должен быть списком."
            )
        row["calculation_refs"] = [
            CalculationRef(**_filtered(CalculationRef, ref)) for ref in refs
        ]
        structure.add_equipment(Equipment(**_filtered(Equipment, row)))
    for row in _list(data, "placements"):
        structure.place_equipment(
            EquipmentPlacement(**_filtered(EquipmentPlacement, row))
        )
    return structure


def _load_network(raw: dict[str, Any], name: str) -> Network:
    for key in ("nodes", "transformers3w", "branches", "loads", "modes"):
        _unique_ids(_list(raw, key), key)

    neutral = raw.get("neutral", {})
    if not isinstance(neutral, dict):
        raise ValueError("Раздел 'neutral' должен быть объектом JSON.")

    net = Network(name=name)
    net.neutral = neutral

    for row in _list(raw, "nodes"):
        net.add_node(Node(**_filtered(Node, row)))

    for source in _list(raw, "transformers3w"):
        row = dict(source)
        if isinstance(row.get("ct_ratio"), list):
            row["ct_ratio"] = tuple(row["ct_ratio"])
        if row.get("ct_ratio") is not None and row.get("ct_node") is None:
            row["ct_node"] = row.get("node_hv")
        if "prot" in row and row["prot"] is not None:
            row["prot"] = ProtectionSettings(
                **_filtered(ProtectionSettings, row["prot"])
            )
        net.add_transformer3w(Transformer3W(**_filtered(Transformer3W, row)))

    for source in _list(raw, "branches"):
        row = dict(source)
        kind = row.get("kind", "branch")
        cls = BRANCH_CLASSES.get(kind)
        if cls is None:
            raise ValueError(f"Неизвестный тип ветви '{kind}' (ветвь {row.get('id')})")
        if row.get("id") in net.branches:
            raise ValueError(
                f"Ветвь '{row.get('id')}' конфликтует со служебной ветвью "
                "трёхобмоточного трансформатора."
            )
        if isinstance(row.get("ct_ratio"), list):
            row["ct_ratio"] = tuple(row["ct_ratio"])
        if row.get("ct_ratio") is not None and row.get("ct_node") is None:
            row["ct_node"] = (row.get("node_to") if row.get("node_from") == "GRID"
                              else row.get("node_from"))
        if "prot" in row:
            row["prot"] = ProtectionSettings(
                **_filtered(ProtectionSettings, row["prot"])
            )
        net.add_branch(cls(**_filtered(cls, row)))

    for row in _list(raw, "loads"):
        net.add_load(Load(**_filtered(Load, row)))
    for row in _list(raw, "modes"):
        net.add_mode(Mode(**_filtered(Mode, row)))
    return net


def _load_methodology(raw: dict[str, Any], path: str | Path) -> Methodology:
    reference = raw.get("methodology")
    if reference is not None and not isinstance(reference, dict):
        raise ValueError("Раздел 'methodology' должен быть объектом JSON.")
    if isinstance(reference, dict) and "file" in reference:
        if not isinstance(reference["file"], str) or not reference["file"].strip():
            raise ValueError("methodology.file должен содержать непустой путь.")
        methodology_path = Path(reference["file"])
        if not methodology_path.is_absolute():
            methodology_path = Path(path).resolve().parent / methodology_path
        if not methodology_path.exists():
            raise FileNotFoundError(
                "Указанный файл методики не найден: " + str(methodology_path)
                + ". Расчёт по методике по умолчанию не выполнялся."
            )
        return Methodology.load(methodology_path)
    if isinstance(reference, dict):
        return Methodology.from_dict(reference)
    return Methodology.load()


def _copy_methodology_override(value: Methodology) -> Methodology:
    """Validate and detach an explicitly supplied calculation profile.

    The existing project format reserves root ``file`` for an external
    reference. Reject that ambiguous profile instead of deleting its metadata
    or writing an allegedly self-contained project that follows a file later.
    """
    if not isinstance(value, Methodology):
        raise ProjectFormatError("methodology_override должен быть объектом Methodology.")
    try:
        methodology = value.snapshot().to_methodology()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ProjectFormatError(f"Некорректный methodology_override: {exc}") from exc
    if "file" in methodology.data:
        raise ProjectFormatError(
            "methodology_override содержит зарезервированное корневое поле 'file': "
            "его нельзя сохранить как независимый inline-профиль без потери данных."
        )
    return methodology


def load_project(path: str | Path, *,
                 methodology_override: Methodology | None = None) -> ProjectData:
    """Load the complete project, optionally using an explicit saved profile.

    An override is validated and copied; the project's external methodology
    file is not read. Its independent inline reference survives Save As.
    Root ``file`` is reserved by the existing reference format and is rejected
    in an override. All electrical, ID, port and presentation checks still run.
    Without an override the established methodology-loading path is unchanged.
    """
    source_path = Path(path).resolve()
    try:
        with open(source_path, encoding="utf-8") as stream:
            raw = json.load(stream, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ProjectFormatError(
            f"Некорректный JSON проекта: строка {exc.lineno}, столбец {exc.colno}: "
            f"{exc.msg}."
        ) from exc
    _validate_json_tree(raw)
    if not isinstance(raw, dict):
        raise ValueError("Корень файла проекта должен быть объектом JSON.")

    source_version = _read_version(raw)
    migrated = _migrate(raw, source_version)
    project_meta = migrated.get("project")
    if not isinstance(project_meta, dict):
        raise ValueError("Раздел 'project' обязателен и должен быть объектом JSON.")

    if source_version < 3:
        legacy_network = _load_network(
            migrated, project_meta.get("name", "Проект")
        )
        electrical_model = import_legacy_network(legacy_network)
    else:
        if "electrical_model" not in migrated:
            raise ValueError(
                "В форматах v3/v4/v5/v6/v7 раздел 'electrical_model' обязателен."
            )
        electrical_model = electrical_model_from_dict(migrated["electrical_model"])
        if source_version == 3:
            # v4 introduced physical line sections and the distinct recloser
            # type.  Add only definitions absent from the v3 project snapshot;
            # existing definitions and instance data are never rewritten.
            for definition in builtin_equipment_types():
                key = (definition.id, definition.schema_version)
                if key not in electrical_model.equipment_types:
                    electrical_model.register_equipment_type(definition)
        metadata_name = project_meta.get("name")
        if not isinstance(metadata_name, str) or not metadata_name.strip():
            raise ValueError(
                "В форматах v3/v4/v5/v6/v7 project.name должен быть непустой строкой."
            )
        if metadata_name != electrical_model.name:
            raise ValueError(
                "project.name и electrical_model.name должны совпадать."
            )
    adaptation = _adapt_project_model_or_blocked(electrical_model)
    net = adaptation.network
    structure = _load_structure(migrated)
    problems = structure.validate(
        None if _calculation_view_may_be_incomplete(adaptation.diagnostics) else net
    )
    if problems:
        raise ValueError("Ошибки физической структуры:\n- " + "\n- ".join(problems))

    if methodology_override is None:
        reference = migrated.get("methodology")
        methodology = _load_methodology(migrated, source_path)
    else:
        methodology = _copy_methodology_override(methodology_override)
        # ProjectData receives a deepcopy below, separate from both the caller
        # and the restored Methodology; no old external reference is retained.
        reference = methodology.data
    _validate_json_tree(methodology.data, "methodology")
    if source_version < 3:
        legacy_auxiliary_used_ids = set(electrical_model._object_id_values())
        user_catalog = UserCatalog(
            catalog_id=CatalogId(_allocate_migration_id(
                CatalogId,
                DEFAULT_USER_CATALOG_ID.value,
                legacy_auxiliary_used_ids,
                "legacy-user-catalog",
            ))
        )
    else:
        raw_catalog = migrated.get("catalogs")
        if not isinstance(raw_catalog, dict):
            raise ValueError(
                "В форматах v4/v5/v6/v7 раздел 'catalogs' обязателен и должен быть объектом."
            )
        user_catalog = user_catalog_from_dict(raw_catalog)
        _validate_user_catalog(electrical_model, user_catalog)

    if source_version < 3:
        diagram = _empty_diagram_document(DiagramDocumentId(
            _allocate_migration_id(
                DiagramDocumentId,
                "diagram.main",
                legacy_auxiliary_used_ids,
                "legacy-diagram",
            )
        ))
        catalog_snapshots = ProjectCatalogSnapshots(
            id=CatalogId(_allocate_migration_id(
                CatalogId,
                DEFAULT_PROJECT_CATALOG_ID.value,
                legacy_auxiliary_used_ids,
                "legacy-project-catalog-snapshots",
            ))
        )
        migration_journal = (
            _new_migration_record(
                source_version,
                FORMAT_VERSION,
                [
                    "Старая расчётная схема импортирована в объектную электрическую модель.",
                    "Добавлены страницы схемы, конструктивные участки и снимки каталогов.",
                ],
            ),
        )
    else:
        if "diagram" not in migrated:
            raise ValueError("В форматах v6/v7 раздел 'diagram' обязателен.")
        if "catalog_snapshots" not in migrated:
            raise ValueError("В форматах v6/v7 раздел 'catalog_snapshots' обязателен.")
        diagram = diagram_from_dict(migrated["diagram"])
        catalog_snapshots = catalog_snapshots_from_dict(
            migrated["catalog_snapshots"]
        )
        migration_journal = _load_migration_journal(
            migrated.get("migration_journal")
        )
    diagram_problems = diagram.validate_targets(electrical_model)
    snapshot_problems = catalog_snapshots.validate_targets(electrical_model)
    if diagram_problems or snapshot_problems:
        raise ValueError(
            "Ошибки ссылок представления проекта:\n- "
            + "\n- ".join((*diagram_problems, *snapshot_problems))
        )
    adapter_diagnostics = tuple((
        *adaptation.diagnostics,
        *_migration_review_diagnostics(diagram, electrical_model),
    ))
    _validate_project_stable_ids(
        electrical_model,
        user_catalog,
        diagram,
        catalog_snapshots,
        _build_project_projection_or_none(
            electrical_model, adapter_diagnostics
        ),
    )
    return ProjectData(
        network=net,
        methodology=methodology,
        metadata=project_meta,
        structure=structure,
        source_format_version=source_version,
        electrical_model=electrical_model,
        methodology_ref=deepcopy(reference) if isinstance(reference, dict) else None,
        adapter_diagnostics=adapter_diagnostics,
        source_path=source_path,
        user_catalog=user_catalog,
        diagram=diagram,
        catalog_snapshots=catalog_snapshots,
        migration_journal=migration_journal,
    )


def load(path: str | Path, *,
         methodology_override: Methodology | None = None
         ) -> tuple[Network, Methodology, dict[str, Any]]:
    """Расчётный интерфейс с тем же override, без обхода блокировок модели."""
    project = load_project(path, methodology_override=methodology_override)
    project.require_calculation_ready()
    return project.network, project.methodology, project.metadata


def _clean(obj: Any) -> dict[str, Any]:
    data = asdict(obj)
    return {
        key: value for key, value in data.items()
        if value not in (None, "", {}, []) or key in ("id", "name", "kind")
    }


def _structure_dict(structure: ProjectStructure) -> dict[str, Any]:
    return {
        "facilities": [_clean(item) for item in structure.facilities.values()],
        "voltage_levels": [_clean(item) for item in structure.voltage_levels.values()],
        "bus_sections": [_clean(item) for item in structure.bus_sections.values()],
        "bays": [_clean(item) for item in structure.bays.values()],
        "equipment": [_clean(item) for item in structure.equipment.values()],
        "placements": [_clean(item) for item in structure.placements.values()],
    }


def save(path: str | Path, net: Network, meta: dict[str, Any] | None = None,
         methodology_file: str | None = None, *,
         structure: ProjectStructure | None = None,
         methodology_ref: dict[str, Any] | None = None) -> None:
    """Legacy API: сохранить расчётный Network в совместимом формате v2.

    Новый v3 нельзя честно восстановить из одного Network: в нём отсутствуют
    самостоятельные реальные QF, порты и Equipment. Для v3 используйте
    ``save_project`` с канонической ``electrical_model``.
    """
    structure = structure or ProjectStructure()
    problems = structure.validate(net)
    if problems:
        raise ValueError("Ошибки физической структуры:\n- " + "\n- ".join(problems))

    generated_nodes = {item.star_node_id for item in net.transformers3w.values()}
    generated_branches = {
        branch_id
        for item in net.transformers3w.values()
        for branch_id in item.branch_ids
    }

    raw: dict[str, Any] = {
        "format_version": 2,
        "project": (meta or {}) | {"name": net.name},
        "neutral": net.neutral,
        "structure": _structure_dict(structure),
        "nodes": [_clean(node) for node in net.nodes.values()
                  if node.id not in generated_nodes],
        "transformers3w": [_clean(item) for item in net.transformers3w.values()],
        "branches": [_clean(branch) for branch in net.branches.values()
                     if branch.id not in generated_branches],
        "loads": [_clean(load_item) for load_item in net.loads.values()],
        "modes": [_clean(mode) for mode in net.modes.values()],
    }
    if methodology_file is not None and methodology_ref is not None:
        raise ValueError("Задайте либо methodology_file, либо methodology_ref, но не оба.")
    if methodology_file is not None:
        raw["methodology"] = {"file": methodology_file}
    elif methodology_ref is not None:
        raw["methodology"] = deepcopy(methodology_ref)

    _write_json(path, raw)


def _methodology_reference_for_target(
    project: ProjectData, target_path: str | Path
) -> dict[str, Any] | None:
    """Rebase a loaded file reference so Save As remains reopenable."""
    if project.methodology_ref is None:
        return None
    reference = deepcopy(project.methodology_ref)
    if "file" not in reference:
        return reference
    raw_file = reference["file"]
    if not isinstance(raw_file, str) or not raw_file.strip():
        raise ProjectFormatError("methodology.file должен содержать непустой путь.")

    methodology_path: Path | None = None
    if project.methodology.path is not None:
        methodology_path = Path(project.methodology.path).resolve()
    else:
        candidate = Path(raw_file)
        if candidate.is_absolute():
            methodology_path = candidate.resolve()
        elif project.source_path is not None:
            methodology_path = (Path(project.source_path).resolve().parent / candidate).resolve()

    # A manually assembled ProjectData may not carry source_path or a loaded
    # Methodology.path. Preserve its explicitly supplied target-relative value.
    if methodology_path is None:
        return reference

    target_parent = Path(target_path).resolve().parent
    try:
        relative = os.path.relpath(methodology_path, start=target_parent)
    except ValueError:  # different Windows drives cannot share a relative path
        reference["file"] = str(methodology_path)
    else:
        reference["file"] = Path(relative).as_posix()
    return reference


def _create_migration_backup(path: Path, source_version: int) -> Path:
    """Создать неизменяемую соседнюю копию до перезаписи старого формата."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Файл для резервного копирования не найден: {path}"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(
        f"{path.name}.before-v{FORMAT_VERSION}.from-v{source_version}."
        f"{stamp}.{uuid4().hex[:8]}.backup"
    )
    shutil.copy2(path, backup)
    # Windows требует дескриптор, открытый на запись, для FlushFileBuffers.
    with open(backup, "r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    return backup


def save_project(path: str | Path, project: ProjectData,
                 methodology_file: str | None = None) -> Path | None:
    """Сохранить v7: canonical ElectricalModel и независимые data-only слои.

    При перезаписи исходного файла старой версии сначала создаётся соседняя
    резервная копия. Возвращаем её путь; обычное сохранение v7 возвращает
    ``None``.
    """
    adapted = project.refresh_calculation_view(force=True)
    _validate_user_catalog(project.electrical_model, project.user_catalog)
    problems = project.structure.validate(
        None if _calculation_view_may_be_incomplete(project.adapter_diagnostics) else adapted
    )
    if problems:
        raise ValueError("Ошибки физической структуры:\n- " + "\n- ".join(problems))
    diagram_problems = project.diagram.validate_targets(project.electrical_model)
    snapshot_problems = project.catalog_snapshots.validate_targets(
        project.electrical_model
    )
    if diagram_problems or snapshot_problems:
        raise ValueError(
            "Ошибки ссылок представления проекта:\n- "
            + "\n- ".join((*diagram_problems, *snapshot_problems))
        )
    _validate_project_stable_ids(
        project.electrical_model,
        project.user_catalog,
        project.diagram,
        project.catalog_snapshots,
        _build_project_projection_or_none(
            project.electrical_model, project.adapter_diagnostics
        ),
    )

    metadata = deepcopy(project.metadata)
    metadata["name"] = project.electrical_model.name
    # ProjectData is mutable for compatibility, therefore callers can replace
    # its journal after loading. Canonicalize and validate it before any
    # backup or write so save_project never creates a v7 file that its own
    # loader rejects.
    migration_journal = list(
        _load_migration_journal(
            [deepcopy(item) for item in project.migration_journal]
        )
    )
    raw: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "project": metadata,
        "structure": _structure_dict(project.structure),
        "electrical_model": electrical_model_to_dict(project.electrical_model),
        "catalogs": user_catalog_to_dict(project.user_catalog),
        "diagram": diagram_to_dict(project.diagram),
        "catalog_snapshots": catalog_snapshots_to_dict(
            project.catalog_snapshots
        ),
        "migration_journal": migration_journal,
    }
    if methodology_file is not None:
        raw["methodology"] = {"file": methodology_file}
    else:
        methodology_reference = _methodology_reference_for_target(project, path)
        if methodology_reference is not None:
            raw["methodology"] = methodology_reference

    # Полностью проверяем будущий JSON до создания резервной копии.
    _validate_json_tree(raw)
    target = Path(path).resolve()
    backup_path: Path | None = None
    if (
        project.source_path is not None
        and Path(project.source_path).resolve() == target
        and project.source_format_version < FORMAT_VERSION
        and target.exists()
    ):
        backup_path = _create_migration_backup(
            target, project.source_format_version
        )
        migration_journal.append(
            _new_migration_record(
                project.source_format_version,
                FORMAT_VERSION,
                [
                    "Создана резервная копия исходного проекта.",
                    "Проект атомарно записан в актуальном формате.",
                ],
                backup_file=backup_path.name,
            )
        )
        migration_journal = list(
            _load_migration_journal(migration_journal)
        )
        raw["migration_journal"] = migration_journal

    _write_json(path, raw)
    project.source_path = target
    project.source_format_version = FORMAT_VERSION
    project.migration_journal = tuple(deepcopy(migration_journal))
    return backup_path


def migrate_project_file(path: str | Path) -> Path | None:
    """Безопасно преобразовать старый проект на месте и вернуть backup."""
    project = load_project(path)
    if project.source_format_version == FORMAT_VERSION:
        return None
    backup = save_project(path, project)
    if backup is None:
        raise ProjectFormatError(
            "Миграция на месте завершилась без обязательной резервной копии."
        )
    return backup


def restore_project_backup(
    backup_path: str | Path,
    target_path: str | Path,
) -> Path | None:
    """Восстановить проект из backup, сохранив копию заменяемого файла."""
    backup = Path(backup_path).resolve()
    target = Path(target_path).resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"Резервная копия не найдена: {backup}")
    # Проверка чтением не допускает восстановления повреждённого JSON.
    load_project(backup)
    previous: Path | None = None
    if target.exists():
        previous = _create_migration_backup(target, FORMAT_VERSION)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore.tmp")
    try:
        shutil.copy2(backup, temporary)
        with open(temporary, "r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return previous
