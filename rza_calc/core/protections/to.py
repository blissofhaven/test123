# -*- coding: utf-8 -*-
"""Токовая отсечка."""
from __future__ import annotations

import math

from ..context import Context
from ..impedance import belongs_to_stage, line_impedance, stage_voltage
from ..load_current import nominal_current
from ..model import (GRID, Branch, GeneratorBranch, LineBranch, Mode,
                     TieBranch, TransformerBranch)
from .selection import (ABSOLUTE_BAND, RELATIVE_BAND, iter_in_declaration_order,
                        pick_extreme, tie_note)
from ..result import (FAIL, OK, UNRESOLVED, Check, ProtectionResult,
                      SettingRangeError, round_to_scale)
from ..trace import Step, fmt

SQRT3 = math.sqrt(3.0)


def calc_to(ctx: Context, br: Branch) -> ProtectionResult:
    m, net = ctx.meth, ctx.net
    res = ProtectionResult(br.id, br.name, "ТО")

    if not br.has_protection_point:
        res.messages.append("На присоединении не задан ТТ — защита не рассчитывается.")
        return res
    if isinstance(br, GeneratorBranch):
        res.messages.append(
            "Токовая отсечка генератора в этой версии не рассчитывается: она отстраивается "
            "от тока внешнего КЗ с учётом затухания подпитки и обычно вытесняется "
            "продольной дифференциальной защитой. Требуется отдельная методика.")
        return res
    from ..parameter_validation import block_unconfirmed_parameters
    blocked = block_unconfirmed_parameters(ctx, br, "ТО")
    if blocked is not None:
        return blocked

    modes, excluded, mode_problems = ctx.protection_modes(br)
    modes = iter_in_declaration_order(net, modes)
    res.messages.extend(excluded)
    res.record_coverage("Применимость режимов ТО", len(modes),
                        len(modes) + len(mode_problems), mode_problems)
    if not modes:
        res.messages.append("Не удалось определить применимые режимы ТО." if mode_problems else
                            "Присоединение не под напряжением ни в одном режиме.")
        return res

    u_pr = ctx.protection_side_u(br, modes[0])          # класс — для подписей и Iном
    u_stage_pr = ctx.protection_side_stage(br)          # ступень — для приведения токов
    conditions: list[tuple[float, str, Step, Mode]] = []

    # ── условие 1: отстройка от КЗ за пределами зоны мгновенного действия ──
    reach = br.prot.to_reach
    rows = []
    ext_required = ext_checked = 0
    ext_problems: list[str] = []
    inrush_candidates: dict[str, list[TransformerBranch]] = {}
    inrush_search_problems: list[str] = []
    for mode in modes:
        label = f"режим «{mode.name}» ({mode.id})"
        try:
            _, load_end, _ = net.orient(br, mode)
            candidates = ([br] if isinstance(br, TransformerBranch) else
                          _transformers_at(ctx, br, mode)
                          if reach == "behind_transformer" else [])
            inrush_candidates[mode.id] = candidates
            targets = [(load_end, f"на «{net.node(load_end).name}»")]
            target_known = True
            if reach == "behind_transformer" and not isinstance(br, TransformerBranch):
                if candidates:
                    targets = [(net.orient(tr, mode)[1], f"за трансформатором «{tr.name}»")
                               for tr in candidates]
                else:
                    target_known = False
                    ext_problems.append(
                        f"{label}: за концом присоединения нет включённого трансформатора; "
                        "КЗ в конце самого присоединения даёт только предварительное условие.")
            # Electrically coincident fault points are one physical case per mode.
            point = ctx._electrical_point(mode)
            unique_targets = {}
            for node, kind in targets:
                unique_targets.setdefault(point(node), (node, kind))
            targets = list(unique_targets.values())
        except Exception as exc:
            ext_required += 1
            ext_problems.append(f"{label}: зона внешнего КЗ не определена: {exc}")
            if isinstance(br, TransformerBranch) or reach == "behind_transformer":
                inrush_search_problems.append(f"{label}: трансформатор в зоне не определён: {exc}")
            continue
        for target_node, kind_ru in targets:
            ext_required += 1
            try:
                _require_solver(ctx, mode)
                i, sc, d = ctx.current_through(mode, br, target_node, "i3")
                _finite_current(i)
            except Exception as exc:
                ext_problems.append(f"Точка «{net.node(target_node).name}», {label}: {exc}")
                continue
            ext_checked += int(target_known)
            rows.append((i, mode, sc, d, kind_ru))
    res.record_coverage("Отстройка ТО от внешнего КЗ", ext_checked, ext_required, ext_problems)
    best, tied = pick_extreme(rows, key=lambda r: r[0], largest=True)
    if best is None:
        res.messages.append("Не удалось определить ток КЗ для отстройки.")
    else:
        i_ext, mode_ext, sc_ext, d_ext, kind_ru = best
        tie = _mode_tie_note(rows, tied, mode_ext.name)
        k_ots = m.k("to.k_ots")
        i_c1 = k_ots * i_ext
        st1 = Step(
            what="Отстройка ТО от внешнего КЗ",
            why=("Отсечка не имеет выдержки времени, поэтому она не должна срабатывать при КЗ "
                 "за пределами своей зоны — иначе теряется селективность."),
            given={"Расчётная точка": f"КЗ {kind_ru}, режим «{mode_ext.name}»"
                   + ("" if tie is None else " (и ещё режимы с тем же током)"),
                   "Доля тока через защиту": f"{d_ext*100:.0f} % полного тока КЗ",
                   "Iк(3)max через защиту": f"{fmt(i_ext)} А",
                   "kотс": fmt(k_ots)},
            formula="Iсз ≥ kотс · Iк(3)max.внеш",
            substitution=f"Iсз ≥ {fmt(k_ots)} · {fmt(i_ext)} = {fmt(i_c1)} А",
            result=f"Iсз ≥ {fmt(i_c1)} А",
            source=m.cite("to.k_ots"),
            note=tie,
            children=sc_ext.step_for("i3"),
        )
        conditions.append((i_c1, "отстройка от внешнего КЗ", st1, mode_ext))

    # ── условие 2: бросок тока намагничивания трансформатора в зоне ──
    conditions.extend(_inrush_conditions(ctx, br, modes, inrush_candidates,
                                         u_pr, res, inrush_search_problems))

    # ── выбор наибольшего условия ──
    if not conditions:
        res.record_coverage("Проверка чувствительности ТО", 0, len(modes),
                            ["Уставка ТО не определена."])
        res.recompute_status()
        return res
    i_calc, governing, _, governing_mode = max(conditions, key=lambda c: c[0])
    for _, _, s, _ in conditions:
        res.steps.append(s)
    if len(conditions) > 1:
        res.messages.append("Определяющее условие: " + governing + ".")

    scale = br.prot.i_scale or m.ct_scale()
    try:
        i_prim, i_sec, note = round_to_scale(i_calc, br.ct_k, scale)
    except SettingRangeError as exc:
        res.i_calc = i_calc
        res.t = br.prot.t_to if br.prot.t_to is not None else m.k("to.t")
        res.messages.append(str(exc))
        res.checks.append(Check(
            "Диапазон токовой уставки терминала", i_calc / br.ct_k, None,
            FAIL, str(exc),
        ))
        res.record_coverage("Проверка чувствительности ТО", 0, len(modes),
                            ["Допустимая уставка терминала не выбрана."])
        res.recompute_status()
        return res
    pick = Step(
        what="Принятая уставка ТО",
        why="Из всех условий отстройки принимается наибольшее, затем — ближайшее большее по шкале терминала.",
        given={f"условие {i+1}: {d}": f"{fmt(v)} А" for i, (v, d, _, _) in enumerate(conditions)},
        formula="Iсз = max(условия отстройки), округлённое вверх по шкале уставок",
        substitution=f"Iсз = {fmt(i_calc)} А → {fmt(i_prim)} А",
        result=f"Iсз перв. = {fmt(i_prim)} А;  Iсз втор. = {fmt(i_sec)} А",
        accepted=f"{fmt(i_prim)} А (первичн.) / {fmt(i_sec)} А (вторичн.) — {note}",
    )
    res.steps.append(pick)
    res.i_calc, res.i_primary, res.i_secondary = i_calc, i_prim, i_sec
    res.t = br.prot.t_to if br.prot.t_to is not None else m.k("to.t")
    res.governing_mode = governing_mode.name

    # ── чувствительность в месте установки ──
    # Точка расчёта — узел, где физически стоит ТТ, а не тот конец ветви,
    # который оказался питающим. Иначе для защиты на стороне НН ток брался бы
    # с другой ступени напряжения и пересчитывался отношением классов.
    prot_node = net.protection_node_id(br)
    req = m.k("sensitivity.kch_to")
    sens_rows: list[tuple] = []
    sens_problems: list[str] = []
    for mode in modes:
        try:
            _require_solver(ctx, mode)
            i_node, sc = ctx.current_at(mode, prot_node, u_stage_pr, "i2")
            share = ctx.share_at_installation(mode, br, prot_node)
            _finite_current(i_node * share)
        except Exception as exc:
            sens_problems.append(f"режим «{mode.name}»: {exc}")
            continue
        i = i_node * share
        sens_rows.append((i, mode, sc, share))
    res.record_coverage("Проверка чувствительности ТО", len(sens_rows), len(modes), sens_problems)
    best, sens_tied = pick_extreme(sens_rows, key=lambda r: r[0], largest=False)
    if best is None:
        res.checks.append(Check(
            "Kч ТО в месте установки", None, req, UNRESOLVED,
            "; ".join(sens_problems) or "нет режима для проверки",
        ))
        if sens_problems:
            res.messages.append(
                "Чувствительность ТО не проверена: " + "; ".join(sens_problems) + ".")
    else:
        i_min, mode_min, sc_min, share_min = best
        if share_min <= 1e-9:
            message = (
                f"Повреждение в начале защищаемого элемента «{br.name}» не питается "
                f"через узел установки ТТ «{net.node(prot_node).name}»: весь ток "
                "приходит по самой защищаемой ветви и через измерительный "
                "трансформатор не проходит. Из этой точки измерения мгновенная "
                "отсечка защищаемый элемент не видит. Проверьте, на той ли стороне "
                "задан ТТ, и нужна ли здесь ТО вообще.")
            res.checks.append(Check(
                "Kч ТО в месте установки", None, req, UNRESOLVED,
                "ток повреждения не проходит через ТТ", None,
            ))
            res.messages.append(message)
        else:
            kch = i_min / i_prim
            sens_tie = _mode_tie_note(sens_rows, sens_tied, mode_min.name, largest=False)
            given = {
                "Точка": f"{net.node(prot_node).name} (узел установки ТТ), "
                         f"режим «{mode_min.name}»"
                         + ("" if sens_tie is None else
                            " (и ещё режимы с тем же током)"),
                "Iк(2)min в узле": f"{fmt(i_min / share_min)} А",
                "Доля через ТТ": f"{share_min * 100:.0f} % полного тока КЗ",
                "Iк(2)min через защиту": f"{fmt(i_min)} А",
                "Iсз перв.": f"{fmt(i_prim)} А",
            }
            cst = Step(
                what="Чувствительность ТО в месте установки",
                why="Отсечка должна надёжно работать при КЗ в начале защищаемого участка.",
                given=given,
                formula="Kч = Iк(2)min через защиту / Iсз",
                substitution=f"Kч = {fmt(i_min)} / {fmt(i_prim)} = {fmt(kch)}",
                result=f"Kч = {fmt(kch)} (требуется ≥ {fmt(req)})",
                source=m.cite("sensitivity.kch_to"),
                note=" ".join(filter(None, (
                    ("Повреждение в начале элемента питается всем, кроме самой "
                     "защищаемой ветви: ток с её дальнего конца попадает в место "
                     "повреждения, не проходя через ТТ. При радиальном питании "
                     "доля равна 100 %." if share_min < 0.999 else None),
                    sens_tie,
                ))) or None,
            )
            res.steps.append(cst)
            ok = kch >= req
            res.checks.append(Check("Kч ТО в месте установки", kch, req,
                                    OK if ok else FAIL, f"режим «{mode_min.name}»", cst))
            if not ok:
                res.messages.append(
                    f"Отсечка не проходит по чувствительности: уставка отстройки ({fmt(i_prim)} А) "
                    f"выше минимального тока КЗ в месте установки ({fmt(i_min)} А). Физический "
                    "смысл — сопротивление защищаемого участка мало по сравнению с сопротивлением "
                    "до шин, поэтому зоны у отсечки практически нет. Типовые решения: отказаться "
                    "от ТО на этом присоединении, применить ступенчатую МТЗ с ускорением или "
                    "защиту с абсолютной селективностью. Выбор — за проектировщиком.")
        if sens_problems:
            res.messages.append(
                "Часть режимов не проверена по чувствительности ТО: "
                + "; ".join(sens_problems) + ".")

    # ── зона действия по длине линии ──
    if isinstance(br, LineBranch):
        zone_problems: list[str] = []
        zone = _reach_km(ctx, br, i_prim, diagnostics=zone_problems)
        if zone is not None:
            frac = 100.0 * zone / br.length_km
            zst = Step(
                what="Зона действия ТО по длине линии",
                why="Показывает, какая часть линии охвачена мгновенной отсечкой в самом лёгком режиме.",
                given={"Длина линии": f"{fmt(br.length_km)} км", "Iсз перв.": f"{fmt(i_prim)} А"},
                formula="Точка, где Iк(3) в минимальном режиме падает до Iсз",
                substitution=f"lзоны = {fmt(zone)} км",
                result=f"Зона ТО ≈ {fmt(zone)} км ({frac:.0f} % длины линии)",
                note=("Отсечка охватывает только часть линии — это нормально; остальное "
                      "резервирует МТЗ. Оценка сделана по минимальному из заданных режимов."),
            )
            res.steps.append(zst)
            res.messages.append(f"Зона мгновенного действия ТО ≈ {fmt(zone)} км из "
                                f"{fmt(br.length_km)} км ({frac:.0f} %).")
        elif zone_problems:
            res.messages.append("Зона действия ТО по длине линии не определена: "
                                + "; ".join(zone_problems) + ".")

    res.recompute_status()
    return res


