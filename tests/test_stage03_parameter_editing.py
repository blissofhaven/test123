"""Parameter cards and pasted batches share one atomic, audited contract."""
from dataclasses import replace
from pathlib import Path

import pytest

from rza_calc.domain.electrical import (
    DataConfirmation, ElectricalModel, ElectricalNode, ElectricalNodeId,
    LineKind, VoltageClassId, EquipmentTypeId, LineConstructionSegment,
    LineConstructionSegmentId, thaw_json,
)
from rza_calc.domain.catalog import UserCatalog, CatalogEntry, CatalogEntryId, CatalogCategoryId, CatalogOrigin
from rza_calc.editor import ProjectEditorController
from rza_calc.editor.controller import EditorCommandError
from rza_calc.editor.history import ProjectMemento
from rza_calc.editor.parameter_editing import ParameterPatch, ParameterValue, PROVENANCE_KEY
from rza_calc.io.project import load_project, save_project
from test_ui_connected_commands import _controller as _empty_controller


def _controller():
    project=_empty_controller()._project
    project.user_catalog=UserCatalog()
    return ProjectEditorController(project)


def _line_model(confirmation=DataConfirmation.UNCONFIRMED):
    model=ElectricalModel.with_builtins()
    voltage=VoltageClassId("builtin.voltage.ac.10kv")
    first=ElectricalNode(ElectricalNodeId("parameter.first"),"First",declared_voltage_class_id=voltage)
    last=ElectricalNode(ElectricalNodeId("parameter.last"),"Last",declared_voltage_class_id=voltage)
    model.add_node(first);model.add_node(last)
    line,section,_=model.create_logical_line("КЛ",LineKind.CABLE,first.id,last.id,1000000,
        inherited_properties={"r1_ohm_per_km":.3,"x1_ohm_per_km":.1},
        impedance_confirmation=confirmation)
    return model,line,section


@pytest.mark.parametrize("operation",["set","inherit","clear"])
def test_editing_numbers_never_confirms_unconfirmed_line(operation):
    model,line,section=_line_model()
    if operation=="set":model.set_section_override(section.equipment_id,"r1_ohm_per_km",.4)
    elif operation=="inherit":model.set_line_inherited_property(line.id,"r1_ohm_per_km",.4)
    else:
        model.set_section_override(section.equipment_id,"r1_ohm_per_km",.4)
        model.clear_section_override(section.equipment_id,"r1_ohm_per_km")
    assert model.line_sections[section.equipment_id].construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED


def test_changed_confirmed_impedance_requires_confirmation_again_but_noop_keeps_it():
    model,line,section=_line_model(DataConfirmation.CONFIRMED)
    model.set_line_inherited_property(line.id,"r1_ohm_per_km",.3)
    assert model.line_sections[section.equipment_id].construction_segments[0].impedance_confirmation is DataConfirmation.CONFIRMED
    model.set_section_override(section.equipment_id,"r1_ohm_per_km",.4)
    assert model.line_sections[section.equipment_id].construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED


def test_two_equipment_batch_preview_is_readonly_and_apply_undo_redo_is_one_command():
    c=_controller()
    generator=c.add_equipment("builtin.generator","G",x=0,y=0,
        properties={"s_nom":1000,"u_nom":10,"xd2":.2})
    load=c.add_equipment("builtin.load","L",x=200,y=0,properties={"p_kw":100})
    before=ProjectMemento.capture(c._project);count=len(c.journal)
    patches=(ParameterPatch(generator.equipment_id,{"s_nom":ParameterValue(1500,"Паспорт G",DataConfirmation.CONFIRMED)}),
             ParameterPatch(load.equipment_id,{"p_kw":ParameterValue(125)}))
    preview=c.preview_parameter_patch(patches)
    assert preview.valid,preview.diagnostics
    assert ProjectMemento.capture(c._project)==before
    assert len(preview.changes)==2
    result=c.apply_parameter_preview(preview)
    assert set(result.equipment_ids)=={generator.equipment_id,load.equipment_id}
    assert len(c.journal)==count+1
    after=ProjectMemento.capture(c._project)
    assert after.diagram==before.diagram
    assert after.electrical.connections==before.electrical.connections
    assert after.electrical.ports==before.electrical.ports
    assert c.model.equipment[generator.equipment_id].properties["s_nom"]==1500
    assert c.equipment_parameter_snapshot(generator.equipment_id).fields["s_nom"].confirmation is DataConfirmation.CONFIRMED
    c.undo();assert ProjectMemento.capture(c._project)==before
    c.redo();assert ProjectMemento.capture(c._project)==after


