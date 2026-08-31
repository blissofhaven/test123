# -*- coding: utf-8 -*-
"""Снимок расчётных чисел проекта — эталон и сравнение с ним.

Модуль намеренно не зависит ни от pytest, ни от Qt: его используют и
регрессионный тест `test_calculation_baseline.py`, и утилита обновления
эталона `tools_update_baseline.py`. Одна реализация снимка на оба применения —
иначе тест и утилита могли бы разойтись, и эталон обновлялся бы не тем, что
проверяется.

Что входит в снимок
-------------------
* по каждому узлу и каждому режиму: Iк(3), Iк(2), Zth (R и X отдельно),
  расчётное напряжение ступени;
* по каждой защите: расчётный ток, первичная и вторичная уставка, выдержка,
  определяющий режим, статус и **все** проверки со значением и требованием;
* пары селективности с выдержками, Δt и требованием;
* предупреждения расчёта.

Устойчивость снимка
-------------------
* все словари сериализуются с сортировкой ключей — снимок не зависит от
  порядка обхода;
* числа записываются с фиксированным числом значащих цифр через `repr`
  дробной части, без локали;
* `None` сохраняется как `null` и сравнивается как `None`: «нет числа» и
  «число ноль» — разные состояния, и снимок обязан их различать.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from rza_calc.core.engine import run
from rza_calc.io.project import load_project

SCHEMA = "rza-calculation-baseline/1"

#  Значащих цифр при записи. 12 — заведомо больше, чем несёт любая физическая
#  величина расчёта, и заведомо меньше 15–16 цифр двойной точности, поэтому
#  запись не тащит в эталон шум последних битов.
SIGNIFICANT_DIGITS = 12

#  Допуск сравнения. Обоснование, а не подбор:
#  накопленная ошибка обращения матрицы узловых проводимостей имеет порядок
#  eps_double · cond(Y). При числах обусловленности демо-проектов (10^3…10^5)
#  это 10^-11…10^-13 относительной величины. Допуск 1e-9 лежит на два-четыре
#  порядка выше этого шума и на три порядка ниже любого физически значимого
#  изменения (изменение уставки на 0,0001 % уже видно). Абсолютный допуск
#  нужен только для величин вблизи нуля: 1e-12 — ниже порога нулевого
#  сопротивления ядра (1e-12 Ом), поэтому не может замаскировать реальное
#  изменение.
RELATIVE_TOLERANCE = 1e-9
ABSOLUTE_TOLERANCE = 1e-12

BASELINE_PATH = Path(__file__).resolve().parent / "baseline" / "calculation_baseline.json"

#  Проекты, числа которых защищены эталоном.
PROJECTS = {
    #  Схема, которая открывается при запуске программы. Её числа человек
    #  видит первыми, поэтому они защищены эталоном наравне с остальными.
    "ps_promyshlennaya": "rza_calc/examples/ps_promyshlennaya.json",
    "energoraion": "rza_calc/examples/energoraion.json",
    "gtes_sever": "rza_calc/examples/gtes_sever.json",
    "ps_severnaya": "rza_calc/examples/ps_severnaya.json",
}

ROOT = Path(__file__).resolve().parent.parent


def _round(value: Any) -> Any:
    """Округлить до значащих цифр, сохранив None и нечисловые значения."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        return str(number)
    if number == 0.0:
        return 0.0
    return float(f"{number:.{SIGNIFICANT_DIGITS}g}")


def _status(value: Any) -> str:
    return str(getattr(value, "value", value))


def build_snapshot(project_path: str | Path) -> dict[str, Any]:
    """Снять полный снимок расчётных чисел одного проекта."""
    project = load_project(Path(ROOT) / project_path)
    result = run(project.network, project.methodology)

    faults: dict[str, dict[str, Any]] = {}
    for mode_id, solver in result.ctx.solvers.items():
        rows: dict[str, Any] = {}
        for node_id in project.network.nodes:
            try:
                sc = solver.at(node_id)
            except Exception as error:                # обесточенный узел и т. п.
                rows[node_id] = {"error": type(error).__name__}
                continue
            rows[node_id] = {
                "i3_ka": _round(sc.i3),
                "i2_ka": _round(sc.i2),
                "zth_r_ohm": _round(sc.z_th.real),
                "zth_x_ohm": _round(sc.z_th.imag),
                "u_stage_kv": _round(sc.u_stage),
                "regime": _status(sc.regime),
            }
        faults[str(mode_id)] = rows

    protections: dict[str, Any] = {}
    for row in result.all_results():
        key = f"{row.branch_id}|{row.kind}"
        checks = {}
        for check in row.checks:
            checks[str(check.name)] = {
                "value": _round(check.value),
                "required": _round(check.required),
                "status": _status(check.status),
            }
        protections[key] = {
            "name": row.branch_name,
            "i_calc_a": _round(row.i_calc),
            "i_primary_a": _round(row.i_primary),
            "i_secondary_a": _round(row.i_secondary),
            "t_s": _round(row.t),
            "status": _status(row.status),
            "governing_mode": None if row.governing_mode is None else str(
                getattr(row.governing_mode, "id", row.governing_mode)
            ),
            "checks": checks,
        }

    selectivity = {}
    for pair in result.pairs:
        key = f"{pair.upper_id}|{pair.lower_id}|{pair.mode_id}"
        selectivity[key] = {
            "t_upper_s": _round(pair.t_upper),
            "t_lower_s": _round(pair.t_lower),
            "dt_s": _round(pair.dt),
            "required_s": _round(pair.required),
            "status": _status(pair.status),
        }

    return {
        "fault_currents": faults,
        "protections": protections,
        "selectivity": selectivity,
        "warnings": [str(item) for item in result.warnings],
    }


