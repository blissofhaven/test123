# -*- coding: utf-8 -*-
"""Централизованные пользовательские строки базового редактора.

Внутренние ключи стабильны и не показываются пользователю. Компоненты GUI
этапа 3 используют :func:`tr`, поэтому русские подписи и сообщения не
разбрасываются по обработчикам холста.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any


_RU = {
    "mode.edit": "Редактирование",
    "mode.analysis": "Анализ",
    "panel.project": "Проект",
    "panel.equipment": "Оборудование",
    "panel.properties": "Свойства",
    "panel.issues": "Ошибки и предупреждения",
    "panel.diagnostics": "Диагностика",
    "panel.journal": "Журнал",
    "property.general": "Общие",
    "property.electrical": "Электрические",
    "property.graphics": "Графическое оформление",
    "property.service": "Служебные данные",
    "command.add_equipment": "Добавить оборудование",
    "command.add_node": "Добавить электрический узел",
    "command.place": "Разместить объект на схеме",
    "command.move": "Переместить объекты",
    "command.resize": "Изменить размер объекта",
    "command.rotate": "Изменить ориентацию объекта",
    "command.label": "Изменить подпись объекта",
    "command.property": "Изменить электрическое свойство",
    "command.rename": "Переименовать оборудование",
    "command.switch": "Переключить коммутационный аппарат",
    "command.paste": "Вставить объекты",
    "command.duplicate": "Дублировать объекты",
    "command.delete_project": "Удалить объекты из проекта",
    "command.remove_page": "Убрать объекты с этой страницы",
    "command.create_page": "Создать страницу схемы",
    "journal.done": "Выполнено: {description}",
    "journal.undo": "Отменено: {description}",
    "journal.redo": "Повторено: {description}",
    "error.edit_mode_required": "Операция доступна только в режиме редактирования.",
    "error.analysis_move": "В режиме анализа изменение геометрии схемы запрещено.",
    "error.empty_selection": "Не выбрано ни одного объекта.",
    "error.page_missing": "Страница схемы не найдена.",
    "error.representation_missing": "Графическое представление не найдено.",
    "error.equipment_missing": "Оборудование не найдено.",
    "error.node_missing": "Электрический узел не найден.",
    "error.clipboard_empty": "Буфер обмена редактора пуст.",
    "error.remove_last_representation": (
        "Нельзя убрать последнее представление объекта со страницы. "
        "Сначала разместите его на другой странице или явно пометьте как «Не размещено»."
    ),
    "error.switch_confirmation": "Для переключения аппарата требуется подтверждение пользователя.",
    "error.delete_confirmation": "Для удаления объекта из проекта требуется подтверждение пользователя.",
    "error.structure_delete": (
        "Удаление из проекта заблокировано: навигационная структура содержит "
        "ссылки на расчётную схему. Сначала удалите или переназначьте эти связи "
        "в дереве проекта, чтобы сохранение не создало повреждённый проект."
    ),
    "error.external_change": "Проект был изменён вне общей истории команд; операция не выполнена.",
    "error.undo_empty": "Нет команды для отмены.",
    "error.redo_empty": "Нет команды для повтора.",
    "error.command_failed": "Операция не выполнена: {details}",
    "default.page": "Основная схема",
    "default.node": "Электрический узел",
    "copy.suffix": " — копия",
}


RU_STRINGS = MappingProxyType(_RU)


def tr(key: str, **values: Any) -> str:
    """Вернуть русскую строку по стабильному внутреннему ключу."""
    try:
        template = RU_STRINGS[key]
    except KeyError as exc:
        raise KeyError(f"Неизвестный ключ строки интерфейса: {key}") from exc
    return template.format(**values)


__all__ = ["RU_STRINGS", "tr"]
