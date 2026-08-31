# -*- coding: utf-8 -*-
"""
Селективность по всей цепочке.

Программа не спрашивает у пользователя, кто у кого вышестоящий: она обходит
граф и определяет это сама — отдельно ДЛЯ КАЖДОГО РЕЖИМА. Это принципиально:
при включённом СВ вышестоящей для фидера 1 секции может оказаться совсем
другая защита, и ступень, честная в нормальном режиме, там нарушается.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .context import Context
from .model import Branch, Mode
from .result import FAIL, OK, UNRESOLVED, Check, ProtectionResult
from .trace import Step, fmt


@dataclass
class Pair:
    lower_id: str
    lower_name: str
    upper_id: str
    upper_name: str
    mode_id: str
    mode_name: str
    t_lower: float | None
    t_upper: float | None
    dt: float | None
    required: float
    status: str

    def line(self) -> str:
        mark = {OK: "✓", FAIL: "✗", UNRESOLVED: "?"}[self.status]
        d = fmt(self.dt) + " с" if self.dt is not None else "—"
        return (f"{mark} {self.lower_name} → {self.upper_name}"
                f"   Δt = {d}   (требуется ≥ {fmt(self.required)} с)"
                f"   [режим «{self.mode_name}»]")


def hierarchy(ctx: Context) -> dict[str, dict[str, str | None]]:
    """mode_id → {branch_id защиты: branch_id ближайшей вышестоящей защиты}."""
    out: dict[str, dict[str, str | None]] = {}
    for mode in ctx.modes:
        h: dict[str, str | None] = {}
        for br in ctx.net.protection_points(mode, "mtz"):
            up = ctx.net.upstream_branch(br, mode, "mtz")
            h[br.id] = up.id if up else None
        out[mode.id] = h
    return out


def assign_times(ctx: Context, results: dict[str, dict[str, ProtectionResult]]) -> list[Step]:
    """
    Назначить выдержки МТЗ снизу вверх: t(вышестоящей) = max t(нижестоящих) + Δt.
    Заданные вручную выдержки не трогаются.
    Возвращает протокол назначения.
    """
    net, m = ctx.net, ctx.meth
    dt = m.k("selectivity.dt")
    h = hierarchy(ctx)

    # объединённая иерархия по всем режимам: у защиты может быть несколько «верхних»
    children: dict[str, set[str]] = {}
    for mh in h.values():
        for low, up in mh.items():
            if up:
                children.setdefault(up, set()).add(low)

    steps: list[Step] = []
    times: dict[str, float] = {}

    class HierarchyConflict(Exception):
        def __init__(self, branch_ids: set[str]):
            self.branch_ids = branch_ids

    def resolve(bid: str, seen: set[str]) -> float:
        if bid in times:
            return times[bid]
        if bid in seen:
            raise HierarchyConflict(seen | {bid})
        seen = seen | {bid}
        br = net.branches[bid]
        fixed = br.prot.t_mtz
        kids = sorted(children.get(bid, set()))
        try:
            kid_times = [(k, resolve(k, seen)) for k in kids]
        except HierarchyConflict as conflict:
            raise HierarchyConflict(conflict.branch_ids | {bid}) from None
        if fixed is not None:
            times[bid] = fixed
            if kid_times:
                steps.append(Step(
                    what=f"Выдержка времени МТЗ «{br.name}»",
                    why="Выдержка задана пользователем; программа её не изменяет, только проверяет.",
                    given={"t (задано)": f"{fmt(fixed)} с"} |
                          {f"t нижестоящей «{net.branches[k].name}»": f"{fmt(v)} с" for k, v in kid_times},
                    result=f"t = {fmt(fixed)} с",
                ))
            return fixed
        if not kid_times:
            t = m.k("mtz.t_step_default")
            times[bid] = t
            steps.append(Step(
                what=f"Выдержка времени МТЗ «{br.name}»",
                why="Нижний уровень цепочки — ниже нет защит, с которыми нужно согласовываться.",
                given={"t по умолчанию из профиля методики": f"{fmt(t)} с"},
                result=f"t = {fmt(t)} с",
                source=m.cite("mtz.t_step_default"),
            ))
            return t
        base_id, base_t = max(kid_times, key=lambda kv: kv[1])
        t = base_t + dt
        times[bid] = t
        steps.append(Step(
            what=f"Выдержка времени МТЗ «{br.name}»",
            why="Согласование с самой медленной из нижестоящих защит.",
            given={f"t «{net.branches[k].name}»": f"{fmt(v)} с" for k, v in kid_times} |
                  {"Ступень селективности Δt": f"{fmt(dt)} с"},
            formula="t = max(t нижестоящих) + Δt",
            substitution=f"t = {fmt(base_t)} (по «{net.branches[base_id].name}») + {fmt(dt)} = {fmt(t)} с",
            result=f"t = {fmt(t)} с",
            source=m.cite("selectivity.dt"),
        ))
        return t

    all_ids = {bid for mh in h.values() for bid in mh}
    conflicts: set[str] = set()
    for bid in sorted(all_ids):
        try:
            resolve(bid, set())
        except HierarchyConflict as conflict:
            conflicts.update(conflict.branch_ids)

    if conflicts:
        names = ", ".join(net.branches[bid].name for bid in sorted(conflicts))
        message = (
            "Направление селективности меняется между режимами. Для цепочки "
            f"{names} единый комплект ненаправленных автоматических выдержек "
            "назначить нельзя; требуются направленные защиты, разные группы уставок "
            "или изменение режимов."
        )
        steps.append(Step(
            what="Проверка направления селективности между режимами",
            why="В разных режимах одна и та же защита не может быть одновременно выше и ниже другой.",
            result="Автоматическое назначение выдержек не выполнено",
            note=message,
        ))
        for bid in conflicts:
            result = results.get(bid, {}).get("МТЗ")
            if result is None:
                continue
            result.messages.append(message)
            result.checks.append(Check(
                "Направление селективности", None, None, UNRESOLVED, message
            ))
            result.t = net.branches[bid].prot.t_mtz
            result.recompute_status()

    for bid, t in times.items():
        r = results.get(bid, {}).get("МТЗ")
        if r is not None:
            r.t = t
    return steps


def check(ctx: Context, results: dict[str, dict[str, ProtectionResult]]) -> list[Pair]:
    """Проверка ступени селективности между каждой парой смежных защит, во всех режимах."""
    net, m = ctx.net, ctx.meth
    dt_req = m.k("selectivity.dt")
    pairs: list[Pair] = []
    for mode in ctx.modes:
        for br in net.protection_points(mode, "mtz"):
            up = net.upstream_branch(br, mode, "mtz")
            if up is None:
                continue
            lo = results.get(br.id, {}).get("МТЗ")
            hi = results.get(up.id, {}).get("МТЗ")
            t_lo = lo.t if lo else None
            t_hi = hi.t if hi else None
            if t_lo is None or t_hi is None:
                status, d = UNRESOLVED, None
            else:
                d = t_hi - t_lo
                status = OK if d >= dt_req - 1e-9 else FAIL
            pairs.append(Pair(br.id, br.name, up.id, up.name, mode.id, mode.name,
                              t_lo, t_hi, d, dt_req, status))
    return pairs


def violations(pairs: list[Pair]) -> list[Pair]:
    return [p for p in pairs if p.status == FAIL]


def report(ctx: Context, pairs: list[Pair]) -> str:
    out: list[str] = []
    by_mode: dict[str, list[Pair]] = {}
    for p in pairs:
        by_mode.setdefault(p.mode_name, []).append(p)
    for mode_name, ps in by_mode.items():
        out.append(f"Режим «{mode_name}»")
        for p in ps:
            out.append("   " + p.line())
        out.append("")
    bad = violations(pairs)
    if bad:
        out.append("⚠ НАРУШЕНИЯ СЕЛЕКТИВНОСТИ")
        for p in bad:
            out.append(f"   {p.lower_name}: МТЗ = {fmt(p.t_lower)} с")
            out.append(f"   {p.upper_name}: МТЗ = {fmt(p.t_upper)} с")
            out.append(f"   Δt = {fmt(p.dt)} с, требуется ≥ {fmt(p.required)} с "
                       f"(режим «{p.mode_name}»)")
            out.append("")
    else:
        out.append("Нарушений ступени селективности не обнаружено "
                   "во всех заданных режимах.")
    return "\n".join(out)


def time_map(ctx: Context, results: dict[str, dict[str, ProtectionResult]],
             mode: Mode, width: int = 46, t_max: float | None = None) -> str:
    """Карта селективности в текстовом виде: полоса времени на каждую защиту."""
    net = ctx.net
    rows: list[tuple[int, str, float | None, float | None]] = []
    h = hierarchy(ctx)[mode.id]

    def depth(bid: str, seen=()) -> int:
        up = h.get(bid)
        if up is None or up in seen:
            return 0
        return 1 + depth(up, tuple(seen) + (bid,))

    for br in net.protection_points(mode, "mtz"):
        r = results.get(br.id, {})
        rows.append((depth(br.id), br.name,
                     r.get("ТО").t if r.get("ТО") and r["ТО"].i_primary else None,
                     r.get("МТЗ").t if r.get("МТЗ") else None))
    if not rows:
        return "Нет защит для построения карты."
    rows.sort(key=lambda x: (-x[0], x[1]))
    tm = t_max or max([t for _, _, a, b in rows for t in (a, b) if t is not None] + [1.0])
    tm = tm * 1.15
    name_w = max(len(n) for _, n, _, _ in rows) + 1

    out = [f"КАРТА СЕЛЕКТИВНОСТИ — режим «{mode.name}»", ""]
    for _, name, t_to, t_mtz in rows:
        bar = [" "] * width
        for t, ch in ((t_to, "─"), (t_mtz, "═")):
            if t is None:
                continue
            end = max(1, int(round(t / tm * (width - 1))))
            for i in range(0, end + 1):
                if bar[i] == " ":
                    bar[i] = ch
            bar[end] = "│"
        label = []
        if t_to is not None:
            label.append(f"ТО {fmt(t_to)} с")
        if t_mtz is not None:
            label.append(f"МТЗ {fmt(t_mtz)} с")
        out.append(f"{name:<{name_w}}|{''.join(bar)}  {', '.join(label)}")
    out.append(" " * name_w + "+" + "-" * width)
    out.append(" " * name_w + f" 0{' ' * (width - 8)}{fmt(tm)} с")
    out.append("")
    out.append("  ─── ТО     ═══ МТЗ")
    return "\n".join(out)
