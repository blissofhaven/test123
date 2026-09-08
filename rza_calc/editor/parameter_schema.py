"""Shared, explicit engineering field descriptions for cards and batch input.

Storage paths are relative to equipment.properties. Historical line r0/x0
are positive-sequence impedances and are mapped explicitly, never relabelled.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
import math
import re
from types import SimpleNamespace
from typing import Any

from ..domain.catalog_compatibility import parameter_family, LINE_ALIASES
from ..domain.electrical import EquipmentId, EquipmentTypeId

ALL_FAULTS = ("3ph", "2ph", "1ph_g", "2ph_g")
ASYMMETRIC = ALL_FAULTS[1:]
EARTH_FAULTS = ALL_FAULTS[2:]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    value_kind: str
    unit: str = ""
    group: str = "Основные"
    nullable: bool = True
    choices: tuple[tuple[str, Any], ...] = ()
    editable: bool = True
    storage_path: tuple[str, ...] = ()
    scope: str = "equipment"
    minimum: float | None = None
    maximum: float | None = None
    minimum_inclusive: bool = True
    display_units: tuple[str, ...] = ()
    help_text: str = ""
    side: str = ""
    required_for: tuple[str, ...] = ()
    calculation_use: str = ""
    storage_multiplier: str = "1"
    instance_only: bool = False


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    key: str
    code: str
    message: str
    fault_types: tuple[str, ...] = ()
    severity: str = "warning"


# Factors are exact decimals. A length and an impedance per length are separate
# dimensions: a dropdown can never convert ohms into ohms per kilometre.
_UNITS = {
    "": ("scalar", "1"), "mm": ("length", "0.001"), "m": ("length", "1"), "km": ("length", "1000"),
    "V": ("voltage", "1"), "kV": ("voltage", "1000"),
    "A": ("current", "1"), "kA": ("current", "1000"),
    "kVA": ("apparent_power", "1000"), "MVA": ("apparent_power", "1000000"),
    "kW": ("active_power", "1000"), "MW": ("active_power", "1000000"),
    "s": ("time", "1"), "ms": ("time", "0.001"),
    "ohm": ("impedance", "1"), "ohm/km": ("specific_impedance", "1"),
    "A/km": ("specific_current", "1"), "mm2": ("area", "1"),
    "%": ("relative", "0.01"), "pu": ("relative", "1"),
    "deg": ("angle", "1"), "C": ("temperature", "1"),
}
UNIT_LABELS = {"mm": "мм", "m": "м", "km": "км", "V": "В", "kV": "кВ",
    "A": "А", "kA": "кА", "kVA": "кВА", "MVA": "МВА", "kW": "кВт", "MW": "МВт",
    "ohm": "Ом", "ohm/km": "Ом/км", "A/km": "А/км", "mm2": "мм²",
    "s": "с", "ms": "мс", "pu": "о.е.", "deg": "°", "C": "°C"}


def unit_label(unit):
    return UNIT_LABELS.get(unit, unit)


def _convert(value: Decimal, source: str, target: str) -> Decimal:
    if source == target:
        return value
    a, b = _UNITS.get(source), _UNITS.get(target)
    if a is None or b is None or a[0] != b[0]:
        raise ValueError("Несовместимые единицы измерения.")
    with localcontext() as context:
        context.prec = max(50, len(value.as_tuple().digits) + 16)
        return value * Decimal(a[1]) / Decimal(b[1])


def _display_unit(spec, display_unit):
    unit = spec.unit if display_unit is None else display_unit
    if unit not in (spec.unit, *spec.display_units):
        raise ValueError(f"{spec.label}: эта единица измерения не разрешена.")
    return unit


def parse_parameter_value(spec, text, display_unit=None):
    """Parse a visible value without substituting zero for an empty field."""
    unit = _display_unit(spec, display_unit)
    if isinstance(text, str):
        text = text.strip()
    if text is None or text == "":
        value = None
    elif spec.value_kind == "string":
        value = str(text)
    elif spec.value_kind in {"boolean", "enum"}:
        options = spec.choices or (("Да", True), ("Нет", False))
        matches = [value for label, value in options if (type(text) is type(value) and text == value)
                   or (isinstance(text, str) and text.casefold() in {str(label).casefold(), str(value).casefold()})]
        if not matches:
            raise ValueError(f"{spec.label}: выберите значение из списка.")
        value = matches[0]
    elif spec.value_kind in {"number", "integer"}:
        if isinstance(text, bool):
            raise ValueError(f"{spec.label}: требуется число, а не логический признак.")
        raw = str(text).replace("\u00a0", " ").replace("\u202f", " ")
        pattern = r"[+-]?(?:\d+|\d{1,3}(?: \d{3})+)(?:[.,]\d*)?(?:[eE][+-]?\d+)?"
        if not re.fullmatch(pattern, raw) and not re.fullmatch(r"[+-]?[.,]\d+(?:[eE][+-]?\d+)?", raw):
            raise ValueError(f"{spec.label}: введите число; запятая и точка допустимы.")
        try:
            number = Decimal(raw.replace(" ", "").replace(",", "."))
            if not number.is_finite() or (number and abs(number.adjusted()) > 308):
                raise ValueError(f"{spec.label}: число вне допустимого диапазона.")
            number = _convert(number, unit, spec.unit)
            if spec.value_kind == "integer":
                if number != number.to_integral_value():
                    raise ValueError(f"{spec.label}: значение должно быть целым в единицах {unit_label(spec.unit)}.")
                value = int(number)
            else:
                value = float(number)
        except (InvalidOperation, OverflowError) as exc:
            raise ValueError(f"{spec.label}: неверное числовое значение.") from exc
    else:
        raise ValueError(f"{spec.label}: для этого поля нужен специальный редактор.")
    error = validate_field_value(spec, value)
    if error:
        raise ValueError(error)
    return value


def format_parameter_value(spec, value, display_unit=None):
    unit = _display_unit(spec, display_unit)
    if value is None:
        return ""
    if spec.value_kind in {"boolean", "enum"}:
        options = spec.choices or (("Да", True), ("Нет", False))
        return next((label for label, candidate in options if type(candidate) is type(value) and candidate == value), str(value))
    if spec.value_kind not in {"number", "integer"}:
        return str(value)
    number = _convert(Decimal(str(value)), spec.unit, unit)
    if not number.is_finite():
        return str(value)
    if number and (number.adjusted() > 15 or number.adjusted() < -8):
        return str(number.normalize()).replace(".", ",")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result.replace(".", ",")


def schemas_for(model, equipment) -> tuple[FieldSpec, ...]:
    """Return an explicit native or compatibility schema, with no model writes."""
    type_id = str(equipment.type_id)
    family = parameter_family(type_id)
    legacy = type_id.startswith("compat.rza_calc.")
    prefix = ("legacy_payload",) if legacy else ()
    result = [FieldSpec("name", "Название", "string", nullable=False, scope="identity",
                        storage_path=("name",), instance_only=True)]

    def add(key, label, kind="number", unit="", group="Паспорт", *, path=None, **kwargs):
        display = {"mm": ("m", "km"), "kVA": ("kVA", "MVA"), "MVA": ("kVA", "MVA"),
            "A": ("A", "kA"), "kA": ("A", "kA"), "kV": ("V", "kV"),
            "kW": ("kW", "MW"), "MW": ("kW", "MW"), "s": ("s", "ms")}.get(unit, ())
        result.append(FieldSpec(key, label, kind, unit=unit, group=group,
            storage_path=prefix + ((key,) if path is None else tuple(path)), display_units=display, **kwargs))

    def positive(key, label, unit="", **kwargs):
        add(key, label, unit=unit, minimum=0, minimum_inclusive=False, **kwargs)

    if family == "source":
        for system, title in (("max", "Максимальный режим"), ("min", "Минимальный режим")):
            add("input_mode_" + system, "Способ задания источника", "enum", group=title,
                choices=(("Мощность КЗ", "power"), ("Ток КЗ", "current")))
            positive("s_kz_" + system, "Мощность КЗ", "MVA", group=title)
            positive("i_kz_" + system, "Ток трёхфазного КЗ", "kA", group=title)
        positive("voltage_kv", "Напряжение источника", "kV", side="Вывод источника")
        add("x_r_ratio", "Отношение X/R", minimum=0, help_text="Пустое поле сохраняет существующую модель чисто индуктивного источника; это допущение отображается в расчёте. Ноль означает чисто активное сопротивление.")
    elif family == "generator":
        positive("s_nom", "Полная номинальная мощность", "kVA")
        positive("p_nom", "Активная номинальная мощность", "MW")
        positive("cos_phi", "Номинальный cos φ", maximum=1)
        positive("u_nom", "Номинальное напряжение", "kV", required_for=ALL_FAULTS, side="Вывод генератора")
        positive("xd2", "Сверхпереходное сопротивление x″d", "pu", required_for=ALL_FAULTS,
                 help_text="Паспортное x″d генератора. Это не сопротивление обратной последовательности Z2.")
        add("r_pu", "Активное сопротивление генератора", unit="pu", minimum=0)
    elif family in {"transformer_2w", "transformer_3w"}:
        positive("s_nom", "Номинальная мощность", "kVA", required_for=ALL_FAULTS)
        for side in (("hv", "ВН"), ("mv", "СН"), ("lv", "НН")):
            if side[0] == "mv" and family != "transformer_3w":
                continue
            positive("u_" + side[0], "Напряжение обмотки " + side[1], "kV", side=side[1], required_for=ALL_FAULTS)
        if family == "transformer_2w":
            positive("uk", "Напряжение КЗ uk", "%", required_for=ALL_FAULTS)
        else:
            for key, pair in (("uk_hm", "ВН–СН"), ("uk_hl", "ВН–НН"), ("uk_ml", "СН–НН")):
                positive(key, "Напряжение КЗ " + pair, "%", required_for=("3ph",))
        add("p_k", "Потери КЗ", unit="kW", minimum=0)
        add("group", "Группа соединения обмоток", "string")
        positive("i_inrush_ratio", "Кратность броска намагничивания")
        if family == "transformer_2w":
            add("z_loop", "Паспортное сопротивление петли фаза–нуль", unit="ohm", minimum=0,
                help_text="Параметр прежней методики КЗ 0,4 кВ; он не заменяет эквивалент Z0 для четырёх видов КЗ.")
    elif family == "line":
        physical = type_id.startswith("builtin.line_section.")
        old_names = not physical
        names = {value: key for key, value in LINE_ALIASES.items()} if old_names else {}
        add("line_type", "Вид линии", "enum", editable=False,
            choices=(("Кабельная линия", "cable"), ("Воздушная линия", "overhead")))
        add("length_mm", "Длина", "integer", unit="mm", path=("length_mm",) if physical else ("length_km",),
            scope="line_section" if physical else "equipment", minimum=0, minimum_inclusive=False,
            storage_multiplier="1" if physical else "0.000001", instance_only=True, required_for=ALL_FAULTS)
        for key, label, kind, unit in (("conductor_mark", "Марка", "string", ""),
                ("material", "Материал жилы/провода", "string", ""),
                ("cross_section_mm2", "Сечение", "number", "mm2"),
                ("parallel_count", "Параллельных цепей", "integer", "")):
            add(key, label, kind, unit, path=(names.get(key, key),),
                minimum=1 if key == "parallel_count" else 0 if key == "cross_section_mm2" else None,
                minimum_inclusive=key != "cross_section_mm2", instance_only=key == "parallel_count")
        for seq in (1, 2, 0):
            for component in ("r", "x"):
                key = f"{component}{seq}_ohm_per_km"
                add(key, f"{component.upper()}{seq}", unit="ohm/km", group="Сопротивления линии",
                    path=(names.get(key, key),), minimum=0 if component == "r" or seq == 1 else None,
                    required_for=ALL_FAULTS if seq == 1 else ())
        add("capacitive_current_a_per_km", "Удельный ёмкостный ток", unit="A/km", minimum=0,
            path=(names.get("capacitive_current_a_per_km", "capacitive_current_a_per_km"),),
            help_text="Исходное значение прежнего расчёта ОЗЗ. Полная ёмкостная схема последовательностей вводится отдельным этапом.")
    elif family == "switch":
        for key, label, unit in (("rated_current_a", "Номинальный ток", "A"),
                ("rated_breaking_current_a", "Номинальный ток отключения", "A"),
                ("thermal_short_time_current_a", "Ток термической стойкости", "A"),
                ("thermal_duration_s", "Время термической стойкости", "s"),
                ("dynamic_peak_current_a", "Ток электродинамической стойкости", "A")):
            positive(key, label, unit, path=(key,), scope="nameplate",
                calculation_use="Паспорт; автоматическая проверка стойкости — следующий этап")
            result[-1] = replace(result[-1], storage_path=(key,))
        for key, label in (("manufacturer", "Изготовитель"), ("model", "Модель аппарата")):
            add(key, label, "string", scope="nameplate")
            result[-1] = replace(result[-1], storage_path=(key,))
    elif family == "load":
        add("p_kw", "Активная мощность", unit="kW", minimum=0)
        positive("cos_phi", "cos φ нагрузки", maximum=1)
        add("k_use", "Коэффициент использования", minimum=0, maximum=1)
        positive("k_szp", "Коэффициент самозапуска")
        add("motor_share", "Доля двигательной нагрузки", minimum=0, maximum=1,
            calculation_use="Исходные данные; двигательная подпитка КЗ пока не рассчитывается")

    # Preserve collected passport inputs without inventing a physical model.
    def passport(key, label, kind="number", unit="", *, group="Дополнительные исходные данные", side="", **kwargs):
        add(key, label, kind, unit, group, scope="nameplate", side=side,
            calculation_use="Сохраняется в карточке; автоматическая физическая модель — следующий этап", **kwargs)
        result[-1] = replace(result[-1], storage_path=(key,))

    if family == "line":
        passport("temperature_c", "Температура проводника", unit="C", minimum=-100, maximum=500,
            help_text="Сейчас минимальный режим использует коэффициент температуры из методики. Это поле пока не заменяет его.")
        passport("reference_temperature_c", "Температура паспортного R", unit="C", minimum=-100, maximum=500)
    if family in {"source", "generator", "transformer_2w", "transformer_3w"}:
        sides = (("hv", "ВН"), ("lv", "НН")) if family.startswith("transformer") else (("terminal", "Вывод"),)
        if family == "transformer_3w":
            sides = (("hv", "ВН"), ("mv", "СН"), ("lv", "НН"))
        for key, label in sides:
            passport(f"neutral_{key}", "Нейтраль — " + label, "enum", side=label,
                choices=(("Изолирована", "isolated"), ("Глухозаземлена", "grounded"),
                         ("Резистор", "resistor"), ("Реактор", "reactor")),
                help_text="Описание нейтрали не заменяет явно заданные Z0 и его схему. Дополнительно 3Zn к заданному Z0 не прибавляется.")
            passport(f"neutral_r_{key}_ohm", "R нейтрали — " + label, unit="ohm", minimum=0, side=label)
            passport(f"neutral_x_{key}_ohm", "X нейтрали — " + label, unit="ohm", side=label)
    if family.startswith("transformer"):
        passport("tap_position", "Положение переключателя ответвлений", "integer")
        passport("tap_step_percent", "Шаг регулирования", unit="%", minimum=0)
        passport("tap_winding", "Регулируемая обмотка", "string")

    if family in {"source", "generator", "transformer_2w", "line"}:
        add("negative_sequence_equal_positive", "Явно принять Z2 = Z1", "boolean", group="Последовательности",
            help_text="Только явно принятое допущение; не выбирается автоматически.")
        if family != "line":
            for sequence in (2, 0):
                for component in ("r", "x"):
                    add(f"{component}{sequence}_ohm", f"{component.upper()}{sequence} эквивалента", unit="ohm",
                        group="Последовательности", minimum=0 if component == "r" else None)
            positive("sequence_reference_kv", "Ступень напряжения эквивалента", "kV", group="Последовательности")
        add("zero_sequence_connection", "Схема нулевой последовательности", "enum", group="Последовательности",
            choices=(("Продольная ветвь", "series"), ("Сторона начала — земля", "from_ground"),
                     ("Сторона конца — земля", "to_ground"), ("Путь отсутствует", "blocked")),
            help_text="Z0 задаётся как явный эквивалент с уже включённым 3Zn. Пустое поле не означает отсутствующий путь.")
        if family == "transformer_2w":
            add("sequence_phase_shift_deg", "Сдвиг фаз от начала к концу", unit="deg", group="Последовательности",
                required_for=ASYMMETRIC, side="Начало → конец", help_text="Укажите явно, включая 0°. Не выводится автоматически из текста группы соединения.")

    if family in {"source", "generator"}:
        for system, title in (("max", "Эквиваленты — max"), ("min", "Эквиваленты — min")):
            for key, label, unit in (("r2_ohm", "R2", "ohm"), ("x2_ohm", "X2", "ohm"),
                    ("r0_ohm", "R0", "ohm"), ("x0_ohm", "X0", "ohm"),
                    ("sequence_reference_kv", "Ступень напряжения", "kV")):
                add(f"sequence_by_system.{system}.{key}", label, unit=unit, group=title,
                    path=("sequence_by_system", system, key), minimum=0 if key.startswith("r") or key.endswith("kv") else None,
                    minimum_inclusive=not key.endswith("kv"), help_text="Пустое поле использует общее значение выше; при отсутствии обоих данных расчёт этого вида КЗ недоступен.")

    if family not in {"load", "custom"}:
        for key, label, index in (("ct_primary_a", "Первичный ток ТТ", "0"), ("ct_secondary_a", "Вторичный ток ТТ", "1")):
            positive(key, label, "A", group="ТТ присоединения", path=("ct_ratio", index))
        choices = []
        for port_id in getattr(equipment, "port_ids", ()):
            port = model.ports.get(port_id)
            if port is not None:
                node = model.node_for_port(port_id)
                roles = {"from": "Начало", "to": "Конец", "start": "Начало", "end": "Конец",
                         "terminal": "Вывод", "hv": "ВН", "mv": "СН", "lv": "НН",
                         "primary": "Первичная сторона", "secondary": "Вторичная сторона"}
                title = roles.get(str(port.role), str(port.role)) + (" — " + node.name if node is not None else " — не подключён")
                choices.append((title, port_id.value))
        add("ct_port", "Сторона установки ТТ", "enum", group="ТТ присоединения", choices=tuple(choices),
            scope="ct_port", path=(), instance_only=True, editable=bool(choices),
            help_text="Физический вывод аппарата. Положение ТТ не определяется направлением нарисованной стрелки.")
        if family != "transformer_3w":
            add("ct_accuracy", "Класс точности ТТ", "string", group="ТТ присоединения")
        add("breaker_t_off", "Полное время отключения выключателя", unit="s", minimum=0, group="Паспорт")
        add("terminal", "Тип терминала РЗА", "string", group="ТТ присоединения")
        if not legacy:
            ct_keys = {"ct_primary_a", "ct_secondary_a", "ct_accuracy", "breaker_t_off", "terminal"}
            result = [replace(spec, scope="ct") if spec.key in ct_keys else spec for spec in result]
        if family == "transformer_3w":
            add("ct_accuracy", "Класс точности ТТ", "string", group="ТТ присоединения", scope="nameplate")
            result[-1] = replace(result[-1], storage_path=("ct_accuracy",))

    stored_only = {"ct_accuracy", "breaker_t_off", "terminal", "k_szp", "z_loop"}
    result = [replace(spec, calculation_use="Сохраняется в карточке; текущие формулы защит этот параметр не используют")
              if spec.key in stored_only else spec for spec in result]

    if family == "custom":
        definition = model.equipment_type(equipment.type_id, equipment.type_version)
        for prop in definition.property_definitions:
            if prop.value_kind in {"number", "integer", "boolean", "string"}:
                add(prop.key, prop.key, prop.value_kind, prop.unit, nullable=not prop.required)
    return tuple(result)


def catalog_field_specs(model, type_id, type_version=1, properties=None):
    equipment = SimpleNamespace(id=EquipmentId("catalog.preview"), type_id=EquipmentTypeId(str(type_id)),
        type_version=type_version, properties=properties or {}, port_ids=(), extensions={})
    return tuple(replace(spec, editable=True) if spec.key == "line_type" and str(type_id) == "compat.rza_calc.line" else spec
                 for spec in schemas_for(model, equipment)
                 if not spec.instance_only and spec.scope in {"equipment", "nameplate", "ct"})


def validate_field_value(spec: FieldSpec, value: Any) -> str | None:
    """Return a user-facing error for an invalid canonical value, otherwise None."""
    if value is None:
        return None if spec.nullable else f"{spec.label}: значение обязательно."
    if spec.value_kind == "string":
        if not isinstance(value, str) or (not spec.nullable and not value.strip()):
            return f"{spec.label}: требуется непустой текст." if not spec.nullable else f"{spec.label}: требуется текст."
    elif spec.value_kind == "boolean":
        if type(value) is not bool:
            return f"{spec.label}: выберите «Да» или «Нет»."
    elif spec.value_kind == "enum":
        if not any(type(value) is type(item) and value == item for _, item in spec.choices):
            return f"{spec.label}: значение отсутствует в списке."
    elif spec.value_kind in {"number", "integer"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{spec.label}: требуется число."
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            return f"{spec.label}: число должно быть конечным."
        if spec.value_kind == "integer" and type(value) is not int:
            return f"{spec.label}: требуется целое число."
        if spec.minimum is not None and (value < spec.minimum or value == spec.minimum and not spec.minimum_inclusive):
            return f"{spec.label}: значение должно быть {'не меньше' if spec.minimum_inclusive else 'больше'} {spec.minimum:g}."
        if spec.maximum is not None and value > spec.maximum:
            return f"{spec.label}: значение должно быть не больше {spec.maximum:g}."
    else:
        return f"{spec.label}: неизвестный тип поля."
    return None


def parameter_readiness(specs, values, *, family: str = "") -> tuple:
    """Per-fault completeness; absence remains distinct from an explicit zero."""
    issues = []
    by_key = {spec.key: spec for spec in specs}
    def raw(key):
        value = values.get(key)
        return getattr(value, "value", value)
    def require(key, faults, message=None):
        spec = by_key.get(key)
        if spec is None:
            return
        value = values.get(key)
        if raw(key) is None or raw(key) == "":
            issues.append(ReadinessIssue(key, "missing_parameter", message or f"Не задано: {spec.label}", faults))
        elif hasattr(value, "confirmation") and str(value.confirmation) != "confirmed":
            issues.append(ReadinessIssue(key, "unconfirmed_parameter", f"Не подтверждено: {spec.label}", faults))
    for spec in specs:
        if spec.required_for:
            require(spec.key, spec.required_for)
        if raw(spec.key) is not None:
            error = validate_field_value(spec, raw(spec.key))
            if error:
                issues.append(ReadinessIssue(spec.key, "invalid_parameter", error, spec.required_for))
    if family == "source" or "s_kz_max" in by_key:
        for mode in ("max", "min"):
            selector = raw("input_mode_" + mode)
            if selector is not None:
                require("input_mode_" + mode, ALL_FAULTS)
            power, current = raw("s_kz_" + mode), raw("i_kz_" + mode)
            if selector == "power":
                require("s_kz_" + mode, ALL_FAULTS)
            elif selector == "current":
                require("i_kz_" + mode, ALL_FAULTS)
            elif power is not None and current is not None:
                issues.append(ReadinessIssue("input_mode_" + mode, "ambiguous_source_input", "Выберите мощность или ток КЗ для режима " + mode, ALL_FAULTS))
            else:
                require(("s_kz_" if power is not None or current is None else "i_kz_") + mode, ALL_FAULTS)
        for key in ("x_r_ratio", "voltage_kv"):
            if raw(key) is not None:
                require(key, ALL_FAULTS)
    if family == "generator" or "xd2" in by_key:
        if raw("s_nom") is not None:
            require("s_nom", ALL_FAULTS)
        else:
            require("p_nom", ALL_FAULTS)
            require("cos_phi", ALL_FAULTS)
        if raw("r_pu") is not None:
            require("r_pu", ALL_FAULTS)
    if family.startswith("transformer") and raw("p_k") is not None:
        require("p_k", ALL_FAULTS)
    if family == "line" and raw("parallel_count") is not None:
        require("parallel_count", ALL_FAULTS)
    if family == "load":
        for key in ("p_kw", "cos_phi", "k_use"):
            require(key, (), "Для защит не задано: " + by_key[key].label)
    if "negative_sequence_equal_positive" in by_key:
        equal = raw("negative_sequence_equal_positive") is True
        line = "r2_ohm_per_km" in by_key
        suffix = "_ohm_per_km" if line else "_ohm"
        mode_specific = any(key.startswith("sequence_by_system.") for key in by_key)
        if equal and any(raw(key) is not None for key in by_key if key in {"r2_ohm", "x2_ohm", "r2_ohm_per_km", "x2_ohm_per_km"}
                         or key.startswith("sequence_by_system.") and key.endswith((".r2_ohm", ".x2_ohm"))):
            issues.append(ReadinessIssue("negative_sequence_equal_positive", "conflicting_sequence_inputs",
                "Одновременно заданы Z2 и допущение Z2 = Z1. Выберите один способ задания.", ASYMMETRIC))
        for seq, faults in ((2, ASYMMETRIC), (0, EARTH_FAULTS)):
            if seq == 2 and equal:
                require("negative_sequence_equal_positive", faults)
                continue
            if seq == 0 and raw("zero_sequence_connection") == "blocked":
                require("zero_sequence_connection", faults)
                continue
            modes = ("max", "min") if mode_specific else (None,)
            for mode in modes:
                for component in ("r", "x"):
                    key = f"{component}{seq}{suffix}"
                    override = f"sequence_by_system.{mode}.{key}"
                    require(override if mode is not None and raw(override) is not None else key, faults)
                if not line:
                    override = f"sequence_by_system.{mode}.sequence_reference_kv"
                    require(override if mode is not None and raw(override) is not None else "sequence_reference_kv", faults)
        if not line:
            require("zero_sequence_connection", EARTH_FAULTS)
    if family == "transformer_3w":
        issues.append(ReadinessIssue("group", "unsupported_sequence_model", "Для трёхобмоточного трансформатора не принята физическая модель несимметричных КЗ.", ASYMMETRIC))
    if "ct_primary_a" in by_key and any(raw(key) is not None for key in ("ct_primary_a", "ct_secondary_a")):
        for key in ("ct_primary_a", "ct_secondary_a", "ct_port"):
            require(key, (), "Для защит не задано: " + by_key[key].label)
    unique = {(row.key, row.code, row.message, row.fault_types): row for row in issues}
    return tuple(unique.values())
