"""Qt worker consumes a captured input; it never reads the live project."""
from PySide6.QtCore import QThread, Signal


class CalculationWorker(QThread):
    progressed = Signal(int, int, str)
    completed = Signal(object, str)

    def __init__(self, captured, parent=None):
        super().__init__(parent)
        self.captured = captured

    def run(self):
        from ..core.engine import run_input, CalculationCancelled
        try:
            result = run_input(self.captured, cancelled=self.isInterruptionRequested,
                               progress=self.progressed.emit)
            if self.isInterruptionRequested():
                self.completed.emit(None, 'Расчёт отменён. Частичный результат не опубликован.')
            else:
                self.completed.emit(result, '')
        except CalculationCancelled:
            self.completed.emit(None, 'Расчёт отменён. Частичный результат не опубликован.')
        except Exception as exc:
            self.completed.emit(None, str(exc))
