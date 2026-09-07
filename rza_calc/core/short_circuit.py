# -*- coding: utf-8 -*-
"""
Расчёт токов короткого замыкания.

Схема замещения собирается в матрицу узловых проводимостей, обращается,
и собственное сопротивление узла Zkk = сопротивление относительно источника
(эквивалент Тевенена). Это даёт правильный результат при ЛЮБОЙ топологии,
включая параллельную работу трансформаторов через включённый СВ, — без
отдельной ветки кода на каждый случай.

Ветви нулевого сопротивления (СВ) объединяют узлы, а не получают
искусственно малое сопротивление: так матрица не теряет обусловленность.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .impedance import branch_impedance, stage_voltage
from .methodology import Methodology
from .model import (GRID, Branch, GeneratorBranch, Mode, Network, SourceBranch,
                    TieBranch, TransformerBranch)
from .trace import Step, Value, fmt, record

SQRT3 = math.sqrt(3.0)

#: Порог, ниже которого сопротивление ветви считается нулевым и её узлы
#: объединяются. Абсолютная величина в омах НА БАЗИСНОЙ СТУПЕНИ: после
#: приведения одно и то же физическое сопротивление на разных ступенях
#: выражается разными числами, поэтому порог относится именно к базису.
#: Значение переопределяется методикой (short_circuit.zero_impedance_ohm),
#: чтобы оно было видимым и объяснимым, а не зашитой константой.
DEFAULT_ZERO_IMPEDANCE_OHM = 1e-12


def zero_impedance_threshold(m: Methodology) -> float:
    """Порог объединения узлов, взятый из профиля методики."""
    try:
        value = float(m.k("short_circuit.zero_impedance_ohm"))
    except Exception:
        return DEFAULT_ZERO_IMPEDANCE_OHM
    if not math.isfinite(value) or value < 0:
        raise ShortCircuitStatusError(
            "INVALID_INPUT",
            "Порог нулевого сопротивления в методике должен быть конечным и ≥ 0.",
        )
    return value


class CurrentDistributionError(ValueError):
    """Ток через конкретную ветвь нельзя однозначно получить из модели."""


class ShortCircuitStatusError(ValueError):
    """Структурированная инженерная ошибка расчёта ТКЗ."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class NodeNotEnergizedError(KeyError, ShortCircuitStatusError):
    """Точка КЗ не связана с активным источником в выбранном режиме."""

    def __init__(self, node_id: str, mode_name: str):
        self.node_id = node_id
        self.mode_name = mode_name
        ShortCircuitStatusError.__init__(
            self,
            "NOT_ENERGIZED",
            f"Узел '{node_id}' не запитан в режиме «{mode_name}».",
        )


class _Union:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # GRID всегда остаётся представителем своей группы
            if rb == GRID:
                ra, rb = rb, ra
            self.p[rb] = ra


@dataclass
class ScResult:
    """Токи КЗ в одном узле, в одном режиме."""
    node_id: str
    node_name: str
    u_nom: float                      # кВ, класс узла — для подписи
    regime: str                       # 'max' | 'min'
    z_th: complex                     # Ом, приведено к ступени узла
    i3: float                         # кА, трёхфазное КЗ
    i2: float                         # кА, оценка двухфазного КЗ при Z2=Z1
    steps: list[Step] = field(default_factory=list)
    # Расчётное напряжение ступени точки КЗ. Именно по нему приводится ток
    # между ступенями; класс узла остаётся только подписью.
    u_stage: float = 0.0
    i3_complex: complex | None = None  # кА, комплексный ток при Uф с углом 0°
    #: Вклад каждого источника и генератора в ток точки, кА, комплексно и с
    #: направлением. Сумма вкладов равна полному току: это и проверка расчёта,
    #: и исходные данные для направленных защит и для протокола.
    source_contributions: dict[str, complex] = field(default_factory=dict)
    i2_is_approximation: bool = True
    i2_note: str = (
        "Оценка Iк(2) выполнена только для частного допущения Z2 = Z1. "
        "Сеть обратной последовательности ещё не построена."
    )

    @property
    def i_min(self) -> float:
        """Расчётный минимальный ток КЗ для проверки чувствительности."""
        return self.i2

    def step_for(self, which: str = "i3") -> list[Step]:
        """Шаг протокола, соответствующий запрошенному току."""
        key = "трёхфазного" if which == "i3" else "двухфазного"
        for st in self.steps:
            if key in st.what:
                return [st]
        return self.steps[-1:]

    def as_value(self, which: str = "i3") -> Value:
        v = getattr(self, which)
        st = self.steps[-1] if self.steps else None
        return Value(v, "кА", f"Iк({3 if which=='i3' else 2}) в узле «{self.node_name}»", st)


