# -*- coding: utf-8 -*-
"""Явная Qt-независимая машина состояний инструментов редактора.

Модуль хранит только краткоживущее состояние взаимодействия. Он не содержит
ссылок на электрическую модель, не создаёт объекты проекта и не записывает
undo-команды. Фиксация результата остаётся обязанностью контроллера проекта.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .orientation import editor_rotation, next_editor_rotation
from .state import EditorMode


class EditorTool(StrEnum):
    """Взаимоисключающие инструменты одного редактора схемы."""

    SELECT = "select"
    PLACE_EQUIPMENT_ONCE = "place_equipment_once"
    PLACE_EQUIPMENT_REPEAT = "place_equipment_repeat"
    DRAW_CONNECTION = "draw_connection"
    DRAG_OBJECT = "drag_object"
    DRAG_ROUTE_SEGMENT = "drag_route_segment"
    MARQUEE_SELECT = "marquee_select"
    PAN = "pan"
    ROTATE_PREVIEW = "rotate_preview"
    ROTATE_OBJECT = "rotate_object"


_EDIT_ONLY_TOOLS = frozenset(
    {
        EditorTool.PLACE_EQUIPMENT_ONCE,
        EditorTool.PLACE_EQUIPMENT_REPEAT,
        EditorTool.DRAW_CONNECTION,
        EditorTool.DRAG_OBJECT,
        EditorTool.DRAG_ROUTE_SEGMENT,
        EditorTool.ROTATE_PREVIEW,
        EditorTool.ROTATE_OBJECT,
    }
)
_PLACEMENT_TOOLS = frozenset(
    {
        EditorTool.PLACE_EQUIPMENT_ONCE,
        EditorTool.PLACE_EQUIPMENT_REPEAT,
    }
)

_TOOL_NAMES: dict[EditorTool, str] = {
    EditorTool.SELECT: "Выбор",
    EditorTool.PLACE_EQUIPMENT_ONCE: "Вставка оборудования",
    EditorTool.PLACE_EQUIPMENT_REPEAT: "Многократная вставка оборудования",
    EditorTool.DRAW_CONNECTION: "Провод",
    EditorTool.DRAG_OBJECT: "Перемещение объекта",
    EditorTool.DRAG_ROUTE_SEGMENT: "Перемещение сегмента линии",
    EditorTool.MARQUEE_SELECT: "Рамочное выделение",
    EditorTool.PAN: "Перемещение полотна",
    EditorTool.ROTATE_PREVIEW: "Поворот предварительного объекта",
    EditorTool.ROTATE_OBJECT: "Поворот объекта",
}

_TOOL_HINTS: dict[EditorTool, str] = {
    EditorTool.SELECT: "Щёлкните объект для выбора или протяните рамку по свободному месту.",
    EditorTool.PLACE_EQUIPMENT_ONCE: "Укажите место для одного объекта; Esc отменяет вставку.",
    EditorTool.PLACE_EQUIPMENT_REPEAT: "Размещайте объекты последовательно; Esc завершает вставку.",
    EditorTool.DRAW_CONNECTION: "Укажите электрические окончания соединения; Esc отменяет операцию.",
    EditorTool.DRAG_OBJECT: "Переместите выбранный объект и отпустите кнопку мыши.",
    EditorTool.DRAG_ROUTE_SEGMENT: "Переместите выбранный сегмент линии и отпустите кнопку мыши.",
    EditorTool.MARQUEE_SELECT: "Протяните рамку вокруг нужных объектов.",
    EditorTool.PAN: "Перетаскивайте свободное поле схемы.",
    EditorTool.ROTATE_PREVIEW: "Поворачивается только предварительное изображение объекта.",
    EditorTool.ROTATE_OBJECT: "Удерживайте маркер поворота; отпустите для фиксации, Esc — отмена.",
}


def _normalized_mode(value: EditorMode | str) -> EditorMode:
    try:
        return value if isinstance(value, EditorMode) else EditorMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Неизвестный режим редактора.") from exc


def _normalized_tool(value: EditorTool | str) -> EditorTool:
    try:
        return value if isinstance(value, EditorTool) else EditorTool(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Неизвестный инструмент редактора.") from exc


def _normalized_quarter_turn(value: int | float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Угол предварительного объекта должен быть числом.")
    try:
        return editor_rotation(value)
    except ValueError as exc:
        raise ValueError("Угол предварительного объекта должен быть кратен 90°.") from exc


@dataclass(frozen=True, slots=True)
class EditorToolSnapshot:
    """Неизменяемый снимок текущего состояния для GUI и тестов."""

    editor_mode: EditorMode
    tool: EditorTool
    payload: Any = None
    tool_name: str = ""
    preview_rotation_deg: int = 0
    resume_tool: EditorTool | None = None

    @property
    def active(self) -> bool:
        return self.tool is not EditorTool.SELECT

    @property
    def is_placement(self) -> bool:
        return self.tool in _PLACEMENT_TOOLS or (
            self.tool is EditorTool.ROTATE_PREVIEW
            and self.resume_tool in _PLACEMENT_TOOLS
        )

    @property
    def is_repeat_placement(self) -> bool:
        return self.tool is EditorTool.PLACE_EQUIPMENT_REPEAT or (
            self.tool is EditorTool.ROTATE_PREVIEW
            and self.resume_tool is EditorTool.PLACE_EQUIPMENT_REPEAT
        )

    @property
    def display_name(self) -> str:
        name = self.tool_name.strip()
        if self.tool is EditorTool.PLACE_EQUIPMENT_ONCE and name:
            return f"Вставка: {name}"
        if self.tool is EditorTool.PLACE_EQUIPMENT_REPEAT and name:
            return f"Многократная вставка: {name}"
        if self.tool is EditorTool.DRAW_CONNECTION and name:
            return name
        if self.tool is EditorTool.ROTATE_PREVIEW and name:
            return f"Поворот: {name}"
        return _TOOL_NAMES[self.tool]

    @property
    def hint(self) -> str:
        return _TOOL_HINTS[self.tool]

    @property
    def status_text(self) -> str:
        mode = "Редактирование" if self.editor_mode is EditorMode.EDIT else "Анализ"
        insertion = ""
        if self.is_placement:
            insertion = (
                " • многократная вставка"
                if self.is_repeat_placement
                else " • одноразовая вставка"
            )
        return f"{mode} • {self.display_name}{insertion} • {self.hint}"


@dataclass(frozen=True, slots=True)
class EditorToolTransition:
    """Результат одного детерминированного перехода машины состояний."""

    previous: EditorToolSnapshot
    current: EditorToolSnapshot
    accepted: bool
    message: str
    cancelled: bool = False
    clear_selection: bool = False
    select_created_object: bool = False

    @property
    def changed(self) -> bool:
        return self.previous != self.current


class EditorToolStateMachine:
    """Единственный владелец активного инструмента редактора.

    Первый ``Esc`` отменяет активный инструмент, не затрагивая выбор. Когда
    уже активен ``SELECT``, следующий ``Esc`` может запросить снятие выбора.
    Сам выбор хранится снаружи, поэтому вызывающая сторона передаёт
    ``has_selection``.
    """

    def __init__(self, editor_mode: EditorMode | str = EditorMode.EDIT):
        self._editor_mode = _normalized_mode(editor_mode)
        self._tool = EditorTool.SELECT
        self._payload: Any = None
        self._tool_name = ""
        self._preview_rotation_deg = 0
        self._resume_tool: EditorTool | None = None

    @property
    def state(self) -> EditorToolSnapshot:
        return EditorToolSnapshot(
            self._editor_mode,
            self._tool,
            self._payload,
            self._tool_name,
            self._preview_rotation_deg,
            self._resume_tool,
        )

    @property
    def tool(self) -> EditorTool:
        return self._tool

    @property
    def editor_mode(self) -> EditorMode:
        return self._editor_mode

    def _transition(
        self,
        previous: EditorToolSnapshot,
        *,
        accepted: bool = True,
        message: str | None = None,
        cancelled: bool = False,
        clear_selection: bool = False,
        select_created_object: bool = False,
    ) -> EditorToolTransition:
        current = self.state
        return EditorToolTransition(
            previous,
            current,
            accepted,
            message if message is not None else current.status_text,
            cancelled,
            clear_selection,
            select_created_object,
        )

    def _reset_to_select(self) -> None:
        self._tool = EditorTool.SELECT
        self._payload = None
        self._tool_name = ""
        self._preview_rotation_deg = 0
        self._resume_tool = None

    def activate(
        self,
        tool: EditorTool | str,
        *,
        payload: Any = None,
        tool_name: str = "",
        preview_rotation_deg: int | float = 90,
    ) -> EditorToolTransition:
        """Активировать ровно один инструмент и заменить предыдущий."""

        normalized = _normalized_tool(tool)
        previous = self.state
        if normalized in _EDIT_ONLY_TOOLS and self._editor_mode is EditorMode.ANALYSIS:
            return self._transition(
                previous,
                accepted=False,
                message=(
                    f"В режиме «Анализ» инструмент «{_TOOL_NAMES[normalized]}» недоступен."
                ),
            )
        if normalized in _PLACEMENT_TOOLS and payload is None:
            raise ValueError("Для вставки оборудования не задан объект библиотеки.")
        if normalized is EditorTool.ROTATE_PREVIEW:
            return self.begin_preview_rotation(clockwise=True)

        self._tool = normalized
        self._payload = None if normalized is EditorTool.SELECT else payload
        self._tool_name = "" if normalized is EditorTool.SELECT else str(tool_name).strip()
        self._preview_rotation_deg = (
            _normalized_quarter_turn(preview_rotation_deg)
            if normalized in _PLACEMENT_TOOLS
            else 0
        )
        self._resume_tool = None
        return self._transition(previous)

    def begin_placement(
        self,
        payload: Any,
        tool_name: str,
        *,
        repeat: bool = False,
        preview_rotation_deg: int | float = 90,
    ) -> EditorToolTransition:
        return self.activate(
            EditorTool.PLACE_EQUIPMENT_REPEAT
            if repeat
            else EditorTool.PLACE_EQUIPMENT_ONCE,
            payload=payload,
            tool_name=tool_name,
            preview_rotation_deg=preview_rotation_deg,
        )

    def select_tool(self) -> EditorToolTransition:
        return self.activate(EditorTool.SELECT)

    def set_editor_mode(self, mode: EditorMode | str) -> EditorToolTransition:
        """Синхронизировать режим и отменить запрещённую операцию."""

        normalized = _normalized_mode(mode)
        previous = self.state
        self._editor_mode = normalized
        cancelled = False
        if normalized is EditorMode.ANALYSIS and self._tool in _EDIT_ONLY_TOOLS:
            self._reset_to_select()
            cancelled = True
        message = self.state.status_text
        if cancelled:
            message = "Режим «Анализ»: активная операция редактирования отменена."
        return self._transition(previous, message=message, cancelled=cancelled)

    def begin_preview_rotation(
        self,
        *,
        clockwise: bool = True,
        current_rotation_deg: int | float | None = None,
    ) -> EditorToolTransition:
        """Переключить ghost-preview между вертикалью и горизонталью.

        Направление клавиши сохранено в API, но при двух положениях оба
        направления ведут в одно и то же соседнее положение.
        ``current_rotation_deg`` передаёт фактический угол автоматического
        preview: его ось могла измениться при наведении на линию.
        """

        previous = self.state
        placement_tool = (
            self._resume_tool
            if self._tool is EditorTool.ROTATE_PREVIEW
            else self._tool
        )
        if placement_tool not in _PLACEMENT_TOOLS:
            return self._transition(
                previous,
                accepted=False,
                message="Предварительный объект для поворота не выбран.",
            )
        next_rotation = next_editor_rotation(
            self._preview_rotation_deg
            if current_rotation_deg is None
            else current_rotation_deg
        )
        self._resume_tool = placement_tool
        self._tool = EditorTool.ROTATE_PREVIEW
        self._preview_rotation_deg = next_rotation
        return self._transition(
            previous,
            message=f"Предварительный объект повернут на {self._preview_rotation_deg}°.",
        )

    def finish_preview_rotation(self) -> EditorToolTransition:
        """Вернуться к вставке, сохранив угол и тот же payload."""

        previous = self.state
        if self._tool is not EditorTool.ROTATE_PREVIEW or self._resume_tool not in _PLACEMENT_TOOLS:
            return self._transition(
                previous,
                accepted=False,
                message="Поворот предварительного объекта не выполняется.",
            )
        self._tool = self._resume_tool
        self._resume_tool = None
        return self._transition(previous)

    def placement_succeeded(self) -> EditorToolTransition:
        """Завершить one-shot либо продолжить явно закреплённую вставку."""

        previous = self.state
        placement_tool = (
            self._resume_tool
            if self._tool is EditorTool.ROTATE_PREVIEW
            else self._tool
        )
        if placement_tool not in _PLACEMENT_TOOLS:
            return self._transition(
                previous,
                accepted=False,
                message="Нет активной операции вставки оборудования.",
            )
        if placement_tool is EditorTool.PLACE_EQUIPMENT_ONCE:
            self._reset_to_select()
            message = "Объект размещён. Активен инструмент «Выбор»."
        else:
            self._tool = EditorTool.PLACE_EQUIPMENT_REPEAT
            self._resume_tool = None
            message = (
                f"Объект размещён. Многократная вставка: "
                f"{self._tool_name or 'оборудование'} — Esc для завершения."
            )
        return self._transition(
            previous,
            message=message,
            select_created_object=True,
        )

    def placement_failed(self, reason: str = "") -> EditorToolTransition:
        """Оставить инструмент активным после недопустимой фиксации."""

        previous = self.state
        placement_tool = (
            self._resume_tool
            if self._tool is EditorTool.ROTATE_PREVIEW
            else self._tool
        )
        if placement_tool not in _PLACEMENT_TOOLS:
            return self._transition(
                previous,
                accepted=False,
                message="Нет активной операции вставки оборудования.",
            )
        self._tool = placement_tool
        self._resume_tool = None
        detail = str(reason).strip() or "выбранное место недопустимо"
        return self._transition(
            previous,
            accepted=False,
            message=f"Нельзя разместить объект: {detail}.",
        )

    def cancel_active_tool(self) -> EditorToolTransition:
        previous = self.state
        if self._tool is EditorTool.SELECT:
            return self._transition(
                previous,
                message="Активен инструмент «Выбор».",
            )
        cancelled_name = previous.display_name
        self._reset_to_select()
        return self._transition(
            previous,
            message=f"Инструмент «{cancelled_name}» отменён.",
            cancelled=True,
        )

    def escape(self, *, has_selection: bool) -> EditorToolTransition:
        """Реализовать двухступенчатый Esc без скрытого переключателя."""

        if self._tool is not EditorTool.SELECT:
            return self.cancel_active_tool()
        previous = self.state
        if has_selection:
            return self._transition(
                previous,
                message="Выделение снято.",
                clear_selection=True,
            )
        return self._transition(
            previous,
            message="Нет активной операции или выделения.",
        )


__all__ = [
    "EditorTool",
    "EditorToolSnapshot",
    "EditorToolStateMachine",
    "EditorToolTransition",
]
