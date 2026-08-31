# -*- coding: utf-8 -*-
"""Единый формат результата по одной защите."""
from __future__ import annotations

from dataclasses import dataclass, field

from .trace import Step, fmt

OK, FAIL, UNRESOLVED = "ok", "fail", "unresolved"

MARK = {OK: "✓", FAIL: "✗", UNRESOLVED: "?"}


class SettingRangeError(ValueError):
    """Расчётную уставку невозможно выставить на выбранном терминале."""


@dataclass
class Check:
    name: str
    value: float | None
    required: float | None
    status: str
    comment: str = ""
    step: Step | None = None

    def line(self) -> str:
        v = fmt(self.value) if self.value is not None else "—"
        r = f" (треб. ≥ {fmt(self.required)})" if self.required is not None else ""
        return f"{MARK[self.status]} {self.name}: {v}{r}" + (f" — {self.comment}" if self.comment else "")


@dataclass
class ProtectionResult:
    branch_id: str
    branch_name: str
    kind: str                     # 'МТЗ' | 'ТО' | 'ОЗЗ'
    i_primary: float | None = None      # А, принятая первичная уставка
    i_secondary: float | None = None    # А, вторичная
    i_calc: float | None = None         # А, расчётная до округления
    t: float | None = None              # с
    checks: list[Check] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    governing_mode: str = ""            # режим, определивший уставку
    status: str = UNRESOLVED

    def recompute_status(self) -> None:
        if any(c.status == FAIL for c in self.checks):
            self.status = FAIL
        elif any(c.status == UNRESOLVED for c in self.checks) or self.i_primary is None:
            self.status = UNRESOLVED
        else:
            self.status = OK

    def explain(self) -> str:
        out = [f"{self.branch_name} — {self.kind}", "=" * 62]
        for s in self.steps:
            out.append(s.render())
            out.append("")
        if self.checks:
            out.append("ПРОВЕРКИ")
            for c in self.checks:
                out.append("  " + c.line())
        if self.messages:
            out.append("ЗАМЕЧАНИЯ")
            for msg in self.messages:
                out.append("  • " + msg)
        return "\n".join(out)


def round_to_scale(i_primary: float, ct_k: float, scale: list[float] | None,
                   mode: str = "up") -> tuple[float, float, str]:
    """
    Привести первичную уставку к шкале вторичных уставок терминала.
    Возвращает (принятая первичная, вторичная, пояснение).
    """
    i_sec = i_primary / ct_k
    if not scale:
        return i_primary, i_sec, "шкала уставок терминала не задана — округление не выполнялось"
    higher = [s for s in scale if s >= i_sec - 1e-9]
    if not higher:
        raise SettingRangeError(
            f"расчётная вторичная уставка {fmt(i_sec)} А выходит за верх шкалы терминала "
            f"({fmt(max(scale))} А) — требуется другой коэффициент ТТ или другой терминал")
    if mode == "up":
        chosen = min(higher)
        note = f"вторичная уставка округлена вверх по шкале терминала до {fmt(chosen)} А"
    else:
        chosen = min(scale, key=lambda s: abs(s - i_sec))
        direction = "вверх" if chosen >= i_sec else "вниз"
        note = (f"вторичная уставка округлена {direction} до ближайшего значения "
                f"шкалы терминала: {fmt(chosen)} А")
    return chosen * ct_k, chosen, note
