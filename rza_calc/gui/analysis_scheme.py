# -*- coding: utf-8 -*-
"""Однолинейная схема во вкладке «Анализ и расчёты» — только для просмотра.

Вторая система графики удалена: анализ показывает ту же сцену, те же условные
обозначения и ту же раскладку, что и редактор. Отличие одно — здесь ничего
нельзя переместить, создать или удалить, поэтому сцена работает в режиме
``CanvasMode.ANALYSIS`` и не связана с командной историей.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..domain.diagram import DiagramDocument
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
        layout.addWidget(self.view)
        self._vm = None
        self._model = None
        self._document = None
        self._fitted = False
        self._empty = True
        self.scene.representationSelectionChanged.connect(self._selection_changed)
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
        if self._model is None or self._document is None:
            return
        wanted = self._representation_for(
            kind, object_id, self._model, self._document
        )
        if wanted is not None:
            self.scene.select_representations((wanted,), ensure_visible=True)

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
        for representation in document.representations.values():
            if kind in ("branch", "load") and representation.equipment_id is not None:
                equipment = model.equipment.get(representation.equipment_id)
                if equipment is not None and self._legacy_id(equipment) == object_id:
                    return representation.id
            if kind == "node" and representation.electrical_node_id is not None:
                node = model.electrical_nodes.get(representation.electrical_node_id)
                if node is not None and self._legacy_id(node) == object_id:
                    return representation.id
        return None

    def _selection_changed(self, ids) -> None:
        if self._model is None or self._document is None or self._vm is None:
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
        self._document = document
        self._model = model
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
            operating_state_id=operating_state_id,
            topology_state_available=operating_state_id is not None,
        )
        self.scene.set_mode(CanvasMode.ANALYSIS)
        if not self._fitted:
            self.view.fit_all()
            self._fitted = True
