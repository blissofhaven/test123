"""Units and actual calculation inputs agree across editing and persistence."""
from pathlib import Path
from dataclasses import replace

import pytest

from rza_calc.editor.parameter_schema import FieldSpec, parse_parameter_value, format_parameter_value, schemas_for
from rza_calc.editor.parameter_editing import ParameterPatch, ParameterValue
from rza_calc.editor import ProjectEditorController
from rza_calc.domain.electrical import DataConfirmation, ElectricalNodeId, LineSection, LineConstructionSegmentId
from rza_calc.core.fault_types import FaultSpec
from rza_calc.core.short_circuit import ShortCircuitSolver, ShortCircuitStatusError
from rza_calc.io.project import load_project, save_project
from rza_calc.adapters.legacy_calculation import adapt_to_calculation
from test_ui_connected_commands import _native_line
from test_stage03_parameter_editing import _controller

FIXTURE = Path(__file__).parent / "fixtures/legacy_projects/four_fault_types.json"


@pytest.mark.parametrize("unit,text,expected", [("m", "2500,125", 2500125), ("km", "2.500125", 2500125),
    ("mm", "2 500 125", 2500125), ("m", "0,001", 1)])
def test_length_conversion_is_exact_and_reversible(unit, text, expected):
    spec = FieldSpec("length_mm", "Длина", "integer", unit="mm", display_units=("m", "km"))
    assert parse_parameter_value(spec, text, unit) == expected
    assert parse_parameter_value(spec, format_parameter_value(spec, expected, unit), unit) == expected


@pytest.mark.parametrize("text", ["nan", "inf", "1,2.3", "12 34", True, "1e999", "1e308", "0,0001"])
def test_malformed_or_fractional_millimetres_are_not_silently_rounded(text):
    spec = FieldSpec("length_mm", "Длина", "integer", unit="mm", display_units=("m",))
    with pytest.raises(ValueError):
        parse_parameter_value(spec, text, "m")


@pytest.mark.parametrize("unit,value,expected", [("MVA", "1,25", 1250), ("kVA", "1250", 1250)])
def test_power_display_units_are_explicit(unit, value, expected):
    spec = FieldSpec("s_nom", "S", "number", unit="kVA", display_units=("MVA",))
    assert parse_parameter_value(spec, value, unit) == expected
    with pytest.raises(ValueError):
        parse_parameter_value(spec, value, "kA")


def _apply(c, eid, key, value, *, confirmed=False):
    pv = ParameterValue(value, "Проверочный источник", DataConfirmation.CONFIRMED if confirmed else DataConfirmation.UNCONFIRMED)
    preview = c.preview_parameter_patch((ParameterPatch(eid, {key: pv}),))
    assert preview.valid, preview.diagnostics
    c.apply_parameter_preview(preview)


def test_explicit_unconfirmed_z0_blocks_earth_only_then_confirmation_enables_it(tmp_path):
    project = load_project(FIXTURE)
    controller = ProjectEditorController(project)
    eid = next(e.id for e in controller.model.equipment.values() if str(e.type_id) == "compat.rza_calc.line")
    _apply(controller, eid, "r0_ohm_per_km", .5)
    path = tmp_path / "draft.json"
    save_project(path, project)
    reopened = load_project(path)
    for mode in reopened.network.modes.values():
        solver = ShortCircuitSolver(reopened.network, mode, reopened.methodology)
        assert solver.fault_at("FAULT", FaultSpec("3ph"))
        assert solver.fault_at("FAULT", FaultSpec("2ph"))
        for kind in ("1ph_g", "2ph_g"):
            with pytest.raises(ShortCircuitStatusError) as error:
                solver.fault_at("FAULT", FaultSpec(kind))
            assert error.value.code == "UNCONFIRMED_INPUT"
    controller = ProjectEditorController(reopened)
    _apply(controller, eid, "r0_ohm_per_km", .5, confirmed=True)
    for mode in reopened.network.modes.values():
        solver = ShortCircuitSolver(reopened.network, mode, reopened.methodology)
        for kind in ("3ph", "2ph", "1ph_g", "2ph_g"):
            assert solver.fault_at("FAULT", FaultSpec(kind))