def _inrush_voltage(m, transformer: TransformerBranch, u_pr: float,
                    basis: str) -> tuple[float, str]:
    """Напряжение, на котором считается Iном трансформатора, и его описание.

    ``stage_average`` — прежнее поведение: среднее расчётное напряжение
    ступени. ``nameplate`` — паспортное напряжение той обмотки, которая
    обращена к защите; если паспорт не задан или не относится к ступени
    защиты, берётся номинальный класс узла. Класс узла — тоже номинальное, а
    не среднее напряжение, поэтому запасной вариант не возвращает расчёт к
    прежнему поведению незаметно для пользователя.
    """
    if basis == "stage_average":
        return m.u_avg(u_pr), f"среднее расчётное напряжение ступени {fmt(m.u_avg(u_pr))} кВ"
    matching = [
        (value, side) for value, side in
        ((transformer.u_hv, "ВН"), (transformer.u_lv, "НН"))
        if value and belongs_to_stage(float(value), float(u_pr))
    ]
    # ВН-луч трёхобмоточного трансформатора хранит одно и то же паспортное
    # напряжение в u_hv и u_lv. Это два описания одной величины, не два
    # неоднозначных кандидата. Различные паспортные значения не сближаем
    # допуском: они должны по-прежнему приводить к явному запасному варианту.
    if len({float(value) for value, _ in matching}) == 1:
        value, side = matching[0]
        return float(value), (f"паспортное напряжение обмотки {side} "
                              f"«{transformer.name}» {fmt(value)} кВ")
    return float(u_pr), (f"номинальный класс стороны защиты {fmt(u_pr)} кВ "
                         "(паспортное напряжение обмотки не определено однозначно)")


