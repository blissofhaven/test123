# -*- coding: utf-8 -*-
"""Каталожная целостность при разбиении и объединении физических линий."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

import pytest

from rza_calc.domain.catalog import (
    CatalogCategoryId,
    CatalogEntry,
    CatalogEntryId,
    CatalogOrigin,
)
from rza_calc.domain.catalog_snapshot import (
    CatalogBinding,
    CatalogEntrySnapshot,
    ParameterOverride,
    ProjectCatalogSnapshots,
)
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
    VoltageClassId,
)
from rza_calc.editor.controller import (
    EditorCommandError,
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.io.catalog_snapshot import catalog_snapshots_to_dict
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_to_dict


U10 = VoltageClassId("builtin.voltage.ac.10kv")
STAMP = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _blank_project(token: str) -> _Project:
    page = DiagramPage(PageId(f"page.catalog.{token}"), "Основная схема")
    return _Project(
        ElectricalModel.with_builtins(f"Каталожная проверка {token}"),
        DiagramDocument.create(
            "Каталожная проверка",
            (page,),
            document_id=DiagramDocumentId(f"diagram.catalog.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )


def _line_binding(project: _Project, section_id) -> CatalogBinding:
    equipment = project.electrical_model.equipment[section_id]
    entry = CatalogEntry(
        CatalogEntryId("catalog.line-section.test"),
        CatalogOrigin.USER,
        CatalogCategoryId("catalog.category.line-sections"),
        "Проверенная марка ВЛ",
        equipment.type_id,
        equipment.type_version,
        manufacturer="Испытательный завод",
        model="Провод-70",
        properties={"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
        voltage_class_by_group={"main": U10},
        source="Паспорт линии № 70",
        description="Неизменяемый тестовый снимок",
        modified_at=STAMP,
        entry_version=3,
        extensions={"verified": True},
    )
    snapshot = CatalogEntrySnapshot.from_entry(entry)
    override = ParameterOverride(
        "r1_ohm_per_km",
        0.4,
        0.42,
        "Уточнено по протоколу измерений",
        "Протокол № 15",
        STAMP,
    )
    return CatalogBinding(
        section_id,
        snapshot,
        {"installation": "воздух"},
        {"z1_ohm": 4.2},
        {"r1_ohm_per_km": override},
        {"binding_note": "исходная ветвь"},
    )


def _bound_line(token: str):
    project = _blank_project(token)
    builder = ProjectEditorController(project)
    start = builder.add_electrical_node(
        "Начало",
        x=0.0,
        y=0.0,
        voltage_class_id=U10,
    )
    finish = builder.add_electrical_node(
        "Конец",
        x=400.0,
        y=0.0,
        voltage_class_id=U10,
    )
    line = builder.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(finish.node_id),
        physical=PhysicalLineInput(
            10_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )
    binding = _line_binding(project, line.section_id)
    project.catalog_snapshots = ProjectCatalogSnapshots.from_bindings((binding,))
    return project, ProjectEditorController(project), line, binding


def _binding_payload(binding: CatalogBinding) -> tuple[object, ...]:
    return (
        binding.entry,
        binding.instance_overrides,
        binding.calculated_values,
        binding.parameter_overrides,
        binding.extensions,
    )


def _project_state(project: _Project) -> tuple[object, ...]:
    return (
        electrical_model_to_dict(project.electrical_model),
        diagram_to_dict(project.diagram),
        catalog_snapshots_to_dict(project.catalog_snapshots),
    )


def _create_tap(controller: ProjectEditorController, section_id):
    branch_end = controller.add_electrical_node(
        "Конец отпайки",
        x=200.0,
        y=180.0,
        voltage_class_id=U10,
    )
    return controller.create_tap(
        section_id,
        4_000_000,
        "Отпайка",
        LineKind.CABLE,
        NodeTarget(branch_end.node_id),
        physical=PhysicalLineInput(
            500_000,
            DataConfirmation.CONFIRMED,
        ),
    )


@pytest.mark.parametrize("operation", ("split", "tap", "recloser"))
def test_catalog_binding_is_copied_to_both_physical_parts_without_orphan(
    operation: str,
) -> None:
    project, controller, line, original = _bound_line(f"copy-{operation}")

    if operation == "split":
        result = controller.split_physical_line(line.section_id, 4_000_000)
        part_ids = (result.first_section_id, result.second_section_id)
    elif operation == "tap":
        result = _create_tap(controller, line.section_id)
        part_ids = (result.first_section_id, result.second_section_id)
    else:
        result = controller.insert_recloser(
            line.section_id,
            4_000_000,
            "Реклоузер Р-1",
        )
        part_ids = (result.left_section_id, result.right_section_id)

    bindings = project.catalog_snapshots.bindings
    assert line.section_id not in bindings
    assert set(bindings) == set(part_ids)
    assert line.section_id not in project.electrical_model.equipment
    assert project.catalog_snapshots.validate_targets(project.electrical_model) == ()
    for part_id in part_ids:
        assert bindings[part_id].equipment_id == part_id
        assert _binding_payload(bindings[part_id]) == _binding_payload(original)


@pytest.mark.parametrize("operation", ("tap", "recloser"))
def test_remove_tap_or_recloser_moves_binding_to_merged_section(
    operation: str,
) -> None:
    project, controller, line, original = _bound_line(f"merge-{operation}")

    if operation == "tap":
        inserted = _create_tap(controller, line.section_id)
        part_ids = (inserted.first_section_id, inserted.second_section_id)
        removed = controller.remove_tap(
            inserted.main_logical_line_id,
            inserted.tap_node_id,
            branch_line_ids=(inserted.branch_logical_line_id,),
        )
    else:
        inserted = controller.insert_recloser(
            line.section_id,
            4_000_000,
            "Реклоузер Р-2",
        )
        part_ids = (inserted.left_section_id, inserted.right_section_id)
        removed = controller.remove_recloser_from_line(inserted.recloser_id)

    assert removed.merged_section_id is not None
    merged_id = removed.merged_section_id
    bindings = project.catalog_snapshots.bindings
    assert set(bindings) == {merged_id}
    assert all(part_id not in bindings for part_id in part_ids)
    assert _binding_payload(bindings[merged_id]) == _binding_payload(original)
    assert merged_id in project.electrical_model.line_sections
    assert project.catalog_snapshots.validate_targets(project.electrical_model) == ()


@pytest.mark.parametrize("operation", ("tap", "recloser"))
@pytest.mark.parametrize("damage", ("missing", "mismatch"))
def test_unsafe_catalog_merge_is_rejected_atomically_in_russian(
    operation: str,
    damage: str,
) -> None:
    project, controller, line, _ = _bound_line(
        f"reject-{operation}-{damage}"
    )
    if operation == "tap":
        inserted = _create_tap(controller, line.section_id)
        part_ids = (inserted.first_section_id, inserted.second_section_id)
    else:
        inserted = controller.insert_recloser(
            line.section_id,
            4_000_000,
            "Реклоузер аварийной проверки",
        )
        part_ids = (inserted.left_section_id, inserted.right_section_id)

    first_binding = project.catalog_snapshots.bindings[part_ids[0]]
    if damage == "missing":
        project.catalog_snapshots = (
            project.catalog_snapshots.without_equipment(part_ids[1])
        )
        expected = "разные каталожные привязки"
    else:
        second_binding = project.catalog_snapshots.bindings[part_ids[1]]
        different_entry = replace(
            second_binding.entry,
            id=CatalogEntryId("catalog.line-section.other"),
            display_name="Другая марка ВЛ",
        )
        project.catalog_snapshots = project.catalog_snapshots.with_binding(
            replace(second_binding, entry=different_entry)
        )
        expected = "разные каталожные данные"
    # Новый controller принимает подготовленное, но целостное исходное
    # состояние; проверяем именно атомарность последующей команды merge.
    controller = ProjectEditorController(project)
    before = _project_state(project)
    journal_before = tuple(controller.journal)

    with pytest.raises(EditorCommandError) as caught:
        if operation == "tap":
            controller.remove_tap(
                inserted.main_logical_line_id,
                inserted.tap_node_id,
                branch_line_ids=(inserted.branch_logical_line_id,),
            )
        else:
            controller.remove_recloser_from_line(inserted.recloser_id)

    message = str(caught.value).lower()
    assert expected in message
    assert "автоматическое объединение небезопасно" in message
    assert _project_state(project) == before
    assert tuple(controller.journal) == journal_before
    assert project.catalog_snapshots.bindings[part_ids[0]] == first_binding
