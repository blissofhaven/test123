"""A legacy line split may be saved, but cannot weaken its protection zone."""
from dataclasses import replace
from pathlib import Path

import pytest

from rza_calc import cli
from rza_calc.core.engine import run
from rza_calc.domain.electrical import thaw_json
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import load, load_project, save_project

EXAMPLE = Path(__file__).parents[1] / 'tests/fixtures/legacy_projects/ps_severnaya.json'
MARKER = 'rza_calc.protection_zone_review'
CODE = 'line_protection_zone_review_required'
REASON = 'После создания отпайки требуется подтвердить зону защиты исходной линии.'


def mark(controller, value):
    row = next(e for e in controller.model.equipment.values()
               if controller.model.equipment_type(e.type_id, e.type_version).behavior_key == 'legacy.line'
               and e.properties['legacy_payload'].get('ct_ratio'))
    def command(draft):
        source = draft.electrical_model.equipment[row.id]
        extensions = thaw_json(source.extensions)
        extensions[MARKER] = value
        draft.electrical_model._equipment[row.id] = replace(source, extensions=extensions)
    controller._execute('Требуется уточнить зону после отпайки', command)
    return row


def test_zone_review_hides_old_results_and_survives_roundtrip_undo_cli(tmp_path, monkeypatch, capsys):
    vm = ProjectViewModel.open(EXAMPLE)
    controller = ProjectEditorController(vm.project)
    result = vm.current_result
    assert result is not None
    original_fp = electrical_model_fingerprint(controller.model)
    original = mark(controller, {'required': True, 'reason': REASON,
        'original_to_node_id': 'original-end', 'original_length_km': 2,
        'split_node_ids': ['new-tap']})
    assert controller.model.equipment[original.id].properties == original.properties
    assert vm.current_result is None
    assert any(d.code == CODE and d.object_id == original.id.value
               for d in vm.project.calculation_blockers)
    with pytest.raises(ValueError, match='зону защиты'):
        vm.project.require_calculation_ready()
    with pytest.raises(ValueError, match='зону защиты'):
        run(vm.project.network, vm.project.methodology)
    monkeypatch.setattr('rza_calc.gui.view_model.run', lambda *_: pytest.fail('Blocked data reached the solver'))
    assert vm.recalculate() is False
    assert 'зону защиты' in vm.calculation_error
    assert vm.setting_rows() == []
    assert 'зону защиты' in vm.report_text()
    path = tmp_path / 'line-with-pending-zone.json'
    save_project(path, vm.project)
    restored = load_project(path)
    assert restored.electrical_model.equipment[original.id].properties == original.properties
    assert any(d.code == CODE for d in restored.calculation_blockers)
    with pytest.raises(ValueError, match='зону защиты'):
        load(path)
    assert cli.main([str(path), 'report']) == cli.EXIT_INPUT_ERROR
    assert 'зону защиты' in capsys.readouterr().out
    controller.undo()
    assert electrical_model_fingerprint(controller.model) == original_fp
    assert not vm.project.calculation_blockers
    assert result.is_current_for(vm.net, vm.project.methodology)
    controller.redo()
    assert any(d.code == CODE for d in vm.project.calculation_blockers)


@pytest.mark.parametrize('value', (None, False, {}, {'required': False}, {'required': True, 'reason': ''}))
def test_marker_cannot_be_cleared_by_a_false_or_malformed_flag(value):
    project = load_project(EXAMPLE)
    controller = ProjectEditorController(project)
    mark(controller, value)
    assert any(d.code == CODE for d in project.calculation_blockers)
    with pytest.raises(ValueError, match='зону защиты'):
        run(project.network, project.methodology)
