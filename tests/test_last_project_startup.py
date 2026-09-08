"""Remember only completed user actions; never touch the user's registry."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog, QPushButton

from rza_calc.gui import app, main_window, newprojectchooser
from rza_calc.gui.view_model import ProjectViewModel


LAST_PROJECT_KEY = "startup/last_project_path"
EXAMPLE = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/four_fault_types.json"


@pytest.fixture
def session(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    settings_class = QtCore.QSettings
    ini = tmp_path / "isolated-settings.ini"
    settings = settings_class(str(ini), settings_class.Format.IniFormat)
    factories = []

    def isolated_settings(organization, application):
        factories.append((organization, application))
        return settings_class(str(ini), settings_class.Format.IniFormat)

    monkeypatch.setattr(QtCore, "QSettings", isolated_settings)
    loaded = sys.modules.get("rza_calc.gui.project_settings")
    if loaded is not None:
        monkeypatch.setattr(loaded, "QSettings", isolated_settings)
    default = tmp_path / "oilfield_gtes.json"
    remembered = tmp_path / "Последняя схема.json"
    explicit = tmp_path / "command-line.json"
    for path in (default, remembered, explicit):
        shutil.copyfile(EXAMPLE, path)
    monkeypatch.setattr(app, "default_project_path", lambda: default)
    monkeypatch.setattr(qt, "exec", lambda: 0)
    messages = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    windows = []
    window_class = main_window.MainWindow

    def tracked_window(*args, **kwargs):
        window = window_class(*args, **kwargs)
        windows.append(window)
        return window

    monkeypatch.setattr(main_window, "MainWindow", tracked_window)
    choices, chooser_calls = [], []

    def choose(settings, *, notice=""):
        chooser_calls.append(notice)
        return choices.pop(0) if choices else None

    monkeypatch.setattr(newprojectchooser, "choose_project", choose)
    state = SimpleNamespace(
        qt=qt, settings=settings, ini=ini, factories=factories,
        default=default, remembered=remembered, explicit=explicit,
        messages=messages, windows=windows, window_class=window_class,
        choices=choices, chooser_calls=chooser_calls,
    )
    yield state
    for window in windows:
        window.close()
        window.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qt.processEvents()
    if hasattr(qt, "_rza_project_windows"):
        qt._rza_project_windows = []


def seed(session, path, *, auto_open=False):
    session.settings.setValue(LAST_PROJECT_KEY, str(path))
    session.settings.setValue("startup/auto_open_last_project", auto_open)
    session.settings.sync()


def stored(session):
    session.settings.sync()
    return session.settings.value(LAST_PROJECT_KEY, "")


def test_opt_in_startup_reopens_last_successful_project(session):
    seed(session, session.remembered, auto_open=True)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == session.remembered
    assert session.windows[-1].isVisible()
    assert not session.messages
    assert session.factories == [("RZA Calc", "РЗА-Про")]
    assert not session.chooser_calls
    assert session.windows[-1].vm.result is None


def test_successful_explicit_path_wins_and_is_remembered(session):
    seed(session, session.remembered)
    assert app.main([str(session.explicit)]) == 0
    assert session.windows[-1].vm.path == session.explicit
    assert stored(session) == str(session.explicit.resolve())


def test_failed_remembered_project_returns_to_chooser_with_reason(session):
    missing = session.remembered.with_name("перемещён.json")
    seed(session, missing, auto_open=True)
    session.choices.append(session.default)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == session.default
    status = session.windows[-1].statusBar().currentMessage()
    assert str(missing) in status
    assert "не удалось" in status.lower()
    assert str(missing) in session.chooser_calls[0]
    assert not session.messages


def test_failed_explicit_path_does_not_fall_back_or_replace_remembered(session):
    seed(session, session.remembered)
    missing = session.explicit.with_name("absent.json")
    assert app.main([str(missing)]) == 1
    assert not session.windows
    assert str(missing) in session.messages[-1][2]
    assert stored(session) == str(session.remembered)


def test_first_startup_opens_explicit_chooser_selection_and_remembers_it(session):
    session.choices.append(session.default)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == session.default
    assert stored(session) == str(session.default.resolve())
    assert session.chooser_calls == [""]


def test_actual_default_is_the_new_oilfield_project():
    expected = Path(__file__).resolve().parents[1] / "rza_calc/examples/oilfield_gtes.json"
    assert app.default_project_path() == expected
    assert expected.is_file()


@pytest.mark.parametrize("name", (
    "energoraion.json", "energoraion_v1.json", "four_fault_types.json",
    "gtes_sever.json", "gtes_sever_v1.json",
    "ps_promyshlennaya.json", "ps_promyshlennaya_v1.json",
    "ps_severnaya.json", "ps_severnaya_v1.json",
))
@pytest.mark.parametrize("exists", (True, False))
def test_retired_builtin_auto_history_returns_to_chooser_without_opening_old(session, monkeypatch, name, exists):
    old = session.default.parent / "previous-installation/rza_calc/examples" / name
    old.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        shutil.copyfile(EXAMPLE, old)
    seed(session, old, auto_open=True)
    session.choices.append(session.default)
    attempted = []
    original_open = ProjectViewModel.open

    def observed_open(path, **kwargs):
        attempted.append(Path(path))
        assert kwargs == {"calculate": False}
        return original_open(path, **kwargs)

    monkeypatch.setattr(ProjectViewModel, "open", observed_open)
    assert app.main([]) == 0
    assert attempted == [session.default]
    assert session.windows[-1].vm.path == session.default
    assert stored(session) == str(session.default.resolve())
    assert not session.messages


def test_custom_project_with_retired_example_name_remains_the_last_project(session):
    custom = session.remembered.with_name("gtes_sever.json")
    shutil.copyfile(EXAMPLE, custom)
    seed(session, custom, auto_open=True)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == custom
    assert stored(session) == str(custom.resolve())


def test_explicit_cli_project_wins_even_if_it_uses_a_retired_builtin_path(session):
    explicit = session.default.parent / "rza_calc/examples/ps_promyshlennaya.json"
    explicit.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE, explicit)
    seed(session, session.remembered)
    assert app.main([str(explicit)]) == 0
    assert session.windows[-1].vm.path == explicit
    assert stored(session) == str(explicit.resolve())


@pytest.mark.parametrize("failure", ("missing", "corrupt"))
def test_missing_or_corrupt_last_project_opens_only_the_chooser_selection(session, failure):
    if failure == "missing":
        session.remembered.unlink()
    else:
        session.remembered.write_text("{broken json", encoding="utf-8")
    seed(session, session.remembered, auto_open=True)
    session.choices.append(session.default)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == session.default
    assert session.default.name == "oilfield_gtes.json"
    assert stored(session) == str(session.default.resolve())
    assert "не удалось" in session.windows[-1].statusBar().currentMessage().lower()
    assert not session.messages


def test_success_survives_a_new_settings_instance_and_next_startup(session):
    assert app.main([str(session.explicit)]) == 0
    session.windows[-1].close()
    session.choices.append(session.explicit)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == session.explicit


def test_relative_cli_path_is_stored_as_absolute(session, monkeypatch):
    monkeypatch.chdir(session.explicit.parent)
    assert app.main([session.explicit.name]) == 0
    assert stored(session) == str(session.explicit.resolve())


@pytest.mark.parametrize("corrupt", (False, True))
def test_unreadable_last_project_chooser_cancel_preserves_setting(
    session, monkeypatch, corrupt,
):
    if corrupt:
        session.remembered.write_text("{broken json", encoding="utf-8")
    else:
        session.remembered.unlink()
    seed(session, session.remembered, auto_open=True)
    session.default.unlink()
    assert app.main([]) == 0
    assert not session.windows
    assert stored(session) == str(session.remembered)
    assert str(session.remembered) in session.chooser_calls[-1]
    assert not session.messages


def test_last_project_full_window_failure_returns_to_chooser_before_recording(session, monkeypatch):
    seed(session, session.remembered, auto_open=True)
    session.choices.append(session.default)
    construct = main_window.MainWindow

    def fail_last(vm, **kwargs):
        assert stored(session) == str(session.remembered)
        if vm.path == session.remembered:
            raise RuntimeError("Не удалось построить сцену")
        return construct(vm, **kwargs)

    monkeypatch.setattr(main_window, "MainWindow", fail_last)
    assert app.main([]) == 0
    assert session.windows[-1].vm.path == session.default
    assert "Не удалось построить сцену" in session.windows[-1].statusBar().currentMessage()
    assert stored(session) == str(session.default)
    assert not session.messages


def test_explicit_project_full_window_failure_does_not_remember(session, monkeypatch):
    seed(session, session.remembered)

    def fail_window(*args, **kwargs):
        raise RuntimeError("Не удалось построить сцену")

    monkeypatch.setattr(main_window, "MainWindow", fail_window)
    assert app.main([str(session.explicit)]) == 1
    assert not session.windows
    assert stored(session) == str(session.remembered)
    assert "Не удалось построить сцену" in session.messages[-1][2]


@pytest.mark.parametrize("explicit", (False, True))
def test_smoke_test_never_reads_or_writes_user_project_settings(session, monkeypatch, explicit):
    seed(session, session.remembered)
    timers = []
    monkeypatch.setattr(QtCore.QTimer, "singleShot", lambda *args: timers.append(args))
    args = ["--smoke-test"]
    if explicit:
        args.append(str(session.explicit))
    assert app.main(args) == 0
    expected = session.explicit if explicit else session.default
    assert session.windows[-1].vm.path == expected
    assert session.factories == []
    assert not visible_progress(session)
    assert stored(session) == str(session.remembered)
    assert any(args[0] == 150 for args in timers)
    # Saving from the smoke window is isolated as well.
    session.windows[-1]._save_editor_project()
    assert stored(session) == str(session.remembered)


def test_direct_mainwindow_constructor_and_save_do_not_enable_persistence(session):
    seed(session, session.remembered)
    window = main_window.MainWindow(ProjectViewModel.open(session.explicit))
    window.show()
    window._save_editor_project()
    assert session.factories == []
    assert stored(session) == str(session.remembered)
    assert not session.messages


def visible_progress(session):
    return [widget for widget in session.qt.topLevelWidgets()
            if isinstance(widget, QProgressDialog) and widget.isVisible()]


def test_busy_indicator_is_visible_before_load_and_preparing_window_then_closes(session, monkeypatch):
    original_open = ProjectViewModel.open
    construct = main_window.MainWindow
    stages = []

    def observed_open(path, **kwargs):
        progress, = visible_progress(session)
        assert progress.minimum() == progress.maximum() == 0
        assert not [button for button in progress.findChildren(QPushButton) if button.isVisible()]
        stages.append(progress.labelText())
        return original_open(path, **kwargs)

    def observed_construct(*args, **kwargs):
        progress, = visible_progress(session)
        stages.append(progress.labelText())
        return construct(*args, **kwargs)

    monkeypatch.setattr(ProjectViewModel, "open", observed_open)
    monkeypatch.setattr(main_window, "MainWindow", observed_construct)
    assert app.main([str(session.explicit)]) == 0
    assert stages == [f"Открываю проект…\n{session.explicit.name}",
                      f"Подготавливаю схему…\n{session.explicit.name}"]
    assert not visible_progress(session)
    assert session.windows[-1].isVisible()


def test_busy_indicator_updates_for_selected_retry_and_closes_on_cancel(session, monkeypatch):
    seed(session, session.remembered, auto_open=True)
    session.choices.append(session.default)
    original_open = ProjectViewModel.open
    labels = []
    session.remembered.unlink()
    session.default.unlink()

    def observed_open(path, **kwargs):
        progress, = visible_progress(session)
        labels.append(progress.labelText())
        return original_open(path, **kwargs)

    def observed_error(*args):
        assert not visible_progress(session)
        session.messages.append(args)

    monkeypatch.setattr(ProjectViewModel, "open", observed_open)
    monkeypatch.setattr(QMessageBox, "critical", observed_error)
    assert app.main([]) == 0
    assert labels == [f"Открываю проект…\n{session.remembered.name}",
                      f"Открываю проект…\n{session.default.name}"]
    assert not visible_progress(session)
    assert not session.messages
    assert len(session.chooser_calls) == 2


def test_successful_dialog_open_remembers_only_after_window_is_shown(session, monkeypatch):
    assert app.main([str(session.remembered)]) == 0
    original = session.windows[-1]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(session.explicit), ""))
    show = session.window_class.showMaximized

    def checked_show(window):
        assert stored(session) == str(session.remembered)
        show(window)

    monkeypatch.setattr(session.window_class, "showMaximized", checked_show)
    original._open_project_dialog()
    replacement = session.windows[-1]
    assert replacement is not original
    assert replacement.vm.path == session.explicit
    assert replacement.isVisible() and not original.isVisible()
    assert stored(session) == str(session.explicit)
    assert not session.messages


@pytest.mark.parametrize("choice", ("cancel", "missing", "corrupt", "window_failure", "show_failure"))
def test_failed_or_cancelled_dialog_preserves_current_window_and_setting(session, monkeypatch, choice):
    assert app.main([str(session.remembered)]) == 0
    original = session.windows[-1]
    if choice == "missing":
        session.explicit.unlink()
    elif choice == "corrupt":
        session.explicit.write_text("{broken json", encoding="utf-8")
    elif choice == "window_failure":
        def fail_window(*args, **kwargs):
            raise RuntimeError("Не удалось построить сцену")
        monkeypatch.setattr(main_window, "MainWindow", fail_window)
    elif choice == "show_failure":
        def fail_show(window):
            raise RuntimeError("Не удалось показать окно")
        monkeypatch.setattr(session.window_class, "showMaximized", fail_show)
    selected = "" if choice == "cancel" else str(session.explicit)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (selected, ""))
    original._open_project_dialog()
    assert all(not window.isVisible() for window in session.windows if window is not original)
    assert original.isVisible()
    assert stored(session) == str(session.remembered)
    assert len(session.messages) == (0 if choice == "cancel" else 1)


def test_successful_save_remembers_written_project(session):
    assert app.main([str(session.explicit)]) == 0
    window = session.windows[-1]
    seed(session, session.remembered)
    window._save_editor_project()
    assert stored(session) == str(session.explicit)
    assert "Проект сохранён" in window.statusBar().currentMessage()
    assert not session.messages
    assert ProjectViewModel.open(session.explicit).net.name == window.vm.net.name


def test_failed_save_preserves_remembered_path_and_project_bytes(session, monkeypatch):
    assert app.main([str(session.explicit)]) == 0
    window = session.windows[-1]
    before = session.explicit.read_bytes()
    seed(session, session.remembered)

    def fail_save(*args):
        raise PermissionError("Нет доступа к файлу")

    monkeypatch.setattr(main_window, "save_project", fail_save)
    window._save_editor_project()
    assert stored(session) == str(session.remembered)
    assert session.explicit.read_bytes() == before
    assert "Нет доступа к файлу" in session.messages[-1][2]


def test_explicit_window_show_failure_closes_candidate_and_preserves_setting(session, monkeypatch):
    seed(session, session.remembered)

    def fail_show(window):
        window.show()
        raise RuntimeError("Не удалось показать окно")

    monkeypatch.setattr(session.window_class, "showMaximized", fail_show)
    assert app.main([str(session.explicit)]) == 1
    assert not session.windows[-1].isVisible()
    assert stored(session) == str(session.remembered)
    assert "Не удалось показать окно" in session.messages[-1][2]


@pytest.mark.parametrize("status_name", ("AccessError", "FormatError"))
def test_qsettings_sync_error_does_not_prevent_startup_and_is_nonmodal(session, monkeypatch, status_name):
    from rza_calc.gui import project_settings
    factory = project_settings.QSettings

    def failed_settings(*args):
        settings = factory(*args)
        monkeypatch.setattr(settings, "status", lambda: getattr(settings.Status, status_name))
        return settings

    monkeypatch.setattr(project_settings, "QSettings", failed_settings)
    assert app.main([str(session.explicit)]) == 0
    window = session.windows[-1]
    assert window.isVisible()
    assert "Не удалось запомнить проект" in window.statusBar().currentMessage()
    assert not window._project_settings.remember_project(session.explicit)
    assert not session.messages


@pytest.mark.parametrize("failure", ("status", "exception"))
def test_persistence_error_after_save_does_not_claim_project_save_failed(session, monkeypatch, failure):
    assert app.main([str(session.explicit)]) == 0
    window = session.windows[-1]
    if failure == "status":
        settings = window._project_settings._settings
        monkeypatch.setattr(settings, "status", lambda: settings.Status.AccessError)
    else:
        def cannot_remember(path):
            raise OSError("Настройки недоступны")
        monkeypatch.setattr(window._project_settings, "remember_project", cannot_remember)
    window._save_editor_project()
    status = window.statusBar().currentMessage()
    assert "Проект сохранён" in status
    assert "Не удалось запомнить проект" in status
    assert not session.messages
    assert ProjectViewModel.open(session.explicit).net.name == window.vm.net.name


def test_persistence_error_does_not_break_successful_dialog_open(session, monkeypatch):
    assert app.main([str(session.remembered)]) == 0
    original = session.windows[-1]
    settings = original._project_settings._settings
    monkeypatch.setattr(settings, "status", lambda: settings.Status.AccessError)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(session.explicit), ""))
    original._open_project_dialog()
    replacement = session.windows[-1]
    assert replacement.isVisible() and not original.isVisible()
    assert replacement.vm.path == session.explicit
    assert "Проект открыт" in replacement.statusBar().currentMessage()
    assert "Не удалось запомнить проект" in replacement.statusBar().currentMessage()
    assert not session.messages
