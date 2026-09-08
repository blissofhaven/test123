# -*- coding: utf-8 -*-
"""Подготовка данных проекта для GUI без зависимости от PySide6.

Этот слой намеренно не содержит виджетов. Поэтому интерфейс можно менять,
не затрагивая расчётное ядро, а преобразование данных проверяется обычными
автоматическими тестами даже на машине без графической библиотеки.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from ..core.engine import CalculationInputError, ProjectResult, run, run_input
from ..core.fault_types import FaultSpec, FaultType
from ..calculation.fault_measurement import (FaultMeasurementRow, MEASUREMENT_ASSUMPTIONS,
                                           branch_fault_measurements)
from ..core.model import GeneratorBranch, Mode
from ..core.result import FAIL, OK, UNRESOLVED
from ..io.project import ProjectData, load_project
from .mode_state import ModeStateMixin


FAULT_TYPE_LABELS = {
    FaultType.THREE_PHASE: "Трёхфазное КЗ",
    FaultType.LINE_LINE: "Двухфазное КЗ (B–C)",
    FaultType.LINE_GROUND: "Однофазное КЗ на землю (A)",
    FaultType.LINE_LINE_GROUND: "Двухфазное КЗ на землю (B–C)",
}

FAULT_STATUS_LABELS = {
    "UNCONFIRMED_INPUT": "Данные не подтверждены",
    "MISSING_SEQUENCE_DATA": "Нет исходных данных",
    "NOT_ENERGIZED": "Нет питания",
    "NO_ZERO_SEQUENCE_RETURN_PATH": "Нет контура нулевой последовательности",
    "NO_SEQUENCE_RETURN_PATH": "Нет контура последовательности",
    "NUMERIC_FAILURE": "Численная ошибка",
    "INVALID_INPUT": "Ошибка данных",
    "MODE_UNAVAILABLE": "Режим не рассчитан",
    "CALCULATION_ERROR": "Расчёт не выполнен",
    "STALE_RESULT": "Результаты устарели",
}


def fault_status_label(code: str) -> str:
    return FAULT_STATUS_LABELS.get(code, "Расчёт не выполнен")


KIND_RU = {
    "generator": "Генератор",
    "source": "Питающая система",
    "transformer": "Трансформатор",
    "line": "Линия",
    "tie": "Коммутационный аппарат",
    "bus": "Шины",
    "point": "Расчётная точка",
    "load": "Нагрузка",
    "power_plant": "Электростанция",
    "substation": "Подстанция",
    "switching_station": "Распределительный пункт",
    "ktp": "КТП",
}

FIELD_RU = {
    "id": "Идентификатор",
    "name": "Наименование",
    "kind": "Тип",
    "u_nom": "Номинальное напряжение",
    "node_from": "Начальный узел",
    "node_to": "Конечный узел",
    "section": "Секция",
    "switchable": "Коммутируемый",
    "normally_closed": "Нормальное состояние",
    "ct_ratio": "Коэффициент ТТ",
    "ct_node": "Место установки ТТ",
    "terminal": "Терминал РЗА",
    "breaker_t_off": "Время отключения выключателя",
    "p_nom": "Активная мощность",
    "s_nom": "Полная мощность",
    "cos_phi": "cos φ",
    "xd2": "x″d",
    "u_hv": "Напряжение ВН",
    "u_mv": "Напряжение СН",
    "u_lv": "Напряжение НН",
    "uk": "Напряжение КЗ",
    "length_km": "Длина",
    "brand": "Марка",
    "section_mm2": "Сечение",
    "p_kw": "Активная мощность",
    "note": "Примечание",
}


@dataclass(frozen=True)
class TreeEntry:
    """Один пункт дерева навигации."""

    key: str
    title: str
    kind: str
    children: tuple["TreeEntry", ...] = ()
    subtitle: str = ""


@dataclass(frozen=True)
class GeneratorStatus:
    branch_id: str
    name: str
    power_mw: float
    enabled: bool


@dataclass(frozen=True)
class PropertyRow:
    label: str
    value: str


@dataclass(frozen=True)
class SettingRow:
    kind: str
    current: str
    time: str
    mode: str
    status: str


@dataclass(frozen=True)
class FaultRow:
    mode_id: str
    mode_name: str
    i3_ka: float | None
    i2_ka: float | None
    error: str = ""
    fault_type: FaultType = FaultType.THREE_PHASE
    iabc_ka: tuple[complex, ...] | None = None
    vabc_kv: tuple[complex, ...] | None = None
    residual_current_ka: complex | None = None
    status_code: str = ""
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class FaultBranchView:
    title: str
    choices: tuple[tuple[str, str], ...] = ()
    branch_id: str = ''
    rows: tuple[FaultMeasurementRow, ...] = ()
    status: str = ''
    assumptions: tuple[str, ...] = MEASUREMENT_ASSUMPTIONS


@dataclass(frozen=True)
class _PresentationSnapshot:
    project: ProjectData
    network: Any
    blockers: tuple[Any, ...]
    raw_result: ProjectResult | None
    current_result: ProjectResult | None
    electrical_model: Any
    electrical_revision: int
    methodology: Any


def _fmt_number(value: float, digits: int = 2) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _human_value(name: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if name == "kind":
        return KIND_RU.get(str(value), str(value))
    if name == "normally_closed":
        return "Включён" if value else "Отключён"
    if name == "ct_ratio" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{_fmt_number(float(value[0]))} / {_fmt_number(float(value[1]))} А"
    if isinstance(value, float):
        suffix = {
            "u_nom": " кВ", "u_hv": " кВ", "u_mv": " кВ", "u_lv": " кВ",
            "length_km": " км", "section_mm2": " мм²", "p_kw": " кВт",
            "p_nom": " МВт", "s_nom": " кВА", "breaker_t_off": " с",
            "uk": " %",
        }.get(name, "")
        return _fmt_number(value) + suffix
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {val}" for key, val in value.items()) or "—"
    return str(value)


class ProjectViewModel(ModeStateMixin):
    """Состояние открытого проекта и представление данных для окна."""

    def __init__(self, project: ProjectData, path: str | Path):
        self.project = project
        self.path = Path(path)
        self.result: ProjectResult | None = None
        self.calculation_error = ""
        self.selected_kind = "node"
        self.selected_fault_type = FaultType.THREE_PHASE
        self.selected_id = next(iter(self.net.nodes), "")
        self._enable_switching()
        self.mode_id = self._preferred_mode()
        self._custom_base = self.mode_id
        self.recalculate()

    @contextmanager
    def presentation_snapshot(self):
        """One fresh input/result read for a synchronous, read-only GUI render.

        The scope must not dispatch events or run modal dialogs. It never
        survives a render, including exceptions. Mutating VM actions discard
        it immediately; the next render always performs the full input guards.
        """
        if not isinstance(self.project, ProjectData):
            # Embedded/legacy VM clients may provide only a calculation view.
            # Without canonical input identity, keep their ordinary per-read
            # guards instead of creating an unverifiable shared snapshot.
            yield
            return
        depth = self.__dict__.get("_presentation_depth", 0)
        if depth == 0:
            network = self.net
            blockers = self.project.calculation_blockers
            raw_result = self.result
            canonical = getattr(raw_result, 'calculation_input', None)
            current = (raw_result if raw_result is not None and not blockers
                       and self.mode_draft is None and not getattr(self, 'calculation_busy', False)
                       and (self._canonical_result_current(raw_result) if canonical is not None
                            else raw_result.is_current_for(network, self.project.methodology)) else None)
            self._presentation = _PresentationSnapshot(
                self.project, network, blockers, raw_result, current,
                self.project.electrical_model, self.project.electrical_model.revision,
                self.project.methodology,
            )
        self._presentation_depth = depth + 1
        try:
            yield
        finally:
            self._presentation_depth -= 1
            if self._presentation_depth == 0:
                self._discard_presentation_snapshot()

    def _discard_presentation_snapshot(self) -> None:
        self.__dict__.pop("_presentation", None)

    def _presentation_values(self):
        snapshot = self.__dict__.get("_presentation")
        if snapshot is not None and (snapshot.project is not self.project
                or snapshot.raw_result is not self.result
                or snapshot.electrical_model is not self.project.electrical_model
                or snapshot.electrical_revision != self.project.electrical_model.revision
                or snapshot.methodology is not self.project.methodology):
            self._discard_presentation_snapshot()
            return None
        return snapshot

    @property
    def net(self):
        """Всегда возвращать актуальный расчётный view проекта.

        UI не владеет сохранённой mutable-ссылкой на ``core.Network``: после
        редактирования canonical ElectricalModel проект сам перестраивает
        совместимое расчётное представление.

        Сохранённые режимы принадлежат canonical модели. Незавершённые
        переключения находятся в отдельном ModeDraft и не дописываются в view.
        """
        presentation = self._presentation_values()
        if presentation is not None:
            return presentation.network
        network = self.project.network
        if not isinstance(self.project, ProjectData) and network is not self.__dict__.get("_applied_network"):
            self.__dict__["_applied_network"] = network
            self._enable_switching()
            custom = self.__dict__.get("_custom_mode")
            if custom is not None and custom.id not in network.modes:
                try:
                    network.add_mode(custom)
                except ValueError:
                    pass
        return network

    @classmethod
    def open(cls, path: str | Path) -> "ProjectViewModel":
        return cls(load_project(path), path)

    @property
    def current_result(self) -> ProjectResult | None:
        """Допуск к числам только для текущих электрических исходных данных.

        Исторический снимок сохраняется в ``result`` для undo, но потребители
        GUI и экспорта должны брать его через эту проверку. Геометрия схемы
        не входит в расчётный fingerprint.
        """
        if self.mode_draft is not None or getattr(self, 'calculation_busy', False):
            return None
        presentation = self._presentation_values()
        if presentation is not None:
            return presentation.current_result
        if self.result is not None and getattr(self.result, 'calculation_input', None) is not None:
            return self.result if self._canonical_result_current(self.result) else None
        result = self.result
        if result is None or self.project.calculation_blockers:
            return None
        return result if result.is_current_for(self.net, self.project.methodology) else None

    def _canonical_result_current(self, result):
        from ..version import ALGORITHM_VERSION, KERNEL_VERSION
        case = result.calculation_case
        return (case is not None and case.kernel_version == KERNEL_VERSION
                and case.algorithm_version == ALGORITHM_VERSION
                and result.calculation_input.is_current_for(self.project))

    def result_unavailable_reason(self) -> str:
        if self.mode_draft is not None:
            return 'Черновик режима: прежние расчётные числа скрыты. Примените или сбросьте изменения.'
        if getattr(self, 'calculation_busy', False):
            return 'Расчёт выполняется. Доступен просмотр схемы; изменения временно недоступны.'
        if self.current_result is not None:
            return ""
        blockers = self._calculation_blocker_error()
        if blockers:
            return blockers
        if self.result is not None:
            return "Результаты устарели: исходные данные изменены. Нажмите «Пересчитать»."
        return self.calculation_error or "Расчёт не выполнен. Нажмите «Пересчитать»."

    def require_current_result(self) -> ProjectResult:
        """Общая граница для выдачи расчёта, отчёта или паспорта случая."""
        result = self.current_result
        if result is None:
            raise ValueError(self.result_unavailable_reason())
        return result

    def _calculation_blocker_error(self) -> str:
        presentation = self._presentation_values()
        blockers = presentation.blockers if presentation is not None else self.project.calculation_blockers
        if not blockers:
            return ""
        details = "\n".join(
            "• " + self.project.calculation_blocker_message(item)
            for item in blockers[:8]
        )
        return (
            "Расчёт заблокирован: электрическая схема содержит "
            "незавершённые или недопустимые подключения.\n" + details
        )

    def _preferred_mode(self) -> str:
        if isinstance(self.project, ProjectData):
            saved = self.mode_controller.active_operating_state_id
            choice = next((row for row in self.operating_mode_choices() if row.state_id == saved), None)
            if choice is not None:
                return choice.calculation_mode_id
        if "max" in self.net.modes:
            return "max"
        return next(iter(self.net.modes), "")

    @property
    def mode(self) -> Mode | None:
        return self.net.modes.get(self.mode_id)

    def select_mode(self, mode_id: str) -> None:
        self._discard_presentation_snapshot()
        if isinstance(self.project, ProjectData):
            selected = next((row for row in self.operating_mode_choices()
                             if row.calculation_mode_id == mode_id or str(row.state_id) == str(mode_id)), None)
            if selected is not None:
                self.choose_operating_mode(selected.state_id)
                return
        if mode_id not in self.net.modes:
            raise KeyError(f"Режим '{mode_id}' не найден.")
        self.mode_id = mode_id
        if mode_id != "gui_custom":
            self._custom_base = mode_id

    def ensure_custom_mode(self):
        """Начать копию сохранённого режима; применение выполняется явно."""
        if isinstance(self.project, ProjectData):
            return self.begin_mode_draft(clone=True)
        self._discard_presentation_snapshot()
        existing = self.net.modes.get("gui_custom")
        if existing is not None:
            self.__dict__["_custom_mode"] = existing
            self.mode_id = existing.id
            return existing
        base = self.net.modes.get(self._custom_base) or self.mode
        states = deepcopy(base.states) if base else {}
        custom = Mode(
            id="gui_custom",
            name="Пользовательский",
            states=states,
            system=base.system if base else "max",
            description="Пользовательский состав оборудования (пока только в памяти).",
        )
        self.net.add_mode(custom)
        # Запомнить объект режима: расчётный view пересобирается при каждой
        # правке канонической модели, и режим нужно вернуть в новый Network.
        self.__dict__["_custom_mode"] = custom
        self.mode_id = custom.id
        return custom

    def set_generator_enabled(self, branch_id: str, enabled: bool) -> None:
        if isinstance(self.project, ProjectData):
            from dataclasses import replace
            from ..domain.electrical import EquipmentAvailability
            draft = self.begin_mode_draft()
            equipment_id = self.mode_controller.resolve_operating_mode_equipment(branch_id)
            values = dict(draft.availability)
            values[equipment_id] = EquipmentAvailability.IN_SERVICE if enabled else EquipmentAvailability.OUT_OF_SERVICE
            self.update_mode_draft(replace(draft, availability=values))
            return
        self._discard_presentation_snapshot()
        branch = self.net.branches.get(branch_id)
        if not isinstance(branch, GeneratorBranch):
            raise KeyError(f"Генератор '{branch_id}' не найден.")
        custom = self.ensure_custom_mode()
        custom.states[branch_id] = bool(enabled)

    # ---------- карта электрических состояний ----------
    def electrical_map(self):
        """Единственный источник истины о напряжении и токах для схемы."""
        from ..core.electrical import build_electrical_map
        mode = self.mode
        if mode is None:
            return None
        solver = None
        result = self.current_result
        if result is not None:
            solver = result.ctx.solvers.get(mode.id)
        currents = {}
        if result is not None:
            selected = result.ctx.net.modes.get(mode.id)
            records = getattr(selected, 'operating_parameters', {}).get('working_currents', {})
            ambiguous = set()
            for record in records.values():
                value = record.get('value')
                if record.get('confirmation') != 'confirmed' or not record.get('source') or value is None:
                    continue
                for branch_id in record.get('applies_to', ()):
                    if branch_id in currents and currents[branch_id] != float(value):
                        ambiguous.add(branch_id)
                    currents[branch_id] = float(value)
            for branch_id in ambiguous:
                currents.pop(branch_id, None)
        return build_electrical_map(self.net, mode, solver, currents)

    def switch_closed(self, switch_id: str) -> bool:
        from ..core.electrical import collect_switches, switch_closed
        switches = collect_switches(self.net)
        switch = switches.get(switch_id)
        if switch is None or self.mode is None:
            return False
        return switch_closed(switch, self.net.branches[switch.branch_id], self.mode)

    def toggle_switch(self, switch_id: str) -> bool:
        """Изменить положение в общем черновике; применение и расчёт отдельно."""
        if isinstance(self.project, ProjectData):
            return self.stage_calculation_switch(switch_id)
        self._discard_presentation_snapshot()
        from ..core.model import parse_switch_id
        parsed = parse_switch_id(switch_id)
        if parsed is None or parsed[0] not in self.net.branches:
            raise KeyError(f"Выключатель '{switch_id}' не найден.")
        branch = self.net.branches[parsed[0]]
        if not getattr(branch, "switchable", False):
            raise ValueError(
                f"«{branch.name}»: состояние выключателя не хранится в модели, "
                "переключить его нельзя."
            )
        custom = self.ensure_custom_mode()
        had_key = switch_id in custom.states
        previous = custom.states.get(switch_id)
        new_state = not self.switch_closed(switch_id)
        custom.states[switch_id] = new_state
        if not self.recalculate():
            if had_key:
                custom.states[switch_id] = previous
            else:
                custom.states.pop(switch_id, None)
            error = self.calculation_error
            self.recalculate()
            raise ValueError(error or "Расчёт не выполнен, переключение отменено.")
        return new_state

    def set_branch_enabled(self, branch_id: str, enabled: bool) -> None:
        """Переключить любое коммутируемое присоединение из схемы."""
        self._discard_presentation_snapshot()
        branch = self.net.branches.get(branch_id)
        if branch is None:
            raise KeyError(f"Присоединение '{branch_id}' не найдено.")
        if not getattr(branch, "switchable", False):
            raise ValueError(
                f"«{branch.name}» не является коммутируемым: у присоединения "
                "не задан выключатель."
            )
        if isinstance(self.project, ProjectData):
            self.set_generator_enabled(branch_id, enabled)
            return
        custom = self.ensure_custom_mode()
        custom.states[branch_id] = bool(enabled)

    def toggle_branch(self, branch_id: str) -> bool:
        """Инвертировать состояние присоединения. Возвращает новое состояние."""
        self._discard_presentation_snapshot()
        branch = self.net.branches[branch_id]
        mode = self.mode
        closed = mode.is_closed(branch) if mode else branch.normally_closed
        self.set_branch_enabled(branch_id, not closed)
        return not closed

    def is_branch_closed(self, branch_id: str) -> bool:
        branch = self.net.branches.get(branch_id)
        if branch is None:
            return False
        mode = self.mode
        return mode.is_closed(branch) if mode else branch.normally_closed

    def _enable_switching(self) -> None:
        """Присоединение, у которого на схеме есть выключатель, коммутируемо.

        В файлах формата 1 у линий и трансформаторов признак не проставлен,
        поэтому ядро отказывалось принимать их состояние в режиме. Признак
        выставляется по наличию паспортных данных выключателя и сохраняется
        в проект при следующей записи.
        """
        if isinstance(self.project, ProjectData):
            return
        for branch in self.net.branches.values():
            if getattr(branch, "switchable", False):
                continue
            if not (getattr(branch, "breaker_t_off", None)
                    or getattr(branch, "ct_ratio", None)):
                continue
            try:
                branch.switchable = True
            except Exception:
                continue
            if getattr(branch, "normally_closed", None) is None:
                branch.normally_closed = True

    def recalculate(self) -> bool:
        if self.mode_draft is not None or getattr(self, 'calculation_busy', False):
            self.calculation_error = 'Примените или сбросьте черновик; дождитесь завершения текущего расчёта.'
            return False
        self._discard_presentation_snapshot()
        # Старый удачный ответ не остаётся текущим при ошибке новой попытки.
        # Номер попытки также защищает более новый ответ от позднего завершения.
        generation = self.__dict__.get("_calculation_generation", 0) + 1
        self._calculation_generation = generation
        self.result = None
        self.calculation_error = ""
        try:
            blockers = self._calculation_blocker_error()
            if blockers:
                self.calculation_error = blockers
                return False
            # Ленивые solver не должны читать поздние inplace-правки режима
            # или ветви через сохранённую ссылку на производный Network.
            if isinstance(self.project, ProjectData):
                from ..calculation.input import capture_project_input
                candidate = run_input(capture_project_input(self.project))
            else:
                candidate = run(deepcopy(self.net), self.project.methodology)
            if generation != self._calculation_generation:
                return False
            blockers = self._calculation_blocker_error()
            canonical = getattr(candidate, 'calculation_input', None)
            current = (canonical.is_current_for(self.project) if canonical is not None
                       else candidate.is_current_for(self.net, self.project.methodology))
            if blockers or not current:
                self.calculation_error = blockers or (
                    "Исходные данные изменились во время расчёта. "
                    "Нажмите «Пересчитать» для текущей схемы."
                )
                return False
            self.result = candidate
            return True
        except CalculationInputError as exc:
            if generation == self._calculation_generation:
                self.calculation_error = str(exc)
            return False
        except Exception as exc:  # GUI должен показать ошибку, а не закрыться
            if generation == self._calculation_generation:
                self.calculation_error = f"Ошибка расчёта: {exc}"
            return False

    # ---------- верхняя панель ----------
    def mode_choices(self) -> list[tuple[str, str]]:
        return [(mode.id, mode.name) for mode in self.net.modes.values()
                if mode.id != "gui_custom"]

    def generators(self) -> list[GeneratorStatus]:
        mode = self.mode
        rows = []
        for branch in self.net.branches.values():
            if not isinstance(branch, GeneratorBranch):
                continue
            enabled = mode.is_closed(branch) if mode else branch.normally_closed
            if self.mode_draft is not None:
                from ..domain.electrical import EquipmentAvailability
                equipment_id = self.mode_controller.resolve_operating_mode_equipment(branch.id)
                availability = self.mode_draft.availability.get(equipment_id)
                if availability is not None:
                    enabled = availability is EquipmentAvailability.IN_SERVICE
            rows.append(GeneratorStatus(
                branch.id,
                branch.name,
                float(branch.p_nom or 0.0),
                enabled,
            ))
        return rows

    def total_generation_mw(self) -> float:
        return sum(item.power_mw for item in self.generators() if item.enabled)

    # ---------- дерево ----------
    def tree(self) -> tuple[TreeEntry, ...]:
        if self.project.structure.facilities:
            return tuple(self._facility_entry(item.id)
                         for item in self.project.structure.root_facilities())
        return (self._calculation_tree(),)

    def _facility_entry(self, facility_id: str) -> TreeEntry:
        structure = self.project.structure
        obj = structure.facilities[facility_id]
        children: list[TreeEntry] = []
        children.extend(self._facility_entry(child.id)
                        for child in structure.child_facilities(facility_id))
        for level in structure.levels_of(facility_id):
            level_children: list[TreeEntry] = []
            for section in structure.sections_of(level.id):
                level_children.append(TreeEntry(
                    f"node:{section.calculation_node_id}" if section.calculation_node_id
                    else f"section:{section.id}",
                    section.name,
                    "node" if section.calculation_node_id else "section",
                ))
            for bay in structure.bays_of(level.id):
                equipment = tuple(TreeEntry(
                    self._equipment_key(item), item.name, "equipment"
                ) for item in structure.equipment_in_bay(bay.id))
                level_children.append(TreeEntry(f"bay:{bay.id}", bay.name, "bay", equipment))
            children.append(TreeEntry(
                f"level:{level.id}", level.name, "level", tuple(level_children),
                f"{_fmt_number(level.u_nom)} кВ",
            ))
        return TreeEntry(
            f"facility:{obj.id}", obj.name, "facility", tuple(children),
            KIND_RU.get(obj.kind, obj.kind),
        )

    def _equipment_key(self, equipment: Any) -> str:
        for ref in equipment.calculation_refs:
            if ref.kind in ("branch", "node", "load"):
                return f"{ref.kind}:{ref.object_id}"
        return f"equipment:{equipment.id}"

    def _calculation_tree(self) -> TreeEntry:
        """Честное резервное дерево для старого v1 без физической структуры."""
        generators = [self._branch_entry(b) for b in self.net.branches.values()
                      if b.kind == "generator"]
        transformers = [self._branch_entry(b) for b in self.net.branches.values()
                        if b.kind == "transformer"]
        lines = [self._branch_entry(b) for b in self.net.branches.values()
                 if b.kind == "line"]
        switches = [self._branch_entry(b) for b in self.net.branches.values()
                    if b.kind == "tie"]
        levels: list[TreeEntry] = []
        voltages = sorted({node.u_nom for node in self.net.nodes.values()}, reverse=True)
        for voltage in voltages:
            nodes = tuple(TreeEntry(
                f"node:{node.id}", node.name, "node",
                subtitle=f"{_fmt_number(node.u_nom)} кВ",
            ) for node in self.net.nodes.values() if node.u_nom == voltage)
            levels.append(TreeEntry(
                f"voltage:{voltage}", f"Шины и точки {_fmt_number(voltage)} кВ",
                "group", nodes,
            ))
        loads = tuple(TreeEntry(
            f"load:{item.id}", item.name, "load", subtitle=f"{_fmt_number(item.p_kw)} кВт"
        ) for item in self.net.loads.values())
        groups = [
            TreeEntry("group:generators", "Генераторные установки", "group", tuple(generators)),
            TreeEntry("group:levels", "Уровни напряжения", "group", tuple(levels)),
            TreeEntry("group:transformers", "Трансформаторы", "group", tuple(transformers)),
            TreeEntry("group:lines", "Линии и фидеры", "group", tuple(lines)),
            TreeEntry("group:switches", "Вводы и секционные связи", "group", tuple(switches)),
            TreeEntry("group:loads", "Нагрузки", "group", loads),
        ]
        return TreeEntry("project:root", self.net.name, "project", tuple(groups),
                         "Расчётная схема v1")

    @staticmethod
    def _branch_entry(branch: Any) -> TreeEntry:
        subtitle = KIND_RU.get(branch.kind, branch.kind)
        return TreeEntry(f"branch:{branch.id}", branch.name, "branch", subtitle=subtitle)

    # ---------- выбранный объект ----------
    def select(self, kind: str, object_id: str) -> None:
        if kind == "node" and object_id in self.net.nodes:
            self.selected_kind, self.selected_id = kind, object_id
        elif kind == "branch" and object_id in self.net.branches:
            self.selected_kind, self.selected_id = kind, object_id
        elif kind == "load" and object_id in self.net.loads:
            self.selected_kind, self.selected_id = kind, object_id

    def selected_object(self) -> Any | None:
        stores = {
            "node": self.net.nodes,
            "branch": self.net.branches,
            "load": self.net.loads,
        }
        return stores.get(self.selected_kind, {}).get(self.selected_id)

    def selection_title(self) -> str:
        obj = self.selected_object()
        return getattr(obj, "name", self.net.name)

    def properties(self) -> list[PropertyRow]:
        obj = self.selected_object()
        if obj is None:
            return []
        if is_dataclass(obj):
            names = [item.name for item in fields(obj)]
            values = asdict(obj)
        else:
            names = list(vars(obj))
            values = vars(obj)
        preferred = [
            "id", "name", "kind", "u_nom", "node_from", "node_to", "section",
            "p_nom", "s_nom", "cos_phi", "u_hv", "u_lv", "uk", "length_km",
            "brand", "section_mm2", "ct_ratio", "ct_node", "terminal",
            "breaker_t_off", "switchable", "normally_closed", "note",
        ]
        order = preferred + [name for name in names if name not in preferred]
        rows = []
        for name in order:
            if name not in values or name == "prot":
                continue
            value = values[name]
            if value in (None, "", [], {}):
                continue
            rows.append(PropertyRow(FIELD_RU.get(name, name), _human_value(name, value)))
        return rows

    # ---------- расчётные результаты ----------
    def select_fault_type(self, value: FaultType | str) -> None:
        """Вид повреждения меняет только запрос результата, не модель сети."""
        self.selected_fault_type = FaultType(value)

    def fault_type_label(self) -> str:
        return FAULT_TYPE_LABELS[self.selected_fault_type]

    def selected_fault_node(self) -> str | None:
        if self.selected_kind == "node":
            return self.selected_id
        if self.selected_kind == "load":
            load = self.net.loads.get(self.selected_id)
            return load.node if load else None
        if self.selected_kind == "branch":
            branch = self.net.branches.get(self.selected_id)
            if branch is None:
                return None
            mode = self.mode
            if mode is not None:
                try:
                    _, load_end, _ = self.net.orient(branch, mode)
                    return load_end if load_end != "GRID" else None
                except Exception:
                    pass
            return branch.node_to if branch.node_to != "GRID" else branch.node_from
        return None

    def fault_rows(self) -> list[FaultRow]:
        node_id = self.selected_fault_node()
        if not node_id:
            return []
        result = self.current_result
        if result is None:
            reason = self.result_unavailable_reason()
            code = "STALE_RESULT" if self.result is not None else "CALCULATION_ERROR"
            return [FaultRow(mode.id, mode.name, None, None, reason,
                             self.selected_fault_type, status_code=code)
                    for mode in self.net.modes.values()]
        rows: list[FaultRow] = []
        for mode in self.net.modes.values():
            solver = result.ctx.solvers.get(mode.id)
            if solver is None:
                rows.append(FaultRow(mode.id, mode.name, None, None,
                                     result.ctx.errors.get(mode.id, "Режим не рассчитан"),
                                     self.selected_fault_type, status_code="MODE_UNAVAILABLE"))
                continue
            # Старые поля нужны потребителям прежней таблицы и не означают,
            # что выбранное несимметричное повреждение рассчитано по i2.
            i3 = i2 = None
            try:
                sc = solver.at(node_id)
                i3, i2 = sc.i3, sc.i2
            except Exception:
                # Новое повреждение имеет собственный результат/диагностику;
                # не блокировать его ошибкой прежнего вспомогательного API.
                pass
            try:
                fault = solver.fault_at(node_id, FaultSpec(self.selected_fault_type))
                rows.append(FaultRow(
                    mode.id, mode.name, i3, i2, fault_type=self.selected_fault_type,
                    iabc_ka=tuple(fault.iabc_ka), vabc_kv=tuple(fault.vabc_kv),
                    residual_current_ka=fault.residual_current_ka,
                    assumptions=tuple(fault.assumptions),
                ))
            except Exception as exc:
                rows.append(FaultRow(
                    mode.id, mode.name, i3, i2, str(exc), self.selected_fault_type,
                    status_code=str(getattr(exc, "code", "CALCULATION_ERROR")),
                ))
        return rows

    def fault_details_text(self, rows: list[FaultRow] | None = None) -> str:
        """Фазные значения и причина недоступности для текущего режима."""
        # Параметр оставлен для совместимости с виджетами. Их строки могут
        # относиться к прежнему снимку, узлу или виду КЗ даже после успешного
        # пересчёта, поэтому подробности всегда берутся из текущего состояния.
        rows = self.fault_rows()
        row = next((item for item in rows if item.mode_id == self.mode_id), None)
        title = self.fault_type_label()
        if row is None:
            return title + ": " + (self.result_unavailable_reason() or "Нет результата для выбранного узла и режима.")
        lines = [f"{title} · {row.mode_name}"]
        if row.error:
            label = fault_status_label(row.status_code)
            lines.append(row.error if row.error.startswith(label) else f"{label}: {row.error}")
        else:
            for phase, current, voltage in zip("ABC", row.iabc_ka or (), row.vabc_kv or ()):
                lines.append(f"Фаза {phase}: |I| = {_fmt_number(abs(current), 4)} кА; "
                             f"|Uф| = {_fmt_number(abs(voltage), 4)} кВ")
            if row.residual_current_ka is not None:
                lines.append(f"|3I0| = {_fmt_number(abs(row.residual_current_ka), 4)} кА")
            lines.extend(str(item) for item in row.assumptions)
        lines.append("Uф — остаточное напряжение фазы относительно земли в точке КЗ. "
                     "Токи — первичные, на ступени выбранного узла. Zповр = 0 Ом.")
        return "\n".join(lines)

    def fault_branch_view(self, branch_id: str | None = None) -> FaultBranchView:
        """Lazily solve a fault once, then select physical measurements by ID.

        Cache entries belong to one current, immutable calculation result.
        Every public read first checks freshness; an electrical draft or stale
        result clears the cache. Neither branch selection nor this method runs
        the project/protection calculation or mutates a saved project.
        """
        result = self.current_result
        if result is None:
            self.__dict__.pop('_fault_network_cache', None)
            return FaultBranchView(self.fault_type_label(), status=self.result_unavailable_reason())
        node_id = self.selected_fault_node()
        net = result.ctx.net
        mode = net.modes.get(self.mode_id)
        title = f'{self.fault_type_label()} · {mode.name if mode else self.mode_id}'
        if node_id not in net.nodes:
            return FaultBranchView(title, status='Выберите точку КЗ в дереве или на схеме.')
        title += f' · {net.nodes[node_id].name}'
        solver = result.ctx.solvers.get(self.mode_id)
        if solver is None:
            return FaultBranchView(title, status=result.ctx.errors.get(self.mode_id, 'Режим не рассчитан.'))
        if not hasattr(solver, 'fault_network_at'):
            return FaultBranchView(title, status='Распределение токов ветвей недоступно в этом расчётном API.')
        cached = self.__dict__.get('_fault_network_cache')
        if cached is None or cached[0] is not result:
            cached = (result, {})
            self._fault_network_cache = cached
        key = (self.mode_id, node_id, FaultSpec(self.selected_fault_type))
        entries = cached[1]
        if key not in entries:
            if len(entries) >= 8:
                entries.pop(next(iter(entries)))
            try:
                entries[key] = (solver.fault_network_at(node_id, key[2]), '')
            except Exception as exc:
                entries[key] = (None, f'{fault_status_label(str(getattr(exc, "code", "CALCULATION_ERROR")))}: {exc}')
        network, error = entries[key]
        if network is None:
            return FaultBranchView(title, status=error)
        choices = tuple((bid, f'{net.branches[bid].name} [{bid}]') for bid in network.branches if bid in net.branches)
        wanted = branch_id or self.__dict__.get('selected_fault_branch_id')
        if not wanted and self.selected_kind == 'branch':
            wanted = self.selected_id
        if wanted not in network.branches or wanted not in net.branches:
            wanted = choices[0][0] if choices else ''
        self.selected_fault_branch_id = wanted
        if not wanted:
            return FaultBranchView(title, status='Нет ветвей с расчётным представлением.')
        rows = branch_fault_measurements(net.branches[wanted], network.branches[wanted])
        diagnostics = ' '.join(dict.fromkeys(item.message for item in network.diagnostics
            if not item.branch_id or item.branch_id == wanted))
        return FaultBranchView(title, choices, wanted, rows, diagnostics,
                               tuple(dict.fromkeys((*MEASUREMENT_ASSUMPTIONS, *network.assumptions))))

    def setting_rows(self) -> list[SettingRow]:
        result = self.current_result
        if result is None or self.selected_kind != "branch":
            return []
        by_kind = result.results.get(self.selected_id, {})
        rows: list[SettingRow] = []
        for kind in ("МТЗ", "ТО", "ОЗЗ"):
            item = by_kind.get(kind)
            if item is None:
                continue
            current = "—" if item.i_primary is None else f"{_fmt_number(item.i_primary)} А"
            time = "—" if item.t is None else f"{_fmt_number(item.t)} с"
            rows.append(SettingRow(
                kind, current, time, item.governing_mode or "—", item.status,
            ))
        return rows

    def selected_checks(self) -> list[tuple[str, str, str]]:
        result = self.current_result
        if result is None or self.selected_kind != "branch":
            return []
        rows: list[tuple[str, str, str]] = []
        for protection in result.results.get(self.selected_id, {}).values():
            for check in protection.checks:
                value = "—" if check.value is None else _fmt_number(check.value)
                rows.append((f"{protection.kind}: {check.name}", value, check.status))
        return rows

    def selectivity_pairs(self) -> list:
        result = self.current_result
        return list(result.pairs) if result is not None else []

    def status_counts(self) -> dict[str, int]:
        counts = {OK: 0, FAIL: 0, UNRESOLVED: 0}
        result = self.current_result
        if result is None:
            counts[UNRESOLVED] = 1
            return counts
        for item in result.all_results():
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def warnings(self) -> list[str]:
        result = self.current_result
        if result is None:
            return [self.result_unavailable_reason()]
        return list(result.warnings)

    def report_text(self) -> str:
        """Короткая сводка для диалога GUI; экспорт файла появится отдельно."""
        counts = self.status_counts()
        lines = [
            self.net.name,
            f"Проект: {self.path}",
            f"Режим отображения: {self.mode.name if self.mode else '—'}",
            "",
            f"Защит без нарушений: {counts.get(OK, 0)}",
            f"Защит с нарушениями: {counts.get(FAIL, 0)}",
            f"Требуют исходных данных: {counts.get(UNRESOLVED, 0)}",
        ]
        result = self.current_result
        if result is not None:
            lines.append("Полнота расчёта: " + (
                "все обязательные проверки выполнены."
                if result.is_complete else "проверка не завершена; есть неопределённые случаи."
            ))
        if self.warnings():
            lines += ["", "Предупреждения:"] + [f"• {item}" for item in self.warnings()]
        point = self.selected_fault_node()
        point_name = self.net.nodes[point].name if point in self.net.nodes else "—"
        lines += ["", f"Точка КЗ: {point_name}", self.fault_details_text()]
        return "\n".join(lines)


def iter_tree(entries: Iterable[TreeEntry]) -> Iterable[TreeEntry]:
    """Плоский обход дерева, удобный для поиска и тестов."""
    for entry in entries:
        yield entry
        yield from iter_tree(entry.children)
