# -*- coding: utf-8 -*-
"""Обновление эталона расчётных чисел — только явной командой.

Эталон защищает уставки от незаметного сдвига. Поэтому обновление сделано
нарочно неудобным:

* команда не запускается автоматически ни из тестов, ни из сборки;
* без указания этапа и причины не работает;
* перед записью печатает полный отчёт о том, что именно изменится;
* требует подтверждения;
* пишет запись в ``docs/calculation-audit/baseline-log.md``.

Использование::

    python tools/update_baseline.py --stage A5 --reason "перенос ТТ в объект" \\
        --evidence "контрольный пример 6, ручной расчёт в о.е."

Без ``--yes`` показывает отчёт и останавливается — это штатный способ
посмотреть, что изменилось, ничего не меняя.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timezone, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import baseline_snapshot as snapshot  # noqa: E402

LOG_PATH = ROOT / "docs" / "calculation-audit" / "baseline-log.md"

LOG_HEADER = """# Журнал эталона расчётных чисел

Каждая запись отвечает на четыре вопроса: **когда**, **каким этапом**,
**почему** изменились числа и **чем подтверждено** новое значение.

Правило: эталон обновляется только командой `tools/update_baseline.py`.
Обновление без записи в этом журнале — нарушение правил дорожной карты
(`docs/roadmap/collaboration.md`, п. 5).

---
"""


def summarize(differences) -> str:
    """Краткая сводка для журнала: сколько и где изменилось."""
    if not differences:
        return "Числа не изменились (пересобран только формат снимка)."
    buckets: dict[str, int] = {}
    worst = None
    for item in differences:
        parts = item.path.split("/")
        area = parts[1] if len(parts) > 1 else parts[0]
        buckets[area] = buckets.get(area, 0) + 1
        percent = item.relative_percent
        if percent is not None and (worst is None or percent > worst[0]):
            worst = (percent, item.path)
    rows = ", ".join(f"{area}: {count}" for area, count in sorted(buckets.items()))
    text = f"Изменено значений: {len(differences)} ({rows})."
    if worst is not None:
        text += f" Наибольшее относительное отклонение {worst[0]:.4g} % — `{worst[1]}`."
    return text


def append_log(stage: str, reason: str, evidence: str, summary: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        LOG_PATH.write_text(LOG_HEADER, encoding="utf-8")
    stamp = datetime.now(timezone.utc).date().isoformat()
    entry = (
        f"\n## {stamp} · этап {stage}\n\n"
        f"**Причина.** {reason}\n\n"
        f"**Что изменилось.** {summary}\n\n"
        f"**Чем подтверждено новое значение.** {evidence}\n"
    )
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Обновить эталон расчётных чисел (только вручную)."
    )
    parser.add_argument("--stage", required=True,
                        help="Идентификатор этапа, в рамках которого числа изменились (A5, R2, …)")
    parser.add_argument("--reason", required=True,
                        help="Почему числа изменились — по существу, а не «правки»")
    parser.add_argument("--evidence", required=True,
                        help="Чем подтверждено новое значение: ручной расчёт, контрольный пример, эталонная программа")
    parser.add_argument("--yes", action="store_true",
                        help="Подтвердить запись. Без него команда только показывает отчёт")
    parser.add_argument("--limit", type=int, default=60,
                        help="Сколько расхождений печатать (по умолчанию 60)")
    args = parser.parse_args(argv)

    for name, value in (("--stage", args.stage), ("--reason", args.reason),
                        ("--evidence", args.evidence)):
        if not value.strip():
            parser.error(f"{name} не может быть пустым")

    print("Пересчитываю все демо-проекты…")
    actual = snapshot.build_all()

    if snapshot.BASELINE_PATH.exists():
        expected = snapshot.load_baseline()
        differences = snapshot.compare(expected.get("projects", {}), actual["projects"])
    else:
        expected, differences = None, []
        print("Эталона ещё нет — будет создан впервые.")

    print()
    print(snapshot.format_report(differences, limit=args.limit))
    print()

    summary = summarize(differences)
    print("Сводка для журнала:", summary)
    print(f"Этап: {args.stage}")
    print(f"Причина: {args.reason}")
    print(f"Подтверждение: {args.evidence}")
    print()

    if not differences and expected is not None:
        print("Числа не изменились — обновлять нечего. Эталон оставлен как есть.")
        return 0

    if not args.yes:
        print("Запись НЕ выполнена: добавьте --yes, если изменения действительно "
              "ожидаемы и объяснены.")
        return 1

    snapshot.BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot.BASELINE_PATH.write_text(snapshot.dumps(actual), encoding="utf-8")
    append_log(args.stage, args.reason, args.evidence, summary)
    print(f"Эталон записан: {snapshot.BASELINE_PATH}")
    print(f"Журнал дополнен: {LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
