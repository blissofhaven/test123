"""Startup choices, exclusive new files and deferred real Qt windows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog

from rza_calc.gui import app, newprojectchooser as chooser, view_model
from rza_calc.gui.project_settings import ProjectSettings
from rza_calc.io.project import load_project, save_project
from rza_calc.editor.state import EditorWorkspaceState
from rza_calc.domain.fingerprint import electrical_model_fingerprint

from test_last_project_startup import session, seed, stored


@pytest.fixture
def settings(tmp_path):
    raw = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return ProjectSettings(raw)


def test_history_read_seeds_old_last_without_writes_or_auto_open(settings, tmp_path):
    last = tmp_path / "Схема.json"
    settings._settings.setValue(settings.LAST_PROJECT_KEY, str(last))
    settings._settings.sync()
    before = Path(settings._settings.fileName()).read_bytes()
    assert settings.recent_project_paths() == (last,)
    assert not settings.auto_open_last_project()
    assert Path(settings._settings.fileName()).read_bytes() == before


def test_history_is_bounded_deduplicated_ordered_and_keeps_missing_files(settings, tmp_path):
    paths = [tmp_path / f"{index}.json" for index in range(16)]
    for path in paths:
        assert settings.remember_project(path)
    assert settings.recent_project_paths() == tuple(reversed(paths[-12:]))
    settings.remember_project(paths[-4])
    expected = [paths[-4], *[path for path in reversed(paths[-12:]) if path != paths[-4]]]
    assert settings.recent_project_paths() == tuple(expected)
    assert settings.last_project_path() == paths[-4]


@pytest.mark.parametrize("raw", (None, "not-a-list", 37, {"unexpected": "value"}, [None, 7, ""]))
def test_malformed_history_is_ignored_without_changing_it(settings, raw):
    settings._settings.setValue(settings.RECENT_PROJECTS_KEY, raw)
    assert settings.recent_project_paths() == ()


@pytest.mark.parametrize("raw,expected", [(None, False), (False, False), ("false", False), ("garbage", False), (1, False), (True, True), ("true", True)])
def test_auto_open_requires_explicit_true(settings, raw, expected):
    settings._settings.setValue(settings.AUTO_OPEN_KEY, raw)
    assert settings.auto_open_last_project() is expected


def test_default_startup_shows_chooser_even_with_last_path_and_cancel_keeps_it(session, monkeypatch):
    seed(session, session.remembered)
    monkeypatch.setattr(view_model.ProjectViewModel, "open", lambda *a, **k: pytest.fail("Chooser must not load a project"))
    assert app.main([]) == 0
    assert session.chooser_calls == [""]
    assert not session.windows
    assert stored(session) == str(session.remembered)


@pytest.mark.parametrize("launch", ("explicit", "smoke", "chooser", "auto"))
def test_actual_window_opens_without_solver_or_full_engine(session, monkeypatch, launch):
    def forbidden(*args, **kwargs):
        pytest.fail("Opening a project must not start a calculation")
    monkeypatch.setattr(view_model, "run_input", forbidden)
    monkeypatch.setattr(view_model, "run", forbidden)
    from rza_calc.core.short_circuit import ShortCircuitSolver
    monkeypatch.setattr(ShortCircuitSolver, "__init__", forbidden)
    before = session.explicit.read_bytes()
    if launch == "explicit":
        args = [str(session.explicit)]
    elif launch == "smoke":
        args = ["--smoke-test", str(session.explicit)]
    elif launch == "auto":
        seed(session, session.explicit, auto_open=True)
        args = []
    else:
        session.choices.append(session.explicit)
        args = []
    assert app.main(args) == 0
    window = session.windows[-1]
    assert window.isVisible() and window.centralWidget() is not None
    assert window.vm.result is None and window.vm.current_result is None
    assert not window.vm.calculation_error
    assert window.vm.project.diagram.pages
    assert "Расчёт не выполнен" in window.vm.result_unavailable_reason()
    assert session.explicit.read_bytes() == before


def test_create_is_a_blank_native_project_that_reopens_and_has_working_page(tmp_path):
    path = chooser.create_empty_project("  Мой объект  ", tmp_path / "new.json")
    project = load_project(path)
    assert project.network.name == "Мой объект"
    assert not project.network.nodes and not project.network.branches
    assert not project.electrical_model.equipment
    assert len(project.diagram.pages) == len(project.electrical_model.operating_states) == 1
    assert project.electrical_model.equipment_types and project.electrical_model.voltage_classes
    state = EditorWorkspaceState.from_diagram(project.diagram)
    assert state.active_page_id == next(iter(project.diagram.pages)).value
    assert state.active_operating_state_id == next(iter(project.electrical_model.operating_states)).value
    before = electrical_model_fingerprint(project.electrical_model)
    save_project(path, project)
    assert electrical_model_fingerprint(load_project(path).electrical_model) == before


def test_empty_new_project_opens_in_real_editor(session, tmp_path):
    path = chooser.create_empty_project("Новая схема", tmp_path / "blank.json")
    session.choices.append(path)
    assert app.main([]) == 0
    window = session.windows[-1]
    assert window.isVisible() and window.editor_workspace.canvas.page_id is not None
    assert window.vm.result is None
    assert not window.vm.project.electrical_model.equipment


def test_training_copy_preserves_source_and_electrical_inputs(session, tmp_path):
    source = session.explicit
    before = source.read_bytes()
    fingerprint = electrical_model_fingerprint(load_project(source).electrical_model)
    target = tmp_path / "Учебная копия.json"
    assert chooser.copy_training_project(source, target) == target
    loaded = load_project(target)
    assert electrical_model_fingerprint(loaded.electrical_model) == fingerprint
    assert loaded.diagram.pages
    assert source.read_bytes() == before
    save_project(target, loaded)
    assert source.read_bytes() == before


def test_copy_rebases_relative_methodology_reference(session, tmp_path):
    source = session.explicit
    raw = json.loads(source.read_text(encoding="utf-8"))
    method = tmp_path / "profile.json"
    from rza_calc.core.methodology import DEFAULT_PATH
    method.write_bytes(DEFAULT_PATH.read_bytes())
    raw["methodology"] = {"file": method.name}
    source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    folder = tmp_path / "copies"
    folder.mkdir()
    target = chooser.copy_training_project(source, folder / "copy.json")
    assert load_project(target).methodology.path.resolve() == method.resolve()


@pytest.mark.parametrize("operation", ("empty", "copy"))
def test_new_file_never_overwrites_existing(session, operation):
    before = session.remembered.read_bytes()
    with pytest.raises(FileExistsError):
        if operation == "empty":
            chooser.create_empty_project("test", session.remembered)
        else:
            chooser.copy_training_project(session.explicit, session.remembered)
    assert session.remembered.read_bytes() == before


def test_destination_created_during_validation_is_not_overwritten(session, tmp_path, monkeypatch):
    import rza_calc.io.project as persistence
    target = tmp_path / "race.json"
    real_save = persistence.save_project
    def racing_save(path, project):
        real_save(path, project)
        target.write_bytes(b"another writer")
    monkeypatch.setattr(persistence, "save_project", racing_save)
    with pytest.raises(FileExistsError):
        chooser.create_empty_project("test", target)
    assert target.read_bytes() == b"another writer"
    assert not list(tmp_path.glob(".rza-new-*"))


def test_failed_validation_creates_no_destination_or_history(settings, tmp_path, monkeypatch):
    import rza_calc.io.project as persistence
    def fail(*args):
        raise ValueError("Invalid input")
    monkeypatch.setattr(persistence, "save_project", fail)
    with pytest.raises(ValueError, match="Invalid input"):
        chooser.create_empty_project("test", tmp_path / "not-created.json")
    assert not (tmp_path / "not-created.json").exists()
    assert settings.last_project_path() is None
    assert not list(tmp_path.glob(".rza-new-*"))


def test_real_chooser_selection_and_cancel_do_not_record_last_path(session, settings, monkeypatch):
    settings.remember_project(session.remembered)
    dialog = chooser.ProjectChooser(settings, examples=[])
    try:
        assert not dialog.auto_open_check.isChecked()
        assert not dialog.open_recent_button.isEnabled()
        assert dialog.project_path is None
        dialog.recent_list.setCurrentRow(0)
        assert dialog.open_recent_button.isEnabled()
        dialog._open_recent()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.project_path == session.remembered
        assert settings.last_project_path() == session.remembered
    finally:
        dialog.close()
    def cancel_file(*args):
        return "", ""
    monkeypatch.setattr(QFileDialog, "getOpenFileName", cancel_file)
    dialog = chooser.ProjectChooser(settings, examples=[])
    dialog._open_file()
    assert dialog.project_path is None
    dialog.reject()
    assert settings.last_project_path() == session.remembered


def test_missing_recent_stays_visible_and_does_not_accept(session, settings):
    missing = session.remembered.with_name("moved.json")
    settings.remember_project(missing)
    dialog = chooser.ProjectChooser(settings, examples=[])
    dialog.recent_list.setCurrentRow(0)
    dialog._open_recent()
    assert dialog.project_path is None
    assert "Файл недоступен" in dialog.notice_label.text()
    assert dialog.recent_list.count() == 1
    assert settings.last_project_path() == missing
    dialog.close()


def test_create_button_uses_user_name_and_path_without_template(session, settings, monkeypatch, tmp_path):
    target = tmp_path / "My-new-project.json"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a: ("Мой объект", True))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
    dialog = chooser.ProjectChooser(settings, examples=[])
    dialog._create_empty()
    assert dialog.project_path == target
    assert load_project(target).network.name == "Мой объект"
    assert not load_project(target).electrical_model.equipment
    assert settings.last_project_path() is None
    dialog.close()


def test_training_registry_contains_only_shipped_visible_examples():
    examples = chooser.training_projects()
    assert {item.path.name for item in examples} <= {"oilfield_gtes.json", "compact_training.json"}
    assert "oilfield_gtes.json" in {item.path.name for item in examples}
    assert all(item.path.is_file() and item.title and item.description for item in examples)


def test_common_light_theme_is_installed_before_real_chooser(session, monkeypatch):
    from rza_calc.gui.theme import STYLESHEET
    seen = []
    def choose(settings, *, notice=""):
        dialog = chooser.ProjectChooser(settings, examples=[])
        seen.append(session.qt.styleSheet() == STYLESHEET)
        assert dialog.palette().color(dialog.backgroundRole()).lightness() > 200
        dialog.reject()
        return None
    monkeypatch.setattr(chooser, "choose_project", choose)
    assert app.main([]) == 0
    assert seen == [True]
