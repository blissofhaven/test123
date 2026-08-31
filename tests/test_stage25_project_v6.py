# -*- coding: utf-8 -*-
"""Сквозные проверки формата проекта и целостности Этапа 2.5."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.calculation import CalculationProjectionBuilder
from rza_calc.core.methodology import Methodology
from rza_calc.domain import ProjectStructure
from rza_calc.domain.catalog import (
    CatalogId,
    DEFAULT_PROJECT_CATALOG_ID,
    DEFAULT_USER_CATALOG_ID,
    UserCatalog,
    builtin_system_catalog,
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
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RoutePoint,
)
from rza_calc.domain.electrical import (
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineConstructionSegmentId,
    LineKind,
    LogicalLineId,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
    deterministic_id,
)
from rza_calc.io.catalog_snapshot import catalog_snapshots_to_dict
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.project import (
    FORMAT_VERSION,
    ProjectData,
    ProjectFormatError,
    _write_json,
    load_project,
    migrate_project_file,
    restore_project_backup,
    save_project,
)
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.v6.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _connect(
    model: ElectricalModel,
    equipment_id: EquipmentId,
    role: str,
    node_id: ElectricalNodeId,
) -> None:
    model.connect_port(
        model.port_by_role(equipment_id, role).id,
        node_id,
        connection_id=ConnectionId(
            f"connection.v6.{equipment_id.value}.{role}"
        ),
    )


def _project() -> tuple[ProjectData, EquipmentId]:
    model = ElectricalModel.with_builtins("Проект Этапа 2.5")
    first = _node(model, "first")
    second = _node(model, "second")
    third = _node(model, "third")
    source, _ = model.create_equipment(
        "builtin.external_grid",
        "Источник",
        equipment_id=EquipmentId("equipment.v6.source"),
        port_ids_by_role={"terminal": PortId("port.v6.source")},
        voltage_class_by_group={"main": U10},
    )
    _connect(model, source.id, "terminal", first.id)
    model.create_logical_line(
        "ВЛ-10 кВ №5",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        1_500_000,
        logical_line_id=LogicalLineId("line.v6.main"),
        section_equipment_id=EquipmentId("equipment.v6.line"),
        port_ids_by_role={
            "from": PortId("port.v6.line.from"),
            "to": PortId("port.v6.line.to"),
        },
        connection_ids=(
            ConnectionId("connection.v6.line.from"),
            ConnectionId("connection.v6.line.to"),
        ),
        section_properties={
            "conductor_mark": "АС-70",
            "r1_ohm_per_km": 0.42,
            "x1_ohm_per_km": 0.36,
        },
    )
    entry = builtin_system_catalog().get("system.recloser.generic_10kv")
    parameters = entry.instance_parameters()
    recloser, _ = model.create_equipment(
        parameters.type_id,
        "Реклоузер",
        equipment_id=EquipmentId("equipment.v6.recloser"),
        port_ids_by_role={
            "a": PortId("port.v6.recloser.a"),
            "b": PortId("port.v6.recloser.b"),
        },
        **parameters.as_create_kwargs(),
    )
    _connect(model, recloser.id, "a", second.id)
    _connect(model, recloser.id, "b", third.id)
    normal = OperatingState(
        OperatingStateId("state.v6.normal"),
        "Нормальная схема",
        {recloser.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(normal)

    pages = tuple(
        DiagramPage(PageId(f"page.v6.{index}"), f"Страница {index}", order=index)
        for index in range(1, 4)
    )
    representations = [
        GraphicalRepresentation(
            GraphicalRepresentationId(f"representation.v6.node.{index}"),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=second.id,
            x=float(index * 100),
            y=50.0,
            symbol_key="межсхемный_порт",
            route_points=(RoutePoint(0.0, 0.0), RoutePoint(20.0, 0.0)),
        )
        for index, page in enumerate(pages, start=1)
    ]
    representations.append(GraphicalRepresentation(
        GraphicalRepresentationId("representation.v6.recloser"),
        pages[0].id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=recloser.id,
        x=200.0,
        y=100.0,
        symbol_key="recloser",
    ))
    diagram = DiagramDocument.create(
        "Однолинейная схема",
        pages,
        representations,
        document_id=DiagramDocumentId("diagram.main"),
    )
    override = ParameterOverride(
        "rated_current_a",
        630,
        700,
        "Уточнено по паспорту экземпляра",
        "Паспорт №25",
        datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    binding = CatalogBinding(
        recloser.id,
        CatalogEntrySnapshot.from_entry(entry),
        {"rated_current_a": 700},
        {"source_checked": True},
        {"rated_current_a": override},
    )
    snapshots = ProjectCatalogSnapshots.from_bindings((binding,))
    topology = TopologyEngine().compile(model, normal.id)
    adaptation = adapt_to_calculation(model, topology)
    return ProjectData(
        adaptation.network,
        Methodology.load(),
        {"name": model.name},
        ProjectStructure(),
        FORMAT_VERSION,
        model,
        adapter_diagnostics=adaptation.diagnostics,
        diagram=diagram,
        catalog_snapshots=snapshots,
    ), recloser.id


def test_three_real_taps_create_four_main_branches_and_keep_continuation() -> None:
    model = ElectricalModel.with_builtins("Три полноценные отпайки")
    start = _node(model, "tap.start")
    finish = _node(model, "tap.finish")
    branch_ends = tuple(_node(model, f"tap.branch.{index}") for index in range(3))
    line, section, _ = model.create_logical_line(
        "Главная ВЛ",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        4_000,
    )
    current_section_id = section.equipment_id
    tap_nodes: list[ElectricalNodeId] = []
    side_lines = []
    for index, branch_end in enumerate(branch_ends, start=1):
        split, side_line, _, _ = model.create_tap_line(
            current_section_id,
            1_000,
            f"Отпайка {index}",
            LineKind.CABLE,
            branch_end.id,
            500,
        )
        tap_nodes.append(split.tap_node_id)
        side_lines.append(side_line.id)
        current_section_id = split.second_section_id

    main = model.logical_lines[line.id]
    assert len(main.section_equipment_ids) == 4
    assert len(side_lines) == 3
    assert len(set(tap_nodes)) == 3
    assert model._section_endpoint_ids(main.section_equipment_ids[0]) == (
        start.id,
        tap_nodes[0],
    )
    assert model._section_endpoint_ids(main.section_equipment_ids[-1]) == (
        tap_nodes[-1],
        finish.id,
    )
    for index in range(2):
        assert model._section_endpoint_ids(main.section_equipment_ids[index + 1]) == (
            tap_nodes[index],
            tap_nodes[index + 1],
        )
    assert model.validate_integrity() == []


def test_v6_roundtrip_preserves_topology_pages_snapshots_and_stable_ids(tmp_path) -> None:
    project, _ = _project()
    before_projection = CalculationProjectionBuilder().build(
        project.electrical_model,
        OperatingStateId("state.v6.normal"),
    )
    target = tmp_path / "project-v6.json"
    save_project(target, project)
    raw = json.loads(target.read_text(encoding="utf-8"))
    reopened = load_project(target)
    after_projection = CalculationProjectionBuilder().build(
        reopened.electrical_model,
        OperatingStateId("state.v6.normal"),
    )

    assert raw["format_version"] == FORMAT_VERSION == 7
    assert {"diagram", "catalog_snapshots", "migration_journal"} <= set(raw)
    assert diagram_to_dict(reopened.diagram) == diagram_to_dict(project.diagram)
    assert catalog_snapshots_to_dict(
        reopened.catalog_snapshots
    ) == catalog_snapshots_to_dict(project.catalog_snapshots)
    assert before_projection.semantic_fingerprint() == after_projection.semantic_fingerprint()
    shared = ElectricalNodeId("node.v6.second")
    assert len(reopened.diagram.representations_for_node(shared)) == 3

    electrical_ids = reopened.electrical_model._object_id_values()
    diagram_ids = {
        reopened.diagram.id.value,
        *(item.value for item in reopened.diagram.pages),
        *(item.value for item in reopened.diagram.representations),
    }
    calculation_ids = {
        *(item.value for item in after_projection.nodes),
        *(item.value for item in after_projection.branches),
    }
    all_ids = (*electrical_ids, *diagram_ids, *calculation_ids)
    assert len(all_ids) == len(set(all_ids))


def test_v5_to_v6_in_place_migration_creates_backup_journal_and_segments(tmp_path) -> None:
    project, _ = _project()
    target = tmp_path / "legacy-v5.json"
    save_project(target, project)
    raw_v5 = json.loads(target.read_text(encoding="utf-8"))
    raw_v5["format_version"] = 5
    for key in ("diagram", "catalog_snapshots", "migration_journal"):
        raw_v5.pop(key)
    for logical_line in raw_v5["electrical_model"]["logical_lines"]:
        logical_line.pop("feeder_id", None)
    for section in raw_v5["electrical_model"]["line_sections"]:
        section["length_mm"] = sum(
            item["length_mm"] for item in section.pop("construction_segments")
        )
    target.write_text(
        json.dumps(raw_v5, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    source_bytes = target.read_bytes()
    before = load_project(target)
    before_signature = TopologyEngine().compile(
        before.electrical_model,
        OperatingStateId("state.v6.normal"),
    ).semantic_signature()

    backup = migrate_project_file(target)

    assert backup is not None and backup.is_file()
    assert backup.read_bytes() == source_bytes
    migrated_raw = json.loads(target.read_text(encoding="utf-8"))
    assert migrated_raw["format_version"] == 7
    assert all(
        section["construction_segments"]
        and "length_mm" not in section
        for section in migrated_raw["electrical_model"]["line_sections"]
    )
    assert any(item["backup_file"] == backup.name for item in migrated_raw["migration_journal"])
    reopened = load_project(target)
    after_signature = TopologyEngine().compile(
        reopened.electrical_model,
        OperatingStateId("state.v6.normal"),
    ).semantic_signature()
    assert after_signature == before_signature

    migrated_section_id = EquipmentId("equipment.v6.line")
    migrated_section = reopened.electrical_model.line_sections[
        migrated_section_id
    ]
    assert migrated_section.construction_segments[0].properties == {}
    reopened.electrical_model.set_section_override(
        migrated_section_id, "conductor_mark", "АС-95"
    )
    assert reopened.electrical_model.effective_line_construction_segment_properties(
        migrated_section_id,
        migrated_section.construction_segments[0].id,
    )["conductor_mark"] == "АС-95"

    previous_v6 = restore_project_backup(backup, target)
    assert previous_v6 is not None and previous_v6.is_file()
    assert target.read_bytes() == source_bytes
    assert load_project(target).source_format_version == 5


def test_v5_migration_allocates_deterministic_collision_free_auxiliary_ids(
    tmp_path,
) -> None:
    project, _ = _project()
    target = tmp_path / "legacy-v5-id-collisions.json"
    save_project(target, project)
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["format_version"] = 5
    for key in ("diagram", "catalog_snapshots", "migration_journal"):
        raw.pop(key)
    # Настоящий v5 ещё не имел ID корня пользовательского каталога.
    raw["catalogs"].pop("id", None)
    for logical_line in raw["electrical_model"]["logical_lines"]:
        logical_line.pop("feeder_id", None)
    for section in raw["electrical_model"]["line_sections"]:
        section["length_mm"] = sum(
            item["length_mm"] for item in section.pop("construction_segments")
        )

    preferred_node_ids = (
        "diagram.main",
        DEFAULT_USER_CATALOG_ID.value,
        DEFAULT_PROJECT_CATALOG_ID.value,
    )
    id_map: dict[str, str] = {}
    for node, replacement_id in zip(
        raw["electrical_model"]["electrical_nodes"],
        preferred_node_ids,
    ):
        id_map[node["id"]] = replacement_id
        node["id"] = replacement_id
    for connection in raw["electrical_model"]["connections"]:
        connection["electrical_node_id"] = id_map.get(
            connection["electrical_node_id"],
            connection["electrical_node_id"],
        )

    preferred_segment_id = deterministic_id(
        LineConstructionSegmentId,
        "legacy-line-section",
        "equipment.v6.line",
    ).value
    raw["electrical_model"]["operating_states"][0]["id"] = (
        preferred_segment_id
    )
    target.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    first = load_project(target)
    second = load_project(target)
    first_auxiliary = (
        first.diagram.id.value,
        first.user_catalog.id.value,
        first.catalog_snapshots.id.value,
    )
    second_auxiliary = (
        second.diagram.id.value,
        second.user_catalog.id.value,
        second.catalog_snapshots.id.value,
    )

    assert first_auxiliary == second_auxiliary
    assert not set(first_auxiliary) & set(preferred_node_ids)
    assert len(first_auxiliary) == len(set(first_auxiliary))
    assert OperatingStateId(preferred_segment_id) in first.electrical_model.operating_states
    migrated_segment_id = first.electrical_model.line_sections[
        EquipmentId("equipment.v6.line")
    ].construction_segments[0].id.value
    assert migrated_segment_id != preferred_segment_id
    assert any(
        "без коллизий" in step
        for record in first.migration_journal
        for step in record["steps"]
    )


def test_atomic_json_writer_keeps_existing_file_on_validation_error(tmp_path) -> None:
    target = tmp_path / "atomic.json"
    original = b'{"status":"original"}\n'
    target.write_bytes(original)

    with pytest.raises(ProjectFormatError):
        _write_json(target, {"bad": object()})

    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_save_rejects_invalid_migration_journal_before_creating_file(tmp_path) -> None:
    project, _ = _project()
    project.migration_journal = ({"bad": "row"},)
    target = tmp_path / "invalid-journal.json"

    with pytest.raises(ValueError) as error:
        save_project(target, project)

    assert "migration_journal" in str(error.value)
    assert not target.exists()

    project.migration_journal = ({
        "id": "migration.future.invalid",
        "from_version": 99,
        "to_version": 100,
        "applied_at": "2026-08-25T00:00:00Z",
        "steps": ["Несуществующая будущая миграция."],
        "backup_file": "",
    },)
    future_target = tmp_path / "future-journal.json"
    with pytest.raises(ValueError) as future_error:
        save_project(future_target, project)
    assert "новее" in str(future_error.value).lower()
    assert not future_target.exists()


def test_project_rejects_stable_id_collision_between_domain_and_diagram(
    tmp_path,
) -> None:
    project, _ = _project()
    representations = list(project.diagram.representations.values())
    representations[0] = replace(
        representations[0],
        id=GraphicalRepresentationId("equipment.v6.source"),
    )
    project.diagram = DiagramDocument.create(
        project.diagram.name,
        project.diagram.pages.values(),
        representations,
        document_id=project.diagram.id,
    )
    target = tmp_path / "duplicate-id.json"

    with pytest.raises(ValueError) as error:
        save_project(target, project)

    assert "постоянный id" in str(error.value).lower()
    assert not target.exists()


def test_project_rejects_catalog_id_collision_with_electrical_domain(
    tmp_path,
) -> None:
    project, _ = _project()
    project.user_catalog = UserCatalog(
        catalog_id=CatalogId("equipment.v6.source")
    )
    target = tmp_path / "duplicate-catalog-id.json"

    with pytest.raises(ValueError) as error:
        save_project(target, project)

    assert "постоянный id" in str(error.value).lower()
    assert "каталог" in str(error.value).lower()
    assert not target.exists()


def test_project_rejects_orphan_diagram_and_catalog_targets_after_deletion(tmp_path) -> None:
    project, recloser_id = _project()
    equipment = project.electrical_model.equipment[recloser_id]
    project.electrical_model.remove_equipment(recloser_id, cascade=True)

    assert all(port_id not in project.electrical_model.ports for port_id in equipment.port_ids)
    assert all(
        connection.port_id not in equipment.port_ids
        for connection in project.electrical_model.connections.values()
    )
    with pytest.raises(ValueError) as error:
        save_project(tmp_path / "orphan.json", project)
    assert "поврежд" in str(error.value).lower() or "ссыл" in str(error.value).lower()
