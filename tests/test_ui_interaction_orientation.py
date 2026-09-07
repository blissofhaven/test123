# -*- coding: utf-8 -*-
"""Контракты дискретной ориентации без зависимости от Qt."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

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
    EquipmentTypeId,
    LineKind,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import (
    EditorCommandError,
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.editor.orientation import (
    OrientationMode,
    QuarterTurn,
    normalize_quarter_turn,
    port_anchor_by_id,
)
from rza_calc.editor.orthogonal_routing import RouteDirection
from rza_calc.editor.state import (
    EditorMode,
    GRAPHICS_EXTENSION_KEY,
    RepresentationGraphics,
    orientation_mode_for_representation,
)
from rza_calc.editor.validation import (
    ProjectDiagnosticSeverity,
    ProjectValidationService,
)
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict
from rza_calc.io.project import load_project, save_project


U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller(token: str = "orientation") -> ProjectEditorController:
    page = DiagramPage(PageId(f"page.{token}"), "Основная схема")
    return ProjectEditorController(
        _Project(
            ElectricalModel.with_builtins("Ориентация"),
            DiagramDocument.create(
                "Однолинейная схема",
                (page,),
                document_id=DiagramDocumentId(f"diagram.{token}"),
            ),
            ProjectCatalogSnapshots(),
        )
    )


def _physical_line(
    controller: ProjectEditorController,
    start: tuple[float, float],
    end: tuple[float, float],
):
    first = controller.add_electrical_node(
        "Начало",
        x=start[0],
        y=start[1],
        voltage_class_id=U10,
    )
    second = controller.add_electrical_node(
        "Конец",
        x=end[0],
        y=end[1],
        voltage_class_id=U10,
    )
    return controller.create_physical_line(
        "ВЛ-10 кВ",
        LineKind.OVERHEAD,
        NodeTarget(first.node_id, first.representation_id),
        NodeTarget(second.node_id, second.representation_id),
        physical=PhysicalLineInput(
            1_000_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
            DataConfirmation.CONFIRMED,
        ),
    )


def _effective_size(representation) -> tuple[float, float]:
    raw = representation.extensions.get(GRAPHICS_EXTENSION_KEY, {})
    return float(raw.get("width", 74.0)), float(raw.get("height", 54.0))


def test_only_quarter_turns_are_accepted_and_helpers_do_not_accumulate_error() -> None:
    controller = _controller("quarters")
    added = controller.add_equipment("builtin.circuit_breaker", "QF-1")

    with pytest.raises(EditorCommandError, match="0°.*90°.*180°.*270°"):
        controller.rotate_representation(added.representation_id, 45.0)

    assert [
        controller.rotate_representation_clockwise(added.representation_id)
        for _ in range(4)
    ] == [90.0, 180.0, 270.0, 0.0]
    assert controller.diagram.representations[
        added.representation_id
    ].rotation_deg == 0.0
    assert normalize_quarter_turn(-90) is QuarterTurn.DEG_270


def test_new_equipment_rejects_non_quarter_rotation_without_partial_change() -> None:
    controller = _controller("new-angle")
    model_before = controller.model
    diagram_before = controller.diagram

    with pytest.raises(EditorCommandError, match="0°.*90°.*180°.*270°"):
        controller.add_equipment(
            "builtin.circuit_breaker",
            "QF-45",
            rotation_deg=45.0,
        )

    assert controller.model == model_before
    assert controller.diagram == diagram_before


def test_legacy_non_quarter_rotation_loads_losslessly_and_is_diagnosed() -> None:
    controller = _controller("legacy-angle")
    added = controller.add_equipment("builtin.circuit_breaker", "QF legacy")
    payload = diagram_to_dict(controller.diagram)
    payload["representations"][0]["rotation_deg"] = 45.0

    loaded = diagram_from_dict(payload, controller.model)
    fingerprint = electrical_model_fingerprint(controller.model)
    restored = loaded.representations[added.representation_id]
    assert restored.rotation_deg == 45.0

    diagnostics = ProjectValidationService().validate(controller.model, loaded)
    angle_diagnostics = tuple(
        item
        for item in diagnostics
        if item.code == "diagram.legacy_non_quarter_rotation"
    )
    assert len(angle_diagnostics) == 1
    diagnostic = angle_diagnostics[0]
    assert diagnostic.severity is ProjectDiagnosticSeverity.WARNING
    assert diagnostic.object_id == added.representation_id.value
    assert diagnostic.page_id == restored.page_id
    assert diagnostic.representation_id == added.representation_id
    assert "45°" in diagnostic.message
    assert "стар" in diagnostic.message.lower()
    assert all(value in diagnostic.action for value in ("0°", "90°", "180°", "270°"))

    # Диагностика не является скрытой миграцией: угол и электрическая модель
    # остаются неизменными, а повторное сохранение не теряет legacy-значение.
    assert loaded.representations[added.representation_id].rotation_deg == 45.0
    assert diagram_to_dict(loaded)["representations"][0]["rotation_deg"] == 45.0
    assert electrical_model_fingerprint(controller.model) == fingerprint


@pytest.mark.parametrize("angle", (0.0, 90.0, 180.0, 270.0))
def test_canonical_loaded_rotation_has_no_legacy_diagnostic(angle: float) -> None:
    controller = _controller(f"canonical-{int(angle)}")
    controller.add_equipment(
        "builtin.circuit_breaker",
        f"QF-{int(angle)}",
        rotation_deg=angle,
    )

    diagnostics = ProjectValidationService().validate(
        controller.model,
        diagram_from_dict(diagram_to_dict(controller.diagram), controller.model),
    )
    assert not any(
        item.code == "diagram.legacy_non_quarter_rotation"
        for item in diagnostics
    )


def test_transformer_semantic_ports_and_ids_survive_all_rotations() -> None:
    controller = _controller("transformer")
    added = controller.add_equipment("builtin.transformer_3w", "Т-1")
    equipment_before = controller.model.equipment[added.equipment_id]
    ids_by_role = {
        controller.model.ports[port_id].role: port_id
        for port_id in equipment_before.port_ids
    }
    assert set(ids_by_role) == {"hv", "mv", "lv"}

    positions: dict[int, dict[str, tuple[float, float]]] = {}
    for angle in (0, 90, 180, 270):
        controller.rotate_representation(added.representation_id, angle)
        representation = controller.diagram.representations[added.representation_id]
        equipment = controller.model.equipment[added.equipment_id]
        definition = controller.model.equipment_type(
            equipment.type_id, equipment.type_version
        )
        width, height = _effective_size(representation)
        positions[angle] = {}
        for role, port_id in ids_by_role.items():
            anchor = port_anchor_by_id(
                equipment,
                definition,
                port_id,
                width=width,
                height=height,
                rotation=angle,
                center_x=representation.x,
                center_y=representation.y,
            )
            assert anchor.role == role
            assert anchor.port_id == port_id
            positions[angle][role] = (anchor.x, anchor.y)
        assert controller.model.equipment[added.equipment_id] == equipment_before

    assert positions[0]["hv"] != positions[180]["hv"]
    assert positions[90]["lv"] != positions[270]["lv"]


def test_rotation_reroutes_only_incident_routes_and_undo_redo_is_atomic() -> None:
    controller = _controller("routes")
    first = controller.add_equipment("builtin.load", "Нагрузка-1", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Нагрузка-2", x=240, y=0, voltage_class_by_group={"main": U10})
    third = controller.add_equipment("builtin.load", "Нагрузка-3", x=0, y=240, voltage_class_by_group={"main": U10})
    fourth = controller.add_equipment("builtin.load", "Нагрузка-4", x=240, y=240, voltage_class_by_group={"main": U10})
    incident = controller.connect_ports(first.port_ids[0], second.port_ids[0])
    unrelated = controller.connect_ports(third.port_ids[0], fourth.port_ids[0])
    assert incident.route_id is not None and unrelated.route_id is not None

    representation_before = controller.diagram.representations[
        first.representation_id
    ]
    incident_before = controller.diagram.routes[incident.route_id]
    unrelated_before = controller.diagram.routes[unrelated.route_id]
    fingerprint = electrical_model_fingerprint(controller.model)
    equipment_before = controller.model.equipment[first.equipment_id]

    controller.rotate_representation(first.representation_id, 90)

    representation_after = controller.diagram.representations[first.representation_id]
    incident_after = controller.diagram.routes[incident.route_id]
    assert representation_after.rotation_deg == 90.0
    assert incident_after.waypoints != incident_before.waypoints
    assert controller.diagram.routes[unrelated.route_id] == unrelated_before
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert controller.model.equipment[first.equipment_id] == equipment_before
    assert controller.journal[-1].electrical_fingerprint_before == fingerprint
    assert controller.journal[-1].electrical_fingerprint_after == fingerprint

    controller.undo()
    assert controller.diagram.representations[first.representation_id] == (
        representation_before
    )
    assert controller.diagram.routes[incident.route_id] == incident_before
    controller.redo()
    assert controller.diagram.representations[first.representation_id] == (
        representation_after
    )
    assert controller.diagram.routes[incident.route_id] == incident_after

    baseline_geometry = tuple(
        (item.x, item.y)
        for item in controller.diagram.routes[incident.route_id].waypoints
    )
    for _ in range(4):
        controller.rotate_representation_clockwise(first.representation_id)
    assert tuple(
        (item.x, item.y)
        for item in controller.diagram.routes[incident.route_id].waypoints
    ) == baseline_geometry


def test_manual_and_auto_modes_roundtrip_with_safe_legacy_default() -> None:
    controller = _controller("modes")
    added = controller.add_equipment("builtin.circuit_breaker", "QF-1")
    representation = controller.diagram.representations[added.representation_id]
    assert orientation_mode_for_representation(representation) is OrientationMode.AUTO

    controller.rotate_representation_clockwise(added.representation_id)
    representation = controller.diagram.representations[added.representation_id]
    assert orientation_mode_for_representation(representation) is OrientationMode.MANUAL

    controller.auto_orient_representation(added.representation_id, 180)
    representation = controller.diagram.representations[added.representation_id]
    assert representation.rotation_deg == 180.0
    assert orientation_mode_for_representation(representation) is OrientationMode.AUTO

    reopened = diagram_from_dict(
        diagram_to_dict(controller.diagram), controller.model
    )
    restored = reopened.representations[added.representation_id]
    assert restored.rotation_deg == 180.0
    assert orientation_mode_for_representation(restored) is OrientationMode.AUTO

    legacy = replace(restored, extensions={})
    assert orientation_mode_for_representation(legacy) is OrientationMode.MANUAL
    assert RepresentationGraphics.from_representation(
        legacy
    ).orientation_mode is OrientationMode.MANUAL


@pytest.mark.parametrize(
    ("start", "end", "placement", "expected"),
    (
        ((0.0, 0.0), (400.0, 0.0), (200.0, 0.0), 0.0),
        ((0.0, 0.0), (0.0, 400.0), (0.0, 200.0), 90.0),
        ((400.0, 0.0), (0.0, 0.0), (200.0, 0.0), 180.0),
        ((0.0, 400.0), (0.0, 0.0), (0.0, 200.0), 270.0),
    ),
)
def test_inline_recloser_auto_orientation_uses_directed_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    placement: tuple[float, float],
    expected: float,
) -> None:
    controller = _controller(f"inline-{int(expected)}")
    line = _physical_line(controller, start, end)

    inserted = controller.insert_recloser(
        line.section_id,
        500_000,
        "Реклоузер",
        x=placement[0],
        y=placement[1],
    )

    representation = controller.diagram.representations[
        inserted.representation_id
    ]
    assert representation.rotation_deg == expected
    assert orientation_mode_for_representation(representation) is OrientationMode.AUTO
    equipment = controller.model.equipment[inserted.recloser_id]
    assert {
        controller.model.ports[port_id].role for port_id in equipment.port_ids
    } == {"a", "b"}

    for route_id in inserted.route_ids:
        route = controller.diagram.routes[route_id]
        for anchor, waypoint in (
            (route.start_anchor, route.waypoints[0]),
            (route.end_anchor, route.waypoints[-1]),
        ):
            if anchor.representation_id != representation.id:
                continue
            assert anchor.target_port_id is not None
            definition = controller.model.equipment_type(
                equipment.type_id, equipment.type_version
            )
            width, height = _effective_size(representation)
            port_anchor = port_anchor_by_id(
                equipment,
                definition,
                anchor.target_port_id,
                width=width,
                height=height,
                rotation=representation.rotation_deg,
                center_x=representation.x,
                center_y=representation.y,
            )
            assert (waypoint.x, waypoint.y) == pytest.approx(
                (port_anchor.x, port_anchor.y)
            )


def test_manual_rotation_does_not_change_switch_state() -> None:
    controller = _controller("switch")
    added = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-1",
        normal_position=SwitchPosition.OPEN,
    )
    equipment_before = controller.model.equipment[added.equipment_id]
    controller.rotate_representation(added.representation_id, 270)
    assert controller.model.equipment[added.equipment_id] == equipment_before
    assert controller.model.equipment[
        added.equipment_id
    ].normal_position is SwitchPosition.OPEN


def test_recloser_ports_and_open_state_survive_all_quarter_turns() -> None:
    controller = _controller("recloser-state")
    added = controller.add_equipment(
        "builtin.recloser",
        "Реклоузер-1",
        normal_position=SwitchPosition.OPEN,
    )
    equipment_before = controller.model.equipment[added.equipment_id]
    ids_by_role = {
        controller.model.ports[port_id].role: port_id
        for port_id in equipment_before.port_ids
    }
    fingerprint = electrical_model_fingerprint(controller.model)

    for angle in (0, 90, 180, 270):
        controller.rotate_representation(added.representation_id, angle)
        equipment = controller.model.equipment[added.equipment_id]
        assert equipment.normal_position is SwitchPosition.OPEN
        assert {
            controller.model.ports[port_id].role: port_id
            for port_id in equipment.port_ids
        } == ids_by_role
        assert electrical_model_fingerprint(controller.model) == fingerprint


def test_branch_attachment_keeps_requested_orientation_and_port_anchor() -> None:
    controller = _controller("branch-orientation")
    line = _physical_line(controller, (0.0, 0.0), (400.0, 0.0))
    definition = controller.model.equipment_type(EquipmentTypeId("builtin.load"))
    terminal_role = definition.port_definitions[0].role

    attached = controller.attach_equipment_to_line(
        line.section_id,
        500_000,
        definition.id,
        "Нагрузка на отпайке",
        terminal_role=terminal_role,
        tap_x=200.0,
        tap_y=0.0,
        equipment_x=200.0,
        equipment_y=140.0,
        rotation_deg=90,
        orientation_mode=OrientationMode.AUTO,
    )
    representation = controller.diagram.representations[
        attached.representation_id
    ]
    assert representation.rotation_deg == 90.0
    assert orientation_mode_for_representation(representation) is OrientationMode.AUTO

    equipment = controller.model.equipment[attached.equipment_id]
    selected_port = controller.model.port_by_role(
        equipment.id, terminal_role
    )
    definition = controller.model.equipment_type(
        equipment.type_id, equipment.type_version
    )
    width, height = _effective_size(representation)
    expected = port_anchor_by_id(
        equipment,
        definition,
        selected_port.id,
        width=width,
        height=height,
        rotation=representation.rotation_deg,
        center_x=representation.x,
        center_y=representation.y,
    )
    branch_route = next(
        route
        for route_id in attached.route_ids
        for route in (controller.diagram.routes[route_id],)
        if route.start_anchor.representation_id == representation.id
    )
    assert (branch_route.waypoints[0].x, branch_route.waypoints[0].y) == (
        expected.x,
        expected.y,
    )


@pytest.mark.parametrize(
    ("type_id", "terminal_role", "expected_rotation"),
    (
        ("builtin.generator", "terminal", 180.0),
        ("builtin.load", "terminal", 0.0),
        ("builtin.transformer_2w", "hv", 90.0),
        ("builtin.transformer_3w", "hv", 90.0),
    ),
)
def test_branch_auto_orientation_faces_selected_semantic_port_toward_tap(
    type_id: str,
    terminal_role: str,
    expected_rotation: float,
) -> None:
    token = type_id.rsplit(".", 1)[-1]
    controller = _controller(f"branch-auto-{token}")
    line = _physical_line(controller, (0.0, 0.0), (400.0, 0.0))

    attached = controller.attach_equipment_to_line(
        line.section_id,
        500_000,
        type_id,
        f"AUTO {token}",
        terminal_role=terminal_role,
        tap_x=200.0,
        tap_y=0.0,
        equipment_x=200.0,
        equipment_y=140.0,
        orientation_mode=OrientationMode.AUTO,
    )

    representation = controller.diagram.representations[
        attached.representation_id
    ]
    assert representation.rotation_deg == expected_rotation
    assert (
        orientation_mode_for_representation(representation)
        is OrientationMode.AUTO
    )
    equipment = controller.model.equipment[attached.equipment_id]
    port = controller.model.port_by_role(equipment.id, terminal_role)
    definition = controller.model.equipment_type(
        equipment.type_id, equipment.type_version
    )
    width, height = _effective_size(representation)
    anchor = port_anchor_by_id(
        equipment,
        definition,
        port.id,
        width=width,
        height=height,
        rotation=representation.rotation_deg,
        center_x=representation.x,
        center_y=representation.y,
    )
    assert anchor.direction is RouteDirection.UP
    branch_route = next(
        controller.diagram.routes[route_id]
        for route_id in attached.route_ids
        if controller.diagram.routes[route_id].start_anchor.representation_id
        == representation.id
    )
    assert (branch_route.waypoints[0].x, branch_route.waypoints[0].y) == (
        anchor.x,
        anchor.y,
    )


def test_full_project_roundtrip_keeps_orientation_mode(tmp_path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "tests" / "fixtures" / "legacy_projects"
        / "gtes_sever.json"
    )
    project = load_project(source)
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.EDIT)
    added = controller.add_equipment(
        "builtin.circuit_breaker", "QF roundtrip", x=20_000, y=20_000
    )
    controller.rotate_representation(added.representation_id, 270)

    target = tmp_path / "orientation-roundtrip.json"
    save_project(target, project)
    reopened = load_project(target)
    representation = reopened.diagram.representations[added.representation_id]
    assert representation.rotation_deg == 270.0
    assert orientation_mode_for_representation(representation) is OrientationMode.MANUAL
