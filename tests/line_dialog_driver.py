"""Bounded input for the real Qt line dialog in creation/inspector tests."""
from contextlib import contextmanager

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from rza_calc.gui.line_parameters import LineParametersDialog


@contextmanager
def choose_draft_parameters(canvas):
    seen = []
    poll, watchdog = QTimer(canvas), QTimer(canvas)
    watchdog.setSingleShot(True)

    def current():
        return next((widget for widget in QApplication.topLevelWidgets()
            if isinstance(widget, LineParametersDialog) and widget.isVisible()
            and widget.parent() is canvas), None)

    def answer():
        dialog = current()
        if dialog is None:
            return
        poll.stop()
        seen.append(True)
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("draft"))
        QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)
        watchdog.stop()

    def timeout():
        poll.stop()
        dialog = current()
        if dialog is not None:
            dialog.reject()

    poll.timeout.connect(answer)
    watchdog.timeout.connect(timeout)
    poll.start(1)
    watchdog.start(1500)
    try:
        yield
        assert seen == [True], "The real line parameter dialog must be answered once"
    finally:
        poll.stop()
        watchdog.stop()