def test_bad_last_cell_rejects_whole_batch_and_zero_is_not_a_blank():
    c=_controller()
    first=c.add_equipment("builtin.load","L1",x=0,y=0,properties={"p_kw":100})
    last=c.add_equipment("builtin.load","L2",x=200,y=0,properties={"p_kw":200})
    before=ProjectMemento.capture(c._project);count=len(c.journal)
    bad=c.preview_parameter_patch((ParameterPatch(first.equipment_id,{"p_kw":ParameterValue(0)}),
        ParameterPatch(last.equipment_id,{"cos_phi":ParameterValue(1.2)})))
    assert not bad.valid and bad.errors[0].ref.equipment_id==last.equipment_id
    with pytest.raises(EditorCommandError):c.apply_parameter_preview(bad)
    assert ProjectMemento.capture(c._project)==before and len(c.journal)==count
    good=c.preview_parameter_patch((ParameterPatch(first.equipment_id,{"p_kw":ParameterValue(0)}),))
    assert good.valid,good.diagnostics
    c.apply_parameter_preview(good)
    assert c.model.equipment[first.equipment_id].properties["p_kw"]==0
    assert c.model.equipment[last.equipment_id].properties["p_kw"]==200


def test_stale_same_revision_external_record_rejects_without_overwriting_it():
    c=_controller();load=c.add_equipment("builtin.load","L",x=0,y=0,properties={"p_kw":100})
    proposal=c.preview_parameter_patch((ParameterPatch(load.equipment_id,{"p_kw":ParameterValue(200)}),))
    assert proposal.valid,proposal.diagnostics
    equipment=c.model.equipment[load.equipment_id]
    c.model._equipment[equipment.id]=replace(equipment,note="External edit, same revision")
    before=ProjectMemento.capture(c._project)
    with pytest.raises(EditorCommandError,match="изменился"):
        c.apply_parameter_preview(proposal)
    assert ProjectMemento.capture(c._project)==before


def test_empty_batch_and_unchanged_cell_do_not_create_history():
    c=_controller();load=c.add_equipment("builtin.load","L",x=0,y=0,properties={"p_kw":100})
    before=ProjectMemento.capture(c._project);count=len(c.journal)
    c.apply_parameter_preview(c.preview_parameter_patch(()))
    snapshot=c.equipment_parameter_snapshot(load.equipment_id)
    proposal=c.preview_parameter_patch((ParameterPatch(load.equipment_id,{"p_kw":snapshot.fields["p_kw"]}),))
    assert proposal.valid,proposal.diagnostics
    c.apply_parameter_preview(proposal)
    assert ProjectMemento.capture(c._project)==before and len(c.journal)==count


def test_confirmation_needs_source_but_incomplete_values_are_savable():
    c=_controller();load=c.add_equipment("builtin.load","L",x=0,y=0,properties={"p_kw":100})
    bad=c.preview_parameter_patch((ParameterPatch(load.equipment_id,{"p_kw":ParameterValue(200,"",DataConfirmation.CONFIRMED)}),))
    assert not bad.valid
    incomplete=c.preview_parameter_patch((ParameterPatch(load.equipment_id,{"p_kw":ParameterValue(None)}),))
    assert incomplete.valid,incomplete.diagnostics
    c.apply_parameter_preview(incomplete)
    assert c.model.equipment[load.equipment_id].properties["p_kw"] is None


def test_bulk_snapshots_capture_once_and_keep_distinct_ids(monkeypatch):
    c=_controller()
    ids=[c.add_equipment("builtin.load",str(i),x=i*100,y=0).equipment_id for i in range(3)]
    calls=[];original=ProjectMemento.capture.__func__
    def capture(cls,project):
        calls.append(project);return original(cls,project)
    monkeypatch.setattr(ProjectMemento,"capture",classmethod(capture))
    rows=c.equipment_parameter_snapshots(ids)
    assert [r.equipment_id for r in rows]==ids and len(calls)==1
    assert all(r.stamp is rows[0].stamp for r in rows)


def _physical_controller():
    model,line,section=_line_model()
    project=_controller()._project
    project.electrical_model=model
    return ProjectEditorController(project),line,section


