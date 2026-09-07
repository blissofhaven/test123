"""A protection review blocks calculation, never ordinary project integrity."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from rza_calc.calculation import CalculationProjectionBuilder
from rza_calc.domain import CalculationRef, Equipment
from rza_calc.domain.diagram import DiagramDocumentId
from rza_calc.domain.electrical import (
    ElectricalNode, ElectricalNodeId, LineKind, VoltageClassId, thaw_json,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.io.project import load_project, save_project


EXAMPLE = Path(__file__).parents[1] / "tests/fixtures/legacy_projects/ps_severnaya.json"
MARKER = "rza_calc.protection_zone_review"
ZONE_CODE = "line_protection_zone_review_required"


def _project(*, marked=True, incomplete=False):
    project = load_project(EXAMPLE)
    model = project.electrical_model
    if marked:
        line = next(
            row for row in model.equipment.values()
            if model.equipment_type(row.type_id, row.type_version).behavior_key == "legacy.line"
            and row.properties["legacy_payload"].get("ct_ratio")
        )
        extensions = thaw_json(line.extensions)
        extensions[MARKER] = {"required": True, "reason": "Нужно подтвердить зону защиты."}
        model._equipment[line.id] = replace(line, extensions=extensions)
    if incomplete:
        voltage = VoltageClassId("builtin.voltage.ac.10kv")
        nodes = tuple(ElectricalNode(
            ElectricalNodeId(f"node.zone-review.{name}"), name,
            declared_voltage_class_id=voltage,
        ) for name in ("start", "finish"))
        for node in nodes:
            model.add_node(node)
        model.create_logical_line(
            "Линия без подтверждённых сопротивлений", LineKind.OVERHEAD,
            nodes[0].id, nodes[1].id, 12_000, voltage_class_id=voltage,
        )
        codes = {item.code for item in project.calculation_blockers}
        assert "line_data_unconfirmed" in codes
        assert (ZONE_CODE in codes) is marked
    return project


def _seed(tmp_path, project):
    path = tmp_path / "seed.json"
    save_project(path, project)
    return json.loads(path.read_text(encoding="utf-8"))


def _write(tmp_path, raw):
    path = tmp_path / "incoming.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _missing_reference():
    return Equipment("physical.review", "Проверяемая связь", "other", [
        CalculationRef("branch", "MISSING_BRANCH"),
    ])


def _calculation_node_id(project):
    model = project.electrical_model
    state = sorted(model.operating_states, key=lambda item: item.value)[0]
    return next(iter(CalculationProjectionBuilder().build(model, state).nodes)).value


@pytest.mark.parametrize("marked", (False, True))
def test_missing_branch_reference_rejected_on_load(tmp_path, marked):
    raw = _seed(tmp_path, _project(marked=marked))
    raw["structure"]["equipment"].append({
        "id": "physical.review", "name": "Проверяемая связь", "kind": "other",
        "calculation_refs": [{"kind": "branch", "object_id": "MISSING_BRANCH"}],
    })
    with pytest.raises(ValueError, match="MISSING_BRANCH"):
        load_project(_write(tmp_path, raw))


@pytest.mark.parametrize("marked", (False, True))
def test_missing_branch_reference_rejected_before_save(tmp_path, monkeypatch, marked):
    project = _project(marked=marked)
    project.structure.add_equipment(_missing_reference())
    monkeypatch.setattr("rza_calc.io.project._write_json", lambda *_: pytest.fail("Invalid structure reached writer"))
    with pytest.raises(ValueError, match="MISSING_BRANCH"):
        save_project(tmp_path / "must-not-exist.json", project)
    assert not (tmp_path / "must-not-exist.json").exists()


@pytest.mark.parametrize("marked", (False, True))
def test_derived_id_collision_rejected_on_load(tmp_path, marked):
    project = _project(marked=marked)
    collision = _calculation_node_id(project)
    raw = _seed(tmp_path, project)
    raw["diagram"]["id"] = collision
    with pytest.raises(ValueError, match="используется одновременно"):
        load_project(_write(tmp_path, raw))


@pytest.mark.parametrize("marked", (False, True))
def test_derived_id_collision_rejected_before_save(tmp_path, monkeypatch, marked):
    project = _project(marked=marked)
    project.diagram = replace(project.diagram, id=DiagramDocumentId(_calculation_node_id(project)))
    monkeypatch.setattr("rza_calc.io.project._write_json", lambda *_: pytest.fail("ID collision reached writer"))
    with pytest.raises(ValueError, match="используется одновременно"):
        save_project(tmp_path / "must-not-exist.json", project)
    assert not (tmp_path / "must-not-exist.json").exists()


def test_valid_zone_blocked_roundtrip_preserves_complete_graph_and_marker(tmp_path):
    project = _project()
    project.structure.add_equipment(Equipment(
        "physical.valid", "Исходная ветвь", "other", [CalculationRef("branch", "S")],
    ))
    fingerprint = electrical_model_fingerprint(project.electrical_model)
    counts = tuple(len(getattr(project.network, name)) for name in ("nodes", "branches", "loads"))
    path = tmp_path / "review-pending.json"
    save_project(path, project)
    reopened = load_project(path)
    assert electrical_model_fingerprint(reopened.electrical_model) == fingerprint
    assert tuple(len(getattr(reopened.network, name)) for name in ("nodes", "branches", "loads")) == counts
    assert reopened.structure.validate(reopened.network) == []
    assert {item.code for item in reopened.calculation_blockers} == {ZONE_CODE}
    with pytest.raises(ValueError, match="зону защиты"):
        reopened.require_calculation_ready()


@pytest.mark.parametrize("marked", (False, True))
def test_incomplete_or_mixed_blocker_rejects_duplicate_physical_reference_on_load(tmp_path, marked):
    raw = _seed(tmp_path, _project(marked=marked, incomplete=True))
    reference = {"kind": "branch", "object_id": "S"}
    raw["structure"]["equipment"].append({
        "id": "physical.duplicate", "name": "Повторная связь", "kind": "other",
        "calculation_refs": [reference, reference],
    })
    with pytest.raises(ValueError, match="повторяется связь"):
        load_project(_write(tmp_path, raw))


@pytest.mark.parametrize("marked", (False, True))
def test_incomplete_or_mixed_blocker_rejects_duplicate_physical_reference_before_save(tmp_path, monkeypatch, marked):
    project = _project(marked=marked, incomplete=True)
    reference = CalculationRef("branch", "S")
    project.structure.add_equipment(Equipment(
        "physical.duplicate", "Повторная связь", "other", [reference, reference],
    ))
    monkeypatch.setattr("rza_calc.io.project._write_json", lambda *_: pytest.fail("Invalid structure reached writer"))
    with pytest.raises(ValueError, match="повторяется связь"):
        save_project(tmp_path / "must-not-exist.json", project)


def test_mixed_blocker_does_not_claim_calculation_reference_completeness(tmp_path):
    project = _project(incomplete=True)
    project.structure.add_equipment(_missing_reference())
    path = tmp_path / "incomplete.json"
    save_project(path, project)
    reopened = load_project(path)
    assert reopened.structure.validate() == []
    assert {item.code for item in reopened.calculation_blockers} == {ZONE_CODE, "line_data_unconfirmed"}
    with pytest.raises(ValueError):
        reopened.require_calculation_ready()
