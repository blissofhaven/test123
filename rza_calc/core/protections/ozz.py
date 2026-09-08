# -*- coding: utf-8 -*-
"""Защита от однофазных замыканий на землю (ОЗЗ) по току нулевой последовательности."""
from __future__ import annotations

from ..context import Context
from ..model import Branch, LineBranch, Mode, Network, TransformerBranch
from .selection import (iter_in_declaration_order, pick_extreme,
                        tie_note)
from ..result import FAIL, OK, UNRESOLVED, Check, ProtectionResult
from ..trace import Step, fmt


def galvanic_group(net: Network, node_id: str, mode: Mode) -> set[str]:
    """
    Гальванически связанная часть сети: обход от узла без перехода через
    трансформаторы. Ёмкостный ток ОЗЗ считается именно в её пределах.
    """
    adj = net.adjacency(mode)
    seen, stack = {node_id}, [node_id]
    while stack:
        cur = stack.pop()
        for nxt, b in adj.get(cur, []):
            if isinstance(b, TransformerBranch) or nxt in seen or nxt == "GRID":
                continue
            seen.add(nxt)
            stack.append(nxt)
    return seen


def capacitive_current(net: Network, nodes: set[str], mode: Mode
                       ) -> tuple[float, list[str], list[str]]:
    """(Ic, расшифровка по линиям, линии без данных по ёмкостному току)."""
    total, detail, missing = 0.0, [], []
    for b in net.active_branches(mode):
        if not isinstance(b, LineBranch):
            continue
        if b.node_from not in nodes and b.node_to not in nodes:
            continue
        if b.ic_per_km is None:
            missing.append(b.name)
            continue
        ic = b.ic_per_km * b.length_km * max(1, b.n_parallel)
        total += ic
        detail.append(f"«{b.name}»: {fmt(b.ic_per_km)} А/км · {fmt(b.length_km)} км = {fmt(ic)} А")
    return total, detail, missing


