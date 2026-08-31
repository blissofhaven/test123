"""Own Windows window, real gestures, no project-file writes."""
from __future__ import annotations

import faulthandler
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

os.environ["QT_QPA_PLATFORM"] = "windows"
faulthandler.enable(all_threads=True)
ROOT = Path(r"C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening")
OUT = Path(__file__).resolve().parent
DEMO = ROOT / "rza_calc/examples/energoraion.json"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu
from rza_calc.domain.electrical import DataConfirmation, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.connection_tool import ConnectionTargetFeedback, ConnectionTargetKind
from rza_calc.editor.connection_voltage import endpoint_voltage
from rza_calc.editor.line_bridges import build_wire_displays
from rza_calc.gui.main_window import MainWindow
from rza_calc.gui.theme import STYLESHEET
from rza_calc.gui.view_model import ProjectViewModel
from test_ui_direct_connections import _mouse, _state

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
errors, frames, actions = [], [], []
sys.excepthook = lambda kind, value, tb: errors.append("".join(traceback.format_exception(kind, value, tb)))
raw = DEMO.read_bytes()
digest = hashlib.sha256(raw).hexdigest()
window = MainWindow(ProjectViewModel.open(DEMO))
window.setWindowTitle("ПРОВЕРКА — изменения только в памяти, проект не сохраняется")
window.resize(1760, 1000)
window.show()
window.raise_()
window.activateWindow()
assert QTest.qWaitForWindowExposed(window, 5000)
QTest.qWait(100)
workspace = window.editor_workspace
canvas = workspace.canvas
controller = canvas.controller
canvas.set_snap_enabled(False)
canvas.view.actual_size()
workspace._show_error = errors.append
baseline = _state(canvas)
basejournal = len(controller.journal)
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def forbidden_modal(*_args, **_kwargs):
    raise AssertionError("Creation must not open modal type/name/length prompts")


QMenu.exec = forbidden_modal
QInputDialog.getText = forbidden_modal
QInputDialog.getDouble = forbidden_modal


def capture(name, **details):
    app.processEvents()
    assert not errors, errors
    assert DEMO.read_bytes() == raw
    path = OUT / (name + ".png")
    assert window.grab().save(str(path), "PNG")
    frames.append({"phase": name, "png": str(path), "fingerprint": electrical_model_fingerprint(controller.model),
                   "journal": len(controller.journal), **details})


def frame_points(*points, zoom=1.0):
    left, right = min(p.x() for p in points), max(p.x() for p in points)
    top, bottom = min(p.y() for p in points), max(p.y() for p in points)
    viewport = canvas.view.viewport()
    scale = min(zoom, (viewport.width() - 150) / max(right-left, 1),
                (viewport.height() - 150) / max(bottom-top, 1))
    canvas.view.set_zoom(scale)
    canvas.view.centerOn((left+right)/2, (top+bottom)/2)
    canvas.view.viewportChanged.emit(canvas.view.viewport_state())
    canvas.view.setFocus()
    app.processEvents()


def drag(start, end):
    _mouse(canvas, "move", start)
    _mouse(canvas, "press", start)
    _mouse(canvas, "move", (start+end)/2)
    _mouse(canvas, "move", end)


def clean_draft():
    assert not canvas.scene.connection_active
    assert not canvas.scene._preview_item.isVisible()
    assert not canvas.scene._preview_item._vertices
    assert all(item._target_feedback is ConnectionTargetFeedback.NEUTRAL
               for item in (*canvas.scene._items_by_id.values(), *canvas.scene._route_items_by_id.values()))


def inspector_value(key):
    tree = workspace.inspector.tree
    for i in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(i)
        for j in range(group.childCount()):
            item = group.child(j)
            if item.data(0, workspace.inspector.FIELD_ROLE) == key:
                return item
    raise AssertionError(key)


