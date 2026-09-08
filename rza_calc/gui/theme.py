# -*- coding: utf-8 -*-
"""Светлая инженерная тема первого интерфейса."""

from ..editor.symbols import (
    DIAGRAM_DEENERGIZED_STROKE,
    DIAGRAM_MONOCHROME_STROKE,
    DIAGRAM_NEUTRAL_STROKE,
    DIAGRAM_OUT_OF_SERVICE_STROKE,
    VOLTAGE_STROKES_BY_NOMINAL_V,
    DiagramColorMode,
    voltage_stroke,
)

COLORS = {
    "background": "#F5F7FB",
    "surface": "#FFFFFF",
    "surface_alt": "#F8FAFD",
    "border": "#DCE3ED",
    "border_strong": "#CBD5E1",
    "text": "#172033",
    "muted": "#68758A",
    "blue": "#2563EB",
    "blue_dark": "#1949B8",
    "blue_soft": "#EAF1FF",
    "green": "#169B62",
    "green_soft": "#E8F7EF",
    "red": "#DC3545",
    "amber": "#D97706",
    "amber_soft": "#FFF7E6",
}


STYLESHEET = """
* {
    font-family: "Segoe UI";
    font-size: 12px;
    color: #172033;
}
/*  Диалоги обязаны нести СВОЙ фон, а не наследовать системный.
    Правило `*` выше задаёт тёмный текст всему приложению, но у QMessageBox
    фона не было, и в тёмной теме Windows он приходил тёмным: сообщение об
    ошибке становилось нечитаемым — тёмное по тёмному, — и его приходилось
    выделять мышью, чтобы прочесть. Сообщение об ошибке, которое нельзя
    прочесть, хуже отсутствующего: человек видит, что что-то не так, и не
    знает что.  */
QMessageBox, QInputDialog, QDialog, QFileDialog {
    background: #FFFFFF;
    color: #172033;
}
QMessageBox QLabel, QInputDialog QLabel, QDialog QLabel {
    color: #172033;
    background: transparent;
}
QMessageBox QPushButton, QInputDialog QPushButton, QDialog QPushButton {
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #C9D4E4;
    border-radius: 4px;
    padding: 5px 16px;
    min-width: 84px;
}
QMessageBox QPushButton:hover, QInputDialog QPushButton:hover,
QDialog QPushButton:hover { background: #F1F5FB; border-color: #B9C7DC; }
QMessageBox QPushButton:default, QInputDialog QPushButton:default {
    background: #2563EB;
    color: #FFFFFF;
    border-color: #2563EB;
}
QMainWindow, QWidget#rootWindow {
    background: #F5F7FB;
}
QWidget#header, QWidget#leftPanel, QWidget#inspector, QWidget#bottomPanel {
    background: #FFFFFF;
}
QWidget#header {
    border-bottom: 1px solid #DCE3ED;
}
QFrame#card, QFrame#modeCard, QFrame#summaryCard, QFrame#resultCard {
    background: #FFFFFF;
    border: 1px solid #DCE3ED;
    border-radius: 8px;
}
QLabel#brand {
    color: #173B7A;
    font-size: 20px;
    font-weight: 700;
}
QLabel#brandSub, QLabel#muted, QLabel#cardCaption, QLabel#helpText {
    color: #68758A;
}
QLabel#sectionTitle, QLabel#cardTitle {
    font-size: 13px;
    font-weight: 650;
}
QLabel#inspectorTitle {
    font-size: 16px;
    font-weight: 700;
}
QLabel#summaryValue {
    font-size: 21px;
    font-weight: 750;
}
QLabel#statusOk { color: #169B62; font-weight: 650; }
QLabel#statusFail { color: #DC3545; font-weight: 650; }
QLabel#statusWarn { color: #D97706; font-weight: 650; }
QPushButton {
    min-height: 30px;
    padding: 0 11px;
    border: 1px solid #D7DEE9;
    border-radius: 6px;
    background: #FFFFFF;
}
QPushButton:hover { background: #F1F5FB; border-color: #B9C7DC; }
QPushButton:pressed { background: #E7EDF7; }
QPushButton#primaryButton, QPushButton#modeButton:checked {
    color: white;
    background: #2563EB;
    border-color: #2563EB;
    font-weight: 650;
}
QPushButton#primaryButton:hover, QPushButton#modeButton:checked:hover {
    background: #1949B8;
}
QPushButton#toolButton {
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    padding: 0;
    font-size: 16px;
}
QPushButton#actionButton {
    min-width: 68px;
    min-height: 46px;
    color: #344155;
}
QPushButton#modeButton {
    min-width: 106px;
}
QToolButton {
    min-height: 26px;
    padding: 3px 8px;
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #D7DEE9;
    border-radius: 6px;
}
QToolButton:hover {
    color: #172033;
    background: #F1F5FB;
    border-color: #B9C7DC;
}
QToolButton:checked {
    color: #174FBF;
    background: #E4EDFF;
    border-color: #8CACF1;
}
QToolButton:checked:hover {
    color: #174FBF;
    background: #D5E4FF;
    border-color: #4E83EE;
}
QToolButton:pressed, QToolButton:checked:pressed {
    color: #172033;
    background: #C8DAFA;
    border-color: #2563EB;
}
QToolButton:focus { border-color: #2563EB; }
QToolButton:disabled {
    color: #56647A;
    background: #F3F5F8;
    border-color: #E1E6EE;
}
QToolButton:checked:disabled {
    color: #56647A;
    background: #E8EDF5;
    border-color: #B9C7DC;
}
QLineEdit, QComboBox {
    min-height: 30px;
    padding: 0 9px;
    background: #FFFFFF;
    border: 1px solid #D7DEE9;
    border-radius: 6px;
    selection-background-color: #CFE0FF;
    selection-color: #172033;
}
QLineEdit:focus, QComboBox:focus { border-color: #4E83EE; }
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled,
QCheckBox:disabled, QLabel:disabled, QPushButton:disabled {
    color: #68758A;
}
QAbstractSpinBox {
    background: #FFFFFF;
    color: #172033;
    border: 1px solid #D7DEE9;
    border-radius: 4px;
    selection-background-color: #CFE0FF;
    selection-color: #172033;
}
QScrollArea, QScrollArea > QWidget > QWidget {
    background: #FFFFFF;
}
QMenuBar, QMenu {
    background: #FFFFFF;
    color: #172033;
}
QMenu { border: 1px solid #CBD5E1; padding: 4px; }
QMenu::item { padding: 6px 28px; background: transparent; }
QMenu::item:selected, QMenuBar::item:selected, QMenuBar::item:pressed {
    background: #EAF1FF;
    color: #172033;
}
QMenu::item:disabled { color: #68758A; }
QMenu::separator { height: 1px; background: #DCE3ED; margin: 4px 8px; }
QComboBox QAbstractItemView {
    background: #FFFFFF;
    color: #172033;
    border: 1px solid #CBD5E1;
    selection-background-color: #CFE0FF;
    selection-color: #172033;
}
QTreeView, QTableView, QListView, QTextEdit, QPlainTextEdit {
    background: #FFFFFF;
    color: #172033;
    border: none;
    outline: none;
    alternate-background-color: #F8FAFD;
    selection-background-color: #CFE0FF;
    selection-color: #172033;
}
QTreeWidget::item {
    min-height: 27px;
    border-radius: 5px;
    padding: 1px 3px;
}
QTreeWidget::item:hover { background: #F1F5FB; }
QTreeWidget::item:selected { background: #E4EDFF; color: #174FBF; }
QHeaderView::section {
    background: #F7F9FC;
    color: #68758A;
    border: none;
    border-bottom: 1px solid #DCE3ED;
    padding: 7px;
    font-weight: 600;
}
QTableWidget::item {
    padding: 5px;
    border-bottom: 1px solid #EDF1F6;
}
QTabWidget::pane { border: none; background: #FFFFFF; }
QTabBar::tab {
    min-width: 72px;
    padding: 10px 9px 8px 9px;
    color: #68758A;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: #2563EB; }
QTabBar::tab:selected {
    color: #2563EB;
    border-bottom-color: #2563EB;
    font-weight: 650;
}
QSplitter::handle { background: #E4E9F1; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QScrollBar:vertical { width: 10px; background: transparent; }
QScrollBar::handle:vertical { background: #C8D1DE; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip {
    background: #FFFFFF;
    color: #172033;
    border: 1px solid #CBD5E1;
    padding: 5px;
}
"""