def calc_ozz(ctx: Context, br: Branch) -> ProtectionResult:
    m, net = ctx.meth, ctx.net
    res = ProtectionResult(br.id, br.name, "ОЗЗ")

    if not br.has_protection_point:
        res.messages.append("На присоединении не задан ТТ — защита не рассчитывается.")
        return res
    if not isinstance(br, LineBranch):
        res.messages.append("Расчёт ОЗЗ в этой версии выполняется только для линий.")
        return res

    from ..parameter_validation import block_unconfirmed_parameters
    blocked = block_unconfirmed_parameters(ctx, br, "ОЗЗ")
    if blocked is not None:
        return blocked

    protection_node = net.protection_node_id(br)
    u_nom = net.node(protection_node).u_nom
    neutral = net.neutral_mode(u_nom)
    neutral_ru = net.neutral_mode_ru(u_nom)

    if neutral == "compensated":
        res.messages.append(
            f"Нейтраль {neutral_ru}. Уставка ОЗЗ по ёмкостному току здесь неприменима: "
            "остаточный ток зависит от настройки дугогасящего реактора и степени расстройки "
            "компенсации. Требуются данные ДГР — расчёт в этой версии не выполняется.")
        return res
    if neutral in ("earthed", "resistor"):
        res.messages.append(
            f"Нейтраль {neutral_ru}. Замыкание на землю здесь является КЗ, а не ОЗЗ; "
            "вместо токовой защиты нулевой последовательности по ёмкостному току требуется "
            "расчёт по току однофазного КЗ — в этой версии не реализован.")
        return res

    modes = ctx.modes_where_active(br)
    if not modes:
        res.messages.append("Присоединение не под напряжением ни в одном режиме.")
        return res

    # ── ёмкостные токи по каждому режиму ─────────────────────────────────
    # Уставка — по режиму с НАИБОЛЬШИМ собственным током (тяжелее для отстройки).
    # Чувствительность — по режиму с НАИМЕНЬШИМ током через защиту (тяжелее для Kч).
    # Это разные режимы, и брать один на оба условия нельзя.
    per_mode = []
    missing: list[str] = []
    for mode in iter_in_declaration_order(net, modes):
        group = galvanic_group(net, protection_node, mode)
        i_net, detail, miss = capacitive_current(net, group, mode)
        for x in miss:
            if x not in missing:
                missing.append(x)
        _, load_end, _ = net.orient(br, mode)
        own_nodes = net.downstream_nodes(br, mode) | {load_end}
        i_own, own_detail = 0.0, []
        for b in net.active_branches(mode):
            if not isinstance(b, LineBranch) or b.ic_per_km is None:
                continue
            if b.id == br.id or b.node_from in own_nodes or b.node_to in own_nodes:
                v = b.ic_per_km * b.length_km * max(1, b.n_parallel)
                i_own += v
                own_detail.append(f"«{b.name}»: {fmt(v)} А")
        per_mode.append((mode, i_net, i_own, detail, own_detail))

    if not per_mode:
        res.messages.append("Ни в одном режиме присоединение не под напряжением.")
        return res

    # Порядок объявления режимов разрешает равенство: ёмкостный ток фидера в
    # разных режимах часто совпадает до последнего бита, и строгий max выбирал
    # бы режим по шуму решателя, а не по существу.
    chosen, tied_own = pick_extreme(per_mode, key=lambda r: r[2], largest=True)
    mode, i_net, i_own, detail, own_detail = chosen
    worst, tied_worst = pick_extreme(
        per_mode, key=lambda r: r[1] - r[2], largest=False)
    own_tie = tie_note(tied_own, mode.name)
    worst_tie = tie_note(tied_worst, worst[0].name)

    if missing:
        res.messages.append(
            "Не задан удельный ёмкостный ток (ic_per_km) у линий: " + ", ".join(missing) +
            ". Ёмкостный ток сети занижен, уставка и чувствительность ОЗЗ недостоверны. "
            "Возьмите значения из каталога кабеля.")
    if i_net <= 0:
        res.messages.append("Ёмкостный ток сети не определён — уставку ОЗЗ рассчитать нельзя.")
        return res

    res.messages.append(
        "Удельные ёмкостные токи взяты из данных проекта. Убедитесь, что они получены "
        "из каталога применённого кабеля, а не приняты ориентировочно: уставка ОЗЗ "
        "линейно зависит от них.")
    k_ots, k_br = m.k("ozz.k_ots"), m.k("ozz.k_br")
    i_calc = k_ots * k_br * i_own

    st = Step(
        what=f"Ток срабатывания ОЗЗ присоединения «{br.name}»",
        why=("При замыкании на землю на ДРУГОМ присоединении через данное протекает его "
             "собственный ёмкостный ток. Защита должна быть от него отстроена, иначе "
             "отключит неповреждённый фидер."),
        given={"Режим нейтрали": neutral_ru,
               "Режим сети": mode.name
                            + ("" if own_tie is None else
                               " (и ещё режимы с тем же током)"),
               "Ёмкостный ток сети Ic": f"{fmt(i_net)} А  ({'; '.join(detail)})",
               "Собственный ёмкостный ток Ic.собств": f"{fmt(i_own)} А"
                                                     + (f"  ({'; '.join(own_detail)})" if own_detail else ""),
               "kотс": fmt(k_ots), "kбр": fmt(k_br)},
        formula="Iсз ≥ kотс · kбр · Ic.собств",
        substitution=f"Iсз ≥ {fmt(k_ots)} · {fmt(k_br)} · {fmt(i_own)} = {fmt(i_calc)} А",
        result=f"Iсз = {fmt(i_calc)} А (первичный ток 3I0)",
        source=m.cite("ozz.k_ots"),
        note=" ".join(filter(None, (
            "Уставка задаётся первичным током 3I0 — вторичная зависит от типа "
            "трансформатора тока нулевой последовательности (ТТНП), а не от фазных ТТ "
            "присоединения. ТТНП в модели пока не описывается.",
            own_tie,
        ))),
    )
    res.steps.append(st)
    res.i_calc = res.i_primary = i_calc
    res.t = br.prot.t_ozz if br.prot.t_ozz is not None else m.k("ozz.t")
    res.governing_mode = mode.name

    # ── чувствительность ──
    mode_s, i_net_s, i_own_s, _, _ = worst
    i_through = i_net_s - i_own_s   # ток при замыкании НА защищаемом присоединении
    kch = i_through / i_calc if i_calc else 0.0
    req = m.k("sensitivity.kch_ozz")
    cst = Step(
        what="Чувствительность ОЗЗ",
        why=("При замыкании на защищаемом присоединении через его защиту протекает "
             "ёмкостный ток ОСТАЛЬНОЙ сети — он и должен превышать уставку."),
        given={"Расчётный режим": mode_s.name
                                 + ("" if worst_tie is None else
                                    " (и ещё режимы с тем же током)"),
               "Ic сети": f"{fmt(i_net_s)} А", "Ic.собств": f"{fmt(i_own_s)} А",
               "Ток через защиту при КЗ в её зоне": f"{fmt(i_through)} А",
               "Iсз": f"{fmt(i_calc)} А"},
        formula="Kч = (Ic − Ic.собств) / Iсз",
        substitution=f"Kч = ({fmt(i_net_s)} − {fmt(i_own_s)}) / {fmt(i_calc)} = {fmt(kch)}",
        result=f"Kч = {fmt(kch)} (требуется ≥ {fmt(req)})",
        source=m.cite("sensitivity.kch_ozz"),
        note=" ".join(filter(None, (
            "Режим выбран как наихудший для чувствительности: в нём через защиту "
            "протекает наименьший ёмкостный ток остальной сети. Уставка при этом "
            f"принята по режиму «{mode.name}», где собственный ток наибольший.",
            worst_tie,
        ))),
    )
    res.steps.append(cst)
    status = OK if kch >= req else FAIL
    res.checks.append(Check("Kч ОЗЗ", kch, req, status,
                            "ёмкостный ток сети мал относительно собственного"
                            if status == FAIL else "", cst))
    if status == FAIL:
        res.messages.append(
            "Чувствительность не обеспечивается. Типовые пути: применить защиту "
            "направленного действия или защиту на высших гармониках, ввести выдержку "
            "времени и снизить kбр, либо пересмотреть деление сети. Выбор решения — "
            "за проектировщиком.")
    res.recompute_status()
    return res
