# -*- coding: utf-8 -*-
"""Project-level команды редактора этапа 3 без зависимости от Qt."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
)
from rza_calc.domain.electrical import (
    ElectricalNode,
    ElectricalNodeId,
    ElectricalModel,
    EquipmentId,
    LineKind,
    OperatingState,
    OperatingStateId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor import (
    EditorCommandError,
    EditorMode,
    ProjectEditorController,
    RepresentationGraphics,
)


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project(*, with_page: bool = True) -> _Project:
    page = DiagramPage(PageId("page.stage3.main"), "Основная схема")
    return _Project(
        ElectricalModel.with_builtins("Редактор"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,) if with_page else (),
            document_id=DiagramDocumentId("diagram.stage3"),
        ),
        ProjectCatalogSnapshots(),
    )


def _add_representation(
    project: _Project,
    equipment_id: EquipmentId,
    token: str,
    x: float,
    y: float,
) -> GraphicalRepresentationId:
    representation_id = GraphicalRepresentationId(token)
    representation = GraphicalRepresentation(
        representation_id,
        PageId("page.stage3.main"),
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=equipment_id,
        x=x,
        y=y,
    )
    project.diagram = DiagramDocument.create(
        project.diagram.name,
        project.diagram.pages.values(),
        (*project.diagram.representations.values(), representation),
        document_id=project.diagram.id,
        extensions=project.diagram.extensions,
    )
    return representation_id


def test_add_is_one_project_command_and_move_does_not_touch_topology() -> None:
    project = _project(with_page=False)
    controller = ProjectEditorController(project)

    added = controller.add_equipment(
        "builtin.load", "Нагрузка-1", x=13.0, y=27.0
    )
    equipment = project.electrical_model.equipment[added.equipment_id]
    representation = project.diagram.representations[added.representation_id]
    assert equipment.port_ids == added.port_ids
    assert representation.equipment_id == equipment.id
    assert added.page_id in project.diagram.pages
    signature = project.electrical_model.connectivity_signature()
    electrical_revision = project.electrical_model.revision

    controller.move_representations((added.representation_id,), 40.0, 20.0)
    assert project.electrical_model.connectivity_signature() == signature
    assert project.electrical_model.revision == electrical_revision

    controller.undo()
    assert project.diagram.representations[added.representation_id].x == 20.0
    controller.undo()
    assert added.equipment_id not in project.electrical_model.equipment
    assert added.representation_id not in project.diagram.representations
    controller.redo()
    assert added.equipment_id in project.electrical_model.equipment


def test_switch_library_defaults_create_breaker_disconnector_and_recloser() -> None:
    project = _project()
    controller = ProjectEditorController(project)

    breaker = controller.add_equipment("builtin.circuit_breaker", "QF-1")
    disconnector = controller.add_equipment("builtin.disconnector", "QS-1")
    recloser = controller.add_equipment("builtin.recloser", "REC-1")

    for equipment_id in (
        breaker.equipment_id,
        disconnector.equipment_id,
        recloser.equipment_id,
    ):
        assert project.electrical_model.equipment[equipment_id].normal_position is SwitchPosition.CLOSED
    recloser_row = project.electrical_model.equipment[recloser.equipment_id]
    assert recloser_row.properties["rated_current_a"] == 630.0
    assert recloser_row.properties["rated_voltage_v"] == 10_000
    assert recloser_row.voltage_class_by_group["main"].value == "builtin.voltage.ac.10kv"
    assert project.electrical_model.equipment_type(
        project.electrical_model.equipment[disconnector.equipment_id].type_id
    ).display_name == "Разъединитель"


def test_duplicate_group_remaps_ids_and_preserves_only_internal_connections() -> None:
    project = _project()
    first, _ = project.electrical_model.create_equipment(
        "builtin.circuit_breaker", "QF-1", normal_position=SwitchPosition.CLOSED
    )
    second, _ = project.electrical_model.create_equipment(
        "builtin.load", "Нагрузка"
    )
    project.electrical_model.connect_ports(first.port_ids[1], second.port_ids[0])
    first_rep = _add_representation(
        project, first.id, "representation.stage3.first", 20.0, 20.0
    )
    second_rep = _add_representation(
        project, second.id, "representation.stage3.second", 100.0, 20.0
    )
    controller = ProjectEditorController(project)
    original_port_ids = set(first.port_ids + second.port_ids)

    result = controller.duplicate((first_rep, second_rep), offset_x=40.0, offset_y=40.0)

    assert len(result.equipment_ids) == 2
    assert len(result.node_ids) == 1
    assert len(result.connection_ids) == 2
    assert set(result.equipment_ids).isdisjoint({first.id, second.id})
    copied_ports = {
        port_id
        for equipment_id in result.equipment_ids
        for port_id in project.electrical_model.equipment[equipment_id].port_ids
    }
    assert copied_ports.isdisjoint(original_port_ids)
    copied_connections = [
        project.electrical_model.connections[item]
        for item in result.connection_ids
    ]
    assert {item.port_id for item in copied_connections} <= copied_ports
    assert {item.electrical_node_id for item in copied_connections} == set(result.node_ids)


def test_delete_is_atomic_and_undo_redo_restore_model_graphics_and_links() -> None:
    project = _project()
    first, _ = project.electrical_model.create_equipment(
        "builtin.circuit_breaker", "QF-1", normal_position=SwitchPosition.CLOSED
    )
    second, _ = project.electrical_model.create_equipment(
        "builtin.load", "Нагрузка"
    )
    project.electrical_model.connect_ports(first.port_ids[1], second.port_ids[0])
    first_rep = _add_representation(
        project, first.id, "representation.stage3.delete", 20.0, 20.0
    )
    _add_representation(
        project, second.id, "representation.stage3.keep", 100.0, 20.0
    )
    original_connections = dict(project.electrical_model.connections)
    controller = ProjectEditorController(project)

    controller.delete_from_project((first_rep,))
    assert first.id not in project.electrical_model.equipment
    assert first_rep not in project.diagram.representations
    assert all(
        item.port_id not in first.port_ids
        for item in project.electrical_model.connections.values()
    )

    controller.undo()
    assert project.electrical_model.equipment[first.id] == first
    assert dict(project.electrical_model.connections) == original_connections
    assert first_rep in project.diagram.representations
    controller.redo()
    assert first.id not in project.electrical_model.equipment
    assert first_rep not in project.diagram.representations


def test_analysis_blocks_geometry_but_switching_keeps_coordinates() -> None:
    project = _project()
    recloser, _ = project.electrical_model.create_equipment(
        "builtin.recloser",
        "REC-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
        voltage_class_by_group={"main": next(
            item for item in project.electrical_model.voltage_classes
            if item.value == "builtin.voltage.ac.10kv"
        )},
        normal_position=SwitchPosition.CLOSED,
    )
    state = OperatingState(OperatingStateId("state.stage3"), "Нормальный режим")
    project.electrical_model.add_operating_state(state)
    representation_id = _add_representation(
        project, recloser.id, "representation.stage3.recloser", 60.0, 80.0
    )
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.ANALYSIS)

    with pytest.raises(EditorCommandError):
        controller.move_representations((representation_id,), 20.0, 0.0)
    before = project.diagram.representations[representation_id]
    controller.set_switch_position(
        state.id, recloser.id, SwitchPosition.OPEN, confirmed=True
    )
    after = project.diagram.representations[representation_id]
    assert (after.x, after.y) == (before.x, before.y)
    assert project.electrical_model.operating_states[state.id].positions[recloser.id] is SwitchPosition.OPEN


def test_node_busbar_resize_rotate_and_label_are_graphics_only() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    added = controller.add_electrical_node(
        "Шина-1",
        symbol_key="busbar",
        width=240.0,
        height=12.0,
        rotation_deg=90.0,
    )
    node_ids = set(project.electrical_model.electrical_nodes)
    electrical_revision = project.electrical_model.revision

    controller.resize_representation(added.representation_id, 320.0, 12.0)
    controller.rotate_representation(added.representation_id, 0.0)
    controller.set_label(
        added.representation_id,
        text="Секция 1",
        label_x=15.0,
        label_y=-20.0,
        visible=False,
    )

    assert set(project.electrical_model.electrical_nodes) == node_ids
    assert project.electrical_model.revision == electrical_revision
    row = project.diagram.representations[added.representation_id]
    graphics = RepresentationGraphics.from_representation(row)
    assert graphics.width == 320.0
    assert graphics.label_visible is False
    assert row.rotation_deg == 0.0
    assert row.label == "Секция 1"


def test_remove_from_page_requires_other_representation_or_unplaced_marker() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    added = controller.add_equipment("builtin.load", "Нагрузка")

    with pytest.raises(EditorCommandError):
        controller.remove_from_page((added.representation_id,))
    controller.remove_from_page(
        (added.representation_id,), mark_as_unplaced=True
    )
    assert added.equipment_id in project.electrical_model.equipment
    assert added.representation_id not in project.diagram.representations
    assert added.equipment_id.value in project.diagram.extensions[
        "stage3_unplaced_target_ids"
    ]
    controller.undo()
    assert added.representation_id in project.diagram.representations


def test_workspace_grid_snap_view_and_mode_are_serializable_diagram_data() -> None:
    project = _project()
    controller = ProjectEditorController(project)
    model_revision = project.electrical_model.revision
    controller.set_grid(False, grid_size=25.0)
    controller.set_snap(False)
    controller.set_view(1.75, 12_000.0, -8_000.0)
    controller.set_mode(EditorMode.ANALYSIS)

    restored = controller.workspace_state
    assert restored.grid_visible is False
    assert restored.snap_enabled is False
    assert restored.grid_size == 25.0
    assert restored.zoom == 1.75
    assert (restored.view_x, restored.view_y) == (12_000.0, -8_000.0)
    assert restored.mode is EditorMode.ANALYSIS
    assert project.electrical_model.revision == model_revision


def test_real_gtes_place_delete_and_save_remains_consistent(tmp_path: Path) -> None:
    from rza_calc.io.project import load_project, save_project

    source = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json"
    project = load_project(source)
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.EDIT)
    equipment_id = next(iter(project.electrical_model.equipment))
    representation_id = controller.place_existing_equipment(
        equipment_id, x=100.0, y=100.0, label="Проверка удаления"
    )
    controller.delete_from_project((representation_id,))

    target = tmp_path / "gtes-stage3-save.json"
    save_project(target, project)
    restored = load_project(target)
    assert equipment_id not in restored.electrical_model.equipment
    assert restored.diagram.validate_targets(restored.electrical_model) == ()


def test_unconnected_editor_draft_saves_reopens_and_blocks_only_calculation(
    tmp_path: Path,
) -> None:
    """Этап 3 сохраняет аппарат до появления соединительного UI Этапа 4."""
    from rza_calc.domain import CalculationRef, Equipment
    from rza_calc.io.project import load_project, save_project

    source = (
        Path(__file__).resolve().parent.parent
        / "tests" / "fixtures" / "legacy_projects"
        / "gtes_sever.json"
    )
    project = load_project(source)
    calculation_counts = (
        len(project.network.nodes),
        len(project.network.branches),
        len(project.network.loads),
    )
    project.structure.add_equipment(Equipment(
        "structure.stage3.vt1",
        "Трансформатор VT1",
        "power_transformer",
        [CalculationRef("branch", "VT1")],
    ))
    assert project.structure.validate(project.network) == []
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.EDIT)
    added = controller.add_equipment(
        "builtin.disconnector",
        "QS-черновик",
        x=240.0,
        y=160.0,
    )
    target = tmp_path / "gtes-stage3-unconnected-draft.json"

    save_project(target, project)
    restored = load_project(target)

    assert added.equipment_id in restored.electrical_model.equipment
    assert added.representation_id in restored.diagram.representations
    assert restored.diagram.validate_targets(restored.electrical_model) == ()
    assert any(
        item.severity == "error"
        and item.code.startswith("topology.")
        for item in restored.adapter_diagnostics
    )
    assert (
        len(restored.network.nodes),
        len(restored.network.branches),
        len(restored.network.loads),
    ) == calculation_counts
    assert restored.structure.validate(restored.network) == []
    from rza_calc.gui.view_model import ProjectViewModel
    from rza_calc.io.project import load

    vm = ProjectViewModel.open(target)
    assert vm.result is None
    assert "Расчёт заблокирован" in vm.calculation_error
    with pytest.raises(ValueError) as error:
        load(target)
    assert "Расчёт заблокирован" in str(error.value)


def test_duplicate_and_delete_physical_line_keep_logical_records_valid() -> None:
    project = _project()
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    first = ElectricalNode(
        ElectricalNodeId("node.stage3.line.a"),
        "A",
        declared_voltage_class_id=voltage,
    )
    second = ElectricalNode(
        ElectricalNodeId("node.stage3.line.b"),
        "B",
        declared_voltage_class_id=voltage,
    )
    project.electrical_model.add_node(first)
    project.electrical_model.add_node(second)
    line, section, _ = project.electrical_model.create_logical_line(
        "ВЛ-10 кВ",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        1_500_000,
        voltage_class_id=voltage,
    )
    representation_id = _add_representation(
        project,
        section.equipment_id,
        "representation.stage3.line",
        20.0,
        40.0,
    )
    controller = ProjectEditorController(project)

    copied = controller.duplicate((representation_id,))
    assert len(copied.equipment_ids) == 1
    assert len(copied.node_ids) == 2
    assert len(copied.connection_ids) == 2
    assert len(project.electrical_model.logical_lines) == 2
    assert copied.equipment_ids[0] in project.electrical_model.line_sections
    assert not [
        issue for issue in project.electrical_model.validate_integrity()
        if issue.severity == "error"
    ]

    controller.delete_from_project((representation_id,))
    assert line.id not in project.electrical_model.logical_lines
    assert section.equipment_id not in project.electrical_model.line_sections
    controller.undo()
    assert line.id in project.electrical_model.logical_lines
    assert section.equipment_id in project.electrical_model.line_sections
