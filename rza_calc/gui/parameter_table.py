"""A local, typed equipment spreadsheet; only the preview service commits it."""
from __future__ import annotations

import csv
import io
from dataclasses import replace

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableView,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..domain.electrical import DataConfirmation
from ..editor.parameter_editing import ParameterPatch, ParameterValue
from ..editor.parameter_schema import format_parameter_value, parse_parameter_value, parameter_readiness, unit_label


def parameter_type_title(family, fallback="Оборудование"):
    return {"source": "Питающая система", "generator": "Генератор",
        "transformer_2w": "Двухобмоточный трансформатор", "transformer_3w": "Трёхобмоточный трансформатор",
        "line": "Линия", "switch": "Коммутационный аппарат", "load": "Нагрузка"}.get(family, fallback)


def page_equipment_ids(controller, page_id=None):
    """Physical routes and repeated apparatus images refer to the same row."""
    page_id = page_id or controller.workspace_state.active_page_id
    if page_id is None:
        page_id = next(iter(controller.diagram.pages), None)
    return frozenset(
        item.equipment_id
        for collection in (controller.diagram.representations, controller.diagram.routes)
        for item in collection.values()
        if str(item.page_id) == str(page_id) and item.equipment_id is not None
    )


class EquipmentParameterTableModel(QAbstractTableModel):
    draftChanged = Signal()
    ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1
    KEY_ROLE = ID_ROLE + 1
    FIXED_COLUMNS = 3

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.rows = []
        self.specs = []
        self.family = ""
        self.display_units = {}
        self.drafts = {}  # (stable equipment ID, canonical field key) -> ParameterValue
        self.clears = set()
        self.errors = {}
        self.raw_errors = {}
        self.validation_errors = {}
        self.readiness = {}
        self.reload()

    def reload(self):
        self.beginResetModel()
        self.rows = []
        self.readiness.clear()
        snapshots = self.controller.equipment_parameter_snapshots(tuple(self.controller.model.equipment))
        for snapshot in snapshots:
            equipment = self.controller.model.equipment[snapshot.equipment_id]
            definition = self.controller.model.equipment_type(equipment.type_id, equipment.type_version)
            family = snapshot.family
            self.rows.append((equipment, snapshot, family, parameter_type_title(family, definition.display_name)))
            self.readiness[equipment.id] = snapshot.readiness
        self.rows.sort(key=lambda row: (row[0].name.casefold(), row[0].id.value))
        self.drafts.clear()
        self.clears.clear()
        self.errors.clear()
        self.raw_errors.clear()
        self.validation_errors.clear()
        self._set_specs()
        self.endResetModel()
        self.draftChanged.emit()

    def _set_specs(self):
        rows = [row for row in self.rows if not self.family or row[2] == self.family]
        if not rows:
            self.specs = []
            return
        candidates = tuple(rows[0][1].specs)
        self.specs = [spec for spec in candidates if any(
            other.key == spec.key and other.editable for row in rows for other in row[1].specs
        ) and all(
            any(other.key == spec.key and other.value_kind == spec.value_kind
                and other.unit == spec.unit for other in row[1].specs)
            for row in rows[1:]
        )]
        for spec in self.specs:
            self.display_units.setdefault(spec.key, spec.unit)

    def set_family(self, family):
        self.beginResetModel()
        self.family = family or ""
        self._set_specs()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.FIXED_COLUMNS + len(self.specs)

    def cell_ref(self, index):
        if not index.isValid() or index.column() < self.FIXED_COLUMNS:
            return None
        return self.rows[index.row()][0].id, self.specs[index.column() - self.FIXED_COLUMNS].key

    def field_spec(self, index):
        ref = self.cell_ref(index)
        return next((spec for spec in self.rows[index.row()][1].specs if spec.key == ref[1]), None) if ref else None

    def value_at(self, index):
        ref = self.cell_ref(index)
        if ref in self.clears:
            return ParameterValue(None)
        return self.drafts.get(ref, self.rows[index.row()][1].fields.get(ref[1], ParameterValue(None)))

    def row_missing(self, row):
        return any(issue.code == "missing_parameter" for issue in self.readiness[self.rows[row][0].id])

    def _refresh_row_readiness(self, index):
        equipment, snapshot, family, title = self.rows[index.row()]
        values = dict(snapshot.fields)
        values.update({key: value for (eid, key), value in self.drafts.items() if eid == equipment.id})
        values.update({key: ParameterValue(None) for eid, key in self.clears if eid == equipment.id})
        self.readiness[equipment.id] = parameter_readiness(snapshot.specs, values, family=family)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        equipment, snapshot, family, title = self.rows[index.row()]
        if role == self.ID_ROLE:
            return equipment.id
        if role == self.KEY_ROLE:
            ref = self.cell_ref(index)
            return ref[1] if ref else None
        if index.column() < self.FIXED_COLUMNS:
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
                return (equipment.name, title, "Есть пропуски" if self.row_missing(index.row()) else "Заполнено")[index.column()]
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{equipment.name}\n{equipment.id.value}"
            return None
        spec = self.field_spec(index)
        ref = self.cell_ref(index)
        value = self.value_at(index)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if ref in self.raw_errors:
                return self.raw_errors[ref]
            if ref in self.clears and role == Qt.ItemDataRole.DisplayRole:
                return "Будет очищено"
            return format_parameter_value(spec, value.value, self.display_units.get(spec.key))
        if role == Qt.ItemDataRole.BackgroundRole:
            if ref in self.errors or ref in self.validation_errors:
                return QColor("#ffe0dc")
            if ref in self.drafts or ref in self.clears:
                return QColor("#fff2cc")
        if role == Qt.ItemDataRole.ToolTipRole:
            state = "Подтверждено" if value.confirmation == DataConfirmation.CONFIRMED else "Не подтверждено"
            return "\n".join(filter(None, (equipment.name, self.errors.get(ref) or self.validation_errors.get(ref), spec.help_text,
                f"Источник: {value.source or 'не указан'}", state,
                "Пустой ввод оставляет значение. Для удаления используйте «Очистить явно».")))
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        if section < self.FIXED_COLUMNS:
            return ("Оборудование", "Тип", "Заполнение")[section]
        spec = self.specs[section - self.FIXED_COLUMNS]
        unit = self.display_units.get(spec.key, spec.unit)
        label = spec.label
        if sum(other.label == spec.label for other in self.specs) > 1:
            label = spec.group + "\n" + label
        return label + (f", {unit_label(unit)}" if unit else "")

    def flags(self, index):
        result = super().flags(index)
        if index.isValid() and (spec := self.field_spec(index)) is not None and spec.editable:
            result |= Qt.ItemFlag.ItemIsEditable
        return result

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or self.cell_ref(index) is None:
            return False
        ref = self.cell_ref(index)
        spec = self.field_spec(index)
        if not spec.editable:
            return False
        text = str(value)
        if not text.strip():
            return True  # Excel blank means KEEP, including an earlier draft.
        try:
            parsed = parse_parameter_value(spec, text, self.display_units.get(spec.key))
            original = self.value_at(index)
            self.drafts[ref] = replace(original, value=parsed,
                confirmation=DataConfirmation.UNCONFIRMED, origin="manual")
            self.clears.discard(ref)
            self.errors.pop(ref, None)
            self.raw_errors.pop(ref, None)
        except (ValueError, TypeError) as exc:
            self.errors[ref] = str(exc)
            self.raw_errors[ref] = text
        self._refresh_row_readiness(index)
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), self.columnCount() - 1))
        self.draftChanged.emit()
        return True

    def set_parameter_value(self, index, value):
        ref = self.cell_ref(index)
        if ref is None or not self.field_spec(index).editable:
            return
        self.drafts[ref] = value
        self.clears.discard(ref)
        self.errors.pop(ref, None)
        self.raw_errors.pop(ref, None)
        self._refresh_row_readiness(index)
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), self.columnCount() - 1))
        self.draftChanged.emit()

    def clear_cell(self, index):
        ref = self.cell_ref(index)
        if ref is None:
            return
        spec = self.field_spec(index)
        if not spec.nullable or not spec.editable:
            raise ValueError(f"{spec.label}: поле нельзя очистить.")
        self.drafts.pop(ref, None)
        self.errors.pop(ref, None)
        self.raw_errors.pop(ref, None)
        self.clears.add(ref)
        self._refresh_row_readiness(index)
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), self.columnCount() - 1))
        self.draftChanged.emit()

    def set_display_unit(self, key, unit):
        if self.errors:
            raise ValueError("Сначала исправьте ошибочный ввод: его единицы нельзя менять.")
        self.display_units[key] = unit
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.columnCount() - 1)
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))

    def patches(self):
        if self.errors:
            raise ValueError("Исправьте отмеченные ячейки до проверки изменений.")
        ids = {eid for eid, key in self.drafts} | {eid for eid, key in self.clears}
        return tuple(ParameterPatch(eid,
            values={**{key: value for (owner, key), value in self.drafts.items() if owner == eid},
                    **{key: ParameterValue(None, origin="manual") for owner, key in self.clears if owner == eid}})
            for eid in sorted(ids, key=lambda value: value.value))


class EquipmentParameterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.page_ids = None
        self.family = ""
        self.only_missing = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        equipment, snapshot, family, title = model.rows[row]
        return ((self.page_ids is None or equipment.id in self.page_ids)
                and (not self.family or self.family == family)
                and (not self.only_missing or model.row_missing(row)))


class EquipmentParameterTableDialog(QDialog):
    commandApplied = Signal(object)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.preview = None
        self.setWindowTitle("Исходные данные — таблица оборудования")
        self.resize(1180, 760)
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("Текущий лист", False)
        self.scope_combo.addItem("Весь проект", True)
        self.family_combo = QComboBox()
        self.family_combo.addItem("Все типы — общие поля", "")
        self.model = EquipmentParameterTableModel(controller, self)
        families = {}
        for equipment, snapshot, family, title in self.model.rows:
            families.setdefault(family, title)
        for key, title in sorted(families.items(), key=lambda item: item[1]):
            self.family_combo.addItem(title, key)
        self.missing_check = QCheckBox("Только с пропусками")
        filters.addWidget(self.scope_combo)
        filters.addWidget(self.family_combo)
        filters.addWidget(self.missing_check)
        filters.addStretch()
        filters.addWidget(QLabel("Единица выбранного столбца:"))
        self.unit_combo = QComboBox()
        filters.addWidget(self.unit_combo)
        layout.addLayout(filters)
        self.proxy = EquipmentParameterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setObjectName("equipmentParameterTable")
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setDefaultSectionSize(165)
        self.table.setColumnWidth(0, 280)
        layout.addWidget(self.table, 1)
        self.hint = QLabel("Пустые ячейки вставки оставляют исходные значения. Выберите тип для его параметров. Изменения пока не сохранены.")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        for title, callback in (("Вставить из Excel", self.paste_clipboard),
                                ("Очистить явно", self.clear_selected),
                                ("Источник и подтверждение…", self.edit_selected_value),
                                ("Карточка оборудования…", self.open_selected_card)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        self.preview_button = QPushButton("Проверить все изменения")
        self.preview_button.clicked.connect(self.preview_changes)
        actions.addWidget(self.preview_button)
        self.apply_button = QPushButton("Применить проверенные изменения")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_changes)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self.diff = QTreeWidget()
        self.diff.setHeaderLabels(["Оборудование / поле", "Было", "Станет / ошибка"])
        self.diff.header().setStretchLastSection(False)
        self.diff.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.diff.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.diff.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.diff.setMaximumHeight(220)
        layout.addWidget(self.diff)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.scope_combo.currentIndexChanged.connect(self._filter)
        self.family_combo.currentIndexChanged.connect(self._family_changed)
        self.missing_check.toggled.connect(self._filter)
        self.model.draftChanged.connect(self._invalidate_preview)
        self.table.selectionModel().currentChanged.connect(self._selected_column)
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        self.paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self.table)
        self.paste_shortcut.activated.connect(self.paste_clipboard)
        self._filter()

    def _family_changed(self):
        self.model.set_family(self.family_combo.currentData())
        self._filter()

    def _filter(self):
        self.proxy.page_ids = None if self.scope_combo.currentData() else page_equipment_ids(self.controller)
        self.proxy.family = self.family_combo.currentData()
        self.proxy.only_missing = self.missing_check.isChecked()
        self.proxy.invalidateFilter()

    def _invalidate_preview(self):
        self.preview = None
        self.model.validation_errors.clear()
        self.apply_button.setEnabled(False)
        self.diff.clear()
        self.hint.setText(f"Изменённых ячеек: {len(self.model.drafts) + len(self.model.clears)}; ошибок: {len(self.model.errors)}. Проверьте весь пакет перед применением.")

    def _selected_column(self, current, previous=QModelIndex()):
        index = self.proxy.mapToSource(current)
        self.unit_combo.blockSignals(True)
        self.unit_combo.clear()
        if self.model.cell_ref(index):
            spec = self.model.specs[index.column() - self.model.FIXED_COLUMNS]
            for unit in dict.fromkeys((spec.unit, *spec.display_units)):
                self.unit_combo.addItem(unit_label(unit), unit)
            self.unit_combo.setCurrentIndex(self.unit_combo.findData(self.model.display_units.get(spec.key, spec.unit)))
        self.unit_combo.blockSignals(False)

    def _unit_changed(self):
        index = self.proxy.mapToSource(self.table.currentIndex())
        ref = self.model.cell_ref(index)
        if ref and self.unit_combo.currentData():
            try:
                self.model.set_display_unit(ref[1], self.unit_combo.currentData())
            except ValueError as exc:
                self.hint.setText(str(exc))
                self._selected_column(self.table.currentIndex())

    def paste_text(self, text, anchor=None):
        anchor = anchor if anchor is not None else self.table.currentIndex()
        if not anchor.isValid():
            raise ValueError("Выберите начальную ячейку параметра.")
        cells = list(csv.reader(io.StringIO(text), delimiter="\t"))
        if not cells:
            return
        width = max(map(len, cells))
        if anchor.row() + len(cells) > self.proxy.rowCount() or anchor.column() + width > self.proxy.columnCount():
            raise ValueError("Блок выходит за видимые строки или столбцы. Ничего не вставлено.")
        # Freeze the targets before editing: sorting and missing filters may move rows.
        targets = []
        for y, row in enumerate(cells):
            for x, value in enumerate(row):
                index = self.proxy.mapToSource(self.proxy.index(anchor.row() + y, anchor.column() + x))
                if value.strip() and (self.model.field_spec(index) is None or not self.model.field_spec(index).editable):
                    raise ValueError("Блок содержит столбец названия или статуса. Начните с параметра.")
                targets.append((index, value))
        for index, value in targets:
            if value.strip():
                self.model.setData(index, value)

    def paste_clipboard(self):
        try:
            self.paste_text(QApplication.clipboard().text())
        except ValueError as exc:
            self.hint.setText(str(exc))

    def clear_selected(self):
        targets = [self.proxy.mapToSource(index) for index in self.table.selectedIndexes()]
        if any(self.model.cell_ref(index) and (not self.model.field_spec(index).nullable or not self.model.field_spec(index).editable) for index in targets):
            self.hint.setText("В выделении есть обязательное поле: очистка не выполнена.")
            return
        for index in targets:
            self.model.clear_cell(index)

    def edit_selected_value(self):
        from .equipment_parameters import ParameterFieldEditor
        index = self.proxy.mapToSource(self.table.currentIndex())
        if self.model.cell_ref(index) is None:
            self.hint.setText("Выберите ячейку параметра.")
            return
        spec = self.model.field_spec(index)
        dialog = QDialog(self)
        dialog.setWindowTitle(spec.label)
        layout = QVBoxLayout(dialog)
        editor = ParameterFieldEditor(spec, self.model.value_at(index), dialog)
        layout.addWidget(editor)
        error = QLabel()
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        def accept():
            try:
                value = editor.value()
                if value.value is None:
                    self.model.clear_cell(index)
                else:
                    self.model.set_parameter_value(index, value)
            except ValueError as exc:
                error.setText(str(exc))
                return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def open_selected_card(self):
        from .equipment_parameters import EquipmentParameterCardDialog
        if self.model.drafts or self.model.clears or self.model.errors:
            self.hint.setText("Сначала примените правки таблицы или закройте её без применения. Карточка откроет отдельный ввод.")
            return
        index = self.proxy.mapToSource(self.table.currentIndex())
        if not index.isValid():
            self.hint.setText("Выберите оборудование в таблице.")
            return
        dialog = EquipmentParameterCardDialog(self.controller, self.model.rows[index.row()][0].id, self)
        dialog.commandApplied.connect(self.commandApplied.emit)
        dialog.exec()
        self.model.reload()

    def preview_changes(self):
        self.apply_button.setEnabled(False)
        self.diff.clear()
        try:
            patches = self.model.patches()
            if not patches:
                raise ValueError("Нет изменений для применения.")
            self.preview = self.controller.preview_parameter_patch(patches)
            lookup = {row[0].id: row for row in self.model.rows}
            for change in self.preview.changes:
                row = lookup[change.equipment_id]
                spec = next(item for item in row[1].specs if item.key == change.key)
                unit = self.model.display_units.get(spec.key, spec.unit)
                label = row[0].name + " / " + spec.label + (f", {unit_label(unit)}" if unit else "")
                if sum(other.label == spec.label for other in row[1].specs) > 1:
                    label = row[0].name + " / " + spec.group + " / " + spec.label + (f", {unit_label(unit)}" if unit else "")
                item = QTreeWidgetItem([label,
                    format_parameter_value(spec, change.before.value, unit),
                    format_parameter_value(spec, change.after.value, unit)])
                item.setToolTip(2, f"Источник: {change.after.source or 'не указан'}\n"
                    + ("Подтверждено" if change.after.confirmation == DataConfirmation.CONFIRMED else "Не подтверждено"))
                self.diff.addTopLevelItem(item)
            for error in self.preview.errors:
                row = lookup.get(error.equipment_id)
                title = row[0].name if row else "Проект"
                if row and error.key:
                    self.model.validation_errors[(error.equipment_id, error.key)] = error.message
                self.diff.addTopLevelItem(QTreeWidgetItem([title + " / " + error.key, "", error.message]))
            if self.model.validation_errors:
                self.model.dataChanged.emit(self.model.index(0, 0),
                    self.model.index(self.model.rowCount() - 1, self.model.columnCount() - 1))
            self.apply_button.setEnabled(self.preview.valid and bool(self.preview.changes))
            self.hint.setText("Проверьте весь пакет ниже. Применение — одна команда истории, без автоматического расчёта.")
        except (ValueError, RuntimeError) as exc:
            self.preview = None
            self.hint.setText(str(exc))

    def apply_changes(self):
        if self.preview is None or not self.apply_button.isEnabled():
            return
        try:
            result = self.controller.apply_parameter_preview(self.preview)
        except (ValueError, RuntimeError) as exc:
            self.hint.setText(str(exc))
            self.preview = None
            self.apply_button.setEnabled(False)
            return
        self.model.reload()
        self.commandApplied.emit(result)
        self.hint.setText("Изменения применены. Расчёт запускается отдельно.")
