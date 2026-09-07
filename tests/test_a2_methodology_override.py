"""A2.1: explicit passport methodology does not bypass project validation."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rza_calc.core.fingerprint import methodology_fingerprint, network_fingerprint
from rza_calc.core.methodology import Methodology
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.io.project import ProjectFormatError, load, load_project, save_project


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests/fixtures/legacy_projects/ps_severnaya.json"


def _write_project(tmp_path, *, name="project.json", raw=None):
    if raw is None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["methodology"] = {"file": "external-methodology.json"}
    path = tmp_path / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _external_profile(tmp_path, state):
    path = tmp_path / "external-methodology.json"
    if state == "corrupt":
        path.write_text("{not valid JSON", encoding="utf-8")
    elif state == "changed":
        changed = Methodology.load()
        changed.data["mtz"]["k_ots"]["value"] = 9.99
        changed.save(path)
    else:
        assert state == "missing"
    return path


@pytest.mark.parametrize("api", [load_project, load])
@pytest.mark.parametrize("external_state", ["missing", "corrupt", "changed"])
def test_explicit_override_never_reads_external_methodology(
    tmp_path, monkeypatch, api, external_state,
):
    source = _write_project(tmp_path)
    _external_profile(tmp_path, external_state)
    override = Methodology.load()
    expected = methodology_fingerprint(override)

    def no_file_load(cls, path=None):
        pytest.fail("An explicit override must not read any methodology file")

    monkeypatch.setattr(Methodology, "load", classmethod(no_file_load))
    result = api(source, methodology_override=override)
    restored = result.methodology if api is load_project else result[1]
    assert methodology_fingerprint(restored) == expected
    assert restored is not override
    assert restored.path is None
    if api is load_project:
        assert result.methodology_ref == override.data
        assert "file" not in result.methodology_ref


@pytest.mark.parametrize("api", [load_project, load])
@pytest.mark.parametrize("external_state", ["missing", "corrupt"])
def test_without_override_external_file_errors_are_preserved(tmp_path, api, external_state):
    source = _write_project(tmp_path)
    _external_profile(tmp_path, external_state)
    expected_error = FileNotFoundError if external_state == "missing" else json.JSONDecodeError
    with pytest.raises(expected_error):
        api(source)
    with pytest.raises(expected_error):
        api(source, methodology_override=None)


@pytest.mark.parametrize("api", [load_project, load])
def test_without_override_current_external_profile_is_still_loaded(tmp_path, api):
    source = _write_project(tmp_path)
    external = _external_profile(tmp_path, "changed")
    result = api(source)
    methodology = result.methodology if api is load_project else result[1]
    assert methodology.k("mtz.k_ots") == 9.99
    assert methodology.path == external


def test_override_caller_restored_profile_and_inline_reference_are_independent(tmp_path):
    source = _write_project(tmp_path)
    override = Methodology.load()
    initial = deepcopy(override.data)
    project = load_project(source, methodology_override=override)

    override.data["mtz"]["k_ots"]["value"] = 9.99
    override.data["references"]["ПУЭ"] = "caller changed"
    assert project.methodology.data == initial
    assert project.methodology_ref == initial

    project.methodology.data["mtz"]["k_ots"]["value"] = 8.88
    assert project.methodology_ref == initial
    project.methodology_ref["references"]["ПУЭ"] = "reference changed"
    assert project.methodology.references()["ПУЭ"] == initial["references"]["ПУЭ"]


@pytest.mark.parametrize("invalid", ["mapping", "number", "empty", "missing", "choice", "nan", "container"])
def test_malformed_override_is_rejected_before_it_can_be_used(tmp_path, invalid):
    source = _write_project(tmp_path)
    override = Methodology.load()
    if invalid == "mapping":
        override = override.data
    elif invalid == "number":
        override = 7
    elif invalid == "empty":
        override = Methodology.from_dict({})
    elif invalid == "missing":
        del override.data["mtz"]["k_ots"]
    elif invalid == "choice":
        override.data["to"]["i_nom_basis"].update(value="unknown", options=["unknown"])
    elif invalid == "nan":
        override.data["custom_metadata"] = float("nan")
    elif invalid == "container":
        override.data["ct"] = None
    with pytest.raises(ProjectFormatError, match="methodology_override"):
        load_project(source, methodology_override=override)


def test_override_rejects_reserved_root_file_instead_of_losing_it_on_save(tmp_path):
    source = _write_project(tmp_path)
    override = Methodology.load()
    override.data["file"] = "arbitrary-profile-metadata.json"
    # The profile itself is valid and its passport may retain unknown fields,
    # but this key cannot be represented as an inline project methodology.
    assert override.blocking_errors() == []
    assert override.snapshot().to_methodology().data["file"] == override.data["file"]
    with pytest.raises(ProjectFormatError, match="зарезервированное корневое поле 'file'"):
        load_project(source, methodology_override=override)
    assert override.data["file"] == "arbitrary-profile-metadata.json"


@pytest.mark.parametrize("api", [load_project, load])
def test_override_is_keyword_only(tmp_path, api):
    with pytest.raises(TypeError):
        api(_write_project(tmp_path), Methodology.load())


def test_save_after_override_is_self_contained_and_preserves_model_and_ids(tmp_path):
    source = _write_project(tmp_path)
    override = Methodology.load()
    project = load_project(source, methodology_override=override)
    expected_network = network_fingerprint(project.network)
    expected_electrical = electrical_model_fingerprint(project.electrical_model)
    expected_methodology = methodology_fingerprint(project.methodology)
    expected_ids = project.electrical_model._object_id_values()
    expected_ports = dict(project.electrical_model.ports)
    expected_order = {
        name: list(getattr(project.network, name))
        for name in ("nodes", "branches", "transformers3w", "loads", "modes")
    }
    target = tmp_path / "independent.json"
    save_project(target, project)
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["methodology"] == override.data
    assert not (tmp_path / "external-methodology.json").exists()

    reopened = load_project(target)
    assert methodology_fingerprint(reopened.methodology) == expected_methodology
    assert network_fingerprint(reopened.network) == expected_network
    assert electrical_model_fingerprint(reopened.electrical_model) == expected_electrical
    assert reopened.electrical_model._object_id_values() == expected_ids
    assert dict(reopened.electrical_model.ports) == expected_ports
    assert {
        name: list(getattr(reopened.network, name)) for name in expected_order
    } == expected_order
    assert reopened.methodology.path is None
    assert methodology_fingerprint(load(target)[1]) == expected_methodology


@pytest.mark.parametrize("api", [load_project, load])
@pytest.mark.parametrize("damage", ["dangling_port", "duplicate_id", "wrong_name"])
def test_override_does_not_bypass_electrical_model_and_id_validation(tmp_path, api, damage):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    model = raw["electrical_model"]
    if damage == "dangling_port":
        model["connections"][0]["port_id"] = "missing_port"
    elif damage == "duplicate_id":
        model["ports"][1]["id"] = model["ports"][0]["id"]
    elif damage == "wrong_name":
        raw["project"]["name"] = "not the electrical model name"
    source = _write_project(tmp_path, raw=raw)
    with pytest.raises(ValueError):
        api(source, methodology_override=Methodology.load())


def test_load_with_override_still_blocks_an_editable_unconnected_draft(tmp_path):
    from rza_calc.editor import EditorMode, ProjectEditorController

    project = load_project(EXAMPLE)
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.EDIT)
    controller.add_equipment("builtin.disconnector", "QS draft", x=240, y=160)
    target = tmp_path / "draft.json"
    save_project(target, project)
    override = Methodology.load()
    editable = load_project(target, methodology_override=override)
    assert editable.calculation_blockers
    with pytest.raises(ValueError, match="Расчёт заблокирован"):
        load(target, methodology_override=override)