def apply_light_theme(app) -> None:
    """Keep native controls and unstyled containers readable on dark Windows."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: COLORS["surface"],
        QPalette.ColorRole.WindowText: COLORS["text"],
        QPalette.ColorRole.Base: COLORS["surface"],
        QPalette.ColorRole.AlternateBase: COLORS["surface_alt"],
        QPalette.ColorRole.Text: COLORS["text"],
        QPalette.ColorRole.Button: COLORS["surface"],
        QPalette.ColorRole.ButtonText: COLORS["text"],
        QPalette.ColorRole.BrightText: COLORS["surface"],
        QPalette.ColorRole.Highlight: COLORS["blue"],
        QPalette.ColorRole.HighlightedText: COLORS["surface"],
        QPalette.ColorRole.PlaceholderText: COLORS["muted"],
        QPalette.ColorRole.ToolTipBase: COLORS["surface"],
        QPalette.ColorRole.ToolTipText: COLORS["text"],
        QPalette.ColorRole.Link: COLORS["blue"],
        QPalette.ColorRole.LinkVisited: COLORS["blue_dark"],
        QPalette.ColorRole.Light: COLORS["surface"],
        QPalette.ColorRole.Midlight: COLORS["surface_alt"],
        QPalette.ColorRole.Mid: COLORS["border"],
        QPalette.ColorRole.Dark: COLORS["border_strong"],
        QPalette.ColorRole.Shadow: COLORS["muted"],
        QPalette.ColorRole.Accent: COLORS["blue"],
    }
    for role, color in roles.items():
        # setColor(role, color) covers active, inactive and disabled windows.
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS["muted"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight,
                     QColor(COLORS["border"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText,
                     QColor(COLORS["muted"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)


__all__ = [
    "apply_light_theme",
    "COLORS",
    "DIAGRAM_DEENERGIZED_STROKE",
    "DIAGRAM_MONOCHROME_STROKE",
    "DIAGRAM_NEUTRAL_STROKE",
    "DIAGRAM_OUT_OF_SERVICE_STROKE",
    "DiagramColorMode",
    "STYLESHEET",
    "VOLTAGE_STROKES_BY_NOMINAL_V",
    "voltage_stroke",
]
