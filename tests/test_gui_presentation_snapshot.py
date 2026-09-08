"""A render may share reads; no render may reuse yesterday's electrical data."""
from dataclasses import replace
from pathlib import Path

import pytest

from rza_calc.domain.electrical import thaw_json
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import load_project

EXAMPLE = Path(__file__).parents[1] / "tests/fixtures/legacy_projects/ps_severnaya.json"


@pytest.fixture
def vm():
    return ProjectViewModel.open(EXAMPLE)


def edit_load(vm, *, through_controller=False):
    model = vm.project.electrical_model
    load = next(row for row in model.equipment.values()
                if "p_kw" in row.properties.get("legacy_payload", {}))
    payload = thaw_json(load.properties["legacy_payload"])
    payload["p_kw"] += 123
    if through_controller:
        ProjectEditorController(vm.project).set_equipment_property(load.id, "legacy_payload", payload)
    else:
        # Deliberately bypass revision bump: the next render must use full FP.
        properties = dict(load.properties)
        properties["legacy_payload"] = payload
        model._equipment[load.id] = replace(load, properties=properties)


def test_one_nested_render_checks_inputs_once_and_rechecks_after_exception(vm, monkeypatch):
    import rza_calc.io.project as project_module
    original = project_module.electrical_model_fingerprint
    calls = []
    monkeypatch.setattr(project_module, "electrical_model_fingerprint",
                        lambda model: (calls.append(model), original(model))[1])
    result = vm.result
    with pytest.raises(RuntimeError, match="render failure"):
        with vm.presentation_snapshot():
            before = len(calls)
            for _ in range(5):
                with vm.presentation_snapshot():
                    assert vm.current_result is result
                    assert vm.net is vm.net
                    vm.fault_rows()
                    vm.warnings()
            assert len(calls) == before
            raise RuntimeError("render failure")
    assert not vm.__dict__.get("_presentation")
    assert vm.__dict__.get("_presentation_depth") == 0
    before = len(calls)
    with vm.presentation_snapshot():
        assert vm.current_result is result
    assert len(calls) > before


def test_external_edit_without_revision_bump_is_stale_on_next_render(vm):
    with vm.presentation_snapshot():
        before = vm.net
        assert vm.current_result is vm.result
    revision = vm.project.electrical_model.revision
    edit_load(vm)
    assert vm.project.electrical_model.revision == revision
    with vm.presentation_snapshot():
        assert vm.net is not before
        assert vm.current_result is None
        assert "устар" in vm.report_text().lower()
    assert vm.current_result is None


@pytest.mark.parametrize("action", ("mode", "generator", "branch", "recalculate", "controller_edit", "project"))
def test_reentrant_mutation_discards_presentation_input_and_result(vm, action):
    if action == "generator":
        vm = ProjectViewModel.open(EXAMPLE.with_name("gtes_sever.json"))
    with vm.presentation_snapshot():
        before = vm.net
        old_result = vm.result
        if action == "mode":
            vm.select_mode(next(key for key in before.modes if key != vm.mode_id))
        elif action == "generator":
            generator = next(iter(vm.generators()))
            vm.set_generator_enabled(generator.branch_id, not generator.enabled)
        elif action == "branch":
            branch = next(row for row in before.branches.values() if row.switchable)
            vm.set_branch_enabled(branch.id, not vm.is_branch_closed(branch.id))
        elif action == "recalculate":
            assert vm.recalculate()
            assert vm.current_result is vm.result and vm.result is not old_result
        elif action == "controller_edit":
            edit_load(vm, through_controller=True)
        else:
            other = load_project(EXAMPLE)
            vm.project = other
        # Read itself detects project/controller/result changes, too.
        current = vm.current_result
        assert not vm.__dict__.get("_presentation")
        if action in ("generator", "branch", "controller_edit"):
            assert current is None
        if action == "project":
            assert vm.net is not before
    assert not vm.__dict__.get("_presentation")


def test_canonical_mode_edit_is_stale_on_next_render(vm):
    with vm.presentation_snapshot():
        network = vm.net
        assert vm.current_result is not None
    mode = vm.selected_operating_mode()
    draft = vm.mode_controller.operating_mode_draft(mode.state_id)
    preview = vm.mode_controller.preview_operating_mode(replace(draft, description='Новое описание режима'))
    vm.mode_controller.apply_operating_mode_preview(preview)
    with vm.presentation_snapshot():
        assert vm.current_result is None


@pytest.mark.parametrize("replacement", ("electrical_model", "methodology"))
def test_input_object_replacement_within_same_project_discards_render(vm, replacement):
    from copy import deepcopy
    from types import SimpleNamespace
    with vm.presentation_snapshot():
        old_revision = vm.project.electrical_model.revision
        assert vm.current_result is not None
        if replacement == "electrical_model":
            other = load_project(EXAMPLE)
            edit_load(SimpleNamespace(project=other))
            vm.project.electrical_model = other.electrical_model
        else:
            methodology = deepcopy(vm.project.methodology)
            methodology.data["mtz"]["k_ots"]["value"] += 0.5
            vm.project.methodology = methodology
        assert vm.project.electrical_model.revision == old_revision
        assert vm.current_result is None
        assert not vm.__dict__.get("_presentation")


def test_lightweight_legacy_project_keeps_per_read_guards_inside_nested_render():
    from test_four_fault_ui_cli import make_vm
    vm, _ = make_vm()
    result = vm.result
    original = result.is_current_for
    calls = []
    result.is_current_for = lambda net, method: (calls.append((net, method)), original(net, method))[1]
    with vm.presentation_snapshot():
        assert vm.current_result is result
        with vm.presentation_snapshot():
            assert vm.current_result is result
        vm.project.methodology = object()
        assert vm.current_result is None
    assert len(calls) == 3
    assert not vm.__dict__.get("_presentation")
    assert vm.__dict__.get("_presentation_depth", 0) == 0


def test_canonical_render_can_replace_project_with_lightweight_client(vm):
    from test_four_fault_ui_cli import make_vm
    legacy, _ = make_vm()
    with vm.presentation_snapshot():
        assert vm.current_result is vm.result
        vm.project = legacy.project
        vm.result = legacy.result
        with vm.presentation_snapshot():
            assert vm.current_result is legacy.result
        assert not vm.__dict__.get("_presentation")
    assert vm.__dict__.get("_presentation_depth", 0) == 0
