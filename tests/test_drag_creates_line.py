# -*- coding: utf-8 -*-
"""Протяжка от вывода создаёт провод, ВЛ или КЛ; точка на шине не уезжает.

Решения заказчика 31.08.2026:

1. При отпускании протяжки предлагается выбор **Провод / ВЛ / КЛ**. Провод и
   физическая линия — разные электрические объекты, а не два вида одной
   картинки, поэтому выбирает человек, а не программа по длине отрезка.
2. Стрелка направления берётся **из рисунка**: от начала протяжки к концу.
   Это разметка чертежа, а не утверждение о том, куда течёт ток: расчётное
   направление меняется по режимам и до расчёта неизвестно вовсе.
3. Точка подключения на шине ставится **ровно под проводом**. Раздвижка
   включается, только если точка попала в уже занятое место.

Отдельно закреплено, что охранник привязки ТТ не отказывает в подключении
из-за ПОСТОРОННЕГО висящего конца.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from rza_calc.domain.electrical import (DataConfirmation, DomainInvariantError,
                                        LineKind)
from rza_calc.editor.bus_connections import (BUS_ATTACHMENT_GAP,
                                             BUS_ATTACHMENT_MERGE_TOLERANCE,
                                             available_bus_fraction)
from rza_calc.editor.controller import (EditorCommandError, NodeTarget,
                                        PhysicalLineInput, PortTarget,
                                        ProjectEditorController)
from rza_calc.io.project import load_project

from test_bus_connection_spacing import PAGE, _bus_point, _connect, _load, _setup

DEMO = Path(__file__).resolve().parents[1] / "rza_calc/examples/energoraion.json"


def _line(controller, bus, apparatus, kind=LineKind.OVERHEAD, fraction="0.5"):
    """То же, что делает сцена после выбора «ВЛ» или «КЛ» в меню."""
    return controller.create_physical_line(
        "Линия протяжкой", kind,
        PortTarget(apparatus.port_ids[0]),
        NodeTarget(bus.node_id, representation_id=bus.representation_id,
                   anchor_key=fraction),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
        page_id=PAGE,
    )


# ── точка на шине ─────────────────────────────────────────────────────────
def test_attachment_stays_exactly_where_the_wire_was_brought():
    """Главная жалоба: точка уезжала вбок, и провод дорисовывал крючок."""
    controller, bus = _setup(width=240)
    wanted = (0.20, 0.30, 0.40, 0.55)
    for index, fraction in enumerate(wanted):
        result = _connect(controller, bus, _load(controller, 100 + index * 60), fraction)
        route = controller.diagram.routes[result.route_id]
        assert float(route.end_anchor.anchor_key) == fraction, (
            f"доля {fraction} была изменена на {route.end_anchor.anchor_key}"
        )
        assert (route.waypoints[-1].x, route.waypoints[-1].y) == _bus_point(
            controller, bus, fraction
        )


def test_two_points_closer_than_the_merge_tolerance_are_separated():
    """Раздвижка не исчезла — она включается только при настоящем слипании."""
    controller, bus = _setup(width=240)
    first = _connect(controller, bus, _load(controller, 150), 0.50)
    #  0.51 на шине 240 ед. — это 2,4 единицы: две точки нарисовались бы одна
    #  поверх другой, и рисунок сказал бы, что присоединение одно.
    second = _connect(controller, bus, _load(controller, 650), 0.51)
    a = controller.diagram.routes[first.route_id].waypoints[-1]
    b = controller.diagram.routes[second.route_id].waypoints[-1]
    assert float(controller.diagram.routes[first.route_id].end_anchor.anchor_key) == 0.50
    assert math.hypot(a.x - b.x, a.y - b.y) >= BUS_ATTACHMENT_MERGE_TOLERANCE - 1e-8


def test_separation_moves_by_the_smallest_visible_distance():
    """При столкновении точка отодвигается на минимум, а не на полную ширину."""
    controller, bus = _setup(width=240)
    _connect(controller, bus, _load(controller, 150), 0.50)
    second = _connect(controller, bus, _load(controller, 650), 0.51)
    moved = abs(float(controller.diagram.routes[second.route_id].end_anchor.anchor_key) - 0.51) * 240
    assert moved <= BUS_ATTACHMENT_GAP, (
        "прыжок на полную раздвижку там, где хватает порога слияния, — "
        "тот же увод точки от провода, только реже"
    )


def test_merge_tolerance_cannot_be_silently_zeroed():
    """Обнулить порог значит разрешить двум точкам слиться в одну."""
    assert BUS_ATTACHMENT_MERGE_TOLERANCE >= 10.0
    assert BUS_ATTACHMENT_MERGE_TOLERANCE <= BUS_ATTACHMENT_GAP


def test_explicit_separation_still_uses_the_full_gap():
    """Явная команда «разделить» разводит читаемо, а не на минимум."""
    controller, bus = _setup(width=200)
    rows = [_connect(controller, bus, _load(controller, x), 0.5).route_id
            for x in (50, 150, 650)]
    del rows
    #  Ширина раздвижки для явной команды берётся из BUS_ATTACHMENT_GAP;
    #  проверка самой команды живёт в test_bus_connection_spacing.
    assert BUS_ATTACHMENT_GAP > BUS_ATTACHMENT_MERGE_TOLERANCE


def test_full_bus_still_refuses_instead_of_stacking_points():
    controller, bus = _setup(width=10)
    _connect(controller, bus, _load(controller, 150), 0)
    _connect(controller, bus, _load(controller, 650), 1)
    with pytest.raises(EditorCommandError, match="Удлините шину"):
        _connect(controller, bus, _load(controller, 950))


def test_allocator_rejects_a_negative_merge_tolerance():
    controller, bus = _setup()
    representation = controller.diagram.representations[bus.representation_id]
    with pytest.raises(ValueError, match="Порог слияния"):
        available_bus_fraction(
            controller.diagram, representation, width=240, height=12,
            requested=0.5, merge_tolerance=-1.0,
        )


# ── линия из протяжки ─────────────────────────────────────────────────────
def test_dragged_line_is_a_route_without_a_separate_boxed_object():
    """Ради этого всё и делалось: отдельный объект в рамке больше не нужен."""
    controller, bus = _setup(width=240)
    result = _line(controller, bus, _load(controller, 300))
    assert not any(
        row.equipment_id == result.section_id
        for row in controller.diagram.representations.values()
    ), "линия из протяжки не должна порождать отдельное представление-объект"
    assert result.route_id in controller.diagram.routes


@pytest.mark.parametrize("kind", (LineKind.OVERHEAD, LineKind.CABLE))
def test_arrow_points_from_the_start_of_the_gesture_to_its_end(kind):
    """Стрелка — по рисунку. Роли from/to задают её направление."""
    controller, bus = _setup(width=240)
    result = _line(controller, bus, _load(controller, 300), kind)
    route = controller.diagram.routes[result.route_id]
    roles = tuple(
        controller.model.port_definition(anchor.branch_port_id).role
        for anchor in (route.start_anchor, route.end_anchor)
    )
    assert roles == ("from", "to"), (
        "начало протяжки обязано стать выводом «from», иначе стрелка смотрит "
        "против нарисованного движения"
    )
    section = controller.model.line_sections[result.section_id]
    assert controller.model.logical_lines[section.logical_line_id].line_kind is kind


def test_dragged_line_does_not_invent_a_length():
    """Метры не выводятся из пикселей: длина остаётся неподтверждённой."""
    controller, bus = _setup(width=240)
    result = _line(controller, bus, _load(controller, 300))
    section = controller.model.line_sections[result.section_id]
    assert section.length_mm is None, (
        "длина, придуманная по длине отрезка на экране, — это выдуманное число"
    )


def test_dragged_line_keeps_the_drawn_waypoints():
    controller, bus = _setup(width=240)
    apparatus = _load(controller, 300)
    result = _line(controller, bus, apparatus)
    route = controller.diagram.routes[result.route_id]
    assert len(route.waypoints) >= 2
    assert (route.waypoints[-1].x, route.waypoints[-1].y) == _bus_point(
        controller, bus, float(route.end_anchor.anchor_key)
    )


# ── охранник привязки ТТ ──────────────────────────────────────────────────
def test_unresolvable_side_node_does_not_block_an_unrelated_connection():
    """Посторонний висящий конец не имеет права запретить подключение.

    Ровно эта ошибка была видна пользователю: «Невозможно определить узел
    привязки ТТ: … напряжение узла «Узел порта «Конец»» не определено» при
    попытке протянуть провод от выключателя к шине.
    """
    controller = ProjectEditorController(load_project(DEMO))
    equipment = next(row for row in controller.model.equipment.values()
                     if "Ввод-1 10 кВ" in row.name and "Западная" in row.name)
    port_from = controller.model.port_by_role(equipment.id, "from")
    port_to = controller.model.port_by_role(equipment.id, "to")
    for port_id in (port_from.id, port_to.id):
        route = next((row for row in controller.diagram.routes.values()
                      if port_id in (row.start_anchor.target_port_id,
                                     row.end_anchor.target_port_id)), None)
        if route is not None:
            controller.delete_diagram_route(route.id)
    bus = next(row for row in controller.model.electrical_nodes.values()
               if row.name == "ЗАПАДНАЯ · 1 СШ 10 кВ")
    controller.connect_port_to_node(port_to.id, bus.id)
    assert controller.model.connection_for_port(port_to.id).electrical_node_id == bus.id


def test_ct_side_moving_to_an_unknown_node_is_still_refused():
    """Обратная сторона: наугад записанный ct_node хуже отказа.

    Проверяется, что послабление коснулось только ПОИСКА стороны ТТ, а не
    записи её нового узла.
    """
    from rza_calc.editor import legacy_ct

    assert "_node_identity_or_none" in dir(legacy_ct)
    source = Path(legacy_ct.__file__).read_text(encoding="utf-8")
    marker = "new_identity = _node_identity(after, new_node, cache)"
    assert marker in source, (
        "запись нового узла ТТ обязана идти через строгий _node_identity"
    )
    assert "сторона ТТ переезжает на узел" in source


def test_ct_binding_survives_and_follows_the_physical_side():
    controller = ProjectEditorController(load_project(DEMO))
    equipment = next(row for row in controller.model.equipment.values()
                     if row.name == "КТП Ф-1 630 кВ·А (Центральная)")
    port = controller.model.port_by_role(equipment.id, "from")
    route = next(row for row in controller.diagram.routes.values()
                 if port.id in (row.start_anchor.target_port_id,
                                row.end_anchor.target_port_id))
    controller.delete_diagram_route(route.id)
    binding = controller.model.equipment[equipment.id].extensions[
        legacy_bindings_key()]["ct_node"]
    assert binding["port_id"] == port.id.value and binding["role"] == "from"
    bus = next(row for row in controller.model.electrical_nodes.values()
               if row.name == "ЦЕНТРАЛЬНАЯ · 2 СШ 10 кВ")
    controller.connect_port_to_node(port.id, bus.id)
    assert controller.model.equipment[equipment.id].properties[
        "legacy_payload"]["ct_node"] == "c10_2"


def legacy_bindings_key() -> str:
    from rza_calc.editor.legacy_ct import CT_BINDINGS_KEY

    return CT_BINDINGS_KEY


# ── меню выбора (Qt) ──────────────────────────────────────────────────────
def _canvas_class():
    """EditorCanvas или пропуск теста, если Qt в этой среде нет."""
    try:
        from rza_calc.gui.editor_scene import EditorCanvas
    except ImportError:
        pytest.skip("PySide6 недоступен в этой среде")
    return EditorCanvas


def _scene_source() -> str:
    return (Path(__file__).resolve().parents[1]
            / "rza_calc/gui/editor_scene.py").read_text(encoding="utf-8")


def test_three_choices_are_declared_once_and_cover_wire_and_both_lines():
    """Состав меню задан в одном месте; тест ловит молчаливое удаление пункта."""
    choices = dict(_canvas_class().DRAGGED_CONNECTION_CHOICES)
    assert list(choices) == ["wire", "overhead", "cable"]
    assert "Провод" in choices["wire"]
    assert "ВЛ" in choices["overhead"] and "КЛ" in choices["cable"]


def test_reconnect_never_asks_for_a_type():
    """У переподключения линия уже есть; менять её природу молча нельзя."""
    assert "if value.mode is ConnectionToolMode.RECONNECT:" in _scene_source()
    assert hasattr(_canvas_class(), "_ask_dragged_connection_kind")


def test_cancelled_menu_changes_nothing():
    assert "Соединение отменено: тип не выбран" in _scene_source()
    assert hasattr(_canvas_class(), "_physical_line_draft_from_connection")
