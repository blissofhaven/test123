# -*- coding: utf-8 -*-
"""Read-only equipment label presentation for B3.

The legacy DTO boundary is deliberately local to this module. It reads the
already stored ``legacy_payload`` or the model's effective input properties;
it never builds a calculation DTO, runs a solver, or writes derived values to
the project. A3 can replace this reader without changing the canvas contract.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from rza_calc.domain.diagram import DiagramRoute, DiagramRouteKind, GraphicalRepresentation
from rza_calc.domain.electrical import (
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    EquipmentInstance,
)

from .state import label_is_manual


@dataclass(frozen=True, slots=True)
class LabelContent:
    name: str
    parameter: str = ""
    tooltip: str = ""

    def text(self, *, show_name: bool = True, show_parameters: bool = True) -> str:
        return " · ".join(
            value for enabled, value in (
                (show_name, self.name), (show_parameters, self.parameter)
            ) if enabled and value
        )


def _number(value: Any, *, allow_zero: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        return None
    return result


def _format(value: float) -> str:
    """Six significant figures are display precision, not new input data."""

    return format(value, ".6g").replace(".", ",")


def _power(value: Any, *, base_unit: str, missing: str, allow_zero: bool = False) -> str:
    number = _number(value, allow_zero=allow_zero)
    if number is None:
        return missing
    if base_unit == "кВА":
        return f"{_format(number / 1000.0)} МВ·А"
    if base_unit == "кВт" and number >= 1000.0:
        return f"{_format(number / 1000.0)} МВт"
    return f"{_format(number)} {base_unit}"


def _properties(model: ElectricalModel, equipment: EquipmentInstance) -> Mapping[str, Any]:
    """Resolve existing inheritance; unwrap the saved DTO exactly once."""

    values = model.effective_equipment_properties(equipment.id)
    legacy = values.get("legacy_payload")
    if legacy is not None:
        if not isinstance(legacy, Mapping):
            raise DomainInvariantError("Сохранённые legacy-параметры повреждены.")
        return legacy
    return values


def _transformer(values: Mapping[str, Any], *, three_winding: bool) -> str:
    power = _power(values.get("s_nom"), base_unit="кВА", missing="Sном не задана")
    keys = ("u_hv", "u_mv", "u_lv") if three_winding else ("u_hv", "u_lv")
    voltages = tuple(_number(values.get(key)) for key in keys)
    ratio = (
        "/".join(_format(value) for value in voltages if value is not None) + " кВ"
        if all(value is not None for value in voltages)
        else "Uном не задано"
    )
    return f"{power} · {ratio}"


def _alias(values: Mapping[str, Any], canonical: str, legacy: str) -> Any:
    first = values.get(canonical)
    second = values.get(legacy)
    if first is not None and second is not None and first != second:
        raise DomainInvariantError(
            f"Параметры «{canonical}» и «{legacy}» противоречат друг другу."
        )
    return first if first is not None else second


def _conductor(values: Mapping[str, Any]) -> str:
    mark = _alias(values, "conductor_mark", "brand")
    mark_text = mark.strip() if isinstance(mark, str) else ""
    section = _number(_alias(values, "cross_section_mm2", "section_mm2"))
    # Neither the number of cable cores nor parallel circuits means the other.
    # No "3×" is synthesized from an isolated cross-section value.
    section_text = f"{_format(section)} мм²" if section is not None else "сечение не задано"
    return f"{mark_text or 'марка не задана'} · {section_text}"


def _length(value: Any, *, confirmed: bool = True) -> str:
    number = _number(value)
    if number is None:
        return "длина не задана"
    suffix = "" if confirmed else " (не подтверждена)"
    return f"{_format(number)} км{suffix}"


def _line(
    model: ElectricalModel,
    equipment: EquipmentInstance,
    values: Mapping[str, Any],
    *,
    physical_section: bool,
) -> tuple[str, str]:
    if not physical_section or equipment.id not in model.line_sections:
        return f"{_conductor(values)} · {_length(values.get('length_km'))}", ""

    section = model.line_sections[equipment.id]
    segments = section.construction_segments
    rows: list[str] = []
    conductors: list[str] = []
    all_confirmed = True
    for index, segment in enumerate(segments, start=1):
        properties = model.effective_line_construction_segment_properties(
            equipment.id, segment.id
        )
        conductor = _conductor(properties)
        conductors.append(conductor)
        confirmed = segment.length_confirmation is DataConfirmation.CONFIRMED
        all_confirmed = all_confirmed and confirmed
        length = None if segment.length_mm is None else segment.length_mm / 1_000_000.0
        rows.append(f"Участок {index}: {conductor} · {_length(length, confirmed=confirmed)}")
    total = None if section.length_mm is None else section.length_mm / 1_000_000.0
    # Different construction segments must not masquerade as the first one.
    conductor_summary = (
        conductors[0] if conductors and len(set(conductors)) == 1
        else f"составная линия: {len(segments)} уч."
    )
    parameter = f"{conductor_summary} · {_length(total, confirmed=all_confirmed)}"
    return parameter, "\n".join(rows)


def _equipment_label(
    model: ElectricalModel,
    equipment: EquipmentInstance,
    name: str,
) -> LabelContent:
    """Return honest input-data labels without modifying either model.

    Unknown equipment kinds retain their name only. Missing requested inputs
    are explicit rather than replaced by assumed ratings or calculated power.
    Invalid property data stays visible as a warning and is not silently read
    from a second source after a failed authoritative lookup.
    """

    definition = model.equipment_types.get((equipment.type_id, equipment.type_version))
    behavior = definition.behavior_key if definition is not None else ""
    if behavior.startswith("legacy."):
        behavior = behavior[len("legacy."):]
    supported = {
        "transformer_2w", "transformer_3w", "line", "line_section",
        "switch", "tie", "recloser", "load", "generator",
    }
    if behavior not in supported:
        return LabelContent(name)

    try:
        values = _properties(model, equipment)
        detail = ""
        if behavior in {"transformer_2w", "transformer_3w"}:
            parameter = _transformer(values, three_winding=behavior == "transformer_3w")
        elif behavior in {"line", "line_section"}:
            parameter, detail = _line(
                model, equipment, values, physical_section=behavior == "line_section"
            )
        elif behavior in {"switch", "tie", "recloser"}:
            current = _number(values.get("rated_current_a"))
            parameter = f"{_format(current)} А" if current is not None else "Iном не задан"
        elif behavior == "generator":
            parameter = _power(values.get("p_nom"), base_unit="МВт", missing="Pном не задан")
        else:
            parameter = _power(
                values.get("p_kw"), base_unit="кВт", missing="P не задана", allow_zero=True
            )
        tooltip = "\n".join(value for value in (name, parameter, detail) if value)
        return LabelContent(name, parameter, tooltip)
    except (DomainInvariantError, KeyError, TypeError, ValueError, OverflowError) as exc:
        return LabelContent(name, "параметры недоступны", f"{name}\n{exc}")


def present_label(
    model: ElectricalModel,
    representation: GraphicalRepresentation,
) -> LabelContent:
    """Present one real graphical representation without creating any IDs."""

    equipment = model.equipment.get(representation.equipment_id)
    if equipment is None:
        node = model.electrical_nodes.get(representation.electrical_node_id)
        name = representation.label or (node.name if node is not None else "Электрический узел")
        return LabelContent(name)
    return _equipment_label(model, equipment, representation.label or equipment.name)


def present_route_label(model: ElectricalModel, route: DiagramRoute) -> LabelContent:
    """Present native physical branches which have no object representation.

    The route's actual equipment reference is sufficient. Plain graphical
    connections do not gain fictitious equipment names or parameters.
    """

    if route.kind is not DiagramRouteKind.EQUIPMENT_BRANCH:
        return LabelContent("")
    equipment = model.equipment.get(route.equipment_id)
    if equipment is None:
        return LabelContent("Оборудование не найдено")
    return _equipment_label(model, equipment, equipment.name)


__all__ = ["LabelContent", "label_is_manual", "present_label", "present_route_label"]
