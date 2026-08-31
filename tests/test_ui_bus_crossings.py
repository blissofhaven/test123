"""Bus dragging must not turn accidental same-net crossings into local joints."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import ProjectEditorController
from rza_calc.editor.collision import DiagramCollisionService
from rza_calc.editor.line_bridges import BridgeWire, build_wire_displays
from rza_calc.gui import editor_scene
from rza_calc.io.project import load_project


ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "rza_calc/examples/energoraion.json"
_APP = None


@pytest.mark.parametrize("reverse", (False, True))
def test_shared_remote_bus_node_does_not_make_an_interior_crossing_a_junction(reverse):
    points = (((0, 0), (100, 0)), ((50, -50), (50, 50)))
    wires = tuple(BridgeWire(key, row[::-1] if reverse else row, "bus-node",
                             ("bus-node", "bus-node"))
                  for key, row in zip(("a", "b"), points))
    displays = build_wire_displays(wires)
    assert not any(display.junctions for display in displays.values())
    assert len(displays["a"].bridges) == 1
    assert displays["a"].bridges[0].center == (50, 0)
    assert displays["b"].gaps
    assert displays == build_wire_displays(wires[::-1])
    assert all(displays[wire.key].wire is wire for wire in wires)


@pytest.mark.parametrize("is_bus", (False, True))
@pytest.mark.parametrize("same_node", (False, True))
def test_real_endpoint_t_and_bus_attachment_require_the_same_node(is_bus, same_node):
    displays = build_wire_displays((
        BridgeWire("a", ((0, 0), (100, 0)), "node-a", bridge_allowed=not is_bus),
        BridgeWire("b", ((50, 0), (50, 60)), "node-a" if same_node else "node-b"),
    ))
    assert bool(displays["a"].junctions) is same_node
    if same_node:
        assert displays["a"].junctions == ((50, 0),)
        assert not any(display.bridges or display.gaps for display in displays.values())
    else:
        assert not any(display.junctions for display in displays.values())


def _electrical_identity(controller):
    model = controller.model
    return (
        electrical_model_fingerprint(model), model.revision,
        model.connectivity_signature(), dict(model.equipment),
        dict(model.electrical_nodes), dict(model.connections), dict(model.ports),
        frozenset(controller.diagram.representations), frozenset(controller.diagram.routes),
        {route.id: (route.start_anchor, route.end_anchor)
         for route in controller.diagram.routes.values()},
    )


def _mouse(canvas, kind, point):
    screen = canvas.view.mapFromScene(point)
    if kind == "move":
        QTest.mouseMove(canvas.view.viewport(), screen, delay=1)
    else:
        {"press": QTest.mousePress, "release": QTest.mouseRelease}[kind](
            canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=screen)
    QApplication.processEvents()


def _interior_route_junctions(displays):
    routes = [display for display in displays.values() if display.wire.key.startswith("route:")]
    invalid = []
    for display in routes:
        wire = display.wire
        for point in display.junctions:
            # A real endpoint supplies local evidence. Sharing only the remote
            # bus's net ID is insufficient for drawing a new junction here.
            local_endpoints = [other.wire for other in routes
                               if other.wire.node_id == wire.node_id
                               and point in (other.wire.points[0], other.wire.points[-1])]
            if not local_endpoints:
                invalid.append((wire.key, point))
    return invalid


def test_real_bus_out_and_back_preview_commit_and_undo_preserve_electrical_identity(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    source_hash = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    controller = ProjectEditorController(load_project(DEMO))
    canvas = editor_scene.EditorCanvas(controller)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb:
                        errors.append("".join(traceback.format_exception(kind, value, tb))))
    assert len(canvas.scene._items_by_id) == 157
    assert len(canvas.scene._route_items_by_id) == 160
    electrical_before = _electrical_identity(controller)
    document_before = controller.diagram
    journal_before = len(controller.journal)
    captured = {}
    original_planner = editor_scene.build_wire_displays
    def capture(wires):
        result = original_planner(wires)
        captured.clear()
        captured.update(result)
        return result
    monkeypatch.setattr(editor_scene, "build_wire_displays", capture)
    try:
        canvas.resize(1100, 800)
        canvas.show()
        QApplication.processEvents()
        canvas.view.actual_size()
        canvas.set_snap_enabled(False)
        item = next(item for item in canvas.scene._items_by_id.values()
                    if item._name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ")
        start = QPointF(item.pos())
        for step, delta in enumerate((40.0, -40.0), start=1):
            canvas.scene.select_representations((item.representation_id,))
            canvas.view.centerOn(item.pos())
            canvas.view.setFocus()
            QApplication.processEvents()
            point = item.body_scene_rect().center() + QPointF(40, 0)
            hit = canvas.scene.resolve_hit_target(point, canvas.view.transform())
            assert hit.kind is editor_scene.HitTestKind.BODY and hit.item is item
            stored = controller.diagram
            _mouse(canvas, "move", point + QPointF(0, 25))
            _mouse(canvas, "move", point)
            _mouse(canvas, "press", point)
            _mouse(canvas, "move", point + QPointF(delta / 2, 0))
            _mouse(canvas, "move", point + QPointF(delta, 0))
            assert controller.diagram is stored, "A preview must not write the document"
            assert not _interior_route_junctions(captured)
            assert _electrical_identity(controller) == electrical_before
            _mouse(canvas, "release", point + QPointF(delta, 0))
            assert len(controller.journal) == journal_before + step
            expected = start + (QPointF(40, 0) if step == 1 else QPointF())
            assert item.pos() == expected
            saved = controller.diagram.representations[item.representation_id]
            assert QPointF(saved.x, saved.y) == expected
            assert not _interior_route_junctions(captured)
            assert _electrical_identity(controller) == electrical_before
        canvas.undo()
        assert item.pos() == start + QPointF(40, 0)
        canvas.undo()
        # Viewport/snap changes have their own workspace revision. The two
        # undo operations must restore every persisted diagram object/route,
        # including original waypoint IDs, without rewinding view preferences.
        assert controller.diagram.representations == document_before.representations
        assert controller.diagram.routes == document_before.routes
        assert controller.diagram.pages == document_before.pages
        assert item.pos() == start
        assert not _interior_route_junctions(captured)
        assert _electrical_identity(controller) == electrical_before
        assert not errors
        assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == source_hash
    finally:
        canvas.close()
        QApplication.processEvents()


@pytest.mark.parametrize("delta, allowed", ((40.0, True), (80.0, False)))
def test_bus_preview_matches_canonical_collision_and_reuses_one_snapshot(monkeypatch, delta, allowed):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    source_hash = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    canvas = editor_scene.EditorCanvas(ProjectEditorController(load_project(DEMO)))
    controller = canvas.controller
    before = _electrical_identity(controller)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb:
                        errors.append("".join(traceback.format_exception(kind, value, tb))))
    constructions = []
    def counted_snapshot(*args, **kwargs):
        snapshot = DiagramCollisionService(*args, **kwargs)
        constructions.append(snapshot)
        return snapshot
    try:
        canvas.resize(1100, 800)
        canvas.show()
        QApplication.processEvents()
        canvas.view.actual_size()
        canvas.set_snap_enabled(False)
        item = next(item for item in canvas.scene._items_by_id.values()
                    if item._name == "ЦЕНТРАЛЬНАЯ · 1 СШ 10 кВ")
        canvas.scene.select_representations((item.representation_id,))
        canvas.view.centerOn(item.pos())
        canvas.view.setFocus()
        QApplication.processEvents()
        initial = QPointF(item.pos())
        stored = controller.diagram
        journal = len(controller.journal)
        point = item.body_scene_rect().center() + QPointF(40, 0)
        _mouse(canvas, "move", point + QPointF(0, 25))
        _mouse(canvas, "move", point)
        _mouse(canvas, "press", point)
        monkeypatch.setattr(editor_scene, "DiagramCollisionService", counted_snapshot)
        for fraction in (0.25, 0.5, 0.75, 1.0):
            _mouse(canvas, "move", point + QPointF(delta * fraction, 0))
            exact = DiagramCollisionService(stored, controller.model).check_move(
                (item.representation_id,), delta * fraction, 0)
            assert bool(canvas.scene._drag_collision_ids) is not exact.allowed
        assert len(constructions) == 1, "Do not reconstruct the whole collision index per pixel"
        assert bool(canvas.scene._drag_collision_ids) is not allowed
        assert controller.diagram is stored
        _mouse(canvas, "release", point + QPointF(delta, 0))
        assert item.pos() == initial + (QPointF(delta, 0) if allowed else QPointF())
        assert len(controller.journal) == journal + int(allowed)
        assert canvas.scene._drag_collision_snapshot is None
        assert _electrical_identity(controller) == before
        for terminal in ("escape", "sync"):
            # A new gesture needs a fresh saved-state index, and neither
            # cancellation nor a document refresh may retain that snapshot.
            canvas.view.centerOn(item.pos())
            canvas.view.setFocus()
            QApplication.processEvents()
            point = item.body_scene_rect().center() + QPointF(40, 0)
            _mouse(canvas, "move", point + QPointF(0, 25))
            _mouse(canvas, "move", point)
            _mouse(canvas, "press", point)
            assert canvas.scene._drag_collision_snapshot is None
            count = len(constructions)
            _mouse(canvas, "move", point + QPointF(10, 0))
            _mouse(canvas, "move", point + QPointF(20, 0))
            assert len(constructions) == count + 1
            assert canvas.scene._drag_collision_snapshot is constructions[-1]
            assert constructions[-1] is not constructions[0]
            if terminal == "escape":
                QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
            else:
                canvas.refresh()
            QApplication.processEvents()
            assert canvas.scene._drag_collision_snapshot is None
            assert not canvas.scene._start_positions
            _mouse(canvas, "release", point + QPointF(20, 0))
            assert len(controller.journal) == journal + int(allowed)
            saved = controller.diagram.representations[item.representation_id]
            assert item.pos() == QPointF(saved.x, saved.y)
            assert _electrical_identity(controller) == before
        assert not errors
        assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == source_hash
    finally:
        canvas.close()
        QApplication.processEvents()
