"""Batch inspection preserves distinct physical CT sides and refreshes edits."""
from rza_calc.editor.parameter_editing import ParameterValue
from test_stage03_parameter_editing import _legacy_controller, _apply


def test_batch_ct_lookup_matches_cards_and_does_not_outlive_parameter_edit():
    controller,line = _legacy_controller()
    ids = tuple(controller.model.equipment)
    before = controller.equipment_parameter_snapshots(ids)
    assert {row.equipment_id: row.fields for row in before} == {
        eid:controller.equipment_parameter_snapshot(eid).fields for eid in ids}
    other = next(pid for pid in line.port_ids
                 if pid.value != controller.equipment_parameter_snapshot(line.id).fields['ct_port'].value)
    _apply(controller,line.id,{'ct_port':ParameterValue(other.value)})
    after = controller.equipment_parameter_snapshots(ids)
    assert next(row for row in after if row.equipment_id==line.id).fields['ct_port'].value == other.value
    assert {row.equipment_id:row.fields for row in after if row.equipment_id != line.id} == {
        row.equipment_id:row.fields for row in before if row.equipment_id != line.id}
