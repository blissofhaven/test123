# -*- coding: utf-8 -*-
"""Контрольные тесты Этапа 2.5: линия и история команд."""
from __future__ import annotations

from dataclasses import replace

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.core.impedance import line_impedance
from rza_calc.core.methodology import Methodology
from rza_calc.domain import ProjectStructure
from rza_calc.domain.electrical import (
    ConnectionId,
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    LogicalLineId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.history import (
    CommandHistory,
    CommandHistoryError,
    ElectricalModelMemento,
)
from rza_calc.io.project import (
    FORMAT_VERSION,
    ProjectData,
    load_project,
    save_project,
)
from rza_calc.topology import TopologyEngine


VOLTAGE = VoltageClassId("builtin.voltage.ac.10kv")


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.25.{token}"),
        token,
        declared_voltage_class_id=VOLTAGE,
    )
    model.add_node(node)
    return node


def _line(
    model: ElectricalModel,
    token: str,
    first: ElectricalNode,
    second: ElectricalNode,
    length_mm: int = 1_000,
):
    return model.create_logical_line(
        f"Линия {token}",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        length_mm,
        logical_line_id=LogicalLineId(f"line.25.{token}"),
        section_equipment_id=EquipmentId(f"equipment.25.{token}"),
        port_ids_by_role={
            "from": PortId(f"port.25.{token}.from"),
            "to": PortId(f"port.25.{token}.to"),
        },
        connection_ids=(
            ConnectionId(f"connection.25.{token}.from"),
            ConnectionId(f"connection.25.{token}.to"),
        ),
    )


def _segment(
    token: str,
    kind: LineKind,
    length_mm: int,
    mark: str,
) -> LineConstructionSegment:
    return LineConstructionSegment(
        LineConstructionSegmentId(f"segment.25.{token}"),
        kind,
        length_mm,
        {"conductor_mark": mark},
    )


def test_mixed_construction_segments_do_not_create_electrical_nodes() -> None:
    model = ElectricalModel.with_builtins("Смешанная линия")
    first = _node(model, "mixed.first")
    second = _node(model, "mixed.second")
    _, branch, _ = _line(model, "mixed", first, second, 2_300)
    signature = model.connectivity_signature()
    node_ids = set(model.electrical_nodes)

    segments = (
        _segment("mixed.overhead", LineKind.OVERHEAD, 1_200, "АС-70"),
        _segment("mixed.cable", LineKind.CABLE, 800, "АПвПу2г-95"),
        _segment("mixed.busduct", LineKind.BUSDUCT, 300, "ШМА-630"),
    )
    model.replace_line_construction_segments(branch.equipment_id, segments)

    stored = model.line_sections[branch.equipment_id]
    assert stored.length_mm == 2_300
    assert stored.construction_segments == segments
    assert model.connectivity_signature() == signature
    assert set(model.electrical_nodes) == node_ids
    snapshot = TopologyEngine().compile(model)
    assert len(snapshot.links) == 1
    assert model.effective_line_construction_segment_properties(
        branch.equipment_id, segments[1].id
    )["conductor_mark"] == "АПвПу2г-95"
    with pytest.raises(DomainInvariantError):
        model.set_section_length(branch.equipment_id, 2_500)
    assert model.validate_integrity() == []