def test_positive_draft_is_not_used_as_verified_input_then_explicit_confirmation_works():
    project = load_project(FIXTURE)
    c = ProjectEditorController(project)
    eid = next(e.id for e in c.model.equipment.values() if str(e.type_id) == "compat.rza_calc.source")
    _apply(c, eid, "s_kz_max", 600)
    with pytest.raises(ValueError, match="не подтверждён"):
        ShortCircuitSolver(project.network, project.network.modes["max"], project.methodology)
    assert ShortCircuitSolver(project.network, project.network.modes["min"], project.methodology).fault_at("FAULT", FaultSpec("3ph"))
    _apply(c, eid, "s_kz_max", 600, confirmed=True)
    assert ShortCircuitSolver(project.network, project.network.modes["max"], project.methodology).fault_at("FAULT", FaultSpec("3ph"))


def test_cleared_optional_source_selector_and_xr_do_not_require_confirming_a_blank():
    project = load_project(FIXTURE)
    c = ProjectEditorController(project)
    eid = next(e.id for e in c.model.equipment.values() if str(e.type_id) == "compat.rza_calc.source")
    for key in ("input_mode_max", "x_r_ratio", "voltage_kv"):
        _apply(c, eid, key, None)
    assert ShortCircuitSolver(project.network, project.network.modes["max"], project.methodology).fault_at("FAULT", FaultSpec("3ph"))


def test_source_xr_zero_is_explicit_active_impedance_and_not_missing():
    project = load_project(FIXTURE)
    c = ProjectEditorController(project)
    eid = next(e.id for e in c.model.equipment.values() if str(e.type_id) == "compat.rza_calc.source")
    _apply(c, eid, "x_r_ratio", 0, confirmed=True)
    result = ShortCircuitSolver(project.network, project.network.modes["max"], project.methodology).fault_at("BUS", FaultSpec("3ph"))
    assert result.z012_ohm[1] == pytest.approx(.2205 + 0j, rel=1e-12, abs=1e-14)


def test_native_ct_uses_physical_owned_port_and_follows_reconnection():
    c, first, last, line = _native_line()
    eid = next(iter(c.model.line_sections))
    equipment = c.model.equipment[eid]
    end = c.model.ports[equipment.port_ids[-1]]
    values = {"ct_primary_a": ParameterValue(600), "ct_secondary_a": ParameterValue(5),
              "ct_accuracy": ParameterValue("10P"), "ct_port": ParameterValue(end.id.value)}
    preview = c.preview_parameter_patch((ParameterPatch(eid, values),))
    assert preview.valid, preview.diagnostics
    c.apply_parameter_preview(preview)
    adapted = adapt_to_calculation(c.model)
    branch = next(iter(adapted.network.branches.values()))
    assert branch.ct_ratio == (600, 5)
    assert branch.ct_accuracy == "10P"
    assert branch.ct_node == branch.node_to
    assert "ct_ratio" not in c.model.equipment[eid].properties
    assert c.model.equipment[eid].port_ids == equipment.port_ids
    node = replace(c.model.node_for_port(end.id), id=ElectricalNodeId("ct.new-terminal"), name="Другая шина")
    c.model.add_node(node)
    c.model.reconnect_port(end.id, node.id)
    moved = next(iter(adapt_to_calculation(c.model).network.branches.values()))
    assert moved.ct_node == moved.node_to and moved.ct_node != branch.ct_node


