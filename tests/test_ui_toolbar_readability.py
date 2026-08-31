"""Toolbar colors do not leak the native palette; compact icons keep actions."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QStyle, QStyleFactory, QStyleOptionToolButton,
    QToolButton,
)

from rza_calc.gui.editor_panels import EditorCommandBar
from rza_calc.gui.theme import STYLESHEET


_APP = None
_STATE_COLORS = (
    ("normal", "#ffffff", "#172033"),
    ("hover", "#f1f5fb", "#172033"),
    ("pressed", "#c8dafa", "#172033"),
    ("checked", "#e4edff", "#174fbf"),
    ("checked_hover", "#d5e4ff", "#174fbf"),
    ("checked_pressed", "#c8dafa", "#172033"),
    ("disabled", "#f3f5f8", "#56647a"),
    ("checked_disabled", "#e8edf5", "#56647a"),
    ("disabled_hover", "#f3f5f8", "#56647a"),
    ("checked_disabled_hover", "#e8edf5", "#56647a"),
)


@pytest.fixture(scope="module", autouse=True)
def _application():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture
def toolbar():
    bar = EditorCommandBar()
    # Own the native base style, and keep all palette/style changes local.
    # Changing QApplication's palette here would contaminate other GUI tests.
    style = QStyleFactory.create("Fusion")
    style.setParent(bar)
    bar.setStyle(style)
    yield bar
    bar.close()
    bar.deleteLater()
    QApplication.processEvents()


def _set_theme(bar, dark):
    palette = bar.palette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button):
        palette.setColor(role, QColor("#323232" if dark else "#f0f0f0"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(role, QColor("#ffffff" if dark else "#000000"))
    bar.setPalette(palette)
    for button in bar.findChildren(QToolButton):
        button.setPalette(palette)
    bar.setStyleSheet(STYLESHEET)
    bar.ensurePolished()


def _render_button(button, state, *, focused=False):
    button.setChecked(state.startswith("checked"))
    button.setEnabled("disabled" not in state)
    button.setDown("pressed" in state)
    button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, "hover" in state or "pressed" in state)
    button.resize(360, 42)
    button.ensurePolished()
    option = QStyleOptionToolButton()
    button.initStyleOption(option)
    for flag in (QStyle.StateFlag.State_MouseOver, QStyle.StateFlag.State_Sunken,
                 QStyle.StateFlag.State_Raised, QStyle.StateFlag.State_HasFocus):
        option.state &= ~flag
    if "hover" in state:
        option.state |= QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Raised
    if "pressed" in state:
        option.state |= QStyle.StateFlag.State_Sunken | QStyle.StateFlag.State_MouseOver
    if focused:
        option.state |= QStyle.StateFlag.State_HasFocus
    image = QImage(button.size(), QImage.Format.Format_RGBA8888)
    image.fill(QColor("#f5f7fb"))
    painter = QPainter(image)
    button.style().drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option, painter, button)
    painter.end()
    return image


def _contrast(first, second):
    def luminance(name):
        color = QColor(name)
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [channel / 12.92 if channel <= 0.04045
                  else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    values = sorted((luminance(first), luminance(second)))
    return (values[1] + 0.05) / (values[0] + 0.05)


@pytest.mark.parametrize("dark", (False, True), ids=("light-native", "dark-native"))
@pytest.mark.parametrize("action_name", ("grid_action", "snap_action", "confirm_switching_action"))
@pytest.mark.parametrize("state,background,foreground", _STATE_COLORS)
def test_native_toolbutton_states_have_explicit_readable_colors(
    toolbar, dark, action_name, state, background, foreground,
):
    _set_theme(toolbar, dark)
    button = toolbar.widgetForAction(getattr(toolbar, action_name))
    image = _render_button(button, state)
    assert image.pixelColor(5, 5).name() == background
    # Assert actual rendered ink, not the base QPalette (which omits :checked
    # overrides). No font shape, spelling raster, or screenshot is frozen.
    color = QColor(foreground)
    rgba = bytes((color.red(), color.green(), color.blue(), 255))
    pixels = bytes(image.constBits())
    assert rgba in pixels, f"No {foreground} text ink in {state}"
    assert _contrast(foreground, background) >= 4.5


@pytest.mark.parametrize("dark", (False, True))
def test_keyboard_focus_has_a_visible_border(toolbar, dark):
    _set_theme(toolbar, dark)
    button = toolbar.widgetForAction(toolbar.grid_action)
    image = _render_button(button, "normal", focused=True)
    assert image.pixelColor(0, image.height() // 2).name() == "#2563eb"


def test_compact_orientation_buttons_keep_names_and_absolute_action_api(toolbar):
    toolbar.set_selection_count(1)
    angles = []
    toolbar.rotationPositionRequested.connect(angles.append)
    for action in (toolbar.vertical_orientation_action, toolbar.horizontal_orientation_action):
        button = toolbar.widgetForAction(action)
        assert isinstance(button, QToolButton)
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert not action.icon().isNull()
        assert button.accessibleName() == action.text()
        assert action.text() in action.toolTip()
        assert button.accessibleDescription()
        action.trigger()
    assert angles == [90, 180]
    assert toolbar.rotate_right_action is toolbar.vertical_orientation_action
    assert toolbar.rotate_left_action is toolbar.horizontal_orientation_action
    toolbar.set_selection_count(0)
    assert not toolbar.vertical_orientation_action.isEnabled()
    assert not toolbar.horizontal_orientation_action.isEnabled()


@pytest.mark.parametrize("vertical", (False, True))
@pytest.mark.parametrize("scale", (1.0, 2.0))
@pytest.mark.parametrize("mode", (QIcon.Mode.Normal, QIcon.Mode.Disabled))
def test_orientation_icons_are_distinct_vectors_at_normal_and_high_dpi(toolbar, vertical, scale, mode):
    action = toolbar.vertical_orientation_action if vertical else toolbar.horizontal_orientation_action
    pixmap = action.icon().pixmap(QSize(24, 24), scale, mode, QIcon.State.Off)
    assert pixmap.size() == QSize(round(24 * scale), round(24 * scale))
    assert pixmap.devicePixelRatio() == scale
    image = pixmap.toImage()
    ink = [(x, y) for x in range(image.width()) for y in range(image.height())
           if image.pixelColor(x, y).alpha() > 128]
    assert ink
    width = max(x for x, _ in ink) - min(x for x, _ in ink)
    height = max(y for _, y in ink) - min(y for _, y in ink)
    assert (height > width) == vertical


def test_overflow_menu_retains_readable_orientation_action_names(toolbar):
    window = QMainWindow()
    window.addToolBar(toolbar)
    window.setStyleSheet(STYLESHEET)
    window.resize(320, 160)
    window.show()
    try:
        QApplication.processEvents()
        extension = toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        assert extension is not None and extension.isVisible()
        menu = extension.menu()
        assert menu is not None
        for action in (toolbar.vertical_orientation_action, toolbar.horizontal_orientation_action):
            assert action in menu.actions()
            assert action.text() and action.text() in action.toolTip()
    finally:
        window.removeToolBar(toolbar)
        toolbar.setParent(None)
        window.close()
        window.deleteLater()
