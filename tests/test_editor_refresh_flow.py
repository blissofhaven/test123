"""A committed edit updates the canvas once and still refreshes all panels."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.editor_panels import EditorWorkspaceWidget
from rza_calc.io.project import load_project

EXAMPLE = Path(__file__).parents[1] / "tests/fixtures/legacy_projects/energoraion.json"


@pytest.mark.parametrize("action", ("canvas_move", "inspector_label", "external_signal"))
def test_command_refreshes_canvas_once_without_skipping_committed_geometry(monkeypatch, action):
    app = QApplication.instance() or QApplication([])
    controller = ProjectEditorController(load_project(EXAMPLE))
    widget = EditorWorkspaceWidget(controller)
    fingerprint = electrical_model_fingerprint(controller.model)
    representation = next(row for row in controller.diagram.representations.values()
                          if row.label == "КТП Ф-4 400 кВ·А (Центральная)")
    widget.scene.select_representations((representation.id,))
    calls = []
    original = widget.scene.sync_document
    def synchronized(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(widget.scene, "sync_document", synchronized)
    try:
        if action == "canvas_move":
            widget.canvas._move((representation.id,), 40, 0, True)
            saved = controller.diagram.representations[representation.id]
            assert saved.x == representation.x + 40
        elif action == "inspector_label":
            widget._edit_property("graphics.label", "Изменённая подпись")
            saved = controller.diagram.representations[representation.id]
            assert saved.label == "Изменённая подпись"
        else:
            result = controller.set_label(representation.id, text="Внешняя команда")
            widget.canvas.commandCompleted.emit(result)
            saved = controller.diagram.representations[representation.id]
            assert saved.label == "Внешняя команда"
        assert len(calls) == 1
        assert widget.scene._items_by_id[representation.id].representation == saved
        assert widget.command_bar.undo_action.isEnabled()
        assert len(controller.journal) == 1
        assert electrical_model_fingerprint(controller.model) == fingerprint
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
