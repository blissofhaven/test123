# -*- coding: utf-8 -*-
"""Централизованные пользовательские строки редактора схем.

Ключи являются внутренним API и не показываются пользователю.  Компоненты
редактора получают подписи через :func:`ui_text`, поэтому добавление второго
языка не потребует поиска строк по виджетам.
"""
from __future__ import annotations

from typing import Any


DEFAULT_LOCALE = "ru-RU"


RU: dict[str, str] = {
    # Главное окно и панели.
    "app.title": "РЗА-Про",
    "editor.title": "Редактор однолинейной схемы",
    "panel.project": "Проект",
    "panel.equipment": "Оборудование",
    "panel.properties": "Свойства",
    "panel.problems": "Ошибки и предупреждения",
    "panel.diagnostics": "Диагностика",
    "panel.journal": "Журнал",
    "panel.unplaced": "Не размещено на схеме",
    # Режимы.
    "mode.edit": "Редактирование",
    "mode.analysis": "Анализ",
    "mode.edit.tooltip": "Добавление, перемещение и изменение оборудования",
    "mode.analysis.tooltip": "Просмотр схемы без случайного изменения геометрии",
    # Верхняя панель.
    "action.file": "Файл",
    "action.save": "Сохранить",
    "action.undo": "Отменить",
    "action.redo": "Повторить",
    "action.fit": "Показать всю схему",
    "action.actual_size": "Масштаб 100 %",
    "action.grid": "Сетка",
    "action.snap": "Привязка",
    "action.validate": "Проверить схему",
    "action.zoom_in": "Увеличить",
    "action.zoom_out": "Уменьшить",
    "action.select_all": "Выделить всё",
    "action.copy": "Копировать",
    "action.paste": "Вставить",
    "action.duplicate": "Дублировать",
    "action.delete_project": "Удалить из проекта",
    "action.remove_page": "Убрать с этой страницы",
    "action.confirm_delete": "Удалить",
    "action.cancel": "Отмена",
    "action.close": "Закрыть",
    "action.select_same_type": "Выделить объекты этого типа",
    "action.cancel_placement": "Отменить размещение",
    "action.developer_overlay": "Показывать служебные данные",
    "action.start_connection": "Начать соединение",
    "action.reconnect": "Переподключить",
    "action.add_tap": "Добавить отпайку",
    "action.remove_tap": "Удалить отпайку",
    "action.split_line": "Разделить линию",
    "action.insert_recloser": "Вставить реклоузер",
    "action.remove_recloser_from_line": "Удалить реклоузер из линии",
    "action.remove_series_equipment_from_line": "Удалить аппарат и восстановить линию",
    "action.confirm_length": "Подтвердить длину",
    "action.delete_connection": "Удалить соединение или линию",
    "action.switch_on": "Включить",
    "action.switch_off": "Отключить",
    "action.create_physical_line": "Создать физическую линию",
    "action.add_route_waypoint": "Добавить точку маршрута",
    "action.remove_route_waypoint": "Удалить точку маршрута",
    "action.properties": "Свойства",
    "action.rotate_right": "Повернуть вправо",
    "action.rotate_left": "Повернуть влево",
    "action.rotate_180": "Развернуть на 180°",
    "action.orientation_vertical": "Вертикально — 90°",
    "action.orientation_horizontal": "Горизонтально — 180°",
    "action.auto_orientation": "Автоматически ориентировать по соединению",
    "action.repeat_placement": "Многократная вставка",
    "action.select_tool": "Выбор",
    "action.find_in_tree": "Перейти к объекту в дереве",
    "action.port_diagnostics": "Диагностика портов",
    # Холст.
    "canvas.empty": "Перетащите оборудование из библиотеки на схему",
    "canvas.edit_hint": "Колесо мыши — масштаб; средняя кнопка или Пробел — обзор",
    "canvas.analysis_hint": "Режим анализа: геометрия схемы защищена от изменений",
    "canvas.snap_hint": "Удерживайте Alt, чтобы временно отключить привязку",
    "canvas.drop_unsupported": "Этот объект нельзя разместить на схеме",
    "canvas.place_once": "Вставка: {name} — один объект; R — повернуть; Esc — отменить",
    "canvas.place_repeat": "Многократная вставка: {name} — Esc для завершения",
    "canvas.select_hint": "Выбор — щелчок выбирает; рамка выделяет группу; Space — обзор",
    # Панель свойств.
    "property.none": "Объект не выбран",
    "property.multiple": "Выбрано объектов: {count}",
    "property.name": "Свойство",
    "property.value": "Значение",
    "property.unit": "Единица",
    "property.source": "Источник",
    "property.required": "Обязательное свойство",
    "property.invalid": "Значение содержит ошибку",
    "property.orientation_preserved": "Сохранено {angle}° (из файла)",
    "property.orientation_hint": "Два положения: вертикально — 90°, горизонтально — 180°. Также можно потянуть угловой маркер поворота на схеме.",
    "group.general": "Общие",
    "group.electrical": "Электрические",
    "group.graphics": "Графическое оформление",
    "group.service": "Служебные данные",
    # Диагностика и журнал.
    "diagnostics.no_problems": "Ошибок и предупреждений нет",
    "diagnostics.no_data": "Диагностические данные отсутствуют",
    "journal.empty": "Операции ещё не выполнялись",
    "status.ready": "Редактор готов",
    "status.saved": "Проект сохранён",
    "status.analysis_locked": "В режиме анализа перемещение запрещено",
    "status.placement_cancelled": "Размещение отменено",
    "status.connection_started": "Выберите цель соединения; щелчок по пустому месту добавляет точку трассы",
    "status.connection_cancelled": "Создание соединения отменено без изменений проекта",
    "status.connection_waypoint_added": "Добавлена закреплённая точка трассы",
    "status.connection_waypoint_removed": "Последняя точка трассы удалена",
    "status.connection_no_waypoint": "В трассе нет пользовательских точек",
    "status.connection_incompatible": "Выбранная цель несовместима",
    "status.connection_completed": "Электрическое соединение создано",
    "status.physical_line_started": "Укажите трассу и завершите ВЛ/КЛ на порте, шине, узле или свободном месте",
    "status.physical_line_completed": "ВЛ/КЛ создана. Задайте марку и физическую длину в свойствах справа; неизвестные данные не подставляются.",
    "status.selection_tool": "Активный инструмент: Выбор",
    "status.rotation_done": "Объект повёрнут; электрические подключения сохранены",
    "status.rotation_blocked": "Поворот невозможен: недостаточно свободного места",
    "status.orientation_two_positions": "Выберите вертикальное положение (90°) или горизонтальное (180°)",
    "status.placement_collision": "Нельзя разместить объект: пересечение с {name}",
    "status.move_collision": "Перемещение отклонено: оборудование нельзя накладывать друг на друга",
    "status.properties_focused": "Свойства выбранного объекта открыты справа",
    # Подтверждения и ошибки.
    "confirm.delete.title": "Удаление оборудования",
    "confirm.delete.text": "Удалить выбранные объекты из проекта вместе с их портами и соединениями?",
    "confirm.switching": "Подтверждать переключение аппаратов",
    "error.title": "Ошибка",
    "warning.title": "Предупреждение",
    # Оборудование.
    "equipment.sources": "Источники",
    "equipment.nodes_buses": "Шины",
    "equipment.lines": "Линии",
    "equipment.switchgear": "Коммутационные аппараты",
    "equipment.transformers": "Трансформаторы",
    "equipment.consumers": "Потребители",
    "equipment.external_grid": "Эквивалент энергосистемы",
    "equipment.connection_point": "Электрический узел",
    "equipment.busbar_horizontal": "Горизонтальная шина",
    "equipment.busbar_vertical": "Вертикальная шина",
    "equipment.line": "Простой участок линии",
    "equipment.overhead_line": "Воздушная линия (ВЛ)",
    "equipment.cable_line": "Кабельная линия (КЛ)",
    "equipment.circuit_breaker": "Выключатель",
    "equipment.disconnector": "Разъединитель",
    "equipment.recloser": "Реклоузер",
    "equipment.transformer_2w": "Двухобмоточный трансформатор",
    "equipment.load": "Нагрузка",
    # Колонки дерева проекта.
    "tree.pages": "Страницы",
    "tree.current_page": "Текущая страница",
    "tree.search": "Поиск…",
}


