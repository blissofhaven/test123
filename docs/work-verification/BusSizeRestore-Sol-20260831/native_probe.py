"""Read-only actual Qt window: restored bus equals the previous thin symbol."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
import types
from zipfile import ZipFile

os.environ['QT_QPA_PLATFORM'] = 'windows'
ROOT = Path(r'C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening')
OUT = Path(__file__).resolve().parent
OLD = Path(r'C:\Users\shock\Documents\Codex\РЗА — резерв\2026-08-31-before-connection-cleanup-delivery\rza-2026-08-31-safe-drawing.zip')
sys.path.insert(0, str(ROOT))
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from rza_calc.editor.symbols import build_symbol
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.diagram import diagram_to_dict

assert hashlib.sha256(OLD.read_bytes()).hexdigest() == '3e1df2e01b14b3910e21658895f29f7e1f83bcb9b90871b8b90d92e8b2202e8b'
old_symbols = types.ModuleType('rza_calc.editor._before_bus_size')
sys.modules[old_symbols.__name__] = old_symbols
with ZipFile(OLD) as archive:
    exec(compile(archive.read('rza-calc-0.3-safe-hardening/rza_calc/editor/symbols.py'), str(OLD), 'exec'), old_symbols.__dict__)
for width, height in ((160.,16.), (240.,12.), (12.,240.), (150.,22.), (22.,150.)):
    assert dataclasses.asdict(build_symbol('busbar', width=width, height=height)) == dataclasses.asdict(old_symbols.build_symbol('busbar', width=width, height=height))

demo = ROOT / 'rza_calc/examples/energoraion.json'
raw = demo.read_bytes()
app = QApplication([])
app.setStyle('Fusion')
app.setStyleSheet(STYLESHEET)
errors, frames = [], []
sys.excepthook = lambda kind, value, tb: errors.append(f'{kind.__name__}: {value}')
window = MainWindow(ProjectViewModel.open(demo))
window.setWindowTitle('Проверка прежней толщины шин — без сохранения проекта')
window.resize(1560, 950)
window.show()
try:
    assert QTest.qWaitForWindowExposed(window, 5000)
    canvas = window.editor_workspace.canvas
    model = canvas.controller.model
    document = diagram_to_dict(canvas.controller.diagram)
    fingerprint = electrical_model_fingerprint(model)
    target = next(item for item in canvas.scene._items_by_id.values()
                  if item._canonical_key == 'busbar' and 'ЦЕНТРАЛЬНАЯ · 1' in item._name)
    points = tuple((p.x(), p.y()) for p in target._bus_junction_points)
    canvas.view.set_zoom(1.0)
    canvas.view.centerOn(target)
    # Camera state is explicitly view-only and allowed to change in memory.
    document = diagram_to_dict(canvas.controller.diagram)
    for selected in (False, True):
        canvas.scene.select_representations((target.representation_id,) if selected else ())
        app.processEvents()
        name = 'native-thin-bus-selected.png' if selected else 'native-thin-bus.png'
        assert window.grab().save(str(OUT / name), 'PNG')
        assert tuple((p.x(), p.y()) for p in target._bus_junction_points) == points
        assert electrical_model_fingerprint(canvas.controller.model) == fingerprint
        assert diagram_to_dict(canvas.controller.diagram) == document
        frames.append({'file':name,'selected':selected,'sha256':hashlib.sha256((OUT/name).read_bytes()).hexdigest()})
    assert not errors
    assert demo.read_bytes() == raw
    report = {'platform':app.platformName(),'automated_not_manual':True,'old_symbol_exact_match':True,
              'old_symbol_size_cases':5,'white_contacts':len(points),'electrical_fingerprint':fingerprint,
              'demo_sha256':hashlib.sha256(raw).hexdigest(),'project_file_unchanged':True,
              'qt_callback_errors':errors,'frames':frames}
    (OUT/'native-probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    window.hide()
    window.deleteLater()
    app.processEvents()
