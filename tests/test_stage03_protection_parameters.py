"""Draft protection inputs cannot become apparently checked settings."""
from copy import deepcopy

import pytest

from rza_calc.core.context import Context
from rza_calc.core.engine import run
from rza_calc.core.fault_types import FaultSpec
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, LineBranch, Load, Mode, Network, Node, SourceBranch, ProtectionSettings
from rza_calc.core.protections.mtz import calc_mtz
from rza_calc.core.protections.to import calc_to
from rza_calc.core.protections.ozz import calc_ozz
from rza_calc.core.result import UNRESOLVED
from test_stage01_to_integrity import _own_transformer


def _network():
    net=Network("Protection inputs")
    for key in ("a","b","c"):
        net.add_node(Node(key,key,10))
    net.add_branch(SourceBranch("S","S",GRID,"a",s_kz_max=200,s_kz_min=150,
        r2_ohm=0,x2_ohm=1,r0_ohm=0,x0_ohm=1,sequence_reference_kv=10.5,
        zero_sequence_connection="series"))
    for key,node in (("F","b"),("Other","c")):
        net.add_branch(LineBranch(key,key,"a",node,length_km=1,r0=.1,x0=.1,ic_per_km=5,
            r2_ohm_per_km=.1,x2_ohm_per_km=.1,r0_ohm_per_km=.2,x0_ohm_per_km=.2,
            ct_ratio=(100,5),ct_node="a",prot=ProtectionSettings(mtz=True,to=True,ozz=True,i_scale=[1,5,50,500,1000])))
        net.add_load(Load("load"+key,"load"+key,node,p_kw=50,cos_phi=.9))
    net.add_mode(Mode("max","Max",system="max"))
    return net


def _record(obj,key,confirmed=False):
    obj.parameter_provenance[key]={"confirmation":"confirmed" if confirmed else "unconfirmed",
        "source":"Паспорт"}


@pytest.mark.parametrize("key",["ct_primary_a","ct_secondary_a","ct_port"])
@pytest.mark.parametrize("kind,calculate",[("МТЗ",calc_mtz),("ТО",calc_to)])
def test_unconfirmed_ct_blocks_direct_and_project_protection_but_not_any_fault(key,kind,calculate):
    net=_network();methodology=Methodology.load();branch=net.branches["F"]
    original=calculate(Context(net,methodology),branch)
    assert original.i_primary is not None
    _record(branch,key)
    ctx=Context(net,methodology)
    for fault in ("3ph","2ph","1ph_g","2ph_g"):
        assert ctx.solvers["max"].fault_at("b",FaultSpec(fault))
    result=calculate(ctx,branch)
    assert result.status==UNRESOLVED and result.i_primary is None
    assert key in result.explain()
    full=run(net,methodology)
    assert full.get("F",kind).i_primary is None
    assert full.get("Other",kind).i_primary is not None
    _record(branch,key,True)
    assert calculate(Context(net,methodology),branch).i_primary==original.i_primary


@pytest.mark.parametrize("key",["p_kw","cos_phi","k_use"])
def test_unconfirmed_load_blocks_only_its_mtz_then_confirmation_restores_number(key):
    net=_network();methodology=Methodology.load()
    original=run(net,methodology).get("F","МТЗ").i_primary
    _record(net.loads["loadF"],key)
    project=run(net,methodology)
    result=project.get("F","МТЗ")
    assert result.i_primary is None and result.status==UNRESOLVED
    assert key in result.explain() and "loadF" in result.explain()
    assert project.get("Other","МТЗ").i_primary is not None
    assert project.get("F","ТО").i_primary is not None
    _record(net.loads["loadF"],key,True)
    assert run(net,methodology).get("F","МТЗ").i_primary==original


def test_unconfirmed_capacitance_blocks_group_ozz_only():
    net=_network();methodology=Methodology.load()
    baseline=run(net,methodology).get("F","ОЗЗ").i_primary
    _record(net.branches["Other"],"capacitive_current_a_per_km")
    result=run(net,methodology)
    assert result.get("F","ОЗЗ").i_primary is None
    assert result.get("Other","ОЗЗ").i_primary is None
    assert result.get("F","МТЗ").i_primary is not None
    _record(net.branches["Other"],"capacitive_current_a_per_km",True)
    assert run(net,methodology).get("F","ОЗЗ").i_primary==baseline