def _legacy_controller():
    path=Path(__file__).resolve().parent / "fixtures/legacy_projects/four_fault_types.json"
    controller=ProjectEditorController(load_project(path))
    line=next(e for e in controller.model.equipment.values() if str(e.type_id)=="compat.rza_calc.line")
    return controller,line


def _apply(controller,equipment_id,values,**kwargs):
    proposal=controller.preview_parameter_patch((ParameterPatch(equipment_id,values,**kwargs),))
    assert proposal.valid,proposal.diagnostics
    controller.apply_parameter_preview(proposal)
    return proposal


def test_legacy_length_exact_units_and_payload_save_reload(tmp_path):
    c,line=_legacy_controller()
    before=ProjectMemento.capture(c._project)
    assert c.equipment_parameter_snapshot(line.id).fields["length_mm"].value==2_000_000
    _apply(c,line.id,{"length_mm":ParameterValue(1_234_567,"Обмер трассы",DataConfirmation.CONFIRMED)})
    payload=thaw_json(c.model.equipment[line.id].properties["legacy_payload"])
    expected=thaw_json(line.properties["legacy_payload"]);expected["length_km"]=1.234567
    assert payload==expected
    assert c.model.equipment[line.id].port_ids==line.port_ids
    assert c._project.diagram==before.diagram
    output=tmp_path / "length.json";save_project(output,c._project)
    reloaded=ProjectEditorController(load_project(output))
    assert reloaded.equipment_parameter_snapshot(line.id).fields["length_mm"]==ParameterValue(1_234_567,"Обмер трассы",DataConfirmation.CONFIRMED)
    assert reloaded.model.equipment[line.id].properties==c.model.equipment[line.id].properties
    c.undo();assert ProjectMemento.capture(c._project)==before


def test_native_ct_side_is_semantic_owned_connected_port_and_atomic_pair():
    c,_,section=_physical_controller();eid=section.equipment_id
    equipment=c.model.equipment[eid];port=equipment.port_ids[1]
    before=ProjectMemento.capture(c._project)
    _apply(c,eid,{"ct_primary_a":ParameterValue(600),"ct_secondary_a":ParameterValue(1),
        "ct_port":ParameterValue(port.value),"ct_accuracy":ParameterValue("10P")})
    equipment=c.model.equipment[eid]
    assert equipment.extensions["rza_calc.ct_parameters"]["ct_ratio"]==(600,1)
    assert equipment.extensions["rza_calc.ct_port_id"]==port.value
    assert "ct_ratio" not in equipment.properties and "ct_node" not in equipment.properties
    assert c.equipment_parameter_snapshot(eid).fields["ct_port"].value==port.value
    assert c.model.connections==dict(before.electrical.connections)
    c.undo();assert ProjectMemento.capture(c._project)==before
    foreign=c.add_equipment("builtin.circuit_breaker","Foreign",x=0,y=0)
    foreign_port=c.model.equipment[foreign.equipment_id].port_ids[0]
    bad=c.preview_parameter_patch((ParameterPatch(eid,{"ct_port":ParameterValue(foreign_port.value)}),))
    assert not bad.valid
    free=c.preview_parameter_patch((ParameterPatch(foreign.equipment_id,{"ct_port":ParameterValue(foreign_port.value)}),))
    assert not free.valid and "подключ" in free.errors[0].message


def test_legacy_ct_side_keeps_payload_and_slot_together_and_persists(tmp_path):
    c,line=_legacy_controller();port=line.port_ids[1]
    original=thaw_json(line.properties["legacy_payload"])
    _apply(c,line.id,{"ct_primary_a":ParameterValue(800),"ct_secondary_a":ParameterValue(5),
        "ct_port":ParameterValue(port.value,"Схема вторичных цепей",DataConfirmation.CONFIRMED)})
    equipment=c.model.equipment[line.id]
    payload=equipment.properties["legacy_payload"]
    slot=equipment.extensions["editor_legacy_ct_ports"]["ct_node"]
    assert slot["port_id"]==port.value and slot["node_id"]==payload["ct_node"]
    expected={**original,"ct_ratio":[800,5],"ct_node":slot["node_id"]}
    assert thaw_json(payload)==expected
    output=tmp_path / "ct.json";save_project(output,c._project)
    loaded=ProjectEditorController(load_project(output))
    assert loaded.equipment_parameter_snapshot(line.id).fields["ct_port"].value==port.value
    assert loaded.model.equipment[line.id].extensions==equipment.extensions


