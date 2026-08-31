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
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print(
            "Не установлен PySide6. Выполните в папке программы:\n\n"
            "    python -m pip install -r requirements.txt\n"
        )
        return 2

    from .main_window import MainWindow
    from .theme import STYLESHEET
    from .view_model import ProjectViewModel

    args = list(sys.argv[1:] if argv is None else argv)
    smoke_test = "--smoke-test" in args
    args = [item for item in args if item != "--smoke-test"]
    project_path = Path(args[0]).expanduser() if args else default_project_path()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication([sys.argv[0], *args])
    app.setApplicationName("РЗА-Про")
    app.setOrganizationName("RZA Calc")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    try:
        vm = ProjectViewModel.open(project_path)
    except Exception as exc:
        QMessageBox.critical(
            None,
            "Не удалось открыть проект",
            f"Файл: {project_path}\n\n{exc}",
        )
        return 1
    window = MainWindow(vm)
    window.showMaximized()
    if smoke_test:
        QTimer.singleShot(150, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