PROPERTY_LABELS: dict[str, str] = {
    "name": "Наименование",
    "description": "Описание",
    "note": "Примечание",
    "type": "Тип оборудования",
    "voltage_class": "Класс напряжения",
    "normal_position": "Нормальное состояние",
    "x": "Координата X",
    "y": "Координата Y",
    "rotation_deg": "Ориентация",
    "orientation_mode": "Режим ориентации",
    "width": "Ширина",
    "height": "Высота",
    "label": "Подпись",
    "label_visible": "Показывать подпись",
    "label_offset_x": "Смещение подписи X",
    "label_offset_y": "Смещение подписи Y",
    "line_width": "Толщина линии",
    "equipment_id": "Идентификатор оборудования",
    "representation_id": "Идентификатор графического представления",
    "electrical_node_id": "Идентификатор электрического узла",
    "page_id": "Идентификатор страницы",
    "ports": "Электрические порты",
    "connection_count": "Количество соединений",
    "logical_line_id": "Логическая линия",
    "conductor_mark": "Марка проводника",
    "cross_section_mm2": "Сечение",
    "material": "Материал",
    "parallel_count": "Количество параллельных проводников",
    "r1_ohm_per_km": "Удельное активное сопротивление прямой последовательности",
    "x1_ohm_per_km": "Удельное реактивное сопротивление прямой последовательности",
    "r2_ohm_per_km": "Удельное активное сопротивление обратной последовательности",
    "x2_ohm_per_km": "Удельное реактивное сопротивление обратной последовательности",
    "r0_ohm_per_km": "Удельное активное сопротивление нулевой последовательности",
    "x0_ohm_per_km": "Удельное реактивное сопротивление нулевой последовательности",
    "capacitive_current_a_per_km": "Погонный ёмкостный ток",
    "custom_parameters": "Дополнительные параметры",
    "manufacturer": "Производитель",
    "model": "Модель",
    "rated_voltage_v": "Номинальное напряжение",
    "rated_current_a": "Номинальный ток",
    "rated_breaking_current_a": "Номинальный ток отключения",
    "short_circuit_limit_a": "Предельный ток короткого замыкания",
    "thermal_short_time_current_a": "Ток термической стойкости",
    "thermal_duration_s": "Длительность термической стойкости",
    "dynamic_peak_current_a": "Ток динамической стойкости",
    "full_opening_time_s": "Полное время отключения",
    "protection_settings": "Параметры защит",
    "auto_reclose_settings": "Параметры автоматического повторного включения",
    "directional_settings": "Направленные защиты",
    "control_settings": "Параметры управления",
    "scada_settings": "Связь с системой диспетчерского управления",
}


UNIT_LABELS: dict[str, str] = {
    "": "",
    "mm2": "мм²",
    "ohm/km": "Ом/км",
    "A/km": "А/км",
    "V": "В",
    "A": "А",
    "s": "с",
}


def ui_text(key: str, /, **values: Any) -> str:
    """Вернуть русскую строку и безопасно подставить именованные значения."""

    try:
        template = RU[key]
    except KeyError as exc:
        raise KeyError(f"Не найдена пользовательская строка '{key}'.") from exc
    return template.format(**values) if values else template


def property_label(key: str) -> str:
    """Человекочитаемое название свойства без показа внутреннего snake_case."""

    return PROPERTY_LABELS.get(key, "Дополнительный параметр")


def unit_label(unit: str) -> str:
    """Русское инженерное обозначение единицы измерения."""

    return UNIT_LABELS.get(unit, unit)


__all__ = [
    "DEFAULT_LOCALE",
    "PROPERTY_LABELS",
    "RU",
    "UNIT_LABELS",
    "property_label",
    "unit_label",
    "ui_text",
]
