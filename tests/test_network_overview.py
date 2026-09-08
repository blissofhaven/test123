"""Read-only hierarchy and circuit identities, independent of drawing positions."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rza_calc.application import network_overview as overview
from rza_calc.domain.diagram import DiagramDocument, DiagramDocumentId, GraphicalRepresentationId
from rza_calc.domain.electrical import (
    ElectricalModel, ElectricalNode, ElectricalNodeId, EquipmentAvailability,
    EquipmentId, VoltageClassId,
)
from rza_calc.domain.model import (
    Bay, BusSection, CalculationRef, Equipment, EquipmentPlacement,
    Facility, ProjectStructure, VoltageLevel,
)
from rza_calc.editor.history import ProjectMemento
from rza_calc.io.project import load_project
from rza_calc.topology import DiagnosticSeverity, TopologyDiagnostic, TopologyEngine


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project():
    return load_project(ROOT / 'rza_calc/examples/compact_training.json')


def legacy(objects, name):
    return next(item for item in objects.values()
                if item.extensions.get('legacy_calculation', {}).get('legacy_id') == name)


def row(project, fid):
    return next(r for r in project.metadata['training_demo']['facilities'] if r['id'] == fid)


def build(project, **kwargs):
    return overview.build_network_overview(project, **kwargs)


def fail(*args, **kwargs):
    raise AssertionError('Overview must not calculate or compile topology implicitly')


def test_compact_exact_physical_counts_ids_and_no_calculation_or_mutation(project, monkeypatch):
    before = ProjectMemento.capture(project), deepcopy(project.metadata), deepcopy(project.structure)
    monkeypatch.setattr(TopologyEngine, 'compile', fail)
    from rza_calc.core.short_circuit import ShortCircuitSolver
    monkeypatch.setattr(ShortCircuitSolver, '__init__', fail)
    # Deliberately withhold the Network property: projection only needs these four stores.
    source = SimpleNamespace(electrical_model=project.electrical_model, diagram=project.diagram,
                             metadata=project.metadata, structure=project.structure)
    result = build(source)
    assert result.source == 'training_demo'
    assert not result.diagnostics
    assert len(result.facilities) == 4 and len(result.links) == 6
    assert [f.transformer_count for f in result.facilities] == [2, 2, 2, 2]
    assert [f.outgoing_count for f in result.facilities] == [4, 4, 4, 2]
    assert [f.incoming_count for f in result.facilities] == [0, 2, 2, 2]
    assert all(f.counts_complete and f.unassigned_count == 0 for f in result.facilities)
    assert all(f.status.code == 'unknown' and f.outgoing_in_service is None for f in result.facilities)
    assert all(t.status.code == 'unknown' for t in result.targets.values())
    assert set(result.facility_ids_by_equipment) == set(project.electrical_model.equipment)
    assert not result.unassigned.equipment_ids
    for f in result.facilities:
        assert f.preferred_page_id in project.diagram.pages
        assert result.page_facility_ids[f.preferred_page_id] == (f.id,)
        assert result.targets[f.target.key] == f.target
    for link in result.links:
        first = project.electrical_model.connection_for_port(link.from_port_id)
        last = project.electrical_model.connection_for_port(link.to_port_id)
        assert first.electrical_node_id == link.from_node_id
        assert last.electrical_node_id == link.to_node_id
        assert project.electrical_model.ports[first.port_id].equipment_id in link.equipment_ids
        assert first.port_id != last.port_id
    ktp = [link for link in result.links if link.to_facility_id == 'train_ktp']
    assert len(ktp) == 2 and len({link.from_bus_id for link in ktp}) == 2
    assert next(f for f in result.facilities if f.id == 'train_ktp').input_sections_distinct is True
    assert before == (ProjectMemento.capture(project), project.metadata, project.structure)


def test_lookup_and_dto_are_immutable_and_do_not_fingerprint(project, monkeypatch):
    result = build(project)
    monkeypatch.setattr(overview, 'electrical_model_fingerprint', fail)
    for f in result.facilities:
        for eid in f.target.equipment_ids:
            assert f.id in result.facility_ids_by_equipment[eid]
        assert result.targets[f.target.key] == f.target
    with pytest.raises(TypeError):
        result.targets['other'] = result.unassigned
    with pytest.raises(FrozenInstanceError):
        result.facilities[0].status.code = 'energized'


def test_current_topology_status_and_outgoing_circuits_then_repair(project):
    model = project.electrical_model
    state = legacy(model.operating_states, 'normal_max')
    initial = TopologyEngine().compile(model, state.id)
    result = build(project, operating_state_id=state.id, topology=initial)
    assert all(f.status.code == 'energized' for f in result.facilities)
    assert [f.outgoing_in_service for f in result.facilities] == [4, 4, 4, 2]
    outgoing = row(project, 'train_ktp')['loads'][0]['branch_ids'][0]
    breaker = legacy(model.equipment, outgoing)
    model.set_equipment_availability(state.id, breaker.id, EquipmentAvailability.OUT_OF_SERVICE)
    current = TopologyEngine().compile(model, state.id)
    result = build(project, operating_state_id=state.id, topology=current)
    facility = next(f for f in result.facilities if f.id == 'train_ktp')
    assert facility.status.code == 'energized' and facility.status.repair
    assert facility.outgoing_in_service == 1
    leaves = [t for t in result.targets.values() if t.kind == 'equipment' and breaker.id in t.equipment_ids]
    assert leaves and all(t.status.repair and t.status.code == 'open' for t in leaves)
    stale = build(project, operating_state_id=state.id, topology=initial)
    assert all(t.status.code == 'unknown' and not t.status.repair for t in stale.targets.values())
    assert all(f.outgoing_in_service is None for f in stale.facilities)
    assert 'stale_topology' in {d.code for d in stale.diagnostics}


def test_wrong_state_snapshot_is_unknown_even_with_same_model(project):
    states = list(project.electrical_model.operating_states)
    snapshot = TopologyEngine().compile(project.electrical_model, states[0])
    result = build(project, operating_state_id=states[1], topology=snapshot)
    assert all(f.status.code == 'unknown' for f in result.facilities)
    assert all(link.status.code == 'unknown' for link in result.links)


def test_blocking_topology_diagnostic_cannot_paint_green_leaves(project):
    model = project.electrical_model
    snapshot = TopologyEngine().compile(model, next(iter(model.operating_states)))
    node = legacy(model.electrical_nodes, 'train_gtes_lv1')
    snapshot = replace(snapshot, diagnostics=(*snapshot.diagnostics,
        TopologyDiagnostic(DiagnosticSeverity.ERROR, 'TEST_BLOCKING', 'Bad topology', electrical_node_id=node.id)))
    result = build(project, topology=snapshot)
    affected = [t for t in result.targets.values() if node.id in t.node_ids and t.kind != 'unassigned']
    assert affected and all(t.status.code == 'error' for t in affected)
    assert all(f.outgoing_in_service is None for f in result.facilities)


@pytest.mark.parametrize('corrupt', [
    lambda m: m.update(schema_version=999),
    lambda m: m.update(facilities='bad'),
    lambda m: m['facilities'][0].update(buses=[]),
    lambda m: m['facilities'][0].update(transformers=[None]),
    lambda m: m['facilities'][0].update(generators=[{'id':'G', 'node_ids':False}]),
    lambda m: m['facilities'][1]['incoming'][0].update(upstream_id={}),
])
def test_malformed_manifest_is_explicit_unassigned_not_exception(project, corrupt):
    corrupt(project.metadata['training_demo'])
    result = build(project)
    assert not result.facilities and not result.links
    assert set(result.unassigned.equipment_ids) == set(project.electrical_model.equipment)
    assert result.diagnostics


def test_changed_electrical_chain_does_not_reuse_stale_manifest_link(project):
    model = project.electrical_model
    original = build(project)
    link = next(link for link in original.links if link.to_facility_id == 'train_ktp')
    replacement = legacy(model.electrical_nodes, 'train_ktp_hv2')
    model.reconnect_port(link.to_port_id, replacement.id)
    result = build(project)
    assert link.id not in {link.id for link in result.links}
    ktp = next(f for f in result.facilities if f.id == 'train_ktp')
    assert ktp.input_sections_distinct is None and not ktp.counts_complete
    assert ktp.incoming_count == 1
    assert 'changed_chain' in {d.code for d in result.diagnostics}


def test_missing_section_and_new_equipment_are_not_guessed_from_name_or_position(project):
    manifest = row(project, 'train_ktp')
    manifest['buses']['hv'][0] = 'missing-existing-id'
    equipment, _ = project.electrical_model.create_equipment('builtin.load', 'train_ktp_hv1')
    snapshot = TopologyEngine().compile(project.electrical_model, next(iter(project.electrical_model.operating_states)))
    result = build(project, topology=snapshot)
    assert equipment.id in result.unassigned.equipment_ids
    f = next(f for f in result.facilities if f.id == 'train_ktp')
    assert f.status.code == 'unknown' and f.status.unknown_sections >= 1
    assert f.status.total_sections == 4 and not f.counts_complete
    assert f.target.status == f.status


def test_stale_voltage_metadata_is_not_presented_as_canonical(project):
    row(project, 'train_ktp')['lv_kv'] = 35
    result = build(project)
    ktp = next(f for f in result.facilities if f.id == 'train_ktp')
    assert ktp.voltage_levels == (10.0, 0.4)
    assert 'changed_level_voltage' in {d.code for d in result.diagnostics}


def test_names_positions_and_duplicate_views_do_not_change_circuit_counts(project):
    before = build(project)
    model = project.electrical_model
    equipment = next(iter(model.equipment.values()))
    model.rename_equipment(equipment.id, 'Произвольное новое имя')
    reps = dict(project.diagram.representations)
    source = next(r for r in reps.values() if r.equipment_id == equipment.id)
    duplicate = replace(source, id=GraphicalRepresentationId.new(), x=source.x+10000, y=source.y-10000)
    reps[duplicate.id] = duplicate
    pages = {pid:replace(page, name='Лист ' + str(i), order=10-i) for i,(pid,page) in enumerate(project.diagram.pages.items())}
    project.diagram = replace(project.diagram, representations=reps, pages=pages)
    after = build(project)
    assert [(f.id,f.transformer_count,f.outgoing_count,f.preferred_page_id) for f in before.facilities] == [
        (f.id,f.transformer_count,f.outgoing_count,f.preferred_page_id) for f in after.facilities]
    assert before.links == after.links
    assert any(t.name == 'Произвольное новое имя' for t in after.targets.values())


def structure_project(*, node_names=None):
    model = ElectricalModel.with_builtins('Независимая структура')
    voltage = VoltageClassId('builtin.voltage.ac.10kv')
    first, last = ElectricalNodeId('A'), ElectricalNodeId('B')
    for nid in (first,last):
        model.add_node(ElectricalNode(nid, (node_names or {}).get(nid.value, nid.value), declared_voltage_class_id=voltage))
    line, _ = model.create_equipment('builtin.line', 'Линия', equipment_id=EquipmentId('line'))
    for role,nid in (('from',first),('to',last)):
        model.connect_port(model.port_by_role(line.id,role).id,nid)
    structure = ProjectStructure()
    structure.add_facility(Facility('fA','ПС A','substation'))
    structure.add_facility(Facility('fB','ПС B','substation',parent_id='fA'))
    structure.add_equipment(Equipment('physical-line','Общая линия','line',[CalculationRef('branch',line.id.value)]))
    for letter,nid,role,kind in (('A',first,'from','outgoing'),('B',last,'to','incoming')):
        structure.add_voltage_level(VoltageLevel('v'+letter,'f'+letter,'РУ '+letter,10))
        structure.add_bus_section(BusSection('s'+letter,'v'+letter,'Секция '+letter,nid.value))
        structure.add_bay(Bay('b'+letter,'v'+letter,'Ячейка '+letter,kind,'s'+letter))
        structure.place_equipment(EquipmentPlacement('p'+letter,'physical-line','b'+letter,role))
    return SimpleNamespace(electrical_model=model, structure=structure, metadata={},
        diagram=DiagramDocument(DiagramDocumentId('diagram'),'Пустой чертёж'))


def test_structure_hierarchy_and_shared_line_port_sides_are_exact():
    project = structure_project()
    result = build(project)
    assert result.source == 'structure' and not result.diagnostics
    assert len(result.links) == 1
    link = result.links[0]
    assert (link.from_facility_id,link.to_facility_id) == ('fA','fB')
    assert result.facility_ids_by_equipment[EquipmentId('line')] == ('fA','fB')
    assert [t.kind for t in result.targets['level:vA'].children] == ['section']
    assert result.targets['section:sA'].children[0].key == 'bay:bA'
    assert result.targets['bay:bA'].children[0].port_ids == (link.from_port_id,)
    assert len(result.roots) == 1 and result.roots[0].children[0].key == 'facility:fB'
    assert [f.outgoing_count for f in result.facilities] == [1,0]


def test_ambiguous_structure_terminal_is_not_arbitrarily_assigned():
    project = structure_project()
    for placement in project.structure.placements.values():
        placement.terminal = ''
    result = build(project)
    assert not result.links
    assert 'ambiguous_line_sides' in {d.code for d in result.diagnostics}
    assert all(not facility.counts_complete for facility in result.facilities)


def test_unmapped_bay_has_explicit_unassigned_section_and_unknown_count():
    project = structure_project()
    project.structure.bays['bA'].bus_section_id = None
    result = build(project)
    bay = result.targets['bay:bA']
    assert bay.status.code == 'unknown'
    assert result.targets['level:vA:unassigned'].children == (bay,)
    assert result.facilities[0].unassigned_count == 1
    assert not result.facilities[0].counts_complete


def test_structure_cycle_does_not_recurse_or_guess_parent():
    project = structure_project()
    project.structure.facilities['fA'].parent_id = 'fB'
    result = build(project)
    assert not result.facilities
    assert result.unassigned.equipment_ids == (EquipmentId('line'),)
    assert 'invalid_structure' in {d.code for d in result.diagnostics}


def test_no_structure_or_manifest_leaves_all_equipment_unassigned(project):
    project.metadata.clear()
    result = build(project)
    assert not result.facilities and not result.links
    assert result.roots == (result.unassigned,)
    assert set(result.unassigned.equipment_ids) == set(project.electrical_model.equipment)


@pytest.mark.parametrize('group,expected_count', [('loads',2),('incoming',2)])
def test_same_physical_feeder_under_new_manifest_id_is_not_double_counted(project, group, expected_count):
    facility = row(project, 'train_ktp')
    duplicate = deepcopy(facility[group][0])
    duplicate['id'] += '_another_label'
    facility[group].append(duplicate)
    result = build(project)
    ktp = next(f for f in result.facilities if f.id == 'train_ktp')
    assert (ktp.outgoing_count if group == 'loads' else ktp.incoming_count) == expected_count
    assert len(result.links) == 6
    assert not ktp.counts_complete
    assert {'duplicate_feeder','duplicate_incoming'} & {d.code for d in result.diagnostics}


def test_three_winding_multiple_calculation_legs_remain_one_physical_transformer():
    project = structure_project()
    model, structure = project.electrical_model, project.structure
    transformer, _ = model.create_equipment('builtin.transformer_3w', 'T3',
        equipment_id=EquipmentId('physical-T3'),
        extensions={'legacy_calculation': {'schema_version':1, 'legacy_id':'old-T3'}})
    structure.add_equipment(Equipment('t3-record','Трансформатор 3W','power_transformer',[
        CalculationRef('branch','old-T3'), CalculationRef('branch','old-T3_mv'), CalculationRef('branch','old-T3_lv')]))
    for n,role in enumerate(('hv','mv','lv')):
        structure.add_bay(Bay('t3-bay'+role,'vA',role,'transformer','sA'))
        structure.place_equipment(EquipmentPlacement('t3-place'+role,'t3-record','t3-bay'+role,role))
    result = build(project)
    first = result.facilities[0]
    assert first.transformer_count == 1
    for role in ('hv','mv','lv'):
        leaf = result.targets['bay:t3-bay'+role].children[0]
        assert leaf.equipment_ids == (transformer.id,)
        assert leaf.port_ids == (model.port_by_role(transformer.id,role).id,)
    assert result.facility_ids_by_equipment[transformer.id] == ('fA',)


def test_node_calculation_ref_keeps_bus_target_without_invented_equipment():
    project = structure_project()
    project.structure.add_equipment(Equipment('bus-record','Реальная секция','busbar',[CalculationRef('node','A')]))
    project.structure.add_bay(Bay('bus-bay','vA','Секция','busbar','sA'))
    project.structure.place_equipment(EquipmentPlacement('bus-place','bus-record','bus-bay'))
    result = build(project)
    target = result.targets['placement:bus-place']
    assert target.node_ids == (ElectricalNodeId('A'),)
    assert target.equipment_ids == ()
    assert result.facilities[0].outgoing_count == 1


def test_line_voltage_uses_actual_terminal_stage_not_facility_highest_ru():
    project = structure_project()
    node = ElectricalNode(ElectricalNodeId('HV'), '110 кВ',
                          declared_voltage_class_id=VoltageClassId('builtin.voltage.ac.110kv'))
    project.electrical_model.add_node(node)
    project.structure.add_voltage_level(VoltageLevel('hv-level','fB','РУ 110 кВ',110))
    project.structure.add_bus_section(BusSection('hv-section','hv-level','ВН',node.id.value))
    result = build(project)
    assert result.facilities[1].voltage_levels == (110.0,10.0)
    assert result.links[0].nominal_kv == 10.0


def test_section_anchor_does_not_include_child_nodes_or_depend_on_names():
    project = structure_project()
    # One attached apparatus exposes both nodes, as a coupler can do. The
    # navigation subtree must not turn its remote end into this section's bus.
    project.structure.placements['pA'].terminal = ''
    before = build(project).targets['section:sA']
    assert before.node_ids == (ElectricalNodeId('A'), ElectricalNodeId('B'))
    assert before.anchor_node_ids == (ElectricalNodeId('A'),)
    renamed = structure_project(node_names={'A':'Имя узла другое', 'B':'Секция A'})
    renamed.structure.placements['pA'].terminal = ''
    renamed.structure.bus_sections['sA'].name = 'Новое имя секции'
    after = build(renamed).targets['section:sA']
    assert after.name != before.name
    assert after.anchor_node_ids == before.anchor_node_ids
    assert after.node_ids == before.node_ids
    assert overview.OverviewTarget('old-call', 'section', 'Compatibility').anchor_node_ids == ()


@pytest.mark.parametrize('terminal', ['', 'from'])
def test_repeated_structure_feeder_placement_does_not_invent_second_circuit(terminal):
    project = structure_project()
    project.structure.placements['pA'].terminal = terminal
    project.structure.add_bay(Bay('duplicate-bay','vA','Ещё одно изображение','outgoing','sA'))
    project.structure.place_equipment(EquipmentPlacement('duplicate-place','physical-line','duplicate-bay',terminal))
    if terminal:
        # Existing structure validation already rejects an explicit repeated
        # terminal. The new guard closes only its permitted blank-side case.
        assert project.structure.validate()
        result = build(project)
        assert not result.facilities and not result.links
        assert result.unassigned.equipment_ids == (EquipmentId('line'),)
        assert 'invalid_structure' in {d.code for d in result.diagnostics}
        return
    assert project.structure.validate() == []
    result = build(project)
    facility = result.facilities[0]
    assert facility.outgoing_count == 1
    assert not facility.counts_complete and facility.outgoing_in_service is None
    assert 'duplicate_feeder' in {d.code for d in result.diagnostics}
    # Both explicit structure rows remain navigable; only the physical count deduplicates.
    assert 'bay:duplicate-bay' in result.targets and 'bay:bA' in result.targets
    assert result.targets['bay:duplicate-bay'].equipment_ids == (EquipmentId('line'),)