def test_segment_edit_and_clear_preserve_other_segment_and_restore_inherited_source():
    c,line,section=_physical_controller();eid=section.equipment_id
    first=section.construction_segments[0]
    second=LineConstructionSegment(LineConstructionSegmentId("segment.second"),LineKind.CABLE,700000,
        properties={"r1_ohm_per_km":.9,"x1_ohm_per_km":.8},
        impedance_confirmation=DataConfirmation.UNCONFIRMED)
    c.model.add_line_construction_segment(eid,second)
    c=ProjectEditorController(c._project)
    confirmed=lambda value:ParameterValue(value,"Паспорт линии",DataConfirmation.CONFIRMED)
    _apply(c,eid,{"r1_ohm_per_km":confirmed(.4),"x1_ohm_per_km":confirmed(.2)})
    sections=c.model.line_sections[eid].construction_segments
    assert sections[0].impedance_confirmation is DataConfirmation.CONFIRMED
    assert sections[1].impedance_confirmation is DataConfirmation.UNCONFIRMED
    assert c.equipment_parameter_snapshot(eid,second.id).fields["r1_ohm_per_km"].source==""
    first_before=sections[0]
    _apply(c,eid,{"length_mm":ParameterValue(123456,"Измерение",DataConfirmation.CONFIRMED),
        "r1_ohm_per_km":ParameterValue(.7)},segment_id=second.id)
    assert c.model.line_sections[eid].construction_segments[0]==first_before
    assert c.model.line_sections[eid].construction_segments[1].length_mm==123456
    preview=c.preview_parameter_patch((ParameterPatch(eid,clear={"r1_ohm_per_km"},segment_id=second.id),))
    assert preview.valid,preview.diagnostics
    c.apply_parameter_preview(preview)
    value=c.equipment_parameter_snapshot(eid,second.id).fields["r1_ohm_per_km"]
    assert value.value==.4 and value.source=="Паспорт линии" and value.confirmation is DataConfirmation.CONFIRMED
    assert c.model.line_sections[eid].construction_segments[1].impedance_confirmation is DataConfirmation.UNCONFIRMED
    aggregate=c.preview_parameter_patch((ParameterPatch(eid,{"length_mm":ParameterValue(1)}),))
    assert not aggregate.valid


def test_old_line_setter_invalidates_persisted_field_confirmation():
    c,line,section=_physical_controller();eid=section.equipment_id
    confirmed=lambda value:ParameterValue(value,"Паспорт",DataConfirmation.CONFIRMED)
    _apply(c,eid,{"r1_ohm_per_km":confirmed(.4),"x1_ohm_per_km":confirmed(.2)})
    c.model.set_section_override(eid,"r1_ohm_per_km",.6)
    snapshot=c.equipment_parameter_snapshot(eid)
    assert snapshot.fields["r1_ohm_per_km"].confirmation is DataConfirmation.UNCONFIRMED
    assert snapshot.fields["x1_ohm_per_km"].confirmation is DataConfirmation.CONFIRMED
    assert c.model.line_sections[eid].construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED


def _entry(type_id,properties,*,extensions=None):
    return CatalogEntry(CatalogEntryId("catalog.parameter.brand"),CatalogOrigin.USER,CatalogCategoryId("parameters"),
        "Проверенная марка",EquipmentTypeId(type_id),1,properties=properties,source="Паспорт производителя",
        extensions=extensions or {})


