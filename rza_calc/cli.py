# -*- coding: utf-8 -*-
"""
Командный интерфейс расчётного ядра.

GUI на PySide6 надстраивается сверху и вызывает те же функции. Пока ядро
не считает правильно в CLI, делать интерфейс рано.

    python -m rza_calc <проект.json> <команда> [аргументы]
    python -m rza_calc example <команда>      — встроенный пример нефтепромысла с ГТЭС

Команды:
    check                    проверить модель и профиль методики
    modes                    список режимов
    sc                       токи КЗ по всем узлам и режимам
    sc --fault-type <тип> [--node <id>] [--mode <id>]
                             фазные токи и напряжения: 3ph, 2ph, 1ph_g, 2ph_g
    table                    сводная таблица уставок всей ПС
    feeder <id>              карточка результата по присоединению
    explain <id> <защита>    полный протокол расчёта (кнопка «?»)
    selectivity              карта селективности и проверка ступеней
    report                   всё сразу
    save-case <паспорт.json>  сохранить полный профиль и паспорт расчёта
    replay-case <паспорт.json> повторить расчёт по сохранённой методике
    save-input <вход.json> сохранить неизменяемый вход без геометрии
    replay-input          рассчитать архив входа, указанный вместо проекта

save-case создаёт только новый файл (существующий не перезаписывается).
Сохранение паспорта не превращает отрицательный инженерный результат в успех:
для save-case действуют те же коды 0/1/3, что и для table.
replay-case выводит полный протокол и возвращает те же коды 0/1/3. Он читает
электрическую модель проекта, но не его внешний файл методики. Методика берётся
только из паспорта; другая модель, порядок ID или версия ядра/алгоритма — ошибка 2.

Коды возврата
-------------
    0   расчёт выполнен, нарушений не найдено
    1   расчёт выполнен, есть непройденные проверки защит или нарушения
        селективности — это инженерный результат, а НЕ ошибка программы
    3   расчёт выполнен, но часть проверок не завершена (статус «?»):
        программа не смогла получить достоверное число и не выдаёт его за ноль
    2   расчёт НЕ выполнен: блокирующая ошибка исходных данных, отсутствующий
        файл, неизвестная команда

Для sc с опциями код 0 означает, что все запрошенные случаи КЗ рассчитаны,
3 — хотя бы один случай не завершён, 2 — ошибка запроса/исходных данных.
Эта команда не оценивает прохождение уставок защит. sc без опций сохраняет
прежний формат и прежние коды возврата.

Команда `check` проверяет ИСХОДНЫЕ ДАННЫЕ, поэтому возвращает только 0 или 2:
непройденная уставка — не дефект модели и не повод считать проверку данных
проваленной. Раньше `check` возвращал 1 из-за чужого результата, и по нему
нельзя было автоматически отличить «схема описана неверно» от «защита не
проходит по чувствительности» (дефект OPEN-14).
"""
from __future__ import annotations

import sys
from pathlib import Path

from .core import selectivity as sel
from .core.engine import CalculationCase, ProjectResult, render_table, run, run_input, summary_table
from .calculation.input import (capture_project_input, load_calculation_input,
                                save_calculation_input)
from .core.fault_types import FaultSpec, FaultType
from .core.result import FAIL, OK, UNRESOLVED
from .core.trace import fmt

W = 78

#  Коды возврата. Смысл описан в докстроке модуля; здесь — единственное место,
#  где они заданы, чтобы их нельзя было развести по вызовам.
EXIT_OK = 0
EXIT_PROTECTION_FAILS = 1
EXIT_INPUT_ERROR = 2
EXIT_UNRESOLVED = 3

#  Команды, отвечающие за исходные данные, а не за результат расчёта.
INPUT_ONLY_COMMANDS = ("check", "modes")


class CliInputError(ValueError):
    """Неизвестный объект запроса, а не отрицательный результат защиты."""


