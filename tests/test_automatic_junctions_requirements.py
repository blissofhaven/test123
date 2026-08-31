# -*- coding: utf-8 -*-
"""Точечные пробелы обязательных сценариев автоматических узлов.

Файл намеренно проверяет только уже существующие публичные команды и
защитные инварианты. Производственная модель ради тестов не изменяется.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog

from rza_calc.adapters import adapt_to_calculation
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.electrical import (
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineKind,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import (
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.gui.editor_scene import EditorCanvas
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")
U35 = VoltageClassId("builtin.voltage.ac.35kv")
_APP: QApplication | None = None


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _project_controller(token: str) -> ProjectEditorController:
    page = DiagramPage(PageId(f"page.requirements.{token}"), "Основная схема")
    project = _Project(
        ElectricalModel.with_builtins(f"Проверка требований: {token}"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,),
            document_id=DiagramDocumentId(f"diagram.requirements.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )
    return ProjectEditorController(project)


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.requirements.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _line_model(token: str = "line"):
    model = ElectricalModel.with_builtins(f"Проверка линии: {token}")
    first = _node(model, f"{token}.first")
    second = _node(model, f"{token}.second")
    line, section, _ = model.create_logical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        10_000_000,
        voltage_class_id=U10,
        inherited_properties={
            "r1_ohm_per_km": 0.4,
            "x1_ohm_per_km": 0.3,
        },
    )
    return model, first, second, line, section


def test_requirement_08_tap_is_a_node_and_line_sections_not_equipment() -> None:
    model, _, _, _, section = _line_model("tap-not-equipment")
    branch_end = _node(model, "tap-not-equipment.branch")

    split, branch, branch_section, _ = model.create_tap_line(
        section.equipment_id,
        4_000_000,
        "Отпайка",
        LineKind.CABLE,
        branch_end.id,
        750_000,
    )

    assert split.tap_node_id in model.electrical_nodes
    assert model.electrical_nodes[split.tap_node_id].extensions[
        "junction_kind"
    ] == "line_tap"
    assert branch.id in model.logical_lines
    assert branch_section.equipment_id in model.line_sections
    assert set(model.equipment) == set(model.line_sections)
    assert all(
        "tap" not in equipment.type_id.value.casefold()
        and "отпай" not in equipment.type_id.value.casefold()
        for equipment in model.equipment.values()
    )


def test_requirements_11_and_31_moving_crossing_route_creates_no_node_or_fingerprint_change() -> None:
    controller = _project_controller("crossing-move")
    first_a = controller.add_electrical_node("A1", x=0.0, y=0.0, voltage_class_id=U10)
    first_b = controller.add_electrical_node("A2", x=200.0, y=0.0, voltage_class_id=U10)
    second_a = controller.add_electrical_node("B1", x=100.0, y=-100.0, voltage_class_id=U10)
    second_b = controller.add_electrical_node("B2", x=100.0, y=100.0, voltage_class_id=U10)
    horizontal = controller.create_physical_line(
        "Горизонтальная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(first_a.node_id),
        NodeTarget(first_b.node_id),
        physical=PhysicalLineInput(1_000_000, DataConfirmation.CONFIRMED),
        route_waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.requirements.h.1"), 0.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.requirements.h.2"), 200.0, 0.0),
        ),
    )
    controller.create_physical_line(
        "Вертикальная КЛ",
        LineKind.CABLE,
        NodeTarget(second_a.node_id),
        NodeTarget(second_b.node_id),
        physical=PhysicalLineInput(1_000_000, DataConfirmation.CONFIRMED),
        route_waypoints=(
            RouteWaypoint(RouteWaypointId("waypoint.requirements.v.1"), 100.0, -100.0),
            RouteWaypoint(RouteWaypointId("waypoint.requirements.v.2"), 100.0, 100.0),
        ),
    )
    fingerprint_before = electrical_model_fingerprint(controller.model)
    node_ids_before = set(controller.model.electrical_nodes)
    connection_ids_before = set(controller.model.connections)

    controller.reroute_diagram_route(
        horizontal.route_id,
        (
            RouteWaypoint(RouteWaypointId("waypoint.requirements.moved.1"), 0.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.requirements.moved.2"), 80.0, 0.0),
            RouteWaypoint(RouteWaypointId("waypoint.requirements.moved.3"), 80.0, 40.0),
            RouteWaypoint(RouteWaypointId("waypoint.requirements.moved.4"), 200.0, 40.0),
        ),
    )

    assert set(controller.model.electrical_nodes) == node_ids_before
    assert set(controller.model.connections) == connection_ids_before
    assert electrical_model_fingerprint(controller.model) == fingerprint_before
    assert not TopologyEngine().compile(controller.model).has_path(
        first_a.node_id, second_a.node_id
    )


def test_requirement_20_transformer_drop_on_line_creates_branch_not_inline_insertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app()
    controller = _project_controller("transformer-drop")
    start = controller.add_electrical_node("Начало", x=0.0, y=0.0, voltage_class_id=U10)
    finish = controller.add_electrical_node("Конец", x=400.0, y=0.0, voltage_class_id=U10)
    main = controller.create_physical_line(
        "Основная ВЛ",
        LineKind.OVERHEAD,
        NodeTarget(start.node_id),
        NodeTarget(finish.node_id),
        physical=PhysicalLineInput(10_000_000, DataConfirmation.CONFIRMED),
    )
    canvas = EditorCanvas(controller)
    monkeypatch.setattr(
        canvas,
        "_ask_physical_split_offset",
        lambda *args, **kwargs: (True, 4_000_000),
    )
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda *args, **kwargs: (args[3][0], True),
    )

    canvas._add_equipment(
        {
            "target_kind": "equipment",
            "type_id": "builtin.transformer_2w",
            "type_version": 1,
            "name": "Т-1",
        },
        200.0,
        0.0,
    )

    transformer = next(
        item
        for item in controller.model.equipment.values()
        if item.type_id.value == "builtin.transformer_2w"
    )
    tap_connection = next(
        item
        for port_id in transformer.port_ids
        if (item := controller.model.connection_for_port(port_id)) is not None
    )
    main_line = controller.model.logical_lines[main.logical_line_id]
    tap_node = controller.model.electrical_nodes[tap_connection.electrical_node_id]
    assert len(main_line.section_equipment_ids) == 2
    assert len(controller.model.logical_lines) == 1
    assert tap_node.extensions["junction_kind"] == "line_tap"
    assert sum(
        connection.electrical_node_id == tap_node.id
        for connection in controller.model.connections.values()
    ) == 3
    assert transformer.extensions["placement_origin"] == "automatic_branch_attachment"
    assert "line_insertion" not in transformer.extensions
    assert sum(
        controller.model.connection_for_port(port_id) is not None
        for port_id in transformer.port_ids
    ) == 1
    canvas.close()


@pytest.mark.parametrize("mismatch", ("line_kind", "voltage_class"))
def test_requirement_27_different_line_kind_or_voltage_cannot_be_merged(
    mismatch: str,
) -> None:
    model, _, _, _, section = _line_model(f"merge-{mismatch}")
    inserted = model.insert_series_equipment_in_line(
        section.equipment_id,
        4_000_000,
        "builtin.circuit_breaker",
        "QF-1",
    )
    right = model.logical_lines[inserted.right_logical_line_id]
    if mismatch == "line_kind":
        incompatible = replace(right, line_kind=LineKind.CABLE)
    else:
        incompatible = replace(right, voltage_class_id=U35)
    # Имитируется корректно распознанная несовместимость двух половин,
    # например после импорта или независимого редактирования данных линии.
    model._logical_lines[right.id] = incompatible
    fingerprint_before = electrical_model_fingerprint(model)
    revision_before = model.revision

    with pytest.raises(DomainInvariantError, match="несовместимые общие параметры"):
        model.remove_series_equipment_from_line(inserted.equipment_id)

    assert model.revision == revision_before
    assert electrical_model_fingerprint(model) == fingerprint_before
    assert inserted.equipment_id in model.equipment


def test_requirement_30_electrical_tap_operation_changes_fingerprint() -> None:
    model, _, _, _, section = _line_model("fingerprint-change")
    branch_end = _node(model, "fingerprint-change.branch")
    fingerprint_before = electrical_model_fingerprint(model)

    model.create_tap_line(
        section.equipment_id,
        4_000_000,
        "Отпайка",
        LineKind.CABLE,
        branch_end.id,
        500_000,
    )

    assert electrical_model_fingerprint(model) != fingerprint_before


def test_requirement_32_native_model_adapts_without_compat_equipment() -> None:
    model, _, _, _, section = _line_model("native-without-compat")
    fingerprint_before = electrical_model_fingerprint(model)

    result = adapt_to_calculation(model)

    assert model.validate_integrity() == []
    assert section.equipment_id.value in result.trace.domain_equipment_to_legacy
    assert all(
        not definition.id.value.startswith("compat.rza_calc.")
        for definition in model.equipment_types.values()
    )
    assert all(
        not equipment.type_id.value.startswith("compat.rza_calc.")
        for equipment in model.equipment.values()
    )
    assert electrical_model_fingerprint(model) == fingerprint_before
