# -*- coding: utf-8 -*-
"""Современные панели рабочего места редактора схем.

Виджеты не содержат электрической логики: все изменения выполняет переданный
``ProjectEditorController``.  Старый экран расчёта может продолжать жить рядом
с :class:`EditorWorkspaceWidget` как отдельный режим анализа.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QMimeData, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDrag, QIcon, QIconEngine, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..domain.diagram import DiagramRouteId, GraphicalRepresentationId, PageId
from ..domain.electrical import EquipmentId, EquipmentTypeId, LineKind, VoltageClassId, thaw_json
from ..editor.connection_voltage import endpoint_voltage
from ..editor.controller import EditorCommandError
from ..editor.collision import DiagramCollisionService
from ..editor.validation import ProjectValidationService
from .editor_scene import CanvasMode, EditorCanvas, EQUIPMENT_MIME_TYPE
from .strings import property_label, ui_text, unit_label
from .theme import COLORS


@dataclass(frozen=True, slots=True)
class EquipmentLibraryEntry:
    category: str
    title: str
    type_id: str
    symbol_key: str = ""
    graphics: Mapping[str, Any] | None = None
    target_kind: str = "equipment"

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type_id": self.type_id,
            "target_kind": self.target_kind,
            "name": self.title,
        }
        if self.symbol_key:
            result["symbol_key"] = self.symbol_key
        if self.graphics:
            result["graphics"] = dict(self.graphics)
        return result


@dataclass(frozen=True, slots=True)
class PropertyField:
    key: str
    label: str
    value: Any
    group: str
    unit: str = ""
    source: str = ""
    required: bool = False
    error: str = ""
    hint: str = ""
    editable: bool = True
    choices: tuple[tuple[str, Any], ...] = ()


class EquipmentLibraryTree(QTreeWidget):
    placementRequested = Signal(object)

    PAYLOAD_ROLE = Qt.ItemDataRole.UserRole + 20

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._repeat_enabled = False
        self.itemClicked.connect(self._activate)

    def set_entries(self, entries: Iterable[EquipmentLibraryEntry]) -> None:
        self.clear()
        categories: dict[str, QTreeWidgetItem] = {}
        for entry in entries:
            category = categories.get(entry.category)
            if category is None:
                category = QTreeWidgetItem([entry.category])
                font = category.font(0)
                font.setBold(True)
                category.setFont(0, font)
                categories[entry.category] = category
                self.addTopLevelItem(category)
            item = QTreeWidgetItem([entry.title])
            item.setData(0, self.PAYLOAD_ROLE, entry.payload())
            item.setToolTip(
                0,
                f"{entry.title}\nЩёлкните для одноразовой вставки; "
                "Shift+щелчок — многократно",
            )
            category.addChild(item)
        self.expandAll()

    def startDrag(self, supported_actions) -> None:  # noqa: N802 - Qt API
        del supported_actions
        item = self.currentItem()
        payload = item.data(0, self.PAYLOAD_ROLE) if item is not None else None
        if not isinstance(payload, dict):
            return
        mime = QMimeData()
        mime.setData(
            EQUIPMENT_MIME_TYPE,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def _activate(self, item: QTreeWidgetItem, column: int) -> None:
        del column
        payload = item.data(0, self.PAYLOAD_ROLE)
        if isinstance(payload, dict):
            effective = dict(payload)
            effective["placement_repeat"] = bool(
                self._repeat_enabled
                or QApplication.keyboardModifiers()
                & Qt.KeyboardModifier.ShiftModifier
            )
            self.placementRequested.emit(effective)

    def set_repeat_enabled(self, enabled: bool) -> None:
        self._repeat_enabled = bool(enabled)


class ProjectEquipmentPanel(QWidget):
    representationSelectionRequested = Signal(object)
    graphicsSelectionRequested = Signal(object, object)
    pageSelectionRequested = Signal(object)
    placementRequested = Signal(object)

    REPRESENTATION_ROLE = Qt.ItemDataRole.UserRole + 21
    PAGE_ROLE = Qt.ItemDataRole.UserRole + 24
    ROUTE_ROLE = Qt.ItemDataRole.UserRole + 25
    LARGE_PAGE_COUNT = 8

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("leftPanel")
        self.setMinimumWidth(250)
        self.search = QLineEdit()
        self.search.setPlaceholderText(ui_text("tree.search"))
        self.tabs = QTabWidget()
        self.project_tree = QTreeWidget()
        self.project_tree.setHeaderHidden(True)
        self.project_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.project_tree.itemSelectionChanged.connect(self._project_selection_changed)
        self.project_tree.itemClicked.connect(self._project_item_activated)
        self.project_tree.itemActivated.connect(self._project_item_activated)
        self.library = EquipmentLibraryTree()
        self.library.placementRequested.connect(self.placementRequested.emit)
        self.tabs.addTab(self.project_tree, ui_text("panel.project"))
        self.tabs.addTab(self.library, ui_text("panel.equipment"))
        self.search.textChanged.connect(self._filter)
        self.tabs.currentChanged.connect(lambda _index: self._filter(self.search.text()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 8, 8)
        layout.addWidget(self.search)
        layout.addWidget(self.tabs, 1)
        self._syncing = False
        self._document_id = None
        self._page_expansion: dict[str, bool] = {}
        self._searching = False

    def _remember_page_expansion(self) -> None:
        for index in range(self.project_tree.topLevelItemCount()):
            item = self.project_tree.topLevelItem(index)
            page_id = item.data(0, self.PAGE_ROLE)
            if page_id:
                self._page_expansion[str(page_id)] = item.isExpanded()

    def refresh(self, controller: Any) -> None:
        if self._document_id != controller.diagram.id:
            self._document_id = controller.diagram.id
            self._page_expansion.clear()
        elif not self._searching:
            self._remember_page_expansion()
        selected = {
            str(item.data(0, self.REPRESENTATION_ROLE))
            for item in self.project_tree.selectedItems()
            if item.data(0, self.REPRESENTATION_ROLE)
        }
        selected_routes = {
            str(item.data(0, self.ROUTE_ROLE))
            for item in self.project_tree.selectedItems()
            if item.data(0, self.ROUTE_ROLE)
        }
        self._syncing = True
        try:
            self.project_tree.clear()
            representations_by_page: dict[PageId, list[Any]] = {}
            for representation in controller.diagram.representations.values():
                representations_by_page.setdefault(representation.page_id, []).append(representation)
            routes_by_page: dict[PageId, list[Any]] = {}
            for route in controller.diagram.routes.values():
                if route.equipment_id is not None:
                    routes_by_page.setdefault(route.page_id, []).append(route)
            pages = sorted(controller.diagram.pages.values(), key=lambda row: (row.order, row.id.value))
            active_page = getattr(getattr(controller, "workspace_state", None), "active_page_id", None)
            if not any(page.id.value == active_page for page in pages):
                active_page = pages[0].id.value if pages else None
            for page in pages:
                page_item = QTreeWidgetItem([page.name])
                page_item.setData(0, self.PAGE_ROLE, page.id.value)
                page_font = page_item.font(0)
                page_font.setBold(True)
                page_item.setFont(0, page_font)
                self.project_tree.addTopLevelItem(page_item)
                for representation in sorted(
                    representations_by_page.get(page.id, ()),
                    key=lambda row: (row.z_index, row.id.value),
                ):
                    title = representation.label or self._target_name(controller, representation)
                    child = QTreeWidgetItem([title])
                    child.setData(0, self.REPRESENTATION_ROLE, representation.id.value)
                    child.setToolTip(0, self._target_name(controller, representation)
                                     + "\n" + representation.id.value)
                    page_item.addChild(child)
                    child.setSelected(representation.id.value in selected)
                for route in sorted(routes_by_page.get(page.id, ()), key=lambda row: row.id.value):
                    child = QTreeWidgetItem([self._target_name(controller, route)])
                    child.setData(0, self.ROUTE_ROLE, route.id.value)
                    child.setToolTip(0, route.id.value)
                    page_item.addChild(child)
                    child.setSelected(route.id.value in selected_routes)
                expanded = self._page_expansion.setdefault(
                    page.id.value,
                    len(pages) <= self.LARGE_PAGE_COUNT or page.id.value == active_page,
                )
                page_item.setExpanded(expanded)

            placed_equipment = {
                item.equipment_id for item in controller.diagram.representations.values()
                if item.equipment_id is not None
            }
            placed_equipment.update(
                route.equipment_id for route in controller.diagram.routes.values()
                if route.equipment_id is not None
            )
            unplaced = [
                item for item in controller.model.equipment.values()
                if item.id not in placed_equipment
            ]
            if unplaced:
                root = QTreeWidgetItem([ui_text("panel.unplaced")])
                root.setForeground(0, QColor(COLORS["amber"]))
                for equipment in sorted(unplaced, key=lambda row: row.name.casefold()):
                    child = QTreeWidgetItem([equipment.name])
                    child.setToolTip(0, equipment.id.value)
                    root.addChild(child)
                self.project_tree.addTopLevelItem(root)
                root.setExpanded(True)
            self.library.set_entries(self._library_entries(controller))
        finally:
            self._syncing = False
        if self.search.text().strip():
            self._filter(self.search.text())

    @staticmethod
    def _target_name(controller: Any, representation: Any) -> str:
        if representation.equipment_id is not None:
            equipment = controller.model.equipment.get(representation.equipment_id)
            return equipment.name if equipment is not None else "Удалённое оборудование"
        node = controller.model.electrical_nodes.get(representation.electrical_node_id)
        return (node.name if node and node.name else "Электрический узел")

    @staticmethod
    def _library_entries(controller: Any) -> tuple[EquipmentLibraryEntry, ...]:
        definitions = {
            definition.id.value: definition
            for definition in controller.model.equipment_types.values()
            if definition.schema_version == 1
        }
        requested = (
            ("builtin.external_grid", "equipment.sources", "equipment.external_grid", "external_grid", None, "equipment"),
            ("__node__", "equipment.nodes_buses", "equipment.busbar_horizontal", "busbar", {"width": 240.0, "height": 12.0, "rotation_deg": 180.0}, "electrical_node"),
            ("__node__", "equipment.nodes_buses", "equipment.busbar_vertical", "busbar", {"width": 240.0, "height": 12.0, "rotation_deg": 90.0}, "electrical_node"),
            ("physical_line.overhead", "equipment.lines", "equipment.overhead_line", "line", None, "physical_line"),
            ("physical_line.cable", "equipment.lines", "equipment.cable_line", "line", None, "physical_line"),
            ("builtin.circuit_breaker", "equipment.switchgear", "equipment.circuit_breaker", "circuit_breaker", None, "equipment"),
            ("builtin.disconnector", "equipment.switchgear", "equipment.disconnector", "disconnector", None, "equipment"),
            ("builtin.recloser", "equipment.switchgear", "equipment.recloser", "recloser", None, "equipment"),
            ("builtin.transformer_2w", "equipment.transformers", "equipment.transformer_2w", "transformer_2w", None, "equipment"),
            ("builtin.load", "equipment.consumers", "equipment.load", "load", None, "equipment"),
        )
        entries = []
        for type_id, category_key, title_key, symbol, graphics, target_kind in requested:
            if target_kind not in {"electrical_node", "physical_line"} and type_id not in definitions:
                continue
            entries.append(EquipmentLibraryEntry(
                ui_text(category_key), ui_text(title_key), type_id, symbol, graphics,
                target_kind,
            ))
        return tuple(entries)

    def select_representations(self, ids: object) -> None:
        wanted = {
            item.value if isinstance(item, GraphicalRepresentationId) else str(item)
            for item in (ids or ())
        }
        self._syncing = True
        try:
            iterator = QTreeWidgetItemIteratorCompat(self.project_tree)
            for item in iterator:
                value = item.data(0, self.REPRESENTATION_ROLE)
                if value:
                    item.setSelected(str(value) in wanted)
        finally:
            self._syncing = False

    def select_routes(self, ids: object) -> None:
        wanted = {getattr(item, "value", str(item)) for item in (ids or ())}
        self._syncing = True
        try:
            for item in QTreeWidgetItemIteratorCompat(self.project_tree):
                value = item.data(0, self.ROUTE_ROLE)
                if value:
                    item.setSelected(str(value) in wanted)
        finally:
            self._syncing = False

    def _project_item_activated(self, item: QTreeWidgetItem, column: int) -> None:
        del column
        page_id = item.data(0, self.PAGE_ROLE)
        if page_id and not self._syncing:
            self.pageSelectionRequested.emit(PageId(str(page_id)))

    def set_repeat_placement(self, enabled: bool) -> None:
        self.library.set_repeat_enabled(enabled)

    def _project_selection_changed(self) -> None:
        if self._syncing:
            return
        values = []
        routes = []
        for item in self.project_tree.selectedItems():
            value = item.data(0, self.REPRESENTATION_ROLE)
            if value:
                values.append(GraphicalRepresentationId(str(value)))
            route_id = item.data(0, self.ROUTE_ROLE)
            if route_id:
                routes.append(DiagramRouteId(str(route_id)))
        self.representationSelectionRequested.emit(tuple(values))
        self.graphicsSelectionRequested.emit(tuple(values), tuple(routes))
        if not values and not routes:
            current = self.project_tree.currentItem()
            if current is not None and current.isSelected():
                self._project_item_activated(current, 0)

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        if needle and not self._searching:
            self._remember_page_expansion()
        elif not needle and self._searching:
            for index in range(self.project_tree.topLevelItemCount()):
                item = self.project_tree.topLevelItem(index)
                page_id = item.data(0, self.PAGE_ROLE)
                if page_id:
                    item.setExpanded(self._page_expansion.get(str(page_id), False))
        self._searching = bool(needle)
        tree = self.project_tree if self.tabs.currentIndex() == 0 else self.library

        def visit(item: QTreeWidgetItem) -> bool:
            child_visible = False
            for index in range(item.childCount()):
                child_visible = visit(item.child(index)) or child_visible
            visible = (not needle or needle in item.text(0).casefold()
                       or needle in item.toolTip(0).casefold() or child_visible)
            item.setHidden(not visible)
            if needle and child_visible:
                item.setExpanded(True)
            return visible

        for index in range(tree.topLevelItemCount()):
            visit(tree.topLevelItem(index))


class QTreeWidgetItemIteratorCompat:
    """Маленький итератор без зависимости от особенностей биндинга Qt."""

    def __init__(self, tree: QTreeWidget):
        self._stack = [tree.topLevelItem(index) for index in reversed(range(tree.topLevelItemCount()))]

    def __iter__(self):
        while self._stack:
            item = self._stack.pop()
            self._stack.extend(item.child(index) for index in reversed(range(item.childCount())))
            yield item


class PropertyInspectorPanel(QWidget):
    propertyEdited = Signal(str, object)

    FIELD_ROLE = Qt.ItemDataRole.UserRole + 22
    TYPE_ROLE = Qt.ItemDataRole.UserRole + 23

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setMinimumWidth(300)
        self.title = QLabel(ui_text("property.none"))
        self.title.setObjectName("inspectorTitle")
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([
            ui_text("property.name"), ui_text("property.value"),
            ui_text("property.unit"), ui_text("property.source"),
        ])
        self.tree.setAlternatingRowColors(True)
        self.tree.itemChanged.connect(self._item_changed)
        self._loading = False
        self._globally_editable = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.addWidget(self.title)
        layout.addWidget(self.tree, 1)

    def set_fields(
        self,
        title: str,
        fields: Iterable[PropertyField],
        *,
        editable: bool,
    ) -> None:
        self._loading = True
        self._globally_editable = bool(editable)
        try:
            self.title.setText(title)
            self.tree.clear()
            groups: dict[str, QTreeWidgetItem] = {}
            for field in fields:
                group = groups.get(field.group)
                if group is None:
                    group = QTreeWidgetItem([field.group])
                    group.setFirstColumnSpanned(True)
                    font = group.font(0)
                    font.setBold(True)
                    group.setFont(0, font)
                    groups[field.group] = group
                    self.tree.addTopLevelItem(group)
                item = QTreeWidgetItem([
                    field.label + (" *" if field.required else ""),
                    self._display_value(field.value), field.unit, field.source,
                ])
                item.setData(0, self.FIELD_ROLE, field.key)
                item.setData(0, self.TYPE_ROLE, type(field.value).__name__)
                hint = "\n".join(value for value in (
                    field.label + (" (" + field.unit + ")" if field.unit else ""),
                    "Источник: " + field.source if field.source else "",
                    field.hint, field.error,
                ) if value)
                if hint:
                    item.setToolTip(0, hint)
                    item.setToolTip(1, hint)
                if field.error:
                    item.setForeground(0, QColor(COLORS["red"]))
                    item.setBackground(1, QColor("#FDEBEC"))
                if editable and field.editable and field.key != "graphics.rotation_deg" and not field.choices:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                group.addChild(item)
                if field.key == "graphics.rotation_deg":
                    self._set_orientation_editor(
                        item, field, editable=editable and field.editable
                    )
                elif field.choices:
                    self._set_choice_editor(item, field, editable=editable and field.editable)
            self.tree.expandAll()
            self._layout_columns()
        finally:
            self._loading = False

    def _layout_columns(self) -> None:
        """Keep values and units visible in the narrow right-hand inspector."""
        width = self.tree.viewport().width()
        compact = width < 540
        self.tree.setColumnHidden(3, compact)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, min(180, max(110, int(width * 0.44))))
        self.tree.setColumnWidth(2, 44)
        if not compact:
            self.tree.setColumnWidth(3, 160)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._layout_columns()

    def _set_choice_editor(self, item, field: PropertyField, *, editable: bool) -> None:
        combo = QComboBox(self.tree)
        combo.setObjectName("voltageClassCombo:" + field.key)
        combo.setPlaceholderText("Выберите класс напряжения")
        for label, value in field.choices:
            combo.addItem(label, value)
        combo.setCurrentIndex(combo.findData(field.value))
        combo.setEnabled(editable)
        combo.setToolTip(field.hint)
        self.tree.setItemWidget(item, 1, combo)
        # Only an explicit user choice emits an edit; loading never chooses
        # the first voltage class for an unknown terminal.
        combo.activated.connect(
            lambda index, widget=combo, key=field.key: self._choice_selected(widget, key, index)
        )

    def _choice_selected(self, combo: QComboBox, key: str, index: int) -> None:
        if self._loading or not self._globally_editable or not combo.isEnabled() or index < 0:
            return
        self.propertyEdited.emit(key, combo.itemData(index))

    def _set_orientation_editor(
        self,
        item: QTreeWidgetItem,
        field: PropertyField,
        *,
        editable: bool,
    ) -> None:
        """Предлагать два положения, не переписывая угол старого файла."""

        combo = QComboBox(self.tree)
        combo.setObjectName("orientationPositionCombo")
        combo.addItem(ui_text("action.orientation_vertical"), 90)
        combo.addItem(ui_text("action.orientation_horizontal"), 180)
        index = combo.findData(field.value)
        if index < 0:
            # An imported 0/270/arbitrary angle is retained until the user
            # explicitly selects one of the two supported editing positions.
            combo.setPlaceholderText(
                ui_text(
                    "property.orientation_preserved",
                    angle=self._display_value(field.value),
                )
            )
        combo.setCurrentIndex(index)
        combo.setEnabled(editable)
        combo.setToolTip(ui_text("property.orientation_hint"))
        self.tree.setItemWidget(item, 1, combo)
        combo.currentIndexChanged.connect(
            lambda selected, widget=combo: self._orientation_selected(
                widget, selected
            )
        )

    def _orientation_selected(self, combo: QComboBox, index: int) -> None:
        if (
            self._loading
            or not self._globally_editable
            or not combo.isEnabled()
            or index < 0
        ):
            return
        value = combo.itemData(index)
        if value in (90, 180):
            self.propertyEdited.emit("graphics.rotation_deg", value)

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, bool):
            return "Да" if value else "Нет"
        if value is None or value == "":
            return "—"
        if isinstance(value, float):
            return f"{value:g}".replace(".", ",")
        if isinstance(value, (tuple, list)):
            return ", ".join(str(item) for item in value)
        return str(value)

    @staticmethod
    def _parse_value(text: str, type_name: str) -> Any:
        value = text.strip()
        if type_name == "bool":
            lowered = value.casefold()
            if lowered in {"да", "истина", "включён", "включено", "1"}:
                return True
            if lowered in {"нет", "ложь", "отключён", "отключено", "0"}:
                return False
            raise ValueError("Введите «Да» или «Нет».")
        if type_name == "int":
            return int(value.replace(" ", ""))
        if type_name == "float":
            return float(value.replace(" ", "").replace(",", "."))
        return value

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._loading or column != 1 or not self._globally_editable:
            return
        key = item.data(0, self.FIELD_ROLE)
        if not key:
            return
        # Changing decoration also emits itemChanged. It is not a second edit,
        # and command callbacks may synchronously rebuild this entire tree.
        self._loading = True
        try:
            try:
                value = self._parse_value(item.text(1), str(item.data(0, self.TYPE_ROLE) or "str"))
            except (TypeError, ValueError) as exc:
                item.setToolTip(1, str(exc))
                item.setBackground(1, QColor("#FDEBEC"))
                return
            item.setBackground(1, QColor(Qt.GlobalColor.transparent))
        finally:
            self._loading = False
        self.propertyEdited.emit(str(key), value)


class DiagnosticsBottomPanel(QWidget):
    objectRequested = Signal(object)

    OBJECT_ROLE = Qt.ItemDataRole.UserRole + 24
    DIAGNOSTIC_ROLE = Qt.ItemDataRole.UserRole + 25

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("bottomPanel")
        self.tabs = QTabWidget()
        self.problems = QListWidget()
        self.diagnostics = QListWidget()
        self.journal = QListWidget()
        self.tabs.addTab(self.problems, ui_text("panel.problems"))
        self.tabs.addTab(self.diagnostics, ui_text("panel.diagnostics"))
        self.tabs.addTab(self.journal, ui_text("panel.journal"))
        self.problems.itemDoubleClicked.connect(self._problem_activated)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 6)
        layout.addWidget(self.tabs)

    def set_problems(self, problems: Iterable[Any]) -> None:
        self.problems.clear()
        rows = tuple(problems)
        if not rows:
            self.problems.addItem(ui_text("diagnostics.no_problems"))
            return
        for problem in rows:
            message = str(getattr(problem, "message", problem))
            action = str(getattr(problem, "action", "")).strip()
            severity = str(getattr(problem, "severity", "warning")).lower()
            visible = message + (f"\nЧто сделать: {action}" if action else "")
            item = QListWidgetItem(visible)
            item.setForeground(QColor(COLORS["red"] if severity == "error" else COLORS["amber"]))
            item.setToolTip(visible)
            item.setData(self.DIAGNOSTIC_ROLE, problem)
            object_id = getattr(problem, "object_id", None)
            if object_id is not None:
                item.setData(self.OBJECT_ROLE, getattr(object_id, "value", str(object_id)))
            self.problems.addItem(item)

    def set_diagnostics(self, values: Mapping[str, Any] | Iterable[str]) -> None:
        self.diagnostics.clear()
        rows = (
            [f"{key}: {value}" for key, value in values.items()]
            if isinstance(values, Mapping) else list(values)
        )
        if not rows:
            rows = [ui_text("diagnostics.no_data")]
        self.diagnostics.addItems([str(row) for row in rows])

    def set_selected_count(self, count: int) -> None:
        """Обновить только счётчик выбора без повторной проверки проекта."""

        text = f"Выбрано: {max(0, int(count))}"
        for index in range(self.diagnostics.count()):
            item = self.diagnostics.item(index)
            if item.text().startswith("Выбрано:"):
                item.setText(text)
                return
        self.diagnostics.addItem(text)

    def set_journal(self, entries: Iterable[Any]) -> None:
        self.journal.clear()
        rows = tuple(entries)
        if not rows:
            self.journal.addItem(ui_text("journal.empty"))
            return
        for entry in reversed(rows):
            self.journal.addItem(str(getattr(entry, "message", getattr(entry, "description", entry))))

    def _problem_activated(self, item: QListWidgetItem) -> None:
        diagnostic = item.data(self.DIAGNOSTIC_ROLE)
        if diagnostic is not None and not isinstance(diagnostic, str):
            self.objectRequested.emit(diagnostic)
            return
        object_id = item.data(self.OBJECT_ROLE)
        if object_id:
            self.objectRequested.emit(object_id)


class _OrientationIconEngine(QIconEngine):
    """Draw orientation at the requested size/DPI, without font glyphs or bitmaps."""

    def __init__(self, *, vertical: bool):
        super().__init__()
        self._vertical = vertical

    def clone(self):
        return _OrientationIconEngine(vertical=self._vertical)

    def paint(self, painter, rect, mode, state):
        del state
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.center())
        scale = min(rect.width(), rect.height()) / 24.0
        painter.scale(scale, scale)
        if not self._vertical:
            painter.rotate(90)
        color = COLORS["muted"] if mode == QIcon.Mode.Disabled else COLORS["text"]
        pen = QPen(QColor(color), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(-4.0, -6.0, 8.0, 12.0), 1.5, 1.5)
        painter.drawLine(0, -10, 0, -6)
        painter.drawLine(0, 6, 0, 10)
        painter.restore()

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):  # noqa: N802
        image = QPixmap(QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale))))
        image.setDevicePixelRatio(scale)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return image


class EditorCommandBar(QToolBar):
    fileRequested = Signal()
    saveRequested = Signal()
    undoRequested = Signal()
    redoRequested = Signal()
    modeRequested = Signal(str)
    fitRequested = Signal()
    actualSizeRequested = Signal()
    gridRequested = Signal(bool)
    snapRequested = Signal(bool)
    validateRequested = Signal()
    developerOverlayRequested = Signal(bool)
    confirmSwitchingRequested = Signal(bool)
    repeatPlacementRequested = Signal(bool)
    rotationPositionRequested = Signal(int)
    autoOrientationRequested = Signal()
    labelDisplayRequested = Signal(object)
    autoLabelsRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMovable(False)
        self.setFloatable(False)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._selection_count = 0
        self._edit_mode = True
        self.file_action = self.addAction(ui_text("action.file"))
        self.save_action = self.addAction(ui_text("action.save"))
        self.addSeparator()
        self.undo_action = self.addAction(ui_text("action.undo"))
        self.redo_action = self.addAction(ui_text("action.redo"))
        self.vertical_orientation_action = self.addAction(
            ui_text("action.orientation_vertical")
        )
        self.horizontal_orientation_action = self.addAction(
            ui_text("action.orientation_horizontal")
        )
        for action, vertical in (
            (self.vertical_orientation_action, True),
            (self.horizontal_orientation_action, False),
        ):
            action.setIcon(QIcon(_OrientationIconEngine(vertical=vertical)))
            action.setToolTip(action.text() + "\n" + ui_text("property.orientation_hint"))
            # Keep QAction text for toolbar overflow and accessibility; only
            # these two visible buttons use compact, vector-drawn icons.
            button = self.widgetForAction(action)
            if isinstance(button, QToolButton):
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                button.setIconSize(QSize(24, 24))
                button.setAccessibleName(action.text())
                button.setAccessibleDescription(ui_text("property.orientation_hint"))
        # Compatibility attributes point at the same two absolute actions;
        # they do not add clockwise/counter-clockwise choices to the toolbar.
        self.rotate_right_action = self.vertical_orientation_action
        self.rotate_left_action = self.horizontal_orientation_action
        self.auto_orientation_action = self.addAction(
            ui_text("action.auto_orientation")
        )
        self.addSeparator()
        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        self.edit_action = QAction(ui_text("mode.edit"), self, checkable=True)
        self.analysis_action = QAction(ui_text("mode.analysis"), self, checkable=True)
        self.edit_action.setToolTip(ui_text("mode.edit.tooltip"))
        self.analysis_action.setToolTip(ui_text("mode.analysis.tooltip"))
        mode_group.addAction(self.edit_action)
        mode_group.addAction(self.analysis_action)
        self.addAction(self.edit_action)
        self.addAction(self.analysis_action)
        self.tool_label = QLabel("Редактирование • Выбор")
        self.tool_label.setObjectName("muted")
        self.tool_label.setMinimumWidth(190)
        self.addWidget(self.tool_label)
        self.addSeparator()
        self.fit_action = self.addAction(ui_text("action.fit"))
        self.actual_action = self.addAction(ui_text("action.actual_size"))
        self.scale_combo = QComboBox()
        self.scale_combo.setMinimumWidth(82)
        for value in (25, 50, 75, 100, 125, 150, 200, 400):
            self.scale_combo.addItem(f"{value} %", value / 100.0)
        self._preset_zoom_count = self.scale_combo.count()
        self.scale_combo.setCurrentIndex(self.scale_combo.findData(1.0))
        self.addWidget(self.scale_combo)
        self.labels_button = QToolButton(self)
        self.labels_button.setText("Подписи")
        self.labels_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        label_menu = QMenu(self.labels_button)
        self.labels_action = label_menu.addAction("Показывать подписи")
        self.parameters_action = label_menu.addAction("Показывать параметры")
        self.results_action = label_menu.addAction("Результаты расчёта — этап B4")
        for action, checked in ((self.labels_action, True), (self.parameters_action, True), (self.results_action, False)):
            action.setCheckable(True)
            action.setChecked(checked)
        self.results_action.setEnabled(False)
        self.results_action.setToolTip("Подготовка этапа B4. В B3 результаты на схеме ещё не отображаются.")
        label_menu.setToolTipsVisible(True)
        label_menu.addSeparator()
        self.auto_labels_action = label_menu.addAction("Автоподписи: выбранные / вся страница")
        self.auto_labels_action.setToolTip("Возвращает подписи к автоматическому размещению. Без выбора — вся страница. Действие можно отменить.")
        self.labels_button.setMenu(label_menu)
        self.addWidget(self.labels_button)
        self.labels_action.toggled.connect(lambda value: self.labelDisplayRequested.emit({"show_labels": value}))
        self.parameters_action.toggled.connect(lambda value: self.labelDisplayRequested.emit({"show_parameters": value}))
        self.auto_labels_action.triggered.connect(lambda: self.autoLabelsRequested.emit())
        self.grid_action = QAction(ui_text("action.grid"), self, checkable=True, checked=True)
        self.snap_action = QAction(ui_text("action.snap"), self, checkable=True, checked=True)
        self.validate_action = QAction(ui_text("action.validate"), self)
        self.developer_action = QAction(ui_text("action.developer_overlay"), self, checkable=True)
        self.confirm_switching_action = QAction(
            ui_text("confirm.switching"), self, checkable=True, checked=True
        )
        self.repeat_placement_action = QAction(
            ui_text("action.repeat_placement"), self, checkable=True
        )
        self.addAction(self.grid_action)
        self.addAction(self.snap_action)
        self.addAction(self.validate_action)
        self.addAction(self.developer_action)
        self.addAction(self.repeat_placement_action)
        self.addAction(self.confirm_switching_action)

        self.file_action.triggered.connect(lambda: self.fileRequested.emit())
        self.save_action.triggered.connect(lambda: self.saveRequested.emit())
        self.undo_action.triggered.connect(lambda: self.undoRequested.emit())
        self.redo_action.triggered.connect(lambda: self.redoRequested.emit())
        self.vertical_orientation_action.triggered.connect(
            lambda: self.rotationPositionRequested.emit(90)
        )
        self.horizontal_orientation_action.triggered.connect(
            lambda: self.rotationPositionRequested.emit(180)
        )
        self.auto_orientation_action.triggered.connect(
            lambda: self.autoOrientationRequested.emit()
        )
        self.edit_action.triggered.connect(lambda checked=False: checked and self.modeRequested.emit(CanvasMode.EDIT.value))
        self.analysis_action.triggered.connect(lambda checked=False: checked and self.modeRequested.emit(CanvasMode.ANALYSIS.value))
        self.fit_action.triggered.connect(lambda: self.fitRequested.emit())
        self.actual_action.triggered.connect(lambda: self.actualSizeRequested.emit())
        self.grid_action.toggled.connect(self.gridRequested.emit)
        self.snap_action.toggled.connect(self.snapRequested.emit)
        self.validate_action.triggered.connect(lambda: self.validateRequested.emit())
        self.developer_action.toggled.connect(self.developerOverlayRequested.emit)
        self.repeat_placement_action.toggled.connect(
            self.repeatPlacementRequested.emit
        )
        self.confirm_switching_action.toggled.connect(self.confirmSwitchingRequested.emit)

    def refresh(self, controller: Any) -> None:
        mode = str(getattr(getattr(controller, "mode", CanvasMode.EDIT), "value", getattr(controller, "mode", CanvasMode.EDIT)))
        self._edit_mode = mode == CanvasMode.EDIT.value
        self.auto_labels_action.setEnabled(self._edit_mode)
        self.undo_action.setEnabled(
            self._edit_mode and bool(getattr(controller, "can_undo", False))
        )
        self.redo_action.setEnabled(
            self._edit_mode and bool(getattr(controller, "can_redo", False))
        )
        self.edit_action.setChecked(mode == CanvasMode.EDIT.value)
        self.analysis_action.setChecked(mode == CanvasMode.ANALYSIS.value)
        self._refresh_orientation_actions()
        state = getattr(controller, "workspace_state", None)
        if state is not None:
            for action, key, default in ((self.labels_action, "show_labels", True), (self.parameters_action, "show_parameters", True), (self.results_action, "show_results", False)):
                action.blockSignals(True)
                action.setChecked(bool(getattr(state, key, default)))
                action.blockSignals(False)
            self.grid_action.blockSignals(True)
            self.snap_action.blockSignals(True)
            self.developer_action.blockSignals(True)
            self.confirm_switching_action.blockSignals(True)
            self.grid_action.setChecked(bool(getattr(state, "grid_visible", True)))
            self.snap_action.setChecked(bool(getattr(state, "snap_enabled", True)))
            self.developer_action.setChecked(bool(getattr(state, "developer_diagnostics", False)))
            self.confirm_switching_action.setChecked(bool(getattr(state, "confirm_switching", True)))
            self.grid_action.blockSignals(False)
            self.snap_action.blockSignals(False)
            self.developer_action.blockSignals(False)
            self.confirm_switching_action.blockSignals(False)

    def set_zoom(self, zoom: float) -> None:
        zoom = float(zoom)
        if not math.isfinite(zoom) or zoom <= 0:
            return
        index = next((index for index in range(self._preset_zoom_count)
                      if math.isclose(float(self.scale_combo.itemData(index)), zoom,
                                      rel_tol=1e-10, abs_tol=1e-12)), -1)
        previous = self.scale_combo.blockSignals(True)
        try:
            if index < 0:
                # Fit/wheel/restored zoom need one current value, not a list
                # of every intermediate scale and not the nearest preset.
                index = self._preset_zoom_count
                label = f"{zoom * 100:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " %"
                if self.scale_combo.count() == index:
                    self.scale_combo.addItem(label, zoom)
                else:
                    self.scale_combo.setItemText(index, label)
                    self.scale_combo.setItemData(index, zoom)
            self.scale_combo.setCurrentIndex(index)
        finally:
            self.scale_combo.blockSignals(previous)

    def set_selection_count(self, count: int) -> None:
        self._selection_count = max(0, int(count))
        self._refresh_orientation_actions()

    def _refresh_orientation_actions(self) -> None:
        enabled = self._edit_mode and self._selection_count == 1
        self.vertical_orientation_action.setEnabled(enabled)
        self.horizontal_orientation_action.setEnabled(enabled)
        self.auto_orientation_action.setEnabled(enabled)

    def set_tool_state(self, state: object) -> None:
        display = str(getattr(state, "display_name", "Выбор"))
        mode = getattr(state, "editor_mode", None)
        mode_value = str(getattr(mode, "value", mode or "edit"))
        mode_text = "Анализ" if mode_value == "analysis" else "Редактирование"
        self.tool_label.setText(f"{mode_text} • {display}")
        self.tool_label.setToolTip(str(getattr(state, "status_text", display)))


class EditorWorkspaceWidget(QWidget):
    """Готовая точка интеграции Этапа 3 в главное окно."""

    saveRequested = Signal()
    fileRequested = Signal()
    validateRequested = Signal()
    selectionChanged = Signal(object)
    statusMessage = Signal(str)
    errorOccurred = Signal(str)

    def __init__(
        self,
        controller: Any,
        parent: QWidget | None = None,
        *,
        confirm_deletions: bool = True,
    ):
        super().__init__(parent)
        self.controller = controller
        self.command_bar = EditorCommandBar()
        self.side_panel = ProjectEquipmentPanel()
        self.canvas = EditorCanvas(
            controller, confirm_deletions=confirm_deletions
        )
        self.inspector = PropertyInspectorPanel()
        self.bottom_panel = DiagnosticsBottomPanel()
        self._validation_service = ProjectValidationService()
        self._selected_ids: tuple[GraphicalRepresentationId, ...] = ()
        self._selected_route_ids: tuple[DiagramRouteId, ...] = ()

        horizontal = QSplitter(Qt.Orientation.Horizontal)
        horizontal.addWidget(self.side_panel)
        horizontal.addWidget(self.canvas)
        horizontal.addWidget(self.inspector)
        horizontal.setStretchFactor(0, 0)
        horizontal.setStretchFactor(1, 1)
        horizontal.setStretchFactor(2, 0)
        horizontal.setSizes([270, 1000, 330])
        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(horizontal)
        vertical.addWidget(self.bottom_panel)
        vertical.setStretchFactor(0, 1)
        vertical.setStretchFactor(1, 0)
        vertical.setSizes([720, 210])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.command_bar)
        layout.addWidget(vertical, 1)

        self.command_bar.fileRequested.connect(self.fileRequested.emit)
        self.command_bar.saveRequested.connect(self.saveRequested.emit)
        self.command_bar.undoRequested.connect(self.canvas.undo)
        self.command_bar.redoRequested.connect(self.canvas.redo)
        self.command_bar.modeRequested.connect(self._set_mode)
        self.command_bar.fitRequested.connect(self.canvas.view.fit_all)
        self.command_bar.actualSizeRequested.connect(self.canvas.view.actual_size)
        self.command_bar.gridRequested.connect(self.canvas.set_grid_visible)
        self.command_bar.snapRequested.connect(self.canvas.set_snap_enabled)
        self.command_bar.labelDisplayRequested.connect(lambda options: self.canvas.set_label_display(**options))
        self.command_bar.autoLabelsRequested.connect(self.canvas.reset_label_positions)
        self.command_bar.validateRequested.connect(self._validate)
        self.command_bar.developerOverlayRequested.connect(self.canvas.set_developer_overlay)
        self.command_bar.confirmSwitchingRequested.connect(self._set_confirm_switching)
        self.command_bar.repeatPlacementRequested.connect(
            self.side_panel.set_repeat_placement
        )
        self.command_bar.rotationPositionRequested.connect(
            lambda angle: self.canvas._set_selected_orientation(
                self._selected_ids, angle
            )
        )
        self.command_bar.autoOrientationRequested.connect(
            lambda: self.canvas._auto_orient_selected(self._selected_ids)
        )
        self.command_bar.scale_combo.currentIndexChanged.connect(self._scale_selected)
        self.side_panel.placementRequested.connect(self.canvas.view.begin_placement)
        self.side_panel.graphicsSelectionRequested.connect(self._tree_graphics_selection)
        self.side_panel.pageSelectionRequested.connect(self._tree_page_selection)
        self.canvas.selectionChanged.connect(self._canvas_selection)
        self.canvas.routeSelectionChanged.connect(self._canvas_route_selection)
        self.canvas.commandCompleted.connect(lambda result: self.refresh())
        self.canvas.errorOccurred.connect(self._show_error)
        self.canvas.statusMessage.connect(self.statusMessage.emit)
        self.canvas.toolStateChanged.connect(self.command_bar.set_tool_state)
        self.canvas.placementFinished.connect(self._placement_finished)
        self.canvas.deleteConfirmationRequested.connect(self._confirm_delete)
        self.canvas.routeDeleteConfirmationRequested.connect(
            self._confirm_delete_route
        )
        self.canvas.propertiesRequested.connect(self._focus_properties)
        self.canvas.view.viewportChanged.connect(lambda state: self.command_bar.set_zoom(state.zoom))
        self.inspector.propertyEdited.connect(self._edit_property)
        self.bottom_panel.objectRequested.connect(self._diagnostic_object_requested)
        self.command_bar.set_tool_state(self.canvas.view.tool_state)
        self.refresh()

    @property
    def scene(self):
        return self.canvas.scene

    @property
    def view(self):
        return self.canvas.view

    def refresh(self) -> None:
        routes = self.canvas.scene.selected_route_ids()
        self.canvas.refresh()
        self.side_panel.refresh(self.controller)
        for route_id in routes:
            item = self.canvas.scene._route_items_by_id.get(route_id)
            if item is not None:
                item.setSelected(True)
        self.command_bar.refresh(self.controller)
        # Saved viewport restoration is intentionally silent; initialize and
        # refresh the indicator from the real view after all bindings exist.
        self.command_bar.set_zoom(self.canvas.view.viewport_state().zoom)
        self._update_inspector()
        self._update_bottom_panel()

    def _canvas_selection(self, ids: object) -> None:
        self._selected_ids = tuple(ids or ())
        self.command_bar.set_selection_count(len(self._selected_ids))
        self.side_panel.select_representations(self._selected_ids)
        self._update_inspector()
        # Выбор не меняет проект. Полная валидация и перестроение списка
        # проблем здесь давали заметную задержку на больших схемах.
        self.bottom_panel.set_selected_count(len(self._selected_ids))
        self.selectionChanged.emit(self._selected_ids)

    def _placement_finished(self, result: object) -> None:
        del result
        if not self.canvas.view.tool_state.is_repeat_placement:
            self.side_panel.library.clearSelection()

    def _canvas_route_selection(self, ids: object) -> None:
        self._selected_route_ids = tuple(ids or ())
        self.side_panel.select_routes(self._selected_route_ids)
        self.bottom_panel.set_selected_count(len(self._selected_ids) + len(self._selected_route_ids))
        self._update_inspector()

    def _focus_properties(self, object_id: object) -> None:
        if isinstance(object_id, GraphicalRepresentationId):
            self._selected_ids = (object_id,)
            self.side_panel.select_representations(self._selected_ids)
            self.canvas.scene.select_representations(self._selected_ids)
            self._update_inspector()
            representation = self.controller.diagram.representations.get(object_id)
            linked_page = (
                representation.extensions.get("linked_page_id")
                if representation is not None
                else None
            )
            if linked_page:
                candidate = PageId(str(linked_page))
                if candidate in self.controller.diagram.pages:
                    self._tree_page_selection(candidate)
        elif isinstance(object_id, DiagramRouteId):
            route = self.controller.diagram.routes.get(object_id)
            if route is not None and route.equipment_id in self.controller.model.line_sections:
                self.canvas.scene.clearSelection()
                item = self.canvas.scene._route_items_by_id.get(object_id)
                if item is not None:
                    item.setSelected(True)
                self._selected_route_ids = (object_id,)
                self._selected_ids = ()
                self._update_inspector()
            candidates = tuple(
                item.id
                for item in self.controller.diagram.representations.values()
                if route is not None
                and route.equipment_id is not None
                and item.equipment_id == route.equipment_id
            )
            if candidates:
                self._selected_ids = (candidates[0],)
                self.side_panel.select_representations(self._selected_ids)
                self.canvas.scene.select_representations(self._selected_ids)
                self._update_inspector()
        self.inspector.setFocus(Qt.FocusReason.OtherFocusReason)
        self.inspector.tree.setFocus(Qt.FocusReason.OtherFocusReason)
        self.statusMessage.emit(ui_text("status.properties_focused"))

    def _tree_selection(self, ids: object) -> None:
        self._tree_graphics_selection(ids, ())

    def _tree_page_selection(self, page_id: PageId) -> None:
        if page_id in self.controller.diagram.pages and page_id != self.canvas.page_id:
            self.canvas.show_page(page_id)
            self.canvas.view.fit_all()

    def _tree_graphics_selection(self, ids: object, route_ids: object) -> None:
        representations = tuple(ids or ())
        routes = tuple(route_ids or ())
        first = next((self.controller.diagram.representations[item] for item in representations
                      if item in self.controller.diagram.representations), None)
        if first is None:
            first = next((self.controller.diagram.routes[item] for item in routes
                          if item in self.controller.diagram.routes), None)
        if first is not None:
            self._tree_page_selection(first.page_id)
        self.canvas.scene.select_representations(representations)
        for route_id in routes:
            item = self.canvas.scene._route_items_by_id.get(route_id)
            if item is not None:
                item.setSelected(True)
        self._update_inspector()
        self.bottom_panel.set_selected_count(len(self._selected_ids) + len(self._selected_route_ids))
        if first is not None:
            item = (self.canvas.scene._items_by_id.get(first.id)
                    or self.canvas.scene._route_items_by_id.get(first.id))
            if item is not None:
                self.canvas.view.ensureVisible(item, 80, 80)

    def _set_mode(self, mode: str) -> None:
        self.canvas.set_mode(mode)
        self.inspector._globally_editable = mode == CanvasMode.EDIT.value
        self.refresh()

    def _set_confirm_switching(self, enabled: bool) -> None:
        try:
            self.controller.set_confirm_switching(enabled)
        except Exception as exc:
            self._show_error(str(exc))

    def _diagnostic_object_requested(self, object_id: object) -> None:
        representation_id = getattr(object_id, "representation_id", None)
        page_id = getattr(object_id, "page_id", None)
        if representation_id in self.controller.diagram.representations:
            representation = self.controller.diagram.representations[representation_id]
            self.canvas.show_page(page_id or representation.page_id)
            self.canvas.scene.select_representations((representation.id,))
            item = self.canvas.scene._items_by_id.get(representation.id)
            if item is not None:
                self.canvas.view.ensureVisible(item, 80, 80)
            return
        token_value = getattr(object_id, "object_id", object_id)
        token = str(getattr(token_value, "value", token_value))
        candidates = []
        for representation in self.controller.diagram.representations.values():
            if token in {
                representation.id.value,
                representation.target_id.value,
            }:
                candidates.append(representation)
                continue
            if representation.equipment_id is not None:
                equipment = self.controller.model.equipment.get(representation.equipment_id)
                if equipment is not None and token in {item.value for item in equipment.port_ids}:
                    candidates.append(representation)
        if not candidates:
            return
        representation = candidates[0]
        self.canvas.show_page(representation.page_id)
        self.canvas.scene.select_representations((representation.id,))
        item = self.canvas.scene._items_by_id.get(representation.id)
        if item is not None:
            self.canvas.view.ensureVisible(item, 80, 80)

    def _scale_selected(self, index: int) -> None:
        value = self.command_bar.scale_combo.itemData(index)
        if value is not None:
            self.canvas.view.set_zoom(float(value))

    def _validate(self) -> None:
        self._update_bottom_panel()
        self.bottom_panel.tabs.setCurrentWidget(self.bottom_panel.problems)
        self.validateRequested.emit()

    def _show_error(self, message: str) -> None:
        self.errorOccurred.emit(message)
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            ui_text("error.title"),
            message,
            parent=self,
        )
        box.addButton(ui_text("action.close"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    def _confirm_delete(self, ids: object) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            ui_text("confirm.delete.title"),
            ui_text("confirm.delete.text"),
            parent=self,
        )
        delete_button = box.addButton(
            ui_text("action.confirm_delete"), QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton(
            ui_text("action.cancel"), QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(cancel_button)
        box.exec()
        if box.clickedButton() is delete_button:
            self.canvas.delete_from_project(ids, confirmed=True)

    def _confirm_delete_route(self, route_id: object) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            ui_text("confirm.delete.title"),
            "Удалить выбранное соединение или физическую линию из проекта?",
            parent=self,
        )
        delete_button = box.addButton(
            ui_text("action.confirm_delete"), QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton(
            ui_text("action.cancel"), QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(cancel_button)
        box.exec()
        if box.clickedButton() is delete_button:
            self.canvas.delete_route(route_id, confirmed=True)

    def _update_bottom_panel(self) -> None:
        project = getattr(self.controller, "_project", None)
        adapter_available = project is not None and hasattr(
            project, "adapter_diagnostics"
        )
        adapter_diagnostics = tuple(
            getattr(project, "adapter_diagnostics", ())
        ) if adapter_available else ()
        issues = self._validation_service.validate(
            self.controller.model,
            self.controller.diagram,
            adapter_diagnostics=adapter_diagnostics,
            active_operating_state_id=(
                self.controller.active_operating_state_id
            ),
        )
        self.bottom_panel.set_problems(issues)
        self.canvas.scene.set_diagnostics(issues)
        self.canvas.scene.set_adapter_diagnostics(
            adapter_diagnostics,
            available=adapter_available,
        )
        diagnostic_rows: dict[str, Any] = {
            "Редакция электрической модели": self.controller.model.revision,
            "Редакция графической модели": self.controller.diagram.revision,
            "Оборудование": len(self.controller.model.equipment),
            "Электрические узлы": len(self.controller.model.electrical_nodes),
            "Соединения": len(self.controller.model.connections),
            "Графические представления": len(self.controller.diagram.representations),
            "Графические трассы": len(self.controller.diagram.routes),
            "Проблемы": len(issues),
            "Ошибки расчётного адаптера": sum(
                str(getattr(item, "severity", "")).lower() == "error"
                for item in adapter_diagnostics
            ) if adapter_available else "данные недоступны",
            "Выбрано": len(self._selected_ids),
        }
        if bool(
            getattr(self.controller.workspace_state, "developer_diagnostics", False)
        ):
            from ..domain.fingerprint import electrical_model_fingerprint

            diagnostic_rows["Электрический отпечаток"] = (
                electrical_model_fingerprint(self.controller.model)
            )
            journal = tuple(getattr(self.controller, "journal", ()))
            if journal:
                latest = journal[-1]
                diagnostic_rows["Отпечаток до операции"] = getattr(
                    latest, "electrical_fingerprint_before", ""
                )
                diagnostic_rows["Отпечаток после операции"] = getattr(
                    latest, "electrical_fingerprint_after", ""
                )
        self.bottom_panel.set_diagnostics(diagnostic_rows)
        self.bottom_panel.set_journal(getattr(self.controller, "journal", ()))

    def _update_inspector(self) -> None:
        fields, title = self._property_fields()
        if self._selected_ids and not title.startswith("Выбрано:"):
            title = "Выбрано: " + title
        editable = str(getattr(getattr(self.controller, "mode", "edit"), "value", getattr(self.controller, "mode", "edit"))) == "edit"
        self.inspector.set_fields(title, fields, editable=editable)

    def _property_fields(self) -> tuple[tuple[PropertyField, ...], str]:
        ids = tuple(item for item in self._selected_ids if item in self.controller.diagram.representations)
        if not ids:
            routes = tuple(self.controller.diagram.routes[item] for item in self._selected_route_ids
                           if item in self.controller.diagram.routes)
            if len(routes) == 1 and routes[0].equipment_id in self.controller.model.line_sections:
                return self._physical_line_fields(routes[0])
            if len(routes) == 1 and routes[0].equipment_id in self.controller.model.equipment:
                route = routes[0]
                equipment = self.controller.model.equipment[route.equipment_id]
                definition = self.controller.model.equipment_type(equipment.type_id, equipment.type_version)
                legacy_fields = self._legacy_line_fields(equipment, definition)
                if legacy_fields:
                    return (
                        PropertyField("equipment.name", "Наименование", equipment.name,
                                      ui_text("group.general"), editable=False, source="Экземпляр"),
                        *legacy_fields,
                        PropertyField("service.equipment_id", "ID ветви", equipment.id.value,
                                      ui_text("group.service"), editable=False),
                        PropertyField("service.route_id", "ID трассы", route.id.value,
                                      ui_text("group.service"), editable=False),
                    ), equipment.name
            return (), ui_text("property.none")
        if len(ids) > 1:
            return (
                PropertyField("selection.count", "Количество объектов", len(ids), ui_text("group.service"), editable=False),
            ), ui_text("property.multiple", count=len(ids))
        representation = self.controller.diagram.representations[ids[0]]
        graphics = _graphics_mapping(representation.extensions)
        fields: list[PropertyField] = []
        equipment = None
        definition = None
        if representation.equipment_id is not None:
            equipment = self.controller.model.equipment.get(representation.equipment_id)
            if equipment is not None:
                definition = self.controller.model.equipment_types.get((equipment.type_id, equipment.type_version))
                fields.extend((
                    PropertyField("equipment.name", property_label("name"), equipment.name, ui_text("group.general"), source="Экземпляр"),
                    PropertyField("equipment.type", property_label("type"), definition.display_name if definition else equipment.type_id.value, ui_text("group.general"), source="Тип оборудования", editable=False),
                    PropertyField("equipment.note", property_label("note"), equipment.note, ui_text("group.general"), source="Экземпляр", editable=False),
                ))
                if definition is not None:
                    fields.extend(self._equipment_voltage_fields(equipment, definition))
                    fields.extend(self._legacy_line_fields(equipment, definition))
                    for property_definition in definition.property_definitions:
                        value = equipment.properties.get(property_definition.key, property_definition.default)
                        fields.append(PropertyField(
                            "equipment.property." + property_definition.key,
                            property_label(property_definition.key),
                            value,
                            ui_text("group.electrical"),
                            unit=unit_label(property_definition.unit),
                            source="Экземпляр" if property_definition.key in equipment.properties else "Тип оборудования",
                            required=property_definition.required,
                        ))
        else:
            node = self.controller.model.electrical_nodes.get(representation.electrical_node_id)
            title = node.name if node and node.name else "Электрический узел"
            fields.append(PropertyField("node.name", property_label("name"), title, ui_text("group.general"), source="Электрическая модель", editable=False))
            if node is not None:
                resolved = endpoint_voltage(self.controller.model, node.id)
                connected = any(row.electrical_node_id == node.id for row in self.controller.model.connections.values())
                fields.append(PropertyField(
                    "node.voltage_class", "Класс сети",
                    resolved.voltage_class_id.value if resolved.valid else None,
                    ui_text("group.electrical"), source="Электрическая модель",
                    required=True, editable=not connected,
                    error="" if resolved.valid else resolved.message,
                    hint="Задайте перед подключением. У подключённого узла изменение запрещено.",
                    choices=self._voltage_choices(),
                ))

        fields.extend((
            PropertyField("graphics.x", property_label("x"), representation.x, ui_text("group.graphics"), unit="ед.", source="Графическое представление"),
            PropertyField("graphics.y", property_label("y"), representation.y, ui_text("group.graphics"), unit="ед.", source="Графическое представление"),
            PropertyField("graphics.rotation_deg", property_label("rotation_deg"), representation.rotation_deg, ui_text("group.graphics"), unit="°", source="Графическое представление"),
            PropertyField(
                "graphics.orientation_mode",
                property_label("orientation_mode"),
                "Автоматически"
                if str(graphics.get("orientation_mode", "manual")) == "auto"
                else "Вручную",
                ui_text("group.graphics"),
                source="Графическое представление",
            ),
            PropertyField("graphics.width", property_label("width"), float(graphics.get("width", 80.0)), ui_text("group.graphics"), unit="ед.", source="Графическое представление"),
            PropertyField("graphics.height", property_label("height"), float(graphics.get("height", 50.0)), ui_text("group.graphics"), unit="ед.", source="Графическое представление"),
            PropertyField("graphics.label", property_label("label"), representation.label, ui_text("group.graphics"), source="Графическое представление"),
            PropertyField("graphics.label_visible", property_label("label_visible"), bool(graphics.get("label_visible", True)), ui_text("group.graphics"), source="Графическое представление"),
            PropertyField("graphics.label_x", property_label("label_offset_x"), float(graphics.get("label_x", 0.0)), ui_text("group.graphics"), unit="ед.", source="Графическое представление"),
            PropertyField("graphics.label_y", property_label("label_offset_y"), float(graphics.get("label_y", -32.0)), ui_text("group.graphics"), unit="ед.", source="Графическое представление"),
            PropertyField("service.representation_id", property_label("representation_id"), representation.id.value, ui_text("group.service"), source="Система", editable=False),
            PropertyField("service.page_id", property_label("page_id"), representation.page_id.value, ui_text("group.service"), source="Система", editable=False),
        ))
        if equipment is not None:
            fields.extend((
                PropertyField("service.equipment_id", property_label("equipment_id"), equipment.id.value, ui_text("group.service"), source="Система", editable=False),
                PropertyField("service.ports", property_label("ports"), tuple(item.value for item in equipment.port_ids), ui_text("group.service"), source="Система", editable=False),
            ))
        title = equipment.name if equipment is not None else (representation.label or "Электрический узел")
        return tuple(fields), title

    def _physical_line_fields(self, route) -> tuple[tuple[PropertyField, ...], str]:
        model = self.controller.model
        equipment = model.equipment[route.equipment_id]
        section = model.line_sections[equipment.id]
        logical = model.logical_lines[section.logical_line_id]
        definition = model.equipment_type(equipment.type_id, equipment.type_version)
        kind_name = {LineKind.OVERHEAD: "Воздушная линия (ВЛ)", LineKind.CABLE: "Кабельная линия (КЛ)",
                     LineKind.BUSDUCT: "Токопровод"}.get(logical.line_kind, logical.line_kind.value)
        fields = [
            PropertyField("equipment.name", "Наименование", equipment.name, ui_text("group.general"), source="Экземпляр"),
            PropertyField("line.kind", "Вид линии", kind_name, ui_text("group.general"), editable=False, source="Логическая линия"),
            PropertyField("line.length_m", "Длина", section.length_mm / 1000 if section.length_mm is not None else None,
                          ui_text("group.electrical"), unit="м", source="Конструктивные участки", required=True,
                          error="Длина не задана. Расстояние на схеме не является физической длиной." if section.length_mm is None else "",
                          hint="Ввод положительного значения явно подтверждает физическую длину. Пустое поле не означает ноль."),
            PropertyField("line.impedance_status", "Параметры", "Подтверждены" if all(
                          segment.impedance_confirmation.value == "confirmed" for segment in section.construction_segments
                          ) else "Не подтверждены",
                          ui_text("group.electrical"), editable=False, source="Электрическая модель",
                          hint="Марка и длина не заменяют проверенные сопротивления. Подтверждённость не назначается автоматически."),
        ]
        fields.extend(self._equipment_voltage_fields(equipment, definition))
        effective = model.effective_equipment_properties(equipment.id)
        for row in definition.property_definitions:
            value = effective.get(row.key, row.default)
            source = ("Ветвь" if row.key in equipment.properties else
                      "Логическая линия" if row.key in logical.inherited_properties else "Тип оборудования")
            overridden = [index for index, segment in enumerate(section.construction_segments, 1)
                          if row.key in segment.properties]
            hint = "Не задано" if value is None else ""
            if overridden:
                hint = "Общее значение; конструктивные участки с отдельным значением: " + ", ".join(map(str, overridden)) + "."
            fields.append(PropertyField("equipment.property." + row.key, property_label(row.key), value,
                ui_text("group.electrical"), unit=unit_label(row.unit),
                source=source,
                required=row.required, editable=row.value_kind not in {"object", "array", "json"},
                hint=hint))
            for index in overridden:
                segment = section.construction_segments[index - 1]
                segment_value = model.effective_line_construction_segment_properties(equipment.id, segment.id)[row.key]
                fields.append(PropertyField("line.segment." + segment.id.value + "." + row.key,
                    f"Участок {index}: {property_label(row.key)}", segment_value,
                    ui_text("group.electrical"), unit=unit_label(row.unit), editable=False,
                    source="Конструктивный участок", hint="Отдельное значение этого конструктивного участка; общее поле его не заменяет."))
        fields.extend((
            PropertyField("service.equipment_id", "ID ветви", equipment.id.value, ui_text("group.service"), editable=False),
            PropertyField("service.route_id", "ID трассы", route.id.value, ui_text("group.service"), editable=False),
            PropertyField("service.ports", "Выводы", tuple(pid.value for pid in equipment.port_ids), ui_text("group.service"), editable=False),
        ))
        return tuple(fields), equipment.name

    def _legacy_line_fields(self, equipment, definition) -> list[PropertyField]:
        if definition.behavior_key != "legacy.line":
            return []
        payload = self.controller.model.effective_equipment_properties(equipment.id).get("legacy_payload")
        if not isinstance(payload, Mapping):
            return []
        kind = {"overhead": "Воздушная линия (ВЛ)", "cable": "Кабельная линия (КЛ)"}.get(payload.get("line_type"), "Вид не определён")
        fields = [PropertyField("legacy.line.kind", "Вид линии", kind, ui_text("group.electrical"),
                                source="Сохранённая физическая линия", editable=False)]
        for key, label, unit in (("brand", "Марка", ""), ("length_km", "Длина", "км"),
                                 ("section_mm2", "Сечение", "мм²"), ("material", "Материал", ""),
                                 ("r0", "R удельное", "Ом/км"), ("x0", "X удельное", "Ом/км")):
            fields.append(PropertyField("legacy.line." + key, label, payload.get(key), ui_text("group.electrical"),
                          unit=unit, source="Сохранённые параметры legacy", editable=False,
                          hint="Исходные расчётные данные сохранены без преобразования в другой тип."))
        return fields

    def _voltage_choices(self, allowed=None) -> tuple[tuple[str, str], ...]:
        values = sorted(self.controller.model.voltage_classes.values(),
                        key=lambda row: (row.system_kind, row.nominal_voltage_v, row.id.value))
        return tuple((row.display_name, row.id.value) for row in values
                     if allowed is None or row.id in allowed)

    def _equipment_voltage_fields(self, equipment, definition) -> list[PropertyField]:
        model = self.controller.model
        groups: dict[str, list[Any]] = {}
        fields = []
        for port_definition in definition.port_definitions:
            if port_definition.voltage_group is not None:
                groups.setdefault(port_definition.voltage_group, []).append(port_definition)
            else:
                # Imported roles stay authoritative; do not invent HV/LV
                # assignments from screen position or symbol orientation.
                port = next((model.ports[pid] for pid in equipment.port_ids
                             if model.ports[pid].role == port_definition.role), None)
                if port is None:
                    continue
                resolved = endpoint_voltage(model, port.id)
                connected = model.connection_for_port(port.id) is not None
                voltage = model.voltage_classes.get(resolved.voltage_class_id)
                fields.append(PropertyField(
                    "equipment.port_voltage." + port_definition.role,
                    "U — " + port_definition.display_name,
                    (voltage.display_name if voltage else "Не определено") if connected else
                    (resolved.voltage_class_id.value if resolved.valid else None),
                    ui_text("group.electrical"), source="Электрическая топология", editable=not connected,
                    choices=() if connected else self._voltage_choices(port_definition.allowed_voltage_class_ids),
                    error="" if resolved.valid else resolved.message,
                    hint="Напряжение порта «" + port_definition.display_name + "».",
                ))
        for group, definitions in groups.items():
            roles = {row.role for row in definitions}
            ports = [pid for pid in equipment.port_ids if model.ports[pid].role in roles]
            connected = any(model.connection_for_port(pid) is not None for pid in ports)
            declared = equipment.voltage_class_by_group.get(group)
            resolved = endpoint_voltage(model, ports[0]) if ports else None
            effective = resolved.voltage_class_id if resolved and resolved.valid else declared
            allowed = None
            for row in definitions:
                if row.allowed_voltage_class_ids is not None:
                    allowed = (set(row.allowed_voltage_class_ids) if allowed is None
                               else allowed & set(row.allowed_voltage_class_ids))
            group_label = {"main": "Класс сети", "hv": "Класс ВН",
                           "mv": "Класс СН", "lv": "Класс НН"}.get(
                               group, "Напряжение — " + ", ".join(row.display_name for row in definitions))
            fields.append(PropertyField(
                "equipment.voltage_class." + group,
                group_label,
                effective.value if effective else None, ui_text("group.electrical"),
                source="Класс сети", required=True,
                editable=not connected, choices=self._voltage_choices(allowed),
                error="" if resolved and resolved.valid else (resolved.message if resolved else "Напряжение не определено."),
                hint="Выводы: " + ", ".join(row.display_name for row in definitions)
                     + ". Задайте класс сети, не паспортное напряжение. Стороны трансформатора задаются отдельно; подключённая группа защищена от изменения.",
            ))
        return fields

    def _edit_property(self, key: str, value: Any) -> None:
        if not self._selected_ids and len(self._selected_route_ids) == 1:
            self._edit_physical_line_property(key, value)
            return
        if len(self._selected_ids) != 1:
            return
        representation = self.controller.diagram.representations.get(self._selected_ids[0])
        if representation is None:
            return
        try:
            if key == "equipment.name" and representation.equipment_id is not None:
                result = self.controller.rename_equipment(representation.equipment_id, str(value))
            elif key.startswith("equipment.voltage_class.") and representation.equipment_id is not None:
                result = self.controller.set_equipment_voltage_class(
                    representation.equipment_id, key.removeprefix("equipment.voltage_class."), VoltageClassId(str(value))
                )
            elif key.startswith("equipment.port_voltage.") and representation.equipment_id is not None:
                port = self.controller.model.port_by_role(representation.equipment_id, key.removeprefix("equipment.port_voltage."))
                result = self.controller.set_legacy_port_voltage_class(port.id, VoltageClassId(str(value)))
            elif key == "node.voltage_class" and representation.electrical_node_id is not None:
                result = self.controller.set_node_voltage_class(representation.electrical_node_id, VoltageClassId(str(value)))
            elif key.startswith("equipment.property.") and representation.equipment_id is not None:
                result = self.controller.set_equipment_property(
                    representation.equipment_id, key.removeprefix("equipment.property."), value
                )
            elif key in {"graphics.x", "graphics.y"}:
                dx = float(value) - representation.x if key.endswith(".x") else 0.0
                dy = float(value) - representation.y if key.endswith(".y") else 0.0
                check = DiagramCollisionService(
                    self.controller.diagram, self.controller.model
                ).check_move((representation.id,), dx, dy)
                if not check.allowed:
                    self.statusMessage.emit(ui_text("status.move_collision"))
                    return
                result = self.controller.move_representations((representation.id,), dx, dy, bypass_snap=True)
            elif key == "graphics.rotation_deg":
                raw_angle = float(value)
                if raw_angle not in (90.0, 180.0):
                    self.statusMessage.emit(ui_text("status.orientation_two_positions"))
                    return
                self.canvas._set_selected_orientation(
                    (representation.id,),
                    int(raw_angle),
                )
                return
            elif key == "graphics.orientation_mode":
                if str(value).strip().casefold().startswith("авто"):
                    self.canvas._auto_orient_selected((representation.id,))
                    return
                else:
                    result = self.controller.rotate_representation(
                        representation.id, representation.rotation_deg
                    )
            elif key in {"graphics.width", "graphics.height"}:
                graphics = _graphics_mapping(representation.extensions)
                width = float(value) if key.endswith(".width") else float(graphics.get("width", 80.0))
                height = float(value) if key.endswith(".height") else float(graphics.get("height", 50.0))
                snapshot = self.canvas.view.collision_snapshot()
                if snapshot is not None:
                    check = snapshot.check_resize(
                        representation.id,
                        width,
                        height,
                    )
                    if not check.allowed:
                        self.statusMessage.emit(check.message)
                        return
                result = self.controller.resize_representation(representation.id, width, height)
            elif key.startswith("graphics.label"):
                # Do not send untouched offsets: that would turn an automatic
                # label into a pinned one merely on rename or visibility edit.
                change = {
                    "graphics.label": ("text", str),
                    "graphics.label_x": ("label_x", float),
                    "graphics.label_y": ("label_y", float),
                    "graphics.label_visible": ("visible", bool),
                }.get(key)
                if change is None:
                    return
                field, convert = change
                result = self.controller.set_label(representation.id, **{field: convert(value)})
            else:
                return
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.canvas.commandCompleted.emit(result)
        self.refresh()

    def _edit_physical_line_property(self, key: str, value: Any) -> None:
        route = self.controller.diagram.routes.get(self._selected_route_ids[0])
        if route is None or route.equipment_id not in self.controller.model.line_sections:
            return
        equipment = self.controller.model.equipment[route.equipment_id]
        try:
            if key == "equipment.name":
                result = self.controller.rename_equipment(equipment.id, str(value))
            elif key == "line.length_m":
                length = float(str(value).replace(" ", "").replace(",", "."))
                if not math.isfinite(length) or length <= 0:
                    raise ValueError("Физическая длина должна быть положительной; неизвестная длина не равна нулю.")
                result = self.controller.confirm_line_length(equipment.id, round(length * 1000))
            elif key.startswith("equipment.property."):
                property_key = key.removeprefix("equipment.property.")
                definition = self.controller.model.equipment_type(equipment.type_id, equipment.type_version)
                row = next(row for row in definition.property_definitions if row.key == property_key)
                if row.value_kind == "number":
                    value = float(str(value).replace(" ", "").replace(",", "."))
                elif row.value_kind == "integer":
                    value = int(str(value).replace(" ", ""))
                result = self.controller.set_line_section_property(equipment.id, property_key, value)
            else:
                return
        except (EditorCommandError, ValueError, TypeError, KeyError) as exc:
            self._show_error(str(exc))
            return
        self.canvas.commandCompleted.emit(result)
        self.refresh()


def _graphics_mapping(extensions: Mapping[str, Any]) -> dict[str, Any]:
    value = extensions.get("stage3_graphics", extensions.get("graphics", {}))
    return thaw_json(value) if isinstance(value, Mapping) else {}


__all__ = [
    "DiagnosticsBottomPanel",
    "EditorCommandBar",
    "EditorWorkspaceWidget",
    "EquipmentLibraryEntry",
    "EquipmentLibraryTree",
    "ProjectEquipmentPanel",
    "PropertyField",
    "PropertyInspectorPanel",
]