def _parse_sc_options(args: list[str]) -> dict[str, str]:
    """Разбирать запрос до расчёта и не игнорировать опечатки в опциях."""
    options: dict[str, str] = {}
    allowed = {"--fault-type": "fault_type", "--node": "node_id", "--mode": "mode_id"}
    index = 0
    while index < len(args):
        flag, equals, value = args[index].partition("=")
        if flag not in allowed:
            raise CliInputError(f"Неизвестный аргумент sc: {args[index]}")
        key = allowed[flag]
        if key in options:
            raise CliInputError(f"Опция {flag} указана повторно.")
        if not equals:
            index += 1
            if index >= len(args) or args[index].startswith("--"):
                raise CliInputError(f"После {flag} требуется значение.")
            value = args[index]
        if not value:
            raise CliInputError(f"После {flag} требуется значение.")
        options[key] = value
        index += 1
    if "fault_type" in options:
        try:
            FaultType(options["fault_type"])
        except ValueError as exc:
            raise CliInputError("Неизвестный тип КЗ. Допустимы: 3ph, 2ph, 1ph_g, 2ph_g.") from exc
    return options


def _phasor(value: complex, unit: str) -> str:
    value = complex(value)
    return f"{fmt(value.real)} {value.imag:+.6g}j {unit}; |·| = {fmt(abs(value))} {unit}"


def cmd_selected_sc(pr: ProjectResult, *, fault_type: str = "3ph",
                    node_id: str | None = None, mode_id: str | None = None) -> tuple[str, bool]:
    """Выбранный вид КЗ; bool означает хотя бы один незавершённый случай."""
    net = pr.ctx.net
    try:
        kind = FaultType(fault_type)
    except ValueError as exc:
        raise CliInputError("Неизвестный тип КЗ. Допустимы: 3ph, 2ph, 1ph_g, 2ph_g.") from exc
    if node_id is not None and node_id not in net.nodes:
        raise CliInputError(f"Узел '{node_id}' не найден.")
    if mode_id is not None and mode_id not in net.modes:
        raise CliInputError(f"Режим '{mode_id}' не найден.")
    nodes = [net.nodes[node_id]] if node_id is not None else list(net.nodes.values())
    modes = [net.modes[mode_id]] if mode_id is not None else list(net.modes.values())
    labels = {
        FaultType.THREE_PHASE: "Трёхфазное КЗ",
        FaultType.LINE_LINE: "Двухфазное КЗ B–C",
        FaultType.LINE_GROUND: "Однофазное КЗ A–земля",
        FaultType.LINE_LINE_GROUND: "Двухфазное КЗ B–C–земля",
    }
    out = [_title(f"{labels[kind]} ({kind.value})")]
    out.append("  Фазные токи и остаточные напряжения в точке КЗ; Zповр = 0 Ом.")
    unresolved = False
    for node in nodes:
        for mode in modes:
            out.append(f"\n  {node.name} [{node.id}] · {mode.name} [{mode.id}]")
            solver = pr.ctx.solvers.get(mode.id)
            if solver is None:
                unresolved = True
                out.append("  НЕ РАССЧИТАНО [MODE_UNAVAILABLE]: " +
                           pr.ctx.errors.get(mode.id, "Режим не рассчитан."))
                continue
            try:
                result = solver.fault_at(node.id, FaultSpec(kind))
            except Exception as exc:
                unresolved = True
                out.append(f"  НЕ РАССЧИТАНО [{getattr(exc, 'code', 'CALCULATION_ERROR')}]: {exc}")
                continue
            for phase, current, voltage in zip("ABC", result.iabc_ka, result.vabc_kv):
                out.append(f"  I{phase}: {_phasor(current, 'кА')}")
                out.append(f"  U{phase}: {_phasor(voltage, 'кВ')}")
            out.append(f"  3I0: {_phasor(result.residual_current_ka, 'кА')}")
            for assumption in result.assumptions:
                out.append(f"  Допущение: {assumption}")
    out.append("\n  Напряжения фаз — относительно земли, токи — первичные на ступени точки КЗ.")
    out.append("  Выбор вида повреждения относится к этому расчёту ТКЗ; уставки защит не изменены.")
    return "\n".join(out), unresolved


def _hr(ch: str = "─") -> str:
    return ch * W


