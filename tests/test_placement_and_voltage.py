# -*- coding: utf-8 -*-
"""Призрак размещения, автокласс напряжения и транзитные точки.

Замечания заказчика 31.08.2026 по снимкам экрана:

1. Текст в окне ошибки не виден — тёмный по тёмному.
2. Между выключателем и нагрузкой три отрезка вместо одного провода.
3. Треугольник нагрузки слишком крупный.
4. В палитре выбран выключатель, а под курсором показывается чужой символ.
5. Новый аппарат нельзя довести до шины, пока не выберешь класс напряжения.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rza_calc.core.engine import run
from rza_calc.domain.electrical import DomainInvariantError, VoltageClassId
from rza_calc.editor.controller import EditorCommandError
from rza_calc.editor.symbols import canonical_key, default_size, symbol_for
from rza_calc.io.project import load, load_project

from test_bus_connection_spacing import PAGE, _setup

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "tests/fixtures/legacy_projects/ps_promyshlennaya.json"
U6 = VoltageClassId("builtin.voltage.ac.6kv")
U10 = VoltageClassId("builtin.voltage.ac.10kv")


# ── 1. Диалоги ────────────────────────────────────────────────────────────
def test_dialogs_carry_their_own_background():
    """Правило `*` задаёт тёмный текст всему; фон диалога обязан быть своим.

    Без явного фона QMessageBox брал системный, и в тёмной теме Windows
    сообщение об ошибке становилось нечитаемым. Сообщение, которое нельзя
    прочесть, хуже отсутствующего: видно, что что-то не так, и не видно что.
    """
    from rza_calc.gui.theme import STYLESHEET

    assert "QMessageBox" in STYLESHEET
    block = STYLESHEET[STYLESHEET.index("QMessageBox, QInputDialog"):]
    block = block[:block.index("}")]
    assert "background: #FFFFFF" in block
    assert "color: #172033" in block


# ── 3. Размер нагрузки ────────────────────────────────────────────────────
def test_load_symbol_is_smaller_than_a_breaker():
    """Нагрузка перестала быть самым крупным объектом схемы."""
    load_w, load_h = default_size("load")
    breaker_w, breaker_h = default_size("circuit_breaker")
    assert load_w * load_h < breaker_w * breaker_h
    assert (load_w, load_h) == (24.0, 32.0)   # на сетке 8 ед.


# ── 4. Призрак размещения ─────────────────────────────────────────────────
def test_breaker_and_disconnector_ghosts_are_not_the_same_picture():
    """Выбрал выключатель — под курсором обязан быть выключатель.

    Прежний призрак рисовался отдельным кодом, одинаковым для выключателя и
    разъединителя: две чёрточки и диагональ. Два рисунка одного и того же
    неизбежно расходятся, и здесь они разошлись с библиотекой обозначений.
    """
    breaker = symbol_for("circuit_breaker", "", width=64.0, height=32.0)
    disconnector = symbol_for("disconnector", "", width=64.0, height=32.0)
    assert breaker.primitives != disconnector.primitives
    assert any(item.kind == "rect" for item in breaker.primitives), (
        "у включённого выключателя есть корпус; призрак обязан его показывать"
    )
    assert not any(item.kind == "rect" for item in disconnector.primitives)


def test_ghost_uses_the_symbol_library_not_its_own_drawing():
    """Проверяется отсутствие второй графики, а не конкретные координаты."""
    source = (ROOT / "rza_calc/gui/editor_scene.py").read_text(encoding="utf-8")
    assert "_placement_symbol" in source
    assert 'painter.drawLine(QPointF(-10.0, 0.0), QPointF(10.0, -height * 0.25))' not in source, (
        "вернулся отдельный рисунок призрака вместо примитивов библиотеки"
    )


def test_ghost_size_comes_from_the_symbol_when_the_palette_is_silent():
    """80×50 было общим умолчанием и не совпадало с тем, что рисуется."""
    for key in ("circuit_breaker", "disconnector", "load", "transformer_2w"):
        width, height = default_size(canonical_key(key, ""))
        assert (width, height) != (80.0, 50.0)
        geometry = symbol_for(key, "", width=width, height=height)
        assert geometry.width == width and geometry.height == height


# ── 5. Класс напряжения ───────────────────────────────────────────────────
def test_new_apparatus_takes_the_voltage_class_from_the_first_connection():
    """Главное неудобство: аппарат из палитры нельзя было довести до шины."""
    controller, bus = _setup(width=240)
    added = controller.add_equipment(
        "builtin.circuit_breaker", "Q-новый", page_id=PAGE, x=300, y=240
    )
    assert not controller.model.equipment[added.equipment_id].voltage_class_by_group
    controller.connect_port_to_node(
        added.port_ids[0], bus.node_id, page_id=PAGE,
        source_representation_id=added.representation_id,
        node_representation_id=bus.representation_id,
    )
    assert controller.model.equipment[added.equipment_id].voltage_class_by_group == {
        "main": U10
    }


def test_an_already_chosen_class_is_never_overwritten_by_a_connection():
    """Заданный класс — решение человека; от него зависят все уставки."""
    controller, bus = _setup(width=240)
    added = controller.add_equipment(
        "builtin.circuit_breaker", "Q-шесть", page_id=PAGE, x=300, y=240,
        voltage_class_by_group={"main": U6},
    )
    with pytest.raises(EditorCommandError):
        controller.connect_port_to_node(
            added.port_ids[0], bus.node_id, page_id=PAGE,
            source_representation_id=added.representation_id,
            node_representation_id=bus.representation_id,
        )
    assert controller.model.equipment[added.equipment_id].voltage_class_by_group == {
        "main": U6
    }, "класс, выбранный человеком, подменён подключением"


def test_adopt_returns_false_and_changes_nothing_when_the_class_is_set():
    controller, _ = _setup(width=240)
    added = controller.add_equipment(
        "builtin.circuit_breaker", "Q", page_id=PAGE, x=300, y=240,
        voltage_class_by_group={"main": U6},
    )
    model = controller.model
    assert model.adopt_group_voltage_class(added.equipment_id, "main", U10) is False
    assert model.equipment[added.equipment_id].voltage_class_by_group == {"main": U6}


def test_adopting_an_unregistered_class_is_refused():
    controller, _ = _setup(width=240)
    added = controller.add_equipment(
        "builtin.circuit_breaker", "Q", page_id=PAGE, x=300, y=240
    )
    with pytest.raises(DomainInvariantError, match="не зарегистрирован"):
        controller.model.adopt_group_voltage_class(
            added.equipment_id, "main", VoltageClassId("нет.такого.класса")
        )


def test_changing_the_class_of_a_connected_apparatus_explains_both_sides():
    """Отказ обязан называть обе стороны и говорить, что делать.

    Заказчик просил разрешить смену с предупреждением. Разрешить её правкой
    редактора нельзя: домен держит инвариант «класс вывода совпадает с классом
    узла», и на нём стоит весь расчётный путь. Инвариант не ломался; вместо
    глухого отказа даётся понятное сообщение.
    """
    controller, bus = _setup(width=240)
    added = controller.add_equipment(
        "builtin.circuit_breaker", "Q1", page_id=PAGE, x=300, y=240
    )
    controller.connect_port_to_node(
        added.port_ids[0], bus.node_id, page_id=PAGE,
        source_representation_id=added.representation_id,
        node_representation_id=bus.representation_id,
    )
    with pytest.raises(EditorCommandError) as error:
        controller.set_equipment_voltage_class(added.equipment_id, "main", U6)
    message = str(error.value)
    assert "10 кВ" in message and "6 кВ" in message
    assert "Отсоедините вывод" in message
    assert "builtin.voltage" not in message, (
        "в сообщении для человека не должно быть внутренних идентификаторов"
    )


# ── 2. Транзитные точки ───────────────────────────────────────────────────
def test_demo_draws_one_wire_from_the_breaker_to_the_load():
    """Три отрезка с двумя точками превратились в один провод."""
    project = load_project(DEMO)
    drawn_nodes = [row for row in project.diagram.representations.values()
                   if row.electrical_node_id is not None]
    assert len(drawn_nodes) == 3, (
        "на схеме обязаны остаться только шины: ОРУ 110 кВ и две секции 10 кВ"
    )
    assert len(project.electrical_model.electrical_nodes) == 24, (
        "узлы скрыты только на рисунке; из МОДЕЛИ они не удалялись"
    )
    assert not project.diagram.validate_targets(project.electrical_model)


def test_hiding_transit_points_did_not_touch_a_single_number():
    """Рисунок не имеет права двигать расчёт."""
    network, methodology, _ = load(DEMO)
    result = run(network, methodology)
    rows = result.all_results()
    assert len(rows) == 23
    assert sum(1 for row in rows if str(row.status) == "ok") == 19
    assert all(str(pair.status) == "ok" for pair in result.pairs)


def test_fault_point_at_the_feeder_end_still_exists():
    """Скрытая точка обязана остаться расчётной точкой КЗ."""
    network, methodology, _ = load(DEMO)
    result = run(network, methodology)
    solver = next(iter(result.ctx.solvers.values()))
    ends = [node_id for node_id in network.nodes if node_id.endswith("_end")]
    assert len(ends) == 8
    for node_id in ends:
        assert solver.at(node_id).i3 > 0.0