def test_split_partitions_and_physically_splits_construction_segment() -> None:
    model = ElectricalModel.with_builtins("Разбиение конструкции")
    first = _node(model, "split.first")
    second = _node(model, "split.second")
    line, branch, _ = _line(model, "split", first, second, 1_000)
    segments = (
        _segment("split.a", LineKind.OVERHEAD, 300, "A"),
        _segment("split.b", LineKind.CABLE, 400, "B"),
        _segment("split.c", LineKind.BUSDUCT, 300, "C"),
    )
    model.replace_line_construction_segments(branch.equipment_id, segments)

    result = model.split_line_section(branch.equipment_id, 500)
    first_branch = model.line_sections[result.first_section_id]
    second_branch = model.line_sections[result.second_section_id]

    assert first_branch.length_mm == 500
    assert second_branch.length_mm == 500
    assert first_branch.construction_segments[0].id == segments[0].id
    assert second_branch.construction_segments[-1].id == segments[-1].id
    assert first_branch.construction_segments[-1].length_mm == 200
    assert second_branch.construction_segments[0].length_mm == 200
    assert first_branch.construction_segments[-1].properties["conductor_mark"] == "B"
    assert second_branch.construction_segments[0].properties["conductor_mark"] == "B"
    split_ids = {
        first_branch.construction_segments[-1].id,
        second_branch.construction_segments[0].id,
    }
    assert segments[1].id not in split_ids
    assert len(split_ids) == 2
    assert model.logical_lines[line.id].section_equipment_ids == (
        result.first_section_id,
        result.second_section_id,
    )
    assert model.validate_integrity() == []


def test_split_on_construction_boundary_preserves_segment_ids() -> None:
    model = ElectricalModel.with_builtins("Разбиение по границе")
    first = _node(model, "boundary.first")
    second = _node(model, "boundary.second")
    _, branch, _ = _line(model, "boundary", first, second, 1_000)
    segments = (
        _segment("boundary.a", LineKind.OVERHEAD, 400, "A"),
        _segment("boundary.b", LineKind.CABLE, 600, "B"),
    )
    model.replace_line_construction_segments(branch.equipment_id, segments)

    result = model.split_line_section(branch.equipment_id, 400)

    assert model.line_sections[
        result.first_section_id
    ].construction_segments == (segments[0],)
    assert model.line_sections[
        result.second_section_id
    ].construction_segments == (segments[1],)
    assert segments[0].id.value not in result.change.added_ids
    assert segments[1].id.value not in result.change.added_ids
    assert segments[0].id.value not in result.change.removed_ids
    assert segments[1].id.value not in result.change.removed_ids
    assert model.validate_integrity() == []


def test_remove_tap_concatenates_different_construction_segments() -> None:
    model = ElectricalModel.with_builtins("Схлопывание смешанной линии")
    first = _node(model, "collapse.first")
    second = _node(model, "collapse.second")
    branch_end = _node(model, "collapse.branch")
    line, branch, _ = _line(model, "collapse", first, second, 1_000)
    split, side_line, _, _ = model.create_tap_line(
        branch.equipment_id,
        400,
        "Боковая линия",
        LineKind.CABLE,
        branch_end.id,
        200,
    )
    left = model.line_sections[split.first_section_id]
    right = model.line_sections[split.second_section_id]
    left_segment = replace(
        left.construction_segments[0],
        line_kind=LineKind.OVERHEAD,
        properties={"conductor_mark": "АС-70"},
    )
    right_segment = replace(
        right.construction_segments[0],
        line_kind=LineKind.CABLE,
        properties={"conductor_mark": "АПвПу-95"},
    )
    model.update_line_construction_segment(split.first_section_id, left_segment)
    model.update_line_construction_segment(split.second_section_id, right_segment)

    merged_id = EquipmentId("equipment.25.collapse.merged")
    model.remove_line_tap(
        line.id,
        split.tap_node_id,
        branch_line_ids=(side_line.id,),
        merged_section_id=merged_id,
        merged_port_ids_by_role={
            "from": PortId("port.25.collapse.merged.from"),
            "to": PortId("port.25.collapse.merged.to"),
        },
        merged_connection_ids=(
            ConnectionId("connection.25.collapse.merged.from"),
            ConnectionId("connection.25.collapse.merged.to"),
        ),
    )

    merged = model.line_sections[merged_id]
    assert merged.length_mm == 1_000
    assert [item.line_kind for item in merged.construction_segments] == [
        LineKind.OVERHEAD,
        LineKind.CABLE,
    ]
    assert [
        item.properties["conductor_mark"]
        for item in merged.construction_segments
    ] == ["АС-70", "АПвПу-95"]
    assert model.validate_integrity() == []


