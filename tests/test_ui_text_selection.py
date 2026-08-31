# -*- coding: utf-8 -*-
"""Selected input text stays readable on the application's light highlight."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit  # noqa: E402

from rza_calc.gui.theme import STYLESHEET  # noqa: E402


_APP: QApplication | None = None


@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


def _luminance(color: QColor) -> float:
    channels = (color.redF(), color.greenF(), color.blueF())
    linear = [
        channel / 12.92 if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


@pytest.mark.parametrize("kind", ("line_edit", "editable_combo"))
@pytest.mark.parametrize("group", (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive))
def test_selected_input_text_has_explicit_contrasting_colors(kind, group):
    text = "Нагрузка 1 — параметры и мощность"
    if kind == "line_edit":
        widget = editor = QLineEdit(text)
    else:
        widget = QComboBox()
        widget.setEditable(True)
        widget.addItem(text)
        editor = widget.lineEdit()
        assert editor is not None

    try:
        # Apply the real stylesheet locally so this regression does not change
        # the global application palette/style used by unrelated GUI tests.
        widget.setStyleSheet(STYLESHEET)
        widget.ensurePolished()
        editor.ensurePolished()
        editor.selectAll()
        assert editor.selectedText() == text
        palette = editor.palette()
        foreground = palette.color(group, QPalette.ColorRole.HighlightedText)
        background = palette.color(group, QPalette.ColorRole.Highlight)
        assert foreground.name() == "#172033"
        assert background.name() == "#cfe0ff"
        first, second = _luminance(foreground), _luminance(background)
        contrast = (max(first, second) + 0.05) / (min(first, second) + 0.05)
        assert contrast >= 7.0, f"Selected text contrast is only {contrast:.2f}:1"
    finally:
        widget.close()
        widget.deleteLater()
