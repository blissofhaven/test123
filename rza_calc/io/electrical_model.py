# -*- coding: utf-8 -*-
"""Строгий JSON codec канонической электрической модели формата проекта v6."""
from __future__ import annotations

from typing import Any, Iterable

from ..domain.electrical import (
    Connection,
    ConnectionId,
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentAvailability,
    EquipmentId,
    EquipmentInstance,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    FeederId,
    LineKind,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineSection,
    LogicalLine,
    LogicalLineId,
    OperatingState,
    OperatingStateId,
    PortDefinition,
    PortId,
    PortInstance,
    PortKindId,
    PropertyDefinition,
    SwitchPosition,
    VoltageClass,
    VoltageClassId,
    thaw_json,
)


class ElectricalModelFormatError(ValueError):
    """JSON не соответствует строгой схеме ElectricalModel v5."""


MODEL_FIELDS = {
    "name",
    "revision",
    "neutral",
    "extensions",
    "voltage_classes",
    "equipment_types",
    "equipment",
    "ports",
    "electrical_nodes",
    "connections",
    "operating_states",
    "logical_lines",
    "line_sections",
}


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ElectricalModelFormatError(f"{context}: ожидался объект JSON.")
    return value


def _strict(
    value: Any,
    fields: set[str],
    context: str,
    *,
    optional: Iterable[str] = (),
) -> dict[str, Any]:
    row = _object(value, context)
    unknown = set(row) - fields
    missing = fields - set(optional) - set(row)
    if unknown:
        raise ElectricalModelFormatError(
            f"{context}: неизвестные поля {sorted(unknown)}."
        )
    if missing:
        raise ElectricalModelFormatError(
            f"{context}: отсутствуют обязательные поля {sorted(missing)}."
        )
    return row


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ElectricalModelFormatError(f"{context}: ожидался массив JSON.")
    return value