def test_segment_ids_are_removed_with_line_branch() -> None:
    model = ElectricalModel.with_builtins("Удаление конструкции")
    first = _node(model, "remove.first")
    second = _node(model, "remove.second")
    line, branch, _ = _line(model, "remove", first, second)
    segment_id = model.line_sections[branch.equipment_id].construction_segments[0].id

    change = model.remove_logical_line(line.id, cascade=True)

    assert segment_id.value in change.removed_ids
    assert branch.equipment_id not in model.line_sections
    assert segment_id.value not in model._object_id_values()
    assert model.validate_integrity() == []


def test_command_history_undo_redo_tap_restores_exact_domain_state() -> None:
    model = ElectricalModel.with_builtins("История отпайки")
    first = _node(model, "history.first")
    second = _node(model, "history.second")
    branch_end = _node(model, "history.branch")
    _, branch, _ = _line(model, "history", first, second, 1_000)
    history = CommandHistory(model)
    before = ElectricalModelMemento.capture(model)
    revision_before = model.revision

    execution = history.execute(
        "Создание отпайки",
        lambda staged: staged.create_tap_line(
            branch.equipment_id,
            400,
            "Отпайка",
            LineKind.CABLE,
            branch_end.id,
            200,
        ),
    )
    split = execution.result[0]
    after = ElectricalModelMemento.capture(model)
    revision_after_execute = model.revision
    assert branch.equipment_id not in model.equipment
    assert split.tap_node_id in model.electrical_nodes
    assert history.can_undo and not history.can_redo

    history.undo()
    revision_after_undo = model.revision
    restored = ElectricalModelMemento.capture(model)
    assert restored.equipment == before.equipment
    assert restored.ports == before.ports
    assert restored.electrical_nodes == before.electrical_nodes
    assert restored.connections == before.connections
    assert restored.logical_lines == before.logical_lines
    assert restored.line_sections == before.line_sections
    assert branch.equipment_id in model.equipment

    history.redo()
    revision_after_redo = model.revision
    repeated = ElectricalModelMemento.capture(model)
    assert repeated.equipment == after.equipment
    assert repeated.ports == after.ports
    assert repeated.electrical_nodes == after.electrical_nodes
    assert repeated.connections == after.connections
    assert repeated.logical_lines == after.logical_lines
    assert repeated.line_sections == after.line_sections
    assert (
        revision_before
        < revision_after_execute
        < revision_after_undo
        < revision_after_redo
    )
    assert [item.message for item in history.journal] == [
        "Выполнено: Создание отпайки",
        "Отменено: Создание отпайки",
        "Повторено: Создание отпайки",
    ]
    assert model.validate_integrity() == []


def test_failed_history_command_is_atomic_and_not_journaled() -> None:
    model = ElectricalModel.with_builtins("Ошибка команды")
    history = CommandHistory(model)
    before = ElectricalModelMemento.capture(model)

    def broken(staged: ElectricalModel) -> None:
        staged.add_node(ElectricalNode(ElectricalNodeId("node.25.temporary")))
        raise DomainInvariantError("Проверочная ошибка")

    with pytest.raises(DomainInvariantError):
        history.execute("Ошибочная команда", broken)

    after = ElectricalModelMemento.capture(model)
    assert after.revision == before.revision
    assert after.electrical_nodes == before.electrical_nodes
    assert history.journal == ()
    assert not history.can_undo and not history.can_redo


def test_history_does_not_overwrite_external_mutation() -> None:
    model = ElectricalModel.with_builtins("Конфликт истории")
    history = CommandHistory(model)
    history.execute(
        "Добавление узла",
        lambda staged: staged.add_node(
            ElectricalNode(ElectricalNodeId("node.25.history"))
        ),
    )
    external = ElectricalNode(ElectricalNodeId("node.25.external"))
    model.add_node(external)

    with pytest.raises(CommandHistoryError):
        history.undo()

    assert external.id in model.electrical_nodes
    assert ElectricalNodeId("node.25.history") in model.electrical_nodes


