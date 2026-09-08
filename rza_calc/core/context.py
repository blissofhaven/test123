# -*- coding: utf-8 -*-
"""Контекст расчёта: сеть + методика + решатели КЗ по всем режимам сразу."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .methodology import Methodology
from .impedance import stage_voltage
from .model import GRID, Branch, Mode, Network, TransformerBranch
from .short_circuit import ScResult, ShortCircuitSolver

SQRT3 = math.sqrt(3.0)


@dataclass
class FaultPoint:
    """Точка КЗ с указанием, радиально ли она питается через рассматриваемую защиту."""
    node_id: str
    name: str
    u_nom: float
    radial: bool
    kind: str          # 'line_end' | 'behind_transformer' | 'bus'


class Context:
    def __init__(self, net: Network, meth: Methodology, *, cancelled=None, progress=None,
                 calculation_input=None):
        self.net, self.meth = net, meth
        self.solvers: dict[str, ShortCircuitSolver] = {}
        self.errors: dict[str, str] = {}
        from ..adapters.operating_parameters import network_for_mode, parallel_source_groups
        from ..calculation.input import checkpoint
        self.mode_networks = {}
        self.mode_warnings = []
        frames = {frame.mode_id: frame for frame in calculation_input.modes} if calculation_input is not None else {}
        if frames and tuple(frames) != tuple(net.modes):
            raise ValueError("Набор режимов не совпадает с подготовленным расчётным входом.")
        for index, (mid, mode) in enumerate(net.modes.items()):
            checkpoint(cancelled, progress, index, len(net.modes), "КЗ: " + mode.name)
            try:
                if frames:
                    from ..calculation.input import validate_mode_execution
                    validate_mode_execution(frames[mid], net, calculation_input.trace)
                effective_net = network_for_mode(net, mode)
                self.mode_networks[mid] = effective_net
                if ((calculation_input is not None and calculation_input.stamp.canonical)
                        or any(item.operating_parameters for item in net.modes.values())):
                    problems = effective_net.validate(effective_net.modes[mid])
                    if problems:
                        raise ValueError("; ".join(problems))
                groups = parallel_source_groups(effective_net, effective_net.modes[mid])
                if groups:
                    permission = mode.operating_parameters.get("parallel_operation")
                    if permission is False:
                        raise ValueError("Параллельная работа соединённых источников запрещена параметрами режима.")
                    if permission is None:
                        self.mode_warnings.append("Режим «" + mode.name + "»: параллельная работа источников присутствует в прежней схеме; явное разрешение режима ещё не записано.")
                self.solvers[mid] = ShortCircuitSolver(effective_net, effective_net.modes[mid], meth)
            except Exception as e:                       # режим может быть некорректным
                self.errors[mid] = str(e)
        checkpoint(cancelled)

    # ---------- режимы ----------
    @property
    def modes(self) -> list[Mode]:
        return [self.net.modes[mid] for mid in self.solvers]

    def modes_where_active(self, br: Branch) -> list[Mode]:
        out = []
        for mode in self.modes:
            if not mode.is_closed(br):
                continue
            live = self.net.energized_nodes(mode)
            if br.node_from in live and br.node_to in live:
                out.append(mode)
        return out

    def protection_modes(self, br: Branch) -> tuple[list[Mode], list[str], list[str]]:
        """Применимость защиты по топологии, независимо от успеха решателя.

        Ошибка сборки решателя не исключает обязательный режим. Если сама
        применимость неизвестна, возвращается причина неполной проверки.
        """
        modes: list[Mode] = []
        excluded: list[str] = []
        problems: list[str] = []
        for mode in self.net.modes.values():
            label = f"Режим «{mode.name}» ({mode.id})"
            try:
                if not self.net.branch_conducting(br, mode):
                    excluded.append(label + ": присоединение отключено.")
                    continue
                live = self.net.energized_nodes(mode)
                if br.node_from not in live or br.node_to not in live:
                    excluded.append(label + ": присоединение не находится под напряжением.")
                    continue
            except Exception as exc:
                problems.append(label + f": применимость не определена: {exc}")
                continue
            modes.append(mode)
        return modes, excluded, problems

    # ---------- ток в точке, приведённый к ступени защиты ----------
    def protection_side_stage(self, br: Branch) -> float:
        """Расчётное напряжение ступени, на которой стоит ТТ присоединения.

        Именно к нему приводятся токи КЗ. Класс узла (``protection_side_u``)
        остаётся для подписей и для номинальных токов: там паспортное значение
        осмысленно, а в приведении токов участвовать не должно.
        """
        node = self.net.node(self.net.protection_node_id(br))
        return stage_voltage(node, self.meth)

    def current_at(self, mode: Mode, point_node: str, protection_stage_kv: float,
                   which: str = "i3") -> tuple[float, ScResult]:
        """Ток КЗ в точке, пересчитанный на ступень, где стоит защита. В амперах.

        Оба напряжения — расчётные напряжения ступеней (средние), тот же
        базис, в котором приведены сопротивления. Смешение с номинальными
        классами здесь давало бы ошибку, растущую с числом ступеней.
        """
        sc = self.solvers[mode.id].at(point_node)
        u_pt = float(sc.u_stage or sc.u_nom)
        u_pr = float(protection_stage_kv)
        if not math.isfinite(u_pt) or not math.isfinite(u_pr) or u_pt <= 0 or u_pr <= 0:
            raise ValueError(
                "Расчётные напряжения точки КЗ и стороны защиты должны быть "
                "конечными и больше нуля."
            )
        i = getattr(sc, which) * 1000.0 * (u_pt / u_pr)
        return i, sc

    def current_through(self, mode: Mode, br: Branch, point_node: str,
                        which: str = "i3") -> tuple[float, ScResult, float]:
        """
        Ток КЗ, протекающий ЧЕРЕЗ данное присоединение при КЗ в точке,
        приведённый к ступени, где стоит защита. Возвращает (ток, результат КЗ,
        коэффициент токораспределения).
        """
        solver = self.solvers[mode.id]
        i_total, sc = self.current_at(
            mode, point_node, self.protection_side_stage(br), which
        )
        # Никакой подстановки 100 % при ошибке: недостоверное токораспределение
        # должно сделать проверку неопределённой, а не выдать ложный OK.
        d = solver.distribution_factor(br, point_node)
        return i_total * d, sc, d

    def share_at_installation(self, mode: Mode, br: Branch, node_id: str) -> float:
        """
        Доля тока КЗ узла, проходящая через ТТ при повреждении в начале
        защищаемого элемента — сразу за измерительным трансформатором.

        Повреждение в этой точке электрически совпадает с узлом установки ТТ,
        но питается всем, КРОМЕ самой защищаемой ветви: ток, приходящий с её
        дальнего конца, попадает в место повреждения, не проходя через ТТ.
        Поэтому доля равна |1 ∓ d|, где d — ориентированный коэффициент
        токораспределения ветви, а знак выбирается по тому, на каком конце
        ветви физически стоит ТТ.

        Для радиального присоединения с ТТ со стороны питания d = 0 и доля
        равна единице — то есть прежнее поведение. Для ветви, питаемой с двух
        сторон, доля меньше единицы. Если весь ток повреждения приходит по
        самой защищаемой ветви (например, ТТ стоит на стороне НН
        трансформатора), доля равна нулю: из этой точки измерения повреждение
        в начале элемента не видно вообще.
        """
        solver = self.solvers[mode.id]
        d = solver.distribution_factor_complex(br, node_id)
        sign = -1.0 if node_id == br.node_from else 1.0
        return abs(1.0 + sign * d)

    # ---------- зоны защиты ----------
    def zone_points(self, br: Branch, mode: Mode) -> tuple[list[FaultPoint], list[FaultPoint]]:
        """
        (точки основной зоны, точки зоны резервирования) для данной ветви и режима.

        По умолчанию зона резервирования НЕ заходит за трансформатор: Kч
        проверяется до концов линий и шин смежного уровня. Проверять защиту
        ввода 110 кВ по КЗ на шинах 0,4 кВ за двумя трансформаторами — строже
        любой методики и даёт ложные нарушения. Поведение переключается
        параметром sensitivity.backup_through_transformer.
        """
        net = self.net
        live = net.energized_nodes(mode)
        src_end, load_end, _ = net.orient(br, mode)
        try:
            zone = net.downstream_nodes(br, mode)
        except Exception:
            zone = set()

        main: list[FaultPoint] = []
        backup: list[FaultPoint] = []
        if load_end in live and load_end != GRID:
            if load_end.endswith("__star"):
                # Обмотка ВН трёхобмоточного трансформатора: зона заканчивается не
                # в расчётной точке звезды, а на шинах среднего и низшего напряжения.
                for b in net.active_branches(mode):
                    if b.node_from != load_end and b.node_to != load_end:
                        continue
                    far = b.node_to if b.node_from == load_end else b.node_from
                    if far in live and far != br.node_from and not far.endswith("__star"):
                        main.append(FaultPoint(far, net.node(far).name,
                                               net.node(far).u_nom, True, "winding"))
            else:
                main.append(FaultPoint(load_end, net.node(load_end).name,
                                       net.node(load_end).u_nom, True, "line_end"))
        deep = br.prot.backup_through_transformer
        if deep is None:
            deep = bool(self.meth.k("sensitivity.backup_through_transformer"))
        # Граница зоны — обход вниз, который ОСТАНАВЛИВАЕТСЯ на трансформаторе,
        # а не пропуск одной ветви-трансформатора. Раньше исключалась только сама
        # ветвь, но не её поддерево, поэтому защита 110 кВ проверялась по КЗ на
        # конце фидера 10 кВ за одной-двумя ступенями трансформации и получала
        # ложные нарушения Kч. Дефект AUD-PROT-010.
        allowed = self._backup_reachable(
            br, mode, deep=deep, seeds=[fp.node_id for fp in main] or [load_end])
        # Точки различаются по ЭЛЕКТРИЧЕСКОЙ принадлежности, а не по ID узла.
        # Идеальный включённый выключатель соединяет два узла ветвью нулевого
        # сопротивления: ток КЗ в них совпадает до последнего бита, и это одна
        # точка сети. Сравнение по ID превращало её в две и добавляло проверку
        # «Kч в зоне резервирования» на месте основной зоны — с требованием
        # 1,2 вместо 1,5, то есть ложное успокоение. Дефект AUD-PROT-011.
        point = self._electrical_point(mode)
        main_points = {point(fp.node_id) for fp in main} | {point(load_end)}
        for b in net.downstream_branches(br, mode):
            b_src, b_load, _ = net.orient(b, mode)
            tgt = b_load
            if tgt not in live or point(tgt) in main_points:
                continue
            if isinstance(b, TransformerBranch) and not deep:
                continue          # не заходим за трансформатор
            if tgt not in allowed:
                continue          # узел лежит за трансформатором
            kind = "behind_transformer" if isinstance(b, TransformerBranch) else "bus"
            backup.append(FaultPoint(tgt, net.node(tgt).name, net.node(tgt).u_nom,
                                     tgt in zone, kind))
        # убрать дубли — тоже по электрической точке; порядок объявления ветвей
        # решает, какой из совпавших узлов остаётся, и он же детерминирован.
        seen, uniq = set(), []
        for fp in backup:
            key = point(fp.node_id)
            if key not in seen:
                seen.add(key); uniq.append(fp)
        return main, uniq

    def _electrical_point(self, mode: Mode):
        """Функция «узел → электрическая точка» для данного режима.

        Если решателя для режима нет, объединение неизвестно, и функция
        возвращает сам узел. Это честный запасной вариант: без решателя
        программа не знает, какие узлы электрически совпадают, и не должна
        делать вид, что знает.
        """
        solver = self.solvers.get(mode.id)
        if solver is None:
            return lambda node_id: node_id
        return solver.electrical_point

    def backup_zone_stops_at_transformer(self, br: Branch, mode: Mode) -> bool:
        """Пуста ли зона резервирования именно из-за границы на трансформаторе.

        Различие важно для сообщения: «ниже ничего нет» и «ниже есть, но за
        трансформатором» — разные факты, и второе нельзя выдавать за первое.
        """
        deep = br.prot.backup_through_transformer
        if deep is None:
            deep = bool(self.meth.k("sensitivity.backup_through_transformer"))
        if deep:
            return False
        _, backup = self.zone_points(br, mode)
        if backup:
            return False
        return any(
            isinstance(b, TransformerBranch)
            for b in self.net.downstream_branches(br, mode)
        )

    def _backup_reachable(self, br: Branch, mode: Mode, *, deep: bool,
                          seeds: list[str]) -> set[str]:
        """Узлы, достижимые вниз от присоединения без пересечения трансформатора.

        При ``deep=True`` ограничения нет и возвращается вся низовая часть: это
        поведение включается параметром ``sensitivity.backup_through_transformer``
        и означает «проверять до самой дальней точки сети».

        При ``deep=False`` (по умолчанию) обход останавливается на каждом
        последующем трансформаторе: зона резервирования заканчивается на его
        ближней стороне. Так это и описано в докстроке метода и в методике — до
        исправления `AUD-PROT-010` реализация этого не делала.
        """
        net = self.net
        adjacency = net.adjacency(mode)
        reachable: set[str] = set()
        # Обход начинается от точек ОСНОВНОЙ зоны — дальних концов самого
        # защищаемого элемента. Сам он трансформатором быть может, и это не
        # мешает: правило запрещает заходить за СЛЕДУЮЩИЙ трансформатор, а не
        # за защищаемый.
        seen = set(seeds)
        frontier = list(seeds)
        while frontier:
            node = frontier.pop()
            for nxt, edge in adjacency.get(node, []):
                if edge.id == br.id or nxt in seen:
                    continue
                if isinstance(edge, TransformerBranch) and not deep:
                    continue      # дальше не идём: это граница зоны
                # Отказ пройти по ребру не означает, что узел посещён: к нему
                # может вести допустимый путь без трансформатора. Иначе зона
                # зависит от порядка объявления параллельных ветвей.
                seen.add(nxt)
                reachable.add(nxt)
                frontier.append(nxt)
        return reachable

    def is_radial_through(self, br: Branch, node_id: str, mode: Mode) -> bool:
        """Питается ли точка КЗ исключительно через данное присоединение."""
        try:
            _, load_end, _ = self.net.orient(br, mode)
            return node_id in self.net.downstream_nodes(br, mode) or node_id == load_end
        except Exception:
            return False

    def protection_side_u(self, br: Branch, mode: Mode | None = None) -> float:
        """
        Номинальное напряжение физической стороны ТТ.

        Параметр ``mode`` оставлен для совместимости, но намеренно не влияет
        на результат: при реверсе питания установленный ТТ не меняет сторону.
        """
        return self.net.protection_side_u(br)