def _require_solver(ctx, mode) -> None:
    if mode.id not in ctx.solvers:
        raise ValueError(ctx.errors.get(mode.id, "Решатель обязательного режима отсутствует."))


def _finite_current(value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Расчётный ток должен быть конечным и неотрицательным.")


def _mode_tie_note(rows, count: int, chosen: str, *, largest=True) -> str | None:
    if count <= 1:
        return None
    extremum = (max if largest else min)(row[0] for row in rows)
    band = max(ABSOLUTE_BAND, RELATIVE_BAND * abs(extremum))
    equal = dict.fromkeys(f"«{row[1].name}» ({row[1].id})" for row in rows
                         if abs(row[0] - extremum) <= band)
    note = tie_note(len(equal), chosen)
    if note is None:
        return None
    return note + " Равнозначные режимы: " + ", ".join(equal) + "."


def _physical_transformer_id(net, transformer: TransformerBranch) -> str:
    for apparatus in net.transformers3w.values():
        if transformer.id in apparatus.branch_ids:
            return apparatus.id
    return transformer.id


def _inrush_conditions(ctx, br, modes, candidates, u_pr, res, search_problems):
    """Individual transformer energizations, counted once per apparatus and mode.

    Kбр is a multiple of the energized winding's nominal current. The legacy
    fault distribution factor does not describe the magnetizing transient.
    Therefore only a unique radial path through the CT admits a numeric bound.
    """
    net, m = ctx.net, ctx.meth
    required = len(search_problems)
    checked = 0
    problems = list(search_problems)
    grouped: dict[tuple, tuple[float, str, Step, Mode]] = {}
    for mode in modes:
        seen: set[str] = set()
        for transformer in candidates.get(mode.id, []):
            physical_id = _physical_transformer_id(net, transformer)
            if physical_id in seen:
                continue
            seen.add(physical_id)
            label = f"«{transformer.name}», режим «{mode.name}» ({mode.id})"
            required += 1
            try:
                source_end, _, ring = net.orient(transformer, mode)
                if ring:
                    raise ValueError("направление включения и токораспределение броска "
                                     "при питании с двух сторон не определены")
                if transformer.id == br.id:
                    if net.protection_node_id(br) != source_end:
                        checked += 1
                        res.messages.append(
                            f"{label}: собственный бросок при включении со стороны "
                            f"«{net.node(source_end).name}» не проходит через ТТ "
                            "другой обмотки; это условие здесь неприменимо.")
                        continue
                elif not ctx.is_radial_through(br, source_end, mode):
                    raise ValueError("нет единственного пути питания трансформатора через "
                                     "защиту; деление броска между вводами не определено")
                ratio = transformer.i_inrush_ratio
                if (not isinstance(ratio, (int, float)) or isinstance(ratio, bool)
                        or not math.isfinite(ratio) or ratio <= 0):
                    raise ValueError("кратность броска i_inrush_ratio не задана конечным "
                                     "положительным числом; подстановка Kбр=1 недопустима")
                basis = m.text("to.i_nom_basis")
                u_i_nom, basis_ru = _inrush_voltage(m, transformer, u_pr, basis)
                i_nom = nominal_current(transformer.s_nom, u_i_nom)
                if not math.isfinite(i_nom) or i_nom <= 0:
                    raise ValueError("номинальный ток включаемой обмотки не определён")
                k_inr = m.k("to.k_ots_inrush")
                i_c2 = k_inr * ratio * i_nom
                _finite_current(i_c2)
            except Exception as exc:
                problems.append(f"{label}: {exc}")
                continue
            checked += 1
            key = (physical_id, u_i_nom, ratio, i_c2)
            mode_label = f"«{mode.name}» ({mode.id})"
            if key in grouped:
                grouped[key][2].given["Применимые режимы"] += ", " + mode_label
                continue
            step = Step(
                what="Отстройка ТО от броска тока намагничивания",
                why="При включении трансформатора бросок намагничивающего тока не должен вызывать отсечку.",
                given={"Трансформатор": f"«{transformer.name}» ({physical_id})",
                       "Применимые режимы": mode_label,
                       "Sном трансформатора": f"{fmt(transformer.s_nom)} кВ·А",
                       "Напряжение для Iном": basis_ru,
                       "Iном (на стороне защиты)": f"{fmt(i_nom)} А",
                       "kотс.бр": fmt(k_inr), "кратность броска": fmt(ratio)},
                formula="Iсз ≥ kотс.бр · Kбр · Iном",
                substitution=f"Iсз ≥ {fmt(k_inr)} · {fmt(ratio)} · {fmt(i_nom)} = {fmt(i_c2)} А",
                result=f"Iсз ≥ {fmt(i_c2)} А", source=m.cite("to.k_ots_inrush"),
                note=("Kбр относится к номинальному току включаемой обмотки. "
                      "Проверено отдельное включение этого трансформатора при "
                      "радиальном питании через ТТ; ток КЗ для деления броска не используется."),
            )
            grouped[key] = (i_c2, "отстройка от броска намагничивания", step, mode)
    if required or isinstance(br, TransformerBranch) or br.prot.to_reach == "behind_transformer":
        res.record_coverage("Отстройка ТО от броска намагничивания", checked, required, problems)
    return list(grouped.values())


def _transformers_at(ctx: Context, br: Branch, mode) -> list[TransformerBranch]:
    """Трансформаторы, присоединённые к концу ветви со стороны нагрузки.

    Конец берётся из фактического направления питания (`Network.orient`), а не
    из того, в каком порядке записаны `node_from`/`node_to`: иначе одна и та же
    схема давала разные уставки в зависимости от того, в какую сторону
    нарисована линия. Учитываются обе стороны трансформатора и только те
    аппараты, которые в данном режиме включены.
    """
    net = ctx.net
    _, load_end, _ = net.orient(br, mode)
    # Closed ideal apparatus does not delimit the electrical terminal bus.
    # Traversal never crosses another impedance or the protected apparatus.
    component, stack = {load_end}, [load_end]
    adjacency = net.adjacency(mode)
    while stack:
        node = stack.pop()
        for nxt, edge in adjacency.get(node, []):
            if (isinstance(edge, TieBranch) and edge.id != br.id
                    and nxt not in component and nxt != GRID):
                component.add(nxt)
                stack.append(nxt)
    found = [
        b for b in net.active_branches(mode)
        if isinstance(b, TransformerBranch)
        and b.id != br.id
        and (b.node_from in component or b.node_to in component)
    ]
    found.sort(key=lambda b: b.id)
    unique = {}
    for transformer in found:
        unique.setdefault(_physical_transformer_id(net, transformer), transformer)
    return list(unique.values())


def _reach_km(ctx: Context, br: LineBranch, i_set: float, *,
              diagnostics: list[str] | None = None) -> float | None:
    """Длина участка линии, охваченного отсечкой (по наименьшему току КЗ из режимов).

    Оценка справедлива только когда повреждение в начале линии питается
    полностью через эту защиту. При питании с двух сторон ток через защиту
    меньше тока в точке, и одномерная оценка по сопротивлению линии перестаёт
    быть корректной. Если хотя бы один обязательный режим не проверен,
    числовая оценка зоны по оставшемуся подмножеству не выдаётся.
    """
    m, net = ctx.meth, ctx.net
    prot_node = net.protection_node_id(br)
    u_avg = stage_voltage(net.node(prot_node), m)
    worst = None
    diagnostics = diagnostics if diagnostics is not None else []
    modes, _, problems = ctx.protection_modes(br)
    if problems:
        diagnostics.extend(problems)
        return None
    if not modes:
        diagnostics.append("нет применимых режимов")
        return None
    for mode in modes:
        label = f"режим «{mode.name}» ({mode.id})"
        try:
            _require_solver(ctx, mode)
            if ctx.share_at_installation(mode, br, prot_node) < 0.999:
                diagnostics.append(label + ": одномерная оценка не применима при питании с двух сторон")
                return None
            z_bus = ctx.solvers[mode.id].at(prot_node).z_th
            z_line, _ = line_impedance(br, m, mode.system)
        except Exception as exc:
            diagnostics.append(f"{label}: {exc}")
            return None
        z0 = z_line / br.length_km if br.length_km else 0
        z_target = u_avg / (SQRT3 * i_set / 1000.0)     # Ом при токе в А
        a = abs(z0) ** 2
        b = 2 * (z_bus.real * z0.real + z_bus.imag * z0.imag)
        c = abs(z_bus) ** 2 - z_target ** 2
        if a <= 0:
            diagnostics.append(label + ": погонное сопротивление линии не определено")
            return None
        d = b * b - 4 * a * c
        if d < 0:
            x = 0.0
        else:
            x = (-b + math.sqrt(d)) / (2 * a)
        x = max(0.0, min(br.length_km, x))
        if worst is None or x < worst:
            worst = x
    return worst
