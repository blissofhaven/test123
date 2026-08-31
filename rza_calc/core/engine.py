# -*- coding: utf-8 -*-
"""Сквозной прогон: сеть → режимы → токи КЗ → уставки → чувствительность → селективность."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from . import selectivity as sel
from .context import Context
from .fingerprint import methodology_fingerprint, network_fingerprint
from .methodology import (PLACEHOLDER, PROJECT, TYPICAL, Methodology,
                          MethodologySnapshot)
from .model import (Branch, LineBranch, Network, SourceBranch,
                    TransformerBranch)
from .protections.mtz import calc_mtz
from .protections.ozz import calc_ozz
from .protections.to import calc_to
from .result import FAIL, OK, UNRESOLVED, ProtectionResult
from .trace import Step, fmt
from ..version import ALGORITHM_VERSION, APPLICATION_VERSION, KERNEL_VERSION


class CalculationCaseError(ValueError):
    """Сохранённый паспорт повреждён или несовместим с данным расчётом."""


def _case_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CalculationCaseError(f"Повторяющееся поле паспорта: {key}.")
        result[key] = value
    return result


def _case_json_constant(value: str) -> None:
    raise CalculationCaseError(f"Недопустимое числовое значение паспорта: {value}.")


_ORDERED_COLLECTIONS = ("nodes", "branches", "transformers3w", "loads", "modes")


def _declaration_order(net: Network) -> tuple[tuple[str, tuple[str, ...]], ...]:
    # Общий fingerprint намеренно не зависит от порядка словарей. Здесь же
    # порядок существенен: равные режимы разрешаются по порядку объявления.
    return tuple((name, tuple(getattr(net, name))) for name in _ORDERED_COLLECTIONS)


def _parse_declaration_order(value: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict) or set(value) != set(_ORDERED_COLLECTIONS):
        raise CalculationCaseError("Некорректный порядок объявления в паспорте.")
    for identifiers in value.values():
        if (not isinstance(identifiers, list)
                or any(not isinstance(item, str) or not item for item in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise CalculationCaseError("Некорректный порядок объявления: ожидаются уникальные ID.")
    return tuple((name, tuple(value[name])) for name in _ORDERED_COLLECTIONS)



@dataclass(frozen=True)
class CalculationCase:
    """Неизменяемый паспорт входа конкретного расчёта.

    Отпечаток методики говорит, ИЗМЕНИЛСЯ ли профиль, но не говорит, чем он
    был. Профиль лежит во внешнем файле: через год после расчёта файл может
    быть отредактирован, и протокол начал бы ссылаться на коэффициенты, по
    которым никогда не считался. Поэтому рядом с отпечатком лежит снимок —
    сами применённые значения вместе с их происхождением.
    """

    model_fingerprint: str
    methodology_fingerprint: str
    application_version: str
    kernel_version: str
    algorithm_version: str
    created_at_utc: str
    methodology_snapshot: MethodologySnapshot | None = None
    declaration_order: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def capture(cls, net: Network, meth: Methodology) -> "CalculationCase":
        return cls(
            model_fingerprint=network_fingerprint(net),
            methodology_fingerprint=methodology_fingerprint(meth),
            application_version=APPLICATION_VERSION,
            kernel_version=KERNEL_VERSION,
            algorithm_version=ALGORITHM_VERSION,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            methodology_snapshot=meth.snapshot(),
            declaration_order=_declaration_order(net),
        )

    def as_dict(self) -> dict[str, Any]:
        """Версионированный паспорт; внешние файлы методики не требуются."""
        if self.methodology_snapshot is None:
            raise CalculationCaseError("В паспорте отсутствует снимок методики.")
        return {
            "schema": "rza-calculation-case/1",
            "model_fingerprint": self.model_fingerprint,
            "methodology_fingerprint": self.methodology_fingerprint,
            "application_version": self.application_version,
            "kernel_version": self.kernel_version,
            "algorithm_version": self.algorithm_version,
            "created_at_utc": self.created_at_utc,
            "methodology_snapshot": self.methodology_snapshot.as_dict(),
            "declaration_order": {name: list(ids) for name, ids in self.declaration_order},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CalculationCase":
        expected = {
            "schema", "model_fingerprint", "methodology_fingerprint",
            "application_version", "kernel_version", "algorithm_version",
            "created_at_utc", "methodology_snapshot", "declaration_order",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise CalculationCaseError("Неполный или неизвестный состав полей паспорта.")
        if payload["schema"] != "rza-calculation-case/1":
            raise CalculationCaseError("Неподдерживаемая версия формата паспорта.")
        for key in expected - {"methodology_snapshot", "declaration_order"}:
            if not isinstance(payload[key], str) or not payload[key].strip():
                raise CalculationCaseError(f"Поле паспорта '{key}' должно быть непустой строкой.")
        for key in ("model_fingerprint", "methodology_fingerprint"):
            value = payload[key]
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise CalculationCaseError(f"Поле '{key}' не является SHA-256.")
        declaration_order = _parse_declaration_order(payload["declaration_order"])
        try:
            created = datetime.fromisoformat(payload["created_at_utc"])
            if created.utcoffset() is None or created.utcoffset().total_seconds() != 0:
                raise ValueError("ожидалось время UTC")
            snapshot = MethodologySnapshot.from_dict(payload["methodology_snapshot"])
            restored = snapshot.to_methodology()
        except (KeyError, TypeError, ValueError) as exc:
            raise CalculationCaseError(f"Некорректный снимок паспорта: {exc}") from exc
        if methodology_fingerprint(restored) != payload["methodology_fingerprint"]:
            raise CalculationCaseError("Отпечаток методики не совпадает с содержимым паспорта.")
        return cls(
            **{key: payload[key] for key in expected - {"schema", "methodology_snapshot", "declaration_order"}},
            methodology_snapshot=snapshot,
            declaration_order=declaration_order,
        )

    def save(self, path: str | Path) -> None:
        """Записать новый файл; существующий файл никогда не затирается."""
        payload = self.as_dict()
        self.from_dict(payload)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with Path(path).open("x", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def load(cls, path: str | Path) -> "CalculationCase":
        try:
            with Path(path).open(encoding="utf-8") as stream:
                payload = json.load(
                    stream, object_pairs_hook=_case_json_object,
                    parse_constant=_case_json_constant,
                )
        except CalculationCaseError:
            raise
        except (ValueError, RecursionError) as exc:
            raise CalculationCaseError(f"Некорректный файл паспорта: {exc}") from exc
        return cls.from_dict(payload)

    def to_methodology(self) -> Methodology:
        """Восстановить собственный профиль, не читая текущее содержимое диска."""
        validated = self.from_dict(self.as_dict())
        return validated.methodology_snapshot.to_methodology()

    def replay(self, net: Network) -> "ProjectResult":
        """Повторить расчёт на той же сети и совместимом ядре.

        Паспорт хранит полный профиль, но не подменяет файл электрической
        модели. Другая модель или другая версия алгоритма требуют нового
        расчёта, а не молчаливого признания старого результата воспроизведённым.
        Возвращается новый результат с фактическим временем повторного прогона.
        """
        meth = self.to_methodology()
        if self.kernel_version != KERNEL_VERSION or self.algorithm_version != ALGORITHM_VERSION:
            raise CalculationCaseError(
                "Версия ядра или алгоритма отличается от записанной в паспорте; "
                "точное воспроизведение не подтверждено."
            )
        if network_fingerprint(net) != self.model_fingerprint:
            raise CalculationCaseError(
                "Электрическая модель отличается от паспорта; повторный расчёт отклонён."
            )
        if _declaration_order(net) != self.declaration_order:
            raise CalculationCaseError(
                "Порядок объявления объектов или режимов отличается от паспорта; "
                "повторный расчёт отклонён."
            )
        return run(net, meth)


CURRENT = "current"
STALE = "stale"


class CalculationInputError(ValueError):
    """Расчёт заблокирован из-за недостоверных или неполных исходных данных."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Расчёт заблокирован:\n- " + "\n- ".join(errors))