def test_recloser_passport_and_protection_data_do_not_change_topology() -> None:
    model = ElectricalModel.with_builtins("Паспорт реклоузера")
    first = _node(model, "recloser.first")
    second = _node(model, "recloser.second")
    recloser, _ = model.create_equipment(
        "builtin.recloser",
        "Реклоузер",
        equipment_id=EquipmentId("equipment.25.recloser"),
        port_ids_by_role={
            "a": PortId("port.25.recloser.a"),
            "b": PortId("port.25.recloser.b"),
        },
        voltage_class_by_group={"main": VOLTAGE},
        properties={"rated_current_a": 630},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(
        model.port_by_role(recloser.id, "a").id,
        first.id,
        connection_id=ConnectionId("connection.25.recloser.a"),
    )
    model.connect_port(
        model.port_by_role(recloser.id, "b").id,
        second.id,
        connection_id=ConnectionId("connection.25.recloser.b"),
    )
    before = TopologyEngine().compile(model).semantic_signature()

    model.update_equipment_properties(recloser.id, {
        "thermal_short_time_current_a": 20_000,
        "thermal_duration_s": 3.0,
        "dynamic_peak_current_a": 51_000,
        "protection_settings": {"mtz_enabled": True},
    })
    after = TopologyEngine().compile(model).semantic_signature()

    assert after == before
    assert model.validate_integrity() == []


def test_incomplete_mixed_line_is_visible_but_blocks_legacy_calculation() -> None:
    model = ElectricalModel.with_builtins("Неполная составная ветвь")
    first = _node(model, "legacy.mixed.first")
    second = _node(model, "legacy.mixed.second")
    _, branch, _ = _line(
        model, "legacy.mixed", first, second, 2_000_000
    )
    model.replace_line_construction_segments(branch.equipment_id, (
        LineConstructionSegment(
            LineConstructionSegmentId("segment.25.legacy.mixed.overhead"),
            LineKind.OVERHEAD,
            1_000_000,
            {
                "conductor_mark": "АС-70",
                "cross_section_mm2": 70,
                "material": "Al",
            },
        ),
        LineConstructionSegment(
            LineConstructionSegmentId("segment.25.legacy.mixed.cable"),
            LineKind.CABLE,
            1_000_000,
            {
                "conductor_mark": "АПвПу2г-240",
                "cross_section_mm2": 240,
                "material": "Cu",
            },
        ),
    ))

    adaptation = adapt_to_calculation(model)
    legacy_branch = next(iter(adaptation.network.branches.values()))
    diagnostic = next(
        item for item in adaptation.diagnostics
        if item.code == "composite_line_legacy_calculation_blocked"
    )

    assert diagnostic.severity == "error"
    assert legacy_branch.calculation_block_reason is not None
    assert any(
        "расчёт заблокирован" in problem
        for problem in adaptation.network.validate()
    )
    with pytest.raises(ValueError):
        line_impedance(legacy_branch, Methodology.load())


def test_busduct_without_explicit_impedance_blocks_legacy_calculation() -> None:
    model = ElectricalModel.with_builtins("Шинопровод без сопротивлений")
    first = _node(model, "legacy.busduct.first")
    second = _node(model, "legacy.busduct.second")
    _, branch, _ = model.create_logical_line(
        "Шинопровод",
        LineKind.BUSDUCT,
        first.id,
        second.id,
        500_000,
        logical_line_id=LogicalLineId("line.25.legacy.busduct"),
        section_equipment_id=EquipmentId("equipment.25.legacy.busduct"),
        port_ids_by_role={
            "from": PortId("port.25.legacy.busduct.from"),
            "to": PortId("port.25.legacy.busduct.to"),
        },
        connection_ids=(
            ConnectionId("connection.25.legacy.busduct.from"),
            ConnectionId("connection.25.legacy.busduct.to"),
        ),
        section_properties={
            "conductor_mark": "ШМА-630",
            "cross_section_mm2": 630,
            "material": "Al",
        },
    )

    adaptation = adapt_to_calculation(model)
    legacy_branch = next(iter(adaptation.network.branches.values()))
    diagnostic = next(
        item for item in adaptation.diagnostics
        if item.code == "busduct_legacy_calculation_blocked"
    )

    assert diagnostic.severity == "error"
    assert legacy_branch.calculation_block_reason is not None
    assert "шинопровода" in legacy_branch.calculation_block_reason
    assert any(
        "расчёт заблокирован" in problem
        for problem in adaptation.network.validate()
    )
    with pytest.raises(ValueError):
        line_impedance(legacy_branch, Methodology.load())

    model.set_section_override(
        branch.equipment_id, "r1_ohm_per_km", 0.08
    )
    model.set_section_override(
        branch.equipment_id, "x1_ohm_per_km", 0.06
    )
    entered = adapt_to_calculation(model)
    entered_branch = next(iter(entered.network.branches.values()))
    assert entered_branch.calculation_block_reason is not None
    with pytest.raises(ValueError):
        line_impedance(entered_branch, Methodology.load())

    # Entering numbers is separate from verifying their source.
    segment = model.line_sections[branch.equipment_id].construction_segments[0]
    model.update_line_construction_segment(branch.equipment_id, replace(
        segment,
        impedance_confirmation=DataConfirmation.CONFIRMED,
        extensions={"rza_calc.parameter_provenance": {
            key: {"confirmation": "confirmed", "source": "Паспорт ШМА-630", "origin": "manual"}
            for key in ("r1_ohm_per_km", "x1_ohm_per_km")
        }},
    ))
    confirmed = adapt_to_calculation(model)
    confirmed_branch = next(iter(confirmed.network.branches.values()))

    assert confirmed_branch.calculation_block_reason is None
    assert not any(
        item.code == "busduct_legacy_calculation_blocked"
        for item in confirmed.diagnostics
    )
    impedance, _ = line_impedance(confirmed_branch, Methodology.load())
    assert impedance == complex(0.04, 0.03)


def test_legacy_calculation_block_does_not_block_canonical_project_roundtrip(
    tmp_path,
) -> None:
    model = ElectricalModel.with_builtins("Сохраняемый неподтверждённый проект")
    first = _node(model, "legacy.save.first")
    second = _node(model, "legacy.save.second")
    _, branch, _ = _line(
        model, "legacy.save", first, second, 2_000_000
    )
    segments = (
        LineConstructionSegment(
            LineConstructionSegmentId("segment.25.legacy.save.overhead"),
            LineKind.OVERHEAD,
            1_000_000,
            {"conductor_mark": "АС-70", "cross_section_mm2": 70},
        ),
        LineConstructionSegment(
            LineConstructionSegmentId("segment.25.legacy.save.cable"),
            LineKind.CABLE,
            1_000_000,
            {"conductor_mark": "АПвПу2г-95", "cross_section_mm2": 95},
        ),
    )
    model.replace_line_construction_segments(branch.equipment_id, segments)
    adaptation = adapt_to_calculation(model)
    project = ProjectData(
        adaptation.network,
        Methodology.load(),
        {"name": model.name},
        ProjectStructure(),
        FORMAT_VERSION,
        model,
        adapter_diagnostics=adaptation.diagnostics,
    )
    target = tmp_path / "blocked-calculation-canonical-project.json"

    save_project(target, project)
    reopened = load_project(target)

    reopened_segments = reopened.electrical_model.line_sections[
        branch.equipment_id
    ].construction_segments
    assert reopened_segments == segments
    assert any(
        item.severity == "error"
        and item.code == "composite_line_legacy_calculation_blocked"
        for item in reopened.adapter_diagnostics
    )
