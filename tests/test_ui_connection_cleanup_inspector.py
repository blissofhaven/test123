"""Physical line drafts retain empty data after the explicit parameter choice."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from rza_calc.domain.electrical import DataConfirmation, LineKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import PhysicalLineInput, PortTarget, ProjectEditorController
from rza_calc.gui.editor_panels import EditorWorkspaceWidget, PropertyField, PropertyInspectorPanel
from rza_calc.gui.editor_scene import CanvasMode
from rza_calc.io.project import load_project
from test_ui_direct_connections import _controller, _mouse, U10
from line_dialog_driver import choose_draft_parameters

_APP = None


@pytest.fixture
def workspace_factory(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    errors, widgets = [], []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb:
                        errors.append("".join(traceback.format_exception(kind, value, tb))))

    def create(controller):
        widget = EditorWorkspaceWidget(controller, confirm_deletions=False)
        monkeypatch.setattr(widget, "_show_error", errors.append)
        widgets.append(widget)
        widget.resize(1540, 900)
        widget.show()
        widget.canvas.view.actual_size()
        widget.canvas.set_snap_enabled(False)
        widget.canvas.view.centerOn(120, 0)
        QApplication.processEvents()
        return widget

    yield create
    for widget in widgets:
        widget.close()
    QApplication.processEvents()
    assert not errors, "\n".join(errors)


def _create_native(workspace_factory, monkeypatch, kind=LineKind.OVERHEAD, gesture="click"):
    controller = _controller()
    first = controller.add_equipment("builtin.circuit_breaker", "Начало", x=0, y=0,
                                     voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "Конец", x=240, y=0,
                                      voltage_class_by_group={"main": U10})
    workspace = workspace_factory(controller)
    canvas = workspace.canvas
    first_port = canvas.scene._items_by_id[first.representation_id].port_item(first.port_ids[-1])
    second_port = canvas.scene._items_by_id[second.representation_id].port_item(second.port_ids[0])
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: pytest.fail("No modal name prompt"))
    monkeypatch.setattr(QInputDialog, "getDouble", lambda *_a, **_k: pytest.fail("No modal length prompt"))
    journal = len(controller.journal)
    canvas.view.begin_placement({"target_kind": "physical_line", "type_id": "physical_line." + kind.value, "name": "Проверяемая линия"})
    _mouse(canvas, "click" if gesture == "click" else "press", first_port.scenePos())
    assert canvas.scene.physical_line_active
    _mouse(canvas, "move", second_port.scenePos())
    with choose_draft_parameters(canvas):
        _mouse(canvas, "click" if gesture == "click" else "release", second_port.scenePos())
    assert len(controller.journal) == journal + 1
    section = next(iter(controller.model.line_sections.values()))
    route = next(row for row in controller.diagram.routes.values() if row.equipment_id == section.equipment_id)
    return workspace, section, route


@pytest.mark.parametrize("kind", (LineKind.OVERHEAD, LineKind.CABLE))
@pytest.mark.parametrize("gesture", ("click", "drag"))
def test_native_line_without_fake_parameters_opens_right_inspector(workspace_factory, monkeypatch, kind, gesture):
    workspace, section, route = _create_native(workspace_factory, monkeypatch, kind, gesture)
    controller = workspace.controller
    assert workspace._selected_route_ids == (route.id,)
    assert workspace.canvas.scene.selected_route_ids() == (route.id,)
    fields = {row.key: row for row in workspace._property_fields()[0]}
    assert fields["line.length_m"].value is None and "не задана" in fields["line.length_m"].error
    assert fields["equipment.property.conductor_mark"].value is None
    assert fields["line.impedance_status"].value == "Не подтверждены"
    assert fields["equipment.name"].value == "Проверяемая линия"
    assert section.length_mm is None
    assert section.construction_segments[0].length_confirmation is DataConfirmation.UNCONFIRMED
    assert section.construction_segments[0].impedance_confirmation is DataConfirmation.UNCONFIRMED
    assert controller.model.logical_lines[section.logical_line_id].line_kind is kind
    assert len(controller.diagram.representations) == 2  # no fake line symbol


def test_line_inspector_length_and_mark_edit_preserve_ids_geometry_and_undo(workspace_factory, monkeypatch):
    workspace, section, route = _create_native(workspace_factory, monkeypatch)
    controller = workspace.controller
    fingerprint = electrical_model_fingerprint(controller.model)
    topology = controller.model.connectivity_signature()
    before_routes = dict(controller.diagram.routes)
    before_reps = dict(controller.diagram.representations)
    ports = controller.model.equipment[section.equipment_id].port_ids
    journal = len(controller.journal)
    workspace.quick_editor.editors["length_mm"].value_edit.setText("1250,5")
    assert workspace.quick_editor.apply_changes()
    assert controller.model.line_sections[section.equipment_id].length_mm == 1_250_500
    workspace.quick_editor.editors["conductor_mark"].value_edit.setText("АПвПу 1×120")
    assert workspace.quick_editor.apply_changes()
    assert controller.model.equipment[section.equipment_id].properties["conductor_mark"] == "АПвПу 1×120"
    assert controller.model.connectivity_signature() == topology
    assert controller.model.equipment[section.equipment_id].port_ids == ports
    assert dict(controller.diagram.routes) == before_routes
    assert dict(controller.diagram.representations) == before_reps
    assert workspace.canvas.scene.selected_route_ids() == (route.id,)
    assert len(controller.journal) == journal + 2
    for _ in range(2):
        workspace.canvas.undo()
    assert electrical_model_fingerprint(controller.model) == fingerprint


@pytest.mark.parametrize("value", ("0", "-1", "nan", "не задано"))
def test_invalid_physical_length_is_not_silently_converted(workspace_factory, monkeypatch, value):
    workspace, section, route = _create_native(workspace_factory, monkeypatch)
    before = electrical_model_fingerprint(workspace.controller.model)
    journal = len(workspace.controller.journal)
    workspace.quick_editor.editors["length_mm"].value_edit.setText(value)
    assert not workspace.quick_editor.apply_changes()
    assert workspace.quick_editor.message.text()
    assert workspace.controller.model.line_sections[section.equipment_id].length_mm is None
    assert len(workspace.controller.journal) == journal
    assert electrical_model_fingerprint(workspace.controller.model) == before


def test_native_route_inspector_is_read_only_in_analysis(workspace_factory, monkeypatch):
    workspace, section, route = _create_native(workspace_factory, monkeypatch)
    workspace.canvas.set_mode(CanvasMode.ANALYSIS)
    workspace.refresh()
    assert not workspace.inspector._globally_editable
    before = electrical_model_fingerprint(workspace.controller.model)
    assert not workspace.quick_editor.editors["length_mm"].isEnabled()
    assert not workspace.quick_editor.apply_changes()
    assert electrical_model_fingerprint(workspace.controller.model) == before


def test_native_inspector_reads_inherited_parameters_and_reports_override_source(workspace_factory):
    controller = _controller()
    first = controller.add_equipment("builtin.circuit_breaker", "Начало", x=0, y=0,
                                     voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "Конец", x=240, y=0,
                                      voltage_class_by_group={"main": U10})
    result = controller.create_physical_line("Линия с исходными данными", LineKind.OVERHEAD,
        PortTarget(first.port_ids[-1], first.representation_id),
        PortTarget(second.port_ids[0], second.representation_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
        inherited_properties={"conductor_mark": "INHERITED", "r1_ohm_per_km": .4, "x1_ohm_per_km": .3})
    workspace = workspace_factory(controller)
    workspace._focus_properties(result.route_id)
    before = electrical_model_fingerprint(controller.model)
    journal = len(controller.journal)
    fields = {row.key: row for row in workspace._property_fields()[0]}
    for key, expected in (("conductor_mark", "INHERITED"), ("r1_ohm_per_km", .4), ("x1_ohm_per_km", .3)):
        assert fields["equipment.property." + key].value == expected
        assert fields["equipment.property." + key].source == "Логическая линия"
    assert electrical_model_fingerprint(controller.model) == before and len(controller.journal) == journal
    workspace.quick_editor.editors["conductor_mark"].value_edit.setText("LOCAL")
    assert workspace.quick_editor.apply_changes()
    fields = {row.key: row for row in workspace._property_fields()[0]}
    assert fields["equipment.property.conductor_mark"].value == "LOCAL"
    assert fields["equipment.property.conductor_mark"].source == "Ветвь"
    workspace.canvas.undo()
    assert electrical_model_fingerprint(controller.model) == before


def test_actual_legacy_lines_show_physical_kind_and_payload_without_conversion(workspace_factory):
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/energoraion.json"
    raw = path.read_bytes()
    workspace = workspace_factory(ProjectEditorController(load_project(path)))
    controller = workspace.controller
    before = electrical_model_fingerprint(controller.model)
    counts = {"overhead": 0, "cable": 0}
    for representation in controller.diagram.representations.values():
        equipment = controller.model.equipment.get(representation.equipment_id)
        if equipment is None:
            continue
        definition = controller.model.equipment_type(equipment.type_id, equipment.type_version)
        if definition.behavior_key != "legacy.line":
            continue
        payload = controller.model.effective_equipment_properties(equipment.id)["legacy_payload"]
        workspace.canvas.scene.select_representations((representation.id,))
        fields = {row.key: row for row in workspace._property_fields()[0]}
        counts[payload["line_type"]] += 1
        assert fields["legacy.line.kind"].value == ("Воздушная линия (ВЛ)" if payload["line_type"] == "overhead" else "Кабельная линия (КЛ)")
        assert fields["legacy.line.length_km"].value == payload["length_km"]
        assert fields["legacy.line.brand"].value == payload["brand"]
        assert workspace.canvas.scene._items_by_id[representation.id].direction_marker() is not None
    assert counts == {"overhead": 21, "cable": 10}
    assert electrical_model_fingerprint(controller.model) == before and path.read_bytes() == raw


def test_inspector_decoration_does_not_emit_a_second_property_edit(workspace_factory):
    workspace = workspace_factory(_controller())
    inspector = workspace.inspector
    inspector.set_fields("Линия", (PropertyField("equipment.property.conductor_mark", "Марка", None, "Данные"),), editable=True)
    events = []
    inspector.propertyEdited.connect(lambda key, value: events.append((key, value)))
    inspector.tree.topLevelItem(0).child(0).setText(1, "REAL")
    assert events == [("equipment.property.conductor_mark", "REAL")]


def test_legacy_stroke_drag_then_native_inspector_edit_in_same_window_keeps_owner(workspace_factory):
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_projects/energoraion.json"
    raw = path.read_bytes()
    workspace = workspace_factory(ProjectEditorController(load_project(path)))
    canvas, controller = workspace.canvas, workspace.controller
    owner = next(item for item in canvas.scene._items_by_id.values()
                 if item._name == "ВЛ-110 Северная — Южная (резерв)")
    old_equipment = controller.model.equipment[owner.representation.equipment_id]
    route = next(item for item in canvas.scene._route_items_by_id.values()
                 if canvas.scene._legacy_physical_line_owner(item) is owner)
    candidates = [QPointF((a.x+b.x)/2, (a.y+b.y)/2)
                  for a, b in zip(route.route.waypoints, route.route.waypoints[1:])
                  if abs(a.x-b.x)+abs(a.y-b.y) > 60]
    point = next(point for point in candidates
                 if canvas.scene.resolve_hit_target(point, canvas.view.transform()).via_route is route)
    canvas.view.centerOn(point)
    before_drag = controller.diagram
    drag_history = len(controller.journal)
    _mouse(canvas, "press", point)
    assert canvas.view.hasFocus()
    assert canvas.scene._start_positions
    _mouse(canvas, "move", point+QPointF(0,-60))
    _mouse(canvas, "release", point+QPointF(0,-60))
    assert canvas.scene.mouseGrabberItem() is None
    assert len(controller.journal) == drag_history + 1
    assert controller.diagram != before_drag
    canvas.undo()
    assert controller.diagram == before_drag
    first = controller.add_equipment("builtin.circuit_breaker", "Новый вход", x=1000, y=2500,
                                     voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "Новый выход", x=1420, y=2500,
                                      voltage_class_by_group={"main": U10})
    result = controller.create_physical_line("Новая ВЛ", LineKind.OVERHEAD,
        PortTarget(first.port_ids[-1], first.representation_id),
        PortTarget(second.port_ids[0], second.representation_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
    canvas.refresh()
    workspace._focus_properties(result.route_id)
    before = electrical_model_fingerprint(controller.model)
    journal = len(controller.journal)
    events = []
    workspace.quick_editor.commandApplied.connect(events.append)
    workspace.quick_editor.editors["conductor_mark"].value_edit.setText("NEW LINE ONLY")
    assert workspace.quick_editor.apply_changes()
    QApplication.processEvents()
    assert len(events) == 1
    assert {change.key for change in events[0].changes} == {"conductor_mark"}
    assert len(controller.journal) == journal+1
    assert not workspace._selected_ids and workspace._selected_route_ids == (result.route_id,)
    assert canvas.scene.selected_route_ids() == (result.route_id,)
    assert controller.model.equipment[old_equipment.id] == old_equipment
    assert controller.model.equipment[result.section_id].properties["conductor_mark"] == "NEW LINE ONLY"
    canvas.undo()
    assert electrical_model_fingerprint(controller.model) == before and path.read_bytes() == raw
