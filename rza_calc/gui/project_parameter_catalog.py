"""Project-local engineering brands and an explicitly selected application diff."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSplitter, QTreeWidget, QHeaderView,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..domain.catalog import CatalogCategoryId, CatalogEntry, CatalogEntryId, CatalogOrigin
from ..domain.catalog_compatibility import (
    build_catalog_properties, build_catalog_extensions, catalog_values, parameter_family,
)
from ..domain.electrical import DataConfirmation, EquipmentId, thaw_json
from ..editor.parameter_editing import PROVENANCE_KEY, ParameterValue, is_manual_override
from ..editor.parameter_schema import catalog_field_specs, format_parameter_value, unit_label
from .equipment_parameters import ParameterFieldEditor
from .parameter_table import page_equipment_ids, parameter_type_title


class CatalogEntryEditorDialog(QDialog):
    """Produce one immutable candidate; accept does not save or apply a brand."""

    def __init__(self, controller, type_id, type_version=1, *, entry=None,
                 equipment_id=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.type_id, self.type_version = type_id, type_version
        self.entry = entry
        self.proposal = None
        self.setWindowTitle("Изменить марку" if entry else "Новая марка оборудования")
        self.resize(690, 790)
        outer = QVBoxLayout(self)
        metadata = QFormLayout()
        self.name_edit = QLineEdit(entry.display_name if entry else "")
        self.manufacturer_edit = QLineEdit(entry.manufacturer if entry else "")
        self.model_edit = QLineEdit(entry.model if entry else "")
        self.source_edit = QLineEdit(entry.source if entry else "")
        self.name_edit.setObjectName("catalogBrandName")
        self.source_edit.setObjectName("catalogBrandSource")
        metadata.addRow("Название марки", self.name_edit)
        metadata.addRow("Изготовитель", self.manufacturer_edit)
        metadata.addRow("Модель", self.model_edit)
        metadata.addRow("Источник марки", self.source_edit)
        outer.addLayout(metadata)
        hint = QLabel("Марка хранится в этом проекте. Длина, число параллельных цепей и место ТТ задаются отдельно на экземпляре. Подтверждайте только проверенные значения.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self.specs = catalog_field_specs(controller.model, type_id, type_version,
            properties=entry.properties if entry else None)
        fields = {}
        if equipment_id is not None:
            fields = dict(controller.equipment_parameter_snapshot(equipment_id).fields)
        if entry is not None:
            # The domain codec uses an explicit template type; no equipment is created.
            from types import SimpleNamespace
            context = SimpleNamespace(type_id=type_id, type_version=type_version, properties=entry.properties)
            values = catalog_values(controller.model, context, entry, include_discriminator=True)
            provenance = entry.extensions.get(PROVENANCE_KEY, {})
            fields = {key: ParameterValue(value,
                provenance.get(key, {}).get("source", entry.source),
                provenance.get(key, {}).get("confirmation", DataConfirmation.UNCONFIRMED),
                "catalog") for key, value in values.items()}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.editors = {}
        group = None
        for spec in self.specs:
            if spec.group != group:
                group = spec.group
                title = QLabel(group)
                title.setStyleSheet("font-weight: 600; margin-top: 10px;")
                form.addRow(title)
            editor = ParameterFieldEditor(spec, fields.get(spec.key, ParameterValue(None)), content)
            self.editors[spec.key] = editor
            form.addRow(spec.label, editor)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B42318")
        outer.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def candidate(self):
        name, source = self.name_edit.text().strip(), self.source_edit.text().strip()
        if not name or not source:
            raise ValueError("Укажите название марки и источник данных.")
        values = {key: editor.value() for key, editor in self.editors.items()}
        properties = build_catalog_properties(self.specs, values,
            base_properties=self.entry.properties if self.entry is not None else None)
        extensions = build_catalog_extensions(self.specs, values,
            base_extensions=self.entry.extensions if self.entry else None)
        old_provenance = extensions.get(PROVENANCE_KEY, {})
        provenance = {key: value for key, value in old_provenance.items() if key not in values}
        provenance.update({key: {"source": value.source or source,
            "confirmation": value.confirmation.value, "origin": "catalog"}
            for key, value in values.items() if value.value is not None})
        extensions[PROVENANCE_KEY] = provenance
        if self.entry:
            return replace(self.entry, display_name=name, manufacturer=self.manufacturer_edit.text().strip(),
                model=self.model_edit.text().strip(), source=source, properties=properties,
                extensions=extensions, modified_at=datetime.now(timezone.utc),
                entry_version=self.entry.entry_version + 1)
        return CatalogEntry(CatalogEntryId.new(), CatalogOrigin.USER,
            CatalogCategoryId("parameters." + parameter_family(self.type_id)), name,
            self.type_id, self.type_version, manufacturer=self.manufacturer_edit.text().strip(),
            model=self.model_edit.text().strip(), source=source, properties=properties,
            extensions=extensions)

    def accept(self):
        try:
            self.proposal = self.candidate()
        except (ValueError, TypeError, RuntimeError) as exc:
            self.error_label.setText(str(exc))
            return
        super().accept()


class ProjectParameterCatalogDialog(QDialog):
    commandApplied = Signal(object)

    def __init__(self, controller, equipment_ids=(), parent=None):
        super().__init__(parent)
        self.controller = controller
        self.preview = None
        self.setWindowTitle("Справочник параметров проекта")
        self.resize(1080, 760)
        self._initial_selection = frozenset(equipment_ids)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.entry_combo = QComboBox()
        self.entry_combo.setObjectName("projectParameterBrand")
        bar.addWidget(QLabel("Марка:"))
        bar.addWidget(self.entry_combo, 1)
        self.type_combo = QComboBox()
        seen = set()
        for definition in sorted(controller.model.equipment_types.values(), key=lambda item: item.display_name):
            key = (definition.id, definition.schema_version)
            if key not in seen and catalog_field_specs(controller.model, *key):
                seen.add(key)
                label = parameter_type_title(parameter_family(definition.id), definition.display_name)
                if parameter_family(definition.id) == "line":
                    if str(definition.id).endswith(".cable"):
                        label = "Кабельная линия"
                    elif str(definition.id).endswith(".overhead") or str(definition.id) == "builtin.line":
                        label = "Воздушная линия"
                self.type_combo.addItem(label, key)
        if self._initial_selection:
            equipment = controller.model.equipment[next(iter(self._initial_selection))]
            index = self.type_combo.findData((equipment.type_id, equipment.type_version))
            if index >= 0:
                self.type_combo.setCurrentIndex(index)
        bar.addWidget(self.type_combo)
        self.new_button = QPushButton("Новая марка…")
        self.edit_button = QPushButton("Изменить марку…")
        self.new_button.clicked.connect(self.new_entry)
        self.edit_button.clicked.connect(self.edit_entry)
        bar.addWidget(self.new_button)
        bar.addWidget(self.edit_button)
        layout.addLayout(bar)
        hint = QLabel("Сохранение марки не меняет схему. Для обновления оборудования выберите экземпляры, проверьте различия и отметьте нужные поля.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        options = QHBoxLayout()
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("Текущий лист", False)
        self.scope_combo.addItem("Весь проект", True)
        self.preserve_manual = QCheckBox("Сохранять ручные переопределения")
        self.preserve_manual.setChecked(True)
        self.inherit_confirmation = QCheckBox("Наследовать источник и подтверждение марки")
        self.inherit_confirmation.setChecked(True)
        options.addWidget(self.scope_combo)
        options.addWidget(self.preserve_manual)
        options.addWidget(self.inherit_confirmation)
        layout.addLayout(options)
        split = QSplitter()
        self.equipment_tree = QTreeWidget()
        self.equipment_tree.setHeaderLabels(["Обновляемые экземпляры", "Тип"])
        self.equipment_tree.setObjectName("catalogTargetEquipment")
        self.diff = QTreeWidget()
        self.diff.setHeaderLabels(["Применить / поле", "Было", "Из марки"])
        self.diff.header().setStretchLastSection(False)
        self.diff.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.diff.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.diff.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.diff.setObjectName("catalogParameterDiff")
        split.addWidget(self.equipment_tree)
        split.addWidget(self.diff)
        split.setSizes([370, 680])
        layout.addWidget(split, 1)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("Показать различия выбранных")
        self.preview_button.clicked.connect(self.preview_changes)
        self.apply_button = QPushButton("Применить отмеченные поля")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        self.entry_combo.currentIndexChanged.connect(self._invalidate)
        self.equipment_tree.itemChanged.connect(self._invalidate)
        self.preserve_manual.toggled.connect(self._policy_changed)
        self.inherit_confirmation.toggled.connect(self._invalidate)
        self.reload_entries()
        self._load_equipment()

    def _entry(self):
        key = self.entry_combo.currentData()
        return self.controller._project.user_catalog.entries.get(key)

    def reload_entries(self, selected=None):
        selected = selected or self.entry_combo.currentData()
        self.entry_combo.blockSignals(True)
        self.entry_combo.clear()
        for entry in sorted(self.controller._project.user_catalog.entries.values(), key=lambda row: row.display_name.casefold()):
            self.entry_combo.addItem(entry.display_name, entry.id)
        index = self.entry_combo.findData(selected)
        if index >= 0:
            self.entry_combo.setCurrentIndex(index)
        self.entry_combo.blockSignals(False)
        self._invalidate()

    def _load_equipment(self):
        self.equipment_tree.blockSignals(True)
        self.equipment_tree.clear()
        for equipment in sorted(self.controller.model.equipment.values(), key=lambda row: (row.name.casefold(), row.id.value)):
            definition = self.controller.model.equipment_type(equipment.type_id, equipment.type_version)
            if not catalog_field_specs(self.controller.model, equipment.type_id, equipment.type_version):
                continue
            item = QTreeWidgetItem([equipment.name, parameter_type_title(parameter_family(equipment.type_id), definition.display_name)])
            item.setData(0, Qt.ItemDataRole.UserRole, equipment.id)
            item.setToolTip(0, equipment.name + "\n" + equipment.id.value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if equipment.id in self._initial_selection else Qt.CheckState.Unchecked)
            self.equipment_tree.addTopLevelItem(item)
        self.equipment_tree.blockSignals(False)
        self._scope_changed()

    def _scope_changed(self):
        visible = None if self.scope_combo.currentData() else page_equipment_ids(self.controller)
        for index in range(self.equipment_tree.topLevelItemCount()):
            item = self.equipment_tree.topLevelItem(index)
            item.setHidden(visible is not None and item.data(0, Qt.ItemDataRole.UserRole) not in visible)
        self._invalidate()

    def selected_equipment_ids(self):
        return tuple(item.data(0, Qt.ItemDataRole.UserRole)
            for index in range(self.equipment_tree.topLevelItemCount())
            if not (item := self.equipment_tree.topLevelItem(index)).isHidden()
            and item.checkState(0) == Qt.CheckState.Checked)

    def _invalidate(self, *args):
        self.preview = None
        self.diff.clear()
        self.apply_button.setEnabled(False)
        self.edit_button.setEnabled(self._entry() is not None)

    def _policy_changed(self):
        # Rebuild the preselection; never apply a hidden change behind KEEP.
        self._invalidate()

    def save_entry(self, entry):
        result = self.controller.save_parameter_catalog_entry(entry)
        self.reload_entries(entry.id)
        self.commandApplied.emit(result)
        self.error_label.setText("Марка сохранена отдельно. Параметры оборудования не изменены.")

    def new_entry(self):
        data = self.type_combo.currentData()
        if not data:
            return
        type_id, version = data
        selected = self.selected_equipment_ids()
        owner = next((eid for eid in selected if self.controller.model.equipment[eid].type_id == type_id), None)
        dialog = CatalogEntryEditorDialog(self.controller, type_id, version, equipment_id=owner, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.save_entry(dialog.proposal)
            except (ValueError, RuntimeError) as exc:
                self.error_label.setText(str(exc))

    def edit_entry(self):
        entry = self._entry()
        if entry is None:
            return
        dialog = CatalogEntryEditorDialog(self.controller, entry.equipment_type_id,
            entry.equipment_type_version, entry=entry, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.save_entry(dialog.proposal)
            except (ValueError, RuntimeError) as exc:
                self.error_label.setText(str(exc))

    def preview_changes(self):
        self._invalidate()
        entry, ids = self._entry(), self.selected_equipment_ids()
        if entry is None or not ids:
            self.error_label.setText("Выберите марку и хотя бы один видимый экземпляр.")
            return
        try:
            self.preview = self.controller.preview_catalog_update(ids, entry)
        except (ValueError, RuntimeError) as exc:
            self.error_label.setText(str(exc))
            return
        for issue in self.preview.incompatible:
            equipment = self.controller.model.equipment.get(issue.equipment_id)
            self.diff.addTopLevelItem(QTreeWidgetItem([equipment.name if equipment else "Проект", "", issue.message]))
        snapshots = {snapshot.equipment_id: snapshot
            for snapshot in self.controller.equipment_parameter_snapshots(ids)}
        for change in self.preview.changes:
            equipment = self.controller.model.equipment[change.equipment_id]
            spec = next((item for item in snapshots[change.equipment_id].specs if item.key == change.key), None)
            label = spec.label if spec else change.key
            if spec and sum(other.label == spec.label for other in snapshots[change.equipment_id].specs) > 1:
                label = spec.group + " / " + label
            before = format_parameter_value(spec, change.before.value) if spec else str(change.before.value)
            after = format_parameter_value(spec, change.after.value) if spec else str(change.after.value)
            if spec and spec.unit:
                label += f", {unit_label(spec.unit)}"
            item = QTreeWidgetItem([equipment.name + " / " + label, before, after])
            item.setData(0, Qt.ItemDataRole.UserRole, change.ref)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            manual = is_manual_override(change.before)
            keep = self.preserve_manual.isChecked() and manual
            item.setCheckState(0, Qt.CheckState.Unchecked if keep else Qt.CheckState.Checked)
            if keep:
                item.setToolTip(0, "Ручное значение будет сохранено. Для замены отключите сохранение ручных переопределений и проверьте различия снова.")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                confirmed = self.inherit_confirmation.isChecked() and change.after.confirmation == DataConfirmation.CONFIRMED
                item.setToolTip(0, f"Источник марки: {change.after.source or entry.source}\n"
                    + ("Подтверждение наследуется" if confirmed else "Будет не подтверждено"))
            self.diff.addTopLevelItem(item)
        self.apply_button.setEnabled(bool(self.preview.changes) and not self.preview.incompatible)
        self.error_label.setText("Отметьте только нужные поля. Длина и место ТТ из марки не переносятся.")

    def apply_selected(self):
        if self.preview is None or not self.apply_button.isEnabled():
            return
        selected = tuple(item.data(0, Qt.ItemDataRole.UserRole)
            for index in range(self.diff.topLevelItemCount())
            if (item := self.diff.topLevelItem(index)).checkState(0) == Qt.CheckState.Checked
            and item.flags() & Qt.ItemFlag.ItemIsEnabled
            and item.data(0, Qt.ItemDataRole.UserRole) is not None)
        if not selected:
            self.error_label.setText("Нет отмеченных изменений.")
            return
        try:
            result = self.controller.apply_catalog_update(self.preview, selected,
                preserve_manual=self.preserve_manual.isChecked(),
                inherit_confirmation=self.inherit_confirmation.isChecked())
        except (ValueError, RuntimeError) as exc:
            self.error_label.setText(str(exc))
            self._invalidate()
            return
        self._invalidate()
        self.commandApplied.emit(result)
        self.error_label.setText("Отмеченные параметры применены одной командой. Расчёт запускается отдельно.")