def test_selected_catalog_values_keep_manual_by_default_and_snapshot_is_truthful(tmp_path):
    c,line=_legacy_controller()
    _apply(c,line.id,{"r1_ohm_per_km":ParameterValue(.7,origin="manual")})
    entry=_entry("builtin.line_section.cable",{"r1_ohm_per_km":.4,"x1_ohm_per_km":.12},extensions={PROVENANCE_KEY:{
        "r1_ohm_per_km":{"confirmation":"confirmed","source":"Протокол R"},
        "x1_ohm_per_km":{"confirmation":"confirmed","source":"Протокол X"}}})
    c.save_parameter_catalog_entry(entry)
    before=ProjectMemento.capture(c._project);count=len(c.journal)
    preview=c.preview_catalog_update((line.id,),entry)
    assert preview.valid,preview.incompatible
    assert {change.key for change in preview.changes}=={"r1_ohm_per_km","x1_ohm_per_km"}
    assert ProjectMemento.capture(c._project)==before
    c.apply_catalog_update(preview,[change.ref for change in preview.changes])
    snapshot=c.equipment_parameter_snapshot(line.id)
    assert snapshot.fields["r1_ohm_per_km"].value==.7
    assert snapshot.fields["x1_ohm_per_km"]==ParameterValue(.12,"Протокол X",DataConfirmation.CONFIRMED,"catalog")
    binding=c._project.catalog_snapshots.bindings[line.id]
    assert binding.entry.equipment_type_id==entry.equipment_type_id
    assert binding.entry.properties==entry.properties
    assert binding.effective_properties["legacy_payload"]["r0"]==.7
    assert binding.effective_properties["legacy_payload"]["x0"]==.12
    assert c.model.equipment[line.id].type_id==line.type_id
    assert len(c.journal)==count+1 and c._project.user_catalog.get(entry.id)==entry
    output=tmp_path / "catalog.json";save_project(output,c._project)
    loaded=ProjectEditorController(load_project(output))
    assert loaded._project.catalog_snapshots.bindings[line.id]==binding
    assert loaded.equipment_parameter_snapshot(line.id).fields==snapshot.fields
    c.undo();assert ProjectMemento.capture(c._project)==before
    c.redo();assert c._project.catalog_snapshots.bindings[line.id]==binding


def test_catalog_preview_rejects_wrong_family_and_shares_single_capture(monkeypatch):
    c=_controller();ids=[c.add_equipment("builtin.load",str(i),x=i*100,y=0).equipment_id for i in range(3)]
    calls=[];original=ProjectMemento.capture.__func__
    def capture(cls,project):calls.append(project);return original(cls,project)
    monkeypatch.setattr(ProjectMemento,"capture",classmethod(capture))
    preview=c.preview_catalog_update(ids,_entry("builtin.generator",{"s_nom":1000}))
    assert not preview.valid and len(preview.incompatible)==3 and len(calls)==1


def test_legacy_clear_is_explicit_missing_not_dto_default():
    c,line=_legacy_controller()
    proposal=c.preview_parameter_patch((ParameterPatch(line.id,clear={"r1_ohm_per_km"}),))
    assert proposal.valid,proposal.diagnostics
    c.apply_parameter_preview(proposal)
    equipment=c.model.equipment[line.id]
    assert "r0" in equipment.properties["legacy_payload"]
    assert equipment.properties["legacy_payload"]["r0"] is None
    assert equipment.extensions[PROVENANCE_KEY]["r1_ohm_per_km"]["confirmation"]=="unconfirmed"


def test_native_ct_can_change_and_clear_side_without_topology_change():
    c,_,section=_physical_controller();eid=section.equipment_id
    first,last=c.model.equipment[eid].port_ids
    connections=dict(c.model.connections)
    _apply(c,eid,{"ct_port":ParameterValue(first.value)})
    _apply(c,eid,{"ct_port":ParameterValue(last.value)})
    assert c.equipment_parameter_snapshot(eid).fields["ct_port"].value==last.value
    preview=c.preview_parameter_patch((ParameterPatch(eid,clear={"ct_port"}),))
    assert preview.valid,preview.diagnostics
    c.apply_parameter_preview(preview)
    assert c.model.equipment[eid].extensions["rza_calc.ct_port_id"] is None
    assert c.model.equipment[eid].extensions[PROVENANCE_KEY]["ct_port"]["confirmation"]=="unconfirmed"
    assert dict(c.model.connections)==connections


def test_catalog_confirmation_can_be_declined_and_manual_edit_updates_override():
    c=_controller();load=c.add_equipment("builtin.load","L",x=0,y=0)
    entry=_entry("builtin.load",{"p_kw":125,"cos_phi":.8},extensions={PROVENANCE_KEY:{
        "p_kw":{"confirmation":"confirmed","source":"Паспорт"}}})
    c.save_parameter_catalog_entry(entry)
    preview=c.preview_catalog_update((load.equipment_id,),entry)
    c.apply_catalog_update(preview,[change.ref for change in preview.changes],preserve_manual=False,inherit_confirmation=False)
    assert c.equipment_parameter_snapshot(load.equipment_id).fields["p_kw"].confirmation is DataConfirmation.UNCONFIRMED
    _apply(c,load.equipment_id,{"p_kw":ParameterValue(130,origin="manual")})
    binding=c._project.catalog_snapshots.bindings[load.equipment_id]
    assert binding.effective_properties["p_kw"]==130
    assert binding.parameter_overrides["p_kw"].source_value==125
    assert binding.parameter_overrides["p_kw"].override_value==130
    assert binding.entry.properties["p_kw"]==125
    assert c._project.user_catalog.get(entry.id)==entry


