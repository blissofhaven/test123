# -*- coding: utf-8 -*-
"""Two-position UI policy must not migrate saved geometry or electrical ports."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.electrical import ElectricalModel
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.orientation import (
    EDITOR_ROTATIONS,
    QuarterTurn,
    bus_anchor_geometry,
    editor_rotation,
    nearest_editor_rotation,
    next_editor_rotation,
    normalize_quarter_turn,
    rotated_port_layout,
)
from rza_calc.editor.orthogonal_routing import RouteDirection
from rza_calc.editor.state import EditorMode
from rza_calc.editor.tool_state import EditorTool, EditorToolStateMachine
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict


@pytest.mark.parametrize(
    ("angle", "expected"),
    ((0, 180), (90, 90), (180, 180), (270, 90), (-90, 90), (360, 180)),
)
def test_new_editor_action_maps_legacy_axis_to_two_positions(angle, expected):
    assert EDITOR_ROTATIONS == (90, 180)
    assert editor_rotation(angle) == expected
    assert isinstance(editor_rotation(angle), int)


@pytest.mark.parametrize(
    ("angle", "expected"),
    ((0, 90), (90, 180), (180, 90), (270, 180)),
)
def test_next_action_switches_axis_even_for_legacy_angle(angle, expected):
    assert next_editor_rotation(angle) == expected


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_low_level_quarter_turn_contract_is_unchanged(angle):
    assert normalize_quarter_turn(angle) is QuarterTurn(angle)


@pytest.mark.parametrize("angle", (45, 12.5, True, "90", float("nan"), float("inf")))
def test_editor_discrete_actions_reject_invalid_angles(angle):
    with pytest.raises(ValueError):
        editor_rotation(angle)
    with pytest.raises(ValueError):
        next_editor_rotation(angle)


@pytest.mark.parametrize(
    ("angle", "expected"),
    (
        (-360, 180), (-270, 90), (-180, 180), (-90, 90), (0, 180),
        (44, 180), (46, 90), (90, 90), (134, 90), (136, 180),
        (180, 180), (224, 180), (226, 90), (270, 90), (314, 90),
        (316, 180), (360, 180), (450, 90),
    ),
)
def test_drag_chooses_nearest_axis_across_a_full_circle(angle, expected):
    assert nearest_editor_rotation(angle) == expected


@pytest.mark.parametrize("angle", (45, 135, 225, 315, -45))
def test_drag_diagonal_tie_preserves_current_axis(angle):
    assert nearest_editor_rotation(angle, current=90) == 90
    assert nearest_editor_rotation(angle, current=180) == 180
    assert nearest_editor_rotation(angle, current=270) == 90
    assert nearest_editor_rotation(angle, current=0) == 180
    assert nearest_editor_rotation(angle) == 90


@pytest.mark.parametrize("angle", (True, "90", float("nan"), float("inf")))
def test_drag_rejects_nonfinite_or_nonnumeric_angles(angle):
    with pytest.raises(ValueError):
        nearest_editor_rotation(angle)


@pytest.mark.parametrize("clockwise", (True, False))
def test_placement_starts_vertical_and_repeated_rotation_has_only_two_states(clockwise):
    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.transformer_3w"}
    machine.begin_placement(payload, "Трансформатор", repeat=True)
    assert machine.state.preview_rotation_deg == 90

    for expected in (180, 90, 180, 90, 180, 90):
        rotating = machine.begin_preview_rotation(clockwise=clockwise)
        assert rotating.current.tool is EditorTool.ROTATE_PREVIEW
        assert rotating.current.preview_rotation_deg == expected
        assert rotating.current.payload is payload
        machine.finish_preview_rotation()
        assert machine.state.tool is EditorTool.PLACE_EQUIPMENT_REPEAT
        assert machine.state.preview_rotation_deg == expected

    machine.placement_succeeded()
    assert machine.state.tool is EditorTool.PLACE_EQUIPMENT_REPEAT
    assert machine.state.preview_rotation_deg == 90


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_explicit_placement_angle_obeys_ui_policy_without_mutating_payload(angle):
    machine = EditorToolStateMachine()
    payload = {"graphics": {"rotation_deg": angle}}
    machine.begin_placement(payload, "Выключатель", preview_rotation_deg=angle)
    assert machine.state.preview_rotation_deg == editor_rotation(angle)
    assert payload["graphics"]["rotation_deg"] == angle


@pytest.mark.parametrize("current", EDITOR_ROTATIONS)
@pytest.mark.parametrize("clockwise", (True, False))
def test_preview_rotation_toggles_actual_auto_axis_not_stale_state(current, clockwise):
    machine = EditorToolStateMachine()
    payload = {"type_id": "builtin.recloser"}
    machine.begin_placement(
        payload, "Реклоузер", preview_rotation_deg=next_editor_rotation(current)
    )

    transition = machine.begin_preview_rotation(
        clockwise=clockwise, current_rotation_deg=current
    )

    assert transition.current.tool is EditorTool.ROTATE_PREVIEW
    assert transition.current.preview_rotation_deg == next_editor_rotation(current)
    assert transition.current.payload is payload
    machine.finish_preview_rotation()
    assert machine.state.tool is EditorTool.PLACE_EQUIPMENT_ONCE
    assert machine.state.preview_rotation_deg == next_editor_rotation(current)


@pytest.mark.parametrize("current", (45, 12.5, True, "90", float("nan"), float("inf")))
@pytest.mark.parametrize("already_rotating", (False, True))
def test_invalid_actual_preview_angle_does_not_partially_change_tool_state(current, already_rotating):
    machine = EditorToolStateMachine()
    machine.begin_placement({"type_id": "builtin.recloser"}, "Реклоузер", repeat=True)
    if already_rotating:
        machine.begin_preview_rotation()
    before = machine.state

    with pytest.raises(ValueError):
        machine.begin_preview_rotation(current_rotation_deg=current)

    assert machine.state == before


def test_object_rotation_is_edit_only_exclusive_cancellable_tool():
    machine = EditorToolStateMachine()
    payload = {"representation_id": "r1"}
    rotated = machine.activate(EditorTool.ROTATE_OBJECT, payload=payload)
    assert rotated.accepted
    assert not rotated.current.is_placement
    assert rotated.current.display_name == "Поворот объекта"
    assert "Удерживайте" in rotated.current.hint
    cancelled = machine.escape(has_selection=True)
    assert cancelled.cancelled
    assert not cancelled.clear_selection
    assert cancelled.current.tool is EditorTool.SELECT

    machine.activate(EditorTool.ROTATE_OBJECT, payload=payload)
    assert machine.set_editor_mode(EditorMode.ANALYSIS).cancelled
    assert not machine.activate(EditorTool.ROTATE_OBJECT, payload=payload).accepted
    assert machine.state.tool is EditorTool.SELECT


def _controller():
    model = ElectricalModel.with_builtins("Two positions")
    diagram = DiagramDocument.create(
        "Test", (DiagramPage(PageId("page.two-positions"), "Test"),)
    )
    return ProjectEditorController(SimpleNamespace(
        electrical_model=model,
        diagram=diagram,
        catalog_snapshots=ProjectCatalogSnapshots(),
    ))


@pytest.mark.parametrize("angle", (0, 270))
def test_loading_and_roundtripping_legacy_angles_is_lossless(angle):
    controller = _controller()
    result = controller.add_equipment(
        "builtin.transformer_2w", "Т1", rotation_deg=angle
    )
    before = diagram_to_dict(controller.diagram)
    fingerprint = electrical_model_fingerprint(controller.model)
    loaded = diagram_from_dict(before, controller.model)

    assert loaded.representations[result.representation_id].rotation_deg == angle
    assert diagram_to_dict(loaded) == before
    assert electrical_model_fingerprint(controller.model) == fingerprint


@pytest.mark.parametrize("type_id", ("builtin.transformer_2w", "builtin.transformer_3w"))
def test_new_orientations_preserve_transformer_port_ids_roles_and_electrical_model(type_id):
    controller = _controller()
    result = controller.add_equipment(type_id, "Т1", rotation_deg=90)
    equipment = controller.model.equipment[result.equipment_id]
    definition = controller.model.equipment_type(equipment.type_id, equipment.type_version)
    expected_roles = {
        port_id: controller.model.ports[port_id].role for port_id in equipment.port_ids
    }
    fingerprint = electrical_model_fingerprint(controller.model)
    positions = {}

    for angle in EDITOR_ROTATIONS:
        controller.rotate_representation(result.representation_id, angle)
        layout = rotated_port_layout(
            equipment, definition, width=64, height=64, rotation=angle
        )
        assert {anchor.port_id: anchor.role for anchor in layout} == expected_roles
        positions[angle] = {anchor.role: (anchor.x, anchor.y) for anchor in layout}
        assert electrical_model_fingerprint(controller.model) == fingerprint

    assert positions[90]["hv"] != positions[180]["hv"]
    assert positions[90]["lv"] != positions[180]["lv"]


@pytest.mark.parametrize(
    ("rotation", "expected"),
    (
        (0, (360.0, 260.0, RouteDirection.UP)),
        (90, (300.0, 320.0, RouteDirection.RIGHT)),
        (180, (240.0, 260.0, RouteDirection.DOWN)),
        (270, (300.0, 200.0, RouteDirection.LEFT)),
    ),
)
def test_bus_anchor_rotates_local_horizontal_axis_and_direction(rotation, expected):
    assert bus_anchor_geometry(
        width=240, height=12, rotation=rotation,
        center_x=300, center_y=260, fraction=0.75,
    ) == expected


@pytest.mark.parametrize(
    ("rotation", "expected"),
    (
        (0, (300.0, 320.0, RouteDirection.LEFT)),
        (90, (240.0, 260.0, RouteDirection.UP)),
        (180, (300.0, 200.0, RouteDirection.RIGHT)),
        (270, (360.0, 260.0, RouteDirection.DOWN)),
    ),
)
def test_bus_anchor_supports_legacy_vertical_local_axis(rotation, expected):
    assert bus_anchor_geometry(
        width=12, height=240, rotation=rotation,
        center_x=300, center_y=260, fraction=0.75,
    ) == expected


@pytest.mark.parametrize(
    ("fraction", "expected_x"),
    ((-2, 180.0), (0, 180.0), (0.5, 300.0), (1, 420.0), (2, 420.0)),
)
def test_bus_anchor_clamps_finite_fraction_to_segment_ends(fraction, expected_x):
    assert bus_anchor_geometry(
        width=240, height=12, rotation=0,
        center_x=300, center_y=260, fraction=fraction,
    ) == (expected_x, 260.0, RouteDirection.UP)


def test_bus_anchor_equal_dimensions_uses_horizontal_local_axis():
    assert bus_anchor_geometry(
        width=24, height=24, rotation=0,
        center_x=0, center_y=0, fraction=0.75,
    ) == (6.0, 0.0, RouteDirection.UP)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("width", 0), ("height", -1), ("width", float("inf")),
        ("height", float("nan")), ("center_x", float("nan")),
        ("center_y", float("inf")), ("fraction", float("nan")),
        ("fraction", float("inf")), ("fraction", True),
        ("width", "240"), ("rotation", 45),
    ),
)
def test_bus_anchor_rejects_invalid_geometry(field, value):
    arguments = dict(
        width=240, height=12, rotation=90,
        center_x=300, center_y=260, fraction=0.75,
    )
    arguments[field] = value
    with pytest.raises(ValueError):
        bus_anchor_geometry(**arguments)
