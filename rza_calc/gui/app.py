# -*- coding: utf-8 -*-
"""Запуск графического приложения."""
from __future__ import annotations

import faulthandler
import sys
from pathlib import Path


def _enable_fault_diagnostics() -> None:
    """Keep native-fault tracebacks in the existing launch console, if any.

    Do not replace a debugger/pytest handler or open a log file. Windowed Python
    can have no stderr; capture streams and closed consoles may have no usable
    file descriptor. Other setup failures remain visible to the caller.
    """
    if faulthandler.is_enabled() or sys.stderr is None:
        return
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except (AttributeError, OSError, ValueError):
        # Missing fileno(), unsupported/closed stream, or an invalid descriptor.
        return


def default_project_path() -> Path:
    """Схема, которая открывается при запуске без аргументов.

    Раньше открывался `gtes_sever.json` — аудиторский проект ГТЭС, у которого
    56 непройденных защит и 25 неопределённых результатов. Он для того и
    сделан: на нём проверяются трудные случаи. Но первым, что видит человек,
    должна быть схема, нарисованная как надо, а не полигон для дефектов.
    """
    return Path(__file__).resolve().parent.parent / "examples" / "ps_promyshlennaya.json"


def main(argv: list[str] | None = None) -> int:
    _enable_fault_diagnostics()
    try:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog
    except ImportError:
        print(
            "Не установлен PySide6. Выполните в папке программы:\n\n"
            "    python -m pip install -r requirements.txt\n"
        )
        return 2

    from .main_window import MainWindow
    from .project_settings import ProjectSettings
    from .theme import STYLESHEET
    from .view_model import ProjectViewModel

    args = list(sys.argv[1:] if argv is None else argv)
    smoke_test = "--smoke-test" in args
    args = [item for item in args if item != "--smoke-test"]
    # Test launches stay deterministic and never read or write user settings.
    project_settings = None if smoke_test else ProjectSettings()
    explicit_path = Path(args[0]) if args else None
    remembered_path = (
        project_settings.last_project_path()
        if project_settings is not None and explicit_path is None else None
    )
    project_path = explicit_path or remembered_path or default_project_path()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication([sys.argv[0], *args])
    app.setApplicationName("РЗА-Про")
    app.setOrganizationName("RZA Calc")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    startup_notice = ""
    progress = None
    if not smoke_test:
        progress = QProgressDialog("", "", 0, 0)
        progress.setWindowTitle("РЗА-Про")
        progress.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint
        )
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setMinimumWidth(360)
    try:
        while True:
            window = None
            try:
                if progress is not None:
                    progress.setLabelText(f"Открываю проект…\n{project_path.name}")
                    progress.show()
                    app.processEvents()
                vm = ProjectViewModel.open(project_path.expanduser())
                if progress is not None:
                    progress.setLabelText(f"Подготавливаю схему…\n{project_path.name}")
                    app.processEvents()
                window = MainWindow(vm, project_settings=project_settings)
                window.showMaximized()
            except Exception as exc:
                if window is not None:
                    window.close()
                if remembered_path is not None:
                    startup_notice = (
                        f"Не удалось открыть последний проект: {project_path}. {exc}."
                    )
                    project_path = default_project_path()
                    remembered_path = None
                    continue
                if progress is not None:
                    progress.hide()
                QMessageBox.critical(
                    None,
                    "Не удалось открыть проект",
                    f"{startup_notice}\nФайл: {project_path}\n\n{exc}".lstrip(),
                )
                return 1
            break
    finally:
        if progress is not None:
            progress.close()
            progress.deleteLater()
    persistence_notice = window._remember_project_path()
    if startup_notice:
        startup_notice += " Открыт проект по умолчанию."
    if startup_notice or persistence_notice:
        window.statusBar().showMessage(" ".join(
            notice for notice in (startup_notice, persistence_notice) if notice
        ))
    if smoke_test:
        QTimer.singleShot(150, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
