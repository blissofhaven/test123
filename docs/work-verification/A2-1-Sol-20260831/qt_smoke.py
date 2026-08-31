"""Windows Qt widget rendering without a visible window or source-project writes."""
import hashlib
import os
from pathlib import Path
import sys

os.environ["QT_QPA_PLATFORM"] = "windows"
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel


def main():
    source = ROOT / "rza_calc/examples/energoraion.json"
    initial_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    vm = ProjectViewModel.open(source)
    assert vm.result is not None, vm.calculation_error
    fingerprint = electrical_model_fingerprint(vm.project.electrical_model)
    window = MainWindow(vm)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    for tab, filename in ((0, "01-editor-final.png"), (1, "02-analysis-final.png")):
        window.workspace_tabs.setCurrentIndex(tab)
        QTest.qWait(250)
        if tab == 0:
            window.editor_workspace.view.fit_all()
        else:
            window.diagram_panel.view.reset_view()
        app.processEvents()
        assert window.grab().save(str(Path(__file__).parent / filename))
        print(f"QT_TAB_{tab}_RENDERED={filename}")
    assert electrical_model_fingerprint(vm.project.electrical_model) == fingerprint
    assert hashlib.sha256(source.read_bytes()).hexdigest() == initial_hash
    assert vm.project.electrical_model.validate_integrity() == []
    print(f"MODEL_FINGERPRINT_UNCHANGED={fingerprint}")
    print(f"SOURCE_SHA256_UNCHANGED={initial_hash}")
    print("QT_WINDOWS_HIDDEN_WIDGET_SMOKE_OK")
    window.close()
    app.quit()


if __name__ == "__main__":
    main()
