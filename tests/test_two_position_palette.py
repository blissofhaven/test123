# -*- coding: utf-8 -*-
"""Two-position editing UI without deleting or migrating electrical nodes."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
)
from rza_calc.domain.electrical import ElectricalModel, VoltageClassId  # noqa: E402
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.editor import EditorMode, ProjectEditorController  # noqa: E402
from rza_calc.gui.editor_panels import (  # noqa: E402
    EditorCommandBar,
    EditorWorkspaceWidget,
    ProjectEquipmentPanel,
    PropertyField,
    PropertyInspectorPanel,
)
from rza_calc.gui.strings import ui_text  # noqa: E402


_APP: QApplication | None = None


@pytest.fixture(autouse=True)
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    yield _APP


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _controller() -> ProjectEditorController:
    page = DiagramPage(PageId("page.two-position-palette"), "Схема")
    return ProjectEditorController(_Project(
        ElectricalModel.with_builtins("Two-position UI"),
        DiagramDocument.create(
            "Two-position UI",
            (page,),
            document_id=DiagramDocumentId("diagram.two-position-palette"),
        ),
        ProjectCatalogSnapshots(),
    ))


def _orientation_field(angle: float, *, editable: bool = True) -> PropertyField:
    return PropertyField(
        "graphics.rotation_deg", "Ориентация", angle, "Графика",
        unit="°", editable=editable,
    )


def _combo(panel: PropertyInspectorPanel) -> QComboBox:
    result = panel.findChild(QComboBox, "orientationPositionCombo")
    assert result is not None
    return result


def test_palette_hides_manual_junction_but_keeps_both_bus_positions() -> None:
    controller = _controller()
    before_model = electrical_model_fingerprint(controller.model)
    before_diagram = controller.diagram
    entries = ProjectEquipmentPanel._library_entries(controller)

    assert all(entry.symbol_key != "connection_point" for entry in entries)
    assert all(entry.title != "Электрический узел" for entry in entries)
    buses = [entry for entry in entries if entry.symbol_key == "busbar"]
    assert len(buses) == 2
    assert {entry.category for entry in buses} == {"Шины"}
    assert {entry.payload()["graphics"]["rotation_deg"] for entry in buses} == {
        90.0, 180.0
    }
    assert all(entry.target_kind == "electrical_node" for entry in buses)
    assert electrical_model_fingerprint(controller.model) == before_model
    assert controller.diagram is before_diagram


def test_removing_palette_entry_does_not_remove_auto_connection_nodes() -> None:
    controller = _controller()
    voltage = {"main": VoltageClassId("builtin.voltage.ac.10kv")}
    first = controller.add_equipment("builtin.load", "Нагрузка 1", x=100, y=100, voltage_class_by_group=voltage)
    second = controller.add_equipment("builtin.load", "Нагрузка 2", x=360, y=100, voltage_class_by_group=voltage)
    first_port = controller.model.equipment[first.equipment_id].port_ids[0]
    second_port = controller.model.equipment[second.equipment_id].port_ids[0]
    joined = controller.connect_ports(first_port, second_port)
    ProjectEquipmentPanel._library_entries(controller)

    assert joined.node_id in controller.model.electrical_nodes
    assert controller.model.connection_for_port(first_port).electrical_node_id == joined.node_id
    assert controller.model.connection_for_port(second_port).electrical_node_id == joined.node_id
    assert joined.route_id in controller.diagram.routes


@pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0, 45.0])
def test_inspector_offers_exactly_two_angles_without_changing_loaded_value(angle) -> None:
    panel = PropertyInspectorPanel()
    emitted = []
    panel.propertyEdited.connect(lambda key, value: emitted.append((key, value)))
    try:
        panel.set_fields("QF1", (_orientation_field(angle),), editable=True)
        combo = _combo(panel)
        assert combo.count() == 2
        assert [combo.itemData(index) for index in range(2)] == [90, 180]
        assert not combo.isEditable()
        assert emitted == []
        if angle in (90.0, 180.0):
            assert combo.currentData() == angle
        else:
            assert combo.currentIndex() == -1
            assert f"{angle:g}°" in combo.placeholderText()
        row = panel.tree.topLevelItem(0).child(0)
        assert not row.flags() & Qt.ItemFlag.ItemIsEditable
    finally:
        panel.close()


def test_inspector_user_choice_emits_only_absolute_position() -> None:
    panel = PropertyInspectorPanel()
    emitted = []
    panel.propertyEdited.connect(lambda key, value: emitted.append((key, value)))
    try:
        panel.set_fields("QF1", (_orientation_field(0.0),), editable=True)
        combo = _combo(panel)
        combo.setCurrentIndex(0)
        combo.setCurrentIndex(1)
        assert emitted == [
            ("graphics.rotation_deg", 90),
            ("graphics.rotation_deg", 180),
        ]
    finally:
        panel.close()


@pytest.mark.parametrize("global_editable, field_editable", [(False, True), (True, False)])
def test_inspector_locked_orientation_cannot_emit_edits(global_editable, field_editable) -> None:
    panel = PropertyInspectorPanel()
    emitted = []
    panel.propertyEdited.connect(lambda key, value: emitted.append((key, value)))
    try:
        panel.set_fields(
            "QF1", (_orientation_field(90.0, editable=field_editable),),
            editable=global_editable,
        )
        combo = _combo(panel)
        assert not combo.isEnabled()
        combo.setCurrentIndex(1)
        assert emitted == []
    finally:
        panel.close()


def test_toolbar_has_two_absolute_actions_and_no_left_right_entries() -> None:
    toolbar = EditorCommandBar()
    controller = _controller()
    emitted = []
    toolbar.rotationPositionRequested.connect(emitted.append)
    try:
        toolbar.refresh(controller)
        toolbar.set_selection_count(1)
        toolbar.vertical_orientation_action.trigger()
        toolbar.horizontal_orientation_action.trigger()
        assert emitted == [90, 180]
        assert toolbar.rotate_right_action is toolbar.vertical_orientation_action
        assert toolbar.rotate_left_action is toolbar.horizontal_orientation_action
        texts = [action.text() for action in toolbar.actions()]
        assert texts.count(ui_text("action.orientation_vertical")) == 1
        assert texts.count(ui_text("action.orientation_horizontal")) == 1
        assert ui_text("action.rotate_left") not in texts
        assert ui_text("action.rotate_right") not in texts

        toolbar.set_selection_count(0)
        assert not toolbar.vertical_orientation_action.isEnabled()
        assert not toolbar.horizontal_orientation_action.isEnabled()
        toolbar.set_selection_count(2)
        assert not toolbar.vertical_orientation_action.isEnabled()
        controller.set_mode(EditorMode.ANALYSIS)
        toolbar.set_selection_count(1)
        toolbar.refresh(controller)
        assert not toolbar.vertical_orientation_action.isEnabled()
        assert not toolbar.horizontal_orientation_action.isEnabled()
    finally:
        toolbar.close()


@pytest.mark.parametrize("angle", [0.0, 270.0])
def test_workspace_refresh_preserves_imported_angle_and_model(angle) -> None:
    controller = _controller()
    added = controller.add_equipment("builtin.circuit_breaker", "QF1", x=100, y=100)
    controller.rotate_representation(added.representation_id, angle)
    before_model = electrical_model_fingerprint(controller.model)
    before_diagram = controller.diagram
    before_journal = len(controller.journal)
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    try:
        workspace.canvas.scene.select_representations((added.representation_id,))
        workspace.refresh()
        assert controller.diagram.representations[added.representation_id].rotation_deg == angle
        assert controller.diagram is before_diagram
        assert len(controller.journal) == before_journal
        assert electrical_model_fingerprint(controller.model) == before_model
        assert _combo(workspace.inspector).currentIndex() == -1
    finally:
        workspace.close()


@pytest.mark.parametrize("angle", [0.0, 270.0, 45.0, 360.0])
def test_workspace_rejects_new_inspector_angles_outside_two_positions(angle) -> None:
    controller = _controller()
    added = controller.add_equipment("builtin.circuit_breaker", "QF1", x=100, y=100)
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    status = []
    workspace.statusMessage.connect(status.append)
    try:
        workspace.canvas.scene.select_representations((added.representation_id,))
        before_diagram = controller.diagram
        before_journal = len(controller.journal)
        workspace._edit_property("graphics.rotation_deg", angle)
        assert controller.diagram is before_diagram
        assert len(controller.journal) == before_journal
        assert ui_text("status.orientation_two_positions") in status
    finally:
        workspace.close()


def test_workspace_toolbar_and_inspector_apply_absolute_positions() -> None:
    controller = _controller()
    added = controller.add_equipment("builtin.circuit_breaker", "QF1", x=100, y=100)
    workspace = EditorWorkspaceWidget(controller, confirm_deletions=False)
    before_model = electrical_model_fingerprint(controller.model)
    try:
        workspace.canvas.scene.select_representations((added.representation_id,))
        workspace.command_bar.horizontal_orientation_action.trigger()
        assert controller.diagram.representations[added.representation_id].rotation_deg == 180.0
        workspace.command_bar.vertical_orientation_action.trigger()
        assert controller.diagram.representations[added.representation_id].rotation_deg == 90.0
        workspace._edit_property("graphics.rotation_deg", 180)
        assert controller.diagram.representations[added.representation_id].rotation_deg == 180.0
        assert electrical_model_fingerprint(controller.model) == before_model
    finally:
        workspace.close()
