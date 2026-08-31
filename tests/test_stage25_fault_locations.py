# -*- coding: utf-8 -*-
"""Физическая адресация будущей точки КЗ без расчёта ТКЗ."""
from __future__ import annotations

import pytest

from rza_calc.calculation import (
    ElectricalNodeFaultLocation,
    FaultLocationError,
    FaultLocationId,
    LineFaultLocation,
)
from rza_calc.domain.electrical import (
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    VoltageClassId,
)


U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _model():
    model = ElectricalModel.with_builtins("Физическая точка КЗ")
    first = ElectricalNode(
        ElectricalNodeId("node.fault.first"),
        "Начало",
        declared_voltage_class_id=U10,
    )
    second = ElectricalNode(
        ElectricalNodeId("node.fault.second"),
        "Конец",
        declared_voltage_class_id=U10,
    )
    model.add_node(first)
    model.add_node(second)
    _, section, _ = model.create_logical_line(
        "Составная линия",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        1_000,
        section_equipment_id=EquipmentId("equipment.fault.line"),
        construction_segments=(
            LineConstructionSegment(
                LineConstructionSegmentId("segment.fault.overhead"),
                LineKind.OVERHEAD,
                400,
                {"conductor_mark": "АС-70"},
            ),
            LineConstructionSegment(
                LineConstructionSegmentId("segment.fault.cable"),
                LineKind.CABLE,
                600,
                {"conductor_mark": "АПвПу-95"},
            ),
        ),
    )
    return model, first, section


def test_fault_location_can_reference_existing_electrical_node() -> None:
    model, first, _ = _model()
    location = ElectricalNodeFaultLocation(
        FaultLocationId("fault.node"), first.id
    )
    location.validate(model)


def test_line_fault_location_uses_physical_distance_and_segment_order() -> None:
    model, _, section = _model()
    location = LineFaultLocation.from_percent(
        model,
        section.equipment_id,
        75.0,
        location_id=FaultLocationId("fault.line.75"),
    )

    assert location.distance_mm_from_start == 750
    assert location.percent_from_start(model) == pytest.approx(75.0)
    assert location.construction_segment_position(model) == (
        LineConstructionSegmentId("segment.fault.cable"),
        350,
    )


def test_line_fault_location_rejects_endpoint_and_unknown_branch() -> None:
    model, _, section = _model()
    endpoint = LineFaultLocation(
        FaultLocationId("fault.endpoint"),
        section.equipment_id,
        section.length_mm,
    )
    with pytest.raises(FaultLocationError):
        endpoint.validate(model)
    with pytest.raises(FaultLocationError):
        LineFaultLocation.from_percent(
            model,
            EquipmentId("equipment.missing"),
            50.0,
        )