def _title(text: str) -> str:
    return f"\n{_hr('═')}\n{text.center(W)}\n{_hr('═')}"


def cmd_check(pr: ProjectResult) -> str:
    out = [_title("ПРОВЕРКА ИСХОДНЫХ ДАННЫХ")]
    if pr.warnings:
        for w in pr.warnings:
            out.append("  ⚠ " + w)
    else:
        out.append("  Замечаний нет.")
    case = pr.calculation_case
    snapshot = case.methodology_snapshot if case is not None else None
    meth = snapshot.to_methodology() if snapshot is not None else pr.ctx.meth
    out.append("")
    out.append(f"  Профиль методики: {meth.name}  [{meth.status}]")
    if pr.calculation_case is not None:
        case = pr.calculation_case
        out.append(f"  Версия приложения: {case.application_version}")
        out.append(f"  Версия ядра: {case.kernel_version}")
        out.append(f"  Алгоритм: {case.algorithm_version}")
        out.append(f"  Отпечаток модели: {case.model_fingerprint[:16]}…")
        out.append(f"  Отпечаток методики: {case.methodology_fingerprint[:16]}…")
    if meth.warning:
        out.append("  " + _wrap(meth.warning, W - 4, "  "))
    out.append(cmd_methodology(pr))
    return "\n".join(out)


