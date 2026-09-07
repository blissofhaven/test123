# -*- coding: utf-8 -*-
"""Коды возврата CLI (дефект OPEN-14, этап A2).

Скрипт, вызывающий программу, видит только код возврата. Пока «схема описана
неверно» и «защита не проходит по чувствительности» возвращали одно и то же
ненулевое число, автоматическая проверка не могла их различить — и любой
конвейер вынужден был либо игнорировать оба события, либо останавливаться на
обоих.

Проверяется контракт, а не текущее поведение: значения кодов зафиксированы
здесь до того, как на них начнут опираться.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from rza_calc.cli import (EXIT_INPUT_ERROR, EXIT_OK, EXIT_PROTECTION_FAILS,
                          EXIT_UNRESOLVED, exit_code)
from rza_calc.cli import main as cli_main
from rza_calc.core.result import FAIL, OK, UNRESOLVED, ProtectionResult

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya.json"
DEMO = ROOT / "tests" / "fixtures" / "legacy_projects" / "energoraion.json"


def _run(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(argv)
    return code, output.getvalue()


class _FakeResult:
    """Минимальная подмена результата: важен только состав статусов."""

    def __init__(self, statuses: list[str], failures: bool):
        self._statuses = statuses
        self.has_failures = failures

    def all_results(self):
        rows = []
        for status in self._statuses:
            row = ProtectionResult("b", "Ветвь", "МТЗ")
            row.status = status
            rows.append(row)
        return rows


def test_codes_are_distinct():
    """Четыре события — четыре разных кода, иначе разделять нечего."""
    codes = {EXIT_OK, EXIT_PROTECTION_FAILS, EXIT_INPUT_ERROR, EXIT_UNRESOLVED}
    assert len(codes) == 4


def test_input_error_is_two_and_not_one():
    """Ошибка исходных данных отделена от непройденной уставки."""
    code, text = _run([str(ROOT / "нет-такого-файла.json"), "check"])
    assert code == EXIT_INPUT_ERROR
    assert "не найден" in text


def test_check_ignores_protection_failures():
    """`check` отвечает за ИСХОДНЫЕ ДАННЫЕ, а не за результат расчёта.

    В демо-сети есть заведомые нарушения защит. Раньше они делали `check`
    ненулевым, и по нему нельзя было понять, исправна ли модель.
    """
    code, _ = _run([str(DEMO), "check"])
    assert code == EXIT_OK

    table_code, _ = _run([str(DEMO), "table"])
    assert table_code == EXIT_PROTECTION_FAILS, (
        "нарушения обязаны быть видны там, где спрашивали про результат"
    )


def test_modes_is_also_an_input_command():
    code, _ = _run([str(DEMO), "modes"])
    assert code == EXIT_OK


def test_failure_wins_over_unresolved():
    """Нарушение важнее незавершённости: скрипт обязан узнать о худшем."""
    result = _FakeResult([FAIL, UNRESOLVED, OK], failures=True)
    assert exit_code(result, "table") == EXIT_PROTECTION_FAILS


def test_unresolved_has_its_own_code():
    """«Не смогли посчитать» — не то же самое, что «всё хорошо».

    Именно это правило запрещает выдавать неопределённый результат за ноль.
    """
    result = _FakeResult([OK, UNRESOLVED], failures=False)
    assert exit_code(result, "table") == EXIT_UNRESOLVED


def test_clean_result_is_zero():
    result = _FakeResult([OK, OK], failures=False)
    assert exit_code(result, "report") == EXIT_OK


def test_unknown_command_is_an_input_error_not_a_silent_success():
    code, _ = _run([str(EXAMPLE), "нет-такой-команды"])
    assert code == EXIT_INPUT_ERROR


def test_documented_codes_match_the_implementation():
    """Докстрока модуля — часть контракта, и она обязана совпадать с кодом."""
    from rza_calc import cli

    doc = cli.__doc__
    for code, meaning in (
        (EXIT_OK, "нарушений не найдено"),
        (EXIT_PROTECTION_FAILS, "непройденные проверки защит"),
        (EXIT_UNRESOLVED, "не завершена"),
        (EXIT_INPUT_ERROR, "расчёт НЕ выполнен"),
    ):
        assert f"    {code}   " in doc, f"код {code} не описан в докстроке"
        assert meaning in doc, f"смысл кода {code} не описан: «{meaning}»"
