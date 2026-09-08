# -*- coding: utf-8 -*-
"""
Модель подстанции.

Сеть описывается как граф: УЗЛЫ (шины и расчётные точки) + ВЕТВИ (элементы
с сопротивлением). Это не «дерево объектов ради дерева», а именно расчётная
схема замещения — поэтому параллельная работа трансформаторов через СВ
получается сама собой, без особых случаев в коде.

Защита ставится не «на объект», а на ВЕТВЬ: у ветви есть выключатель, ТТ и
терминал. Отсюда программа знает, кто ниже кого по сети.
"""
from __future__ import annotations

import math
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

GRID = "GRID"  # узел бесконечной мощности (ЭДС системы); всегда присутствует


# ──────────────────────────────────────────────────────────────────────────
#  Узлы
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Node:
    """Электрический узел: секция шин, точка присоединения, шины 0,4 кВ КТП."""
    id: str
    name: str
    u_nom: float                       # кВ, номинальное напряжение ступени
    kind: Literal["bus", "point"] = "bus"
    section: str | None = None         # обозначение секции (1 СШ / 2 СШ)
    note: str = ""
    # Явные расчётные напряжения отделены от класса сети ``u_nom``.
    calculation_base_kv: float | None = None
    prefault_voltage_kv: float | None = None


# ──────────────────────────────────────────────────────────────────────────
#  Ветви
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class ProtectionSettings:
    """Что стоит на присоединении и какие выдержки заданы принудительно."""
    mtz: bool = True
    to: bool = True
    ozz: bool = False
    t_mtz: float | None = None      # с; None → назначается автоматически по селективности
    t_to: float | None = None
    t_ozz: float | None = None
    to_reach: Literal["line_end", "behind_transformer"] = "line_end"
    k_szp: float | None = None      # индивидуальный коэффициент самозапуска
    i_scale: list[float] | None = None   # шкала вторичных уставок терминала
    backup_through_transformer: bool | None = None  # None → как в профиле методики
    level: int = 0                  # уровень в цепочке; заполняется автоматически


@dataclass
class Branch:
    """Базовая ветвь. Наследники добавляют параметры для схемы замещения."""
    id: str
    name: str
    node_from: str
    node_to: str
    kind: str = "branch"
    switchable: bool = False           # можно ли отключить в режиме
    normally_closed: bool = True       # состояние по умолчанию
    switch_with: str | None = None     # следовать состоянию другой ветви (обмотки одного аппарата)
    # оснащение присоединения (если есть — на ветвь можно вешать защиты)
    ct_ratio: tuple[float, float] | None = None   # (первичный, вторичный), А
    ct_node: str | None = None          # физический узел установки ТТ; не зависит от потока
    ct_accuracy: str = ""
    breaker_t_off: float | None = None            # с, полное время отключения
    terminal: str = ""                            # тип терминала РЗА
    prot: ProtectionSettings = field(default_factory=ProtectionSettings)
    note: str = ""
    # Explicit sequence equivalents.  Absolute impedances are in ohms on
    # sequence_reference_kv, not on the solver's common calculation base.
    # The zero-sequence equivalent INCLUDES every applicable 3*Zn term;
    # the solver must never add a neutral impedance to it a second time.
    r2_ohm: float | None = field(default=None, kw_only=True)
    x2_ohm: float | None = field(default=None, kw_only=True)
    r0_ohm: float | None = field(default=None, kw_only=True)
    x0_ohm: float | None = field(default=None, kw_only=True)
    sequence_reference_kv: float | None = field(default=None, kw_only=True)
    negative_sequence_equal_positive: bool = field(default=False, kw_only=True)
    # None means not supplied, not an ungrounded or an ideal connection.
    zero_sequence_connection: str | None = field(default=None, kw_only=True)
    # From node_from to node_to; meaningful only for TransformerBranch.
    sequence_phase_shift_deg: float | None = field(default=None, kw_only=True)
    # Source/generator sequence equivalents may differ by calculation system.
    # Missing or None members inherit their corresponding common value above.
    sequence_by_system: dict[str, dict[str, float | None]] = field(default_factory=dict, kw_only=True)
    # Explicit editor provenance only; absence preserves historical inputs.
    parameter_provenance: dict[str, dict[str, Any]] = field(default_factory=dict, kw_only=True)

    @property
    def has_protection_point(self) -> bool:
        return self.ct_ratio is not None

    @property
    def ct_k(self) -> float:
        if not self.ct_ratio:
            raise ValueError(f"У ветви «{self.name}» не задан ТТ — вторичную уставку посчитать нельзя.")
        return float(self.ct_ratio[0]) / float(self.ct_ratio[1])


@dataclass
class SourceBranch(Branch):
    """Питающая система. Ветвь GRID → шины высшего напряжения."""
    kind: str = "source"
    # Мощность КЗ или ток КЗ — задаётся что-то одно, для max и min режима
    s_kz_max: float | None = None      # МВ·А
    s_kz_min: float | None = None      # МВ·А
    i_kz_max: float | None = None      # кА, трёхфазный на шинах ВН
    i_kz_min: float | None = None      # кА
    x_r_ratio: float | None = None     # X/R системы; None → чисто индуктивная
    # Если одновременно сохранены Sкз и Iкз, пользователь обязан явно выбрать
    # первичный способ. Без этого неоднозначный источник блокирует расчёт.
    input_mode_max: Literal["power", "current"] | None = None
    input_mode_min: Literal["power", "current"] | None = None
    voltage_kv: float | None = None


