# -*- coding: utf-8 -*-
"""Native end-to-end scenario for automatic electrical junctions."""
from __future__ import annotations

from pathlib import Path

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
)
from rza_calc.domain.electrical import (
    DataConfirmation,
    ElectricalModel,
    LineKind,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.model import ProjectStructure
from rza_calc.editor import (
    PhysicalLineInput,
    PortTarget,
    ProjectEditorController,
)
from rza_calc.io.project import load_project, save_project
from rza_calc.topology import TopologyEngine


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json"
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _fresh_project():
    """Use the public project codec as a shell for one new canonical model."""
    project = load_project(EXAMPLE)
    project.electrical_model = ElectricalModel.with_builtins(
        "Автоматические узлы — сквозной сценарий"
    )
    page = DiagramPage(
        PageId("page.automatic-junctions.integration"),
        "Основная схема",
    )
    project.diagram = DiagramDocument.create(
        "Автоматические узлы",
        (page,),
        document_id=DiagramDocumentId(
            "diagram.automatic-junctions.integration"
        ),
    )
    project.catalog_snapshots = ProjectCatalogSnapshots()
    project.structure = ProjectStructure()
    project.refresh_calculation_view(force=True)
    return project


def _physical(length_mm: int) -> PhysicalLineInput:
    return PhysicalLineInput(
        length_mm,
        DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
        DataConfirmation.CONFIRMED,
    )


def _connected_equipment(model: ElectricalModel, node_id) -> set:
    return {
        model.ports[connection.port_id].equipment_id
        for connection in model.connections.values()
        if connection.electrical_node_id == node_id
    }


def _assert_integrity(project) -> None:
    assert not [
        issue
        for issue in project.electrical_model.validate_integrity()
        if issue.severity == "error"
    ]
    assert project.diagram.validate_targets(project.electrical_model) == ()
    assert (
        project.catalog_snapshots.validate_targets(project.electrical_model)
        == ()
    )


def test_native_10kv_feeder_with_three_taps_recloser_and_backup_roundtrip(
    tmp_path: Path,
) -> None:
    project = _fresh_project()
    controller = ProjectEditorController(project)

    # Every starting object is placed by an ordinary public editor command.
    primary = controller.add_equipment(
        "builtin.external_grid",
        "Основной источник 10 кВ",
        x=0.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
    )
    busbar = controller.add_equipment(
        "builtin.busbar",
        "Шины 10 кВ",
        x=160.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
        symbol_key="busbar_horizontal",
    )
    breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-10 кВ",
        x=320.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    backup = controller.add_equipment(
        "builtin.external_grid",
        "Резервный источник 10 кВ",
        x=1_400.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
    )

    # Source -> busbar creates the node. Busbar -> breaker reuses that same
    # electrical node; no manual ElectricalNode is created by the test.
    source_bus = controller.connect_ports(
        primary.port_ids[0],
        busbar.port_ids[0],
        first_representation_id=primary.representation_id,
        second_representation_id=busbar.representation_id,
    )
    breaker_bus = controller.connect_ports(
        breaker.port_ids[0],
        busbar.port_ids[0],
        first_representation_id=breaker.representation_id,
        second_representation_id=busbar.representation_id,
    )
    assert breaker_bus.node_id == source_bus.node_id
    assert project.electrical_model.electrical_nodes[
        source_bus.node_id
    ].extensions["creation_origin"] == "automatic_port_to_port"
    assert _connected_equipment(project.electrical_model, source_bus.node_id) == {
        primary.equipment_id,
        busbar.equipment_id,
        breaker.equipment_id,
    }

    main = controller.create_physical_line(
        "Основная ВЛ-10 кВ",
        LineKind.OVERHEAD,
        PortTarget(breaker.port_ids[1], breaker.representation_id, "b"),
        PortTarget(backup.port_ids[0], backup.representation_id, "terminal"),
        physical=_physical(12_000_000),
    )
    assert project.electrical_model.electrical_nodes[
        main.start_node_id
    ].extensions["creation_origin"] == "automatic_port_endpoint"
    assert project.electrical_model.electrical_nodes[
        main.end_node_id
    ].extensions["creation_origin"] == "automatic_port_endpoint"

    # Main line: tap KTP-1 -> continuation -> inline recloser -> taps KTP-2/3.
    tap_1 = controller.attach_equipment_to_line(
        main.section_id,
        2_000_000,
        "builtin.transformer_2w",
        "КТП-1",
        terminal_role="hv",
        equipment_x=520.0,
        equipment_y=180.0,
    )
    fingerprint_before_recloser = electrical_model_fingerprint(
        project.electrical_model
    )
    recloser = controller.insert_series_equipment(
        tap_1.second_section_id,
        3_000_000,
        "builtin.recloser",
        "Реклоузер Р-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
        normal_position=SwitchPosition.CLOSED,
        x=700.0,
        y=0.0,
    )
    fingerprint_with_recloser = electrical_model_fingerprint(
        project.electrical_model
    )
    assert fingerprint_with_recloser != fingerprint_before_recloser
    assert recloser.left_node_id != recloser.right_node_id

    # One public command is one exact undo/redo unit, including persistent IDs.
    controller.undo()
    assert (
        electrical_model_fingerprint(project.electrical_model)
        == fingerprint_before_recloser
    )
    assert recloser.equipment_id not in project.electrical_model.equipment
    controller.redo()
    assert (
        electrical_model_fingerprint(project.electrical_model)
        == fingerprint_with_recloser
    )
    assert recloser.equipment_id in project.electrical_model.equipment
    assert recloser.left_node_id in project.electrical_model.electrical_nodes
    assert recloser.right_node_id in project.electrical_model.electrical_nodes

    tap_2 = controller.attach_equipment_to_line(
        recloser.right_section_id,
        2_000_000,
        "builtin.transformer_2w",
        "КТП-2",
        terminal_role="hv",
        equipment_x=900.0,
        equipment_y=180.0,
    )
    tap_3 = controller.attach_equipment_to_line(
        tap_2.second_section_id,
        2_000_000,
        "builtin.transformer_2w",
        "КТП-3",
        terminal_role="hv",
        equipment_x=1_100.0,
        equipment_y=180.0,
    )

    model = project.electrical_model
    for tap, expected_transformer in (
        (tap_1, tap_1.equipment_id),
        (tap_2, tap_2.equipment_id),
        (tap_3, tap_3.equipment_id),
    ):
        assert model.electrical_nodes[tap.tap_node_id].extensions[
            "creation_origin"
        ] == "automatic_line_split"
        assert model.equipment[expected_transformer].extensions[
            "placement_origin"
        ] == "automatic_branch_attachment"
        assert len(_connected_equipment(model, tap.tap_node_id)) == 3
        assert expected_transformer in _connected_equipment(
            model, tap.tap_node_id
        )
    assert model.electrical_nodes[recloser.left_node_id].extensions[
        "creation_origin"
    ] == "automatic_inline_insertion"
    assert model.electrical_nodes[recloser.right_node_id].extensions[
        "creation_origin"
    ] == "automatic_inline_insertion"
    assert all("creation_origin" in node.extensions for node in model.electrical_nodes.values())

    # Both logical pieces remain ordered continuous chains; their physical
    # lengths still total the original 12 km and the final section reaches
    # the reserve-source endpoint.
    left_line = model.logical_lines[recloser.left_logical_line_id]
    right_line = model.logical_lines[recloser.right_logical_line_id]
    assert left_line.section_equipment_ids == (
        tap_1.first_section_id,
        recloser.left_section_id,
    )
    assert right_line.section_equipment_ids == (
        tap_2.first_section_id,
        tap_3.first_section_id,
        tap_3.second_section_id,
    )
    all_main_sections = (
        *left_line.section_equipment_ids,
        *right_line.section_equipment_ids,
    )
    assert sum(
        model.line_sections[item].length_mm or 0 for item in all_main_sections
    ) == 12_000_000
    for line in (left_line, right_line):
        endpoint_pairs = [
            (
                model.node_for_port(model.port_by_role(section_id, "from").id).id,
                model.node_for_port(model.port_by_role(section_id, "to").id).id,
            )
            for section_id in line.section_equipment_ids
        ]
        assert all(
            first[1] == second[0]
            for first, second in zip(endpoint_pairs, endpoint_pairs[1:])
        )
    assert model.node_for_port(
        model.port_by_role(tap_1.first_section_id, "from").id
    ).id == main.start_node_id
    assert model.node_for_port(
        model.port_by_role(tap_3.second_section_id, "to").id
    ).id == main.end_node_id

    normal = TopologyEngine().compile(model)
    assert normal.has_path(source_bus.node_id, main.end_node_id)
    assert normal.has_path(recloser.left_node_id, recloser.right_node_id)

    fingerprint_before_open = electrical_model_fingerprint(model)
    state_id = controller.switch_equipment(
        recloser.equipment_id,
        SwitchPosition.OPEN,
        confirmed=True,
    )
    fingerprint_open = electrical_model_fingerprint(model)
    opened = TopologyEngine().compile(model, state_id)
    assert not opened.has_path(recloser.left_node_id, recloser.right_node_id)
    assert opened.sources_for(tap_1.tap_node_id) == (primary.equipment_id,)
    assert opened.sources_for(tap_2.tap_node_id) == (backup.equipment_id,)
    assert opened.sources_for(tap_3.tap_node_id) == (backup.equipment_id,)
    assert opened.is_energized(tap_1.tap_node_id) is True
    assert opened.is_energized(tap_2.tap_node_id) is True
    assert opened.is_energized(tap_3.tap_node_id) is True

    # Switching is also a single reversible public command.
    controller.undo()
    assert electrical_model_fingerprint(model) == fingerprint_before_open
    assert state_id not in model.operating_states
    assert TopologyEngine().compile(model).has_path(
        recloser.left_node_id, recloser.right_node_id
    )
    controller.redo()
    assert electrical_model_fingerprint(model) == fingerprint_open
    reopened_state = TopologyEngine().compile(model, state_id)
    assert reopened_state.semantic_signature() == opened.semantic_signature()

    _assert_integrity(project)
    target = tmp_path / "automatic-junctions-native.json"
    save_project(target, project)
    restored = load_project(target)

    assert (
        electrical_model_fingerprint(restored.electrical_model)
        == fingerprint_open
    )
    _assert_integrity(restored)
    restored_open = TopologyEngine().compile(restored.electrical_model, state_id)
    assert restored_open.semantic_signature() == opened.semantic_signature()
    assert restored_open.sources_for(tap_1.tap_node_id) == (
        primary.equipment_id,
    )
    assert restored_open.sources_for(tap_2.tap_node_id) == (
        backup.equipment_id,
    )
    assert restored_open.sources_for(tap_3.tap_node_id) == (
        backup.equipment_id,
    )
