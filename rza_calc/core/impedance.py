# -*- coding: utf-8 -*-
"""
Сопротивления элементов схемы замещения (комплексные, в именованных единицах)
и приведение к базисной ступени напряжения.

Физические сопротивления оборудования рассчитываются на его паспортной или
сетевой ступени, а между ступенями приводятся по согласованным номинальным
напряжениям. Среднее расчётное напряжение применяется отдельно при определении
напряжения до КЗ и не подменяет коэффициент трансформации оборудования.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .methodology import Methodology
from .model import (Branch, GeneratorBranch, LineBranch, SourceBranch, TieBranch,
                    TransformerBranch)
from .trace import Step, Value, fmt, record

CABLES_PATH = Path(__file__).resolve().parent.parent / "data" / "cables.json"
_CABLES: dict | None = None


def cables() -> dict:
    global _CABLES
    if _CABLES is None:
        with open(CABLES_PATH, encoding="utf-8") as f:
            _CABLES = json.load(f)
    return _CABLES


#: Допуск, в пределах которого паспортное напряжение обмотки считается
#: принадлежащим классу узла. 115 кВ относится к классу 110 (+4,5 %),
#: 11 кВ — к классу 10 (+10 %), 6,3 кВ — к классу 6 (+5 %). Всё, что дальше,
#: означает несогласованные исходные данные, а не другую запись того же.
STAGE_TOLERANCE = 0.12


def stage_voltage(node, m: Methodology) -> float:
    """Расчётное напряжение ступени узла — единый базис приведения.

    Классическая методика расчёта ТКЗ ведёт весь расчёт в средних напряжениях
    ступеней: и ЭДС, и сопротивления, и приведение между ступенями. Смешение
    средних напряжений с номинальными классами даёт систематическую ошибку,
    поэтому базис определяется ровно в одном месте — здесь.

    Узел может явно переопределить расчётное напряжение через
    ``calculation_base_kv``; это единственный законный способ отойти от
    таблицы средних напряжений.
    """
    explicit = getattr(node, "calculation_base_kv", None)
    if explicit:
        value = float(explicit)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"Узел «{node.name}»: расчётное напряжение ступени должно быть "
                "конечным и больше нуля."
            )
        return value
    return m.u_avg(node.u_nom)


def belongs_to_stage(nameplate_kv: float, class_kv: float,
                     tolerance: float = STAGE_TOLERANCE) -> bool:
    """Относится ли паспортное напряжение обмотки к классу узла."""
    values = (nameplate_kv, class_kv)
    if not all(math.isfinite(float(value)) for value in values):
        return False
    if class_kv <= 0 or nameplate_kv <= 0:
        return False
    return abs(float(nameplate_kv) - float(class_kv)) / float(class_kv) <= tolerance


def transformer_stage_node(net, br: TransformerBranch):
    """Узел стороны, на которой вычисляется сопротивление трансформатора.

    Поле ``u_hv`` — это напряжение той стороны, для которой задано ``Uк``; она
    не обязана быть стороной высшего напряжения и не обязана совпадать с
    ``node_from`` (блочный трансформатор записан как 10/220). Сторона
    определяется сопоставлением паспортных напряжений с классами узлов, а не
    порядком записи концов ветви, поэтому направление рисования на результат
    не влияет.
    """
    first = net.node(br.node_from)
    second = net.node(br.node_to)
    if belongs_to_stage(br.u_hv, first.u_nom) and belongs_to_stage(br.u_lv, second.u_nom):
        return first
    if belongs_to_stage(br.u_hv, second.u_nom) and belongs_to_stage(br.u_lv, first.u_nom):
        return second
    raise ValueError(
        f"Трансформатор «{br.name}»: паспортные напряжения "
        f"{fmt(br.u_hv)}/{fmt(br.u_lv)} кВ не соответствуют классам узлов "
        f"«{first.name}» ({fmt(first.u_nom)} кВ) и «{second.name}» "
        f"({fmt(second.u_nom)} кВ). Приведение сопротивления между ступенями "
        "выполнить нельзя: проверьте паспорт аппарата и классы напряжения узлов."
    )


def refer(z: complex, u_from: float, u_to: float) -> complex:
    """Привести Z между согласованными физическими ступенями напряжения."""
    values = (z.real, z.imag, u_from, u_to)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("Приведение сопротивления: NaN/Infinity недопустимы.")
    if u_from <= 0 or u_to <= 0:
        raise ValueError("Приведение сопротивления: напряжения должны быть > 0.")
    result = z * (u_to / u_from) ** 2
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise ValueError("Приведение сопротивления привело к переполнению числа.")
    return result


# ──────────────────────────────────────────────────────────────────────────
#  Источник
# ──────────────────────────────────────────────────────────────────────────
def source_impedance(
    br: SourceBranch,
    m: Methodology,
    u_nom_kv: float,
    regime: str = "max",
    *,
    prefault_voltage_kv: float | None = None,
) -> tuple[complex, Step]:
    """Эквивалент внешней системы на физической ступени ``u_nom_kv``."""
    u = prefault_voltage_kv if prefault_voltage_kv is not None else m.u_avg(u_nom_kv)
    s_kz = br.s_kz_max if regime == "max" else br.s_kz_min
    i_kz = br.i_kz_max if regime == "max" else br.i_kz_min
    input_mode = br.input_mode_max if regime == "max" else br.input_mode_min
    reg_ru = "максимальном" if regime == "max" else "минимальном"

    if input_mode not in (None, "power", "current"):
        raise ValueError(
            f"Источник «{br.name}»: неизвестный способ задания режима "
            f"{input_mode!r}. Допустимы 'power' и 'current'."
        )
    if s_kz is not None and i_kz is not None and input_mode is None:
        raise ValueError(
            f"Источник «{br.name}»: одновременно заданы Sкз и Iкз в {reg_ru} "
            "режиме. Выберите один первичный способ задания источника."
        )
    if input_mode == "power":
        if s_kz is None:
            raise ValueError(
                f"Источник «{br.name}»: выбран ввод по Sкз, но Sкз в "
                f"{reg_ru} режиме не задана."
            )
        i_kz = None
    elif input_mode == "current":
        if i_kz is None:
            raise ValueError(
                f"Источник «{br.name}»: выбран ввод по Iкз, но Iкз в "
                f"{reg_ru} режиме не задан."
            )
        s_kz = None
    if not math.isfinite(float(u)) or u <= 0:
        raise ValueError(f"Источник «{br.name}»: расчётное напряжение некорректно.")

    if s_kz is not None:
        if not math.isfinite(float(s_kz)) or s_kz <= 0:
            raise ValueError(
                f"Источник «{br.name}»: Sкз в {reg_ru} режиме должна быть "
                "конечным положительным числом."
            )
        z_mag = u ** 2 / s_kz
        formula = "Zс = Uрасч² / Sкз"
        subst = f"Zс = {fmt(u)}² / {fmt(s_kz)} = {fmt(z_mag)} Ом"
        gv = {"Uрасч": f"{fmt(u)} кВ", "Sкз": f"{fmt(s_kz)} МВ·А"}
    elif i_kz is not None:
        if not math.isfinite(float(i_kz)) or i_kz <= 0:
            raise ValueError(
                f"Источник «{br.name}»: Iкз в {reg_ru} режиме должна быть "
                "конечным положительным числом."
            )
        z_mag = u / (math.sqrt(3) * i_kz)
        formula = "Zс = Uрасч / (√3 · Iкз)"
        subst = f"Zс = {fmt(u)} / (1.732 · {fmt(i_kz)}) = {fmt(z_mag)} Ом"
        gv = {"Uрасч": f"{fmt(u)} кВ", "Iкз": f"{fmt(i_kz)} кА"}
    else:
        raise ValueError(f"Источник «{br.name}»: не задан {reg_ru} режим системы.")

    if not math.isfinite(z_mag) or z_mag <= 0:
        raise ValueError(
            f"Источник «{br.name}»: расчёт сопротивления дал недопустимое "
            "или переполненное значение. Проверьте диапазон Sкз/Iкз."
        )

    if br.x_r_ratio is not None:
        if not math.isfinite(float(br.x_r_ratio)) or br.x_r_ratio < 0:
            raise ValueError(f"Источник «{br.name}»: X/R должно быть конечным и ≥ 0.")
        phi = math.atan(br.x_r_ratio)
        z = complex(z_mag * math.cos(phi), z_mag * math.sin(phi))
        note = f"Учтено X/R = {fmt(br.x_r_ratio)}."
    else:
        z = complex(0.0, z_mag)
        note = ("Сопротивление системы принято чисто индуктивным (R = 0). "
                "Это явное упрощение: при наличии данных задайте X/R.")

    if not math.isfinite(z.real) or not math.isfinite(z.imag):
        raise ValueError(f"Источник «{br.name}»: получено неограниченное сопротивление.")

    st = Step(
        what=f"Сопротивление системы в {reg_ru} режиме",
        why="Определяет уровень токов КЗ на шинах и, через них, все уставки ниже по сети.",
        given=gv, formula=formula, substitution=subst,
        result=f"Zс = {fmt(z)} Ом (на ступени {fmt(u_nom_kv)} кВ)",
        source=m.cite("short_circuit"), note=note,
    )
    return z, st


# ──────────────────────────────────────────────────────────────────────────
#  Генератор
# ──────────────────────────────────────────────────────────────────────────
def generator_impedance(br: GeneratorBranch, m: Methodology,
                        u_stage: float | None = None) -> tuple[complex, Step]:
    """Сверхпереходное сопротивление генератора.

    В классической методике сопротивление машины приводится к среднему
    напряжению своей ступени, поэтому расчёт ведётся по ``u_stage``. Для
    типового генератора паспортное напряжение и среднее совпадают (10,5 кВ на
    ступени 10 кВ), и результат не меняется; расхождение отмечается в
    протоколе, чтобы подмена не осталась незамеченной.
    """
    nameplate = br.u_nom
    u = float(u_stage) if u_stage else br.u_nom
    s = br.s_from_p
    if not all(math.isfinite(float(value)) for value in (u, s, br.xd2)):
        raise ValueError(f"Генератор «{br.name}»: NaN/Infinity недопустимы.")
    if u <= 0:
        raise ValueError(f"Генератор «{br.name}»: Uном должно быть больше нуля.")
    if s <= 0:
        raise ValueError(f"Генератор «{br.name}»: не задана ни Sном, ни Pном с cosφ.")
    if br.xd2 <= 0:
        raise ValueError(f'Генератор «{br.name}»: x"d должно быть больше нуля.')
    if br.r_pu is not None and (
        not math.isfinite(float(br.r_pu)) or br.r_pu < 0
    ):
        raise ValueError(
            f"Генератор «{br.name}»: R, о.е. должно быть конечным и ≥ 0."
        )
    x = 1000.0 * br.xd2 * u ** 2 / s
    r = 1000.0 * br.r_pu * u ** 2 / s if br.r_pu else 0.0
    gv = {"Sном": f"{fmt(s)} кВ·А", "x\"d": f"{fmt(br.xd2)} о.е.",
          "Uрасч ступени": f"{fmt(u)} кВ"}
    if nameplate and abs(float(nameplate) - u) > 1e-9:
        gv["Uном (паспорт)"] = f"{fmt(nameplate)} кВ"
    if br.p_nom:
        gv["Pном"] = f"{fmt(br.p_nom)} МВт при cosφ = {fmt(br.cos_phi)}"
    st = Step(
        what=f"Сопротивление генератора «{br.name}»",
        why=("Генератор — не система бесконечной мощности: его вклад в ток КЗ ограничен "
             "сверхпереходным сопротивлением. Поэтому состав работающих машин меняет "
             "токи КЗ по всей сети, а вместе с ними и уставки."),
        given=gv,
        formula='X"d = 1000 · x"d · Uрасч² / Sном',
        substitution=f'X"d = 1000 · {fmt(br.xd2)} · {fmt(u)}² / {fmt(s)} = {fmt(x)} Ом',
        result=f"Zг = {fmt(complex(r, x))} Ом (на ступени {fmt(u)} кВ)",
        source=m.cite("short_circuit"),
        note=("Расчёт ведётся по сверхпереходному режиму (начальное действующее значение "
              "периодической составляющей). Затухание подпитки от генератора во времени "
              "и форсировка возбуждения не моделируются — для проверки чувствительности "
              "защит с выдержкой времени это даёт запас в опасную сторону."),
    )
    return complex(r, x), st


# ──────────────────────────────────────────────────────────────────────────
#  Трансформатор
# ──────────────────────────────────────────────────────────────────────────
def transformer_impedance(br: TransformerBranch, m: Methodology,
                          u_stage: float | None = None) -> tuple[complex, Step]:
    """
    Считается на стороне, для которой задано Uк, затем приводится вызывающим
    кодом.

    Расчёт ведётся по СРЕДНЕМУ напряжению этой ступени (``u_stage``), а не по
    паспортному числу: паспортные 115 кВ и класс 110 кВ описывают одну и ту же
    ступень, и подстановка разных чисел в одну формулу давала бы разный
    результат для одного и того же аппарата. Паспортные напряжения при этом не
    игнорируются — по ним определяется, какая именно сторона считается
    (см. :func:`transformer_stage_node`).

    Uк может быть отрицательным — так бывает у среднего луча звезды
    трёхобмоточного трансформатора. Это не ошибка данных: в схеме замещения
    такой луч имеет отрицательное реактивное сопротивление, и решатель
    работает с ним штатно.
    """
    if not all(
        math.isfinite(float(value))
        for value in (br.u_hv, br.u_lv, br.s_nom, br.uk)
    ):
        raise ValueError(f"Трансформатор «{br.name}»: NaN/Infinity недопустимы.")
    if br.u_hv <= 0 or br.u_lv <= 0 or br.s_nom <= 0:
        raise ValueError(
            f"Трансформатор «{br.name}»: U и Sном должны быть больше нуля."
        )
    if br.uk < 0 and not br.internal_star_leg:
        raise ValueError(
            f"Трансформатор «{br.name}»: отрицательное Uк допустимо только "
            "для служебного луча подтверждённой модели 3W."
        )
    if br.p_k is not None and (
        not math.isfinite(float(br.p_k)) or br.p_k < 0
    ):
        raise ValueError(
            f"Трансформатор «{br.name}»: ΔPк должно быть конечным и ≥ 0."
        )
    u = float(u_stage) if u_stage else br.u_hv
    if not math.isfinite(u) or u <= 0:
        raise ValueError(
            f"Трансформатор «{br.name}»: расчётное напряжение ступени должно "
            "быть конечным и больше нуля."
        )
    z_signed = 10.0 * br.uk * u ** 2 / br.s_nom       # Ом, при S в кВ·А
    z_mag = abs(z_signed)
    sign = 1.0 if z_signed >= 0 else -1.0
    gv = {"Sном": f"{fmt(br.s_nom)} кВ·А", "Uк": f"{fmt(br.uk)} %",
          "Uрасч ступени": f"{fmt(u)} кВ",
          "Паспорт сторон": f"{fmt(br.u_hv)} / {fmt(br.u_lv)} кВ"}
    subst_r, note = "", None

    if br.p_k and sign > 0:
        r = 1000.0 * br.p_k * u ** 2 / br.s_nom ** 2
        if r >= z_mag:
            raise ValueError(
                f"Трансформатор «{br.name}»: Rт ≥ Zт — проверьте ΔPк и Uк, данные несогласованы."
            )
        x = math.sqrt(z_mag ** 2 - r ** 2)
        gv["ΔPк"] = f"{fmt(br.p_k)} кВт"
        subst_r = (f"Rт = 1000 · {fmt(br.p_k)} · {fmt(u)}² / {fmt(br.s_nom)}² = {fmt(r)} Ом;  "
                   f"Xт = √(Zт² − Rт²) = {fmt(x)} Ом")
    else:
        r, x = 0.0, z_mag
        if sign < 0:
            note = ("Отрицательное Uк луча звезды — нормальное следствие пересчёта "
                    "паспортных Uк трёхобмоточного трансформатора, а не ошибка ввода.")
        else:
            note = ("ΔPк не задано — активное сопротивление принято нулевым, Xт = Zт. "
                    "Для крупных трансформаторов это допустимо, для мелких КТП заметно "
                    "завышает ток КЗ: задайте ΔPк по паспорту.")

    z = complex(r, sign * x)
    st = Step(
        what=f"Сопротивление трансформатора «{br.name}»",
        why="Основное сопротивление, ограничивающее ток КЗ на стороне НН.",
        given=gv,
        formula="Zт = 10 · Uк[%] · Uрасч² / Sном ;   Rт = 1000 · ΔPк · Uрасч² / Sном²",
        substitution=(f"Zт = 10 · {fmt(br.uk)} · {fmt(u)}² / {fmt(br.s_nom)} = {fmt(z_signed)} Ом"
                      + (";  " + subst_r if subst_r else "")),
        result=f"Zт = {fmt(z)} Ом (на ступени {fmt(u)} кВ)",
        source=m.cite("short_circuit"), note=note,
    )
    return z, st


# ──────────────────────────────────────────────────────────────────────────
#  Линия
# ──────────────────────────────────────────────────────────────────────────
def line_impedance(br: LineBranch, m: Methodology, regime: str = "max") -> tuple[complex, Step]:
    if not math.isfinite(float(br.length_km)) or br.length_km <= 0:
        raise ValueError(f"Линия «{br.name}»: длина должна быть конечной и > 0.")
    if isinstance(br.n_parallel, bool) or not isinstance(br.n_parallel, int) or br.n_parallel < 1:
        raise ValueError(
            f"Линия «{br.name}»: число параллельных цепей должно быть целым ≥ 1."
        )
    for title, value in (("R", br.r0), ("X", br.x0)):
        if value is not None and (
            not math.isfinite(float(value)) or value < 0
        ):
            raise ValueError(
                f"Линия «{br.name}»: {title} должно быть конечным и неотрицательным."
            )
    if br.calculation_block_reason is not None:
        reason = (
            br.calculation_block_reason.strip()
            if (
                isinstance(br.calculation_block_reason, str)
                and br.calculation_block_reason.strip()
            )
            else "повреждён признак блокировки расчёта"
        )
        raise ValueError(f"Расчёт линии «{br.name}» заблокирован: {reason}")
    cb = cables()
    notes: list[str] = []

    # --- удельное активное ---
    if br.r0 is not None:
        r0 = br.r0
        r0_src = "задано пользователем"
        r0_subst = f"r0 = {fmt(r0)} Ом/км"
    else:
        rho = cb["resistivity"][br.material]["rho"]
        r0 = rho / br.section_mm2
        r0_src = f"ρ({br.material}) = {rho} Ом·мм²/км при 20 °C"
        r0_subst = f"r0 = {rho} / {fmt(br.section_mm2)} = {fmt(r0, 4)} Ом/км"

    # --- удельное индуктивное ---
    if br.x0 is not None:
        x0 = br.x0
        x0_src = "задано пользователем"
    elif br.line_type == "cable":
        table = cb["x0_cable_6_10kv"]
        key = _nearest_section(table, br.section_mm2)
        x0 = float(table[key])
        x0_src = f"справочник кабелей, сечение {key} мм²"
        notes.append(cb["warning"])
        if abs(float(key) - float(br.section_mm2)) > 1e-9:
            notes.append(
                f"Сечения {fmt(br.section_mm2)} мм² нет в справочнике: x0 взято "
                f"для ближайшего табличного сечения {key} мм². Задайте x0 явно, "
                "если разница существенна.")
    else:
        x0 = float(cb["x0_overhead_default"]["value"])
        x0_src = "типовое значение для ВЛ"
        notes.append(cb["x0_overhead_default"]["note"])

    # --- температурный коэффициент в минимальном режиме ---
    kt = 1.0
    if regime == "min":
        kt = m.k("short_circuit.temp_factor_min")
        notes.append(f"Минимальный режим: активное сопротивление увеличено в {fmt(kt)} раза "
                     f"(нагрев жил до рабочей температуры).")

    n = br.n_parallel
    r = r0 * br.length_km * kt / n
    x = x0 * br.length_km / n

    gv = {"Длина": f"{fmt(br.length_km)} км", "r0": f"{fmt(r0, 4)} Ом/км ({r0_src})",
          "x0": f"{fmt(x0, 4)} Ом/км ({x0_src})"}
    if br.section_mm2:
        gv["Сечение"] = f"{fmt(br.section_mm2)} мм², {br.material}"
    if n > 1:
        gv["Цепей параллельно"] = str(n)
    if kt != 1.0:
        gv["kt (нагрев)"] = fmt(kt)

    st = Step(
        what=f"Сопротивление линии «{br.name}»",
        why=("Определяет затухание тока КЗ вдоль линии: от него зависит и уставка ТО, "
             "и чувствительность МТЗ в конце участка."),
        given=gv,
        formula="R = r0 · L · kt / n ;   X = x0 · L / n",
        substitution=(f"{r0_subst};  R = {fmt(r0,4)} · {fmt(br.length_km)}"
                      + (f" · {fmt(kt)}" if kt != 1 else "")
                      + (f" / {n}" if n > 1 else "") + f" = {fmt(r)} Ом;  "
                      f"X = {fmt(x0,4)} · {fmt(br.length_km)}"
                      + (f" / {n}" if n > 1 else "") + f" = {fmt(x)} Ом"),
        result=f"Zл = {fmt(complex(r, x))} Ом",
        note=" ".join(notes) if notes else None,
    )
    return complex(r, x), st


def _nearest_section(table: dict, s: float | None) -> str:
    if not s:
        raise ValueError("Не задано сечение линии и не задано x0 — сопротивление посчитать нечем.")
    return min(table.keys(), key=lambda k: abs(float(k) - s))


# ──────────────────────────────────────────────────────────────────────────
#  Диспетчер: сопротивление любой ветви, приведённое к базисной ступени
# ──────────────────────────────────────────────────────────────────────────
def branch_impedance(net, br: Branch, m: Methodology, u_base: float,
                     regime: str = "max") -> tuple[complex | None, Step | None]:
    """
    Z ветви, приведённое к базисной ступени ``u_base``.

    Возвращает (None, None) для ветви нулевого сопротивления — СВ, шинный мост.

    Ступень элемента — это ВСЕГДА расчётное напряжение ступени его узла
    (:func:`stage_voltage`), то есть среднее напряжение класса либо явное
    переопределение узла. Единый базис для всех элементов и для ЭДС —
    обязательное условие корректного приведения: смешение средних и
    номинальных напряжений даёт систематическую ошибку по ступеням.
    """
    if isinstance(br, TieBranch):
        return None, None

    if isinstance(br, SourceBranch):
        node = net.node(br.node_to)
        stage = stage_voltage(node, m)
        prefault = br.voltage_kv or node.prefault_voltage_kv or stage
        z, st = source_impedance(
            br,
            m,
            stage,
            regime,
            prefault_voltage_kv=prefault,
        )
    elif isinstance(br, GeneratorBranch):
        if not br.u_nom:
            raise ValueError(
                f"Генератор «{br.name}»: не задано паспортное Uном. "
                "Сопротивление генератора и приведение к базисной ступени "
                "считать не от чего."
            )
        stage = stage_voltage(net.node(br.node_to), m)
        z, st = generator_impedance(br, m, stage)
    elif isinstance(br, TransformerBranch):
        stage = stage_voltage(transformer_stage_node(net, br), m)
        z, st = transformer_impedance(br, m, stage)
    elif isinstance(br, LineBranch):
        node = net.node(br.node_from)
        stage = stage_voltage(node, m)
        z, st = line_impedance(br, m, regime)
    else:
        return None, None

    z_ref = refer(z, stage, u_base)
    if abs(stage - u_base) > 1e-9 and st is not None:
        st.note = ((st.note + " ") if st.note else "") + (
            f"Приведено со ступени {fmt(stage)} кВ к базисной {fmt(u_base)} кВ: "
            f"Z' = Z · (Uб/U)² = Z · ({fmt(u_base)}/{fmt(stage)})² "
            f"= {fmt(z_ref)} Ом."
        )
    return z_ref, st