@dataclass
class GeneratorBranch(Branch):
    """
    Генератор (ГТГ, ГПА, дизель) — ЭДС за сверхпереходным сопротивлением.

    В схеме это ветвь от узла GRID к шинам генераторного напряжения. Несколько
    машин — несколько таких ветвей; их параллельная работа получается сама,
    потому что решатель обращает матрицу всей схемы. Состав работающих машин
    задаётся режимом, как и любой другой коммутационный аппарат.
    """
    kind: str = "generator"
    s_nom: float = 0.0                 # кВ·А, полная номинальная мощность
    p_nom: float | None = None         # МВт, активная — для подписи в интерфейсе
    cos_phi: float = 0.8
    u_nom: float = 0.0                 # кВ, генераторное напряжение
    xd2: float = 0.15                  # x"d, о.е. — сверхпереходное сопротивление
    r_pu: float | None = None          # активное сопротивление обмотки, о.е.
    switchable: bool = True
    normally_closed: bool = True

    @property
    def s_from_p(self) -> float:
        """Полная мощность: задана напрямую либо получена из P и cosφ."""
        if self.s_nom:
            return self.s_nom
        if self.p_nom and self.cos_phi:
            return self.p_nom * 1000.0 / self.cos_phi
        return 0.0


@dataclass
class Transformer3W:
    """
    Трёхобмоточный трансформатор или автотрансформатор 220/110/35/10.

    Хранится как описание, а в граф разворачивается тремя ветвями к общему
    внутреннему узлу — классическая звезда. Uк лучевые получаются из
    паспортных Uк(вн-сн), Uк(вн-нн), Uк(сн-нн).
    """
    id: str
    name: str
    node_hv: str
    node_mv: str
    node_lv: str
    s_nom: float                       # кВ·А
    u_hv: float
    u_mv: float
    u_lv: float
    uk_hm: float                       # % , ВН–СН
    uk_hl: float                       # % , ВН–НН
    uk_ml: float                       # % , СН–НН
    p_k: float | None = None           # кВт
    group: str = ""
    i_inrush_ratio: float | None = None
    switchable: bool = True
    normally_closed: bool = True
    ct_ratio: tuple[float, float] | None = None
    ct_node: str | None = None
    breaker_t_off: float | None = None
    terminal: str = ""
    prot: "ProtectionSettings | None" = None
    note: str = ""
    parameter_provenance: dict[str, dict[str, Any]] = field(default_factory=dict, kw_only=True)

    def leg_uk(self) -> tuple[float, float, float]:
        """Uк лучей звезды: (ВН, СН, НН), %."""
        uk_h = 0.5 * (self.uk_hm + self.uk_hl - self.uk_ml)
        uk_m = 0.5 * (self.uk_hm + self.uk_ml - self.uk_hl)
        uk_l = 0.5 * (self.uk_hl + self.uk_ml - self.uk_hm)
        return uk_h, uk_m, uk_l

    @property
    def star_node_id(self) -> str:
        """ID служебного узла звезды в расчётной схеме."""
        return f"{self.id}__star"

    @property
    def branch_ids(self) -> tuple[str, str, str]:
        """ID трёх расчётных лучей, принадлежащих одному аппарату."""
        return self.id, f"{self.id}_mv", f"{self.id}_lv"


@dataclass
class TransformerBranch(Branch):
    """Двухобмоточный силовой трансформатор."""
    kind: str = "transformer"
    s_nom: float = 0.0                 # кВ·А
    u_hv: float = 0.0                  # кВ
    u_lv: float = 0.0                  # кВ
    uk: float = 0.0                    # %, напряжение короткого замыкания
    p_k: float | None = None           # кВт, потери КЗ (для активной составляющей)
    group: str = ""                    # группа соединения, напр. "Д/Ун-11"
    z_loop: float | None = None        # Ом, петля фаза-нуль по каталогу (для КЗ(1) на 0,4 кВ)
    i_inrush_ratio: float | None = None  # кратность броска тока намагничивания к Iном
    # True только для автоматически созданного служебного луча звезды 3W.
    internal_star_leg: bool = False
    switchable: bool = True


@dataclass
class LineBranch(Branch):
    """Кабельная или воздушная линия."""
    kind: str = "line"
    line_type: Literal["cable", "overhead"] = "cable"
    length_km: float = 0.0
    brand: str = ""                    # марка, напр. "АПвПу"
    section_mm2: float | None = None
    material: Literal["Al", "Cu"] = "Al"
    n_parallel: int = 1
    r0: float | None = None            # Ом/км — если задано, перекрывает справочник
    x0: float | None = None            # Ом/км
    # Canonical sequence names.  Historical r0/x0 above remain POSITIVE
    # sequence per-km values and must never be interpreted as zero sequence.
    r2_ohm_per_km: float | None = field(default=None, kw_only=True)
    x2_ohm_per_km: float | None = field(default=None, kw_only=True)
    r0_ohm_per_km: float | None = field(default=None, kw_only=True)
    x0_ohm_per_km: float | None = field(default=None, kw_only=True)
    ic_per_km: float | None = None     # А/км, ёмкостный ток ОЗЗ (по каталогу)
    # Канонический проект может хранить конструкцию, которую старое ядро пока
    # не умеет честно свести к одному LineBranch. DTO остаётся доступным для
    # просмотра/сохранения, но расчёт обязан завершиться явной ошибкой.
    calculation_block_reason: str | None = None


@dataclass
class TieBranch(Branch):
    """Секционный выключатель. Сопротивление принимается нулевым."""
    kind: str = "tie"
    switchable: bool = True
    normally_closed: bool = False


@dataclass
class Load:
    """Нагрузка, приложенная к узлу."""
    id: str
    name: str
    node: str
    p_kw: float = 0.0
    cos_phi: float = 0.9
    k_use: float = 1.0                 # коэффициент использования / одновременности
    k_szp: float | None = None         # индивидуальный коэффициент самозапуска
    motor_share: float | None = None   # доля двигательной нагрузки, 0…1
    parameter_provenance: dict[str, dict[str, Any]] = field(default_factory=dict, kw_only=True)

    @property
    def s_kva(self) -> float:
        return self.p_kw / self.cos_phi if self.cos_phi else 0.0