class ShortCircuitSolver:
    """Один экземпляр на пару (сеть, режим)."""

    def __init__(self, net: Network, mode: Mode, m: Methodology, u_base: float | None = None):
        self.net, self.mode, self.m = net, mode, m
        self.regime = mode.system
        srcs = [b for b in net.branches.values()
                if isinstance(b, (SourceBranch, GeneratorBranch))]
        if not srcs:
            raise ValueError("В схеме нет ни системы, ни генераторов — токи КЗ считать не от чего.")
        live = [b for b in srcs if mode.is_closed(b)]
        if not live:
            raise ValueError("В этом режиме не работает ни один источник: "
                             "все генераторы отключены и связи с системой нет.")
        # Базис — расчётное напряжение ступени, а не номинальный класс:
        # весь расчёт ведётся в средних напряжениях.
        self.u_base = (
            u_base if u_base is not None
            else stage_voltage(net.node(live[0].node_to), m)
        )
        if not math.isfinite(float(self.u_base)) or self.u_base <= 0:
            raise ShortCircuitStatusError(
                "INVALID_INPUT", "Базисное напряжение должно быть конечным и > 0."
            )
        self.zero_threshold = zero_impedance_threshold(m)
        self.numerical_warnings: list[str] = []
        self.condition_number: float | None = None
        self._build()

    # ---------- сборка матрицы ----------
    def _build(self) -> None:
        net, mode, m = self.net, self.mode, self.m
        live = net.energized_nodes(mode)
        branches = [b for b in net.active_branches(mode)
                    if b.node_from in live and b.node_to in live]

        # 1. объединяем узлы, соединённые ветвями нулевого сопротивления
        uf = _Union(list(live))
        for b in branches:
            if isinstance(b, TieBranch):
                uf.union(b.node_from, b.node_to)
                continue
            z_pre, _ = branch_impedance(net, b, m, self.u_base, self.regime)
            if z_pre is not None and abs(z_pre) < self.zero_threshold:
                uf.union(b.node_from, b.node_to)
        self._rep = {n: uf.find(n) for n in live}

        # 2. индексируем узлы (GRID — опорный, в матрицу не входит)
        reps = sorted({r for r in self._rep.values() if r != GRID})
        self._idx = {r: i for i, r in enumerate(reps)}
        n = len(reps)
        if n == 0:
            raise ValueError("В данном режиме от источника не запитан ни один узел.")

        # 3. матрица проводимостей
        Y = np.zeros((n, n), dtype=complex)
        self.branch_steps: dict[str, Step] = {}
        self.branch_z: dict[str, complex] = {}
        for b in branches:
            z, st = branch_impedance(net, b, m, self.u_base, self.regime)
            if st is not None:
                self.branch_steps[b.id] = st
            if z is None:
                continue
            if not math.isfinite(z.real) or not math.isfinite(z.imag):
                raise ShortCircuitStatusError(
                    "INVALID_INPUT",
                    f"Ветвь «{b.name}»: сопротивление содержит NaN/Infinity."
                )
            if abs(z) < self.zero_threshold:
                continue      # луч звезды с нулевым Uк — узлы уже объединены
            self.branch_z[b.id] = z
            a, c = self._rep[b.node_from], self._rep[b.node_to]
            if a == c:
                continue                      # ветвь замкнута сама на себя после объединения
            y = 1.0 / z
            if not math.isfinite(y.real) or not math.isfinite(y.imag):
                raise ShortCircuitStatusError(
                    "NUMERIC_FAILURE",
                    f"Ветвь «{b.name}»: проводимость переполнена."
                )
            if a != GRID and c != GRID:
                ia, ic = self._idx[a], self._idx[c]
                Y[ia, ia] += y; Y[ic, ic] += y
                Y[ia, ic] -= y; Y[ic, ia] -= y
            else:
                live_node = c if a == GRID else a
                Y[self._idx[live_node], self._idx[live_node]] += y

        if not np.isfinite(Y).all():
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE",
                "Матрица проводимостей содержит NaN/Infinity. Проверьте исходные данные."
            )
        try:
            self.condition_number = float(np.linalg.cond(Y))
            if not math.isfinite(self.condition_number):
                raise np.linalg.LinAlgError("неограниченное число обусловленности")
            if self.condition_number > 1.0e11:
                self.numerical_warnings.append(
                    "Матрица проводимостей плохо обусловлена: "
                    f"cond(Y)={self.condition_number:.6g}. Результат требует проверки."
                )
            self._Z = np.linalg.inv(Y)
        except np.linalg.LinAlgError as e:
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE",
                "Схема замещения вырождена: вероятно, часть узлов не связана с источником "
                "или задан участок без сопротивления. Проверьте топологию и состояние "
                f"аппаратов в режиме «{self.mode.name}»."
            ) from e
        if not np.isfinite(self._Z).all():
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE",
                "Решение матрицы проводимостей содержит NaN/Infinity."
            )

    # ---------- результат ----------
    def z_th(self, node_id: str) -> complex:
        """Эквивалентное сопротивление до узла, приведённое к БАЗИСНОЙ ступени."""
        rep = self._rep.get(node_id)
        if rep is None:
            raise NodeNotEnergizedError(node_id, self.mode.name)
        if rep == GRID:
            return 0.0 + 0.0j
        i = self._idx[rep]
        return complex(self._Z[i, i])

    def _z(self, node_id: str, k_node: str) -> complex:
        """Взаимное сопротивление Zbus[node, k] в базисных единицах."""
        a, k = self._rep.get(node_id), self._rep.get(k_node)
        if a is None:
            raise NodeNotEnergizedError(node_id, self.mode.name)
        if k is None:
            raise NodeNotEnergizedError(k_node, self.mode.name)
        if a == GRID or k == GRID:
            return 0.0 + 0.0j
        return complex(self._Z[self._idx[a], self._idx[k]])

    def distribution_factor_complex(
        self, branch: Branch, fault_node: str
    ) -> complex:
        """Ориентированный комплексный коэффициент тока ветви.

        Положительное направление совпадает с ``node_from -> node_to``.
        При радиальном питании коэффициент равен единице, при двух одинаковых
        параллельных элементах — половине. Считается из той же обращённой
        матрицы, что и токи КЗ: d = (Z[i,k] − Z[j,k]) / z(ветви).
        """
        z_br = self.branch_z.get(branch.id)
        if z_br is None or abs(z_br) < self.zero_threshold:
            # Для идеального СВ ток можно определить только если он является
            # единственным путём к точке КЗ. В кольце после объединения узлов
            # распределение по идеальной перемычке математически неоднозначно.
            _, load_end, ring = self.net.orient(branch, self.mode)
            radial_zone = self.net.downstream_nodes(branch, self.mode)
            if not ring and (fault_node == load_end or fault_node in radial_zone):
                return 1.0 + 0.0j
            if not ring:
                return 0.0 + 0.0j
            raise CurrentDistributionError(
                f"Ток через «{branch.name}» не определён: ветвь имеет нулевое "
                "сопротивление и находится в замкнутом контуре. Задайте сопротивление "
                "соединения либо используйте расчёт токораспределения по аппаратам."
            )
        a, c = self._rep[branch.node_from], self._rep[branch.node_to]
        if a == c:
            return 0.0 + 0.0j
        return (
            self._z(branch.node_from, fault_node)
            - self._z(branch.node_to, fault_node)
        ) / z_br

    def distribution_factor(self, branch: Branch, fault_node: str) -> float:
        """Модуль коэффициента для совместимости старых расчётов РЗА."""
        return abs(self.distribution_factor_complex(branch, fault_node))

    def electrical_point(self, node_id: str) -> str:
        """Идентификатор ЭЛЕКТРИЧЕСКОЙ точки, которой принадлежит узел.

        Узлы, соединённые ветвями нулевого сопротивления — идеальным
        выключателем, разъединителем, лучом звезды с нулевым Uк, — это один и
        тот же электрический узел: ток КЗ в них совпадает до последнего бита.
        Матрица проводимостей объединяет их ещё до сборки, и здесь то же
        объединение становится доступно снаружи.

        Нужен там, где решается «одна это точка или разные»: сравнение по ID
        узла на такой вопрос не отвечает и порождает проверки-двойники
        (дефект AUD-PROT-011). Для узла, не участвующего в данном режиме,
        возвращается он сам: выдумывать ему представителя нельзя.
        """
        return self._rep.get(node_id, node_id)

    def distribution_magnitude(self, branch: Branch, fault_node: str) -> float:
        return self.distribution_factor(branch, fault_node)

    def fault_current_complex(self, node_id: str) -> complex:
        """Комплексный ток трёхфазного КЗ, кА, при угле Uф до КЗ = 0°."""
        node = self.net.node(node_id)
        physical_stage = stage_voltage(node, self.m)
        u_fault = node.prefault_voltage_kv or physical_stage
        z_base = self.z_th(node_id)
        z_stage = z_base * (physical_stage / self.u_base) ** 2
        if abs(z_stage) <= 0 or not all(
            math.isfinite(value)
            for value in (z_stage.real, z_stage.imag, float(u_fault))
        ):
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE",
                "Невозможно получить комплексный ток: некорректны U или Z точки КЗ.",
            )
        current = (u_fault / SQRT3) / z_stage
        if not math.isfinite(current.real) or not math.isfinite(current.imag):
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE", "Комплексный ток КЗ содержит NaN/Infinity."
            )
        return complex(current)

    def branch_current_complex(self, branch: Branch, fault_node: str) -> complex:
        """Ориентированный комплексный ток ветви при КЗ, кА."""
        return (
            self.fault_current_complex(fault_node)
            * self.distribution_factor_complex(branch, fault_node)
        )

    def fault_at(self, node_id: str, spec):
        """Explicit three-phase, phase-phase or grounded fault at one node.

        Sequence data is loaded lazily. Missing zero-sequence parameters do
        not prevent the existing three-phase/protection path from running.
        """
        from .sequence_network import SequenceFaultSolver
        return SequenceFaultSolver(self).fault_at(node_id, spec)

    def at(self, node_id: str) -> ScResult:
        net, m = self.net, self.m
        node = net.node(node_id)
        physical_stage = stage_voltage(node, m)
        u_fault = node.prefault_voltage_kv or physical_stage

        z_base = self.z_th(node_id)
        z_stage = z_base * (physical_stage / self.u_base) ** 2

        if not math.isfinite(z_stage.real) or not math.isfinite(z_stage.imag):
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE", "Эквивалентное сопротивление содержит NaN/Infinity."
            )
        if abs(z_stage) <= 0:
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE", "Эквивалентное сопротивление точки КЗ равно нулю."
            )
        i3_complex = (u_fault / SQRT3) / z_stage
        i3 = abs(i3_complex)                              # кВ / Ом = кА
        if not (
            math.isfinite(i3)
            and math.isfinite(i3_complex.real)
            and math.isfinite(i3_complex.imag)
        ):
            raise ShortCircuitStatusError(
                "NUMERIC_FAILURE", "Расчёт тока КЗ дал NaN/Infinity."
            )
        # Только точное частное соотношение при Z2=Z1. Это НЕ полноценный
        # расчёт сети обратной последовательности и помечается в результате.
        k2 = SQRT3 / 2.0
        i2 = i3 * k2

        reg_ru = "максимальном" if self.regime == "max" else "минимальном"
        chain = self._path_steps(node_id)

        st3 = Step(
            what=f"Ток трёхфазного КЗ на «{node.name}»",
            why=("Верхняя граница тока в точке КЗ. От неё отстраивается токовая отсечка "
                 "и проверяется термическая стойкость."),
            given={
                "Режим": f"{self.mode.name} (система в {reg_ru} режиме)",
                "Uрасч до КЗ": f"{fmt(u_fault)} кВ",
                "Класс узла": f"{fmt(node.u_nom)} кВ",
                "Uрасч ступени": f"{fmt(physical_stage)} кВ",
                "Zрез": f"{fmt(z_stage)} Ом  (|Z| = {fmt(abs(z_stage))} Ом)",
            },
            formula="Iк(3) = Uрасч / (√3 · |Zрез|)",
            substitution=f"Iк(3) = {fmt(u_fault)} / (1.732 · {fmt(abs(z_stage))}) = {fmt(i3)} кА",
            result=f"Iк(3) = {fmt(i3)} кА = {fmt(i3*1000)} А",
            source=self.m.cite("short_circuit"),
            note=("Zрез получено обращением матрицы узловых проводимостей всей схемы "
                  "в данном режиме, поэтому параллельные пути (включённый СВ, "
                  "параллельная работа трансформаторов) учтены автоматически."),
            children=chain,
        )
        st2 = Step(
            what=f"Ток двухфазного КЗ на «{node.name}»",
            why=("Минимальный ток повреждения в точке — по нему проверяется "
                 "чувствительность максимальных токовых защит."),
            given={
                "Iк(3)": f"{fmt(i3)} кА",
                "Допущение": "Z2 = Z1",
                "k(2)": f"√3/2 = {fmt(k2)}",
            },
            formula="Оценка Iк(2) = (√3/2) · Iк(3), только при Z2 = Z1",
            substitution=f"Iк(2) = {fmt(k2)} · {fmt(i3)} = {fmt(i2)} кА",
            result=f"Iк(2) = {fmt(i2)} кА = {fmt(i2*1000)} А",
            source=self.m.cite("short_circuit.k_two_phase"),
            note=(
                "Это предварительная оценка, а не общий расчёт двухфазного КЗ. "
                "До реализации сети обратной последовательности результат нельзя "
                "использовать как подтверждённый промышленный расчёт."
            ),
            children=[st3],
        )
        return ScResult(
            node_id, node.name, node.u_nom, self.regime, z_stage, i3, i2,
            [st3, st2], i3_complex=i3_complex, u_stage=physical_stage,
            source_contributions=self.source_contributions(node_id),
        )

    def source_contributions(self, node_id: str) -> dict[str, complex]:
        """Комплексный вклад каждого источника и генератора в ток точки, кА.

        Вклад считается из того же токораспределения, что и токи ветвей,
        поэтому сумма вкладов равна полному току точки; расхождение означало бы
        ошибку в матрице, а не в округлении.

        Знак. ``distribution_factor_complex`` описывает ток в конвенции
        инжекции — от точки повреждения В сеть, поэтому для ветви, питающей
        КЗ, он отрицателен. Вклад источника — величина, ВТЕКАЮЩАЯ в точку, то
        есть с обратным знаком. Без этой поправки сумма вкладов равнялась бы
        полному току с противоположной фазой.
        """
        total = self.fault_current_complex(node_id)
        out: dict[str, complex] = {}
        for branch in self.net.active_branches(self.mode):
            if not isinstance(branch, (SourceBranch, GeneratorBranch)):
                continue
            try:
                share = self.distribution_factor_complex(branch, node_id)
            except (CurrentDistributionError, ShortCircuitStatusError):
                continue
            out[branch.id] = -total * share
        return out

    def _path_steps(self, node_id: str) -> list[Step]:
        """Шаги по сопротивлениям элементов, ВЛИЯЮЩИХ на ток в этой точке.

        Раньше сюда попадали все ветви схемы, и протокол выглядел так, будто в
        результат последовательно вошли все перечисленные элементы — в кольце
        или при нескольких источниках это вводило в заблуждение. Теперь
        элемент попадает в протокол, только если через него при данном КЗ
        действительно течёт ток. Ветвь с неопределённым токораспределением
        показывается: умалчивать о ней хуже, чем показать лишнее.
        """
        out: list[Step] = []
        for branch_id, step in self.branch_steps.items():
            branch = self.net.branches.get(branch_id)
            if branch is None:
                continue
            try:
                share = self.distribution_factor_complex(branch, node_id)
            except CurrentDistributionError:
                out.append(step)
                continue
            except ShortCircuitStatusError:
                continue
            if abs(share) > 1e-9:
                out.append(step)
        return out

    def behind_transformer(self, tr: TransformerBranch, referred_to_kv: float | None = None) -> ScResult:
        """
        Ток КЗ за трансформатором (напр. на шинах 0,4 кВ КТП), при необходимости
        пересчитанный на ступень, где стоит защита, — именно от него отстраивается ТО.
        """
        res = self.at(tr.node_to)
        if referred_to_kv is None:
            return res
        # Приведение тока между ступенями выполняется по расчётным напряжениям
        # ступеней, как и приведение сопротивлений.
        u_lv = res.u_stage or self.net.node(tr.node_to).u_nom
        u_hv = referred_to_kv
        if u_lv <= 0 or u_hv <= 0:
            raise ShortCircuitStatusError(
                "INVALID_INPUT", "Напряжения для приведения тока должны быть > 0."
            )
        k = u_lv / u_hv
        i3 = res.i3 * k
        i2 = res.i2 * k
        st = Step(
            what=f"Ток КЗ за трансформатором «{tr.name}», приведённый к {fmt(referred_to_kv)} кВ",
            why=("Защита на стороне ВН видит именно этот ток. От него отстраивается "
                 "токовая отсечка, чтобы она не срабатывала при КЗ за трансформатором."),
            given={f"Iк(3) на {fmt(res.u_nom)} кВ": f"{fmt(res.i3*1000)} А",
                   "Uрасч НН": f"{fmt(u_lv)} кВ", "Uрасч ВН": f"{fmt(u_hv)} кВ"},
            formula="Iк.вн = Iк.нн · Uрасч.нн / Uрасч.вн",
            substitution=f"Iк.вн = {fmt(res.i3*1000)} · {fmt(u_lv)} / {fmt(u_hv)} = {fmt(i3*1000)} А",
            result=f"Iк(3), приведённый к {fmt(referred_to_kv)} кВ = {fmt(i3*1000)} А",
            note=("Пересчёт тока по согласованным физическим ступеням напряжения. Для трансформаторов со "
                  "схемой соединения Д/Ун ток при однофазном КЗ на стороне 0,4 кВ "
                  "распределяется по фазам ВН неравномерно — для проверки "
                  "чувствительности к однофазным КЗ этого расчёта недостаточно."),
            children=res.steps,
        )
        return ScResult(
            res.node_id, res.node_name, referred_to_kv, res.regime,
            res.z_th, i3, i2, [st],
            i3_complex=(res.i3_complex * k if res.i3_complex is not None else None),
        )


def solve_all(net: Network, m: Methodology) -> dict[str, ShortCircuitSolver]:
    """Решатель на каждый заданный режим — сразу все, как и требуется."""
    out: dict[str, ShortCircuitSolver] = {}
    for mid, mode in net.modes.items():
        out[mid] = ShortCircuitSolver(net, mode, m)
    return out