def _string(value: Any, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        qualifier = "строка" if empty else "непустая строка"
        raise ElectricalModelFormatError(f"{context}: ожидалась {qualifier}.")
    return value


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ElectricalModelFormatError(
            f"{context}: ожидалось целое число не меньше {minimum}."
        )
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ElectricalModelFormatError(f"{context}: ожидалось логическое значение.")
    return value


def _string_array(value: Any, context: str) -> list[str]:
    rows = [
        _string(item, f"{context}[{index}]")
        for index, item in enumerate(_array(value, context))
    ]
    if len(rows) != len(set(rows)):
        raise ElectricalModelFormatError(f"{context}: значения должны быть уникальны.")
    return rows


def _nullable_string(value: Any, context: str) -> str | None:
    return None if value is None else _string(value, context)


def _id(value: Any, cls, context: str):
    try:
        return cls(_string(value, context))
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def electrical_model_to_dict(model: ElectricalModel) -> dict[str, Any]:
    """Сериализовать ElectricalModel без расчётного Network и UI-данных."""
    return {
        "name": model.name,
        "revision": model.revision,
        "neutral": thaw_json(model.neutral),
        "extensions": thaw_json(model.extensions),
        "voltage_classes": [
            {
                "id": item.id.value,
                "nominal_voltage_v": item.nominal_voltage_v,
                "display_name": item.display_name,
                "system_kind": item.system_kind,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.voltage_classes.values()
        ],
        "equipment_types": [
            {
                "id": item.id.value,
                "schema_version": item.schema_version,
                "display_name": item.display_name,
                "behavior_key": item.behavior_key,
                "port_definitions": [
                    {
                        "role": port.role,
                        "display_name": port.display_name,
                        "kind_id": port.kind_id.value,
                        "voltage_group": port.voltage_group,
                        "allowed_voltage_class_ids": (
                            None
                            if port.allowed_voltage_class_ids is None
                            else sorted(
                                value.value
                                for value in port.allowed_voltage_class_ids
                            )
                        ),
                        "required": port.required,
                        "max_connections": port.max_connections,
                    }
                    for port in item.port_definitions
                ],
                "property_definitions": [
                    {
                        "key": prop.key,
                        "value_kind": prop.value_kind,
                        "required": prop.required,
                        "unit": prop.unit,
                        "default": thaw_json(prop.default),
                    }
                    for prop in item.property_definitions
                ],
                "capabilities": sorted(item.capabilities),
                "allow_additional_properties": item.allow_additional_properties,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.equipment_types.values()
        ],
        "equipment": [
            {
                "id": item.id.value,
                "type_id": item.type_id.value,
                "type_version": item.type_version,
                "name": item.name,
                "port_ids": [value.value for value in item.port_ids],
                "properties": thaw_json(item.properties),
                "voltage_class_by_group": {
                    group: value.value
                    for group, value in item.voltage_class_by_group.items()
                },
                "normal_position": (
                    item.normal_position.value if item.normal_position is not None else None
                ),
                "note": item.note,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.equipment.values()
        ],
        "ports": [
            {
                "id": item.id.value,
                "equipment_id": item.equipment_id.value,
                "role": item.role,
            }
            for item in model.ports.values()
        ],
        "electrical_nodes": [
            {
                "id": item.id.value,
                "name": item.name,
                "kind_id": item.kind_id.value,
                "declared_voltage_class_id": (
                    item.declared_voltage_class_id.value
                    if item.declared_voltage_class_id is not None
                    else None
                ),
                "note": item.note,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.electrical_nodes.values()
        ],
        "connections": [
            {
                "id": item.id.value,
                "port_id": item.port_id.value,
                "electrical_node_id": item.electrical_node_id.value,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.connections.values()
        ],
        "logical_lines": [
            {
                "id": item.id.value,
                "name": item.name,
                "line_kind": item.line_kind.value,
                "voltage_class_id": (
                    item.voltage_class_id.value
                    if item.voltage_class_id is not None
                    else None
                ),
                "feeder_id": item.feeder_id.value if item.feeder_id else None,
                "section_equipment_ids": [
                    value.value for value in item.section_equipment_ids
                ],
                "inherited_properties": thaw_json(item.inherited_properties),
                "note": item.note,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.logical_lines.values()
        ],
        "line_sections": [
            {
                "equipment_id": item.equipment_id.value,
                "logical_line_id": item.logical_line_id.value,
                "construction_segments": [
                    {
                        "id": segment.id.value,
                        "line_kind": segment.line_kind.value,
                        "length_mm": segment.length_mm,
                        "length_confirmation": segment.length_confirmation.value,
                        "impedance_confirmation": segment.impedance_confirmation.value,
                        "properties": thaw_json(segment.properties),
                        "extensions": thaw_json(segment.extensions),
                    }
                    for segment in item.construction_segments
                ],
                "extensions": thaw_json(item.extensions),
            }
            for item in model.line_sections.values()
        ],
        "operating_states": [
            {
                "id": item.id.value,
                "name": item.name,
                "positions": {
                    equipment_id.value: position.value
                    for equipment_id, position in item.positions.items()
                },
                "availability": {
                    equipment_id.value: availability.value
                    for equipment_id, availability in item.availability.items()
                },
                "system": item.system,
                "description": item.description,
                "extensions": thaw_json(item.extensions),
            }
            for item in model.operating_states.values()
        ],
    }


def _parse_voltage(value: Any, index: int) -> VoltageClass:
    context = f"electrical_model.voltage_classes[{index}]"
    row = _strict(
        value,
        {"id", "nominal_voltage_v", "display_name", "system_kind", "extensions"},
        context,
    )
    try:
        return VoltageClass(
            _id(row["id"], VoltageClassId, context + ".id"),
            _integer(row["nominal_voltage_v"], context + ".nominal_voltage_v", minimum=1),
            _string(row["display_name"], context + ".display_name"),
            _string(row["system_kind"], context + ".system_kind"),
            _object(row["extensions"], context + ".extensions"),
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_port_definition(value: Any, context: str) -> PortDefinition:
    row = _strict(
        value,
        {
            "role",
            "display_name",
            "kind_id",
            "voltage_group",
            "allowed_voltage_class_ids",
            "required",
            "max_connections",
        },
        context,
    )
    raw_allowed = row["allowed_voltage_class_ids"]
    allowed = (
        None
        if raw_allowed is None
        else frozenset(
            _id(item, VoltageClassId, context + ".allowed_voltage_class_ids")
            for item in _string_array(
                raw_allowed, context + ".allowed_voltage_class_ids"
            )
        )
    )
    try:
        return PortDefinition(
            _string(row["role"], context + ".role"),
            _string(row["display_name"], context + ".display_name"),
            _id(row["kind_id"], PortKindId, context + ".kind_id"),
            _nullable_string(row["voltage_group"], context + ".voltage_group"),
            allowed,
            _boolean(row["required"], context + ".required"),
            _integer(row["max_connections"], context + ".max_connections", minimum=1),
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_property_definition(value: Any, context: str) -> PropertyDefinition:
    row = _strict(
        value,
        {"key", "value_kind", "required", "unit", "default"},
        context,
    )
    try:
        return PropertyDefinition(
            _string(row["key"], context + ".key"),
            _string(row["value_kind"], context + ".value_kind"),
            _boolean(row["required"], context + ".required"),
            _string(row["unit"], context + ".unit", empty=True),
            row["default"],
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_equipment_type(value: Any, index: int) -> EquipmentTypeDefinition:
    context = f"electrical_model.equipment_types[{index}]"
    row = _strict(
        value,
        {
            "id",
            "schema_version",
            "display_name",
            "behavior_key",
            "port_definitions",
            "property_definitions",
            "capabilities",
            "allow_additional_properties",
            "extensions",
        },
        context,
    )
    ports = tuple(
        _parse_port_definition(item, f"{context}.port_definitions[{port_index}]")
        for port_index, item in enumerate(
            _array(row["port_definitions"], context + ".port_definitions")
        )
    )
    properties = tuple(
        _parse_property_definition(
            item, f"{context}.property_definitions[{property_index}]"
        )
        for property_index, item in enumerate(
            _array(row["property_definitions"], context + ".property_definitions")
        )
    )
    try:
        return EquipmentTypeDefinition(
            _id(row["id"], EquipmentTypeId, context + ".id"),
            _integer(row["schema_version"], context + ".schema_version", minimum=1),
            _string(row["display_name"], context + ".display_name"),
            _string(row["behavior_key"], context + ".behavior_key"),
            ports,
            properties,
            frozenset(_string_array(row["capabilities"], context + ".capabilities")),
            _boolean(
                row["allow_additional_properties"],
                context + ".allow_additional_properties",
            ),
            _object(row["extensions"], context + ".extensions"),
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_port(value: Any, index: int) -> PortInstance:
    context = f"electrical_model.ports[{index}]"
    row = _strict(value, {"id", "equipment_id", "role"}, context)
    try:
        return PortInstance(
            _id(row["id"], PortId, context + ".id"),
            _id(row["equipment_id"], EquipmentId, context + ".equipment_id"),
            _string(row["role"], context + ".role"),
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_equipment(value: Any, index: int) -> EquipmentInstance:
    context = f"electrical_model.equipment[{index}]"
    row = _strict(
        value,
        {
            "id",
            "type_id",
            "type_version",
            "name",
            "port_ids",
            "properties",
            "voltage_class_by_group",
            "normal_position",
            "note",
            "extensions",
        },
        context,
    )
    voltage_groups = _object(
        row["voltage_class_by_group"], context + ".voltage_class_by_group"
    )
    raw_position = row["normal_position"]
    try:
        return EquipmentInstance(
            _id(row["id"], EquipmentId, context + ".id"),
            _id(row["type_id"], EquipmentTypeId, context + ".type_id"),
            _integer(row["type_version"], context + ".type_version", minimum=1),
            _string(row["name"], context + ".name"),
            tuple(
                _id(item, PortId, context + ".port_ids")
                for item in _string_array(row["port_ids"], context + ".port_ids")
            ),
            _object(row["properties"], context + ".properties"),
            {
                _string(group, context + ".voltage_class_by_group key"): _id(
                    voltage_id,
                    VoltageClassId,
                    context + f".voltage_class_by_group.{group}",
                )
                for group, voltage_id in voltage_groups.items()
            },
            None
            if raw_position is None
            else SwitchPosition(_string(raw_position, context + ".normal_position")),
            _string(row["note"], context + ".note", empty=True),
            _object(row["extensions"], context + ".extensions"),
        )
    except (DomainInvariantError, ValueError) as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_node(value: Any, index: int) -> ElectricalNode:
    context = f"electrical_model.electrical_nodes[{index}]"
    row = _strict(
        value,
        {
            "id",
            "name",
            "kind_id",
            "declared_voltage_class_id",
            "note",
            "extensions",
        },
        context,
    )
    raw_voltage = row["declared_voltage_class_id"]
    try:
        return ElectricalNode(
            _id(row["id"], ElectricalNodeId, context + ".id"),
            _string(row["name"], context + ".name", empty=True),
            _id(row["kind_id"], PortKindId, context + ".kind_id"),
            None
            if raw_voltage is None
            else _id(
                raw_voltage,
                VoltageClassId,
                context + ".declared_voltage_class_id",
            ),
            _string(row["note"], context + ".note", empty=True),
            _object(row["extensions"], context + ".extensions"),
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_connection(value: Any, index: int) -> Connection:
    context = f"electrical_model.connections[{index}]"
    row = _strict(
        value, {"id", "port_id", "electrical_node_id", "extensions"}, context
    )
    try:
        return Connection(
            _id(row["id"], ConnectionId, context + ".id"),
            _id(row["port_id"], PortId, context + ".port_id"),
            _id(
                row["electrical_node_id"],
                ElectricalNodeId,
                context + ".electrical_node_id",
            ),
            _object(row["extensions"], context + ".extensions"),
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_logical_line(value: Any, index: int) -> LogicalLine:
    context = f"electrical_model.logical_lines[{index}]"
    row = _strict(
        value,
        {
            "id",
            "name",
            "line_kind",
            "voltage_class_id",
            "section_equipment_ids",
            "inherited_properties",
            "note",
            "extensions",
            "feeder_id",
        },
        context,
    )
    try:
        return LogicalLine(
            _id(row["id"], LogicalLineId, context + ".id"),
            _string(row["name"], context + ".name"),
            LineKind(_string(row["line_kind"], context + ".line_kind")),
            None
            if row["voltage_class_id"] is None
            else _id(
                row["voltage_class_id"],
                VoltageClassId,
                context + ".voltage_class_id",
            ),
            tuple(
                _id(item, EquipmentId, context + ".section_equipment_ids")
                for item in _string_array(
                    row["section_equipment_ids"],
                    context + ".section_equipment_ids",
                )
            ),
            _object(
                row["inherited_properties"], context + ".inherited_properties"
            ),
            _string(row["note"], context + ".note", empty=True),
            _object(row["extensions"], context + ".extensions"),
            None
            if row["feeder_id"] is None
            else _id(row["feeder_id"], FeederId, context + ".feeder_id"),
        )
    except (DomainInvariantError, ValueError) as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_line_construction_segment(
    value: Any,
    section_index: int,
    segment_index: int,
) -> LineConstructionSegment:
    context = (
        f"electrical_model.line_sections[{section_index}]"
        f".construction_segments[{segment_index}]"
    )
    row = _strict(
        value,
        {
            "id",
            "line_kind",
            "length_mm",
            "properties",
            "extensions",
            "length_confirmation",
            "impedance_confirmation",
        },
        context,
    )
    try:
        return LineConstructionSegment(
            _id(row["id"], LineConstructionSegmentId, context + ".id"),
            LineKind(_string(row["line_kind"], context + ".line_kind")),
            None
            if row["length_mm"] is None
            else _integer(row["length_mm"], context + ".length_mm", minimum=1),
            _object(row["properties"], context + ".properties"),
            _object(row["extensions"], context + ".extensions"),
            DataConfirmation(_string(
                row["length_confirmation"],
                context + ".length_confirmation",
            )),
            DataConfirmation(_string(
                row["impedance_confirmation"],
                context + ".impedance_confirmation",
            )),
        )
    except (DomainInvariantError, ValueError) as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_line_section(value: Any, index: int) -> LineSection:
    context = f"electrical_model.line_sections[{index}]"
    row = _strict(
        value,
        {
            "equipment_id",
            "logical_line_id",
            "construction_segments",
            "extensions",
        },
        context,
    )
    segments = tuple(
        _parse_line_construction_segment(item, index, segment_index)
        for segment_index, item in enumerate(
            _array(
                row["construction_segments"],
                context + ".construction_segments",
            )
        )
    )
    try:
        return LineSection(
            _id(row["equipment_id"], EquipmentId, context + ".equipment_id"),
            _id(
                row["logical_line_id"],
                LogicalLineId,
                context + ".logical_line_id",
            ),
            extensions=_object(row["extensions"], context + ".extensions"),
            construction_segments=segments,
        )
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def _parse_state(value: Any, index: int) -> OperatingState:
    context = f"electrical_model.operating_states[{index}]"
    row = _strict(
        value,
        {
            "id",
            "name",
            "positions",
            "system",
            "description",
            "extensions",
            "availability",
        },
        context,
    )
    positions = _object(row["positions"], context + ".positions")
    availability = _object(row["availability"], context + ".availability")
    try:
        state = OperatingState(
            _id(row["id"], OperatingStateId, context + ".id"),
            _string(row["name"], context + ".name"),
            {
                _id(key, EquipmentId, context + ".positions key"): SwitchPosition(
                    _string(position, context + f".positions.{key}")
                )
                for key, position in positions.items()
            },
            _string(row["system"], context + ".system"),
            _string(row["description"], context + ".description", empty=True),
            _object(row["extensions"], context + ".extensions"),
            {
                _id(key, EquipmentId, context + ".availability key"):
                EquipmentAvailability(
                    _string(value, context + f".availability.{key}")
                )
                for key, value in availability.items()
            },
        )
        from ..domain.operating_parameters import operating_parameters
        operating_parameters(state)
        return state
    except (DomainInvariantError, ValueError) as exc:
        raise ElectricalModelFormatError(f"{context}: {exc}") from exc


def electrical_model_from_dict(value: Any) -> ElectricalModel:
    """Восстановить модель; неизвестные поля и висячие ссылки запрещены."""
    row = _strict(value, MODEL_FIELDS, "electrical_model")
    revision = _integer(row["revision"], "electrical_model.revision")
    neutral = _object(row["neutral"], "electrical_model.neutral")
    if any(not isinstance(item, str) for item in neutral.values()):
        raise ElectricalModelFormatError(
            "electrical_model.neutral: значения должны быть строками."
        )
    model = ElectricalModel(
        _string(row["name"], "electrical_model.name"),
        neutral=neutral,
        extensions=_object(row["extensions"], "electrical_model.extensions"),
    )
    try:
        for index, item in enumerate(
            _array(row["voltage_classes"], "electrical_model.voltage_classes")
        ):
            model.register_voltage_class(_parse_voltage(item, index))
        for index, item in enumerate(
            _array(row["equipment_types"], "electrical_model.equipment_types")
        ):
            model.register_equipment_type(_parse_equipment_type(item, index))

        ports = [
            _parse_port(item, index)
            for index, item in enumerate(
                _array(row["ports"], "electrical_model.ports")
            )
        ]
        ports_by_id: dict[PortId, PortInstance] = {}
        for port in ports:
            if port.id in ports_by_id:
                raise ElectricalModelFormatError(
                    f"electrical_model.ports: ID '{port.id}' указан повторно."
                )
            ports_by_id[port.id] = port
        used_port_ids: set[PortId] = set()
        for index, item in enumerate(
            _array(row["equipment"], "electrical_model.equipment")
        ):
            equipment = _parse_equipment(item, index)
            try:
                equipment_ports = tuple(ports_by_id[port_id] for port_id in equipment.port_ids)
            except KeyError as exc:
                raise ElectricalModelFormatError(
                    f"electrical_model.equipment[{index}]: порт '{exc.args[0]}' не найден."
                ) from exc
            definition = model.equipment_type(
                equipment.type_id, equipment.type_version
            )
            model.add_equipment_instance(
                equipment,
                equipment_ports,
                # The codec restores section ownership below from the same
                # strict document.  The model is local and is discarded on
                # any later parse/integrity failure.
                _allow_unowned_line_section=(
                    definition.behavior_key == "line_section"
                ),
            )
            used_port_ids.update(equipment.port_ids)
        orphan_ports = set(ports_by_id) - used_port_ids
        if orphan_ports:
            raise ElectricalModelFormatError(
                "electrical_model.ports: найдены порты без оборудования: "
                + ", ".join(sorted(item.value for item in orphan_ports))
            )

        for index, item in enumerate(
            _array(row["electrical_nodes"], "electrical_model.electrical_nodes")
        ):
            model.add_node(_parse_node(item, index))
        for index, item in enumerate(
            _array(row["connections"], "electrical_model.connections")
        ):
            model.add_connection(_parse_connection(item, index))

        section_rows = [
            _parse_line_section(item, index)
            for index, item in enumerate(
                _array(row["line_sections"], "electrical_model.line_sections")
            )
        ]
        sections_by_equipment: dict[EquipmentId, LineSection] = {}
        for section in section_rows:
            if section.equipment_id in sections_by_equipment:
                raise ElectricalModelFormatError(
                    "electrical_model.line_sections: equipment_id "
                    f"'{section.equipment_id}' указан повторно."
                )
            sections_by_equipment[section.equipment_id] = section
        used_sections: set[EquipmentId] = set()
        for index, item in enumerate(
            _array(row["logical_lines"], "electrical_model.logical_lines")
        ):
            line = _parse_logical_line(item, index)
            try:
                sections = tuple(
                    sections_by_equipment[equipment_id]
                    for equipment_id in line.section_equipment_ids
                )
            except KeyError as exc:
                raise ElectricalModelFormatError(
                    f"electrical_model.logical_lines[{index}]: запись участка "
                    f"'{exc.args[0]}' не найдена."
                ) from exc
            model.add_logical_line(line, sections)
            used_sections.update(line.section_equipment_ids)
        orphan_sections = set(sections_by_equipment) - used_sections
        if orphan_sections:
            raise ElectricalModelFormatError(
                "electrical_model.line_sections: найдены записи вне logical_lines: "
                + ", ".join(sorted(item.value for item in orphan_sections))
            )
        for index, item in enumerate(
            _array(row["operating_states"], "electrical_model.operating_states")
        ):
            model.add_operating_state(_parse_state(item, index))
    except DomainInvariantError as exc:
        raise ElectricalModelFormatError(str(exc)) from exc

    issues = model.validate_integrity()
    if issues:
        raise ElectricalModelFormatError(
            "Нарушена целостность electrical_model:\n- "
            + "\n- ".join(item.message for item in issues)
        )
    model._restore_revision(revision)
    return model


__all__ = [
    "ElectricalModelFormatError",
    "electrical_model_from_dict",
    "electrical_model_to_dict",
]