@dataclass
class ProjectResult:
    ctx: Context
    results: dict[str, dict[str, ProtectionResult]] = field(default_factory=dict)
    pairs: list[sel.Pair] = field(default_factory=list)
    time_steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calculation_case: CalculationCase | None = None
    preliminary: bool = True

    def freshness_for(self, net: Network, meth: Methodology) -> str:
        if self.calculation_case is None:
            return STALE
        try:
            current_model = network_fingerprint(net)
            current_methodology = methodology_fingerprint(meth)
        except (TypeError, ValueError):
            return STALE
        return (
            CURRENT
            if current_model == self.calculation_case.model_fingerprint
            and current_methodology == self.calculation_case.methodology_fingerprint
            and _declaration_order(net) == self.calculation_case.declaration_order
            else STALE
        )

    def is_current_for(self, net: Network, meth: Methodology) -> bool:
        return self.freshness_for(net, meth) == CURRENT

    def get(self, branch_id: str, kind: str) -> ProtectionResult | None:
        return self.results.get(branch_id, {}).get(kind)

    def all_results(self) -> list[ProtectionResult]:
        return [r for by in self.results.values() for r in by.values()]

    @property
    def has_failures(self) -> bool:
        return (any(r.status == FAIL for r in self.all_results())
                or bool(sel.violations(self.pairs)))


