# -*- coding: utf-8 -*-
"""Максимальная токовая защита."""
from __future__ import annotations

from dataclasses import replace
import math

from ..context import Context
from ..load_current import working_current
from ..model import Branch, Mode
from .selection import (iter_in_declaration_order, pick_extreme,
                        tie_note)
from ..result import (FAIL, OK, UNRESOLVED, Check, ProtectionResult,
                      SettingRangeError, round_to_scale)
from ..trace import Step, fmt


def calc_mtz(ctx: Context, br: Branch) -> ProtectionResult:
    m, net = ctx.meth, ctx.net
    res = ProtectionResult(br.id, br.name, "МТЗ")

    if not br.has_protection_point:
        res.messages.append("На присоединении не задан ТТ — защита не рассчитывается.")
        return res

    modes, excluded, applicability_problems = ctx.protection_modes(br)
    res.messages.extend(excluded)
    res.record_coverage("Применимость режимов", len(modes) + len(excluded),
                        len(net.modes), applicability_problems)
    solver_problems = [
        f"Режим «{mode.name}» ({mode.id}): {ctx.errors.get(mode.id, 'решатель отсутствует')}"
        for mode in modes if mode.id not in ctx.solvers
    ]
    res.record_coverage("Расчётные режимы", len(modes) - len(solver_problems),
                        len(modes), solver_problems)
    if not modes:
        res.messages.append("Присоединение не находится под напряжением ни в одном из заданных режимов.")
        return res

    # ── 1. Рабочий ток: берём наихудший из ВСЕХ режимов ──────────────────
    ordered = iter_in_declaration_order(net, modes)
    per_mode = {}
    load_problems = []
    for mode in ordered:
        try:
            value = working_current(net, br, mode, m)
            if not math.isfinite(value.value) or value.value < 0:
                raise ValueError("рабочий ток должен быть конечным и неотрицательным")
            per_mode[mode.id] = value
        except Exception as exc:
            load_problems.append(f"Режим «{mode.name}» ({mode.id}): {exc}")
    res.record_coverage("Рабочий ток", len(per_mode), len(ordered), load_problems)
    available = [mode for mode in ordered if mode.id in per_mode]
    if not available:
        res.messages.append("Рабочий ток не рассчитан ни в одном применимом режиме.")
        return res
    # Равные рабочие токи разрешаются порядком объявления режимов, а не
    # последним битом решателя: иначе имя определяющего режима зависело бы от
    # сборки численной библиотеки на конкретной машине.
    worst, tied_rab = pick_extreme(
        available, key=lambda md: per_mode[md.id].value, largest=True)
    i_rab = per_mode[worst.id]
    res.governing_mode = worst.name
    rab_tie = tie_note(tied_rab, worst.name)
    if rab_tie:
        i_rab = replace(i_rab, step=replace(
            i_rab.step,
            note=(i_rab.step.note + " " if i_rab.step.note else "") + rab_tie))

    if i_rab.value <= 0:
        res.messages.append(
            "Максимальный рабочий ток не определён (в зоне присоединения нет заданных нагрузок). "
            "Уставку МТЗ по условию отстройки рассчитать нельзя.")
        res.steps.append(i_rab.step)
        return res

    if len(per_mode) > 1:
        res.messages.append(
            ("Рабочий ток проверен во всех применимых режимах; " if not load_problems else
             "Рабочий ток определён лишь в части применимых режимов; ")
            + "уставка принята по режиму «"
            + worst.name + "» как наиболее тяжёлому ("
            + ", ".join(f"{net.modes[k].name}: {fmt(v.value)} А" for k, v in per_mode.items()) + ").")

    # ── 2. Уставка по условию отстройки ──────────────────────────────────
    k_ots = m.k("mtz.k_ots")
    k_v = m.k("mtz.k_v")
    k_szp = br.prot.k_szp if br.prot.k_szp is not None else m.k("mtz.k_szp")
    i_calc = k_ots * k_szp / k_v * i_rab.value

    st = Step(
        what=f"Ток срабатывания МТЗ присоединения «{br.name}»",
        why=("Защита не должна работать в нормальном режиме и при самозапуске нагрузки "
             "после восстановления питания, но должна возвращаться после отключения "
             "внешнего КЗ."),
        given={"Iраб.max": f"{fmt(i_rab.value)} А (режим «{worst.name}»)",
               "kотс": fmt(k_ots), "kсзп": fmt(k_szp), "kв": fmt(k_v)},
        formula="Iсз = kотс · kсзп / kв · Iраб.max",
        substitution=(f"Iсз = {fmt(k_ots)} · {fmt(k_szp)} / {fmt(k_v)} · {fmt(i_rab.value)} "
                      f"= {fmt(i_calc)} А"),
        result=f"Iсз (расчётная) = {fmt(i_calc)} А",
        source=m.cite("mtz.k_ots"),
        children=[i_rab.step] if i_rab.step else [],
    )

    # ── 3. Приведение к шкале терминала ──────────────────────────────────
    scale = br.prot.i_scale or m.ct_scale()
    try:
        i_prim, i_sec, note = round_to_scale(
            i_calc, br.ct_k, scale, m.data["ct"].get("round_mode", "up")
        )
    except SettingRangeError as exc:
        res.i_calc = i_calc
        res.t = br.prot.t_mtz
        res.steps.append(st)
        res.messages.append(str(exc))
        res.checks.append(Check(
            "Диапазон токовой уставки терминала", i_calc / br.ct_k, None,
            FAIL, str(exc), st,
        ))
        res.recompute_status()
        return res
    st.accepted = (f"Iсз перв. = {fmt(i_prim)} А;  ТТ {fmt(br.ct_ratio[0])}/{fmt(br.ct_ratio[1])} "
                   f"(nт = {fmt(br.ct_k)});  Iсз втор. = {fmt(i_sec)} А  —  {note}")
    res.steps.append(st)
    res.i_calc, res.i_primary, res.i_secondary = i_calc, i_prim, i_sec
    res.t = br.prot.t_mtz

    # ── 4. Чувствительность: минимальный ток КЗ из ВСЕХ режимов ──────────
    for zone_name, points, req_key in (
        ("основной зоне", 0, "sensitivity.kch_mtz_main"),
        ("зоне резервирования", 1, "sensitivity.kch_mtz_backup"),
    ):
        rows: list[tuple] = []   # (ток, режим, точка, решатель, доля)
        non_radial = []
        required = 0
        problems = []
        for mode in ordered:
            label = f"режим «{mode.name}» ({mode.id})"
            try:
                pts = ctx.zone_points(br, mode)[points]
            except Exception as exc:
                required += 1
                problems.append(f"{label}: точки зоны не определены: {exc}")
                continue
            if not pts:
                if points == 0:
                    required += 1
                    problems.append(f"{label}: нет точек КЗ основной зоны")
                else:
                    res.messages.append(f"{label}: в модели нет применимых точек зоны резервирования.")
                continue
            required += len(pts)
            for fp in pts:
                try:
                    if mode.id not in ctx.solvers:
                        raise ValueError(ctx.errors.get(mode.id, "решатель отсутствует"))
                    i, sc, d = ctx.current_through(mode, br, fp.node_id, "i2")
                    if not math.isfinite(i) or i < 0 or not math.isfinite(d) or d < 0:
                        raise ValueError("ток и доля тока должны быть конечными и неотрицательными")
                except Exception as e:
                    message = f"Точка «{fp.name}», {label}: {e}"
                    res.messages.append(message)
                    problems.append(message)
                    continue
                if d < 0.999:
                    non_radial.append(f"{fp.name} / {mode.name}: {d*100:.0f} %")
                rows.append((i, mode, fp, sc, d))
        res.record_coverage(f"Чувствительность в {zone_name}", len(rows), required, problems)
        best, sens_tied = pick_extreme(rows, key=lambda r: r[0], largest=False)
        if best is None:
            if points == 1 and not problems:
                if ctx.backup_zone_stops_at_transformer(br, modes[0]):
                    res.messages.append(
                        "Зона резервирования ограничена трансформатором: всё, что ниже, "
                        "находится за ним, и по методике "
                        "(sensitivity.backup_through_transformer = 0) в зону не входит. "
                        "Резервирование этих элементов обеспечивают их собственные "
                        "защиты, а не эта.")
                else:
                    res.messages.append(
                        "Зона резервирования отсутствует: ниже данного присоединения в модели "
                        "нет других элементов. Если фактически они есть — опишите их в схеме, "
                        "иначе резервирование не проверено.")
            elif points == 0:
                res.checks.append(Check(f"Kч в {zone_name}", None, m.k(req_key), UNRESOLVED,
                                        "нет точек КЗ для проверки"))
            continue
        i_min, mode, fp, sc, d_min = best
        sens_tie = tie_note(sens_tied, mode.name)
        kch = i_min / i_prim
        req = m.k(req_key)
        status = OK if kch >= req else FAIL
        cstep = Step(
            what=f"Коэффициент чувствительности МТЗ в {zone_name}",
            why="Защита обязана надёжно срабатывать при минимальном токе повреждения в своей зоне.",
            given={"Расчётная точка": f"{fp.name}, режим «{mode.name}»"
                   + ("" if sens_tie is None else " (и ещё режимы с тем же током)"),
                   "Доля тока через защиту": f"{d_min*100:.0f} % полного тока КЗ",
                   "Iк(2)min через защиту": f"{fmt(i_min)} А",
                   "Iсз перв.": f"{fmt(i_prim)} А"},
            formula="Kч = Iк(2)min / Iсз",
            substitution=f"Kч = {fmt(i_min)} / {fmt(i_prim)} = {fmt(kch)}",
            result=f"Kч = {fmt(kch)}  (требуется ≥ {fmt(req)})",
            source=m.cite(req_key),
            note=(("Минимум взят по всем применимым режимам."
                   if not problems else
                   "Минимум взят только по рассчитанной части обязательных случаев; "
                   "полная чувствительность не подтверждена.")
                  + (" Точка питается не только через это присоединение, поэтому ток "
                     "через защиту меньше полного тока КЗ; доля посчитана по "
                     "токораспределению: " + "; ".join(non_radial[:3]) + "."
                     if non_radial else "")
                  + ("" if sens_tie is None else " " + sens_tie)),
            children=sc.step_for("i2"),
        )
        res.steps.append(cstep)
        res.checks.append(Check(f"Kч в {zone_name}", kch, req, status,
                                f"по «{fp.name}», режим «{mode.name}»", cstep))

    res.recompute_status()
    return res
