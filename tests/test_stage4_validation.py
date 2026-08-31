# -*- coding: utf-8 -*-
"""Единая русская диагностика редактора соединений Этапа 4."""
from __future__ import annotations

from dataclasses import dataclass

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
)
from rza_calc.domain.electrical import (
    ConnectionId,
    DataConfirmation,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineKind,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.validation import (
    ProjectDiagnosticSeverity,
    ProjectValidationService,
)


U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project() -> _Project:
    page = DiagramPage(PageId("page.stage4.validation"), "Проверка")
    return _Project(
        ElectricalModel.with_builtins("Диагностика Этапа 4"),
        DiagramDocument.create(
            "Схема",
            (page,),
            document_id=DiagramDocumentId("diagram.stage4.validation"),
        ),
        ProjectCatalogSnapshots(),
    )


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.stage4.validation.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def test_unconnected_required_port_has_russian_action_and_navigation_target() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    added = controller.add_equipment(
        "builtin.recloser",
        "Реклоузер Р-1",
        voltage_class_by_group={"main": U10},
    )

    diagnostics = ProjectValidationService().validate(
        project.electrical_model,
        project.diagram,
    )
    diagnostic = next(
        item
        for item in diagnostics
        if item.code == "connection.required_port_unconnected"
        and item.object_id == added.equipment_id.value
    )

    assert diagnostic.severity is ProjectDiagnosticSeverity.ERROR
    assert "не подключён" in diagnostic.message
    assert "Подключите" in diagnostic.action or "Начните" in diagnostic.action
    assert diagnostic.representation_id == added.representation_id
    assert diagnostic.page_id == added.page_id


def test_unknown_line_and_tap_position_are_reported_without_fictitious_length() -> None:
    project = _project()
    model = project.electrical_model
    start = _node(model, "start")
    finish = _node(model, "finish")
    _, section, _ = model.create_logical_line(
        "ВЛ без длины",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        None,
        voltage_class_id=U10,
        length_confirmation=DataConfirmation.UNCONFIRMED,
        impedance_confirmation=DataConfirmation.UNCONFIRMED,
    )
    split = model.split_line_section(section.equipment_id, None)

    diagnostics = ProjectValidationService().validate(model, project.diagram)
    codes = {item.code for item in diagnostics}

    assert model.line_sections[split.first_section_id].length_mm is None
    assert model.line_sections[split.second_section_id].length_mm is None
    assert "line.length_missing" in codes
    assert "line.impedance_unconfirmed" in codes
    assert "tap.physical_position_unconfirmed" in codes


def test_electrical_line_without_canvas_route_is_an_explicit_warning() -> None:
    project = _project()
    model = project.electrical_model
    start = _node(model, "hidden.start")
    finish = _node(model, "hidden.finish")
    _, section, _ = model.create_logical_line(
        "Скрытая ВЛ",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        1_000_000,
        voltage_class_id=U10,
        inherited_properties={"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
    )

    diagnostics = ProjectValidationService().validate(model, project.diagram)
    warning = next(
        item
        for item in diagnostics
        if item.code == "diagram.branch_without_representation"
        and item.object_id == section.equipment_id.value
    )

    assert warning.severity is ProjectDiagnosticSeverity.WARNING
    assert "графического представления" in warning.message
    assert "Разместите" in warning.action


def test_validation_service_does_not_mutate_electrical_or_diagram_models() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    controller.add_equipment("builtin.load", "Нагрузка")
    electrical_before = project.electrical_model.connectivity_signature()
    electrical_revision = project.electrical_model.revision
    diagram_before = project.diagram

    ProjectValidationService().validate(project.electrical_model, project.diagram)

    assert project.electrical_model.connectivity_signature() == electrical_before
    assert project.electrical_model.revision == electrical_revision
    assert project.diagram == diagram_before


def test_topology_diagnostics_use_only_selected_operating_state_and_name_it() -> None:
    project = _project()
    model = project.electrical_model
    source_node = _node(model, "active.source")
    load_node = _node(model, "active.load")
    source_id = EquipmentId("equipment.stage4.validation.source")
    source_port_id = PortId("port.stage4.validation.source")
    source, _ = model.create_equipment(
        "builtin.external_grid",
        "Источник 10 кВ",
        equipment_id=source_id,
        port_ids_by_role={"terminal": source_port_id},
        voltage_class_by_group={"main": U10},
    )
    model.connect_port(
        source_port_id,
        source_node.id,
        connection_id=ConnectionId("connection.stage4.validation.source"),
    )
    recloser_id = EquipmentId("equipment.stage4.validation.recloser")
    from_port_id = PortId("port.stage4.validation.recloser.a")
    to_port_id = PortId("port.stage4.validation.recloser.b")
    recloser, _ = model.create_equipment(
        "builtin.recloser",
        "Реклоузер Р-1",
        equipment_id=recloser_id,
        port_ids_by_role={"a": from_port_id, "b": to_port_id},
        properties={"rated_current_a": 630.0},
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(
        from_port_id,
        source_node.id,
        connection_id=ConnectionId("connection.stage4.validation.recloser.from"),
    )
    model.connect_port(
        to_port_id,
        load_node.id,
        connection_id=ConnectionId("connection.stage4.validation.recloser.to"),
    )
    closed = OperatingState(
        OperatingStateId("state.stage4.validation.closed"),
        "Нормальная схема",
        {recloser.id: SwitchPosition.CLOSED},
    )
    opened = OperatingState(
        OperatingStateId("state.stage4.validation.open"),
        "Ремонтный режим",
        {recloser.id: SwitchPosition.OPEN},
    )
    model.add_operating_state(closed)
    model.add_operating_state(opened)
    service = ProjectValidationService()

    closed_diagnostics = service.validate(
        model,
        project.diagram,
        active_operating_state_id=closed.id,
    )
    assert not any(
        item.code == "topology.component_without_source"
        and item.object_id == load_node.id.value
        for item in closed_diagnostics
    )

    opened_diagnostics = service.validate(
        model,
        project.diagram,
        active_operating_state_id=opened.id,
    )
    source_less = next(
        item
        for item in opened_diagnostics
        if item.code == "topology.component_without_source"
        and item.object_id == load_node.id.value
    )
    assert "Режим: «Ремонтный режим»" in source_less.message
    assert "Нормальная схема" not in source_less.message
    assert source.id == source_id
