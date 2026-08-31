# -*- coding: utf-8 -*-
"""Контрольные тесты изолированных foundation-моделей Этапа 2.5."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from rza_calc.calculation import (
    CalculationBranchKind,
    CalculationNodeKind,
    CalculationProjectionBuilder,
    SequenceImpedance,
)
from rza_calc.domain.catalog import (
    CatalogEntryId,
    CatalogId,
    CatalogOrigin,
    DEFAULT_PROJECT_CATALOG_ID,
    DEFAULT_SYSTEM_CATALOG_ID,
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
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.io.catalog_snapshot import (
    CatalogSnapshotsFormatError,
    catalog_snapshots_from_dict,
    catalog_snapshots_to_dict,
)
from rza_calc.io.catalog import user_catalog_from_dict, user_catalog_to_dict
from rza_calc.io.diagram import DiagramFormatError, diagram_from_dict, diagram_to_dict


U110 = VoltageClassId("builtin.voltage.ac.110kv")
U35 = VoltageClassId("builtin.voltage.ac.35kv")
U10 = VoltageClassId("builtin.voltage.ac.10kv")
U04 = VoltageClassId("builtin.voltage.ac.0_4kv")


def _node(model: ElectricalModel, token: str, voltage: VoltageClassId) -> ElectricalNodeId:
    node_id = ElectricalNodeId("node." + token)
    model.add_node(ElectricalNode(node_id, token, declared_voltage_class_id=voltage))
    return node_id


def _equipment(
    model: ElectricalModel,
    type_id: str,
    token: str,
    roles_to_nodes: dict[str, ElectricalNodeId],
    *,
    voltages: dict[str, VoltageClassId] | None = None,
    properties: dict | None = None,
    normal_position: SwitchPosition | None = None,
) -> EquipmentId:
    equipment_id = EquipmentId("equipment." + token)
    port_ids = {role: PortId(f"port.{token}.{role}") for role in roles_to_nodes}
    equipment, _ = model.create_equipment(
        type_id,
        token,
        equipment_id=equipment_id,
        port_ids_by_role=port_ids,
        voltage_class_by_group=voltages or {},
        properties=properties or {},
        normal_position=normal_position,
    )
    for role, node_id in roles_to_nodes.items():
        model.connect_port(
            port_ids[role],
            node_id,
            connection_id=ConnectionId(f"connection.{token}.{role}"),
        )
    return equipment.id


def test_projection_is_deterministic_and_switch_state_controls_derived_branch() -> None:
    model = ElectricalModel.with_builtins("Проекция")
    source_bus = _node(model, "source", U10)
    middle = _node(model, "middle", U10)
    remote = _node(model, "remote", U10)
    source = _equipment(
        model,
        "builtin.external_grid",
        "source",
        {"terminal": source_bus},
        voltages={"main": U10},
    )
    line = _equipment(
        model,
        "builtin.line",
        "line",
        {"from": source_bus, "to": middle},
        voltages={"main": U10},
        properties={"r1_ohm": 0.2, "x1_ohm": 0.4},
    )
    recloser = _equipment(
        model,
        "builtin.recloser",
        "recloser",
        {"a": middle, "b": remote},
        voltages={"main": U10},
        properties={"rated_voltage_v": 10_000, "rated_current_a": 630},
        normal_position=SwitchPosition.CLOSED,
    )
    load = _equipment(
        model,
        "builtin.load",
        "load",
        {"terminal": remote},
        voltages={"main": U10},
    )
    opened = OperatingState(
        OperatingStateId("state.open"),
        "Реклоузер отключён",
        {recloser: SwitchPosition.OPEN},
    )
    model.add_operating_state(opened)

    builder = CalculationProjectionBuilder()
    first = builder.build(model, opened.id)
    second = builder.build(model, opened.id)

    assert first.semantic_fingerprint() == second.semantic_fingerprint()
    assert tuple(first.nodes) == tuple(second.nodes)
    assert tuple(first.branches) == tuple(second.branches)
    assert len(first.branches_for_equipment(source)) == 1
    assert first.branches_for_equipment(line)[0].kind is CalculationBranchKind.LINE
    assert first.branches_for_equipment(line)[0].sequence_impedance.r1_ohm == pytest.approx(0.2)
    assert first.branches_for_equipment(recloser)[0].kind is CalculationBranchKind.RECLOSER
    assert first.branches_for_equipment(recloser)[0].active is False
    assert first.branches_for_equipment(load)[0].kind is CalculationBranchKind.SHUNT
    assert any(
        item.kind is CalculationNodeKind.INTERNAL_EMF
        and item.owner_equipment_id == source
        for item in first.nodes.values()
    )
    assert any(
        item.kind is CalculationNodeKind.GROUND
        and item.owner_equipment_id == load
        for item in first.nodes.values()
    )


def test_transformer_3w_projects_to_three_branches_and_one_internal_star() -> None:
    model = ElectricalModel.with_builtins("Трёхобмоточный трансформатор")
    hv = _node(model, "hv", U110)
    mv = _node(model, "mv", U35)
    lv = _node(model, "lv", U10)
    transformer = _equipment(
        model,
        "builtin.transformer_3w",
        "t3w",
        {"hv": hv, "mv": mv, "lv": lv},
        voltages={"hv": U110, "mv": U35, "lv": U10},
        properties={
            "hv_r1_ohm": 1.0,
            "mv_r1_ohm": 2.0,
            "lv_r1_ohm": 3.0,
        },
    )

    projection = CalculationProjectionBuilder().build(model)
    branches = projection.branches_for_equipment(transformer)
    stars = [
        item for item in projection.nodes.values()
        if item.kind is CalculationNodeKind.TRANSFORMER_STAR
        and item.owner_equipment_id == transformer
    ]

    assert len(branches) == 3
    assert {item.role for item in branches} == {"hv", "mv", "lv"}
    assert all(item.kind is CalculationBranchKind.TRANSFORMER_WINDING for item in branches)
    assert len(stars) == 1
    assert {item.to_node_id for item in branches} == {stars[0].id}
    assert {
        item.role: item.sequence_impedance.r1_ohm for item in branches
    } == {"hv": 1.0, "mv": 2.0, "lv": 3.0}


def test_projection_reads_ordered_construction_segments_without_extra_nodes() -> None:
    model = ElectricalModel.with_builtins("Конструктивные участки")
    start = _node(model, "construction_start", U10)
    finish = _node(model, "construction_finish", U10)
    segments = (
        LineConstructionSegment(
            LineConstructionSegmentId("segment.overhead"),
            LineKind.OVERHEAD,
            1_200_000,
            {"conductor_mark": "АС-70"},
        ),
        LineConstructionSegment(
            LineConstructionSegmentId("segment.cable"),
            LineKind.CABLE,
            300_000,
            {"conductor_mark": "АПвПу2г-95"},
        ),
    )
    _, section, _ = model.create_logical_line(
        "ВЛ с кабельной вставкой",
        LineKind.OVERHEAD,
        start,
        finish,
        1_500_000,
        construction_segments=segments,
    )
    before_nodes = tuple(model.electrical_nodes)

    projection = CalculationProjectionBuilder().build(model)
    branch = projection.branches_for_equipment(section.equipment_id)[0]

    assert tuple(model.electrical_nodes) == before_nodes
    assert len(projection.electrical_node_map) == 2
    assert [item.source_id for item in branch.construction_segments] == [
        "segment.overhead",
        "segment.cable",
    ]
    assert [item.length_mm for item in branch.construction_segments] == [
        1_200_000,
        300_000,
    ]
    assert [item.line_kind for item in branch.construction_segments] == [
        "overhead",
        "cable",
    ]
    assert branch.construction_segments[1].properties["conductor_mark"] == "АПвПу2г-95"


def test_sequence_and_parameter_override_are_typed_future_containers() -> None:
    sequence = SequenceImpedance(r1_ohm=0.1, x1_ohm=0.2, r0_ohm=0.8, x0_ohm=1.4)
    override = ParameterOverride(
        "r0_ohm",
        0.8,
        0.75,
        "Уточнено по протоколу измерений",
        "Протокол №12",
        datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    assert sequence.x0_ohm == pytest.approx(1.4)
    assert override.manual is True
    assert override.override_value == pytest.approx(0.75)


def test_three_pages_share_one_node_and_coordinates_do_not_change_topology() -> None:
    model = ElectricalModel.with_builtins("Страницы")
    node_id = _node(model, "shared", U10)
    pages = tuple(
        DiagramPage(PageId(f"page.{index}"), f"Страница {index}", order=index)
        for index in range(1, 4)
    )
    representations = tuple(
        GraphicalRepresentation(
            GraphicalRepresentationId(f"representation.{index}"),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_id,
            x=10.0 * index,
            y=20.0,
            symbol_key="межсхемный_узел",
            route_points=(RoutePoint(1.0, 2.0), RoutePoint(3.0, 4.0)),
        )
        for index, page in enumerate(pages, start=1)
    )
    document = DiagramDocument.create(
        "Общая схема",
        pages,
        representations,
        document_id=DiagramDocumentId("diagram.main"),
    )
    document.require_valid_targets(model)
    before = model.connectivity_signature()
    moved = document.moved_representation(
        GraphicalRepresentationId("representation.2"), 999.0, -100.0
    )

    assert len(document.representations_for_node(node_id)) == 3
    assert {item.page_id for item in document.representations_for_node(node_id)} == {
        item.id for item in pages
    }
    assert model.connectivity_signature() == before
    assert moved.revision == document.revision + 1
    assert moved.representations[GraphicalRepresentationId("representation.2")].x == 999.0
    assert document.representations[GraphicalRepresentationId("representation.2")].x != 999.0

    raw = diagram_to_dict(moved)
    restored = diagram_from_dict(raw, model)
    assert diagram_to_dict(restored) == raw
    damaged = dict(raw)
    damaged["unknown"] = True
    with pytest.raises(DiagramFormatError):
        diagram_from_dict(damaged)


def test_catalog_snapshot_is_full_immutable_and_isolated_from_catalog_mutation() -> None:
    system_entry = builtin_system_catalog().get("system.recloser.generic_10kv")
    user_entry = replace(
        system_entry,
        id=CatalogEntryId("user.recloser.test"),
        origin=CatalogOrigin.USER,
        source="Паспорт пользователя",
    )
    catalog = UserCatalog((user_entry,))
    snapshot = CatalogEntrySnapshot.from_entry(
        user_entry,
        source_catalog_id=catalog.id,
    )
    changed_entry = replace(
        user_entry,
        entry_version=2,
        properties={**dict(user_entry.properties), "rated_current_a": 800},
    )
    catalog.replace(changed_entry)

    assert snapshot.entry_version == 1
    assert snapshot.source_catalog_id == catalog.id
    assert snapshot.properties["rated_current_a"] == 630
    assert catalog.get(user_entry.id).properties["rated_current_a"] == 800


def test_catalog_binding_codec_preserves_snapshot_overrides_and_validates_target() -> None:
    model = ElectricalModel.with_builtins("Каталожный снимок")
    first = _node(model, "catalog_a", U10)
    second = _node(model, "catalog_b", U10)
    entry = builtin_system_catalog().get("system.recloser.generic_10kv")
    params = entry.instance_parameters(property_overrides={"rated_current_a": 700})
    equipment_id = EquipmentId("equipment.catalog_recloser")
    equipment, _ = model.create_equipment(
        params.type_id,
        "Реклоузер из каталога",
        equipment_id=equipment_id,
        port_ids_by_role={"a": PortId("port.catalog.a"), "b": PortId("port.catalog.b")},
        **params.as_create_kwargs(),
    )
    model.connect_port(PortId("port.catalog.a"), first, connection_id=ConnectionId("connection.catalog.a"))
    model.connect_port(PortId("port.catalog.b"), second, connection_id=ConnectionId("connection.catalog.b"))
    override = ParameterOverride(
        "rated_current_a",
        630,
        700,
        "Уточнение экземпляра",
        "Паспорт аппарата",
        datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    )
    binding = CatalogBinding(
        equipment.id,
        CatalogEntrySnapshot.from_entry(entry),
        {"rated_current_a": 700},
        {"verified": True},
        {"rated_current_a": override},
    )
    snapshots = ProjectCatalogSnapshots.from_bindings((binding,))
    assert snapshots.validate_targets(model) == ()
    assert binding.effective_properties["rated_current_a"] == 700

    raw = catalog_snapshots_to_dict(snapshots)
    restored = catalog_snapshots_from_dict(raw, model)
    assert catalog_snapshots_to_dict(restored) == raw
    assert restored.bindings[equipment.id].entry.source == entry.source
    assert restored.bindings[equipment.id].parameter_overrides[
        "rated_current_a"
    ].reason == "Уточнение экземпляра"

    damaged = dict(raw)
    damaged["bindings"] = [dict(raw["bindings"][0])]
    damaged["bindings"][0]["equipment_id"] = "equipment.missing"
    with pytest.raises(CatalogSnapshotsFormatError):
        catalog_snapshots_from_dict(damaged, model)


def test_catalog_ids_roundtrip_and_old_json_gets_stable_default_provenance() -> None:
    system_catalog = builtin_system_catalog()
    assert system_catalog.id == DEFAULT_SYSTEM_CATALOG_ID
    assert isinstance(system_catalog.id, CatalogId)

    user_id = CatalogId("catalog.user.site-a")
    user_catalog = UserCatalog(catalog_id=user_id)
    user_raw = user_catalog_to_dict(user_catalog)
    assert user_raw["id"] == user_id.value
    assert user_catalog_from_dict(user_raw).id == user_id

    legacy_user_raw = deepcopy(user_raw)
    legacy_user_raw.pop("id")
    first_legacy_user = user_catalog_from_dict(legacy_user_raw)
    second_legacy_user = user_catalog_from_dict(legacy_user_raw)
    assert first_legacy_user.id == second_legacy_user.id == DEFAULT_USER_CATALOG_ID
    assert user_catalog_to_dict(first_legacy_user)["id"] == DEFAULT_USER_CATALOG_ID.value

    source_catalog_id = CatalogId("catalog.system.release-2026")
    snapshot = CatalogEntrySnapshot.from_entry(
        system_catalog.get("system.recloser.generic_10kv"),
        source_catalog_id=source_catalog_id,
    )
    binding = CatalogBinding(EquipmentId("equipment.catalog-id"), snapshot)
    project_catalog_id = CatalogId("catalog.project.site-a")
    snapshots = ProjectCatalogSnapshots.from_bindings(
        (binding,),
        catalog_id=project_catalog_id,
    )
    raw = catalog_snapshots_to_dict(snapshots)
    assert raw["id"] == project_catalog_id.value
    assert raw["bindings"][0]["entry"]["source_catalog_id"] == source_catalog_id.value

    restored = catalog_snapshots_from_dict(raw)
    restored_binding = restored.bindings[binding.equipment_id]
    assert restored.id == project_catalog_id
    assert restored_binding.source_catalog_id == source_catalog_id

    legacy_raw = deepcopy(raw)
    legacy_raw.pop("id")
    legacy_raw["bindings"][0]["entry"].pop("source_catalog_id")
    legacy = catalog_snapshots_from_dict(legacy_raw)
    assert legacy.id == DEFAULT_PROJECT_CATALOG_ID
    assert legacy.bindings[binding.equipment_id].source_catalog_id == DEFAULT_SYSTEM_CATALOG_ID
    rewritten = catalog_snapshots_to_dict(legacy)
    assert rewritten["id"] == DEFAULT_PROJECT_CATALOG_ID.value
    assert (
        rewritten["bindings"][0]["entry"]["source_catalog_id"]
        == DEFAULT_SYSTEM_CATALOG_ID.value
    )

    with pytest.raises(AttributeError):
        user_catalog.id = CatalogId("catalog.user.changed")
    with pytest.raises(AttributeError):
        user_catalog._id = CatalogId("catalog.user.changed-private")
    with pytest.raises(AttributeError):
        snapshots.id = CatalogId("catalog.project.changed")
