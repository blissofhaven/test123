"""Canonical mode edits are independent, atomic and preserve legacy terminals."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from rza_calc.adapters.legacy_calculation import import_legacy_network, adapt_to_calculation
from rza_calc.core.model import Network, Node, SourceBranch, LineBranch, Load, Mode, GRID, Transformer3W
from rza_calc.domain.electrical import DataConfirmation, EquipmentAvailability, EquipmentId, OperatingStateId, PortId, SwitchPosition, thaw_json
from rza_calc.domain.catalog import UserCatalog
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.operating_parameters import (
    ModeValue, OperatingParameters, OPERATING_PARAMETERS_KEY, operating_parameters,
    operating_parameters_to_dict, operating_parameters_from_dict, with_operating_parameters,
)
from rza_calc.editor.controller import ProjectEditorController, EditorCommandError
from rza_calc.editor.history import ProjectMemento
from rza_calc.editor.state import EditorMode
from rza_calc.io.electrical_model import electrical_model_to_dict, electrical_model_from_dict
from rza_calc.io.project import load_project, save_project


def _project():
    project = load_project(Path(__file__).parent/'fixtures/legacy_projects/four_fault_types.json')
    return project, ProjectEditorController(project)


def _legacy(parallel=False):
    net = Network('Сценарии')
    for name in ('A','B'):
        net.add_node(Node(name, name, 10))
    net.add_branch(SourceBranch('S','Система',GRID,'A',s_kz_max=500,s_kz_min=250))
    net.add_branch(LineBranch('long_line_from_name','Линия','A','B',length_km=1,r0=.1,x0=.1,
        switchable=True,ct_ratio=(100,5),ct_node='B'))
    if parallel:
        net.add_branch(SourceBranch('S2','Система 2',GRID,'A',s_kz_max=500,s_kz_min=250))
    net.add_load(Load('L','Нагрузка','B',p_kw=100))
    net.add_mode(Mode('normal','Нормальный',states={'long_line_from_name':True,'SW:long_line_from_name:from':True,
        'SW:long_line_from_name:to':False},availability={'L':True}))
    net.add_mode(Mode('reserve','Резерв',states={'SW:long_line_from_name:to':True},system='min'))
    model = import_legacy_network(net)
    project=SimpleNamespace(electrical_model=model,diagram=DiagramDocument.create('Схема'),
        user_catalog=UserCatalog(),catalog_snapshots=ProjectCatalogSnapshots())
    return project,ProjectEditorController(project)


def _source(controller):
    return next(e.id for e in controller.model.equipment.values() if str(e.type_id)=='compat.rza_calc.source')


def test_preview_and_cancel_do_not_write_apply_one_undo_exact(tmp_path):
    project, controller = _project()
    sid = next(iter(controller.model.operating_states))
    eid = _source(controller)
    before = ProjectMemento.capture(project)
    equipment_before = dict(controller.model.equipment)
    others = {k:v for k,v in controller.model.operating_states.items() if k != sid}
    draft = controller.operating_mode_draft(sid)
    draft = replace(draft, description='Проверка источника',parameters=OperatingParameters(
        sources={eid:{'s_kz_max':ModeValue(600,'Расчёт энергосистемы',DataConfirmation.CONFIRMED)}},
        load_factor=ModeValue(.8,'График нагрузки')))
    preview = controller.preview_operating_mode(draft)
    assert preview.valid, preview.diagnostics
    preview_model, preview_sid = controller.operating_mode_preview_model(preview)
    assert preview_model is not controller.model and preview_sid == sid
    assert ProjectMemento.capture(project) == before
    count=len(controller.journal)
    assert controller.apply_operating_mode_preview(preview) == sid
    assert len(controller.journal) == count+1
    assert dict(controller.model.equipment) == equipment_before
    assert {k:v for k,v in controller.model.operating_states.items() if k != sid} == others
    path=tmp_path/'mode.json'
    save_project(path,project)
    loaded=load_project(path)
    assert operating_parameters(loaded.electrical_model.operating_states[sid]) == draft.parameters
    assert loaded.electrical_model.operating_states[sid].description == draft.description
    controller.undo()
    assert ProjectMemento.capture(project) == before
    controller.redo()
    assert operating_parameters(controller.model.operating_states[sid]) == draft.parameters


def test_stale_preview_rejects_external_same_revision_change():
    project,controller=_project()
    sid=next(iter(controller.model.operating_states))
    preview=controller.preview_operating_mode(replace(controller.operating_mode_draft(sid),name='Изменённый'))
    controller.model._operating_states[sid]=replace(controller.model.operating_states[sid],description='Внешняя правка')
    before=ProjectMemento.capture(project)
    with pytest.raises(EditorCommandError,match='изменился'):
        controller.apply_operating_mode_preview(preview)
    assert ProjectMemento.capture(project)==before


def test_clone_preserves_every_raw_legacy_terminal_and_has_new_calculation_id():
    project,controller=_legacy()
    sid=next(iter(controller.model.operating_states))
    old=controller.model.operating_states[sid]
    raw=thaw_json(old.extensions['legacy_calculation'])
    draft=controller.operating_mode_draft(clone_from=sid)
    preview=controller.preview_operating_mode(draft)
    assert preview.valid,preview.diagnostics
    new_id=controller.apply_operating_mode_preview(preview)
    new=controller.model.operating_states[new_id]
    assert new_id != sid and controller.model.operating_states[sid] == old
    assert new.extensions['legacy_calculation']['states']==old.extensions['legacy_calculation']['states']
    assert new.extensions['legacy_calculation']['availability']==old.extensions['legacy_calculation']['availability']
    assert 'legacy_id' not in new.extensions['legacy_calculation']
    adapted=adapt_to_calculation(controller.model).network
    assert len(adapted.modes)==3 and adapted.modes['normal'].states==raw['states']
    cloned=next(v for k,v in adapted.modes.items() if k not in {'normal','reserve'})
    assert cloned.states==raw['states'] and cloned.availability==raw['availability']
    restored=electrical_model_from_dict(electrical_model_to_dict(controller.model))
    assert restored.operating_states[new_id]==new


def test_exact_terminal_resolver_and_one_endpoint_change_preserves_other_end():
    _,controller=_legacy()
    sid=next(iter(controller.model.operating_states))
    target=controller.resolve_operating_mode_switch('SW_long_line_from_name_to')
    assert target.legacy_key=='SW:long_line_from_name:to'
    draft=controller.operating_mode_draft(sid)
    draft=replace(draft,extra_positions={**draft.extra_positions,target.legacy_key:True})
    preview=controller.preview_operating_mode(draft)
    assert preview.valid,preview.diagnostics
    controller.apply_operating_mode_preview(preview)
    modes=adapt_to_calculation(controller.model).network.modes
    assert modes['normal'].states['SW:long_line_from_name:to'] is True
    assert modes['normal'].states['SW:long_line_from_name:from'] is True
    assert modes['reserve'].states=={'SW:long_line_from_name:to':True}


def test_snapshot_resolves_sparse_defaults_without_writing_and_selection_only_geometry():
    project,controller=_legacy()
    before=ProjectMemento.capture(project)
    snapshots=controller.operating_mode_snapshots()
    first=snapshots[0]
    assert first.effective_positions['SW:long_line_from_name:to']==SwitchPosition.OPEN
    assert first.calculation_mode_id=='normal'
    assert ProjectMemento.capture(project)==before
    fp=electrical_model_fingerprint(controller.model)
    controller.select_operating_mode(snapshots[1].state_id)
    assert electrical_model_fingerprint(controller.model)==fp
    assert not controller.journal


def test_legacy_parallel_not_confirmed_by_read_new_edit_requires_explicit_permission():
    project,controller=_legacy(parallel=True)
    sid=next(iter(controller.model.operating_states))
    before=ProjectMemento.capture(project)
    draft=controller.operating_mode_draft(sid)
    assert draft.parameters.parallel_operation is None
    assert ProjectMemento.capture(project)==before
    changed=replace(draft,name='Параллельная работа')
    invalid=controller.preview_operating_mode(changed)
    assert not invalid.valid and 'параллель' in invalid.errors[0].message
    assert invalid.can_display
    preview_model,preview_sid=controller.operating_mode_preview_model(invalid)
    assert preview_model.operating_states[preview_sid].name==changed.name
    assert preview_model is not controller.model and ProjectMemento.capture(project)==before
    with pytest.raises(EditorCommandError,match='не применён'):
        controller.apply_operating_mode_preview(invalid)
    explicit=replace(changed,parameters=replace(changed.parameters,parallel_operation=True))
    assert controller.preview_operating_mode(explicit).valid
    assert not controller.preview_operating_mode(replace(explicit,parameters=replace(explicit.parameters,parallel_operation=False))).valid


def test_noop_preview_creates_no_history_command():
    project,controller=_project()
    sid=next(iter(controller.model.operating_states))
    before=ProjectMemento.capture(project)
    preview=controller.preview_operating_mode(controller.operating_mode_draft(sid))
    assert preview.valid and not preview.changes
    controller.apply_operating_mode_preview(preview)
    assert not controller.journal and ProjectMemento.capture(project)==before


def test_templates_are_local_explicit_and_do_not_invent_equipment():
    project,controller=_legacy()
    before=ProjectMemento.capture(project)
    sid=next(iter(controller.model.operating_states))
    load=next(e.id for e in controller.model.equipment.values() if str(e.type_id)=='compat.rza_calc.load')
    draft=controller.operating_mode_template('repair',base_state_id=sid,equipment_id=load)
    assert draft.availability[load]==EquipmentAvailability.OUT_OF_SERVICE
    assert ProjectMemento.capture(project)==before
    with pytest.raises(ValueError,match='конкретный'):
        controller.operating_mode_template('repair',base_state_id=sid)
    with pytest.raises(ValueError,match='генератор'):
        controller.operating_mode_template('generator_loss',base_state_id=sid,equipment_id=load)


@pytest.mark.parametrize('kind',['source','load','port'])
def test_dangling_parameter_references_rejected_on_load(kind):
    _,controller=_legacy()
    sid=next(iter(controller.model.operating_states))
    parameters=OperatingParameters(
        sources={EquipmentId('missing.source'):{'s_kz_max':ModeValue(200)}} if kind=='source' else {},
        load_factors={EquipmentId('missing.load'):ModeValue(.5)} if kind=='load' else {},
        working_currents={PortId('missing.port'):ModeValue(12)} if kind=='port' else {})
    raw=electrical_model_to_dict(controller.model)
    raw['operating_states'][0]['extensions'][OPERATING_PARAMETERS_KEY]=operating_parameters_to_dict(parameters)
    with pytest.raises(ValueError,match='отсутствует'):
        electrical_model_from_dict(raw)


def test_removal_cleans_only_owned_source_load_and_port_inputs():
    _,controller=_legacy()
    model=controller.model
    sid=next(iter(model.operating_states))
    source=_source(controller)
    load=next(e.id for e in model.equipment.values() if str(e.type_id)=='compat.rza_calc.load')
    line=next(e.id for e in model.equipment.values() if str(e.type_id)=='compat.rza_calc.line')
    port=model.equipment[line].port_ids[0]
    params=OperatingParameters(sources={source:{'s_kz_max':ModeValue(600)}},load_factor=ModeValue(.8),
        load_factors={load:ModeValue(.5)},working_currents={port:ModeValue(10,'Протокол',DataConfirmation.CONFIRMED)})
    model._operating_states[sid]=replace(model.operating_states[sid],extensions=with_operating_parameters(model.operating_states[sid].extensions,params))
    model.remove_equipment(line,cascade=True)
    current=operating_parameters(model.operating_states[sid])
    assert not current.working_currents and current.sources==params.sources and current.load_factors==params.load_factors
    model.remove_equipment(load,cascade=True)
    assert not operating_parameters(model.operating_states[sid]).load_factors
    assert 'L' not in model.operating_states[sid].extensions['legacy_calculation']['availability']
    assert 'L' not in adapt_to_calculation(model).network.modes['normal'].availability
    model.remove_equipment(source,cascade=True)
    current=operating_parameters(model.operating_states[sid])
    assert not current.sources and current.load_factor==params.load_factor
    assert not model.validate_integrity()


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,True,'12'])
def test_invalid_working_current_never_enters_model(bad):
    with pytest.raises(ValueError):
        OperatingParameters(working_currents={PortId('test.port'):ModeValue(bad)})


def test_explicit_unknown_distinct_from_inherit_and_zero_and_nested_immutable():
    eid=EquipmentId('source.x')
    original={eid:{'r0_ohm':ModeValue(None),'x0_ohm':ModeValue(0)}}
    parameters=OperatingParameters(sources=original)
    original[eid]['r0_ohm']=ModeValue(42)
    assert parameters.sources[eid]['r0_ohm'].value is None
    assert parameters.sources[eid]['x0_ohm'].value==0
    assert 'r2_ohm' not in parameters.sources[eid]
    with pytest.raises(TypeError):
        parameters.sources[eid]['r0_ohm']=ModeValue(1)
    assert operating_parameters_from_dict(operating_parameters_to_dict(parameters))==parameters


def test_unknown_version_or_key_and_false_confirmation_rejected():
    raw=operating_parameters_to_dict(OperatingParameters())
    with pytest.raises(ValueError):
        operating_parameters_from_dict({**raw,'version':2})
    with pytest.raises(ValueError):
        operating_parameters_from_dict({**raw,'invented':True})
    with pytest.raises(ValueError,match='источника'):
        ModeValue(10,'',DataConfirmation.CONFIRMED)


def test_structurally_bad_draft_cannot_be_rendered_or_applied():
    _,controller=_project()
    sid=next(iter(controller.model.operating_states))
    draft=replace(controller.operating_mode_draft(sid),positions={EquipmentId('missing.switch'):SwitchPosition.OPEN})
    preview=controller.preview_operating_mode(draft)
    assert not preview.valid and not preview.can_display
    with pytest.raises(ValueError,match='структурные'):
        controller.operating_mode_preview_model(preview)


def test_navigation_and_geometry_after_draft_and_preview_are_preserved_by_apply():
    project,controller=_project()
    second=DiagramPage(PageId('page.mode.second'),'Второй лист')
    project.diagram=replace(project.diagram,pages={**project.diagram.pages,second.id:second})
    controller=ProjectEditorController(project)
    sid=next(iter(controller.model.operating_states))
    draft=replace(controller.operating_mode_draft(sid),description='Не меняет рисунок')
    controller.set_active_page(second.id)
    controller.set_view(.8,10,20)
    preview=controller.preview_operating_mode(draft)
    assert preview.valid,preview.diagnostics
    controller.set_active_page(next(iter(project.diagram.pages)))
    node=next(row for row in project.diagram.representations.values() if row.electrical_node_id is not None)
    controller.move_representations((node.id,),0,80,bypass_snap=True)
    geometric=project.diagram
    before_mode=ElectricalModelMemento.capture(controller.model)
    count=len(controller.journal)
    controller.apply_operating_mode_preview(preview)
    assert len(controller.journal)==count+1
    assert project.diagram.representations==geometric.representations
    assert project.diagram.routes==geometric.routes
    assert project.diagram.pages==geometric.pages
    assert controller.workspace_state.view_x==10 and controller.workspace_state.zoom==.8
    controller.undo()
    assert ElectricalModelMemento.capture(controller.model).equipment==before_mode.equipment
    assert project.diagram.representations==geometric.representations
    assert project.diagram.routes==geometric.routes


def test_methodology_change_invalidates_mode_draft():
    project,controller=_project()
    sid=next(iter(controller.model.operating_states))
    draft=replace(controller.operating_mode_draft(sid),description='Изменение')
    project.methodology.data['name']='Внешняя методика'
    with pytest.raises(EditorCommandError,match='изменился'):
        controller.preview_operating_mode(draft)


@pytest.mark.parametrize('key,value',[('s_kz_max',-1),('i_kz_min',True),('x_r_ratio',-1),('input_mode_max','guess'),('voltage_kv',0),('xd2',.2)])
def test_invalid_source_input_rejected_load_and_save_before_write(tmp_path,key,value):
    project,controller=_project()
    sid=next(iter(controller.model.operating_states))
    params=OperatingParameters(sources={_source(controller):{key:ModeValue(value)}})
    original=controller.model.operating_states[sid]
    controller.model._operating_states[sid]=replace(original,extensions=with_operating_parameters(original.extensions,params))
    raw=electrical_model_to_dict(controller.model)
    with pytest.raises(ValueError):
        electrical_model_from_dict(raw)
    path=tmp_path/'unchanged.json'
    path.write_text('original file',encoding='utf-8')
    with pytest.raises(ValueError):
        save_project(path,project)
    assert path.read_text(encoding='utf-8')=='original file'


def test_3w_leg_and_endpoint_raw_states_availability_survive_clone():
    net=Network('Три стороны')
    for nid,voltage in [('HV',110),('MV',35),('LV',10)]:
        net.add_node(Node(nid,nid,voltage))
    net.add_branch(SourceBranch('SYS','Система',GRID,'HV',s_kz_max=1000,s_kz_min=500))
    net.add_transformer3w(Transformer3W('T_1','Три обмотки','HV','MV','LV',16000,110,35,10,10,11,7))
    net.add_mode(Mode('normal','Нормальный',states={'T_1':True,'T_1_mv':False,'SW:T_1_lv:from':False},
        availability={'T_1_mv':False,'T_1_lv':True}))
    model=import_legacy_network(net)
    project=SimpleNamespace(electrical_model=model,diagram=DiagramDocument.create('Схема'),
        user_catalog=UserCatalog(),catalog_snapshots=ProjectCatalogSnapshots())
    controller=ProjectEditorController(project)
    sid=next(iter(model.operating_states))
    draft=controller.operating_mode_draft(clone_from=sid)
    preview=controller.preview_operating_mode(draft)
    assert preview.valid,preview.diagnostics
    new_id=controller.apply_operating_mode_preview(preview)
    modes=adapt_to_calculation(controller.model).network.modes
    copy=next(mode for key,mode in modes.items() if key!='normal')
    assert copy.states==net.modes['normal'].states
    assert copy.availability==net.modes['normal'].availability
    target=controller.resolve_operating_mode_equipment('T_1_mv')
    assert str(controller.model.equipment[target].type_id)=='compat.rza_calc.transformer_3w'
    assert controller.model.operating_states[new_id].name.endswith('копия')


def test_compare_is_readonly_and_returns_source_field_and_position_differences():
    project,controller=_legacy()
    before=ProjectMemento.capture(project)
    first,second=tuple(controller.model.operating_states)
    changes=controller.compare_operating_modes(first,second)
    assert any(row.key=='system' and row.before=='max' and row.after=='min' for row in changes)
    assert any(row.target_id=='SW:long_line_from_name:from' for row in changes)
    assert ProjectMemento.capture(project)==before


def test_mode_draft_and_apply_work_from_analysis_without_enabling_geometry_editing():
    project,controller=_project()
    controller.set_mode(EditorMode.ANALYSIS)
    sid=next(iter(controller.model.operating_states))
    before=ProjectMemento.capture(project)
    preview=controller.preview_operating_mode(replace(controller.operating_mode_draft(sid),description='Из анализа'))
    assert preview.valid and ProjectMemento.capture(project)==before
    controller.apply_operating_mode_preview(preview)
    assert controller.mode==EditorMode.ANALYSIS
    assert controller.model.operating_states[sid].description=='Из анализа'
    with pytest.raises(EditorCommandError):
        controller.move_representations((next(iter(project.diagram.representations)),),10,0)


def test_mode_choices_use_exact_ids_and_live_metadata_without_preparing_electrical_data(monkeypatch):
    import rza_calc.editor.mode_editing as service
    project, controller = _legacy()
    first = next(iter(controller.model.operating_states.values()))
    native = replace(first, id=OperatingStateId.new(), name='Новый режим',
        description='Только название в списке', extensions={})
    controller.model.add_operating_state(native)
    expected_ids = tuple(adapt_to_calculation(controller.model).network.modes)
    before = ElectricalModelMemento.capture(controller.model)
    journal_before = tuple(controller.journal)

    def forbidden(*args, **kwargs):
        raise AssertionError('A mode selector must not prepare electrical inputs')

    monkeypatch.setattr(service, '_stamp', forbidden)
    monkeypatch.setattr(service, 'operating_mode_targets', forbidden)
    monkeypatch.setattr(service, 'operating_mode_snapshots', forbidden)
    monkeypatch.setattr(service.TopologyEngine, 'compile', forbidden)
    monkeypatch.setattr(service.ProjectMemento, 'capture', forbidden)
    monkeypatch.setattr(service.ElectricalModelMemento, 'capture', forbidden)
    choices = controller.operating_mode_choices()
    assert isinstance(choices, tuple)
    assert tuple(row.state_id for row in choices) == tuple(controller.model.operating_states)
    assert tuple(row.calculation_mode_id for row in choices) == expected_ids
    assert [(row.name, row.description, row.system) for row in choices] == [
        (row.name, row.description, row.system) for row in controller.model.operating_states.values()]
    with pytest.raises(FrozenInstanceError):
        choices[0].name = 'Изменить через список нельзя'
    assert tuple(controller.journal) == journal_before
    assert before.to_model().operating_states == controller.model.operating_states
    # Even a replacement outside editor commands must be visible on the next read.
    controller.model._operating_states[first.id] = replace(first, name='Переименованный режим')
    assert controller.operating_mode_choices()[0].name == 'Переименованный режим'
    assert choices[0].name == first.name