def cmd_methodology(pr: ProjectResult) -> str:
    """Применённые коэффициенты с происхождением каждого.

    Печатается снимок из паспорта расчёта, а не текущее содержимое файла
    профиля: файл мог измениться после расчёта, и тогда таблица показывала бы
    не те числа, по которым получен результат.
    """
    case = pr.calculation_case
    snapshot = case.methodology_snapshot if case is not None else None
    if snapshot is None:
        return "\n  Снимок применённых коэффициентов недоступен."

    out = [_title("ПРИМЕНЁННЫЕ КОЭФФИЦИЕНТЫ")]
    out.append(f"  Профиль: {snapshot.profile_name}  [{snapshot.status}]")
    approval = dict(snapshot.approval)
    if any(approval.get(field) for field in ("approved_by", "approved_on", "object")):
        out.append(f"  Утверждён: {approval.get('approved_by', '—')} "
                   f"{approval.get('approved_on', '—')} · объект: "
                   f"{approval.get('object', '—')}")
    out.append("")
    for item in snapshot.entries:
        value = item.value
        shown = fmt(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
        unit = f" {item.unit}" if item.unit else ""
        out.append(f"  {item.path:<38}{shown + unit:>14}")
        out.append(f"      {item.provenance.title}")
        mark = "" if item.provenance.confirmed else "  ⚠ пункт не сверен"
        out.append(f"      источник: {item.provenance.origin or '—'} — "
                   f"{item.provenance.source or '—'}{mark}")
    references = dict(snapshot.references)
    if references:
        out.append("")
        out.append("  ОБОЗНАЧЕНИЯ ИСТОЧНИКОВ:")
        for key, text in references.items():
            out.append(f"    {key}: " + _wrap(text, W - 10, "        "))
    return "\n".join(out)


def cmd_modes(pr: ProjectResult) -> str:
    out = [_title("РЕЖИМЫ СЕТИ")]
    net = pr.ctx.net
    for mode in net.modes.values():
        mark = "✓" if mode.id in pr.ctx.solvers else "✗"
        sysname = "максимальный" if mode.system == "max" else "минимальный"
        out.append(f"  {mark} {mode.name}   (система: {sysname})")
        if mode.description:
            out.append(f"      {mode.description}")
        st = ", ".join(f"{net.branches[b].name} = {'ВКЛ' if v else 'ОТКЛ'}"
                       for b, v in mode.states.items() if b in net.branches)
        if st:
            out.append(f"      {st}")
        if mode.id in pr.ctx.errors:
            out.append(f"      ОШИБКА: {pr.ctx.errors[mode.id]}")
        out.append("")
    return "\n".join(out)


def cmd_sc(pr: ProjectResult) -> str:
    net = pr.ctx.net
    out = [_title("ТОКИ КОРОТКОГО ЗАМЫКАНИЯ")]
    rows = [["Узел", "Uном, кВ"]]
    for mode in pr.ctx.modes:
        rows[0] += [f"Iк(3), А\n{mode.name}", f"Iк(2), А\n{mode.name}"]
    header = ["Узел", "Uном"] + [f"{m.name[:16]}" for m in pr.ctx.modes]
    colw = max(len(n.name) for n in net.nodes.values()) + 2
    out.append("  " + "Узел".ljust(colw) + "Uном".rjust(6) +
               "".join(f"{m.name[:20]:>22}" for m in pr.ctx.modes))
    out.append("  " + " " * colw + " " * 6 + "".join(f"{'Iк(3) / Iк(2), А':>22}" for _ in pr.ctx.modes))
    out.append("  " + _hr()[:colw + 6 + 22 * len(pr.ctx.modes)])
    for nid, node in net.nodes.items():
        line = "  " + node.name.ljust(colw) + f"{fmt(node.u_nom):>6}"
        for mode in pr.ctx.modes:
            try:
                r = pr.ctx.solvers[mode.id].at(nid)
                line += f"{fmt(r.i3*1000) + ' / ' + fmt(r.i2*1000):>22}"
            except Exception:
                line += f"{'не запитан':>22}"
        out.append(line)
    out.append("")
    out.append("  Iк(3) — трёхфазное КЗ положительной последовательности.")
    out.append("  Iк(2) в этой общей таблице — оценка √3/2·Iк(3) при Z2 = Z1.")
    out.append("  Отдельный расчёт по Z2: sc --fault-type 2ph --node <узел> --mode <режим>.")
    out.append("  Фазные токи выбранного КЗ пока не заменяют токи действующих расчётов защит.")
    return "\n".join(out)


def cmd_table(pr: ProjectResult) -> str:
    out = [_title("СВОДНАЯ ТАБЛИЦА УСТАВОК")]
    out.append(render_table(summary_table(pr)))
    out.append("")
    out.append("  Kч — наименьший из рассчитанных коэффициентов чувствительности.")
    out.append("  ✓ — все проверки пройдены,  ✗ — есть нарушение,  ? — расчёт не завершён.")
    bad = [r for r in pr.all_results() if r.status != OK]
    if bad:
        out.append("")
        out.append("  ТРЕБУЮТ ВНИМАНИЯ:")
        for r in bad:
            out.append(f"    • {r.branch_name} / {r.kind}")
            for c in r.checks:
                if c.status != OK:
                    out.append(f"        {c.line()}")
            for msg in r.messages[:3]:
                out.append(f"        {msg}")
    return "\n".join(out)


def cmd_feeder(pr: ProjectResult, bid: str) -> str:
    net = pr.ctx.net
    br = _find(net, bid)
    res = pr.results.get(br.id, {})
    out = [_title(f"{br.name.upper()} — РЕЗУЛЬТАТ РАСЧЁТА")]

    out.append(_hr())
    out.append("РАСЧЁТНЫЕ ТОКИ".center(W))
    out.append(_hr())
    mtz = res.get("МТЗ")
    if mtz and mtz.steps:
        for s in mtz.steps[0].children:
            if "рабочий ток" in s.what.lower() or "оминальный ток" in s.what:
                out.append(f"  {s.what:<52}{s.result.split('= ')[-1]:>22}")
    for mode in pr.ctx.modes_where_active(br):
        try:
            src, load_end, _ = net.orient(br, mode)
            # Оба тока приводятся к стороне, где стоит ТТ: иначе для защиты на
            # стороне НН токи печатались бы на чужой ступени напряжения.
            u_side = (pr.ctx.protection_side_stage(br) if br.has_protection_point
                      else pr.ctx.meth.u_avg(net.node(src).u_nom))
            i1, _ = pr.ctx.current_at(mode, src, u_side, "i3")
            i2, _ = pr.ctx.current_at(mode, load_end, u_side, "i2")
            out.append(f"  Iк(3)max в начале / Iк(2)min в конце, режим «{mode.name[:22]}»")
            out.append(f"  {'':<52}{fmt(i1) + ' / ' + fmt(i2) + ' А':>22}")
        except Exception:
            pass
    if br.ct_ratio:
        out.append(f"  {'Трансформатор тока':<52}"
                   f"{fmt(br.ct_ratio[0]) + '/' + fmt(br.ct_ratio[1]) + ' А':>22}")

    for kind in ("МТЗ", "ТО", "ОЗЗ"):
        r = res.get(kind)
        if r is None:
            continue
        out.append(_hr())
        out.append(kind.center(W))
        out.append(_hr())
        if r.i_primary is None:
            out.append("  Не рассчитано.")
        else:
            if r.i_calc is not None:
                out.append(f"  {'Расчётная уставка':<52}{fmt(r.i_calc) + ' А':>22}")
            out.append(f"  {'Принято, первичный ток':<52}{fmt(r.i_primary) + ' А':>22}")
            if r.i_secondary is not None:
                out.append(f"  {'Принято, вторичный ток':<52}{fmt(r.i_secondary) + ' А':>22}")
            if r.t is not None:
                out.append(f"  {'Выдержка времени':<52}{fmt(r.t) + ' с':>22}")
            if r.governing_mode:
                out.append(f"  {'Определяющий режим':<52}{r.governing_mode[:22]:>22}")
        for c in r.checks:
            out.append("  " + c.line())
        for msg in r.messages:
            out.append("  » " + _wrap(msg, W - 6, "      "))
        out.append("  СТАТУС: " + {OK: "✓ проходит", FAIL: "✗ НЕ ПРОХОДИТ",
                                   UNRESOLVED: "? расчёт не завершён"}[r.status])
    out.append(_hr())
    out.append("  Подробный вывод любой цифры:  explain " + br.id + " МТЗ")
    return "\n".join(out)


def cmd_explain(pr: ProjectResult, bid: str, kind: str) -> str:
    br = _find(pr.ctx.net, bid)
    r = pr.results.get(br.id, {}).get(kind.upper())
    if r is None:
        avail = ", ".join(pr.results.get(br.id, {}))
        return f"Для «{br.name}» защита «{kind}» не рассчитывалась. Есть: {avail or 'ничего'}"
    return _title(f"ПРОТОКОЛ РАСЧЁТА — {br.name} / {r.kind}") + "\n" + r.explain()


def cmd_selectivity(pr: ProjectResult) -> str:
    out = [_title("СЕЛЕКТИВНОСТЬ")]
    for mode in pr.ctx.modes:
        out.append(sel.time_map(pr.ctx, pr.results, mode))
        out.append("")
    out.append(_hr())
    out.append(sel.report(pr.ctx, pr.pairs))
    out.append(_hr())
    out.append("НАЗНАЧЕНИЕ ВЫДЕРЖЕК ВРЕМЕНИ")
    for s in pr.time_steps:
        out.append(s.render(indent=2))
    return "\n".join(out)


def cmd_report(pr: ProjectResult) -> str:
    parts = [cmd_check(pr), cmd_modes(pr), cmd_sc(pr), cmd_table(pr), cmd_selectivity(pr)]
    for bid in pr.results:
        parts.append(cmd_feeder(pr, bid))
    return "\n".join(parts)


def _find(net, bid: str):
    if bid in net.branches:
        return net.branches[bid]
    for b in net.branches.values():
        if b.name.lower().startswith(bid.lower()):
            return b
    raise CliInputError(f"Присоединение '{bid}' не найдено. Есть: "
                        + ", ".join(f"{b.id} ({b.name})" for b in net.protection_points()))


def _wrap(text: str, width: int, pad: str) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return ("\n" + pad).join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] in ("example", "пример"):
        path = Path(__file__).resolve().parent / "examples" / "oilfield_gtes.json"
    else:
        path = Path(argv[0])
    if not path.exists():
        print(f"Файл проекта не найден: {path}")
        print("Подсказка: слово  example  вместо пути откроет встроенный пример "
              "Нефтепромысел «Таёжный» с ГТЭС.")
        return EXIT_INPUT_ERROR

    cmd = argv[1] if len(argv) > 1 else "report"
    args = argv[2:]
    sc_options = {}
    if cmd == "sc":
        try:
            sc_options = _parse_sc_options(args)
        except CliInputError as exc:
            print(f"Ошибка запроса: {exc}")
            return EXIT_INPUT_ERROR
    if cmd == "replay-case" and len(args) != 1:
        print("Ошибка запроса: replay-case требует один путь сохранённого паспорта.")
        return EXIT_INPUT_ERROR

    from .io.project import load, load_project
    saved_case = None
    try:
        if cmd == "replay-case":
            saved_case = CalculationCase.load(args[0])
            net, meth, meta = load(path, methodology_override=saved_case.to_methodology())
            pr = saved_case.replay(net)
        elif cmd == "replay-input":
            if args:
                raise CliInputError("replay-input не принимает дополнительных аргументов.")
            pr = run_input(load_calculation_input(path))
        else:
            project = load_project(path)
            request = capture_project_input(project)
            if cmd == "save-input":
                if len(args) != 1:
                    raise CliInputError("save-input требует путь нового файла расчётного входа.")
                save_calculation_input(request, args[0])
                print(f"Расчётный вход сохранён: {Path(args[0]).resolve()}")
                return EXIT_OK
            project.require_calculation_ready()
            pr = run_input(request)
    except (OSError, KeyError, ValueError) as exc:
        print("Расчёт не выполнен: исходные данные содержат блокирующую ошибку.")
        print(exc)
        return EXIT_INPUT_ERROR

    try:
        if cmd == "check":
            print(cmd_check(pr))
        elif cmd == "modes":
            print(cmd_modes(pr))
        elif cmd == "sc":
            if sc_options:
                text, unresolved = cmd_selected_sc(pr, **sc_options)
                print(text)
                return EXIT_UNRESOLVED if unresolved else EXIT_OK
            print(cmd_sc(pr))
        elif cmd == "table":
            print(cmd_table(pr))
        elif cmd == "feeder":
            print(cmd_feeder(pr, args[0]))
        elif cmd == "explain":
            print(cmd_explain(pr, args[0], args[1] if len(args) > 1 else "МТЗ"))
        elif cmd == "selectivity":
            print(cmd_selectivity(pr))
        elif cmd == "report":
            print(cmd_report(pr))
        elif cmd == "replay-case":
            print(f"Расчёт повторён по паспорту: {Path(args[0]).resolve()}")
            print(f"Исходный паспорт, UTC: {saved_case.created_at_utc}")
            print(cmd_report(pr))
        elif cmd == "replay-input":
            print(f"Расчёт выполнен по неизменяемому входу: {path.resolve()}")
            print(cmd_report(pr))
        elif cmd == "save-case":
            if len(args) != 1:
                raise CliInputError("save-case требует один путь нового файла паспорта.")
            pr.calculation_case.save(args[0])
            print(f"Паспорт расчёта сохранён: {Path(args[0]).resolve()}")
        else:
            print(__doc__)
            return EXIT_INPUT_ERROR
    except IndexError:
        print(__doc__)
        return EXIT_INPUT_ERROR
    except CliInputError as exc:
        print(f"Ошибка запроса: {exc}")
        return EXIT_INPUT_ERROR
    except OSError as exc:
        print(f"Не удалось записать паспорт расчёта: {exc}")
        return EXIT_INPUT_ERROR
    return exit_code(pr, cmd)


def exit_code(pr: ProjectResult, cmd: str) -> int:
    """Код возврата по результату расчёта и по тому, о чём спрашивали.

    Разделение обязательно: «модель описана неверно» и «защита не проходит по
    чувствительности» — разные события с разной реакцией, и сводить их в один
    ненулевой код значит терять эту разницу в любом скрипте.
    """
    if cmd in INPUT_ONLY_COMMANDS:
        return EXIT_OK
    if pr.has_failures:
        return EXIT_PROTECTION_FAILS
    if (any(result.status == UNRESOLVED for result in pr.all_results())
            or not getattr(pr, "is_complete", True)):
        return EXIT_UNRESOLVED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