def run(net: Network, meth: Methodology) -> ProjectResult:
    blocking = ([f"Модель: {p}" for p in net.validate()]
                + [f"Методика: {p}" for p in meth.blocking_errors()])
    if blocking:
        raise CalculationInputError(blocking)

    try:
        case = CalculationCase.capture(net, meth)
        # Контекст и его ленивые расчёты используют собственный профиль:
        # последующая правка исходного объекта не должна менять старый отчёт.
        meth = case.methodology_snapshot.to_methodology()
    except (TypeError, ValueError) as exc:
        raise CalculationInputError([f"Невозможно сформировать паспорт расчёта: {exc}"]) from exc

    warnings: list[str] = [
        "Расчёт имеет статус ПРЕДВАРИТЕЛЬНЫЙ. Текущее ядро подтверждено только "
        "для ограниченных трёхфазных задач положительной последовательности; "
        "промышленная выдача без независимой проверки запрещена.",
        "Iк(2) является оценкой √3/2·Iк(3) только при допущении Z2 = Z1; "
        "сеть обратной последовательности не рассчитывается.",
    ]
    if meth.status == PLACEHOLDER:
        warnings.append(
            "Методика: ЗАГЛУШКА — коэффициенты не согласованы и не могут "
            "применяться на реальном объекте."
        )
    elif meth.status == TYPICAL:
        unconfirmed = meth.unconfirmed_sources()
        warnings.append(
            f"Методика: ТИПОВОЙ профиль «{meth.name}». У коэффициентов назван "
            "источник, но профиль не утверждён для конкретного объекта"
            + (
                f"; точные пункты документов не сверены ({len(unconfirmed)} из "
                f"{len(meth.DOCUMENTED)} позиций)"
                if unconfirmed else ""
            )
            + ". Перед выдачей уставок в эксплуатацию профиль обязан быть "
            "проверен ответственным инженером и переведён в статус ПРОЕКТНЫЙ."
        )
    elif meth.status == PROJECT:
        approval = meth.approval
        warnings.append(
            f"Методика: ПРОЕКТНЫЙ профиль «{meth.name}», утверждён "
            f"{approval.get('approved_by', '?')} {approval.get('approved_on', '?')} "
            f"для объекта «{approval.get('object', '?')}»."
        )
    if meth.warning:
        warnings.append(f"Методика: {meth.warning}")

    motor_loads = [
        load.name for load in net.loads.values()
        if load.motor_share is not None and load.motor_share > 0
    ]
    if motor_loads:
        warnings.append(
            "Подпитка КЗ от двигателей пока НЕ учитывается. Результат может быть "
            "занижен. Нагрузки: " + ", ".join(motor_loads) + "."
        )

    sources_without_xr = [
        branch.name for branch in net.branches.values()
        if isinstance(branch, SourceBranch) and branch.x_r_ratio is None
    ]
    if sources_without_xr:
        warnings.append(
            "Для внешних источников без X/R сопротивление принято чисто "
            "индуктивным: " + ", ".join(sources_without_xr) + "."
        )

    # Приведение между ступенями выполняется по классам напряжения узлов, а не
    # по паспортному коэффициенту трансформации: `u_hv`/`u_lv` в расчёт Z не
    # входят. Пока это так, расхождение паспортных данных с классами узлов
    # обязано быть видно пользователю — молча игнорировать введённое значение
    # нельзя.
    ratio_mismatch: list[str] = []
    for branch in net.branches.values():
        if not isinstance(branch, TransformerBranch) or branch.internal_star_leg:
            continue
        for side, nominal, node_id in (
            ("ВН", branch.u_hv, branch.node_from),
            ("НН", branch.u_lv, branch.node_to),
        ):
            try:
                node_u = net.node(node_id).u_nom
            except KeyError:
                continue
            if not nominal or not node_u:
                continue
            if abs(nominal - node_u) > 1e-9:
                ratio_mismatch.append(
                    f"«{branch.name}» ({side}: паспорт {fmt(nominal)} кВ, "
                    f"узел «{net.node(node_id).name}» {fmt(node_u)} кВ)"
                )
    if ratio_mismatch:
        warnings.append(
            "Паспортные напряжения трансформаторов не совпадают с классами "
            "напряжения узлов, к которым они подключены. Приведение сопротивлений "
            "выполняется ПО КЛАССАМ УЗЛОВ, паспортный коэффициент трансформации "
            "в расчёте не участвует, поэтому введённые значения на результат не "
            "влияют: " + "; ".join(ratio_mismatch) + "."
        )

    fallback_lines = [
        branch.name for branch in net.branches.values()
        if isinstance(branch, LineBranch)
        and (branch.r0 is None or branch.x0 is None)
    ]
    if fallback_lines:
        warnings.append(
            "Часть параметров линий получена из ориентировочного встроенного "
            "справочника и требует подтверждения: " + ", ".join(fallback_lines) + "."
        )

    with net.frozen_topology():
        return _run_frozen(net, meth, case, warnings)


