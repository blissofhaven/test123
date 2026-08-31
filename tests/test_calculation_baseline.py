# -*- coding: utf-8 -*-
"""Эталон расчётных чисел: любое непреднамеренное изменение роняет сборку.

Это ворота этапа A1. После него два исполнителя могут работать параллельно:
правка интерфейса, из-за которой изменилось расчётное число, перестаёт быть
находкой «через несколько недель» и становится красной сборкой сразу.

Правило, действующее для обоих исполнителей: **покрасневший тест — это повод
искать причину, а не обновлять эталон.** Обновление разрешено только командой
`tools_update_baseline.py` с указанием этапа и причины и записью в
`docs/calculation-audit/baseline-log.md`.
"""
from pathlib import Path

import baseline_snapshot as snapshot

BASELINE = snapshot.BASELINE_PATH


def test_baseline_file_exists_and_is_readable():
    assert BASELINE.exists(), (
        f"Эталон {BASELINE} отсутствует. Создать: python tools_update_baseline.py "
        "--stage <этап> --reason <причина> --yes"
    )
    data = snapshot.load_baseline()
    assert data["schema"] == snapshot.SCHEMA
    assert set(data["projects"]) == set(snapshot.PROJECTS)


def test_baseline_tolerance_is_declared_and_justified():
    """Допуск обязан быть записан в эталоне явно, а не жить только в коде."""
    tolerance = snapshot.load_baseline()["tolerance"]
    assert tolerance["relative"] == snapshot.RELATIVE_TOLERANCE
    assert tolerance["absolute"] == snapshot.ABSOLUTE_TOLERANCE
    assert tolerance["note"].strip(), "у допуска должно быть обоснование"


def test_calculation_numbers_match_the_frozen_baseline():
    """Полное сравнение всех расчётных чисел трёх демо-проектов с эталоном."""
    expected = snapshot.load_baseline()
    actual = snapshot.build_all()
    differences = snapshot.compare(expected["projects"], actual["projects"])
    assert not differences, "\n" + snapshot.format_report(differences)


def test_baseline_covers_every_protection_and_fault_point():
    """Эталон обязан покрывать всё, а не выборочные объекты."""
    data = snapshot.load_baseline()
    totals = {"faults": 0, "protections": 0, "pairs": 0}
    for project in data["projects"].values():
        totals["faults"] += sum(len(rows) for rows in project["fault_currents"].values())
        totals["protections"] += len(project["protections"])
        totals["pairs"] += len(project["selectivity"])
    assert totals["faults"] >= 700, totals
    assert totals["protections"] >= 280, totals
    assert totals["pairs"] >= 700, totals


def test_baseline_distinguishes_absent_value_from_zero():
    """«Нет числа» и «ноль» — разные состояния, эталон обязан их различать.

    Иначе защита, у которой уставка не выбрана, выглядела бы как защита с
    нулевой уставкой, и подмена прошла бы незамеченной.
    """
    data = snapshot.load_baseline()
    has_null = False
    for project in data["projects"].values():
        for row in project["protections"].values():
            if row["i_primary_a"] is None:
                has_null = True
                assert row["status"] in ("fail", "unresolved"), row
    assert has_null, "в эталоне нет ни одной защиты без уставки — проверьте охват"


def test_report_shows_the_cause_without_a_debugger():
    """Отчёт о расхождении обязан называть объект, величину, было, стало и Δ."""
    expected = {"p": {"protections": {"L1|МТЗ": {"i_primary_a": 100.0}}}}
    actual = {"p": {"protections": {"L1|МТЗ": {"i_primary_a": 101.0}}}}
    report = snapshot.format_report(snapshot.compare(expected, actual))
    for fragment in ("L1|МТЗ", "i_primary_a", "100", "101", "1"):
        assert fragment in report, report
    assert "tools_update_baseline.py" in report, "отчёт обязан называть штатный путь"


def test_comparison_does_not_depend_on_key_order():
    left = {"a": {"x": 1.0, "y": 2.0}, "b": 3.0}
    right = {"b": 3.0, "a": {"y": 2.0, "x": 1.0}}
    assert snapshot.compare(left, right) == []


def test_comparison_catches_a_change_smaller_than_display_precision():
    """Изменение, невидимое в таблице уставок, обязано ронять тест."""
    left = {"i": 1234.5678}
    right = {"i": 1234.5678 * (1 + 1e-7)}
    differences = snapshot.compare(left, right)
    assert len(differences) == 1
    assert differences[0].relative_percent is not None