# ──────────────────────────────────────────────────────────────────────────
#  Режимы сети — первоклассная сущность, а не «фича версии 0.8»
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Mode:
    """
    Режим = состояние коммутационных аппаратов + режим системы.
    Любой расчёт в ядре — функция от (Network, Mode). Без исключений.
    """
    id: str
    name: str
    states: dict[str, bool] = field(default_factory=dict)   # branch_id → включён
    system: Literal["max", "min"] = "max"
    description: str = ""
    # Sparse service-state map for branches and loads.  It is deliberately
    # separate from switch positions: CLOSED equipment may still be taken out
    # of service, and an IN_SERVICE line need not be a switching apparatus.
    availability: dict[str, bool] = field(default_factory=dict)
    # Derived, explicit operating inputs. Empty preserves historical DTO identity.
    operating_parameters: dict[str, Any] = field(default_factory=dict, kw_only=True)

    def is_available(self, object_id: str) -> bool:
        return bool(self.availability.get(object_id, True))

    def is_closed(self, branch: Branch) -> bool:
        """Состояние присоединения в режиме.

        Явно заданное состояние самой ветви сильнее привязки к головному
        аппарату: так обмотку трёхобмоточного трансформатора можно вывести
        отдельно, а по умолчанию все три стороны по-прежнему следуют за ним.
        """
        if not self.is_available(branch.id):
            return False
        if branch.id in self.states:
            return bool(self.states[branch.id])
        key = branch.switch_with or branch.id
        if branch.switch_with is None and not branch.switchable:
            return True
        return self.states.get(key, branch.normally_closed)


SWITCH_PREFIX = "SW"


def switch_ends(branch: "Branch") -> tuple[str, ...]:
    """Стороны присоединения, на которых стоит выключатель.

    Линия и трансформатор коммутируются с двух сторон, ввод, СВ и генератор —
    с одной. Внешняя система выключателя в модели не имеет.
    """
    if not getattr(branch, "switchable", False):
        return ()
    if isinstance(branch, (LineBranch, TransformerBranch)):
        return ("from", "to")
    if isinstance(branch, (TieBranch, GeneratorBranch)):
        return ("from",)
    return ()


def switch_ids(branch: "Branch") -> tuple[str, ...]:
    """ID выключателей присоединения.

    Результат зависит только от ID ветви, её типа и признака ``switchable``,
    поэтому он запоминается на самом объекте. Кэш проверяется по этим же трём
    величинам, то есть изменение любой из них немедленно пересчитывает ответ:
    сборка строк выполнялась сотни тысяч раз за один расчёт и была одной из
    самых дорогих операций топологического слоя.
    """
    stamp = (branch.id, type(branch), getattr(branch, "switchable", False))
    cached = branch.__dict__.get("_switch_ids_cache")
    if cached is not None and cached[0] == stamp:
        return cached[1]
    value = tuple(f"{SWITCH_PREFIX}:{branch.id}:{end}" for end in switch_ends(branch))
    branch.__dict__["_switch_ids_cache"] = (stamp, value)
    return value


def parse_switch_id(switch_id: str) -> tuple[str, str] | None:
    """(id ветви, сторона) из идентификатора выключателя."""
    parts = switch_id.split(":")
    if len(parts) != 3 or parts[0] != SWITCH_PREFIX:
        return None
    return parts[1], parts[2]