def build_all() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "tolerance": {
            "relative": RELATIVE_TOLERANCE,
            "absolute": ABSOLUTE_TOLERANCE,
            "significant_digits": SIGNIFICANT_DIGITS,
            "note": (
                "Допуск задан до измерений и обоснован двойной точностью: "
                "шум обращения матрицы имеет порядок 1e-11…1e-13 относительной "
                "величины, физически значимое изменение — не менее 1e-6. "
                "Подбирать допуск под полученное расхождение запрещено."
            ),
        },
        "projects": {name: build_snapshot(path) for name, path in sorted(PROJECTS.items())},
    }


def dumps(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── Сравнение ─────────────────────────────────────────────────────────────
class Difference:
    """Одно расхождение снимка с эталоном."""

    __slots__ = ("path", "expected", "actual")

    def __init__(self, path: str, expected: Any, actual: Any):
        self.path, self.expected, self.actual = path, expected, actual

    @property
    def absolute(self) -> float | None:
        if isinstance(self.expected, (int, float)) and isinstance(self.actual, (int, float)):
            return abs(float(self.actual) - float(self.expected))
        return None

    @property
    def relative_percent(self) -> float | None:
        if not isinstance(self.expected, (int, float)) or not isinstance(self.actual, (int, float)):
            return None
        if self.expected == 0:
            return None
        return abs(float(self.actual) - float(self.expected)) / abs(float(self.expected)) * 100.0


def _close(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is actual or expected == actual
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(
            float(actual), float(expected),
            rel_tol=RELATIVE_TOLERANCE, abs_tol=ABSOLUTE_TOLERANCE,
        )
    return expected == actual


def compare(expected: Any, actual: Any, path: str = "") -> list[Difference]:
    """Полное сравнение двух снимков; порядок ключей значения не имеет."""
    differences: list[Difference] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            here = f"{path}/{key}" if path else str(key)
            if key not in actual:
                differences.append(Difference(here, expected[key], "<отсутствует>"))
            elif key not in expected:
                differences.append(Difference(here, "<нет в эталоне>", actual[key]))
            else:
                differences += compare(expected[key], actual[key], here)
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            differences.append(
                Difference(f"{path} (длина)", len(expected), len(actual))
            )
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences += compare(left, right, f"{path}[{index}]")
        return differences
    if not _close(expected, actual):
        differences.append(Difference(path, expected, actual))
    return differences


def format_report(differences: list[Difference], limit: int = 40) -> str:
    """Человекочитаемая таблица расхождений: причина видна без отладчика."""
    if not differences:
        return "Расхождений с эталоном нет."
    lines = [
        f"Расхождений с эталоном: {len(differences)}",
        "",
        f"{'величина':<66} {'эталон':>18} {'стало':>18} {'Δ':>13} {'Δ, %':>11}",
        "─" * 130,
    ]
    for item in differences[:limit]:
        absolute = item.absolute
        percent = item.relative_percent
        lines.append(
            f"{item.path[:66]:<66} "
            f"{_cell(item.expected):>18} {_cell(item.actual):>18} "
            f"{'—' if absolute is None else f'{absolute:.6g}':>13} "
            f"{'—' if percent is None else f'{percent:.4g}':>11}"
        )
    if len(differences) > limit:
        lines.append(f"… ещё {len(differences) - limit} расхождений")
    lines += [
        "",
        "Эталон обновляется ТОЛЬКО командой tools_update_baseline.py с указанием",
        "этапа и причины и записью в docs/calculation-audit/baseline-log.md.",
        "Если причина расхождения не объяснена — это дефект, а не повод обновить эталон.",
    ]
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.10g}"
    text = str(value)
    return text if len(text) <= 18 else text[:15] + "…"
