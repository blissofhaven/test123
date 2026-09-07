# -*- coding: utf-8 -*-
"""Line entry produces a proposal; only the editor command changes the project."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import math

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout,
)

from ..domain.catalog import CatalogCategoryId, CatalogEntry, CatalogEntryId, CatalogOrigin
from ..domain.electrical import DataConfirmation, EquipmentTypeId, LineKind
from ..editor.controller import PhysicalLineInput


@dataclass(frozen=True, slots=True)
class LineParametersResult:
    accepted: bool
    name: str
    physical: PhysicalLineInput
    catalog_entry: CatalogEntry | None = None
    remember_catalog_entry: bool = False


def _optional_number(text: str, label: str, *, positive: bool = False,
                     nonnegative: bool = False) -> float | None:
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{label}: введите число.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label}: требуется конечное число.")
    if positive and value <= 0:
        raise ValueError(f"{label}: значение должно быть больше нуля.")
    if nonnegative and value < 0:
        raise ValueError(f"{label}: значение не может быть отрицательным.")
    return value


def _length_mm(text: str) -> int | None:
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        meters = Decimal(text)
        if not meters.is_finite() or meters <= 0:
            raise ValueError("Длина должна быть конечным числом больше нуля.")
        if meters.adjusted() > 15:
            raise ValueError("Длина слишком велика; проверьте единицы измерения.")
        with localcontext() as context:
            context.prec = max(28, len(meters.as_tuple().digits) + 3)
            millimeters = meters * 1000
        if millimeters != millimeters.to_integral_value():
            raise ValueError("Задайте длину в метрах с точностью не мельче 0,001 м.")
        return int(millimeters)
    except InvalidOperation as exc:
        raise ValueError("Длина: введите число метров.") from exc


class LineParametersDialog(QDialog):
    """Read-only catalog selection and optional manual template proposal.

    Catalog templates hold construction data, never the length of the new
    section. Choosing a template does not confirm engineering inputs.
    """

    def __init__(self, parent, draft, project):
        super().__init__(parent)
        self._project = project
        self._kind = LineKind(draft.line_kind)
        if self._kind not in (LineKind.CABLE, LineKind.OVERHEAD):
            raise ValueError("Диалог параметров предназначен для КЛ и ВЛ.")
        self._default_name = draft.name.strip() or ("КЛ" if self._kind is LineKind.CABLE else "ВЛ")
        self._type_id = EquipmentTypeId(f"builtin.line_section.{self._kind.value}")
        catalog = getattr(project, "user_catalog", None)
        self.entries = tuple(sorted(
            (entry for entry in catalog.entries.values()
             if entry.equipment_type_id == self._type_id
             and entry.equipment_type_version == 1
             and entry.origin is CatalogOrigin.USER),
            key=lambda entry: (entry.display_name.casefold(), entry.id.value),
        )) if catalog is not None else ()
        self.proposal: LineParametersResult | None = None
        self._form_mode = "manual"
        self._manual_values: dict[str, str] = {}
        self._manual_parallel = 1
        self.setWindowTitle("Кабельная линия" if self._kind is LineKind.CABLE else "Воздушная линия")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self._default_name)
        self.name_edit.setObjectName("lineName")
        form.addRow("Название линии", self.name_edit)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("lineParameterMode")
        self.mode_combo.addItem("Ввести вручную", "manual")
        self.mode_combo.addItem("Выбрать из справочника проекта", "catalog")
        self.mode_combo.addItem("Черновик — заполнить позже", "draft")
        form.addRow("Параметры", self.mode_combo)
        self.catalog_combo = QComboBox()
        self.catalog_combo.setObjectName("lineCatalogEntry")
        for entry in self.entries:
            self.catalog_combo.addItem(entry.display_name, entry.id.value)
        if not self.entries:
            self.catalog_combo.addItem("В проекте пока нет марок этого вида", None)
        form.addRow("Справочник проекта", self.catalog_combo)
        self.length_edit = QLineEdit()
        self.length_edit.setObjectName("lineLengthMeters")
        self.length_edit.setPlaceholderText("Не задана")
        self.length_edit.setMaxLength(64)
        self.length_confirm = QCheckBox("Длина проверена")
        length_row = QHBoxLayout()
        length_row.addWidget(self.length_edit)
        length_row.addWidget(self.length_confirm)
        form.addRow("Длина, м", length_row)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 2_147_483_647)
        self.parallel_spin.setValue(1)
        self.parallel_spin.setObjectName("lineParallelCount")
        form.addRow("Параллельных цепей", self.parallel_spin)
        layout.addLayout(form)

        self.brand_group = QGroupBox("Данные марки")
        brand_form = QFormLayout(self.brand_group)
        self.fields: dict[str, QLineEdit] = {}
        for key, label in (
            ("conductor_mark", "Марка"), ("material", "Материал"),
            ("cross_section_mm2", "Сечение, мм²"),
            ("r1_ohm_per_km", "R1, Ом/км"), ("x1_ohm_per_km", "X1, Ом/км"),
            ("source", "Источник данных"),
        ):
            edit = QLineEdit()
            edit.setObjectName("line_" + key)
            edit.setPlaceholderText("Не задано")
            edit.textChanged.connect(self._parameters_changed)
            self.fields[key] = edit
            brand_form.addRow(label, edit)
        self.fields["source"].setPlaceholderText("Паспорт, таблица проекта, замер…")
        self.impedance_confirm = QCheckBox("R1 и X1 проверены по указанному источнику")
        brand_form.addRow(self.impedance_confirm)
        self.remember_check = QCheckBox("Сохранить марку в справочник этого проекта")
        self.remember_check.setObjectName("rememberLineBrand")
        brand_form.addRow(self.remember_check)
        layout.addWidget(self.brand_group)
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #B42318;")
        self.error_label.setObjectName("lineParameterError")
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать линию")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.catalog_combo.currentIndexChanged.connect(self._entry_changed)
        self.length_edit.textChanged.connect(lambda: self.length_confirm.setChecked(False))
        self._mode_changed()

    def _parameters_changed(self) -> None:
        if hasattr(self, "impedance_confirm"):
            self.impedance_confirm.setChecked(False)

    def _selected_entry(self) -> CatalogEntry | None:
        selected_id = self.catalog_combo.currentData()
        return next((entry for entry in self.entries if entry.id.value == selected_id), None)

    def _entry_changed(self) -> None:
        if self.mode_combo.currentData() != "catalog":
            return
        entry = self._selected_entry()
        for key, edit in self.fields.items():
            value = (entry.source if key == "source" else entry.properties.get(key)) if entry else None
            edit.setText("" if value is None else str(value))
        count = entry.properties.get("parallel_count", 1) if entry else 1
        self.parallel_spin.setValue(count if type(count) is int and 1 <= count <= self.parallel_spin.maximum() else 1)
        self.impedance_confirm.setChecked(False)

    def _mode_changed(self) -> None:
        mode = self.mode_combo.currentData()
        if self._form_mode == "manual":
            self._manual_values = {key: edit.text() for key, edit in self.fields.items()}
            self._manual_parallel = self.parallel_spin.value()
        self._form_mode = mode
        self.catalog_combo.setEnabled(mode == "catalog" and bool(self.entries))
        self.brand_group.setEnabled(mode != "draft")
        self.length_edit.setEnabled(mode != "draft")
        self.length_confirm.setEnabled(mode != "draft")
        self.parallel_spin.setEnabled(mode != "draft")
        self.remember_check.setEnabled(mode == "manual")
        for edit in self.fields.values():
            edit.setReadOnly(mode != "manual")
        if mode == "catalog":
            self._entry_changed()
        elif mode == "manual":
            for key, edit in self.fields.items():
                edit.setText(self._manual_values.get(key, ""))
            self.parallel_spin.setValue(self._manual_parallel)
        self.impedance_confirm.setChecked(False)
        self.error_label.clear()
        self.hint_label.setText({
            "manual": "Можно оставить неизвестные поля пустыми. Без проверенных длины и сопротивлений линия сохранится с неполными расчётными данными.",
            "catalog": "Марки хранятся внутри проекта. Выбор марки не подтверждает данные автоматически; остальные параметры выбранной записи также будут сохранены.",
            "draft": "Будет создана линия без марки, длины и сопротивлений. Параметры можно заполнить позднее в свойствах линии.",
        }[mode])

    def parameters(self) -> LineParametersResult:
        """Validate and return a proposal without changing the live project."""
        name = self.name_edit.text().strip() or self._default_name
        mode = self.mode_combo.currentData()
        if mode == "draft":
            return LineParametersResult(True, name, PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
        length = _length_mm(self.length_edit.text())
        if self.length_confirm.isChecked() and length is None:
            raise ValueError("Чтобы подтвердить длину, сначала задайте её.")
        entry = self._selected_entry() if mode == "catalog" else None
        remember = mode == "manual" and self.remember_check.isChecked()
        properties = {"parallel_count": self.parallel_spin.value()}
        if mode == "catalog":
            if entry is None:
                raise ValueError("В справочнике нет подходящей марки. Выберите ручной ввод или черновик.")
            count = entry.properties.get("parallel_count", 1)
            if type(count) is not int or not 1 <= count <= self.parallel_spin.maximum():
                raise ValueError("В записи справочника некорректное число параллельных цепей. Выберите ручной ввод.")
            effective = {**entry.properties, **properties}
        else:
            for key in ("conductor_mark", "material"):
                value = self.fields[key].text().strip()
                if value:
                    properties[key] = value
            for key, label, positive, nonnegative in (
                ("cross_section_mm2", "Сечение", True, False),
                ("r1_ohm_per_km", "R1", False, True),
                ("x1_ohm_per_km", "X1", False, False),
            ):
                value = _optional_number(self.fields[key].text(), label, positive=positive, nonnegative=nonnegative)
                if value is not None:
                    properties[key] = value
            source = self.fields["source"].text().strip()
            if source:
                properties["custom_parameters"] = {"data_source": source}
            if remember:
                mark = properties.get("conductor_mark", "")
                if not mark or not source:
                    raise ValueError("Для сохранения марки укажите её название и источник данных.")
                entry = CatalogEntry(
                    CatalogEntryId.new(), CatalogOrigin.USER,
                    CatalogCategoryId("cable_lines" if self._kind is LineKind.CABLE else "overhead_lines"),
                    mark, self._type_id, 1, model=mark,
                    properties={key: value for key, value in properties.items() if key != "parallel_count"},
                    source=source,
                )
            effective = properties
        if self.impedance_confirm.isChecked():
            for key, label in (("r1_ohm_per_km", "R1"), ("x1_ohm_per_km", "X1")):
                value = effective.get(key)
                if value is None:
                    raise ValueError("Чтобы подтвердить сопротивления, задайте R1 и X1.")
                _optional_number(str(value), label, nonnegative=(key == "r1_ohm_per_km"))
            source = entry.source if mode == "catalog" else self.fields["source"].text().strip()
            if not source:
                raise ValueError("Укажите источник проверенных сопротивлений.")
        physical = PhysicalLineInput(
            length,
            DataConfirmation.CONFIRMED if self.length_confirm.isChecked() else DataConfirmation.UNCONFIRMED,
            properties,
            DataConfirmation.CONFIRMED if self.impedance_confirm.isChecked() else DataConfirmation.UNCONFIRMED,
        )
        return LineParametersResult(True, name, physical, entry, remember)

    def accept(self) -> None:
        try:
            self.proposal = self.parameters()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        super().accept()


def ask_line_parameters(parent, draft, project) -> LineParametersResult:
    dialog = LineParametersDialog(parent, draft, project)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.proposal is not None:
        return dialog.proposal
    return LineParametersResult(False, draft.name, PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
