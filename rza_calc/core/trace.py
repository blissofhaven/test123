# -*- coding: utf-8 -*-
"""
Протокол расчёта.

Ключевая идея всей программы: расчётная функция возвращает НЕ число,
а объект Value, который несёт значение + полную запись того, как оно получено.

Из этой же записи собирается:
  * всплывающее окно по кнопке "?" в интерфейсе;
  * пояснительная записка в Word;
  * лист "Расчёт" в Excel.

Один механизм закрывает три задачи, поэтому он в ядре, а не в GUI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


def fmt(x: float, digits: int = 3) -> str:
    """Аккуратное представление числа для протокола."""
    if x is None:
        return "—"
    if isinstance(x, complex):
        return f"{x.real:.{digits}f} + j{x.imag:.{digits}f}"
    ax = abs(x)
    if ax != 0 and (ax >= 1e6 or ax < 1e-4):
        return f"{x:.{digits}e}".replace("e", "·10^")
    if ax >= 100:
        return f"{x:.1f}"
    if ax >= 10:
        return f"{x:.2f}"
    return f"{x:.{digits}f}"


@dataclass
class Step:
    """Один шаг расчёта — ровно те разделы, что показываются по кнопке '?'."""
    what: str                                   # ЧТО СЧИТАЕМ
    why: str = ""                               # ЗАЧЕМ
    given: dict[str, str] = field(default_factory=dict)   # ИСХОДНЫЕ ДАННЫЕ
    formula: str = ""                           # ФОРМУЛА
    substitution: str = ""                      # ПОДСТАНОВКА
    result: str = ""                            # РЕЗУЛЬТАТ
    accepted: str | None = None                 # ПРИНЯТО (после округления/выбора уставки)
    source: str | None = None                   # откуда взяты коэффициенты/методика
    note: str | None = None                     # предупреждение, допущение, оговорка
    children: list["Step"] = field(default_factory=list)  # вложенные расчёты

    def render(self, indent: int = 0, width: int = 62) -> str:
        pad = " " * indent
        out: list[str] = []
        out.append(pad + "─" * width)
        out.append(pad + "ЧТО СЧИТАЕМ")
        out.append(pad + "  " + self.what)
        if self.why:
            out.append(pad + "ЗАЧЕМ")
            for line in _wrap(self.why, width - 2):
                out.append(pad + "  " + line)
        if self.given:
            out.append(pad + "ИСХОДНЫЕ ДАННЫЕ")
            k = max(len(x) for x in self.given) if self.given else 0
            for name, val in self.given.items():
                out.append(pad + f"  {name:<{k}} = {val}")
        if self.formula:
            out.append(pad + "ФОРМУЛА")
            out.append(pad + "  " + self.formula)
        if self.substitution:
            out.append(pad + "ПОДСТАНОВКА")
            out.append(pad + "  " + self.substitution)
        if self.result:
            out.append(pad + "РЕЗУЛЬТАТ")
            out.append(pad + "  " + self.result)
        if self.accepted:
            out.append(pad + "ПРИНЯТО")
            out.append(pad + "  " + self.accepted)
        if self.source:
            out.append(pad + "ИСТОЧНИК")
            out.append(pad + "  " + self.source)
        if self.note:
            out.append(pad + "ПРИМЕЧАНИЕ")
            for line in _wrap(self.note, width - 2):
                out.append(pad + "  " + line)
        for ch in self.children:
            out.append("")
            out.append(pad + "  ← промежуточный расчёт:")
            out.append(ch.render(indent + 4, width - 4))
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "children" and v}
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines or [""]


@dataclass
class Value:
    """Число вместе с его происхождением."""
    value: float
    unit: str = ""
    label: str = ""
    step: Step | None = None

    # --- арифметика: Value ведёт себя как число там, где это удобно ---
    def __float__(self) -> float:
        return float(self.value)

    def __add__(self, o): return self.value + float(o)
    def __radd__(self, o): return float(o) + self.value
    def __sub__(self, o): return self.value - float(o)
    def __rsub__(self, o): return float(o) - self.value
    def __mul__(self, o): return self.value * float(o)
    def __rmul__(self, o): return float(o) * self.value
    def __truediv__(self, o): return self.value / float(o)
    def __rtruediv__(self, o): return float(o) / self.value
    def __lt__(self, o): return self.value < float(o)
    def __le__(self, o): return self.value <= float(o)
    def __gt__(self, o): return self.value > float(o)
    def __ge__(self, o): return self.value >= float(o)

    def __repr__(self) -> str:
        return f"{fmt(self.value)} {self.unit}".strip()

    def explain(self) -> str:
        """То, что показывается по кнопке '?'."""
        if self.step is None:
            return f"{self.label}: {self} — значение задано пользователем, расчёта нет."
        return self.step.render()


def record(what: str, value: float, unit: str = "", *, why: str = "",
           given: dict[str, str] | None = None, formula: str = "",
           substitution: str = "", accepted: str | None = None,
           source: str | None = None, note: str | None = None,
           children: Iterable[Value | Step] = ()) -> Value:
    """Создать Value со встроенным шагом протокола."""
    kids: list[Step] = []
    for c in children:
        if isinstance(c, Value) and c.step is not None:
            kids.append(c.step)
        elif isinstance(c, Step):
            kids.append(c)
    step = Step(
        what=what, why=why, given=given or {}, formula=formula,
        substitution=substitution, result=f"{what} = {fmt(value)} {unit}".strip(),
        accepted=accepted, source=source, note=note, children=kids,
    )
    return Value(value=value, unit=unit, label=what, step=step)


def given(label: str, value: float, unit: str = "") -> Value:
    """Исходное данное — без расчёта, но с подписью."""
    return Value(value=value, unit=unit, label=label, step=None)
