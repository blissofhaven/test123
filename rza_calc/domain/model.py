# -*- coding: utf-8 -*-
"""
Физическая структура проекта, независимая от расчётной схемы.

``core.model.Network`` отвечает за электрический граф и расчёты. Этот модуль
описывает то, что видит инженер и будущий интерфейс: электростанции,
подстанции, уровни напряжения, ячейки и физическое оборудование.

Один аппарат может иметь несколько размещений. Например, трансформатор
связан с ячейками ВН и НН, трёхобмоточный — с ВН/СН/НН, а линия — с ячейками
двух разных подстанций. Поэтому оборудование не вкладывается напрямую в одну
ячейку: связь хранится отдельным объектом ``EquipmentPlacement``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..core.model import Network


FacilityKind = Literal[
    "power_plant",
    "substation",
    "switching_station",
    "ktp",
    "consumer",
    "other",
]
BayKind = Literal[
    "generator",
    "incoming",
    "outgoing",
    "transformer",
    "busbar",
    "bus_coupler",
    "measurement",
    "auxiliary",
    "other",
]
EquipmentKind = Literal[
    "generator",
    "external_grid",
    "power_transformer",
    "autotransformer",
    "line",
    "cable",
    "busbar",
    "circuit_breaker",
    "disconnector",
    "current_transformer",
    "voltage_transformer",
    "relay_terminal",
    "load",
    "other",
]
CalculationObjectKind = Literal["node", "branch", "load"]


@dataclass
class Facility:
    """Электрический объект верхнего уровня: ГТЭС, ПС, РП, КТП."""

    id: str
    name: str
    kind: FacilityKind
    parent_id: str | None = None
    note: str = ""


@dataclass
class VoltageLevel:
    """Уровень напряжения внутри одного объекта, например ОРУ-35 кВ."""

    id: str
    facility_id: str
    name: str
    u_nom: float
    note: str = ""


@dataclass
class BusSection:
    """Физическая секция шин, связанная с узлом расчётной схемы."""

    id: str
    voltage_level_id: str
    name: str
    calculation_node_id: str | None = None
    note: str = ""


@dataclass
class Bay:
    """Ячейка или присоединение конкретного уровня напряжения."""

    id: str
    voltage_level_id: str
    name: str
    kind: BayKind = "other"
    bus_section_id: str | None = None
    note: str = ""


@dataclass(frozen=True)
class CalculationRef:
    """Связь физического аппарата с объектом расчётного графа."""

    kind: CalculationObjectKind
    object_id: str
    role: str = ""


@dataclass
class Equipment:
    """Единый физический аппарат, не зависящий от числа его выводов."""

    id: str
    name: str
    kind: EquipmentKind
    calculation_refs: list[CalculationRef] = field(default_factory=list)
    note: str = ""


@dataclass
class EquipmentPlacement:
    """Размещение одного вывода/стороны аппарата в ячейке."""

    id: str
    equipment_id: str
    bay_id: str
    terminal: str = ""
    order: int = 0
    note: str = ""


@dataclass
class ProjectStructure:
    """
    Навигационная и физическая структура проекта.

    Она намеренно не наследует ``Network`` и не меняет электрический граф.
    Связь между двумя слоями выполняется только через ``CalculationRef``.
    """

    facilities: dict[str, Facility] = field(default_factory=dict)
    voltage_levels: dict[str, VoltageLevel] = field(default_factory=dict)
    bus_sections: dict[str, BusSection] = field(default_factory=dict)
    bays: dict[str, Bay] = field(default_factory=dict)
    equipment: dict[str, Equipment] = field(default_factory=dict)
    placements: dict[str, EquipmentPlacement] = field(default_factory=dict)

    def _all_ids(self) -> set[str]:
        return (set(self.facilities) | set(self.voltage_levels) | set(self.bus_sections)
                | set(self.bays) | set(self.equipment) | set(self.placements))

    def _check_new_id(self, object_id: str) -> None:
        if not object_id or not object_id.strip():
            raise ValueError("Идентификатор объекта не может быть пустым.")
        if object_id in self._all_ids():
            raise ValueError(f"Идентификатор '{object_id}' уже используется в структуре проекта.")

    def add_facility(self, obj: Facility) -> Facility:
        self._check_new_id(obj.id)
        if obj.parent_id is not None and obj.parent_id not in self.facilities:
            raise KeyError(f"Объект '{obj.name}': родитель '{obj.parent_id}' не найден.")
        self.facilities[obj.id] = obj
        return obj

    def add_voltage_level(self, obj: VoltageLevel) -> VoltageLevel:
        self._check_new_id(obj.id)
        if obj.facility_id not in self.facilities:
            raise KeyError(f"Уровень '{obj.name}': объект '{obj.facility_id}' не найден.")
        if obj.u_nom <= 0:
            raise ValueError(f"Уровень '{obj.name}': номинальное напряжение должно быть больше нуля.")
        self.voltage_levels[obj.id] = obj
        return obj

    def add_bay(self, obj: Bay) -> Bay:
        self._check_new_id(obj.id)
        if obj.voltage_level_id not in self.voltage_levels:
            raise KeyError(
                f"Ячейка '{obj.name}': уровень напряжения "
                f"'{obj.voltage_level_id}' не найден."
            )
        if obj.bus_section_id is not None:
            section = self.bus_sections.get(obj.bus_section_id)
            if section is None:
                raise KeyError(
                    f"Ячейка '{obj.name}': секция шин '{obj.bus_section_id}' не найдена."
                )
            if section.voltage_level_id != obj.voltage_level_id:
                raise ValueError(
                    f"Ячейка '{obj.name}' и секция '{section.name}' относятся к разным "
                    "уровням напряжения."
                )
        self.bays[obj.id] = obj
        return obj

    def add_bus_section(self, obj: BusSection) -> BusSection:
        self._check_new_id(obj.id)
        if obj.voltage_level_id not in self.voltage_levels:
            raise KeyError(
                f"Секция шин '{obj.name}': уровень '{obj.voltage_level_id}' не найден."
            )
        self.bus_sections[obj.id] = obj
        return obj

    def add_equipment(self, obj: Equipment) -> Equipment:
        self._check_new_id(obj.id)
        self.equipment[obj.id] = obj
        return obj

    def place_equipment(self, obj: EquipmentPlacement) -> EquipmentPlacement:
        self._check_new_id(obj.id)
        if obj.equipment_id not in self.equipment:
            raise KeyError(f"Размещение '{obj.id}': оборудование '{obj.equipment_id}' не найдено.")
        if obj.bay_id not in self.bays:
            raise KeyError(f"Размещение '{obj.id}': ячейка '{obj.bay_id}' не найдена.")
        self.placements[obj.id] = obj
        return obj

    # ---------- запросы для будущего GUI ----------
    def root_facilities(self) -> list[Facility]:
        return [f for f in self.facilities.values() if f.parent_id is None]

    def child_facilities(self, facility_id: str) -> list[Facility]:
        return [f for f in self.facilities.values() if f.parent_id == facility_id]

    def levels_of(self, facility_id: str) -> list[VoltageLevel]:
        levels = [v for v in self.voltage_levels.values() if v.facility_id == facility_id]
        return sorted(levels, key=lambda item: item.u_nom, reverse=True)

    def bays_of(self, voltage_level_id: str) -> list[Bay]:
        return [b for b in self.bays.values() if b.voltage_level_id == voltage_level_id]

    def sections_of(self, voltage_level_id: str) -> list[BusSection]:
        return [s for s in self.bus_sections.values()
                if s.voltage_level_id == voltage_level_id]

    def placements_in_bay(self, bay_id: str) -> list[EquipmentPlacement]:
        rows = [p for p in self.placements.values() if p.bay_id == bay_id]
        return sorted(rows, key=lambda item: (item.order, item.id))

    def equipment_in_bay(self, bay_id: str) -> list[Equipment]:
        return [self.equipment[p.equipment_id] for p in self.placements_in_bay(bay_id)]

    def placements_of(self, equipment_id: str) -> list[EquipmentPlacement]:
        return [p for p in self.placements.values() if p.equipment_id == equipment_id]

    # ---------- проверка связности двух моделей ----------
    def validate(self, network: "Network | None" = None) -> list[str]:
        problems: list[str] = []

        for obj in self.facilities.values():
            if not obj.name.strip():
                problems.append(f"Объект '{obj.id}': не задано название.")
            if obj.parent_id is not None and obj.parent_id not in self.facilities:
                problems.append(f"Объект '{obj.id}': родитель '{obj.parent_id}' не найден.")

        for start in self.facilities:
            seen: set[str] = set()
            current: str | None = start
            while current is not None and current in self.facilities:
                if current in seen:
                    problems.append(f"Объекты: обнаружен цикл вложенности с участием '{current}'.")
                    break
                seen.add(current)
                current = self.facilities[current].parent_id

        for obj in self.voltage_levels.values():
            if obj.facility_id not in self.facilities:
                problems.append(
                    f"Уровень '{obj.id}': объект '{obj.facility_id}' не найден."
                )
            if obj.u_nom <= 0:
                problems.append(f"Уровень '{obj.id}': напряжение должно быть больше нуля.")

        for obj in self.bus_sections.values():
            if obj.voltage_level_id not in self.voltage_levels:
                problems.append(
                    f"Секция шин '{obj.id}': уровень '{obj.voltage_level_id}' не найден."
                )

        for obj in self.bays.values():
            if obj.voltage_level_id not in self.voltage_levels:
                problems.append(
                    f"Ячейка '{obj.id}': уровень '{obj.voltage_level_id}' не найден."
                )
            if obj.bus_section_id is not None:
                section = self.bus_sections.get(obj.bus_section_id)
                if section is None:
                    problems.append(
                        f"Ячейка '{obj.id}': секция шин '{obj.bus_section_id}' не найдена."
                    )
                elif section.voltage_level_id != obj.voltage_level_id:
                    problems.append(
                        f"Ячейка '{obj.id}' и секция '{section.id}' относятся к разным "
                        "уровням напряжения."
                    )

        for obj in self.placements.values():
            if obj.equipment_id not in self.equipment:
                problems.append(
                    f"Размещение '{obj.id}': оборудование '{obj.equipment_id}' не найдено."
                )
            if obj.bay_id not in self.bays:
                problems.append(f"Размещение '{obj.id}': ячейка '{obj.bay_id}' не найдена.")

        for obj in self.equipment.values():
            refs = [(ref.kind, ref.object_id, ref.role) for ref in obj.calculation_refs]
            if len(refs) != len(set(refs)):
                problems.append(f"Оборудование '{obj.id}': повторяется связь с расчётной схемой.")

        if network is not None:
            available = {
                "node": set(network.nodes),
                "branch": set(network.branches),
                "load": set(network.loads),
            }
            for section in self.bus_sections.values():
                if (section.calculation_node_id is not None
                        and section.calculation_node_id not in available["node"]):
                    problems.append(
                        f"Секция шин '{section.id}': node '{section.calculation_node_id}' "
                        "не найден в расчётной схеме."
                    )
            for obj in self.equipment.values():
                for ref in obj.calculation_refs:
                    if ref.kind not in available:
                        problems.append(
                            f"Оборудование '{obj.id}': неизвестный тип расчётной связи "
                            f"'{ref.kind}'."
                        )
                    elif ref.object_id not in available[ref.kind]:
                        problems.append(
                            f"Оборудование '{obj.id}': {ref.kind} "
                            f"'{ref.object_id}' не найден в расчётной схеме."
                        )

        # Одинаковая сторона одного аппарата не должна оказаться в двух ячейках.
        terminals: dict[tuple[str, str], str] = {}
        for obj in self.placements.values():
            if not obj.terminal:
                continue
            key = (obj.equipment_id, obj.terminal)
            previous = terminals.get(key)
            if previous is not None:
                problems.append(
                    f"Оборудование '{obj.equipment_id}': сторона '{obj.terminal}' "
                    f"размещена дважды ('{previous}' и '{obj.id}')."
                )
            else:
                terminals[key] = obj.id

        # Не повторяем одинаковые сообщения о цикле при обходе каждой вершины.
        return list(dict.fromkeys(problems))
