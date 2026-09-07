"""Real Energoraion mouse regressions for drag lifecycle and repeated rebuilds.

Counts assert work done, not machine-specific timing. These tests exercise Qt
events on the actual 157-object/160-route document without saving the example.
"""
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
from rza_calc.gui.editor_scene import CanvasMode, DiagramObjectItem, EditorCanvas, HitTestKind
from rza_calc.io.project import load_project


ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tests/fixtures/legacy_projects/energoraion.json"
_APP = None


def _electrical_signature(controller):
    model = controller.model
    return (electrical_model_fingerprint(model), model.revision, model.connectivity_signature(),
            frozenset(model.equipment), frozenset(model.electrical_nodes), frozenset(model.connections),
            dict(model.ports), {key: value.port_ids for key, value in model.equipment.items()},
            frozenset(controller.diagram.representations), frozenset(controller.diagram.routes))


@pytest.fixture
def canvas(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb:
                        errors.append("".join(traceback.format_exception(kind, value, tb))))
    source_hash = hashlib.sha256(DEMO.read_bytes()).hexdigest()
    widget = EditorCanvas(ProjectEditorController(load_project(DEMO)))
    widget.resize(1000, 760)
    widget.show()
    QApplication.processEvents()
    widget.view.actual_size()
    widget.view.setFocus()
    widget.set_snap_enabled(False)
    QApplication.processEvents()
    original = _electrical_signature(widget.controller)
    assert len(widget.scene._items_by_id) == 157
    assert len(widget.scene._route_items_by_id) == 160
    yield widget
    try:
        assert _electrical_signature(widget.controller) == original
        assert hashlib.sha256(DEMO.read_bytes()).hexdigest() == source_hash
        assert not errors, "Uncaught exception in Qt callback:\n" + "\n".join(errors)
    finally:
        widget.close()
        QApplication.processEvents()


def _target(canvas):
    # Fixed real diagram apparatus, not an empty synthetic canvas or a special
    # unconnected item. It has an allowed +40 scene-unit horizontal displacement.
    return next(item for item in canvas.scene._items_by_id.values()
                if item._name == "КТП Ф-4 400 кВ·А (Центральная)")


