# -*- coding: utf-8 -*-
"""Расчётные рабочие токи присоединений."""
from __future__ import annotations

import math

from .methodology import Methodology
from .model import GRID, Branch, GeneratorBranch, Mode, Network, TransformerBranch
from .trace import Step, Value, fmt, record

SQRT3 = math.sqrt(3.0)


def external_current_record(net: Network, br: Branch, mode: Mode):
    """Only the exact recorded physical CT port may supply this scalar RMS A."""
    rows = mode.operating_parameters.get("working_currents", {})
    matches = [(pid, row) for pid, row in rows.items()
               if br.id in row.get("applies_to", ())]
    if len(matches) > 1:
        raise ValueError("Рабочий ток неоднозначен: несколько значений для физического ТТ.")
    return matches[0] if matches else None


def working_current(net: Network, br: Branch, mode: Mode, m: Methodology) -> Value:
    """
    Заданный подтверждённый первичный ток относится к конкретному порту ТТ.
    Без него допускается сумма нагрузок только радиального присоединения
    или прежняя явно названная номинальная отстройка генератора/трансформатора.
    Кольцевое и параллельное питание не заменяется суммой всей группы.
    """
    external = external_current_record(net, br, mode)
    if external is not None:
        port_id, row = external
        value = row.get("value")
        if (row.get("confirmation") != "confirmed" or not isinstance(row.get("source"), str)
                or not row["source"].strip() or isinstance(value, bool)
                or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0):
            raise ValueError("Заданный рабочий ток на физической стороне ТТ отсутствует или не подтверждён.")
        if row.get("node_id") != net.protection_node_id(br):
            raise ValueError("Заданный рабочий ток относится к прежней стороне ТТ.")
        return Value(float(value), "А", "Iраб.max", Step(
            what=f"Заданный рабочий ток присоединения «{br.name}»",
            why="Первичный действующий рабочий ток на физической стороне ТТ задан независимо от радиального суммирования нагрузки.",
            given={"Режим": mode.name, "Физический порт ТТ": port_id, "Источник": row["source"]},
            formula="Iраб.max = Iзаданный", result=f"Iраб.max = {fmt(value)} А",
            note="Скалярный действующий ток для отстройки МТЗ; фазные углы и потокораспределение этим вводом не рассчитываются."))
    from ..adapters.operating_parameters import network_for_mode
    effective = network_for_mode(net, mode)
    if effective is not net:
        net, br, mode = effective, effective.branches[br.id], effective.modes[mode.id]
    if isinstance(br, GeneratorBranch):
        s_gen = br.s_from_p
        i = s_gen / (SQRT3 * br.u_nom)
        step = Step(
            what=f"Номинальный ток генератора «{br.name}»",
            why=("Защита генератора отстраивается от его номинального тока: суммировать "
                 "нагрузку сети здесь бессмысленно, машина работает параллельно с другими."),
            given={"Sном": f"{fmt(s_gen)} кВ·А", "Uном": f"{fmt(br.u_nom)} кВ",
                   **({"Pном": f"{fmt(br.p_nom)} МВт при cosφ = {fmt(br.cos_phi)}"} if br.p_nom else {})},
            formula="Iном = Sном / (√3 · Uном)",
            substitution=f"Iном = {fmt(s_gen)} / (1.732 · {fmt(br.u_nom)}) = {fmt(i)} А",
            result=f"Iном = {fmt(i)} А",
            note=("Для генератора это условие отстройки, но не полный расчёт защиты: "
                  "МТЗ генератора обычно дополняется защитой от перегрузки, минимального "
                  "напряжения и обратной мощности — в этой версии они не считаются."),
        )
        return Value(i, "А", "Iном генератора", step)

    src_end, load_end, ring = net.orient(br, mode)
    if ring:
        raise ValueError("Кольцо или параллельное питание: задайте подтверждённый первичный рабочий ток на физической стороне ТТ; радиальная сумма нагрузок здесь неприменима.")
    # Рабочий ток всегда приводится к физической стороне ТТ, а не к стороне,
    # которая случайно оказалась питающей в данном режиме.
    u_nom = net.protection_side_u(br) if br.has_protection_point else (
        net.node(load_end if src_end == GRID else src_end).u_nom
    )
    loads = net.downstream_loads(br, mode)
    for load in loads:
        params = mode.operating_parameters
        for row in (params.get("load_factor"), params.get("load_factors", {}).get(load.id)):
            if row is not None and (row.get("value") is None or row.get("confirmation") != "confirmed"
                                     or not str(row.get("source", "")).strip()):
                raise ValueError("Коэффициент нагрузки «" + load.name + "» отсутствует или не подтверждён в данном режиме.")
    group_note = None
    if not loads:
        zone, n_par = net.group_zone(br, mode)
        if n_par > 1:
            raise ValueError("Параллельная группа: задайте подтверждённый рабочий ток на физической стороне ТТ; сумма всей нагрузки группы не определяет ток этой ветви.")

    if loads:
        s_total = sum(l.s_kva * l.k_use for l in loads)
        i = s_total / (SQRT3 * u_nom) if u_nom else 0.0
        detail = ";  ".join(
            f"«{l.name}»: {fmt(l.p_kw)} кВт / {fmt(l.cos_phi)}"
            + (f" · Kи {fmt(l.k_use)}" if l.k_use != 1 else "")
            + f" = {fmt(l.s_kva * l.k_use)} кВ·А" for l in loads)
        step = Step(
            what=f"Максимальный рабочий ток присоединения «{br.name}»",
            why=("От него отстраивается МТЗ. Ток берётся не «на глаз», а суммированием "
                 "всех нагрузок, которые в данном режиме питаются через это присоединение."),
            given={"Режим": mode.name, "Uном": f"{fmt(u_nom)} кВ",
                   "Нагрузки в зоне": detail, "ΣS": f"{fmt(s_total)} кВ·А"},
            formula="Iраб.max = ΣS / (√3 · Uном)",
            substitution=f"Iраб.max = {fmt(s_total)} / (1.732 · {fmt(u_nom)}) = {fmt(i)} А",
            result=f"Iраб.max = {fmt(i)} А",
            note=(group_note or
                  "Если по присоединению возможна перегрузка сверх суммы заданных нагрузок "
                  "(перевод питания, резервирование), задайте её отдельной нагрузкой в схеме "
                  "или отдельным режимом."),
        )
        return Value(i, "А", "Iраб.max", step)

    # Нагрузка не задана — для трансформатора безопасно принимаем номинальный
    # ток, но именно на стороне установленного ТТ.
    if isinstance(br, TransformerBranch):
        i = br.s_nom / (SQRT3 * u_nom)
        step = Step(
            what=f"Номинальный ток трансформатора «{br.name}» на стороне ТТ",
            why="Нагрузка в зоне не задана, поэтому за расчётный ток принят номинальный ток трансформатора.",
            given={"Sном": f"{fmt(br.s_nom)} кВ·А", "U стороны ТТ": f"{fmt(u_nom)} кВ"},
            formula="Iном = Sном / (√3 · UТТ)",
            substitution=f"Iном = {fmt(br.s_nom)} / (1.732 · {fmt(u_nom)}) = {fmt(i)} А",
            result=f"Iном = {fmt(i)} А",
            note="Это допущение. Задайте фактическую нагрузку — уставка изменится.",
        )
        return Value(i, "А", "Iном", step)

    step = Step(
        what=f"Максимальный рабочий ток присоединения «{br.name}»",
        why="Нужен для отстройки МТЗ.",
        result="не определён",
        note=("В зоне присоединения нет ни одной заданной нагрузки. Уставку МТЗ "
              "по условию отстройки от рабочего тока рассчитать нельзя."),
    )
    return Value(0.0, "А", "Iраб.max", step)


def nominal_current(s_kva: float, u_kv: float) -> float:
    return s_kva / (SQRT3 * u_kv)
