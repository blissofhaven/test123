# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramDocumentId, DiagramPage, PageId
from rza_calc.domain.electrical import (
    DataConfirmation,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    LineKind,
    OperatingState,
    OperatingStateId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import NodeTarget, PhysicalLineInput, ProjectEditorController
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project() -> _Project:
    page = DiagramPage(PageId("page.automatic-junctions"), "Основная схема")
    return _Project(
        ElectricalModel.with_builtins("Автоматические узлы"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,),
            document_id=DiagramDocumentId("diagram.automatic-junctions"),
        ),
        ProjectCatalogSnapshots(),
    )


def _main_line(controller: ProjectEditorController):
    first = controller.add_electrical_node(
        "Начало", x=0.0, y=0.0, voltage_class_id=U10
    )
    second = controller.add_electrical_node(
        "Конец", x=600.0, y=0.0, voltage_class_id=U10
    )
    return controller.create_physical_line(
        "ВЛ-10 кВ",
        LineKind.OVERHEAD,
        NodeTarget(first.node_id, first.representation_id),
        NodeTarget(second.node_id, second.representation_id),
        physical=PhysicalLineInput(
            10_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )


def test_breaker_insertion_has_two_persistent_nodes_and_one_undo_redo() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    main = _main_line(controller)
    before = electrical_model_fingerprint(controller.model)

    inserted = controller.insert_series_equipment(
        main.section_id,
        4_000_000,
        "builtin.circuit_breaker",
        "QF-1",
        normal_position=SwitchPosition.CLOSED,
    )
    after = electrical_model_fingerprint(controller.model)

    assert inserted.left_node_id != inserted.right_node_id
    assert controller.model.equipment[inserted.equipment_id].type_id.value == (
        "builtin.circuit_breaker"
    )
    assert after != before
    assert TopologyEngine().compile(controller.model).has_path(
        inserted.left_node_id, inserted.right_node_id
    )

    opened = OperatingState(
        OperatingStateId("state.automatic-junctions.breaker-open"),
        "Выключатель отключён",
        {inserted.equipment_id: SwitchPosition.OPEN},
    )
    controller.model.add_operating_state(opened)
    assert not TopologyEngine().compile(controller.model, opened.id).has_path(
        inserted.left_node_id, inserted.right_node_id
    )
    # Режим создан вне истории контроллера; убрать его перед точным undo.
    controller.model._operating_states.pop(opened.id)
    controller.model._revision -= 1

    controller.undo()
    assert electrical_model_fingerprint(controller.model) == before
    assert inserted.equipment_id not in controller.model.equipment
    controller.redo()
    assert electrical_model_fingerprint(controller.model) == after
    assert inserted.equipment_id in controller.model.equipment
    assert inserted.left_node_id in controller.model.electrical_nodes
    assert inserted.right_node_id in controller.model.electrical_nodes
    assert controller.journal[-1].electrical_fingerprint_before == before
    assert controller.journal[-1].electrical_fingerprint_after == after


def test_transformer_drop_creates_tap_not_series_insertion() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    main = _main_line(controller)

    attached = controller.attach_equipment_to_line(
        main.section_id,
        3_000_000,
        "builtin.transformer_2w",
        "Т-1",
        terminal_role="hv",
    )

    transformer = controller.model.equipment[attached.equipment_id]
    assert "line_insertion" not in transformer.extensions
    assert transformer.extensions["placement_origin"] == (
        "automatic_branch_attachment"
    )
    assert controller.model.logical_lines[
        attached.main_logical_line_id
    ].section_equipment_ids == (
        attached.first_section_id,
        attached.second_section_id,
    )
    connected_at_tap = {
        controller.model.ports[item.port_id].equipment_id
        for item in controller.model.connections.values()
        if item.electrical_node_id == attached.tap_node_id
    }
    assert connected_at_tap == {
        attached.first_section_id,
        attached.second_section_id,
        attached.equipment_id,
    }
    assert controller.model.connection_for_port(attached.connected_port_id) is not None
    assert controller.model.connection_for_port(
        controller.model.port_by_role(attached.equipment_id, "lv").id
    ) is None


def test_generic_series_removal_restores_continuous_line_atomically() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    main = _main_line(controller)
    inserted = controller.insert_series_equipment(
        main.section_id,
        6_000_000,
        "builtin.disconnector",
        "QS-1",
        normal_position=SwitchPosition.CLOSED,
    )
    inserted_fingerprint = electrical_model_fingerprint(controller.model)

    removed = controller.remove_series_equipment_from_line(
        inserted.equipment_id
    )

    assert inserted.equipment_id not in controller.model.equipment
    assert inserted.left_node_id not in controller.model.electrical_nodes
    assert inserted.right_node_id not in controller.model.electrical_nodes
    line = controller.model.logical_lines[removed.logical_line_id]
    assert line.section_equipment_ids == (removed.merged_section_id,)
    assert controller.model.line_sections[removed.merged_section_id].length_mm == (
        10_000_000
    )

    controller.undo()
    assert electrical_model_fingerprint(controller.model) == inserted_fingerprint
    assert inserted.equipment_id in controller.model.equipment


def test_graphical_move_does_not_change_electrical_fingerprint() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    main = _main_line(controller)
    route = next(
        item for item in controller.diagram.routes.values()
        if item.equipment_id == main.section_id
    )
    representation_id = route.start_anchor.representation_id
    before = electrical_model_fingerprint(controller.model)

    controller.move_representations((representation_id,), 120.0, 80.0)

    assert electrical_model_fingerprint(controller.model) == before
    assert controller.journal[-1].electrical_fingerprint_before == before
    assert controller.journal[-1].electrical_fingerprint_after == before
