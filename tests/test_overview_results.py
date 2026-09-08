"""Presentation-only overview: no computations, no stale or invented values."""
from dataclasses import FrozenInstanceError, replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from rza_calc.gui.overview_results import (
    OverviewResultsSnapshot, OverviewResultsWidget, OverviewTab, SummaryCard,
)


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    widget = OverviewResultsWidget()
    widget.resize(1200, 500)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def snapshot():
    titles = ("Сеть", "Режимы", "Нагрузки", "КЗ", "Уставки", "Селективность", "Отчёт")
    cards = (
        SummaryCard("Объект", "ПС 35/10 кВ"),
        SummaryCard("Режим", "Минимальный", "Сохранённый режим", "info"),
        SummaryCard("КЗ(3)", "—", "Расчёт не выполнен", "neutral"),
        SummaryCard("Уставки", "Не проверены", "Нужны исходные данные", "warning"),
    )
    return OverviewResultsSnapshot("Выбрана I СШ 10 кВ", cards, tuple(
        OverviewTab(str(index), title, ("Параметр", "Значение"),
                    (("Источник", "Учебное допущение"),))
        for index, title in enumerate(titles)))


def test_snapshot_defensively_freezes_collections_and_rejects_ambiguous_tabs():
    rows = [["I", "1,25 кА"]]
    headers = ["Параметр", "Значение"]
    tab = OverviewTab("fault", "КЗ", headers, rows)
    tabs = [tab]
    cards = [SummaryCard("КЗ", "1,25 кА")]
    value = OverviewResultsSnapshot("Шина", cards, tabs)
    rows[0][1] = "999"
    headers.clear()
    cards.clear()
    tabs.clear()
    assert value.tabs[0].rows == (("I", "1,25 кА"),)
    assert value.tabs[0].headers == ("Параметр", "Значение")
    assert value.cards[0].value == "1,25 кА"
    with pytest.raises(FrozenInstanceError):
        value.cards[0].value = "999"
    with pytest.raises(ValueError, match="unique"):
        OverviewResultsSnapshot("Шина", (), (tab, tab))
    with pytest.raises(ValueError, match="headers"):
        OverviewTab("bad", "КЗ", ("I",), (("1", "2"),))


def test_seven_tabs_render_only_supplied_strings_with_read_only_models(panel):
    value = snapshot()
    panel.set_snapshot(value)
    assert panel.snapshot is value
    assert panel.selection_title.text() == value.selection_title
    assert [panel.tabs.tabText(i) for i in range(panel.tabs.count())] == [tab.title for tab in value.tabs]
    for index, tab in enumerate(value.tabs):
        model = panel.tabs.widget(index).table.model()
        assert model.rowCount() == 1
        assert model.columnCount() == 2
        assert model.data(model.index(0, 1)) == "Учебное допущение"
        assert model.headerData(1, Qt.Orientation.Horizontal) == "Значение"
        assert not model.flags(model.index(0, 1)) & Qt.ItemFlag.ItemIsEditable
    assert panel._card_widgets[2].value.text() == "—"
    assert panel._card_widgets[3].value.text() == "Не проверены"


def test_render_and_tab_navigation_cannot_start_a_calculation(panel, monkeypatch):
    from rza_calc.core import engine
    from rza_calc.core.short_circuit import ShortCircuitSolver
    def forbidden(*args, **kwargs):
        pytest.fail("Overview display must not calculate")
    monkeypatch.setattr(engine, "run", forbidden)
    monkeypatch.setattr(engine, "run_input", forbidden)
    monkeypatch.setattr(ShortCircuitSolver, "__init__", forbidden)
    value = snapshot()
    spy = QSignalSpy(panel.tabs.currentChanged)
    panel.set_snapshot(value)
    assert spy.count() == 0
    for index in range(7):
        panel.tabs.setCurrentIndex(index)
    panel.set_snapshot(replace(value, selection_title="Другой объект"))
    assert spy.count() == 6
    assert panel.tabs.currentIndex() == 6


