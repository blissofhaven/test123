# -*- coding: utf-8 -*-
"""Объединённая русская диагностика редактора электрических соединений.

Сервис ничего не изменяет и не сохраняет результаты в проект. Он объединяет
инварианты канонической модели, графические ссылки, готовность физических
линий и производную активную топологию. ``core.Network`` здесь намеренно не
импортируется.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from ..domain.diagram import (
    DiagramDocument,
    DiagramRouteKind,
    GraphicalRepresentationId,
    PageId,
)
from ..domain.electrical import (
    DataConfirmation,
    ElectricalModel,
    EquipmentId,
    OperatingStateId,
    PortId,
)
from ..topology import DiagnosticSeverity, TopologyEngine
from .collision import DiagramCollisionService
from .orientation import normalize_quarter_turn


class ProjectDiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ProjectDiagnostic:
    code: str
    severity: ProjectDiagnosticSeverity
    message: str
    action: str
    object_id: str = ""
    related_ids: tuple[str, ...] = ()
    page_id: PageId | None = None
    representation_id: GraphicalRepresentationId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, ProjectDiagnosticSeverity):
            object.__setattr__(
                self, "severity", ProjectDiagnosticSeverity(self.severity)
            )
        object.__setattr__(self, "related_ids", tuple(dict.fromkeys(self.related_ids)))


_TOPOLOGY_MESSAGES: dict[str, tuple[str, str]] = {
    "required_port_unconnected": (
        "Не подключён обязательный электрический порт.",
        "Подключите порт к электрическому узлу, шине или допустимому порту.",
    ),
    "incompatible_port_kind": (
        "Тип электрического порта несовместим с узлом.",
        "Выберите узел совместимого типа или исправьте тип оборудования.",
    ),
    "voltage_conflict": (
        "Обнаружен конфликт классов напряжения.",
        "Проверьте номинальные напряжения соединяемых объектов.",
    ),
    "ambiguous_switch_position": (
        "Не задано положение коммутационного аппарата.",
        "Укажите нормальное состояние «Включён» или «Отключён».",
    ),
    "component_without_source": (
        "Активный участок не связан ни с одним источником питания.",
        "Проверьте соединения и положения коммутационных аппаратов.",
    ),
    "multiple_active_sources": (
        "Участок питается от нескольких источников.",
        "Проверьте, соответствует ли это выбранному режиму сети.",
    ),
    "component_topology_incomplete": (
        "Топология участка не завершена.",
        "Завершите обязательные электрические соединения.",
    ),
    "unknown_voltage": (
        "Класс напряжения участка пока не определён.",
        "Укажите класс напряжения источника, шины или оборудования.",
    ),
}


def _severity(value: Any) -> ProjectDiagnosticSeverity:
    raw = getattr(value, "value", value)
    if raw == DiagnosticSeverity.ERROR.value:
        return ProjectDiagnosticSeverity.ERROR
    if raw == DiagnosticSeverity.INFO.value:
        return ProjectDiagnosticSeverity.INFO
    return ProjectDiagnosticSeverity.WARNING


class ProjectValidationService:
    """Проверить электрическую и графическую части одного проекта."""

    def __init__(self, topology_engine: TopologyEngine | None = None):
        self._topology = topology_engine or TopologyEngine()

    @staticmethod
    def _representation_for_object(
        diagram: DiagramDocument, object_id: str
    ) -> tuple[PageId | None, GraphicalRepresentationId | None]:
        for representation in diagram.representations.values():
            if object_id in {
                representation.target_id.value,
                representation.id.value,
            }:
                return representation.page_id, representation.id
        for route in diagram.routes.values():
            if object_id in {
                route.id.value,
                route.target_id.value,
                route.start_anchor.electrical_node_id.value,
                route.end_anchor.electrical_node_id.value,
            }:
                return route.page_id, route.start_anchor.representation_id
        return None, None

    def validate(
        self,
        model: ElectricalModel,
        diagram: DiagramDocument,
        *,
        adapter_diagnostics: Iterable[Any] = (),
        active_operating_state_id: OperatingStateId | str | None = None,
    ) -> tuple[ProjectDiagnostic, ...]:
        if not isinstance(model, ElectricalModel):
            raise TypeError("ProjectValidationService ожидает ElectricalModel.")
        if not isinstance(diagram, DiagramDocument):
            raise TypeError("ProjectValidationService ожидает DiagramDocument.")

        result: list[ProjectDiagnostic] = []
        seen: set[tuple[str, str]] = set()

        def add(
            code: str,
            severity: ProjectDiagnosticSeverity,
            message: str,
            action: str,
            object_id: str = "",
            related_ids: Iterable[str] = (),
        ) -> None:
            key = code, object_id
            if key in seen:
                return
            seen.add(key)
            page_id, representation_id = self._representation_for_object(
                diagram, object_id
            )
            result.append(ProjectDiagnostic(
                code,
                severity,
                message,
                action,
                object_id,
                tuple(related_ids),
                page_id,
                representation_id,
            ))

        for issue in model.validate_integrity():
            add(
                "domain." + issue.code,
                ProjectDiagnosticSeverity(issue.severity),
                issue.message,
                "Исправьте повреждённую ссылку или удалите некорректный объект.",
                issue.object_id,
            )

        for problem in diagram.validate_targets(model):
            add(
                "diagram.invalid_target",
                ProjectDiagnosticSeverity.ERROR,
                problem,
                "Восстановите графическое представление или удалите повреждённую трассу.",
            )

        # Новые команды редактора записывают только канонические четверти
        # оборота. Формат старых проектов намеренно остаётся шире: конечный
        # угол вроде 45 градусов должен загрузиться без аварии и без скрытого
        # изменения рисунка. Поэтому здесь только сообщаем о legacy-значении;
        # проверка проекта не нормализует и не сохраняет его.
        for representation in diagram.representations.values():
            try:
                normalize_quarter_turn(representation.rotation_deg)
            except ValueError:
                display_name = (
                    representation.label.strip()
                    or representation.symbol_key.strip()
                    or representation.id.value
                )
                add(
                    "diagram.legacy_non_quarter_rotation",
                    ProjectDiagnosticSeverity.WARNING,
                    f"Графическое представление «{display_name}» имеет "
                    f"устаревший угол {representation.rotation_deg:g}°, "
                    "не кратный 90°. Угол оставлен без автоматического "
                    "изменения для совместимости со старым проектом.",
                    "В свойствах объекта явно выберите ориентацию 0°, 90°, "
                    "180° или 270°; электрическая модель при этом не изменится.",
                    representation.id.value,
                )

        # Старые проекты не переставляются автоматически. Фиксируем только
        # реальные перекрытия тел (не недостаточный зазор) и даём Problems
        # точную цель перехода. Проверка Qt-независима и не меняет ни одну
        # координату, электрическую ревизию или соединение.
        collision_service = DiagramCollisionService(diagram, model)
        overlap_groups: dict[
            GraphicalRepresentationId, list[GraphicalRepresentationId]
        ] = {}
        for conflict in collision_service.legacy_overlaps():
            overlap_groups.setdefault(conflict.first_id, []).append(
                conflict.second_id
            )
        for representation_id, related in overlap_groups.items():
            geometry = collision_service.geometries[representation_id]
            related_geometries = tuple(
                collision_service.geometries[item] for item in related
            )
            names = ", ".join(
                f"«{item.display_name}»" for item in related_geometries
            )
            add(
                "diagram.equipment_overlap",
                ProjectDiagnosticSeverity.WARNING,
                f"Тело объекта «{geometry.display_name}» перекрывается с {names}. "
                "Координаты старого проекта оставлены без изменений.",
                "Перейдите к объектам и разнесите их вручную; электрические "
                "соединения при этом не изменяются.",
                representation_id.value,
                tuple(item.value for item in related),
            )

        represented_equipment = {
            item.equipment_id
            for item in diagram.representations.values()
            if item.equipment_id is not None
        }
        routed_equipment = {
            item.equipment_id
            for item in diagram.routes.values()
            if item.kind is DiagramRouteKind.EQUIPMENT_BRANCH
            and item.equipment_id is not None
        }
        for section in model.line_sections.values():
            if (
                section.equipment_id not in represented_equipment
                and section.equipment_id not in routed_equipment
            ):
                add(
                    "diagram.branch_without_representation",
                    ProjectDiagnosticSeverity.WARNING,
                    "Электрическая ветвь линии не имеет графического представления.",
                    "Разместите ветвь на странице схемы или подтвердите, что она намеренно скрыта.",
                    section.equipment_id.value,
                )

        for route in diagram.routes.values():
            if (
                route.kind is DiagramRouteKind.NODE_CONNECTION
                and route.electrical_node_id not in model.electrical_nodes
            ) or (
                route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
                and route.equipment_id not in model.equipment
            ):
                add(
                    "diagram.route_without_electrical_entity",
                    ProjectDiagnosticSeverity.ERROR,
                    "Графическая трасса не имеет соответствующей электрической сущности.",
                    "Переподключите трассу к существующему узлу или электрической ветви.",
                    route.id.value,
                )

        connection_by_port = {
            item.port_id: item for item in model.connections.values()
        }
        for equipment in model.equipment.values():
            definition = model.equipment_type(
                equipment.type_id, equipment.type_version
            )
            definitions_by_role = {
                item.role: item for item in definition.port_definitions
            }
            endpoint_nodes = []
            for port_id in equipment.port_ids:
                port = model.ports[port_id]
                connection = connection_by_port.get(port_id)
                port_definition = definitions_by_role.get(port.role)
                if connection is not None:
                    endpoint_nodes.append(connection.electrical_node_id)
                elif port_definition is not None and port_definition.required:
                    add(
                        "connection.required_port_unconnected",
                        ProjectDiagnosticSeverity.ERROR,
                        f"У оборудования «{equipment.name}» не подключён обязательный порт «{port_definition.display_name}».",
                        "Начните соединение от подсвеченного электрического порта.",
                        equipment.id.value,
                        (port_id.value,),
                    )
            if len(endpoint_nodes) >= 2 and len(set(endpoint_nodes)) == 1:
                add(
                    "connection.branch_self_loop",
                    ProjectDiagnosticSeverity.ERROR,
                    f"Обе стороны оборудования «{equipment.name}» подключены к одному узлу.",
                    "Переподключите один конец ветви к другому электрическому узлу.",
                    equipment.id.value,
                )
            if equipment.type_id.value == "builtin.recloser" and len(endpoint_nodes) != 2:
                add(
                    "recloser.one_side_unconnected",
                    ProjectDiagnosticSeverity.ERROR,
                    f"Реклоузер «{equipment.name}» подключён не с двух сторон.",
                    "Подключите вход и выход реклоузера к разным узлам.",
                    equipment.id.value,
                )

        for section in model.line_sections.values():
            equipment = model.equipment.get(section.equipment_id)
            name = equipment.name if equipment is not None else section.equipment_id.value
            for segment in section.construction_segments:
                if segment.length_mm is None:
                    add(
                        "line.length_missing",
                        ProjectDiagnosticSeverity.ERROR,
                        f"Для линии «{name}» не задана физическая длина.",
                        "Откройте свойства линии и подтвердите физическую длину.",
                        section.equipment_id.value,
                        (segment.id.value,),
                    )
                elif segment.length_confirmation is not DataConfirmation.CONFIRMED:
                    add(
                        "line.length_unconfirmed",
                        ProjectDiagnosticSeverity.ERROR,
                        f"Физическая длина линии «{name}» не подтверждена.",
                        "Проверьте источник данных и нажмите «Подтвердить длину».",
                        section.equipment_id.value,
                        (segment.id.value,),
                    )
                if segment.impedance_confirmation is not DataConfirmation.CONFIRMED:
                    add(
                        "line.impedance_unconfirmed",
                        ProjectDiagnosticSeverity.ERROR,
                        f"Сопротивления линии «{name}» не подтверждены.",
                        "Введите подтверждённые погонные параметры или сопротивления частей.",
                        section.equipment_id.value,
                        (segment.id.value,),
                    )

        for node in model.electrical_nodes.values():
            extensions = node.extensions
            if (
                extensions.get("junction_kind") == "line_tap"
                and not extensions.get("physical_position_confirmed", False)
            ):
                add(
                    "tap.physical_position_unconfirmed",
                    ProjectDiagnosticSeverity.ERROR,
                    f"Для отпайки «{node.name or node.id.value}» не подтверждено физическое положение.",
                    "Укажите расстояние от начала линии, подтверждённые длины частей или оставьте расчёт заблокированным.",
                    node.id.value,
                )

        for diagnostic in adapter_diagnostics:
            if getattr(getattr(diagnostic, "severity", ""), "value", getattr(diagnostic, "severity", "")) != "error":
                continue
            object_id = str(getattr(diagnostic, "object_id", ""))
            code = str(getattr(diagnostic, "code", "adapter.blocked"))
            if code == "line_data_unconfirmed":
                add(
                    "adapter.line_data_unconfirmed",
                    ProjectDiagnosticSeverity.ERROR,
                    "Старая расчётная модель не получила неподтверждённые параметры линии.",
                    "Подтвердите длину и сопротивления указанной линии.",
                    object_id,
                )
            elif code == "migration.electrical_route_review_required":
                add(
                    "adapter.migration_electrical_route_review_required",
                    ProjectDiagnosticSeverity.ERROR,
                    str(getattr(diagnostic, "message", "Требуется проверка миграции электрической ветви.")),
                    "Проверьте электрический смысл перенесённой ветви и подтвердите её параметры.",
                    object_id,
                )
            else:
                add(
                    "adapter." + code,
                    ProjectDiagnosticSeverity.ERROR,
                    str(getattr(diagnostic, "message", "Расчётный адаптер заблокирован.")),
                    "Исправьте указанную проблему электрической модели перед расчётом.",
                    object_id,
                )

        if isinstance(active_operating_state_id, str):
            active_operating_state_id = OperatingStateId(
                active_operating_state_id
            )
        elif (
            active_operating_state_id is not None
            and not isinstance(active_operating_state_id, OperatingStateId)
        ):
            raise TypeError(
                "active_operating_state_id должен быть OperatingStateId, строкой или None."
            )

        active_state = (
            model.operating_states.get(active_operating_state_id)
            if active_operating_state_id is not None
            else None
        )
        mode_name = (
            active_state.name
            if active_state is not None
            else "Основная модель"
            if active_operating_state_id is None
            else f"Неизвестный режим {active_operating_state_id.value}"
        )
        snapshot = self._topology.compile(model, active_operating_state_id)
        for diagnostic in snapshot.diagnostics:
            message_action = _TOPOLOGY_MESSAGES.get(diagnostic.code)
            if message_action is None:
                continue
            object_id = (
                diagnostic.equipment_id.value
                if diagnostic.equipment_id is not None
                else diagnostic.electrical_node_id.value
                if diagnostic.electrical_node_id is not None
                else ""
            )
            related = (
                *(item.value for item in diagnostic.related_equipment_ids),
                *(item.value for item in diagnostic.related_port_ids),
                *(item.value for item in diagnostic.related_node_ids),
            )
            add(
                "topology." + diagnostic.code,
                _severity(diagnostic.severity),
                f"{message_action[0]} Режим: «{mode_name}».",
                message_action[1],
                object_id,
                related,
            )

        return tuple(sorted(
            result,
            key=lambda item: (
                0 if item.severity is ProjectDiagnosticSeverity.ERROR else
                1 if item.severity is ProjectDiagnosticSeverity.WARNING else 2,
                item.code,
                item.object_id,
            ),
        ))


__all__ = [
    "ProjectDiagnostic",
    "ProjectDiagnosticSeverity",
    "ProjectValidationService",
]