def _mouse(canvas, kind, point):
    position = canvas.view.mapFromScene(point)
    if kind == "move":
        QTest.mouseMove(canvas.view.viewport(), position, delay=1)
    else:
        {"press": QTest.mousePress, "release": QTest.mouseRelease}[kind](
            canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    QApplication.processEvents()


def _begin(canvas, item, *, group=()):
    canvas.scene.select_representations(group or (item.representation_id,))
    canvas.view.centerOn(item.scenePos())
    canvas.view.setFocus()
    QApplication.processEvents()
    point = item.body_scene_rect().center()
    hit = canvas.scene.resolve_hit_target(point, canvas.view.transform())
    assert hit.kind is HitTestKind.BODY and hit.item is item
    _mouse(canvas, "move", point + QPointF(25, 25))
    _mouse(canvas, "move", point)
    _mouse(canvas, "press", point)
    assert isinstance(canvas.scene.mouseGrabberItem(), DiagramObjectItem)
    return point


def _geometry(canvas):
    return (dict(canvas.controller.diagram.representations), dict(canvas.controller.diagram.routes))


def _assert_scene_matches_saved(canvas):
    for identifier, item in canvas.scene._items_by_id.items():
        saved = canvas.controller.diagram.representations[identifier]
        assert item.pos() == QPointF(saved.x, saved.y), identifier.value
    for identifier, item in canvas.scene._route_items_by_id.items():
        # The display-only crossing bridges are not saved; the underlying
        # electrical polyline must return to its stored vertices after cancel.
        from rza_calc.gui.editor_scene import _route_vertices
        assert item._display_vertices == _route_vertices(canvas.controller.diagram.routes[identifier]), identifier.value


def _count_rebuilds(canvas, monkeypatch):
    counts = {"bridges": 0, "labels": 0}
    for name, key in (("_refresh_route_bridges", "bridges"), ("relayout_labels", "labels")):
        original = getattr(canvas.scene, name)
        def counted(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(canvas.scene, name, counted)
    return counts


def test_noop_apparatus_click_does_not_rebuild_all_routes_individually(canvas, monkeypatch):
    point = _begin(canvas, _target(canvas))
    before = _geometry(canvas)
    counts = _count_rebuilds(canvas, monkeypatch)
    _mouse(canvas, "release", point)
    assert counts["bridges"] <= 1, counts
    assert counts["labels"] <= 1, counts
    assert _geometry(canvas) == before
    _assert_scene_matches_saved(canvas)


def test_rejected_drag_restores_all_geometry_with_one_batched_rebuild(canvas, monkeypatch):
    item = _target(canvas)
    before = _geometry(canvas)
    other = next(row for row in canvas.scene._items_by_id.values()
                 if row.representation.equipment_id is not None and row is not item)
    end = other.pos()
    _begin(canvas, item)
    _mouse(canvas, "move", end)
    assert item.pos() != QPointF(before[0][item.representation_id].x, before[0][item.representation_id].y)
    counts = _count_rebuilds(canvas, monkeypatch)
    _mouse(canvas, "release", end)
    assert counts["bridges"] <= 1, counts
    assert counts["labels"] <= 1, counts
    assert _geometry(canvas) == before
    _assert_scene_matches_saved(canvas)


@pytest.mark.parametrize("terminal", ("escape", "analysis", "focus_out", "pan", "placement", "grab_loss"))
def test_drag_cancel_terminal_restores_scene_and_never_commits_on_late_release(canvas, terminal):
    item = _target(canvas)
    before = _geometry(canvas)
    journal = tuple(canvas.controller.journal)
    point = _begin(canvas, item)
    end = point + QPointF(40, 0)
    _mouse(canvas, "move", point + QPointF(20, 0))
    _mouse(canvas, "move", end)
    assert item.pos() == QPointF(before[0][item.representation_id].x + 40, before[0][item.representation_id].y)
    assert _geometry(canvas) == before
    if terminal == "escape":
        QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    elif terminal == "analysis":
        canvas.set_mode(CanvasMode.ANALYSIS)
    elif terminal == "focus_out":
        canvas.view.clearFocus()
    elif terminal == "pan":
        QTest.keyPress(canvas.view, Qt.Key.Key_Space)
    elif terminal == "placement":
        canvas.view.begin_placement({"target_kind": "equipment", "type_id": "builtin.load", "name": "Не вставлять"})
    else:
        item.ungrabMouse()
    QApplication.processEvents()
    assert not canvas.scene._start_positions
    _assert_scene_matches_saved(canvas)
    _mouse(canvas, "release", end)
    if terminal == "pan":
        QTest.keyRelease(canvas.view, Qt.Key.Key_Space)
    assert _geometry(canvas) == before
    assert tuple(canvas.controller.journal) == journal
    _assert_scene_matches_saved(canvas)


def test_group_drag_escape_restores_every_representation_and_route(canvas):
    item = _target(canvas)
    selected = tuple(canvas.scene._items_by_id)
    before = _geometry(canvas)
    point = _begin(canvas, item, group=selected)
    _mouse(canvas, "move", point + QPointF(20, 12))
    assert len(canvas.scene._start_positions) == 157
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    _mouse(canvas, "release", point + QPointF(20, 12))
    assert _geometry(canvas) == before
    assert len(canvas.scene.selected_representation_ids()) == 157
    assert not canvas.scene._start_positions
    _assert_scene_matches_saved(canvas)


@pytest.mark.parametrize("refresh", ("canvas", "scene"))
def test_refresh_during_body_drag_cancels_preview_without_history(canvas, refresh):
    item = _target(canvas)
    before = _geometry(canvas)
    journal = tuple(canvas.controller.journal)
    point = _begin(canvas, item)
    _mouse(canvas, "move", point + QPointF(40, 0))
    if refresh == "canvas":
        canvas.refresh()
    else:
        canvas.scene.sync_document(canvas.controller.diagram, canvas.controller.model)
    QApplication.processEvents()
    _mouse(canvas, "release", point + QPointF(40, 0))
    assert _geometry(canvas) == before
    assert tuple(canvas.controller.journal) == journal
    assert not canvas.scene._start_positions
    _assert_scene_matches_saved(canvas)


def test_allowed_body_release_commits_exactly_one_command_and_undo_restores_routes(canvas):
    item = _target(canvas)
    before = _geometry(canvas)
    journal_count = len(canvas.controller.journal)
    point = _begin(canvas, item)
    _mouse(canvas, "move", point + QPointF(20, 0))
    _mouse(canvas, "move", point + QPointF(40, 0))
    _mouse(canvas, "release", point + QPointF(40, 0))
    assert len(canvas.controller.journal) == journal_count + 1
    saved = canvas.controller.diagram.representations[item.representation_id]
    assert saved.x == before[0][item.representation_id].x + 40
    _assert_scene_matches_saved(canvas)
    canvas.undo()
    QApplication.processEvents()
    assert _geometry(canvas) == before
    _assert_scene_matches_saved(canvas)


def test_auxiliary_mouse_release_does_not_commit_active_left_button_drag(canvas):
    item = _target(canvas)
    before = _geometry(canvas)
    journal = tuple(canvas.controller.journal)
    point = _begin(canvas, item)
    end = point + QPointF(40, 0)
    _mouse(canvas, "move", end)
    position = canvas.view.mapFromScene(end)
    QTest.mousePress(canvas.view.viewport(), Qt.MouseButton.RightButton, pos=position)
    QTest.mouseRelease(canvas.view.viewport(), Qt.MouseButton.RightButton, pos=position)
    QApplication.processEvents()
    assert _geometry(canvas) == before, "Right-button release prematurely committed the held left-button drag"
    assert tuple(canvas.controller.journal) == journal
    assert canvas.scene._start_positions
    _mouse(canvas, "release", end)
    assert len(canvas.controller.journal) == len(journal) + 1
    _assert_scene_matches_saved(canvas)
