# -*- coding: utf-8 -*-
"""Однолинейная схема во вкладке «Анализ и расчёты» — только для просмотра.

Вторая система графики удалена: анализ показывает ту же сцену, те же условные
обозначения и ту же раскладку, что и редактор. Отличие одно — здесь ничего
нельзя переместить, создать или удалить, поэтому сцена работает в режиме
``CanvasMode.ANALYSIS`` и не связана с командной историей.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..domain.diagram import DiagramDocument, PageId
from ..domain.electrical import ElectricalModel, OperatingStateId
from .editor_scene import CanvasMode, DiagramGraphicsScene, DiagramGraphicsView
from .theme import DiagramColorMode


class AnalysisSchemeView(QWidget):
    """Просмотровая обёртка над сценой редактора."""

    selectionRequested = Signal(str, str)

    def __init__(self, vm, parent: QWidget | None = None):
        super().__init__(parent)
        self.scene = DiagramGraphicsScene(self)
        self.scene.set_mode(CanvasMode.ANALYSIS)
        self.view = DiagramGraphicsView(self.scene, self)
        self.view.setAcceptDrops(False)
        self.view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.page_selector = QComboBox()
        self.page_selector.setAccessibleName("Лист схемы")
        page_bar = QHBoxLayout()
        page_bar.addWidget(QLabel("Лист:"))
        page_bar.addWidget(self.page_selector, 1)
        layout.addLayout(page_bar)
        layout.addWidget(self.view)
        self._vm = None
        self._model = None
        self._document = None
        self._fitted = False
        self._empty = True
        self._workspace_page = None
        self._setting_selection = False
        self.scene.representationSelectionChanged.connect(self._selection_changed)
        self.scene.routeSelectionChanged.connect(self._route_selection_changed)
        self.page_selector.currentIndexChanged.connect(self._page_selected)
        self.refresh(vm)

    # ── Совместимость с прежним API панели схемы ─────────────────────────
    def zoom_in(self) -> None:
        self.view.zoom_in()

    def zoom_out(self) -> None:
        self.view.zoom_out()

    def reset_view(self) -> None:
        self.view.fit_all()

    def actual_size(self) -> None:
        self.view.actual_size()

    def set_color_mode(self, mode: DiagramColorMode | str) -> None:
        """Switch color rendering without touching the project models."""

        self.scene.set_color_mode(mode)

    def set_selection(self, kind: str, object_id: str) -> None:
        """Подсветить объект расчёта, если у него есть представление на схеме."""
        self._setting_selection = True
        try:
            self._set_selection(kind, object_id)
        finally:
            self._setting_selection = False

    def _set_selection(self, kind: str, object_id: str) -> None:
        if self._model is None or self._document is None:
            return
        wanted = self._representation_for(
            kind, object_id, self._model, self._document
        )
        routes = sorted((route for route in self._document.routes.values()
                         if kind == "branch" and route.equipment_id is not None
                         and self._matches_equipment(route.equipment_id, object_id)),
                        key=lambda row: (row.page_id != self.scene.page_id,
                                         self._document.pages[row.page_id].order, row.id.value))
        current_route = bool(routes and routes[0].page_id == self.scene.page_id)
        if wanted is not None and (not current_route or self._document.representations[wanted].page_id == self.scene.page_id):
            representation = self._document.representations[wanted]
            self.show_page(representation.page_id)
            self.scene.select_representations((wanted,), ensure_visible=True)
            item = self.scene._items_by_id.get(wanted)
            if item is not None:
                self.view.ensureVisible(item, 80, 80)
            return
        if routes:
            route = routes[0]
            self.show_page(route.page_id)
            self.scene.clearSelection()
            item = self.scene._route_items_by_id.get(route.id)
            if item is not None:
                item.setSelected(True)
                self.view.ensureVisible(item, 80, 80)

    def _matches_equipment(self, equipment_id, object_id: str) -> bool:
        equipment = self._model.equipment.get(equipment_id)
        return equipment is not None and object_id in {equipment.id.value, self._legacy_id(equipment)}

    def _page_selected(self, index: int) -> None:
        value = self.page_selector.itemData(index)
        if value:
            self.show_page(PageId(str(value)))

    def show_page(self, page_id: PageId) -> None:
        """Navigate the shared diagram without changing electrical data."""
        if self._document is None or page_id not in self._document.pages:
            return
        if page_id != self.scene.page_id:
            state = self._canonical_operating_state_id(str(getattr(self._vm, "mode_id", "")), self._model)
            self.scene.sync_document(self._document, self._model, page_id=page_id,
                                     operating_state_id=state, topology_state_available=state is not None)
            self.scene.set_mode(CanvasMode.ANALYSIS)
            self.view.fit_all()
        self.page_selector.blockSignals(True)
        self.page_selector.setCurrentIndex(self.page_selector.findData(page_id.value))
        self.page_selector.blockSignals(False)

    @property
    def is_empty(self) -> bool:
        """Схема ещё не размещена: у проекта нет ни одного представления."""
        return self._empty

    # ── Внутреннее ────────────────────────────────────────────────────────
    @staticmethod
    def _legacy_id(item) -> str:
        extensions = getattr(item, "extensions", None) or {}
        legacy = extensions.get("legacy_calculation") or {}
        return str(legacy.get("legacy_id") or "")

    @classmethod
    def _canonical_operating_state_id(
        cls,
        mode_id: str,
        model: ElectricalModel,
    ) -> OperatingStateId | None:
        """Map the calculation mode only when its canonical state is known."""

        for state in model.operating_states.values():
            if state.id.value == mode_id or cls._legacy_id(state) == mode_id:
                return state.id
        return None

    def _representation_for(self, kind: str, object_id: str, model, document):
        # Repeated representations of one electrical ID are valid; keep the
        # current page when it already contains the requested object.
        candidates = sorted(document.representations.values(),
                            key=lambda row: (row.page_id != self.scene.page_id,
                                             document.pages[row.page_id].order, row.id.value))
        for representation in candidates:
            if kind in ("branch", "load") and representation.equipment_id is not None:
                equipment = model.equipment.get(representation.equipment_id)
                if equipment is not None and object_id in {equipment.id.value, self._legacy_id(equipment)}:
                    return representation.id
            if kind == "node" and representation.electrical_node_id is not None:
                node = model.electrical_nodes.get(representation.electrical_node_id)
                if node is not None and object_id in {node.id.value, self._legacy_id(node)}:
                    return representation.id
        return None

    def _route_selection_changed(self, ids) -> None:
        if self._setting_selection or self._model is None or self._document is None:
            return
        for route_id in ids:
            route = self._document.routes.get(route_id)
            equipment = self._model.equipment.get(route.equipment_id) if route is not None else None
            legacy = self._legacy_id(equipment) if equipment is not None else ""
            if legacy:
                self.selectionRequested.emit("branch", legacy)
                return

    def _selection_changed(self, ids) -> None:
        if self._setting_selection or self._model is None or self._document is None or self._vm is None:
            return
        loads = getattr(self._vm.net, "loads", {})
        for representation_id in tuple(ids):
            representation = self._document.representations.get(representation_id)
            if representation is None:
                continue
            if representation.equipment_id is not None:
                equipment = self._model.equipment.get(representation.equipment_id)
                legacy = self._legacy_id(equipment) if equipment is not None else ""
                if legacy:
                    self.selectionRequested.emit(
                        "load" if legacy in loads else "branch", legacy
                    )
                    return
            elif representation.electrical_node_id is not None:
                node = self._model.electrical_nodes.get(
                    representation.electrical_node_id
                )
                legacy = self._legacy_id(node) if node is not None else ""
                if legacy:
                    self.selectionRequested.emit("node", legacy)
                    return

    def refresh(self, vm) -> None:
        self._vm = vm
        project = getattr(vm, "project", None)
        document = getattr(project, "diagram", None)
        model = getattr(project, "electrical_model", None)
        if not isinstance(document, DiagramDocument) or not isinstance(
            model, ElectricalModel
        ):
            self._empty = True
            return
        old_document = self._document
        self._document = document
        self._model = model
        workspace = document.extensions.get("stage3_workspace") or {}
        active_page = workspace.get("active_page_id")
        previous_page = self.scene.page_id
        page_id = previous_page
        if old_document is None or old_document.id != document.id or active_page != self._workspace_page:
            candidate = PageId(str(active_page)) if active_page else None
            page_id = candidate if candidate in document.pages else None
        if page_id not in document.pages:
            page_id = next((page.id for page in sorted(document.pages.values(),
                           key=lambda row: (row.order, row.id.value))), None)
        self._workspace_page = active_page
        self.page_selector.blockSignals(True)
        self.page_selector.clear()
        for page in sorted(document.pages.values(), key=lambda row: (row.order, row.id.value)):
            self.page_selector.addItem(page.name, page.id.value)
        self.page_selector.setCurrentIndex(self.page_selector.findData(page_id.value) if page_id else -1)
        self.page_selector.blockSignals(False)
        self._empty = not document.representations
        if self._empty:
            return
        operating_state_id = self._canonical_operating_state_id(
            str(getattr(vm, "mode_id", "")),
            model,
        )
        self.scene.sync_document(
            document,
            model,
            page_id=page_id,
            operating_state_id=operating_state_id,
            topology_state_available=operating_state_id is not None,
        )
        self.scene.set_mode(CanvasMode.ANALYSIS)
        if not self._fitted or page_id != previous_page:
            self.view.fit_all()
            self._fitted = True
