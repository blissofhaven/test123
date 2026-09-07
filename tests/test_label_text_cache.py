"""The cached presenter retains exact wrapping and invalidates font context."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem
from rza_calc.gui import label_layout as layout

_APP = None


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


@pytest.mark.parametrize("family,size", [("Segoe UI", 10), ("Arial", 15), ("Consolas", 8)])
@pytest.mark.parametrize("text", [
    "", "Питание КТП-12 · насосная № 2\n3×150 мм² · 1,25 км",
    "Сверхдлинноеназваниеприсоединения" * 5,
    "  Два   пробела\tтабуляция\n\nКонец\n", "中文 العربية A\u0308 → QF ввод",
])
def test_cached_wrapping_is_exactly_the_original_algorithm(family, size, text):
    label = QGraphicsSimpleTextItem()
    label.setFont(QFont(family, size))
    expected = layout.wrap_text(text, label.font())
    assert layout.wrapped_label_text(label, text) == expected
    assert layout.wrapped_label_text(label, text) == expected


def _count_wrapping(monkeypatch):
    calls = []
    original = layout.wrap_text
    def record(text, font):
        calls.append((text, QFont(font)))
        return original(text, font)
    monkeypatch.setattr(layout, "wrap_text", record)
    return calls


def test_same_font_value_reuses_one_entry_but_text_and_font_changes_do_not(monkeypatch):
    calls = _count_wrapping(monkeypatch)
    label = QGraphicsSimpleTextItem()
    font = QFont("Segoe UI", 10)
    label.setFont(font)
    text = "Длинное название линии ГТЭС — ЦП-1 Север, ввод 1 " * 4
    initial = layout.wrapped_label_text(label, text)
    label.setFont(QFont(font))
    assert layout.wrapped_label_text(label, text) == initial
    assert len(calls) == 1
    font.setPointSize(20)
    label.setFont(font)
    assert layout.wrapped_label_text(label, text) == layout.wrap_text(text, font)
    assert len(calls) == 3  # one miss and the independent reference
    layout.wrapped_label_text(label, text + " изменено")
    layout.wrapped_label_text(label, text)
    assert len(calls) == 5  # the item retains only its last string


@pytest.mark.parametrize("change", ["width", "dpi", "database", "application_font"])
def test_changed_font_context_rewraps_without_replacing_the_label(monkeypatch, qt_app, change):
    label = QGraphicsSimpleTextItem()
    label.setFont(QFont("Segoe UI", 10))
    text = "Название длинной линии и её назначение " * 3
    calls = _count_wrapping(monkeypatch)
    layout.wrapped_label_text(label, text)
    old_application_font = QFont(qt_app.font())
    try:
        if change == "width":
            monkeypatch.setattr(layout, "LABEL_WRAP_WIDTH", 90.0)
        elif change == "dpi":
            original_metrics = layout.QFontMetricsF
            class DifferentDpi:
                def __init__(self, font):
                    self.metrics = original_metrics(font)
                def fontDpi(self):
                    return self.metrics.fontDpi() + 24
                def horizontalAdvance(self, text):
                    return self.metrics.horizontalAdvance(text)
            monkeypatch.setattr(layout, "QFontMetricsF", DifferentDpi)
        elif change == "database":
            qt_app.fontDatabaseChanged.emit()
        else:
            updated = QFont(old_application_font)
            updated.setPointSize(17 if updated.pointSize() != 17 else 18)
            qt_app.setFont(updated)
        result = layout.wrapped_label_text(label, text)
        assert len(calls) == 2
        assert layout.wrapped_label_text(label, text) == result
        assert len(calls) == 2
    finally:
        qt_app.setFont(old_application_font)


def test_cache_is_owned_by_each_label_and_never_reuses_other_items(monkeypatch):
    calls = _count_wrapping(monkeypatch)
    first, second = QGraphicsSimpleTextItem(), QGraphicsSimpleTextItem()
    for label in (first, second, first, second):
        layout.wrapped_label_text(label, "Линия")
    assert len(calls) == 2
