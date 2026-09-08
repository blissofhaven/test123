"""Shared typed field widgets and a local, explicitly applied equipment draft."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


def _issue_message(issue):
    message = str(getattr(issue, "message", issue))
    key = str(getattr(issue, "key", ""))
    if ".max." in key or key.endswith("_max"):
        message = "Максимальный режим: " + message
    elif ".min." in key or key.endswith("_min"):
        message = "Минимальный режим: " + message
    return message


def format_parameter_readiness(issues):
    """Keep the complete per-fault list, including separate source regimes."""
    rows = tuple(issues or ())
    blocks = []
    general = tuple(dict.fromkeys(_issue_message(issue) for issue in rows if not getattr(issue, "fault_types", ())))
    if general:
        blocks.append("Общие замечания\n" + "\n".join("• " + text for text in general))
    for key, label in (("3ph", "КЗ (3) — трёхфазное"), ("2ph", "КЗ (2) — двухфазное"),
                       ("1ph_g", "КЗ (1) — однофазное на землю"), ("2ph_g", "КЗ (2,1) — двухфазное на землю")):
        messages = tuple(dict.fromkeys(_issue_message(issue) for issue in rows if key in getattr(issue, "fault_types", ())))
        blocks.append(label + "\n" + ("\n".join("• " + text for text in messages)
            if messages else "По указанным полям замечаний нет; готовность сети проверяется при расчёте."))
    return "\n\n".join(blocks)


class _ReadinessBox(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumHeight(140)
        self.setMinimumHeight(100)
        self.setObjectName("parameterReadiness")

    def setText(self, text):
        self.setPlainText(text)

from ..domain.electrical import DataConfirmation
from ..editor.parameter_editing import ParameterPatch, ParameterValue
from ..editor.parameter_schema import (
    format_parameter_value, parse_parameter_value, unit_label, validate_field_value,
)


class ParameterFieldEditor(QWidget):
    """A reusable field editor; typing never touches a project or calculation."""

    edited = Signal()

    def __init__(self, spec, value: ParameterValue, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._loading = False
        self._original = value
        self._display_unit = "m" if spec.unit == "mm" and "m" in spec.display_units else spec.unit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 6)
        row = QHBoxLayout()
        if spec.value_kind in {"boolean", "enum"}:
            self.value_edit = QComboBox()
            if spec.nullable:
                self.value_edit.addItem("Не задано", None)
            choices = (("Да", True), ("Нет", False)) if spec.value_kind == "boolean" else spec.choices
            for label, choice in choices:
                self.value_edit.addItem(label, choice)
            self.value_edit.currentIndexChanged.connect(self._input_changed)
        else:
            self.value_edit = QLineEdit()
            self.value_edit.setPlaceholderText("Не задано" if spec.nullable else "Обязательное поле")
            self.value_edit.textChanged.connect(self._input_changed)
        self.value_edit.setObjectName("parameterValue." + spec.key)
        # Visibility is set before insertion into the row. Keep these widgets
        # parented so setVisible cannot briefly activate a top-level window.
        self.unit_label = QLabel(unit_label(spec.unit), self)
        self.unit_combo = QComboBox(self)
        units = tuple(dict.fromkeys((spec.unit, *spec.display_units)))
        for unit in units:
            self.unit_combo.addItem(unit_label(unit), unit)
        self.unit_combo.setCurrentIndex(self.unit_combo.findData(self._display_unit))
        self.unit_combo.setObjectName("parameterUnit." + spec.key)
        self.unit_combo.setVisible(len(units) > 1)
        self.unit_label.setVisible(len(units) <= 1)
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        row.addWidget(self.value_edit, 1)
        row.addWidget(self.unit_label)
        row.addWidget(self.unit_combo)
        layout.addLayout(row)
        provenance = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("parameterSource." + spec.key)
        self.source_edit.setPlaceholderText("Источник: паспорт, расчёт, измерение…")
        self.source_edit.textChanged.connect(self._input_changed)
        self.confirmation_check = QCheckBox("Проверено")
        self.confirmation_check.setObjectName("parameterConfirmed." + spec.key)
        self.confirmation_check.toggled.connect(self._confirmation_changed)
        provenance.addWidget(self.source_edit, 1)
        provenance.addWidget(self.confirmation_check)
        layout.addLayout(provenance)
        self.origin_label = QLabel()
        self.origin_label.setWordWrap(True)
        self.origin_label.setStyleSheet("color: #526174; font-size: 11px;")
        layout.addWidget(self.origin_label)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B42318;")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.setToolTip("\n".join(filter(None, (spec.help_text,
            "Сторона: " + spec.side if spec.side else "", spec.calculation_use))))
        self.set_value(value)
        self.setEnabled(spec.editable)

    def set_value(self, value: ParameterValue) -> None:
        self._loading = True
        try:
            self._original = value
            if isinstance(self.value_edit, QComboBox):
                index = self.value_edit.findData(value.value)
                if index < 0:
                    # Show an imported value honestly; opening a card cannot
                    # silently choose the first supported enum instead.
                    self.value_edit.addItem(str(value.value) if value.value is not None else "Не задано", value.value)
                    index = self.value_edit.count() - 1
                self.value_edit.setCurrentIndex(index)
            else:
                self.value_edit.setText(format_parameter_value(self.spec, value.value, self._display_unit))
            self.source_edit.setText(value.source)
            self.confirmation_check.setChecked(value.confirmation is DataConfirmation.CONFIRMED)
            origins = {"catalog": "Из справочника", "manual": "Ручное значение",
                       "instance": "Экземпляр", "inherited": "Унаследовано",
                       "segment": "Конструктивный участок", "default": "Значение типа",
                       "line": "Линия", "legacy": "Сохранённые исходные данные",
                       "type": "Тип оборудования", "section": "Участок линии",
                       "logical_line": "Логическая линия"}
            self.origin_label.setText(origins.get(value.origin, value.origin or "Экземпляр"))
            self.set_error("")
        finally:
            self._loading = False
        self._initial_state = self._state()

    def _state(self):
        content = self.value_edit.currentData() if isinstance(self.value_edit, QComboBox) else self.value_edit.text()
        return content, self.source_edit.text(), self.confirmation_check.isChecked()

    def is_changed(self) -> bool:
        return self._state() != self._initial_state

    def draft_state(self):
        """Preserve even invalid, unapplied input when changing selection."""
        return (*self._state(), self._display_unit)

    def restore_draft_state(self, state):
        content, source, confirmed, unit = state
        self.unit_combo.setCurrentIndex(self.unit_combo.findData(unit))
        self._loading = True
        try:
            if isinstance(self.value_edit, QComboBox):
                self.value_edit.setCurrentIndex(self.value_edit.findData(content))
            else:
                self.value_edit.setText(content)
            self.source_edit.setText(source)
            self.confirmation_check.setChecked(confirmed)
        finally:
            self._loading = False

    def _input_changed(self, *_args) -> None:
        if self._loading:
            return
        self._loading = True
        self.confirmation_check.setChecked(False)
        self._loading = False
        self.set_error("")
        self.edited.emit()

    def _confirmation_changed(self, *_args) -> None:
        if not self._loading:
            self.set_error("")
            self.edited.emit()

    def _unit_changed(self, _index):
        if self._loading:
            return
        unit = self.unit_combo.currentData()
        if unit == self._display_unit or not isinstance(self.value_edit, QLineEdit):
            return
        try:
            value = parse_parameter_value(self.spec, self.value_edit.text(), self._display_unit)
            display = format_parameter_value(self.spec, value, unit)
        except ValueError as exc:
            self._loading = True
            self.unit_combo.setCurrentIndex(self.unit_combo.findData(self._display_unit))
            self._loading = False
            self.set_error(str(exc))
            return
        self._loading = True
        self._display_unit = unit
        self.value_edit.setText(display)
        self._initial_state = (format_parameter_value(self.spec, self._original.value, unit),
            self._original.source, self._original.confirmation is DataConfirmation.CONFIRMED)
        self._loading = False
        self.set_error("")

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def value(self) -> ParameterValue:
        if not self.is_changed():
            return self._original
        if isinstance(self.value_edit, QComboBox):
            parsed = self.value_edit.currentData()
        else:
            parsed = parse_parameter_value(self.spec, self.value_edit.text(), self._display_unit)
        error = validate_field_value(self.spec, parsed)
        if error:
            raise ValueError(error)
        confirmed = self.confirmation_check.isChecked()
        source = self.source_edit.text().strip()
        if confirmed and (parsed is None or not source):
            raise ValueError("Для подтверждения укажите значение и источник данных.")
        return ParameterValue(parsed, source,
            DataConfirmation.CONFIRMED if confirmed else DataConfirmation.UNCONFIRMED,
            "manual")


class EquipmentParameterCardDialog(QDialog):
    """One equipment card, with separate drafts for its construction segments."""

    commandApplied = Signal(object)

    def __init__(self, controller, equipment_id, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.equipment_id = equipment_id
        self.snapshot = controller.equipment_parameter_snapshot(equipment_id)
        self._scope_editors = {}
        self._scope_snapshots = {}
        self._scope_indexes = {}
        self.editors = {}
        self.setWindowTitle("Карточка оборудования — " + self.snapshot.name)
        self.setObjectName("equipmentParameterCard")
        self.resize(900, 760)
        layout = QVBoxLayout(self)
        self.title_label = QLabel(self.snapshot.name)
        self.title_label.setStyleSheet("font-size: 19px; font-weight: 600;")
        layout.addWidget(self.title_label)
        self.notice_label = QLabel("Изменения применяются одной операцией. Незаполненные данные можно сохранить как черновик. Расчёт запускается отдельно.")
        self.notice_label.setWordWrap(True)
        layout.addWidget(self.notice_label)
        self.segment_combo = QComboBox()
        self.segment_combo.setObjectName("parameterSegment")
        self.segment_combo.addItem("Аппарат / ветвь целиком", None)
        for index, segment_id in enumerate(getattr(self.snapshot, "segments", ()), 1):
            self.segment_combo.addItem(f"Конструктивный участок {index}", segment_id)
        self.segment_combo.setVisible(self.segment_combo.count() > 1)
        layout.addWidget(self.segment_combo)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.readiness_label = _ReadinessBox()
        layout.addWidget(self.readiness_label)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B42318;")
        layout.addWidget(self.error_label)
        self.diff_tree = QTreeWidget()
        self.diff_tree.setObjectName("parameterChanges")
        self.diff_tree.setHeaderLabels(["Поле", "Было", "Станет"])
        self.diff_tree.setMaximumHeight(160)
        self.diff_tree.hide()
        layout.addWidget(self.diff_tree)
        controls = QHBoxLayout()
        self.preview_button = QPushButton("Проверить изменения")
        self.preview_button.clicked.connect(self.preview_changes)
        controls.addWidget(self.preview_button)
        controls.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("Применить")
        self.apply_button.clicked.connect(self.apply_changes)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.rejected.connect(self.reject)
        controls.addWidget(self.buttons)
        layout.addLayout(controls)
        mode = getattr(getattr(controller, "mode", "edit"), "value", getattr(controller, "mode", "edit"))
        self._editable = str(mode) == "edit"
        self.apply_button.setEnabled(self._editable)
        self.preview_button.setEnabled(self._editable)
        if not self._editable:
            self.notice_label.setText("Режим анализа: карточка доступна для просмотра. Для изменения данных перейдите в редактор.")
        self._load_scope(None, self.snapshot)
        self.segment_combo.currentIndexChanged.connect(self._segment_changed)

    @staticmethod
    def _issue_text(issues) -> str:
        return "\n".join(dict.fromkeys(_issue_message(issue) for issue in (issues or ())))

    def _load_scope(self, segment_id, snapshot=None):
        if segment_id not in self._scope_indexes:
            snapshot = snapshot or self.controller.equipment_parameter_snapshot(self.equipment_id, segment_id=segment_id)
            if snapshot.stamp != self.snapshot.stamp:
                raise ValueError("Данные проекта изменились. Закройте и откройте карточку, чтобы загрузить актуальные значения.")
            container = QWidget()
            form = QFormLayout(container)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            editors = {}
            group = None
            for spec in snapshot.specs:
                if spec.group != group:
                    group = spec.group
                    heading = QLabel(group)
                    heading.setStyleSheet("font-weight: 600; margin-top: 9px;")
                    form.addRow(heading)
                editor = ParameterFieldEditor(spec, snapshot.fields.get(spec.key, ParameterValue(None)))
                editor.setEnabled(self._editable and spec.editable)
                editor.edited.connect(self._draft_changed)
                label = QLabel(spec.label + (" · " + spec.side if spec.side else ""))
                label.setWordWrap(True)
                label.setMinimumWidth(185)
                label.setMaximumWidth(250)
                form.addRow(label, editor)
                editors[spec.key] = editor
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(container)
            self._scope_indexes[segment_id] = self.stack.addWidget(area)
            self._scope_editors[segment_id] = editors
            self._scope_snapshots[segment_id] = snapshot
        self.stack.setCurrentIndex(self._scope_indexes[segment_id])
        self.editors = self._scope_editors[segment_id]
        self.readiness_label.setText(format_parameter_readiness(getattr(self._scope_snapshots[segment_id], "readiness", ())))

    def _segment_changed(self, _index):
        try:
            self._load_scope(self.segment_combo.currentData())
        except (ValueError, KeyError, TypeError) as exc:
            self.error_label.setText(str(exc))

    def _draft_changed(self):
        self.diff_tree.clear()
        self.diff_tree.hide()
        self.error_label.clear()
        self.readiness_label.setText("Данные изменены в карточке. «Проверить изменения» покажет готовность черновика.")

    def patches(self):
        patches = []
        for segment_id, editors in self._scope_editors.items():
            values, clear = {}, set()
            for key, editor in editors.items():
                if not editor.spec.editable or not editor.is_changed():
                    continue
                try:
                    value = editor.value()
                except ValueError as exc:
                    editor.set_error(str(exc))
                    raise ValueError(f"{editor.spec.label}: {exc}") from exc
                # Empty means explicitly unknown. Removing an override is a
                # different domain operation and must not revive parent data.
                values[key] = value
            if values or clear:
                patches.append(ParameterPatch(self.equipment_id, values=values,
                    clear=frozenset(clear), segment_id=segment_id))
        return tuple(patches)

    def preview_changes(self):
        if not self._editable:
            return None
        try:
            current = self.controller.equipment_parameter_snapshot(self.equipment_id)
            if current.stamp != self.snapshot.stamp:
                raise ValueError("Данные проекта изменились. Закройте и откройте карточку перед применением.")
            preview = self.controller.preview_parameter_patch(self.patches())
            self.error_label.setText(self._issue_text(preview.diagnostics))
            self.readiness_label.setText(format_parameter_readiness(preview.readiness))
            self.diff_tree.clear()
            for change in preview.changes:
                key = getattr(change, "key", "")
                segment_id = getattr(change, "segment_id", None)
                editor = self._scope_editors.get(segment_id, {}).get(key)
                before = getattr(change.before, "value", change.before)
                after = getattr(change.after, "value", change.after)
                label = editor.spec.label if editor is not None else key
                if editor is not None:
                    before = format_parameter_value(editor.spec, before)
                    after = format_parameter_value(editor.spec, after)
                self.diff_tree.addTopLevelItem(QTreeWidgetItem([label, str(before), str(after)]))
            self.diff_tree.setVisible(bool(preview.changes))
            return preview
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self.error_label.setText(str(exc))
            return None

    def apply_changes(self):
        preview = self.preview_changes()
        if preview is None or not preview.valid:
            return False
        if not preview.changes:
            self.error_label.setText("Изменений нет.")
            return False
        try:
            result = self.controller.apply_parameter_preview(preview)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self.error_label.setText(str(exc))
            return False
        self.commandApplied.emit(result)
        self._reload_applied()
        return True

    def _reload_applied(self):
        selected = self.segment_combo.currentData()
        self.snapshot = self.controller.equipment_parameter_snapshot(self.equipment_id)
        scopes = tuple(self._scope_indexes)
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self._scope_indexes.clear()
        self._scope_editors.clear()
        self._scope_snapshots.clear()
        for segment_id in scopes:
            self._load_scope(segment_id, self.snapshot if segment_id is None else None)
        self._load_scope(selected)
        self.diff_tree.clear()
        self.diff_tree.hide()
        self.error_label.setText("Изменения применены. Для новых результатов запустите расчёт отдельно.")


class ParameterQuickEditor(QWidget):
    """Four main fields use the same draft/preview/apply contract as the card."""

    commandApplied = Signal(object)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.snapshot = None
        self.editors = {}
        self._drafts = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(430)
        self.scroll.setMinimumHeight(140)
        layout.addWidget(self.scroll)
        row = QHBoxLayout()
        self.apply_button = QPushButton("Применить")
        self.apply_button.setObjectName("quickParametersApply")
        self.apply_button.clicked.connect(self.apply_changes)
        self.reset_button = QPushButton("Сбросить")
        self.reset_button.clicked.connect(self.reset_changes)
        row.addWidget(self.apply_button)
        row.addWidget(self.reset_button)
        layout.addLayout(row)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.hide()

    def _remember_draft(self):
        if self.snapshot is None:
            return
        changed = {key: editor.draft_state() for key, editor in self.editors.items() if editor.is_changed()}
        if changed:
            self._drafts[self.snapshot.equipment_id] = (self.snapshot, changed)
        else:
            self._drafts.pop(self.snapshot.equipment_id, None)

    def set_equipment(self, equipment_id):
        self._remember_draft()
        self.editors = {}
        if equipment_id is None:
            self.snapshot = None
            self.hide()
            return
        saved = self._drafts.get(equipment_id)
        self.snapshot = saved[0] if saved else self.controller.equipment_parameter_snapshot(equipment_id)
        mode = getattr(getattr(self.controller, "mode", "edit"), "value", getattr(self.controller, "mode", "edit"))
        editable = str(mode) == "edit"
        container = QWidget()
        form = QVBoxLayout(container)
        form.setContentsMargins(0, 0, 5, 0)
        available = {spec.key: spec for spec in self.snapshot.specs if spec.editable and spec.key not in {"note", "equipment.note"}}
        if "length_mm" in available:
            preferred = ("name", "length_mm", "conductor_mark", "cross_section_mm2", "parallel_count")
        elif "u_hv" in available:
            preferred = ("name", "s_nom", "u_hv", "u_lv", "uk")
        elif "xd2" in available:
            preferred = ("name", "s_nom", "u_nom", "xd2", "cos_phi")
        else:
            preferred = tuple(available)[:4]
        specs = [available[key] for key in preferred if key in available]
        for spec in specs:
            label = QLabel(spec.label + (" · " + spec.side if spec.side else ""))
            label.setWordWrap(True)
            form.addWidget(label)
            editor = ParameterFieldEditor(spec, self.snapshot.fields.get(spec.key, ParameterValue(None)))
            editor.setEnabled(editable)
            editor.edited.connect(self._changed)
            self.editors[spec.key] = editor
            form.addWidget(editor)
        previous = self.scroll.takeWidget()
        if previous is not None:
            previous.deleteLater()
        self.scroll.setWidget(container)
        if saved:
            for key, state in saved[1].items():
                if key in self.editors:
                    self.editors[key].restore_draft_state(state)
        self.apply_button.setEnabled(editable)
        self.reset_button.setEnabled(editable)
        self.message.setText("Есть неприменённые изменения." if saved else "Изменения сохраняются по кнопке «Применить».")
        self.setVisible(bool(specs))

    def _changed(self):
        self.message.setText("Черновик. «Применить» сохранит значения и сведения о проверке.")

    def reset_changes(self):
        if self.snapshot is None:
            return
        equipment_id = self.snapshot.equipment_id
        self._drafts.pop(equipment_id, None)
        self.snapshot = None
        self.set_equipment(equipment_id)

    def apply_changes(self):
        if self.snapshot is None or not self.apply_button.isEnabled():
            return False
        try:
            current = self.controller.equipment_parameter_snapshot(self.snapshot.equipment_id)
            if current.stamp != self.snapshot.stamp:
                raise ValueError("Проект изменился. Нажмите «Сбросить», чтобы загрузить актуальные данные.")
            values, clear = {}, set()
            for key, editor in self.editors.items():
                if not editor.is_changed():
                    continue
                try:
                    value = editor.value()
                except ValueError as exc:
                    editor.set_error(str(exc))
                    raise
                values[key] = value
            if not values and not clear:
                self.message.setText("Изменений нет.")
                return False
            patch = ParameterPatch(self.snapshot.equipment_id, values, frozenset(clear))
            preview = self.controller.preview_parameter_patch((patch,))
            if not preview.valid:
                self.message.setText(EquipmentParameterCardDialog._issue_text(preview.diagnostics))
                return False
            result = self.controller.apply_parameter_preview(preview)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self.message.setText(str(exc))
            return False
        equipment_id = self.snapshot.equipment_id
        self._drafts.pop(equipment_id, None)
        self.snapshot = None
        self.commandApplied.emit(result)
        self.set_equipment(equipment_id)
        self.message.setText("Применено. Расчёт запускается отдельно.")
        return True

    def transfer_draft_to(self, dialog):
        self._remember_draft()
        saved = self._drafts.get(dialog.equipment_id)
        if saved is None:
            return
        if saved[0].stamp != dialog.snapshot.stamp:
            dialog.error_label.setText("В правой панели есть черновик по прежним данным проекта. Он не перенесён; используйте актуальные значения карточки.")
            return
        for key, state in saved[1].items():
            if key in dialog.editors:
                dialog.editors[key].restore_draft_state(state)

    def card_applied(self, equipment_id):
        self._drafts.pop(equipment_id, None)
        if self.snapshot is not None and self.snapshot.equipment_id == equipment_id:
            self.snapshot = None
            self.set_equipment(equipment_id)


__all__ = ["ParameterFieldEditor", "EquipmentParameterCardDialog", "ParameterQuickEditor", "format_parameter_readiness"]
