"""The scale indicator reflects the actual viewport, including saved/fit zoom."""
import pytest

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.editor_panels import EditorWorkspaceWidget
from test_stage3_editor_gui import _app, _controller


@pytest.fixture
def workspace():
    qt = _app()
    controller = _controller()
    controller.add_equipment("builtin.load", "Левая нагрузка", x=-1200, y=-200)
    controller.add_equipment("builtin.load", "Правая нагрузка", x=1400, y=600)
    controller.set_view(.4, 100., 200.)
    widget = EditorWorkspaceWidget(controller, confirm_deletions=False)
    widget.resize(1500, 900)
    widget.show()
    qt.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qt.processEvents()


def _assert_actual_scale(workspace):
    actual = workspace.view.zoom_factor
    combo = workspace.command_bar.scale_combo
    assert combo.currentData() == pytest.approx(actual, rel=1e-10)
    displayed_percent = float(combo.currentText().removesuffix(" %").replace(",", "."))
    assert displayed_percent == pytest.approx(actual * 100, abs=.051)


def test_initial_scale_reads_saved_viewport_even_without_viewport_signal(workspace):
    assert workspace.view.zoom_factor == pytest.approx(.4)
    _assert_actual_scale(workspace)


def test_silent_workspace_restore_refreshes_scale_without_changing_electrical_model(workspace):
    controller = workspace.controller
    before = electrical_model_fingerprint(controller.model), len(controller.journal)
    controller.set_view(.731, 80., 150.)
    workspace.refresh()
    assert workspace.view.zoom_factor == pytest.approx(.731)
    _assert_actual_scale(workspace)
    assert (electrical_model_fingerprint(controller.model), len(controller.journal)) == before


def test_fit_wheel_scales_and_preset_choice_share_one_accurate_indicator(workspace):
    controller = workspace.controller
    before = electrical_model_fingerprint(controller.model), len(controller.journal)
    initial_presets = 8
    workspace.command_bar.fit_action.trigger()
    _assert_actual_scale(workspace)
    for _ in range(15):
        workspace.view.zoom_in()
        _assert_actual_scale(workspace)
        assert workspace.command_bar.scale_combo.count() <= initial_presets + 1
    combo = workspace.command_bar.scale_combo
    combo.setCurrentIndex(combo.findData(.75))
    assert workspace.view.zoom_factor == pytest.approx(.75)
    _assert_actual_scale(workspace)
    assert (electrical_model_fingerprint(controller.model), len(controller.journal)) == before
