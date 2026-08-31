"""Isolated native-Qt evidence, without opening or saving an electrical project."""
import ast
import os
from pathlib import Path
import sys
import zipfile

os.environ["QT_QPA_PLATFORM"] = "windows"
ROOT = Path(r"C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QStyle, QStyleFactory, QStyleOptionToolButton, QToolButton
from rza_calc.gui.editor_panels import EditorCommandBar
from rza_calc.gui.theme import STYLESHEET

app = QApplication([])
app.setStyle("Fusion")
OUT = Path(__file__).resolve().parent
ARCHIVE = Path(r"C:\Users\shock\Documents\Codex\РЗА — резерв\2026-08-31-schematic-before-162446\before-schematic-fixes.zip")
with zipfile.ZipFile(ARCHIVE) as archive:
    name = next(name for name in archive.namelist() if name.endswith("rza_calc/gui/theme.py"))
    parsed = ast.parse(archive.read(name).decode("utf-8-sig"))
    before = next(ast.literal_eval(node.value) for node in parsed.body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "STYLESHEET" for target in node.targets))

STATES = (
    ("normal", "Обычная"), ("hover", "Наведение"), ("pressed", "Нажатие"),
    ("checked", "Включена"), ("checked_hover", "Включена: наведение"),
    ("checked_pressed", "Включена: нажатие"), ("disabled", "Недоступна"),
    ("checked_disabled", "Включена: недоступна"),
)


def toolbar(css):
    bar = EditorCommandBar()
    style = QStyleFactory.create("Fusion")
    style.setParent(bar)
    bar.setStyle(style)
    palette = bar.palette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button):
        palette.setColor(role, QColor("#323232"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(role, QColor("#ffffff"))
    bar.setPalette(palette)
    for button in bar.findChildren(QToolButton):
        button.setPalette(palette)
    bar.setStyleSheet(css)
    bar.ensurePolished()
    return bar


def tile(button, state, width=315):
    button.setChecked(state.startswith("checked"))
    button.setEnabled("disabled" not in state)
    button.setDown("pressed" in state)
    button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, "hover" in state or "pressed" in state)
    button.resize(width, 42)
    button.ensurePolished()
    option = QStyleOptionToolButton()
    button.initStyleOption(option)
    for flag in (QStyle.StateFlag.State_MouseOver, QStyle.StateFlag.State_Sunken,
                 QStyle.StateFlag.State_Raised, QStyle.StateFlag.State_HasFocus):
        option.state &= ~flag
    if "hover" in state:
        option.state |= QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Raised
    if "pressed" in state:
        option.state |= QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken
    image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#f5f7fb"))
    painter = QPainter(image)
    button.style().drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option, painter, button)
    painter.end()
    return image


for tag, css, title in (("before", before, "До исправления"), ("after", STYLESHEET, "После исправления")):
    bar = toolbar(css)
    image = QImage(1170, 595, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#f5f7fb"))
    painter = QPainter(image)
    painter.setPen(QColor("#172033"))
    painter.setFont(QFont("Segoe UI", 13))
    painter.drawText(QRect(16, 12, 1140, 30), title)
    painter.setFont(QFont("Segoe UI", 9))
    painter.drawText(QRect(16, 43, 1140, 25), "Windows Qt / Fusion. Контрольная тёмная палитра; настоящие кнопки EditorCommandBar.")
    for row, (state, label) in enumerate(STATES):
        y = 78 + row * 61
        painter.drawText(QRect(16, y, 188, 42), Qt.AlignmentFlag.AlignVCenter, label)
        for col, action in enumerate((bar.grid_action, bar.snap_action, bar.confirm_switching_action)):
            painter.drawImage(205 + col * 320, y, tile(bar.widgetForAction(action), state))
    painter.end()
    filename = OUT / f"native-toolbar-{tag}-dark.png"
    assert image.save(str(filename))
    print(filename)
    bar.close()
    bar.deleteLater()
    app.processEvents()

bar = toolbar(STYLESHEET)
bar.set_selection_count(1)
image = QImage(700, 190, QImage.Format.Format_ARGB32_Premultiplied)
image.fill(QColor("#f5f7fb"))
painter = QPainter(image)
painter.setPen(QColor("#172033"))
painter.setFont(QFont("Segoe UI", 12))
painter.drawText(QRect(16, 12, 660, 30), "Ориентация выбранного аппарата")
painter.setFont(QFont("Segoe UI", 10))
for index, action in enumerate((bar.vertical_orientation_action, bar.horizontal_orientation_action)):
    x = 20 + 330 * index
    painter.drawImage(x, 58, tile(bar.widgetForAction(action), "normal", 48))
    painter.drawText(QRect(x + 60, 58, 260, 42), Qt.AlignmentFlag.AlignVCenter, action.text())
painter.setFont(QFont("Segoe UI", 9))
painter.drawText(QRect(16, 124, 660, 50), Qt.TextFlag.TextWordWrap,
                 "На панели — только значки. Полные названия сохранены в подсказках, для экранного диктора и в меню переполнения панели.")
painter.end()
assert image.save(str(OUT / "native-orientation-icons.png"))
print(OUT / "native-orientation-icons.png")
bar.close()
