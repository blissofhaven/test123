# -*- coding: utf-8 -*-
"""Типизированная адресация будущей точки КЗ без расчётных формул.

Положение внутри линии хранится как физическое расстояние от начала
электрической ветви. Координаты холста и точки графического маршрута в этот
слой не передаются.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain.electrical import (
    DomainInvariantError,
    ElectricalModel,
    ElectricalNodeId,
    EquipmentId,
    LineConstructionSegmentId,
    StableId,
)


class FaultLocationError(ValueError):
    """Точка КЗ ссылается на отсутствующее или невозможное физическое место."""


class FaultLocationId(StableId):
    prefix = "fault_location"


@dataclass(frozen=True, slots=True)
class ElectricalNodeFaultLocation:
    """Будущая точка КЗ на существующем электрическом узле или шинах."""

    id: FaultLocationId
    electrical_node_id: ElectricalNodeId

    def __post_init__(self) -> None:
        if not isinstance(self.id, FaultLocationId):
            raise TypeError("ElectricalNodeFaultLocation.id должен быть FaultLocationId.")
        if not isinstance(self.electrical_node_id, ElectricalNodeId):
            raise TypeError(
                "electrical_node_id точки КЗ должен быть ElectricalNodeId."
            )

    def validate(self, model: ElectricalModel) -> None:
        if self.electrical_node_id not in model.electrical_nodes:
            raise FaultLocationError(
                f"Электрический узел точки КЗ '{self.electrical_node_id}' не найден."
            )


@dataclass(frozen=True, slots=True)
class LineFaultLocation:
    """Будущая точка КЗ внутри одной электрической ветви линии."""

    id: FaultLocationId
    line_section_equipment_id: EquipmentId
    distance_mm_from_start: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, FaultLocationId):
            raise TypeError("LineFaultLocation.id должен быть FaultLocationId.")
        if not isinstance(self.line_section_equipment_id, EquipmentId):
            raise TypeError(
                "line_section_equipment_id точки КЗ должен быть EquipmentId."
            )
        if (
            isinstance(self.distance_mm_from_start, bool)
            or not isinstance(self.distance_mm_from_start, int)
            or self.distance_mm_from_start < 1
        ):
            raise FaultLocationError(
                "Расстояние точки КЗ должно быть положительным целым числом миллиметров."
            )

    def validate(self, model: ElectricalModel) -> None:
        try:
            section = model.line_section_for_equipment(
                self.line_section_equipment_id
            )
        except DomainInvariantError as exc:
            raise FaultLocationError(
                f"Электрическая ветвь точки КЗ "
                f"'{self.line_section_equipment_id}' не найдена."
            ) from exc
        if self.distance_mm_from_start >= section.length_mm:
            raise FaultLocationError(
                "Точка КЗ внутри линии должна находиться между её конечными узлами."
            )

    @classmethod
    def from_percent(
        cls,
        model: ElectricalModel,
        line_section_equipment_id: EquipmentId,
        percent_from_start: float,
        *,
        location_id: FaultLocationId | None = None,
    ) -> "LineFaultLocation":
        """Преобразовать процент физической длины в канонические миллиметры."""
        if (
            isinstance(percent_from_start, bool)
            or not isinstance(percent_from_start, (int, float))
            or not math.isfinite(float(percent_from_start))
            or not 0.0 < float(percent_from_start) < 100.0
        ):
            raise FaultLocationError(
                "Процент точки КЗ должен находиться строго между 0 и 100."
            )
        try:
            section = model.line_section_for_equipment(
                line_section_equipment_id
            )
        except DomainInvariantError as exc:
            raise FaultLocationError(
                f"Электрическая ветвь точки КЗ "
                f"'{line_section_equipment_id}' не найдена."
            ) from exc
        if section.length_mm < 2:
            raise FaultLocationError(
                "Физическая длина ветви слишком мала для внутренней точки КЗ."
            )
        distance = round(section.length_mm * float(percent_from_start) / 100.0)
        distance = min(max(distance, 1), section.length_mm - 1)
        result = cls(
            location_id or FaultLocationId.new(),
            line_section_equipment_id,
            distance,
        )
        result.validate(model)
        return result

    def percent_from_start(self, model: ElectricalModel) -> float:
        self.validate(model)
        section = model.line_section_for_equipment(
            self.line_section_equipment_id
        )
        return 100.0 * self.distance_mm_from_start / section.length_mm

    def construction_segment_position(
        self, model: ElectricalModel
    ) -> tuple[LineConstructionSegmentId, int]:
        """Вернуть ID конструктивного участка и смещение внутри него."""
        self.validate(model)
        section = model.line_section_for_equipment(
            self.line_section_equipment_id
        )
        cursor = 0
        for segment in section.construction_segments:
            finish = cursor + segment.length_mm
            if self.distance_mm_from_start <= finish:
                return segment.id, self.distance_mm_from_start - cursor
            cursor = finish
        raise FaultLocationError(
            "Физическое положение точки КЗ не попало ни в один участок."
        )


FaultLocation = ElectricalNodeFaultLocation | LineFaultLocation


__all__ = [
    "ElectricalNodeFaultLocation",
    "FaultLocation",
    "FaultLocationError",
    "FaultLocationId",
    "LineFaultLocation",
]
