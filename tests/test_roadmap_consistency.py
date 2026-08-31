# -*- coding: utf-8 -*-
"""Согласованность документации проекта.

Над проектом работают два исполнителя и три параллельных агента, а состояние
берётся из `docs/roadmap/status.json` одним запросом. Значит, расхождение между
картой, статусом и фактическими файлами — не мелочь оформления, а неверная
исходная информация для того, кто возьмёт следующий этап.

Тесты дешёвые и ловят ровно те расхождения, которые уже случались:
версия карты разъехалась со статусом (найдено Sol Ultra 30.08.2026), ТЗ этапа
названо в статусе, но файла нет, отчёт закрытого этапа отсутствует.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROADMAP = ROOT / "docs" / "roadmap" / "ROADMAP.md"
STATUS = ROOT / "docs" / "roadmap" / "status.json"
BRIEF = ROOT / "docs" / "roadmap" / "BRIEF-FOR-SOL.md"


def status() -> dict:
    return json.loads(STATUS.read_text(encoding="utf-8"))


def test_roadmap_version_matches_status():
    """Версия карты в заголовке и в статусе обязана совпадать.

    Расхождение означает, что исполнитель, прочитавший статус, считает
    актуальной не ту карту, которую ему отдали.
    """
    header = ROADMAP.read_text(encoding="utf-8").splitlines()
    match = None
    for line in header[:10]:
        match = re.search(r"Версия карты:\s*([0-9]+\.[0-9]+)", line)
        if match:
            break
    assert match, "в заголовке ROADMAP.md нет строки «Версия карты: X.Y»"
    assert match.group(1) == status()["roadmap_version"], (
        f"ROADMAP.md заявляет версию {match.group(1)}, "
        f"status.json — {status()['roadmap_version']}"
    )


def test_brief_version_matches_status():
    """Бриф для Sol заявляет версию карты — она тоже обязана совпадать.

    Первая проверка сравнивала только ROADMAP.md со статусом, и заголовок брифа
    успел отстать на одну версию незамеченным. Расхождение здесь стоит дороже
    обычного: по брифу правят проект напрямую.
    """
    if not BRIEF.exists():
        return
    match = None
    for line in BRIEF.read_text(encoding="utf-8").splitlines()[:10]:
        match = re.search(r"Версия карты:\**\s*([0-9]+\.[0-9]+)", line)
        if match:
            break
    assert match, "в заголовке брифа нет строки «Версия карты: X.Y»"
    assert match.group(1) == status()["roadmap_version"], (
        f"бриф заявляет версию {match.group(1)}, "
        f"status.json — {status()['roadmap_version']}"
    )


def test_every_referenced_stage_spec_exists():
    """ТЗ, названное в статусе, обязано существовать на диске."""
    missing = []
    for stage in status()["stages"]:
        spec = stage.get("spec")
        if spec and not (ROOT / spec).exists():
            missing.append(f"{stage['id']}: {spec}")
    assert not missing, "ТЗ отсутствуют:\n  " + "\n  ".join(missing)


def test_every_closed_stage_has_a_report():
    """Закрытый этап без отчёта — это этап, о котором нечего проверить."""
    problems = []
    for stage in status()["stages"]:
        if stage.get("status") != "done":
            continue
        report = stage.get("report")
        if not report:
            problems.append(f"{stage['id']}: отчёт не указан")
        elif not (ROOT / report).exists():
            problems.append(f"{stage['id']}: {report} не существует")
    assert not problems, "\n  ".join(problems)


def test_defect_registry_is_complete():
    """У каждого дефекта обязаны быть уровень, владелец и этап закрытия."""
    problems = []
    for defect in status().get("defects", []):
        for field in ("id", "level", "title", "owner"):
            if not defect.get(field):
                problems.append(f"{defect.get('id', '?')}: нет поля «{field}»")
        if not (defect.get("closes") or defect.get("closed_by")):
            problems.append(f"{defect['id']}: не указан этап, который его закрывает")
    assert not problems, "\n  ".join(problems)


def test_stage_dependencies_reference_known_stages():
    """Зависимость на несуществующий этап — опечатка, которая блокирует работу."""
    data = status()
    known = {stage["id"] for stage in data["stages"]}
    problems = []
    for stage in data["stages"]:
        for dependency in stage.get("depends", ()):
            if dependency not in known:
                problems.append(f"{stage['id']} зависит от неизвестного «{dependency}»")
    assert not problems, "\n  ".join(problems)


def test_brief_paths_exist():
    """Бриф для Sol называет файлы, по которым он будет работать напрямую.

    Ссылка на несуществующий файл в этом документе стоит дороже обычной
    опечатки: по нему правят проект.
    """
    if not BRIEF.exists():
        return
    text = BRIEF.read_text(encoding="utf-8")
    deleted = {"audit_svg.py", "rza_calc/gui/svg_scheme.py", "rza_calc/gui/svg_view.py"}
    missing = []
    for path in sorted(set(re.findall(r"`([a-zA-Z0-9_./-]+\.(?:py|json|md))`", text))):
        if path in deleted or "<" in path:
            continue
        if not (ROOT / path).exists():
            missing.append(path)
    assert not missing, "бриф ссылается на несуществующие файлы:\n  " + "\n  ".join(missing)


def test_baseline_state_in_status_matches_reality():
    """Статус эталона обязан описывать тот эталон, который лежит в репозитории."""
    data = status()["baseline"]
    baseline_path = ROOT / data["file"]
    assert baseline_path.exists(), f"эталон {data['file']} отсутствует"
    if not data.get("frozen"):
        return
    snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))
    covers = data["covers"]
    faults = sum(
        len(rows)
        for project in snapshot["projects"].values()
        for rows in project["fault_currents"].values()
    )
    protections = sum(len(p["protections"]) for p in snapshot["projects"].values())
    assert faults == covers["fault_points"], (faults, covers["fault_points"])
    assert protections == covers["protections"], (protections, covers["protections"])
