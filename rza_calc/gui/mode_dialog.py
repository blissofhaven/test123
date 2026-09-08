"""Named operating modes share the VM draft with both schematic views."""
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ..domain.electrical import DataConfirmation, EquipmentAvailability, SwitchPosition
from ..domain.operating_parameters import ModeValue, SOURCE_KEYS
from ..editor.parameter_editing import ParameterValue
from ..editor.parameter_schema import FieldSpec, format_parameter_value, unit_label
from .equipment_parameters import ParameterFieldEditor


class _ChoiceDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        choices = index.data(Qt.ItemDataRole.UserRole)
        if choices is None:
            return super().createEditor(parent, option, index)
        editor = QComboBox(parent)
        for label, value in choices:
            editor.addItem(label, value)
        return editor

    def setEditorData(self, editor, index):
        if isinstance(editor, QComboBox):
            editor.setCurrentIndex(max(0, editor.findText(index.data() or '')))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)
        else:
            super().setModelData(editor, model, index)


class _ModeValueDialog(QDialog):
    def __init__(self, spec, value, parent=None):
        super().__init__(parent)
        self.setWindowTitle(spec.label)
        self.resize(620, 220)
        layout = QVBoxLayout(self)
        self.editor = ParameterFieldEditor(spec, ParameterValue(value.value, value.source, value.confirmation), self)
        layout.addWidget(self.editor)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.inherit = QCheckBox('Удалить режимное переопределение — взять значение оборудования')
        layout.addWidget(self.inherit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.value = None

    def accept(self):
        if not self.inherit.isChecked():
            try:
                value = self.editor.value()
                self.value = ModeValue(value.value, value.source, value.confirmation)
            except (ValueError, TypeError) as exc:
                self.error.setText(str(exc))
                return
        super().accept()


class OperatingModeDialog(QDialog):
    draftChanged = Signal()
    modesApplied = Signal()
    highlightsRequested = Signal(object)

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self.vm, self.controller = vm, vm.mode_controller
        self.setWindowTitle('Режимы электрической сети')
        self.resize(1230, 850)
        self._loading = False
        layout = QVBoxLayout(self)
        hint = QLabel('Положения и доступность относятся только к выбранному режиму. Изменения сохраняются по кнопке «Применить режим». Расчёт запускается отдельно.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        split = QSplitter()
        left = QWidget()
        column = QVBoxLayout(left)
        self.modes = QListWidget()
        self.modes.currentRowChanged.connect(self._selected)
        column.addWidget(self.modes)
        for name, callback in (('Редактировать', self.edit_mode), ('Копировать', self.copy_mode), ('Новый режим', self.new_mode)):
            button = QPushButton(name)
            button.clicked.connect(callback)
            column.addWidget(button)
        split.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName('modeName')
        self.description_edit = QLineEdit()
        self.system_combo = QComboBox()
        self.system_combo.addItem('Максимальный эквивалент источника', 'max')
        self.system_combo.addItem('Минимальный эквивалент источника', 'min')
        self.parallel_combo = QComboBox()
        for text, value in (('Не указано — требуется решение', None), ('Параллельная работа разрешена', True), ('Параллельная работа запрещена', False)):
            self.parallel_combo.addItem(text, value)
        for title, widget in (('Имя', self.name_edit), ('Описание', self.description_edit), ('Источники', self.system_combo), ('Совместная работа источников', self.parallel_combo)):
            form.addRow(title, widget)
        self.name_edit.editingFinished.connect(self._metadata_changed)
        self.description_edit.editingFinished.connect(self._metadata_changed)
        self.system_combo.currentIndexChanged.connect(self._metadata_changed)
        self.parallel_combo.currentIndexChanged.connect(self._metadata_changed)
        right_layout.addLayout(form)
        self.tabs = QTabWidget()
        self.states = self._table(('Оборудование / сторона', 'Положение', 'Доступность'))
        self.states.setColumnWidth(1, 220)
        self.states.setItemDelegate(_ChoiceDelegate(self.states))
        self.states.itemChanged.connect(self._state_changed)
        self.parameters = self._table(('Оборудование / параметр', 'Значение', 'Источник', 'Проверено'))
        self.parameters.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.parameters.cellDoubleClicked.connect(self._edit_parameter)
        self.tabs.addTab(self.states, 'Аппараты и доступность')
        self.tabs.addTab(self.parameters, 'Режимные исходные данные')
        self.rules = QPlainTextEdit()
        self.rules.setReadOnly(True)
        self.tabs.addTab(self.rules, 'Применяемые правила')
        right_layout.addWidget(self.tabs, 1)
        template_row = QHBoxLayout()
        self.template = QComboBox()
        for text, key in (('Нормальный', 'normal'), ('Резервное питание', 'reserve'), ('Ремонт', 'repair'), ('Потеря генератора', 'generator_loss')):
            self.template.addItem(text, key)
        template_button = QPushButton('Предложить шаблон')
        template_button.clicked.connect(self._template)
        template_row.addWidget(self.template)
        template_row.addWidget(template_button)
        self.compare_to = QComboBox()
        template_row.addWidget(self.compare_to)
        compare = QPushButton('Сравнить')
        compare.clicked.connect(self.compare_modes)
        template_row.addWidget(compare)
        right_layout.addLayout(template_row)
        split.addWidget(right)
        split.setSizes([250, 950])
        layout.addWidget(split, 1)
        self.diff = self._table(('Раздел / оборудование', 'Поле', 'Было', 'Станет'))
        self.diff.setMaximumHeight(190)
        self.diff.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.diff)
        self.message = QPlainTextEdit()
        self.message.setReadOnly(True)
        self.message.setMaximumHeight(100)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        for text, callback in (('Проверить черновик', self.preview_changes), ('Применить режим', self.apply_changes), ('Сбросить черновик', self.reset_changes), ('Закрыть', self.reject)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.reload()

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 340)
        table.setAlternatingRowColors(True)
        return table

    def reload(self):
        self._loading = True
        try:
            self.modes.clear()
            self.compare_to.clear()
            for row in self.vm.operating_modes():
                item = QListWidgetItem(row.name)
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.modes.addItem(item)
                self.compare_to.addItem(row.name, row.state_id)
                if row.calculation_mode_id == self.vm.mode_id:
                    self.modes.setCurrentItem(item)
        finally:
            self._loading = False
        self._load_fields()

    def _snapshot(self):
        item = self.modes.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else self.vm.selected_operating_mode()

    def _selected(self, _row):
        if self._loading:
            return
        if self.vm.mode_draft is not None:
            self.message.setPlainText('Сначала примените или сбросьте текущий черновик.')
            self.reload()
            return
        row = self._snapshot()
        if row is not None:
            self.vm.choose_operating_mode(row.state_id)
            self.draftChanged.emit()
            self._load_fields()

    def edit_mode(self):
        self.vm.begin_mode_draft()
        self.draftChanged.emit()
        self._load_fields()

    def copy_mode(self):
        if self.vm.mode_draft is not None:
            self.message.setPlainText('Примените или сбросьте текущий черновик перед копированием.')
            return
        self.vm.begin_mode_draft(clone=True)
        self.draftChanged.emit()
        self._load_fields()

    def new_mode(self):
        if self.vm.mode_draft is not None:
            self.message.setPlainText('Примените или сбросьте текущий черновик перед созданием режима.')
            return
        self.vm.begin_mode_draft(new=True)
        self.draftChanged.emit()
        self._load_fields()

    def _metadata_changed(self, *_):
        if self._loading or self.vm.mode_draft is None:
            return
        draft = self.vm.mode_draft
        parameters = replace(draft.parameters, parallel_operation=self.parallel_combo.currentData())
        self.vm.update_mode_draft(replace(draft, name=self.name_edit.text(), description=self.description_edit.text(),
                                          system=self.system_combo.currentData(), parameters=parameters))
        self.message.setPlainText('Неприменённый черновик. Нажмите «Проверить черновик».')

    def _load_fields(self):
        snapshot = self._snapshot()
        row = self.vm.mode_draft or snapshot
        if row is None:
            return
        self._loading = True
        try:
            self.name_edit.setText(row.name)
            self.description_edit.setText(row.description)
            self.system_combo.setCurrentIndex(self.system_combo.findData(row.system))
            self.parallel_combo.setCurrentIndex(self.parallel_combo.findData(row.parameters.parallel_operation))
            editable = self.vm.mode_draft is not None
            for widget in (self.name_edit, self.description_edit, self.system_combo, self.parallel_combo):
                widget.setEnabled(editable)
            self.states.setEditTriggers((QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked)
                                        if editable else QAbstractItemView.EditTrigger.NoEditTriggers)
            self._state_rows = []
            self.states.setRowCount(0)
            targets = getattr(snapshot, 'targets', ()) or self.controller.operating_mode_targets()
            for target in targets:
                if target.section not in {'positions', 'availability'}:
                    continue
                index = self.states.rowCount()
                self.states.insertRow(index)
                self._state_rows.append(target)
                prefix = 'Контакт присоединения: ' if target.legacy_key else ('Доступность: ' if target.section == 'availability' else 'Аппарат: ')
                title = QTableWidgetItem(prefix + target.label)
                title.setToolTip(prefix + target.label)
                title.setFlags(title.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.states.setItem(index, 0, title)
                for col, kind in ((1, 'positions'), (2, 'availability')):
                    if (kind == 'positions') != (target.section == 'positions'):
                        item = QTableWidgetItem('—')
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    else:
                        choices = (('По исходным данным', None), ('Включён', SwitchPosition.CLOSED), ('Отключён', SwitchPosition.OPEN)) if kind == 'positions' else (
                            ('По исходным данным', None), ('В работе', EquipmentAvailability.IN_SERVICE), ('Выведен из работы', EquipmentAvailability.OUT_OF_SERVICE))
                        key = getattr(target, 'legacy_key', None) or target.equipment_id
                        if target.legacy_key:
                            raw = getattr(row, 'extra_positions', {})
                            value = (SwitchPosition.CLOSED if raw[key] else SwitchPosition.OPEN) if key in raw else None
                            if not editable:
                                value = snapshot.effective_positions.get(target.target_id)
                        else:
                            value = getattr(row, target.section).get(key)
                        item = QTableWidgetItem(next((label for label, val in choices if val == value), 'По исходным данным'))
                        item.setData(Qt.ItemDataRole.UserRole, choices)
                        effective = (snapshot.effective_positions.get(target.target_id) if kind == 'positions'
                                     else snapshot.effective_availability.get(target.equipment_id))
                        item.setToolTip('Итог сохранённого режима: ' + next((label for label, value in choices if value == effective), 'Не задано'))
                    self.states.setItem(index, col, item)
            self._load_parameters(row, targets)
            methodology = self.vm.project.methodology
            factor = methodology.k('short_circuit.temp_factor_min')
            provenance = methodology.provenance('short_circuit.temp_factor_min')
            self.rules.setPlainText(
                f'Температура проводника: для минимального эквивалента применяется множитель активного сопротивления {factor:g} из методики; '
                f'для максимального — без этого множителя. Источник: {provenance.source or "не указан"}; {provenance.source_status}. '
                'Паспортная температура оборудования не заменяет этот коэффициент.\n\n'
                'Нейтрали: используются общие параметры оборудования, схема нулевой последовательности и заданные Z0. '
                'Новые физические модели нейтралей относятся к следующему этапу.\n\n'
                'Предаварийное напряжение: используется выбранная ступень и правило методики. '
                'Отдельный расчёт установившегося режима и редактирование результатов потока мощности здесь не выполняются.\n\n'
                'Рабочий ток: вводится в первичных амперах для указанного вывода, с источником и подтверждением. '
                'Коэффициент нагрузки общий и по объекту перемножаются в расчётном ядре.')
        finally:
            self._loading = False

    def _state_changed(self, item):
        if self._loading or self.vm.mode_draft is None or item.column() not in (1, 2):
            return
        target = self._state_rows[item.row()]
        choices = item.data(Qt.ItemDataRole.UserRole)
        if not choices:
            return
        value = next(value for label, value in choices if label == item.text())
        key = getattr(target, 'legacy_key', None) or target.equipment_id
        section = 'extra_positions' if target.legacy_key else target.section
        values = dict(getattr(self.vm.mode_draft, section))
        if value is None:
            values.pop(key, None)
        else:
            values[key] = (value is SwitchPosition.CLOSED) if target.legacy_key else value
        self.vm.update_mode_draft(replace(self.vm.mode_draft, **{section: values}))
        self.draftChanged.emit()

    def _load_parameters(self, row, targets):
        self._parameter_rows = []
        factor_spec = FieldSpec('load_factor', 'Коэффициент нагрузки', 'number', minimum=0)
        self._parameter_rows.append(('load_factor', None, None, factor_spec, row.parameters.load_factor, 'Все нагрузки'))
        for target in targets:
            if target.section == 'source':
                for spec in getattr(target, 'specs', ()):
                    if spec.key in SOURCE_KEYS:
                        value = row.parameters.sources.get(target.equipment_id, {}).get(spec.key)
                        self._parameter_rows.append(('sources', target.equipment_id, spec.key, spec, value, target.label))
            elif target.section == 'working_current':
                pid = target.port_id
                spec = FieldSpec('working_current', 'Первичный рабочий ток', 'number', unit='A', display_units=('kA',), minimum=0)
                self._parameter_rows.append(('working_currents', pid, None, spec, row.parameters.working_currents.get(pid), target.label))
            elif target.section == 'load':
                self._parameter_rows.append(('load_factors', target.equipment_id, None, factor_spec, row.parameters.load_factors.get(target.equipment_id), target.label))
        self.parameters.setRowCount(len(self._parameter_rows))
        for index, (_, _, _, spec, value, label) in enumerate(self._parameter_rows):
            text = (format_parameter_value(spec, value.value) + (' ' + unit_label(spec.unit) if spec.unit else '')) if value is not None else 'По исходным данным'
            for col, text in enumerate((label + ' · ' + spec.label, text, value.source if value else '',
                                       'Да' if value and value.confirmation is DataConfirmation.CONFIRMED else 'Нет')):
                self.parameters.setItem(index, col, QTableWidgetItem(text))

    def _edit_parameter(self, index, _column):
        if self.vm.mode_draft is None:
            return
        section, key, field, spec, value, label = self._parameter_rows[index]
        editor = _ModeValueDialog(spec, value or ModeValue(None), self)
        editor.setWindowTitle(label + ' · ' + spec.label)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        self.set_parameter(section, key, field, editor.value)

    def set_parameter(self, section, key, field, value):
        parameters = self.vm.mode_draft.parameters
        if section == 'load_factor':
            parameters = replace(parameters, load_factor=value)
        else:
            values = dict(getattr(parameters, section))
            if section == 'sources':
                row = dict(values.get(key, {}))
                if value is None:
                    row.pop(field, None)
                else:
                    row[field] = value
                values[key] = row
            elif value is None:
                values.pop(key, None)
            else:
                values[key] = value
            parameters = replace(parameters, **{section: values})
        self.vm.update_mode_draft(replace(self.vm.mode_draft, parameters=parameters))
        self._load_fields()

    def _show_diff(self, changes):
        self.diff.setRowCount(len(changes))
        highlighted = set()
        rows = getattr(self._snapshot(), 'targets', ())
        targets = {row.target_id: row for row in rows}
        targets.update({row.legacy_key: row for row in rows if row.legacy_key})
        targets.update({str(row.port_id): row for row in rows if row.port_id})
        field_labels = {'name': 'Имя', 'description': 'Описание', 'system': 'Эквивалент источника',
            'positions': 'Положение', 'extra_positions': 'Положение', 'availability': 'Доступность',
            'extra_availability': 'Доступность', 'load_factor': 'Общий коэффициент нагрузки',
            'load_factors': 'Коэффициент нагрузки', 'working_currents': 'Первичный рабочий ток',
            'parallel_operation': 'Параллельная работа'}
        def display(value, spec=None):
            if isinstance(value, ModeValue):
                text = format_parameter_value(spec, value.value) if spec else str(value.value) if value.value is not None else 'Не задано'
                if spec and spec.unit:
                    text += ' ' + unit_label(spec.unit)
                return text + '; ' + (value.source or 'источник не указан') + '; ' + ('проверено' if value.confirmation is DataConfirmation.CONFIRMED else 'не проверено')
            if isinstance(value, (SwitchPosition, EquipmentAvailability)):
                return {SwitchPosition.CLOSED: 'Включён', SwitchPosition.OPEN: 'Отключён',
                    EquipmentAvailability.IN_SERVICE: 'В работе', EquipmentAvailability.OUT_OF_SERVICE: 'Выведен из работы'}[value]
            if isinstance(value, bool):
                return 'Да' if value else 'Нет'
            if value in ('max', 'min'):
                return 'Максимальный' if value == 'max' else 'Минимальный'
            return str(value) if value is not None else 'По исходным данным'
        for index, change in enumerate(changes):
            target = targets.get(change.target_id)
            if target is not None:
                highlighted.add(target.equipment_id)
            title = target.label if target is not None else 'Режим'
            spec = next((spec for spec in getattr(target, 'specs', ()) if spec.key == change.key), None)
            if change.key == 'working_currents':
                spec = FieldSpec('working_current', 'Первичный рабочий ток', 'number', unit='A')
            field = spec.label if spec else field_labels.get(change.key, change.key)
            def cell(value):
                if change.section == 'extra_positions' and isinstance(value, bool):
                    return 'Включён' if value else 'Отключён'
                return display(value, spec)
            for col, value in enumerate((title, field, cell(change.before), cell(change.after))):
                item = QTableWidgetItem(value)
                if col == 3:
                    item.setBackground(QColor('#FFF2CC'))
                self.diff.setItem(index, col, item)
        self.highlightsRequested.emit(tuple(highlighted))

    def preview_changes(self):
        self._metadata_changed()
        try:
            preview = self.vm.preview_mode_draft()
            if preview is None:
                self.message.setPlainText('Нет черновика. Выберите «Редактировать» или «Копировать».')
                return None
            self._show_diff(preview.changes)
            self.message.setPlainText('\n'.join(str(getattr(issue, 'message', issue)) for issue in preview.diagnostics)
                                      or 'Черновик проверен. Применение изменит только этот режим.')
            self.draftChanged.emit()
            return preview
        except (ValueError, RuntimeError) as exc:
            self.message.setPlainText(str(exc))
            return None

    def apply_changes(self):
        if self.preview_changes() is None:
            return False
        try:
            self.vm.apply_mode_draft()
        except (ValueError, RuntimeError) as exc:
            self.message.setPlainText(str(exc))
            return False
        self.reload()
        self.modesApplied.emit()
        self.message.setPlainText('Режим применён. Запустите расчёт для новых результатов.')
        return True

    def reset_changes(self):
        self.vm.reset_mode_draft()
        self.reload()
        self.draftChanged.emit()

    def compare_modes(self):
        try:
            changes = self.controller.compare_operating_modes(self._snapshot().state_id, self.compare_to.currentData())
            self._show_diff(changes)
            self.message.setPlainText('Сравнение сохранённых режимов. Данные не изменены.')
        except (ValueError, RuntimeError) as exc:
            self.message.setPlainText(str(exc))

    def _template(self):
        if self.vm.mode_draft is not None:
            self.message.setPlainText('Примените или сбросьте черновик перед предложением шаблона.')
            return
        try:
            selected_row = self.states.currentRow()
            equipment_id = self._state_rows[selected_row].equipment_id if selected_row >= 0 else None
            draft = self.controller.operating_mode_template(self.template.currentData(),
                base_state_id=self._snapshot().state_id, equipment_id=equipment_id)
            self.vm.update_mode_draft(draft)
            self._load_fields()
            self.preview_changes()
        except (ValueError, RuntimeError) as exc:
            self.message.setPlainText(str(exc))
