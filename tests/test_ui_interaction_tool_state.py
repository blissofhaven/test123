# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from rza_calc.editor import (
    EditorMode,
    EditorTool,
    EditorToolStateMachine,
)


def test_only_one_tool_is_active_and_new_tool_replaces_payload() -> None:
    machine = EditorToolStateMachine()
    first_payload = {"type_id": "builtin.circuit_breaker"}
    second_payload = {"source_port_id": "port.1"}

    machine.begin_placement(first_payload, "Выключатель")
    transition = machine.activate(
        EditorTool.DRAW_CONNECTION,
        payload=second_payload,
        tool_name="Провод",
    )

    assert transition.previous.tool is EditorTool.PLACE_EQUIPMENT_ONCE
    assert transition.current.tool is EditorTool.DRAW_CONNECTION
    assert transition.current.payload is second_payload
    assert transition.current.resume_tool is None
    assert "Провод" in transition.current.status_text


def test_default_placement_is_once_and_success_returns_to_select() -> None:
    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.transformer_2w"}

    machine.begin_placement(payload, "Трансформатор")
    transition = machine.placement_succeeded()

    assert transition.previous.tool is EditorTool.PLACE_EQUIPMENT_ONCE
    assert transition.current.tool is EditorTool.SELECT
    assert transition.current.payload is None
    assert transition.select_created_object
    assert "Объект размещён" in transition.message


def test_failed_once_placement_keeps_same_payload_and_angle() -> None:
    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.circuit_breaker"}
    machine.begin_placement(
        payload,
        "Выключатель",
        preview_rotation_deg=90,
    )

    transition = machine.placement_failed("пересечение с Т1")

    assert not transition.accepted
    assert transition.current.tool is EditorTool.PLACE_EQUIPMENT_ONCE
    assert transition.current.payload is payload
    assert transition.current.preview_rotation_deg == 90
    assert "пересечение с Т1" in transition.message


def test_repeat_placement_requires_explicit_request_and_survives_success() -> None:
    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.load"}

    machine.begin_placement(payload, "Нагрузка", repeat=True)
    transition = machine.placement_succeeded()

    assert transition.current.tool is EditorTool.PLACE_EQUIPMENT_REPEAT
    assert transition.current.payload is payload
    assert transition.current.is_repeat_placement
    assert transition.select_created_object
    assert "Esc для завершения" in transition.message


def test_escape_first_cancels_tool_and_second_requests_selection_clear() -> None:
    machine = EditorToolStateMachine()
    machine.begin_placement(
        {"type_id": "builtin.recloser"},
        "Реклоузер",
        repeat=True,
    )

    first = machine.escape(has_selection=True)
    second = machine.escape(has_selection=True)

    assert first.cancelled
    assert not first.clear_selection
    assert first.current.tool is EditorTool.SELECT
    assert second.clear_selection
    assert not second.cancelled
    assert second.current.tool is EditorTool.SELECT


def test_analysis_rejects_edit_tools_but_allows_selection_marquee_and_pan() -> None:
    machine = EditorToolStateMachine(EditorMode.ANALYSIS)

    rejected = machine.begin_placement(
        {"type_id": "builtin.circuit_breaker"}, "Выключатель"
    )
    assert not rejected.accepted
    assert machine.tool is EditorTool.SELECT
    assert "режиме «Анализ»" in rejected.message

    assert machine.activate(EditorTool.MARQUEE_SELECT).accepted
    assert machine.tool is EditorTool.MARQUEE_SELECT
    assert machine.activate(EditorTool.PAN).accepted
    assert machine.tool is EditorTool.PAN
    assert machine.select_tool().current.tool is EditorTool.SELECT


def test_switching_to_analysis_cancels_active_edit_operation() -> None:
    machine = EditorToolStateMachine(EditorMode.EDIT)
    machine.activate(
        EditorTool.DRAW_CONNECTION,
        payload={"source_port_id": "port.1"},
        tool_name="Провод",
    )

    transition = machine.set_editor_mode(EditorMode.ANALYSIS)

    assert transition.cancelled
    assert transition.current.editor_mode is EditorMode.ANALYSIS
    assert transition.current.tool is EditorTool.SELECT
    assert transition.current.payload is None
    assert "операция редактирования отменена" in transition.message


def test_preview_rotation_is_discrete_and_returns_to_same_placement() -> None:
    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.recloser"}
    machine.begin_placement(payload, "Реклоузер", repeat=True)

    rotating = machine.begin_preview_rotation(clockwise=True)
    restored = machine.finish_preview_rotation()

    assert rotating.current.tool is EditorTool.ROTATE_PREVIEW
    assert rotating.current.resume_tool is EditorTool.PLACE_EQUIPMENT_REPEAT
    assert rotating.current.preview_rotation_deg == 180
    assert rotating.current.payload is payload
    assert restored.current.tool is EditorTool.PLACE_EQUIPMENT_REPEAT
    assert restored.current.preview_rotation_deg == 180
    assert restored.current.payload is payload


def test_four_preview_turns_return_to_vertical_without_rounding_error() -> None:
    machine = EditorToolStateMachine()
    machine.begin_placement({"type_id": "builtin.load"}, "Нагрузка")

    for _ in range(4):
        machine.begin_preview_rotation(clockwise=True)
        machine.finish_preview_rotation()

    assert machine.state.preview_rotation_deg == 90
    assert isinstance(machine.state.preview_rotation_deg, int)


def test_counterclockwise_preview_rotation_selects_other_allowed_position() -> None:
    machine = EditorToolStateMachine()
    machine.begin_placement({"type_id": "builtin.load"}, "Нагрузка")

    transition = machine.begin_preview_rotation(clockwise=False)

    assert transition.current.preview_rotation_deg == 180


def test_invalid_placement_payload_and_non_quarter_angle_are_rejected_in_russian() -> None:
    machine = EditorToolStateMachine()

    with pytest.raises(ValueError, match="не задан объект библиотеки"):
        machine.begin_placement(None, "Выключатель")
    with pytest.raises(ValueError, match="кратен 90"):
        machine.begin_placement(
            {"type_id": "builtin.circuit_breaker"},
            "Выключатель",
            preview_rotation_deg=45,
        )


def test_every_tool_has_russian_name_hint_and_status() -> None:
    def has_cyrillic(value: str) -> bool:
        return any("а" <= char.casefold() <= "я" or char in "ёЁ" for char in value)

    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.load"}
    for tool in EditorTool:
        if tool in {
            EditorTool.PLACE_EQUIPMENT_ONCE,
            EditorTool.PLACE_EQUIPMENT_REPEAT,
        }:
            transition = machine.activate(tool, payload=payload, tool_name="Нагрузка")
        elif tool is EditorTool.ROTATE_PREVIEW:
            machine.begin_placement(payload, "Нагрузка")
            transition = machine.begin_preview_rotation()
        else:
            transition = machine.activate(tool)
        assert transition.accepted
        assert has_cyrillic(transition.current.display_name)
        assert has_cyrillic(transition.current.hint)
        assert has_cyrillic(transition.current.status_text)
        machine.select_tool()
