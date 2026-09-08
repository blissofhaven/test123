"""Read-only overview of a prepared presentation snapshot.

The caller owns selection, freshness, calculation, and panel height. This
module only renders the supplied strings; empty results never become zeros.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHeaderView, QLabel, QLayout, QSizePolicy,
    QTabWidget, QTableView, QVBoxLayout, QWidget,
)


@dataclass(frozen=True, slots=True)
class SummaryCard:
    label: str
    value: str
    note: str = ""
    tone: str = "neutral"


@dataclass(frozen=True, slots=True)
class OverviewTab:
    key: str
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    empty_reason: str = ""

    def __post_init__(self) -> None:
        # Copy caller-owned lists so the snapshot cannot change after display.
        object.__setattr__(self, "headers", tuple(self.headers))
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("Overview rows must match their column headers")


@dataclass(frozen=True, slots=True)
class OverviewResultsSnapshot:
    selection_title: str
    cards: tuple[SummaryCard, ...]
    tabs: tuple[OverviewTab, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cards", tuple(self.cards))
        object.__setattr__(self, "tabs", tuple(self.tabs))
        keys = tuple(tab.key for tab in self.tabs)
        if len(keys) != len(set(keys)):
            raise ValueError("Overview tab keys must be unique")


class _RowsModel(QAbstractTableModel):
    def __init__(self, tab: OverviewTab, parent=None):
        super().__init__(parent)
        self.tab = tab

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tab.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tab.headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.tab.rows[index.row()][index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.tab.headers[section]
        return str(section + 1)

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


def _label(text: str, name: str, parent=None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


class _Card(QFrame):
    def __init__(self, value: SummaryCard, parent=None):
        super().__init__(parent)
        self.setObjectName("overviewCard")
        tone = value.tone if value.tone in {"neutral", "success", "warning", "danger", "info"} else "neutral"
        self.setProperty("tone", tone)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(4)
        self.caption = _label(value.label, "overviewCardLabel", self)
        self.value = _label(value.value, "overviewCardValue", self)
        self.note = _label(value.note, "overviewCardNote", self)
        self.note.setVisible(bool(value.note))
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        layout.addWidget(self.note)


class _TabPage(QWidget):
    def __init__(self, value: OverviewTab, parent=None):
        super().__init__(parent)
        self.key = value.key
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 4)
        self.empty_label = _label(value.empty_reason, "overviewEmptyReason", self)
        self.empty_label.setVisible(bool(value.empty_reason) and not value.rows)
        layout.addWidget(self.empty_label)
        self.table = QTableView(self)
        self.table.setObjectName("overviewTable")
        self.table.setModel(_RowsModel(value, self.table))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultSectionSize(180)
        self.table.setVisible(bool(value.rows))
        layout.addWidget(self.table, 1)
        if not value.rows:
            layout.addStretch(1)


class OverviewResultsWidget(QWidget):
    """Responsive cards and details, rendered without contacting the model."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("overviewResults")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._snapshot: OverviewResultsSnapshot | None = None
        self._card_widgets: list[_Card] = []
        self._card_columns = 0
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(10, 7, 10, 6)
        layout.setSpacing(7)
        self.selection_title = _label("", "overviewSelectionTitle", self)
        layout.addWidget(self.selection_title)
        self.cards_panel = QWidget(self)
        self.cards_layout = QGridLayout(self.cards_panel)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setHorizontalSpacing(8)
        self.cards_layout.setVerticalSpacing(8)
        layout.addWidget(self.cards_panel)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("overviewTabs")
        self.tabs.setUsesScrollButtons(True)
        layout.addWidget(self.tabs, 1)
        self.setStyleSheet("""
            QWidget#overviewResults { background: #F5F7FB; color: #172033; }
            QLabel#overviewSelectionTitle { font-weight: 600; color: #172033; }
            QFrame#overviewCard { background: #FFFFFF; border: 1px solid #DCE3ED;
                border-radius: 6px; }
            QLabel#overviewCardLabel, QLabel#overviewCardNote { color: #68758A;
                background: transparent; border: none; }
            QLabel#overviewCardValue { font-size: 18px; font-weight: 600;
                color: #172033; background: transparent; border: none; }
            QFrame#overviewCard[tone="success"] { border-color: #7AB99D; }
            QFrame#overviewCard[tone="warning"] { border-color: #DCA754; }
            QFrame#overviewCard[tone="danger"] { border-color: #D5828A; }
            QFrame#overviewCard[tone="info"] { border-color: #84A8E6; }
            QLabel#overviewEmptyReason { color: #68758A; }
            QTableView#overviewTable { background: #FFFFFF; color: #172033;
                alternate-background-color: #F8FAFD; gridline-color: #E5EAF2;
                selection-background-color: #EAF1FF; selection-color: #172033; }
        """)

    @property
    def snapshot(self) -> OverviewResultsSnapshot | None:
        return self._snapshot

    def set_snapshot(self, snapshot: OverviewResultsSnapshot) -> None:
        if snapshot == self._snapshot:
            return
        selected = self.tabs.currentWidget()
        selected_key = selected.key if selected is not None else None
        self.selection_title.setText(snapshot.selection_title)
        self.selection_title.setVisible(bool(snapshot.selection_title))
        if self._snapshot is None or snapshot.cards != self._snapshot.cards:
            while self.cards_layout.count():
                self.cards_layout.takeAt(0)
            for card in self._card_widgets:
                card.hide()
                card.deleteLater()
            self._card_widgets = [_Card(card, self.cards_panel) for card in snapshot.cards]
            self.cards_panel.setVisible(bool(self._card_widgets))
            self._card_columns = 0
            self._reflow_cards()
        if self._snapshot is None or snapshot.tabs != self._snapshot.tabs:
            with QSignalBlocker(self.tabs):
                while self.tabs.count():
                    page = self.tabs.widget(0)
                    self.tabs.removeTab(0)
                    page.deleteLater()
                for tab in snapshot.tabs:
                    self.tabs.addTab(_TabPage(tab, self.tabs), tab.title)
                if selected_key is not None:
                    for index, tab in enumerate(snapshot.tabs):
                        if tab.key == selected_key:
                            self.tabs.setCurrentIndex(index)
                            break
        self._snapshot = snapshot

    def _reflow_cards(self) -> None:
        if not self._card_widgets:
            return
        width = max(1, self.width() - 20)
        columns = max(1, min(4, len(self._card_widgets), (width + 8) // 228))
        if columns == self._card_columns:
            return
        while self.cards_layout.count():
            self.cards_layout.takeAt(0)
        for index, card in enumerate(self._card_widgets):
            self.cards_layout.addWidget(card, index // columns, index % columns)
        for column in range(4):
            self.cards_layout.setColumnStretch(column, 1 if column < columns else 0)
        self._card_columns = columns

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_cards()