def test_native_segment_sequence_override_cannot_borrow_parent_confirmation():
    c, first, last, line = _native_line()
    eid = line.section_id
    confirmed = {key: ParameterValue(value, "Паспорт", DataConfirmation.CONFIRMED)
                 for key, value in {"r0_ohm_per_km": .3, "x0_ohm_per_km": .7}.items()}
    c.apply_parameter_preview(c.preview_parameter_patch((ParameterPatch(eid, confirmed),)))
    segment = c.model.line_sections[eid].construction_segments[0]
    preview = c.preview_parameter_patch((ParameterPatch(eid, {"r0_ohm_per_km": ParameterValue(.6),
        "capacitive_current_a_per_km": ParameterValue(1.2)}, segment_id=segment.id),))
    assert preview.valid, preview.diagnostics
    c.apply_parameter_preview(preview)
    branch = next(iter(adapt_to_calculation(c.model).network.branches.values()))
    assert branch.r0_ohm_per_km == .6
    assert branch.parameter_provenance["r0_ohm_per_km"]["confirmation"] == "unconfirmed"
    assert branch.parameter_provenance["x0_ohm_per_km"]["confirmation"] == "confirmed"
    assert branch.parameter_provenance["capacitive_current_a_per_km"]["confirmation"] == "unconfirmed"


def test_malformed_null_segment_provenance_is_not_hidden_by_confirmed_neighbor():
    c, first, last, line = _native_line()
    section = c.model.line_sections[line.section_id]
    original = section.construction_segments[0]
    props = {"r0_ohm_per_km": .3, "x0_ohm_per_km": .7}
    first = replace(original, properties=props, extensions={"rza_calc.parameter_provenance": {
        "r0_ohm_per_km": {"source": "Паспорт", "confirmation": "confirmed"}}})
    second = replace(first, id=LineConstructionSegmentId("segment.invalid-provenance"),
        extensions={"rza_calc.parameter_provenance": {"r0_ohm_per_km": None}})
    c.model._line_sections[line.section_id] = LineSection(line.section_id, section.logical_line_id,
        construction_segments=(first, second))
    branch = next(iter(adapt_to_calculation(c.model).network.branches.values()))
    assert "r0_ohm_per_km" in branch.parameter_provenance
    assert branch.parameter_provenance["r0_ohm_per_km"] is None


def test_three_winding_ct_accuracy_is_saved_as_nameplate_without_inventing_a_dto_field():
    c = _controller()
    item = c.add_equipment("builtin.transformer_3w", "Т3", x=0, y=0,
        properties={"s_nom": 10000, "u_hv": 110, "u_mv": 35, "u_lv": 10, "uk_hm": 10, "uk_hl": 17, "uk_ml": 6})
    _apply(c, item.equipment_id, "ct_accuracy", "10P", confirmed=True)
    equipment = c.model.equipment[item.equipment_id]
    assert equipment.extensions["rza_calc.nameplate"]["ct_accuracy"] == "10P"
    assert "ct_accuracy" not in equipment.properties
    assert c.equipment_parameter_snapshot(item.equipment_id).fields["ct_accuracy"].confirmation == DataConfirmation.CONFIRMED


def test_additional_neutral_data_stays_explicitly_separate_from_sequence_equivalent(tmp_path):
    p = load_project(FIXTURE)
    c = ProjectEditorController(p)
    eid = next(e.id for e in c.model.equipment.values() if str(e.type_id) == "compat.rza_calc.source")
    before = ShortCircuitSolver(p.network, p.network.modes["max"], p.methodology).fault_at("FAULT", FaultSpec("1ph_g"))
    _apply(c, eid, "neutral_r_terminal_ohm", 99, confirmed=True)
    path = tmp_path / "neutral-passport.json"
    save_project(path, p)
    after = load_project(path)
    assert after.electrical_model.equipment[eid].extensions["rza_calc.nameplate"]["neutral_r_terminal_ohm"] == 99
    result = ShortCircuitSolver(after.network, after.network.modes["max"], after.methodology).fault_at("FAULT", FaultSpec("1ph_g"))
    assert result.z012_ohm == before.z012_ohm