try:
    # Actual legacy CF1_T, with its real from/to port identities and ratings.
    equipment = next(row for row in controller.model.equipment.values()
                     if row.name == "КТП Ф-1 630 кВ·А (Центральная)")
    representation = next(row for row in controller.diagram.representations.values() if row.equipment_id == equipment.id)
    port_id = controller.model.port_by_role(equipment.id, "from").id
    incoming = next(row for row in controller.diagram.routes.values()
                    if port_id in (row.start_anchor.target_port_id, row.end_anchor.target_port_id))
    canvas.delete_route(incoming.id, confirmed=True)
    assert controller.model.connection_for_port(port_id) is None
    assert endpoint_voltage(controller.model, port_id).voltage_class_id == U10
    assert incoming.id not in canvas.scene._route_items_by_id
    canvas.scene.select_representations((representation.id,))
    frame_points(QPointF(representation.x, representation.y), zoom=1.2)
    clean_draft()
    capture("native-cf1-disconnected", port_id=port_id.value, deleted_route=incoming.id.value,
            nominal_kv=10, tail_route_removed=True)
    disconnected = _state(canvas)
    for klass, name in (("builtin.voltage.ac.0_4kv", "0_4"), ("builtin.voltage.ac.110kv", "110")):
        source = canvas.scene._items_by_id[representation.id].port_item(port_id)
        start = QPointF(source.scenePos())
        buses = [item for item in canvas.scene._items_by_id.values() if item._canonical_key == "busbar"
                 and endpoint_voltage(controller.model, controller.diagram.representations[item.representation_id].electrical_node_id).voltage_class_id == VoltageClassId(klass)]
        target = min(buses, key=lambda item: (item.scenePos()-start).manhattanLength())
        end = QPointF(target.scenePos())
        frame_points(start, end)
        drag(start, end)
        assert canvas.scene._connection_target.feedback is ConnectionTargetFeedback.INCOMPATIBLE
        capture("native-cf1-reject-" + name, message=canvas.scene._connection_target.message)
        _mouse(canvas, "release", end)
        clean_draft()
        assert _state(canvas) == disconnected
    source = canvas.scene._items_by_id[representation.id].port_item(port_id)
    start = QPointF(source.scenePos())
    bus = next(item for item in canvas.scene._items_by_id.values() if item._name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ")
    end = QPointF(1800, -410)
    frame_points(start, end)
    drag(start, end)
    target = canvas.scene._connection_target
    assert target.kind is ConnectionTargetKind.BUS and target.feedback is ConnectionTargetFeedback.COMPATIBLE
    preview = tuple((p.x, p.y) for p in canvas.scene._preview_item._vertices)
    capture("native-cf1-connect-10-preview", white_point=[target.x, target.y], bus_axis=bus.pos().y())
    _mouse(canvas, "release", end)
    assert controller.model.connection_for_port(port_id) is not None
    assert controller.model.node_for_port(port_id).id == controller.diagram.representations[bus.representation_id].electrical_node_id
    connected = next(row for row in controller.diagram.routes.values() if row.start_anchor.target_port_id == port_id)
    actions.append({"scenario": "actual-CF1-preview-commit-diagnostic", "preview": preview,
        "commit": tuple((p.x,p.y) for p in connected.waypoints), "bus_fraction_preview": target.anchor_key,
        "bus_fraction_commit": connected.end_anchor.anchor_key})
    assert tuple((p.x, p.y) for p in connected.waypoints) == preview
    clean_draft()
    capture("native-cf1-connect-10-committed", ordinary_wire=True, menu_shown=False,
            route=connected.id.value, preserved_port=port_id.value, preview_matches_commit=True)
    canvas.undo()
    canvas.undo()
    assert _state(canvas) == baseline
    actions.append({"scenario": "CF1_T delete/reconnect", "reject_kv": [.4, 110], "accept_kv": 10,
                    "unchanged_port": port_id.value, "undo_restores_original": True})

    # Same window: drag a real legacy VL by its incident stroke, then undo.
    owner = next(item for item in canvas.scene._items_by_id.values() if item._name == "ВЛ-110 Северная — Южная (резерв)")
    route = next(item for item in canvas.scene._route_items_by_id.values() if canvas.scene._legacy_physical_line_owner(item) is owner)
    points = [QPointF((a.x+b.x)/2, (a.y+b.y)/2) for a, b in zip(route.route.waypoints, route.route.waypoints[1:])
              if abs(a.x-b.x)+abs(a.y-b.y) > 60]
    point = next(point for point in points if canvas.scene.resolve_hit_target(point, canvas.view.transform()).via_route is route)
    frame_points(point, owner.scenePos(), zoom=1.2)
    origin = QPointF(owner.pos())
    drag(point, point+QPointF(0,-60))
    assert canvas.scene.selected_representation_ids() == (owner.representation_id,)
    assert owner.pos() == origin+QPointF(0,-60)
    capture("native-legacy-whole-line-preview", owner=owner.representation_id.value,
            grabbed_route=route.route_id.value, electrical_fingerprint_preserved=electrical_model_fingerprint(controller.model)==baseline[0])
    _mouse(canvas, "release", point+QPointF(0,-60))
    assert canvas.scene.mouseGrabberItem() is None
    assert controller.diagram.representations[owner.representation_id].y == origin.y()-60
    capture("native-legacy-whole-line-committed", same_owner=True, arrow_not_separate=True)
    canvas.undo()
    assert _state(canvas) == baseline
    actions.append({"scenario": "Legacy whole VL by incident stroke", "undo_restores_original": True,
                    "owner": owner.representation_id.value})

    # Draft apparatuses far outside the project, in memory only. Use real palette
    # placement+terminal drag and real inspector item edits for both physical kinds.
    for index, kind in enumerate(("overhead", "cable")):
        first = controller.add_equipment("builtin.circuit_breaker", "Начало " + kind, x=1000, y=2500+index*500,
                                         voltage_class_by_group={"main": U10})
        second = controller.add_equipment("builtin.circuit_breaker", "Конец " + kind, x=1420, y=2500+index*500,
                                          voltage_class_by_group={"main": U10})
        canvas.refresh()
        start = QPointF(canvas.scene._items_by_id[first.representation_id].port_item(first.port_ids[-1]).scenePos())
        end = QPointF(canvas.scene._items_by_id[second.representation_id].port_item(second.port_ids[0]).scenePos())
        frame_points(start, end, zoom=1.4)
        journal = len(controller.journal)
        canvas.view.begin_placement({"target_kind": "physical_line", "type_id": "physical_line."+kind,
                                    "name": "Новая " + ("ВЛ" if kind=="overhead" else "КЛ")})
        drag(start, end)
        assert canvas.scene.physical_line_active
        _mouse(canvas, "release", end)
        assert len(controller.journal) == journal+1
        route_id, = workspace._selected_route_ids
        section_id = controller.diagram.routes[route_id].equipment_id
        section = controller.model.line_sections[section_id]
        assert section.length_mm is None
        fields = {row.key: row for row in workspace._property_fields()[0]}
        assert fields["equipment.property.conductor_mark"].value is None
        assert fields["line.impedance_status"].value == "Не подтверждены"
        capture("native-new-"+kind+"-unknown-inspector", no_modal=True, length_mm=None,
                route=route_id.value, section=section_id.value)
        edit_events = []
        def record_edit(key, value):
            edit_events.append((key, value))
        workspace.inspector.propertyEdited.connect(record_edit)
        inspector_value("equipment.property.conductor_mark").setText(1, "ПРОВЕРОЧНАЯ МАРКА")
        app.processEvents()
        inspector_value("line.length_m").setText(1, "1250,5")
        app.processEvents()
        assert len(controller.journal) == journal+3
        assert [key for key, value in edit_events] == ["equipment.property.conductor_mark", "line.length_m"]
        workspace.inspector.propertyEdited.disconnect(record_edit)
        assert not workspace._selected_ids and workspace._selected_route_ids == (route_id,)
        section = controller.model.line_sections[section_id]
        assert section.length_mm == 1250500
        assert controller.model.effective_equipment_properties(section_id)["conductor_mark"] == "ПРОВЕРОЧНАЯ МАРКА"
        assert all(row.impedance_confirmation is DataConfirmation.UNCONFIRMED for row in section.construction_segments)
        props = controller.model.effective_equipment_properties(section_id)
        assert all(props.get(key) is None for key in ("r1_ohm_per_km", "x1_ohm_per_km"))
        capture("native-new-"+kind+"-entered-inspector", length_mm=1250500,
                length_explicitly_entered=True, impedance_not_invented=True, mark="ПРОВЕРОЧНАЯ МАРКА")
        for _ in range(5):
            canvas.undo()
        assert _state(canvas) == baseline
        actions.append({"scenario": "native " + kind, "creation_commands": 1,
                        "length_and_mark_edit_commands": 2, "no_modal": True, "undo_restores_original": True})

    # Actual wide bus: show distinct white contact positions after root's
    # diagram-only spacing migration. Cross-gap rendering is captured separately.
    bus = next(item for item in canvas.scene._items_by_id.values() if item._name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ")
    canvas.scene.select_representations((bus.representation_id,))
    frame_points(bus.scenePos(), zoom=1.1)
    capture("native-actual-wide-bus-white-contacts", bus=bus.representation_id.value,
            width=bus._width, height=bus._height, selected_frame_uses_band=True)
    clean_draft()
    assert _state(canvas) == baseline
    result = {"pid": os.getpid(), "platform": "windows", "project_saved": False,
              "source_sha256": digest, "electrical_fingerprint": baseline[0],
              "final_model_and_diagram_restored": True, "qt_callback_errors": errors,
              "actions": actions, "frames": frames}
    (OUT/"native-cleanup-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"frames": len(frames), "qt_callback_errors": errors, "result": str(OUT/"native-cleanup-probe.json")}, ensure_ascii=False), flush=True)
except BaseException:
    (OUT/"native-cleanup-failure.json").write_text(json.dumps({"exception": traceback.format_exc(), "errors": errors,
        "frames": frames, "actions": actions, "source_unchanged": DEMO.read_bytes()==raw}, ensure_ascii=False, indent=2), encoding="utf8")
    raise
finally:
    window.hide()
    window.deleteLater()
    app.processEvents()
