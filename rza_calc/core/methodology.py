# -*- coding: utf-8 -*-
"""
Профиль методики расчёта.

Ни один коэффициент не зашит в код расчётных модулей. Всё берётся отсюда,
и рядом с каждым числом в протоколе печатается, из какого профиля оно взято.
Это позволяет переключить предприятие/проект, не трогая ядро.

Происхождение коэффициента
--------------------------
У каждого коэффициента есть не только значение, но и происхождение: откуда
оно взято и насколько это подтверждено. Без этого «1,2» в протоколе выглядит
одинаково убедительно и когда оно взято из ПУЭ, и когда его кто-то поставил
наугад. Поля ``origin``/``source``/``source_status`` разводят эти случаи и
попадают в протокол рядом с числом.

Статус профиля — это ЗАЯВКА, которую содержимое обязано подтверждать
--------------------------------------------------------------------
* ``ЗАГЛУШКА``  — профиль ничего не утверждает; расчёт помечается как
  негодный для реального объекта;
* ``ТИПОВОЙ``   — заявлено, что у каждого коэффициента назван источник;
* ``ПРОЕКТНЫЙ`` — заявлено, что источники подтверждены по тексту документов
  и профиль утверждён названным лицом для названного объекта.

Профиль, заявляющий статус, которому не отвечает его собственное содержимое,
блокирует расчёт. Не потому, что от пустого поля «источник» портятся числа, а
потому, что уставки уходят в эксплуатацию вместе с этой заявкой: неверная
заявка о происхождении опаснее отсутствующей.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "methodology_default.json"

#  Статусы профиля, от самого слабого к самому сильному.
PLACEHOLDER = "ЗАГЛУШКА"
TYPICAL = "ТИПОВОЙ"
PROJECT = "ПРОЕКТНЫЙ"
STATUSES = (PLACEHOLDER, TYPICAL, PROJECT)

#  Закрытый словарь происхождений. Свободный текст здесь быстро превращается в
#  набор синонимов, по которому уже нельзя ни отфильтровать, ни проверить.
ORIGINS = (
    "нормативное требование",   # прямо задано нормативным документом
    "типовая практика",         # общепринято, но жёстко не предписано
    "паспорт оборудования",     # свойство конкретного терминала/ТТ/трансформатора
    "решение проекта",          # назначает проектировщик для объекта
    "решение методики",         # выбор способа расчёта, а не числа
    "определение защиты",       # вытекает из определения самой защиты
    "следствие допущения",      # вытекает из принятой расчётной модели
    "численный параметр",       # параметр реализации, физического смысла не несёт
)

UNCONFIRMED = "пункт не подтверждён"
CONFIRMED = "подтверждён"
SOURCE_STATUSES = (UNCONFIRMED, CONFIRMED)

APPROVAL_FIELDS = ("approved_by", "approved_on", "object")

# Файл профиля может сузить выбор, но не добавить несуществующий алгоритм.
IMPLEMENTED_CHOICES = {"to.i_nom_basis": ("nameplate", "stage_average")}
SNAPSHOT_SCHEMA_VERSION = 1


class MethodologyError(KeyError):
    pass


def _text_or_empty(value: Any) -> str:
    """JSON null, число или контейнер не являются заполненным текстом."""
    return value if isinstance(value, str) else ""


def _canonical_json(value: Any) -> str:
    """Полный JSON без потери типов, ключей и неизвестных полей профиля."""
    def validate(item: Any) -> None:
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("Ключи снимка методики должны быть строками JSON.")
            for child in item.values():
                validate(child)
            return
        raise ValueError(f"Снимок методики содержит не-JSON тип {type(item).__name__}.")

    try:
        validate(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, RecursionError) as exc:
        raise ValueError(f"Некорректный JSON снимка методики: {exc}") from exc


def _profile_from_json(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError("profile_json снимка методики должен быть строкой.")
    try:
        profile = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("profile_json снимка методики содержит некорректный JSON.") from exc
    if not isinstance(profile, dict):
        raise ValueError("profile_json снимка методики должен содержать объект.")
    # Сравнение также запрещает NaN/Infinity, повторные ключи и неканонические
    # записи: ни одна часть сохранённого профиля не должна молча теряться.
    if _canonical_json(profile) != raw:
        raise ValueError("profile_json снимка методики не является каноническим JSON.")
    return profile


@dataclass(frozen=True)
class Provenance:
    """Происхождение одного коэффициента — то, что печатается рядом с числом."""

    path: str
    title: str
    origin: str
    source: str
    source_status: str

    @property
    def confirmed(self) -> bool:
        return self.source_status == CONFIRMED

    def line(self) -> str:
        mark = "" if self.confirmed else f"  [{UNCONFIRMED}]"
        origin = self.origin or "происхождение не указано"
        source = self.source or "источник не указан"
        return f"{self.path}: {origin} — {source}{mark}"


@dataclass(frozen=True)
class MethodologyEntry:
    """Одно применённое значение вместе с его происхождением."""

    path: str
    value: float | str | bool | None
    unit: str
    provenance: Provenance


@dataclass(frozen=True)
class MethodologySnapshot:
    """Неизменяемый снимок фактически применённых коэффициентов.

    Профиль лежит во внешнем файле, и файл этот может быть изменён после
    расчёта. Тогда протокол годичной давности начинал бы ссылаться на числа,
    по которым он никогда не считался. Снимок закрывает эту дыру: он
    складывается в паспорт расчёта и дальше не меняется вместе с файлом.
    """

    profile_id: str
    profile_name: str
    status: str
    entries: tuple[MethodologyEntry, ...]
    u_avg: tuple[tuple[str, float], ...]
    ct_scale: tuple[float, ...]
    ct_round_mode: str
    approval: tuple[tuple[str, str], ...]
    references: tuple[tuple[str, str], ...]
    profile_json: str

    def entry(self, path: str) -> MethodologyEntry:
        for item in self.entries:
            if item.path == path:
                return item
        raise MethodologyError(f"В снимке методики нет параметра '{path}'.")

    def value(self, path: str) -> float | str | bool | None:
        return self.entry(path).value

    def as_dict(self) -> dict[str, Any]:
        """Версионированный, независимый JSON-payload паспорта расчёта.

        Структурированные поля удобны для отчёта; profile_json сохраняет
        профиль целиком, включая метаданные и будущие неизвестные поля.
        """
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "profile_json": self.profile_json,
            "profile_sha256": hashlib.sha256(self.profile_json.encode("utf-8")).hexdigest(),
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "status": self.status,
            "approval": dict(self.approval),
            "references": dict(self.references),
            "u_avg": dict(self.u_avg),
            "ct_scale": list(self.ct_scale),
            "ct_round_mode": self.ct_round_mode,
            "entries": [
                {
                    "path": item.path,
                    "value": item.value,
                    "unit": item.unit,
                    "title": item.provenance.title,
                    "origin": item.provenance.origin,
                    "source": item.provenance.source,
                    "source_status": item.provenance.source_status,
                }
                for item in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MethodologySnapshot":
        """Восстановить снимок без обращения к текущему/типовому профилю.

        Все зеркальные поля сверяются с полным профилем. Отсутствующие поля,
        неизвестная версия и противоречащие друг другу данные — ошибки, а не
        приглашение подставить значение из установленной версии программы.
        """
        if not isinstance(payload, dict):
            raise ValueError("Снимок методики должен быть объектом JSON.")
        version = payload.get("schema_version")
        if type(version) is not int or version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"Неподдерживаемая версия снимка методики: {version!r}.")
        profile = _profile_from_json(payload.get("profile_json"))
        try:
            expected = Methodology.from_dict(profile).snapshot()
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError(f"Повреждённый профиль в снимке методики: {exc}") from exc
        if _canonical_json(payload) != _canonical_json(expected.as_dict()):
            raise ValueError("Поля снимка методики не согласованы с полным профилем.")
        return expected

    def to_methodology(self) -> "Methodology":
        """Независимый расчётный профиль из проверенного сохранённого снимка."""
        validated = self.from_dict(self.as_dict())
        return Methodology.from_dict(_profile_from_json(validated.profile_json))


@dataclass
class Methodology:
    data: dict[str, Any]
    path: Path | None = None

    # ---------- загрузка ----------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Methodology":
        p = Path(path) if path else DEFAULT_PATH
        with open(p, encoding="utf-8") as f:
            return cls(json.load(f), p)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Methodology":
        return cls(d, None)

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ---------- доступ ----------
    @property
    def name(self) -> str:
        return self.data.get("name", self.data.get("id", "без имени"))

    @property
    def status(self) -> str:
        return str(self.data.get("status", PLACEHOLDER))

    @property
    def is_placeholder(self) -> bool:
        return self.status == PLACEHOLDER

    @property
    def requires_approval(self) -> bool:
        """Профиль ещё не утверждён для конкретного объекта."""
        return self.status != PROJECT

    @property
    def approval(self) -> dict[str, Any]:
        block = self.data.get("approval")
        return dict(block) if isinstance(block, dict) else {}

    @property
    def warning(self) -> str | None:
        return self.data.get("warning")

    def k(self, path: str) -> float:
        """Коэффициент по пути вида 'mtz.k_ots'."""
        node = self._node(path)
        if isinstance(node, dict) and "value" in node:
            value = node["value"]
            if isinstance(value, str):
                raise MethodologyError(
                    f"'{path}' содержит текстовое значение '{value}'. "
                    "Числовые коэффициенты читаются через k(), выбор варианта — "
                    "через text()."
                )
            return float(value)
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            return float(node)
        raise MethodologyError(f"'{path}' не содержит числового значения")

    def text(self, path: str) -> str:
        """Параметр-выбор: не число, а один из перечисленных вариантов.

        Такие параметры записывают в методику принятое РЕШЕНИЕ (например, на
        каком напряжении считать номинальный ток), а не измеренную величину.
        Значение вне реализованного списка — ошибка профиля, а не повод молча
        взять умолчание: расчёт по неизвестному варианту был бы расчётом
        неизвестно по чему.
        """
        node = self._node(path)
        if not isinstance(node, dict) or "value" not in node:
            raise MethodologyError(f"'{path}' не содержит значения")
        value = node["value"]
        if not isinstance(value, str):
            raise MethodologyError(f"'{path}' должен содержать строковый вариант.")
        implemented = IMPLEMENTED_CHOICES.get(path)
        if implemented is not None and value not in implemented:
            raise MethodologyError(
                f"'{path}' имеет значение '{value}', которого нет среди "
                f"допустимых реализованных вариантов: {', '.join(implemented)}."
            )
        options = node.get("options")
        if options is not None and (
            not isinstance(options, list)
            or not all(isinstance(option, str) for option in options)
        ):
            raise MethodologyError(f"'{path}.options' должен быть списком строк.")
        if implemented is not None and options is not None and any(
            option not in implemented for option in options
        ):
            raise MethodologyError(
                f"'{path}.options' содержит нереализованный вариант; список "
                f"допустимых: {', '.join(implemented)}."
            )
        if options is not None and value not in options:
            raise MethodologyError(
                f"'{path}' имеет значение '{value}', которого нет среди "
                f"допустимых: {', '.join(map(str, options))}."
            )
        return value

    def title(self, path: str) -> str:
        node = self._node(path)
        if isinstance(node, dict):
            return node.get("title", path)
        return path

    # ---------- происхождение ----------
    def provenance(self, path: str) -> Provenance:
        """Происхождение конкретного коэффициента.

        Если у самого узла происхождения нет, оно ищется у секции (`meta`) и
        только затем у профиля в целом. Подмены не происходит: возвращённая
        строка всегда содержит путь, по которому происхождение найдено.
        """
        node = self._node_or_none(path)
        if isinstance(node, dict) and (node.get("origin") or node.get("source")):
            return Provenance(
                path=path,
                title=str(node.get("title", path)),
                origin=_text_or_empty(node.get("origin", "")),
                source=_text_or_empty(node.get("source", "")),
                source_status=_text_or_empty(node.get("source_status", UNCONFIRMED)),
            )
        meta = self._node_or_none(f"{path}.meta")
        if isinstance(meta, dict) and (meta.get("origin") or meta.get("source")):
            return Provenance(
                path=path,
                title=str(meta.get("title", path)),
                origin=_text_or_empty(meta.get("origin", "")),
                source=_text_or_empty(meta.get("source", "")),
                source_status=_text_or_empty(meta.get("source_status", UNCONFIRMED)),
            )
        return Provenance(
            path=path,
            title=str(node.get("title", path)) if isinstance(node, dict) else path,
            origin="",
            source=_text_or_empty(self.data.get("source")),
            source_status=UNCONFIRMED,
        )

    def cite(self, path: str) -> str:
        """Строка «откуда взято» для протокола."""
        prov = self.provenance(path)
        flag = {
            PLACEHOLDER: "  [ЗАГЛУШКА — заменить]",
            TYPICAL: "  [не утверждено для объекта]",
        }.get(self.status, "")
        return f"профиль «{self.name}» · {prov.line()}{flag}"

    def u_avg(self, u_nom_kv: float) -> float:
        """Среднее номинальное напряжение ступени."""
        table = self.data["short_circuit"]["u_avg"]
        key = _fmt_key(u_nom_kv)
        if key in table:
            value = float(table[key])
            if value <= 0:
                raise MethodologyError(
                    f"Среднее расчётное напряжение для ступени {u_nom_kv} кВ "
                    "должно быть больше нуля."
                )
            return value
        raise MethodologyError(
            f"В профиле нет явного среднего расчётного напряжения для ступени "
            f"{u_nom_kv} кВ. Автоматический выбор ближайшей ступени запрещён; "
            "добавьте значение в short_circuit.u_avg."
        )

    def ct_scale(self) -> list[float]:
        return [float(x) for x in self.data["ct"]["secondary_scale"]]

    def references(self) -> dict[str, str]:
        """Расшифровка коротких обозначений источников.

        В поле ``source`` коэффициента стоит короткий ключ («Шабад», «ПУЭ»),
        иначе строка происхождения перестала бы читаться. Полные названия
        документов лежат здесь и печатаются в протоколе один раз.
        """
        block = self.data.get("references")
        return dict(block) if isinstance(block, dict) else {}

    def _node(self, path: str) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise MethodologyError(
                    f"В профиле методики «{self.name}» нет параметра '{path}'. "
                    f"Добавьте его в файл профиля."
                )
            node = node[part]
        return node

    def _node_or_none(self, path: str) -> Any:
        try:
            return self._node(path)
        except MethodologyError:
            return None

    # ---------- снимок применённых значений ----------
    def snapshot(self) -> MethodologySnapshot:
        """Неизменяемый слепок всего, что участвует в расчёте."""
        profile_json = _canonical_json(self.data)
        # Дальше читается только независимая JSON-копия: ни вложенные
        # контейнеры, ни метаданные не остаются ссылками на живой профиль.
        captured = Methodology.from_dict(_profile_from_json(profile_json))
        problems = captured.blocking_errors()
        if problems:
            raise ValueError("Нельзя сохранить некорректную методику:\n- "
                             + "\n- ".join(problems))
        return captured._snapshot_from_canonical(profile_json)

    def _snapshot_from_canonical(self, profile_json: str) -> MethodologySnapshot:
        entries: list[MethodologyEntry] = []
        for path in self.SNAPSHOT_PATHS:
            node = self._node_or_none(path)
            if isinstance(node, dict) and "value" in node:
                value = node["value"]
                unit = str(node.get("unit", ""))
            elif isinstance(node, (int, float, str, bool)):
                value, unit = node, ""
            else:
                continue
            if value is not None and type(value) not in (int, float, str, bool):
                raise ValueError(f"'{path}' в снимке должен быть скалярным значением.")
            entries.append(MethodologyEntry(
                path=path, value=value, unit=unit, provenance=self.provenance(path),
            ))
        u_avg = self.data.get("short_circuit", {}).get("u_avg", {})
        references = self.data.get("references", {})
        if not isinstance(references, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in references.items()
        ):
            raise ValueError("references методики должен содержать строки источников.")
        return MethodologySnapshot(
            profile_id=str(self.data.get("id", "")),
            profile_name=str(self.name),
            status=self.status,
            entries=tuple(entries),
            u_avg=tuple(sorted(
                ((str(key), float(value)) for key, value in u_avg.items()),
                key=lambda pair: float(pair[0]),
            )),
            ct_scale=tuple(self.ct_scale()),
            ct_round_mode=str(self.data.get("ct", {}).get("round_mode", "")),
            approval=tuple(sorted(
                (str(key), str(value)) for key, value in self.approval.items()
            )),
            references=tuple(sorted(references.items())),
            profile_json=profile_json,
        )

    # ---------- проверка комплектности ----------
    REQUIRED = [
        "mtz.k_ots", "mtz.k_v", "mtz.k_szp", "mtz.t_step_default",
        "to.k_ots", "to.k_ots_inrush", "to.t",
        "ozz.k_ots", "ozz.k_br", "ozz.t",
        "sensitivity.kch_mtz_main", "sensitivity.kch_mtz_backup",
        "sensitivity.kch_to", "sensitivity.kch_ozz",
        "sensitivity.backup_through_transformer",
        "selectivity.dt",
        "short_circuit.k_two_phase", "short_circuit.temp_factor_min",
    ]

    #  Параметры-выборы: значение не число, а один из вариантов.
    REQUIRED_CHOICES = ["to.i_nom_basis"]

    #  Что попадает в снимок расчёта. Шире, чем REQUIRED: сюда входят и
    #  параметры реализации, влияющие на результат.
    SNAPSHOT_PATHS = REQUIRED + REQUIRED_CHOICES + [
        "short_circuit.account_active_resistance",
        "short_circuit.zero_impedance_ohm",
    ]

    #  Пути, у которых происхождение обязано быть указано при заявленном
    #  статусе ТИПОВОЙ или ПРОЕКТНЫЙ. Совпадает с составом снимка минус чисто
    #  численные параметры реализации — у них происхождение тоже указано, но
    #  проверять его как методическое было бы подменой понятий.
    DOCUMENTED = REQUIRED + REQUIRED_CHOICES + ["short_circuit", "ct"]

    def blocking_errors(self) -> list[str]:
        problems: list[str] = []
        for p in self.REQUIRED:
            try:
                self.k(p)
            except MethodologyError as e:
                problems.append(str(e))
        for p in self.REQUIRED_CHOICES:
            try:
                self.text(p)
            except MethodologyError as e:
                problems.append(str(e))
        try:
            scale = self.ct_scale()
            if not scale or any(value <= 0 for value in scale):
                problems.append("ct.secondary_scale должна содержать положительные значения.")
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"Некорректная шкала ct.secondary_scale: {exc}")
        round_mode = self.data.get("ct", {}).get("round_mode")
        if round_mode not in ("up", "nearest"):
            problems.append("ct.round_mode должен быть 'up' или 'nearest'.")
        u_avg = self.data.get("short_circuit", {}).get("u_avg")
        if not isinstance(u_avg, dict) or not u_avg:
            problems.append("Не задана таблица short_circuit.u_avg.")
        else:
            try:
                if any(float(key) <= 0 or float(value) <= 0 for key, value in u_avg.items()):
                    problems.append("Все значения short_circuit.u_avg должны быть положительными.")
            except (TypeError, ValueError):
                problems.append("Таблица short_circuit.u_avg содержит нечисловые значения.")
        problems += self.status_claim_errors()
        return list(dict.fromkeys(problems))

    def status_claim_errors(self) -> list[str]:
        """Расхождения между заявленным статусом и содержимым профиля.

        Возвращает пустой список для ``ЗАГЛУШКА``: этот статус ничего не
        утверждает и подтверждать ему нечего.
        """
        if self.status not in STATUSES:
            return [
                f"Неизвестный статус профиля '{self.status}'. Допустимы: "
                + ", ".join(STATUSES) + "."
            ]
        if self.status == PLACEHOLDER:
            return []

        problems: list[str] = []
        lower = TYPICAL if self.status == PROJECT else PLACEHOLDER
        for path in self.DOCUMENTED:
            prov = self.provenance(path)
            if prov.origin not in ORIGINS:
                problems.append(
                    f"'{path}': происхождение '{prov.origin or 'не указано'}' вне "
                    f"допустимого списка ({', '.join(ORIGINS)}). Профиль заявляет "
                    f"статус {self.status}; либо укажите происхождение, либо "
                    f"понизьте статус до {lower}."
                )
            if not prov.source.strip():
                problems.append(
                    f"'{path}': не указан источник, а профиль заявляет статус "
                    f"{self.status}. Либо заполните 'source', либо понизьте "
                    f"статус до {lower}."
                )
            if prov.source_status not in SOURCE_STATUSES:
                problems.append(
                    f"'{path}': 'source_status' должен быть одним из "
                    + " / ".join(SOURCE_STATUSES) + "."
                )
            if self.status == PROJECT and not prov.confirmed:
                problems.append(
                    f"'{path}': источник помечен как «{prov.source_status}», а "
                    f"профиль заявляет статус {PROJECT}. Проектный профиль "
                    "означает, что ссылки сверены с текстом документов. Сверьте "
                    f"и поставьте '{CONFIRMED}' либо понизьте статус до {TYPICAL}."
                )
        if self.status == PROJECT:
            approval = self.approval
            for field in APPROVAL_FIELDS:
                value = approval.get(field)
                if not isinstance(value, str) or not value.strip():
                    problems.append(
                        f"Статус {PROJECT} требует заполненного строкового поля "
                        f"approval.{field}: утверждённая методика обязана "
                        "называть, кто и когда её утвердил и для какого объекта."
                    )
        return problems

    def validate(self) -> list[str]:
        problems = self.blocking_errors()
        if self.is_placeholder:
            problems.append(
                "Профиль помечен как ЗАГЛУШКА: коэффициенты не согласованы и не могут "
                "применяться на реальном объекте."
            )
        return problems

    def unconfirmed_sources(self) -> list[str]:
        """Коэффициенты, чей источник назван, но не сверен с документом."""
        return [
            path for path in self.DOCUMENTED
            if not self.provenance(path).confirmed
        ]


def _fmt_key(u: float) -> str:
    return str(int(u)) if float(u).is_integer() else str(u)