# ──────────────────────────────────────────────────────────────────────────
#  Сеть
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Network:
    name: str = "Проект"
    nodes: dict[str, Node] = field(default_factory=dict)
    branches: dict[str, Branch] = field(default_factory=dict)
    # Физические трёхобмоточные аппараты хранятся отдельно. В ``branches``
    # остаются только их расчётные лучи, автоматически создаваемые ниже.
    transformers3w: dict[str, Transformer3W] = field(default_factory=dict)
    loads: dict[str, Load] = field(default_factory=dict)
    modes: dict[str, Mode] = field(default_factory=dict)
    # режим нейтрали по ступеням напряжения: "10" -> isolated|compensated|earthed
    neutral: dict[str, str] = field(default_factory=dict)

    NEUTRAL_RU = {"isolated": "изолированная", "compensated": "компенсированная",
                  "earthed": "глухозаземлённая", "resistor": "заземлённая через резистор"}

    def neutral_mode(self, u_nom: float) -> str:
        key = str(int(u_nom)) if float(u_nom).is_integer() else str(u_nom)
        return self.neutral.get(key, "isolated")

    def neutral_mode_ru(self, u_nom: float) -> str:
        return self.NEUTRAL_RU.get(self.neutral_mode(u_nom), self.neutral_mode(u_nom))

    # ---------- сборка ----------
    def add_node(self, node: Node) -> Node:
        if node.id == GRID:
            raise ValueError("ID 'GRID' зарезервирован для служебного узла источника.")
        if node.id in self.nodes:
            raise ValueError(f"Узел с ID '{node.id}' уже существует.")
        self.nodes[node.id] = node
        return node

    def add_branch(self, br: Branch) -> Branch:
        if br.id in self.branches:
            raise ValueError(f"Ветвь с ID '{br.id}' уже существует.")
        for n in (br.node_from, br.node_to):
            if n != GRID and n not in self.nodes:
                raise KeyError(f"Ветвь «{br.name}»: узел '{n}' не описан в схеме.")
        if br.ct_ratio is not None:
            # Старые проекты считали, что защита находится у node_from. Фиксируем
            # эту сторону один раз, чтобы при реверсе питания ТТ не «переезжал».
            if br.ct_node is None:
                br.ct_node = br.node_to if br.node_from == GRID else br.node_from
            if br.ct_node == GRID or br.ct_node not in (br.node_from, br.node_to):
                raise ValueError(
                    f"Ветвь «{br.name}»: узел установки ТТ '{br.ct_node}' должен быть "
                    "одним из концов ветви и не может быть GRID."
                )
        self.branches[br.id] = br
        return br

    def add_transformer3w(self, t: "Transformer3W") -> list[Branch]:
        """Развернуть трёхобмоточный трансформатор в звезду из трёх ветвей."""
        if t.id in self.transformers3w:
            raise ValueError(f"Трёхобмоточный трансформатор с ID '{t.id}' уже существует.")
        missing = [n for n in (t.node_hv, t.node_mv, t.node_lv) if n not in self.nodes]
        if missing:
            raise KeyError(
                f"Трансформатор «{t.name}»: не описаны узлы {', '.join(missing)}."
            )
        star_id = t.star_node_id
        if star_id in self.nodes:
            raise ValueError(
                f"Трансформатор «{t.name}»: служебный узел '{star_id}' уже существует."
            )
        occupied = [bid for bid in t.branch_ids if bid in self.branches]
        if occupied:
            raise ValueError(
                f"Трансформатор «{t.name}»: ID расчётных ветвей уже заняты: "
                f"{', '.join(occupied)}."
            )
        if t.ct_ratio is not None:
            if t.ct_node is None:
                t.ct_node = t.node_hv
            if t.ct_node not in (t.node_hv, t.node_mv, t.node_lv):
                raise ValueError(
                    f"Трансформатор «{t.name}»: узел установки ТТ '{t.ct_node}' "
                    "не относится ни к одной обмотке."
                )

        self.transformers3w[t.id] = t
        self.add_node(Node(star_id, f"{t.name}, точка звезды", t.u_hv, kind="point"))
        uk_h, uk_m, uk_l = t.leg_uk()
        legs: list[Branch] = []
        spec = (("", t.node_hv, star_id, uk_h, t.u_hv, "ВН", t.node_hv),
                ("_mv", star_id, t.node_mv, uk_m, t.u_hv, "СН", t.node_mv),
                ("_lv", star_id, t.node_lv, uk_l, t.u_hv, "НН", t.node_lv))
        for suffix, a, b, uk, u_ref, side, physical_node in spec:
            has_ct = t.ct_ratio is not None and t.ct_node == physical_node
            br = TransformerBranch(
                id=t.id + suffix,
                name=f"{t.name} ({side})" if suffix else t.name,
                node_from=a, node_to=b,
                s_nom=t.s_nom, u_hv=u_ref,
                u_lv=t.u_mv if side == "СН" else (t.u_lv if side == "НН" else t.u_hv),
                uk=uk, p_k=t.p_k if not suffix else None,
                group=t.group, i_inrush_ratio=t.i_inrush_ratio,
                # У каждой обмотки свой выключатель, поэтому луч коммутируем.
                # По умолчанию он следует за головной ветвью (снятие напряжения
                # с трансформатора выводит все три стороны), но режим может
                # задать состояние конкретной обмотки явно.
                switchable=t.switchable,
                normally_closed=t.normally_closed,
                switch_with=None if not suffix else t.id,
                ct_ratio=t.ct_ratio if has_ct else None,
                ct_node=t.ct_node if has_ct else None,
                breaker_t_off=t.breaker_t_off,
                terminal=t.terminal if has_ct else "",
                parameter_provenance=deepcopy(t.parameter_provenance),
                internal_star_leg=True,
                note=("Луч звезды трёхобмоточного трансформатора. Отрицательное Uк "
                      "у среднего луча — нормально: это следствие пересчёта паспортных "
                      "Uк и не является ошибкой данных." if uk < 0 else t.note),
            )
            if has_ct and t.prot is not None:
                br.prot = t.prot
            self.add_branch(br)
            legs.append(br)
        return legs

    def add_load(self, ld: Load) -> Load:
        if ld.id in self.loads:
            raise ValueError(f"Нагрузка с ID '{ld.id}' уже существует.")
        if ld.node not in self.nodes:
            raise KeyError(f"Нагрузка «{ld.name}»: узел '{ld.node}' не описан в схеме.")
        self.loads[ld.id] = ld
        return ld

    def add_mode(self, m: Mode) -> Mode:
        if m.id in self.modes:
            raise ValueError(f"Режим с ID '{m.id}' уже существует.")
        self.modes[m.id] = m
        return m

    # ---------- кэш топологии ----------
    #
    # Топологические запросы (`adjacency`, `orient`, `downstream_nodes` и др.)
    # вызываются для каждой защиты, каждой точки зоны и каждого режима, поэтому
    # за один расчёт граф смежности пересобирался тысячи раз. Ответы
    # запоминаются по ПОЛНОЙ подписи состояния: любое изменение состава ветвей,
    # их концов, признаков коммутации или положений аппаратов в режиме даёт
    # другой ключ, поэтому устаревший ответ вернуться не может. Это осознанно
    # дороже счётчика ревизий, зато не зависит от того, кто и где изменил
    # объект «на месте».
    _TOPOLOGY_CACHE_LIMIT = 8

    def _topology_key(self, mode: Mode) -> tuple:
        return (
            mode.id,
            tuple(sorted(mode.states.items())),
            tuple(sorted(mode.availability.items())),
            tuple(
                (
                    b.id,
                    b.node_from,
                    b.node_to,
                    getattr(b, "switchable", False),
                    getattr(b, "normally_closed", True),
                    b.switch_with,
                )
                for b in self.branches.values()
            ),
            tuple(self.nodes),
        )

    @contextmanager
    def frozen_topology(self):
        """Заявить, что внутри блока сеть не изменяется.

        Только на это время подпись состояния вычисляется один раз на режим,
        а не на каждый топологический запрос. Расчёт (`engine.run`) сеть не
        меняет, поэтому заявление истинно. Вне блока подпись считается заново
        при каждом обращении, то есть поведение по умолчанию остаётся
        безопасным для редактора, который сеть меняет.
        """
        outer = self.__dict__.get("_frozen_keys")
        if outer is not None:
            yield self
            return
        self.__dict__["_frozen_keys"] = {}
        try:
            yield self
        finally:
            self.__dict__.pop("_frozen_keys", None)

    def _topology_cache(self, mode: Mode) -> dict:
        """Слот кэша, соответствующий текущему состоянию сети и режима."""
        store = self.__dict__.get("_topology_store")
        if store is None:
            store = {}
            self.__dict__["_topology_store"] = store
        frozen = self.__dict__.get("_frozen_keys")
        if frozen is None:
            key = self._topology_key(mode)
        else:
            key = frozen.get(id(mode))
            if key is None:
                key = self._topology_key(mode)
                frozen[id(mode)] = key
        slot = store.get(key)
        if slot is None:
            if len(store) >= self._TOPOLOGY_CACHE_LIMIT:
                store.pop(next(iter(store)))
            slot = {}
            store[key] = slot
        return slot

    def invalidate_topology_cache(self) -> None:
        """Сбросить кэш вручную. Обычно не требуется: ключ учитывает состояние."""
        self.__dict__.pop("_topology_store", None)

    # ---------- топология ----------
    def branch_conducting(self, branch: Branch, mode: Mode) -> bool:
        """Ветвь проводит только при замкнутых выключателях на ОБОИХ концах.

        Совместимость: если режим ничего не говорит про конкретный выключатель,
        решает состояние самой ветви — старые проекты работают как раньше.
        """
        if not mode.is_closed(branch):
            return False
        for switch_id in switch_ids(branch):
            if switch_id in mode.states and not mode.states[switch_id]:
                return False
        return True

    def active_branches(self, mode: Mode) -> list[Branch]:
        slot = self._topology_cache(mode)
        cached = slot.get("active_branches")
        if cached is None:
            cached = [b for b in self.branches.values() if self.branch_conducting(b, mode)]
            slot["active_branches"] = cached
        return list(cached)

    def adjacency(self, mode: Mode) -> dict[str, list[tuple[str, Branch]]]:
        slot = self._topology_cache(mode)
        adj = slot.get("adjacency")
        if adj is not None:
            return adj
        adj = {GRID: []}
        for nid in self.nodes:
            adj[nid] = []
        for b in self.active_branches(mode):
            adj.setdefault(b.node_from, []).append((b.node_to, b))
            adj.setdefault(b.node_to, []).append((b.node_from, b))
        slot["adjacency"] = adj
        return adj

    def energized_nodes(self, mode: Mode) -> set[str]:
        """Узлы, связанные с источником в данном режиме."""
        slot = self._topology_cache(mode)
        cached = slot.get("energized_nodes")
        if cached is None:
            adj = self.adjacency(mode)
            seen, stack = {GRID}, [GRID]
            while stack:
                cur = stack.pop()
                for nxt, _ in adj.get(cur, []):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            cached = frozenset(seen)
            slot["energized_nodes"] = cached
        return set(cached)

    def orient(self, branch: Branch, mode: Mode) -> tuple[str, str, bool]:
        """
        Определить фактическое направление питания ветви в данном режиме:
        (узел со стороны источника, узел со стороны нагрузки, признак кольца).

        Нужно потому, что node_from/node_to — это то, как элемент НАРИСОВАН,
        а не то, куда в этом режиме течёт мощность. Классический случай — СВ:
        при отключённом Т1 питание идёт через него в обратную сторону, и без
        этой поправки программа теряет всю цепочку защит первой секции.
        """
        slot = self._topology_cache(mode)
        store = slot.setdefault("orient", {})
        cached = store.get(branch.id)
        if cached is not None:
            return cached
        adj = self.adjacency(mode)
        seen, stack = {GRID}, [GRID]
        while stack:
            cur = stack.pop()
            for nxt, b in adj.get(cur, []):
                if b.id == branch.id or nxt in seen:
                    continue
                seen.add(nxt)
                stack.append(nxt)
        a_live = branch.node_from in seen
        b_live = branch.node_to in seen
        if a_live and b_live:
            result = (branch.node_from, branch.node_to, True)      # кольцо
        elif b_live and not a_live:
            result = (branch.node_to, branch.node_from, False)     # поток обратный
        else:
            result = (branch.node_from, branch.node_to, False)
        store[branch.id] = result
        # Побочный результат обхода: узлы, запитанные без этой ветви. Он нужен
        # `downstream_nodes()`, поэтому сохраняется здесь, а не считается заново.
        slot.setdefault("live_without", {})[branch.id] = frozenset(seen)
        return result

    def downstream_nodes(self, branch: Branch, mode: Mode) -> set[str]:
        """
        Узлы, питающиеся ЧЕРЕЗ данную ветвь. Направление определяется по факту,
        а не по тому, как ветвь записана в файле. Если при снятии ветви узел
        всё равно связан с источником (кольцо, второй ввод), он в зону не
        попадает — и это правильно.
        """
        slot = self._topology_cache(mode)
        store = slot.setdefault("downstream_nodes", {})
        cached = store.get(branch.id)
        if cached is not None:
            return set(cached)
        adj = self.adjacency(mode)
        src_end, load_end, ring = self.orient(branch, mode)
        if ring:
            store[branch.id] = frozenset()
            return set()
        seen = slot.get("live_without", {}).get(branch.id)
        if seen is None:
            seen, stack = {GRID}, [GRID]
            while stack:
                cur = stack.pop()
                for nxt, b in adj.get(cur, []):
                    if b.id == branch.id or nxt in seen:
                        continue
                    seen.add(nxt)
                    stack.append(nxt)
        zone, stack = set(), [load_end]
        while stack:
            cur = stack.pop()
            if cur in zone or cur in seen:
                continue
            zone.add(cur)
            for nxt, b in adj.get(cur, []):
                if b.id != branch.id and nxt not in zone:
                    stack.append(nxt)
        store[branch.id] = frozenset(zone)
        return zone

    def parallel_group(self, branch: Branch, mode: Mode) -> list[Branch]:
        """Ветви, идущие параллельно данной между теми же узлами (две ВЛ, два АТ)."""
        pair = frozenset((branch.node_from, branch.node_to))
        return [b for b in self.active_branches(mode)
                if frozenset((b.node_from, b.node_to)) == pair]

    def group_zone(self, branch: Branch, mode: Mode) -> tuple[set[str], int]:
        """
        Зона, питающаяся через ПАРАЛЛЕЛЬНУЮ ГРУППУ, и число элементов в группе.

        Нужно для присоединений, работающих параллельно: у каждого в отдельности
        зоны нет (при его отключении нагрузка остаётся запитанной), но группа в
        целом зону имеет. Рабочий ток такого присоединения берётся по всей зоне
        группы — это режим, когда параллельный элемент выведен и оставшийся
        несёт всё.
        """
        group = self.parallel_group(branch, mode)
        if len(group) < 2:
            return self.downstream_nodes(branch, mode), 1
        adj = self.adjacency(mode)
        ids = {b.id for b in group}
        seen, stack = {GRID}, [GRID]
        while stack:
            cur = stack.pop()
            for nxt, b in adj.get(cur, []):
                if b.id in ids or nxt in seen:
                    continue
                seen.add(nxt)
                stack.append(nxt)
        ends = {branch.node_from, branch.node_to} - seen
        zone, stack = set(), list(ends)
        while stack:
            cur = stack.pop()
            if cur in zone or cur in seen:
                continue
            zone.add(cur)
            for nxt, b in adj.get(cur, []):
                if b.id not in ids and nxt not in zone:
                    stack.append(nxt)
        return zone, len(group)

    def downstream_loads(self, branch: Branch, mode: Mode) -> list[Load]:
        zone = self.downstream_nodes(branch, mode)
        return [
            load for load in self.loads.values()
            if load.node in zone and mode.is_available(load.id)
        ]

    def downstream_branches(self, branch: Branch, mode: Mode) -> list[Branch]:
        zone = self.downstream_nodes(branch, mode)
        out = []
        for b in self.active_branches(mode):
            if b.id == branch.id:
                continue
            if b.node_from in zone or b.node_to in zone:
                out.append(b)
        return out

    def upstream_branch(self, branch: Branch, mode: Mode,
                        protection_kind: str | None = None) -> Branch | None:
        """Ближайшая вышестоящая ветвь с защитой — для проверки селективности."""
        adj = self.adjacency(mode)
        src_end, load_end, _ = self.orient(branch, mode)
        zone = self.downstream_nodes(branch, mode) | {load_end}
        prev: dict[str, tuple[str, Branch]] = {}
        seen, stack = {GRID}, [GRID]
        while stack:
            cur = stack.pop()
            for nxt, b in adj.get(cur, []):
                if nxt in seen or nxt in zone or b.id == branch.id:
                    continue
                seen.add(nxt)
                prev[nxt] = (cur, b)
                stack.append(nxt)
        cur = src_end
        while cur in prev:
            parent, b = prev[cur]
            enabled = (protection_kind is None
                       or bool(getattr(b.prot, protection_kind, False)))
            if b.has_protection_point and enabled:
                return b
            cur = parent
        return None

    def protection_points(self, mode: Mode | None = None,
                          protection_kind: str | None = None) -> list[Branch]:
        bs = self.branches.values() if mode is None else self.active_branches(mode)
        return [b for b in bs if b.has_protection_point and (
            protection_kind is None or bool(getattr(b.prot, protection_kind, False))
        )]

    def protection_node_id(self, branch: Branch) -> str:
        """Физический узел установки ТТ, неизменный во всех режимах."""
        if not branch.has_protection_point:
            raise ValueError(f"У ветви «{branch.name}» не задан ТТ.")
        node_id = branch.ct_node
        if node_id is None:
            # Совместимость для объектов, созданных напрямую без add_branch().
            node_id = branch.node_to if branch.node_from == GRID else branch.node_from
        if node_id == GRID or node_id not in (branch.node_from, branch.node_to):
            raise ValueError(
                f"Ветвь «{branch.name}»: некорректный узел установки ТТ '{node_id}'."
            )
        return node_id

    def protection_side_u(self, branch: Branch) -> float:
        if isinstance(branch, GeneratorBranch):
            # Класс сети узла и паспортное напряжение генератора — разные
            # величины. Ток ветви генератора приводится к его физической
            # паспортной стороне, а не к числу класса сети (например,
            # 10,5 кВ оборудования в сети класса 10 кВ).
            return branch.u_nom
        return self.node(self.protection_node_id(branch)).u_nom

    def node(self, nid: str) -> Node:
        if nid == GRID:
            raise KeyError("GRID — служебный узел ЭДС, у него нет параметров.")
        return self.nodes[nid]

    # ---------- проверка модели ----------
    def validate(self, mode: Mode | None = None, *, require_source_data: bool = True) -> list[str]:
        problems: list[str] = []

        def is_finite(value: object) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )

        def check_finite(owner: str, field_name: str, value: object) -> bool:
            if not is_finite(value):
                problems.append(
                    f"{owner}: {field_name} должно быть конечным числом; "
                    "NaN/Infinity недопустимы."
                )
                return False
            return True

        for node in self.nodes.values():
            if not isinstance(node.id, str) or not node.id.strip():
                problems.append(
                    f"Узел «{node.name}»: ID должен быть непустой строкой."
                )
            if not isinstance(node.name, str) or not node.name.strip():
                problems.append("У каждого узла должно быть непустое строковое название.")
            owner = f"Узел «{node.name}»"
            if check_finite(owner, "напряжение", node.u_nom) and node.u_nom <= 0:
                problems.append(f"{owner}: напряжение должно быть больше нуля.")
            for title, value in (
                ("расчётное базисное напряжение", node.calculation_base_kv),
                ("предаварийное напряжение", node.prefault_voltage_kv),
            ):
                if value is not None and check_finite(owner, title, value) and value <= 0:
                    problems.append(f"{owner}: {title} должно быть больше нуля.")

        gens = [b for b in self.branches.values() if isinstance(b, GeneratorBranch)]
        sources = [b for b in self.branches.values() if isinstance(b, SourceBranch)]
        if not sources and not gens:
            problems.append("В схеме нет ни системы, ни генераторов.")

        for g in gens:
            owner = f"Генератор «{g.name}»"
            required = require_source_data and (mode is None or self.branch_conducting(g, mode))
            if g.node_from != GRID:
                problems.append(f"{owner} должен начинаться от узла GRID.")
            for title, value in (("Uном", g.u_nom), ('x"d', g.xd2), ("cosφ", g.cos_phi)):
                if value is not None or required:
                    check_finite(owner, title, value)
            if g.s_nom:
                check_finite(owner, "Sном", g.s_nom)
            if g.p_nom is not None:
                check_finite(owner, "Pном", g.p_nom)
            if g.r_pu is not None:
                check_finite(owner, "R, о.е.", g.r_pu)
            try:
                apparent_power = g.s_from_p
            except Exception:
                apparent_power = math.nan
            if required and (not is_finite(apparent_power) or apparent_power <= 0):
                problems.append(f"{owner}: не задана положительная мощность.")
            if is_finite(g.s_nom) and g.s_nom < 0:
                problems.append(f"{owner}: мощность не может быть отрицательной.")
            if is_finite(g.xd2) and g.xd2 <= 0:
                problems.append(f'{owner}: x"d должно быть больше нуля.')
            if is_finite(g.u_nom) and g.u_nom <= 0:
                problems.append(f"{owner}: Uном должно быть больше нуля.")
            if is_finite(g.cos_phi) and not 0 < g.cos_phi <= 1:
                problems.append(f"{owner}: cosφ должен быть в диапазоне (0; 1].")
            if g.r_pu is not None and is_finite(g.r_pu) and g.r_pu < 0:
                problems.append(f"{owner}: активное сопротивление не может быть отрицательным.")

        for source in sources:
            owner = f"Источник «{source.name}»"
            if source.node_from != GRID:
                problems.append(f"{owner} должен начинаться от узла GRID.")
            for regime, s_value, i_value, input_mode in (
                ("max", source.s_kz_max, source.i_kz_max, source.input_mode_max),
                ("min", source.s_kz_min, source.i_kz_min, source.input_mode_min),
            ):
                required = (require_source_data and (mode is None or (
                    self.branch_conducting(source, mode) and mode.system == regime)))
                if input_mode not in (None, "power", "current"):
                    problems.append(
                        f"{owner}: способ задания режима {regime} должен быть "
                        "'power' или 'current'."
                    )
                if s_value is None and i_value is None:
                    if required and regime == "max":
                        problems.append(f"{owner}: не задан ни Sкз.max, ни Iкз.max.")
                    elif required:
                        problems.append(f"{owner}: не задан минимальный режим системы.")
                    continue
                if required and s_value is not None and i_value is not None and input_mode is None:
                    problems.append(
                        f"{owner}: одновременно заданы противоречивые способы "
                        f"описания режима {regime} — Sкз и Iкз. Выберите один первичный параметр."
                    )
                if required and input_mode == "power" and s_value is None:
                    problems.append(f"{owner}: для режима {regime} выбран ввод по Sкз, но Sкз не задана.")
                if required and input_mode == "current" and i_value is None:
                    problems.append(f"{owner}: для режима {regime} выбран ввод по Iкз, но Iкз не задан.")
                if s_value is not None:
                    field_name = f"Sкз.{regime}"
                    if check_finite(owner, field_name, s_value) and s_value <= 0:
                        problems.append(f"{owner}: {field_name} должно быть больше нуля.")
                if i_value is not None:
                    field_name = f"Iкз.{regime}"
                    if check_finite(owner, field_name, i_value) and i_value <= 0:
                        problems.append(f"{owner}: {field_name} должно быть больше нуля.")
            if source.x_r_ratio is not None:
                if check_finite(owner, "X/R", source.x_r_ratio) and source.x_r_ratio < 0:
                    problems.append(f"{owner}: X/R не может быть отрицательным.")
            if source.voltage_kv is not None:
                if check_finite(owner, "напряжение источника", source.voltage_kv) and source.voltage_kv <= 0:
                    problems.append(f"{owner}: напряжение источника должно быть больше нуля.")

        registered_star_legs = {
            branch_id
            for transformer in self.transformers3w.values()
            for branch_id in transformer.branch_ids
        }

        for b in self.branches.values():
            owner = f"Ветвь «{b.name}»"
            if not isinstance(b.id, str) or not b.id.strip():
                problems.append(f"{owner}: ID должен быть непустой строкой.")
            if not isinstance(b.name, str) or not b.name.strip():
                problems.append("У каждой ветви должно быть непустое строковое название.")
            for endpoint in (b.node_from, b.node_to):
                if endpoint != GRID and endpoint not in self.nodes:
                    problems.append(f"{owner}: узел '{endpoint}' не найден.")
            if b.switch_with is not None and b.switch_with not in self.branches:
                problems.append(f"{owner}: управляющая ветвь '{b.switch_with}' не найдена.")
            if b.breaker_t_off is not None:
                if check_finite(owner, "время отключения", b.breaker_t_off) and b.breaker_t_off < 0:
                    problems.append(f"{owner}: время отключения не может быть отрицательным.")

            if b.ct_ratio is not None:
                if len(b.ct_ratio) != 2:
                    problems.append(f"{owner}: коэффициент ТТ должен содержать два числа.")
                else:
                    for index, value in enumerate(b.ct_ratio, start=1):
                        if check_finite(owner, f"коэффициент ТТ [{index}]", value) and value <= 0:
                            problems.append(f"{owner}: коэффициент ТТ должен быть положительным.")
                try:
                    self.protection_node_id(b)
                except ValueError as exc:
                    problems.append(str(exc))
                if b.prot.i_scale is not None:
                    if not b.prot.i_scale:
                        problems.append(f"{owner}: шкала терминала не должна быть пустой.")
                    for value in b.prot.i_scale:
                        if not check_finite(owner, "значение шкалы терминала", value) or value <= 0:
                            problems.append(f"{owner}: шкала терминала должна быть положительной.")
                            break
                for title, value in (("t МТЗ", b.prot.t_mtz), ("t ТО", b.prot.t_to), ("t ОЗЗ", b.prot.t_ozz)):
                    if value is not None and check_finite(owner, title, value) and value < 0:
                        problems.append(f"{owner}: {title} не может быть отрицательным.")

            if isinstance(b, TransformerBranch):
                tr_owner = f"Трансформатор «{b.name}»"
                for title, value in (("Sном", b.s_nom), ("Uвн", b.u_hv), ("Uнн", b.u_lv), ("Uк", b.uk)):
                    check_finite(tr_owner, title, value)
                if b.p_k is not None:
                    check_finite(tr_owner, "ΔPк", b.p_k)
                if b.uk == 0 and b.switch_with is None:
                    problems.append(f"{tr_owner}: не задано Uк, %.")
                if (
                    is_finite(b.uk)
                    and b.uk < 0
                    and (
                        not b.internal_star_leg
                        or b.id not in registered_star_legs
                    )
                ):
                    problems.append(f"{tr_owner}: отрицательное Uк допустимо только для служебного луча 3W.")
                if is_finite(b.s_nom) and b.s_nom <= 0:
                    problems.append(f"{tr_owner}: не задана Sном.")
                if is_finite(b.u_hv) and is_finite(b.u_lv) and (b.u_hv <= 0 or b.u_lv <= 0):
                    problems.append(f"{tr_owner}: напряжения должны быть положительными.")
                # Паспортные напряжения обмоток обязаны соответствовать классам
                # узлов: по ним определяется ступень, на которой считается Z, и
                # коэффициент приведения между ступенями. Раньше несоответствие
                # молча игнорировалось — трансформатор 110/6,3 на шинах 10 кВ
                # считался как 110/10.
                if (
                    is_finite(b.u_hv) and is_finite(b.u_lv)
                    and b.u_hv > 0 and b.u_lv > 0
                    and b.node_from in self.nodes and b.node_to in self.nodes
                ):
                    from .impedance import belongs_to_stage

                    first = self.nodes[b.node_from]
                    second = self.nodes[b.node_to]
                    direct = (belongs_to_stage(b.u_hv, first.u_nom)
                              and belongs_to_stage(b.u_lv, second.u_nom))
                    reverse = (belongs_to_stage(b.u_hv, second.u_nom)
                               and belongs_to_stage(b.u_lv, first.u_nom))
                    if not direct and not reverse:
                        problems.append(
                            f"{tr_owner}: паспортные напряжения "
                            f"{b.u_hv} / {b.u_lv} кВ не соответствуют классам "
                            f"узлов «{first.name}» ({first.u_nom} кВ) и "
                            f"«{second.name}» ({second.u_nom} кВ)."
                        )
                if b.p_k is not None and is_finite(b.p_k) and b.p_k < 0:
                    problems.append(f"{tr_owner}: ΔPк не может быть отрицательной.")

            if isinstance(b, LineBranch):
                line_owner = f"Линия «{b.name}»"
                if b.calculation_block_reason is not None:
                    if not isinstance(b.calculation_block_reason, str) or not b.calculation_block_reason.strip():
                        problems.append(f"{line_owner}: повреждён признак блокировки расчёта.")
                    else:
                        problems.append(f"{line_owner}: расчёт заблокирован: {b.calculation_block_reason}")
                if not check_finite(line_owner, "длина", b.length_km) or b.length_km <= 0:
                    problems.append(f"{line_owner}: не задана длина.")
                if b.r0 is None and not b.section_mm2:
                    problems.append(f"{line_owner}: нет ни r0/x0, ни сечения для справочника.")
                for title, value in (("R", b.r0), ("X", b.x0), ("сечение", b.section_mm2), ("ток ОЗЗ", b.ic_per_km)):
                    if value is not None and check_finite(line_owner, title, value) and value < 0:
                        problems.append(f"{line_owner}: {title} не может быть отрицательным.")
                if isinstance(b.n_parallel, bool) or not isinstance(b.n_parallel, int) or b.n_parallel < 1:
                    problems.append(f"{line_owner}: число параллельных цепей должно быть целым ≥ 1.")

        for load in self.loads.values():
            owner = f"Нагрузка «{load.name}»"
            if load.node not in self.nodes:
                problems.append(f"{owner}: узел '{load.node}' не найден.")
            for title, value in (("P", load.p_kw), ("cosφ", load.cos_phi), ("kисп", load.k_use)):
                check_finite(owner, title, value)
            if is_finite(load.p_kw) and load.p_kw < 0:
                problems.append(f"{owner}: мощность не может быть отрицательной.")
            if is_finite(load.cos_phi) and not 0 < load.cos_phi <= 1:
                problems.append(f"{owner}: cosφ должен быть в диапазоне (0; 1].")
            if is_finite(load.k_use) and load.k_use <= 0:
                problems.append(f"{owner}: коэффициент использования должен быть > 0.")
            if load.motor_share is not None:
                if check_finite(owner, "доля двигателей", load.motor_share) and not 0 <= load.motor_share <= 1:
                    problems.append(f"{owner}: доля двигателей должна быть от 0 до 1.")

        for mode in self.modes.values():
            for key, state in mode.states.items():
                parsed = parse_switch_id(key)
                if parsed is not None:
                    owner_id, end = parsed
                    owner = self.branches.get(owner_id)
                    if owner is None:
                        problems.append(
                            f"Режим «{mode.name}»: выключатель '{key}' ссылается на "
                            f"несуществующее присоединение '{owner_id}'."
                        )
                    elif key not in switch_ids(owner):
                        problems.append(
                            f"Режим «{mode.name}»: у присоединения «{owner.name}» нет "
                            f"выключателя со стороны '{end}'."
                        )
                    if not isinstance(state, bool):
                        problems.append(f"Режим «{mode.name}»: состояние '{key}' должно быть true или false.")
                    continue
                branch = self.branches.get(key)
                if branch is None:
                    problems.append(f"Режим «{mode.name}»: ветвь '{key}' не найдена.")
                elif not branch.switchable:
                    problems.append(f"Режим «{mode.name}»: ветвь «{branch.name}» не является переключаемой.")
                if not isinstance(state, bool):
                    problems.append(f"Режим «{mode.name}»: состояние '{key}' должно быть true/false.")
            for object_id, available in mode.availability.items():
                if object_id not in self.branches and object_id not in self.loads:
                    problems.append(f"Режим «{mode.name}»: доступность ссылается на неизвестный объект '{object_id}'.")
                if not isinstance(available, bool):
                    problems.append(f"Режим «{mode.name}»: доступность '{object_id}' должна быть true/false.")

        allowed_neutral = set(self.NEUTRAL_RU)
        for voltage, neutral_mode in self.neutral.items():
            if neutral_mode not in allowed_neutral:
                problems.append(f"Ступень {voltage} кВ: неизвестный режим нейтрали '{neutral_mode}'.")
        if not self.modes:
            problems.append("Не задан ни один режим сети — расчёт бессмысленен.")
        return list(dict.fromkeys(problems))