def test_replacing_snapshot_preserves_tab_key_without_emitting_navigation(panel):
    first = snapshot()
    panel.set_snapshot(first)
    panel.tabs.setCurrentIndex(3)
    spy = QSignalSpy(panel.tabs.currentChanged)
    tabs = tuple(replace(tab, rows=(("Новый", tab.key),)) for tab in reversed(first.tabs))
    panel.set_snapshot(replace(first, tabs=tabs))
    assert spy.count() == 0
    assert panel.tabs.currentWidget().key == "3"
    model = panel.tabs.currentWidget().table.model()
    assert model.data(model.index(0, 0)) == "Новый"


def test_unavailable_snapshot_clears_previous_numbers_and_shows_reason(panel):
    value = snapshot()
    ready = replace(value.tabs[3], rows=(("IA", "2,88 кА"),))
    panel.set_snapshot(replace(value, tabs=(ready,), cards=(SummaryCard("КЗ(3)", "2,88 кА"),)))
    blocked = replace(ready, rows=(), empty_reason="Исходные данные изменились. Выполните КЗ снова.")
    panel.set_snapshot(replace(value, tabs=(blocked,), cards=(SummaryCard("КЗ(3)", "—", "Результат устарел"),)))
    QApplication.processEvents()
    page = panel.tabs.currentWidget()
    assert page.empty_label.isVisible()
    assert page.empty_label.text() == blocked.empty_reason
    assert not page.table.isVisible()
    assert page.table.model().rowCount() == 0
    assert panel._card_widgets[0].value.text() == "—"
    assert "2,88" not in panel._card_widgets[0].value.text()


def test_empty_reason_is_hidden_when_table_has_rows(panel):
    value = OverviewTab("fault", "КЗ", ("Вид", "Результат"),
                        (("КЗ(3)", "2,88 кА"), ("КЗ(1)", "Нет Z0")),
                        "Расчёт не выполнялся.")
    panel.set_snapshot(OverviewResultsSnapshot("Шина", (), (value,)))
    QApplication.processEvents()
    page = panel.tabs.currentWidget()
    assert page.table.isVisible() and not page.empty_label.isVisible()
    assert page.table.model().rowCount() == 2
    assert page.table.model().data(page.table.model().index(1, 1)) == "Нет Z0"


@pytest.mark.parametrize("width,columns", [(1200, 4), (740, 3), (510, 2), (360, 1)])
def test_cards_reflow_without_fixed_panel_height(panel, width, columns):
    value = snapshot()
    panel.set_snapshot(value)
    panel.resize(width, 600)
    QApplication.processEvents()
    assert panel._card_columns == columns
    assert panel.minimumHeight() == 0
    for index, card in enumerate(panel._card_widgets):
        row, column, _, _ = panel.cards_layout.getItemPosition(index)
        assert (row, column) == (index // columns, index % columns)
        assert card.isVisible()
        assert card.geometry().right() <= panel.cards_panel.width()


def test_plain_text_does_not_interpret_equipment_names_as_markup(panel):
    value = OverviewResultsSnapshot("<b>Название</b>",
        (SummaryCard("<i>Узел</i>", "<script>0</script>", "<a>Источник</a>"),),
        (OverviewTab("empty", "Отчёт", (), (), "<b>Нет отчёта</b>"),))
    panel.set_snapshot(value)
    labels = [panel.selection_title, panel._card_widgets[0].caption,
              panel._card_widgets[0].value, panel._card_widgets[0].note,
              panel.tabs.currentWidget().empty_label]
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
    assert panel.selection_title.text() == "<b>Название</b>"


def test_identical_snapshot_preserves_existing_widgets_and_clear_hides_cards(panel):
    value = snapshot()
    panel.set_snapshot(value)
    cards = tuple(panel._card_widgets)
    page = panel.tabs.widget(0)
    panel.set_snapshot(replace(value))
    assert tuple(panel._card_widgets) == cards
    assert panel.tabs.widget(0) is page
    panel.set_snapshot(OverviewResultsSnapshot("Выберите объект", (), ()))
    assert not panel.cards_panel.isVisible()
    assert panel.tabs.count() == 0
    assert panel.selection_title.text() == "Выберите объект"