def test_existing_native_ct_properties_are_visible_and_extension_override_is_explicit():
    c=_controller()
    equipment=c.add_equipment("builtin.circuit_breaker","Q",x=0,y=0,
        properties={"ct_ratio":[400,5],"ct_accuracy":"10P","breaker_t_off":.1,"terminal":"Old terminal"})
    eid=equipment.equipment_id
    snapshot=c.equipment_parameter_snapshot(eid)
    assert snapshot.fields["ct_primary_a"].value==400
    assert snapshot.fields["ct_secondary_a"].value==5
    assert snapshot.fields["ct_accuracy"].value=="10P"
    assert snapshot.fields["terminal"].value=="Old terminal"
    _apply(c,eid,{"ct_primary_a":ParameterValue(600),"ct_accuracy":ParameterValue(None)})
    snapshot=c.equipment_parameter_snapshot(eid)
    assert snapshot.fields["ct_primary_a"].value==600
    assert snapshot.fields["ct_secondary_a"].value==5
    assert snapshot.fields["ct_accuracy"].value is None
    assert c.model.equipment[eid].properties["ct_ratio"]==(400,5)
    assert c.model.equipment[eid].extensions["rza_calc.ct_parameters"]["ct_ratio"]==(600,5)


def test_legacy_float_ulp_read_and_confirmation_do_not_normalize_original_value():
    c,line=_legacy_controller()
    properties=thaw_json(line.properties);properties["legacy_payload"]["length_km"]=1.3499999999999999
    c.model._equipment[line.id]=replace(line,properties=properties)
    c=ProjectEditorController(c._project)
    before=ProjectMemento.capture(c._project)
    snapshot=c.equipment_parameter_snapshot(line.id)
    assert snapshot.fields["length_mm"].value==1_350_000
    assert ProjectMemento.capture(c._project)==before
    _apply(c,line.id,{"length_mm":ParameterValue(1_350_000,"Обмер",DataConfirmation.CONFIRMED)})
    assert c.model.equipment[line.id].properties["legacy_payload"]["length_km"]==1.3499999999999999
    assert c.model.equipment[line.id].properties==properties


def test_genuine_fractional_legacy_millimetre_is_not_silently_rounded():
    c,line=_legacy_controller()
    properties=thaw_json(line.properties);properties["legacy_payload"]["length_km"]=1.3500001
    c.model._equipment[line.id]=replace(line,properties=properties)
    with pytest.raises(ValueError,match="миллиметров"):
        c.equipment_parameter_snapshot(line.id)


def test_catalog_ct_and_nameplate_overrides_keep_full_audit_outside_dto():
    c=_controller();q=c.add_equipment("builtin.circuit_breaker","Q",x=0,y=0)
    eid=q.equipment_id
    entry=_entry("builtin.circuit_breaker",{},extensions={
        "rza_calc.ct_parameters":{"ct_ratio":[400,5]},
        "rza_calc.nameplate":{"rated_current_a":630}})
    c.save_parameter_catalog_entry(entry)
    preview=c.preview_catalog_update((eid,),entry)
    c.apply_catalog_update(preview,[change.ref for change in preview.changes],preserve_manual=False)
    _apply(c,eid,{"ct_primary_a":ParameterValue(600,"Протокол ТТ"),
                 "rated_current_a":ParameterValue(800,"Паспорт Q")})
    binding=c._project.catalog_snapshots.bindings[eid]
    audits=binding.extensions["parameter_extension_overrides"]
    for key,old,new in (("ct_primary_a",400,600),("rated_current_a",630,800)):
        row=audits[key]
        assert row["source_value"]==old and row["override_value"]==new
        assert row["source"] and row["reason"] and row["modified_at"]
        assert key not in binding.parameter_overrides
    assert "ct_ratio" not in binding.effective_properties
    assert "rated_current_a" not in binding.effective_properties
    assert c.equipment_parameter_snapshot(eid).fields["ct_secondary_a"].value==5