def _run_frozen(net: Network, meth: Methodology, case: CalculationCase,
                warnings: list[str]) -> ProjectResult:
    """Тело расчёта. Сеть внутри не изменяется — это использует кэш топологии."""
    ctx = Context(net, meth)
    for mid, err in ctx.errors.items():
        warnings.append(f"Режим «{net.modes[mid].name}» не рассчитан: {err}")
    for mode_id, solver in ctx.solvers.items():
        for warning in solver.numerical_warnings:
            warnings.append(f"Режим «{net.modes[mode_id].name}»: {warning}")

    pr = ProjectResult(ctx, warnings=list(dict.fromkeys(warnings)), calculation_case=case)

    for br in net.protection_points():
        by_kind: dict[str, ProtectionResult] = {}
        if br.prot.mtz:
            by_kind["МТЗ"] = calc_mtz(ctx, br)
        if br.prot.to:
            by_kind["ТО"] = calc_to(ctx, br)
        if br.prot.ozz:
            by_kind["ОЗЗ"] = calc_ozz(ctx, br)
        pr.results[br.id] = by_kind

    pr.time_steps = sel.assign_times(ctx, pr.results)
    pr.pairs = sel.check(ctx, pr.results)
    return pr


def summary_table(pr: ProjectResult) -> list[list[str]]:
    """Сводная таблица всей подстанции."""
    net = pr.ctx.net
    rows = [["Объект", "Защита", "Iсз перв., А", "Iсз втор., А", "t, с", "Kч", "Статус"]]
    order = {b.id: i for i, b in enumerate(net.branches.values())}
    for bid in sorted(pr.results, key=lambda b: order.get(b, 999)):
        for kind in ("ТО", "МТЗ", "ОЗЗ"):
            r = pr.results[bid].get(kind)
            if r is None:
                continue
            kch = [c.value for c in r.checks if c.value is not None]
            mark = {OK: "✓", FAIL: "✗", UNRESOLVED: "?"}[r.status]
            rows.append([
                r.branch_name, kind,
                fmt(r.i_primary) if r.i_primary is not None else "—",
                fmt(r.i_secondary) if r.i_secondary is not None else "—",
                fmt(r.t) if r.t is not None else "—",
                fmt(min(kch)) if kch else "—",
                mark,
            ])
    return rows


def render_table(rows: list[list[str]]) -> str:
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    right = {2, 3, 4, 5}
    out = []
    for n, r in enumerate(rows):
        cells = [(c.rjust(w[i]) if i in right and n else c.ljust(w[i])) for i, c in enumerate(r)]
        out.append("  " + " │ ".join(cells))
        if n == 0:
            out.append("  " + "─┼─".join("─" * x for x in w))
    return "\n".join(out)
