# -*- coding: utf-8 -*-
"""Сериализуемое состояние рабочего поля и графического оформления.

Эти данные принадлежат Diagram Model и никогда не передаются в
``ElectricalModel``. Формат хранится в ``extensions`` существующего v6
проекта, поэтому этап 3 не вводит параллельный файл проекта.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping

from rza_calc.domain.diagram import DiagramDocument, DiagramRoute, GraphicalRepresentation
from rza_calc.domain.electrical import DomainInvariantError, thaw_json

from .orientation import OrientationMode, normalize_orientation_mode


WORKSPACE_EXTENSION_KEY = "stage3_workspace"
GRAPHICS_EXTENSION_KEY = "stage3_graphics"
UNPLACED_EXTENSION_KEY = "stage3_unplaced_target_ids"


class EditorMode(StrEnum):
    EDIT = "edit"
    ANALYSIS = "analysis"


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainInvariantError(f"{name} должно быть числом.")
    result = float(value)
    if not math.isfinite(result):
        raise DomainInvariantError(f"{name}: NaN и Infinity недопустимы.")
    return result


@dataclass(frozen=True, slots=True)
class EditorWorkspaceState:
    """Параметры рабочего поля, сохраняемые вместе с проектом."""

    mode: EditorMode = EditorMode.EDIT
    grid_visible: bool = True
    snap_enabled: bool = True
    grid_size: float = 20.0
    zoom: float = 1.0
    view_x: float = 0.0
    view_y: float = 0.0
    open_panels: tuple[str, ...] = ("project", "properties", "issues")
    developer_diagnostics: bool = False
    confirm_switching: bool = True
    active_page_id: str | None = None
    active_operating_state_id: str | None = None
    show_labels: bool = True
    show_parameters: bool = True
    show_results: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, EditorMode):
            try:
                object.__setattr__(self, "mode", EditorMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise DomainInvariantError("Неизвестный режим редактора.") from exc
        for name in (
            "grid_visible",
            "snap_enabled",
            "developer_diagnostics",
            "confirm_switching",
            "show_labels",
            "show_parameters",
            "show_results",
        ):
            if not isinstance(getattr(self, name), bool):
                raise DomainInvariantError(f"{name} должно иметь логическое значение.")
        grid_size = _finite(self.grid_size, "Шаг сетки")
        zoom = _finite(self.zoom, "Масштаб")
        view_x = _finite(self.view_x, "Положение области просмотра X")
        view_y = _finite(self.view_y, "Положение области просмотра Y")
        if grid_size <= 0:
            raise DomainInvariantError("Шаг сетки должен быть больше нуля.")
        if not 0.02 <= zoom <= 50.0:
            raise DomainInvariantError("Масштаб должен быть в диапазоне от 2% до 5000%.")
        panels = tuple(self.open_panels)
        if any(not isinstance(item, str) or not item.strip() for item in panels):
            raise DomainInvariantError("Имена открытых панелей должны быть непустыми строками.")
        if len(panels) != len(set(panels)):
            raise DomainInvariantError("Открытая панель не должна повторяться.")
        if self.active_page_id is not None and (
            not isinstance(self.active_page_id, str) or not self.active_page_id.strip()
        ):
            raise DomainInvariantError(
                "Идентификатор активной страницы должен быть непустой строкой."
            )
        if self.active_operating_state_id is not None and (
            not isinstance(self.active_operating_state_id, str)
            or not self.active_operating_state_id.strip()
        ):
            raise DomainInvariantError(
                "Идентификатор активного режима должен быть непустой строкой."
            )
        object.__setattr__(self, "grid_size", grid_size)
        object.__setattr__(self, "zoom", zoom)
        object.__setattr__(self, "view_x", view_x)
        object.__setattr__(self, "view_y", view_y)
        object.__setattr__(self, "open_panels", panels)

    @classmethod
    def from_diagram(cls, diagram: DiagramDocument) -> "EditorWorkspaceState":
        value = diagram.extensions.get(WORKSPACE_EXTENSION_KEY, {})
        if not isinstance(value, Mapping):
            raise DomainInvariantError("Сохранённые параметры рабочего поля повреждены.")
        known = {
            "mode", "grid_visible", "snap_enabled", "grid_size", "zoom",
            "view_x", "view_y", "open_panels", "developer_diagnostics",
            "confirm_switching", "active_page_id", "active_operating_state_id",
            "show_labels", "show_parameters", "show_results",
        }
        unknown = set(value) - known
        if unknown:
            raise DomainInvariantError(
                "Параметры рабочего поля содержат неизвестные поля: "
                + ", ".join(sorted(unknown))
            )
        return cls(**thaw_json(value))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "grid_visible": self.grid_visible,
            "snap_enabled": self.snap_enabled,
            "grid_size": self.grid_size,
            "zoom": self.zoom,
            "view_x": self.view_x,
            "view_y": self.view_y,
            "open_panels": list(self.open_panels),
            "developer_diagnostics": self.developer_diagnostics,
            "confirm_switching": self.confirm_switching,
            "active_page_id": self.active_page_id,
            "active_operating_state_id": self.active_operating_state_id,
            "show_labels": self.show_labels,
            "show_parameters": self.show_parameters,
            "show_results": self.show_results,
        }


def diagram_with_workspace(
    diagram: DiagramDocument,
    workspace: EditorWorkspaceState,
) -> DiagramDocument:
    values = thaw_json(diagram.extensions)
    values[WORKSPACE_EXTENSION_KEY] = workspace.to_mapping()
    return replace(diagram, extensions=values, revision=diagram.revision + 1)


@dataclass(frozen=True, slots=True)
class RepresentationGraphics:
    """Редактируемая геометрия символа и его подписи."""

    width: float = 80.0
    height: float = 50.0
    label_x: float = 0.0
    label_y: float = -32.0
    label_visible: bool = True
    line_width: float = 1.5
    # Отсутствующее поле принадлежит старому проекту. MANUAL не допускает
    # неожиданного автоматического поворота уже сохранённой схемы.
    orientation_mode: OrientationMode = OrientationMode.MANUAL
    # None belongs to pre-B3 data: saved offsets are kept until an explicit
    # reset. False opts into automatic layout, True pins even default offsets.
    label_manual: bool | None = None

    def __post_init__(self) -> None:
        for name in ("width", "height", "label_x", "label_y", "line_width"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.width <= 0 or self.height <= 0:
            raise DomainInvariantError("Размер графического объекта должен быть больше нуля.")
        if self.line_width <= 0:
            raise DomainInvariantError("Толщина линии должна быть больше нуля.")
        if not isinstance(self.label_visible, bool):
            raise DomainInvariantError("Видимость подписи должна иметь логическое значение.")
        if self.label_manual is not None and not isinstance(self.label_manual, bool):
            raise DomainInvariantError("Ручное положение подписи должно быть логическим или null.")
        try:
            object.__setattr__(
                self,
                "orientation_mode",
                normalize_orientation_mode(self.orientation_mode),
            )
        except ValueError as exc:
            raise DomainInvariantError(str(exc)) from exc

    @classmethod
    def from_representation(
        cls, representation: GraphicalRepresentation
    ) -> "RepresentationGraphics":
        value = representation.extensions.get(GRAPHICS_EXTENSION_KEY, {})
        if not isinstance(value, Mapping):
            raise DomainInvariantError("Сохранённое графическое оформление повреждено.")
        known = {
            "width",
            "height",
            "label_x",
            "label_y",
            "label_visible",
            "line_width",
            "orientation_mode",
            "label_manual",
        }
        unknown = set(value) - known
        if unknown:
            raise DomainInvariantError(
                "Графическое оформление содержит неизвестные поля: "
                + ", ".join(sorted(unknown))
            )
        return cls(**thaw_json(value))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "label_x": self.label_x,
            "label_y": self.label_y,
            "label_visible": self.label_visible,
            "line_width": self.line_width,
            "orientation_mode": self.orientation_mode.value,
            "label_manual": self.label_manual,
        }


def label_is_manual(representation: GraphicalRepresentation | DiagramRoute) -> bool:
    """Keep all explicit pre-B3 positions; their origin cannot be recovered.

    Old automatic generators and a person's drag wrote identical offsets.
    Guessing from their numeric values would silently discard user work.
    The same legacy containers as the canvas are accepted for read-only use.
    """

    values = representation.extensions
    graphics = values.get(GRAPHICS_EXTENSION_KEY)
    if not isinstance(graphics, Mapping) or not graphics:
        graphics = values.get("graphics")
    if not isinstance(graphics, Mapping) or not graphics:
        graphics = values
    manual = graphics.get("label_manual")
    if manual is not None:
        if not isinstance(manual, bool):
            raise DomainInvariantError("Ручное положение подписи должно быть логическим или null.")
        return manual
    return any(
        key in graphics
        for key in ("label_x", "label_y", "label_offset_x", "label_offset_y")
    )


def representation_with_graphics(
    representation: GraphicalRepresentation,
    graphics: RepresentationGraphics,
) -> GraphicalRepresentation:
    values = thaw_json(representation.extensions)
    if graphics.label_manual is None:
        # Materializing width/height must not turn a previously automatic
        # label into a legacy-pinned one merely by adding default coordinates.
        graphics = replace(graphics, label_manual=label_is_manual(representation))
    values[GRAPHICS_EXTENSION_KEY] = graphics.to_mapping()
    return replace(representation, extensions=values)


def orientation_mode_for_representation(
    representation: GraphicalRepresentation,
) -> OrientationMode:
    """Прочитать режим, считая старое отсутствие поля безопасным MANUAL."""

    value = representation.extensions.get(GRAPHICS_EXTENSION_KEY, {})
    if not isinstance(value, Mapping):
        raise DomainInvariantError("Сохранённое графическое оформление повреждено.")
    try:
        return normalize_orientation_mode(
            value.get("orientation_mode", OrientationMode.MANUAL)
        )
    except ValueError as exc:
        raise DomainInvariantError(str(exc)) from exc


def representation_with_orientation_mode(
    representation: GraphicalRepresentation,
    mode: OrientationMode | str,
) -> GraphicalRepresentation:
    """Изменить только режим, не материализуя legacy-значения размеров."""

    try:
        normalized = normalize_orientation_mode(mode)
    except ValueError as exc:
        raise DomainInvariantError(str(exc)) from exc
    values = thaw_json(representation.extensions)
    current = values.get(GRAPHICS_EXTENSION_KEY, {})
    if not isinstance(current, Mapping):
        raise DomainInvariantError("Сохранённое графическое оформление повреждено.")
    graphics = dict(current)
    graphics["orientation_mode"] = normalized.value
    values[GRAPHICS_EXTENSION_KEY] = graphics
    return replace(representation, extensions=values)


__all__ = [
    "EditorMode",
    "EditorWorkspaceState",
    "GRAPHICS_EXTENSION_KEY",
    "OrientationMode",
    "RepresentationGraphics",
    "UNPLACED_EXTENSION_KEY",
    "WORKSPACE_EXTENSION_KEY",
    "diagram_with_workspace",
    "label_is_manual",
    "orientation_mode_for_representation",
    "representation_with_graphics",
    "representation_with_orientation_mode",
]