def test_inrush_needs_confirmation_only_when_current_crosses_ct():
    methodology=Methodology.load()
    net=_own_transformer(ct_side="hv")
    branch=net.branches["T"]
    baseline=calc_to(Context(net,methodology),branch).i_primary
    _record(branch,"i_inrush_ratio")
    result=calc_to(Context(net,methodology),branch)
    assert result.i_primary is None and "i_inrush_ratio" in result.explain()
    _record(branch,"i_inrush_ratio",True)
    assert calc_to(Context(net,methodology),branch).i_primary==baseline
    net=_own_transformer(ct_side="lv");branch=net.branches["T"]
    _record(branch,"i_inrush_ratio")
    assert calc_to(Context(net,methodology),branch).i_primary is not None


def test_unavailable_load_and_unused_nameplate_do_not_block_other_inputs():
    net=_network();methodology=Methodology.load()
    for key in ("ct_accuracy","terminal","breaker_t_off","z_loop"):
        _record(net.branches["F"],key)
    for key in ("k_szp","motor_share"):
        _record(net.loads["loadF"],key)
    _record(net.loads["loadOther"],"p_kw")
    net.modes["max"].availability["loadOther"]=False
    result=run(net,methodology)
    assert result.get("F","МТЗ").i_primary is not None
    assert result.get("F","ТО").i_primary is not None


def test_draft_on_other_island_does_not_affect_local_ozz():
    net=_network();methodology=Methodology.load()
    net.add_node(Node("island","island",10))
    net.add_branch(SourceBranch("separate","separate",GRID,"island",s_kz_max=200,s_kz_min=150))
    net.add_node(Node("island_end","island end",10))
    remote=LineBranch("remote","remote","island","island_end",length_km=1,r0=.1,x0=.1,ic_per_km=7)
    net.add_branch(remote);_record(remote,"capacitive_current_a_per_km")
    assert run(net,methodology).get("F","ОЗЗ").i_primary is not None


def test_real_card_load_provenance_roundtrip_undo_and_explicit_confirmation(tmp_path):
    from rza_calc.adapters.legacy_calculation import import_legacy_network
    from rza_calc.domain import ProjectStructure
    from rza_calc.domain.electrical import DataConfirmation
    from rza_calc.editor import ProjectEditorController
    from rza_calc.editor.parameter_editing import ParameterPatch, ParameterValue
    from rza_calc.io.project import FORMAT_VERSION, ProjectData, save_project, load_project
    net=_network();methodology=Methodology.load()
    project=ProjectData(net,methodology,{},ProjectStructure(),FORMAT_VERSION,import_legacy_network(net))
    c=ProjectEditorController(project)
    eid=next(e.id for e in c.model.equipment.values() if e.name=="loadF")
    before=run(project.network,methodology).get("F","МТЗ").i_calc
    preview=c.preview_parameter_patch((ParameterPatch(eid,{"p_kw":ParameterValue(60)}),))
    assert preview.valid,preview.diagnostics
    c.apply_parameter_preview(preview)
    assert project.network.loads["loadF"].p_kw==60
    assert project.network.loads["loadF"].parameter_provenance["p_kw"]["confirmation"]=="unconfirmed"
    assert run(project.network,methodology).get("F","МТЗ").i_primary is None
    saved=tmp_path / "load-draft.json";save_project(saved,project)
    loaded=load_project(saved)
    assert run(loaded.network,loaded.methodology).get("F","МТЗ").i_primary is None
    for kind in ("3ph","2ph","1ph_g","2ph_g"):
        assert Context(loaded.network,loaded.methodology).solvers["max"].fault_at("b",FaultSpec(kind))
    c.undo();assert run(project.network,methodology).get("F","МТЗ").i_calc==before
    c.redo()
    preview=c.preview_parameter_patch((ParameterPatch(eid,{"p_kw":ParameterValue(60,"Паспорт нагрузки",DataConfirmation.CONFIRMED)}),))
    assert preview.valid,preview.diagnostics
    c.apply_parameter_preview(preview)
    result=run(project.network,methodology).get("F","МТЗ")
    assert result.i_primary is not None and result.i_calc==pytest.approx(before*1.2)


def test_mutable_context_does_not_reuse_an_old_confirmation_index():
    net=_network();ctx=Context(net,Methodology.load());branch=net.branches["F"]
    assert calc_mtz(ctx,branch).i_primary is not None
    _record(net.loads["loadF"],"p_kw")
    assert calc_mtz(ctx,branch).i_primary is None
