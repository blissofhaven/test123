"""Live conductor display matches full uncached layout without document writes."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont, QPainterPath
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.electrical import DataConfirmation, ElectricalModel, LineKind, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor.controller import NodeTarget, PhysicalLineInput, ProjectEditorController
from rza_calc.gui import editor_scene
from rza_calc.gui.label_layout import wrap_text
from rza_calc.io.project import load_project


LEGACY = Path(__file__).parent / "fixtures/legacy_projects/energoraion.json"
MANUAL_OFFSET = (81.25, -63.5)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _point(point):
    return point.x(), point.y()


def _path(path):
    return tuple((path.elementAt(index).type, path.elementAt(index).x,
                  path.elementAt(index).y) for index in range(path.elementCount()))


def _display(scene):
    """Read painted data independently of the cache and label solver keys."""
    labels, objects, routes = {}, {}, {}
    for family, items in (("object", scene._items_by_id), ("route", scene._route_items_by_id)):
        for identifier, item in items.items():
            label = item._label
            rectangle = label.sceneBoundingRect()
            labels[(family, identifier)] = (
                label.text(), _point(label.pos()), _point(label.scenePos()), label.rotation(),
                (rectangle.x(), rectangle.y(), rectangle.width(), rectangle.height()),
                label.toolTip(), label.isVisible(), item._label_requested_visible,
            )
    for identifier, item in scene._items_by_id.items():
        objects[identifier] = (
            _point(item.pos()), item.rotation(), item.isSelected(),
            tuple((index, _path(path)) for index, path in sorted(item._bridge_paths.items())),
            tuple(_point(point) for point in item._bridge_nodes),
            tuple(_point(point) for point in item._bus_junction_points),
        )
    for identifier, item in scene._route_items_by_id.items():
        routes[identifier] = (
            tuple((vertex.x, vertex.y) for vertex in item._display_vertices),
            _path(item._path), _path(item._display_path), _path(item.shape()),
            tuple(_point(point) for point in item._bridge_nodes), item.isSelected(),
        )
    return labels, objects, routes, tuple(scene.label_layout_conflicts())


def _assert_uncached_equivalence(canvas, monkeypatch):
    observed = _display(canvas.scene)
    original_calls = []

    def original_wrap(label, text):
        original_calls.append((text, label.font()))
        return wrap_text(text, label.font())

    # Use the same native Qt objects, font engine and preview paths. Re-run the
    # entire display calculation, bypassing the new string cache explicitly.
    with monkeypatch.context() as reference:
        reference.setattr(editor_scene, "wrapped_label_text", original_wrap)
        canvas.scene._label_layout_key = None
        canvas.scene._refresh_route_bridges()
        canvas.scene.relayout_labels()
        expected = _display(canvas.scene)
    assert original_calls, "The reference must really measure uncached text"
    assert observed == expected
    return observed


def _identity(controller):
    return (ElectricalModelMemento.capture(controller.model),
            electrical_model_fingerprint(controller.model), controller.diagram,
            tuple(controller.journal))


def _show(controller):
    canvas = editor_scene.EditorCanvas(controller)
    canvas.resize(1100, 760)
    canvas.show()
    QApplication.processEvents()
    canvas.view.actual_size()
    canvas.set_snap_enabled(False)
    canvas.view.centerOn(0, 0)
    canvas.view.setFocus()
    QApplication.processEvents()
    for item in (*canvas.scene._items_by_id.values(), *canvas.scene._route_items_by_id.values()):
        item._label.setFont(QFont("Segoe UI", 9))
    canvas.scene.relayout_labels()
    return canvas


def _native_canvas():
    project = SimpleNamespace(
        electrical_model=ElectricalModel.with_builtins("Display oracle"),
        diagram=DiagramDocument.create("Display oracle", (DiagramPage(PageId("page.display.oracle"), "Oracle"),)),
        catalog_snapshots=ProjectCatalogSnapshots(),
    )
    controller = ProjectEditorController(project)
    voltage = VoltageClassId("builtin.voltage.ac.10kv")
    lines, nodes = [], []
    for index, (first, last) in enumerate((((-300, 0), (300, 0)), ((0, -200), (0, 200)))):
        endpoints = [controller.add_electrical_node(
            f"Узел {index}-{end}: наименование для проверки переноса длинной подписи",
            x=x, y=y, voltage_class_id=voltage)
            for end, (x, y) in enumerate((first, last))]
        nodes.extend(endpoints)
        lines.append(controller.create_physical_line(
            f"КЛ-{index}: длинное наименование нефтепромыслового присоединения",
            LineKind.CABLE,
            NodeTarget(endpoints[0].node_id, endpoints[0].representation_id),
            NodeTarget(endpoints[1].node_id, endpoints[1].representation_id),
            physical=PhysicalLineInput(850_000, DataConfirmation.CONFIRMED,
                {"r1_ohm_per_km": .24, "x1_ohm_per_km": .08,
                 "conductor_mark": "АПвПу2г-10", "cross_section_mm2": 120}, DataConfirmation.CONFIRMED),
        ))
    # A deliberate manual overlap must stay visible in diagnostics. The moving
    # line has a different pinned label, which must follow its live geometry.
    controller.set_route_label(lines[0].route_id, label_x=0, label_y=0)
    controller.set_route_label(lines[1].route_id, label_x=MANUAL_OFFSET[0], label_y=MANUAL_OFFSET[1])
    controller.set_label(nodes[-1].representation_id, visible=False)
    return _show(controller), lines, nodes[-1].representation_id


def _curve_count(scene):
    return sum(any(item._display_path.elementAt(index).type == QPainterPath.ElementType.CurveToElement
                   for index in range(item._display_path.elementCount()))
               for item in scene._route_items_by_id.values())


@pytest.mark.parametrize("cancel", ("escape", "resync"))
def test_native_live_crossings_manual_labels_and_cancel_match_full_reference(app, monkeypatch, cancel):
    canvas, lines, hidden_id = _native_canvas()
    controller, scene = canvas.controller, canvas.scene
    route = scene._route_items_by_id[lines[1].route_id]
    route.setSelected(True)
    scene._refresh_route_bridges()
    baseline = _identity(controller)
    saved_display = _assert_uncached_equivalence(canvas, monkeypatch)
    try:
        assert _curve_count(scene) == 1
        assert any(row.manual for row in scene.label_layout_conflicts())
        assert "\n" in route._label.text()  # A populated, actually wrapped label.
        start = QPointF(0, -100)
        assert scene.begin_connected_drag(route, start)
        for offset in (120, 120, 360):
            scene.update_connected_drag(start + QPointF(offset, 0))
            QApplication.processEvents()
            assert scene._connected_drag is not None
            assert _identity(controller) == baseline
            assert route._display_vertices != tuple()
            assert max(vertex.x for vertex in route._display_vertices) == offset
            assert _point(route._label.scenePos()) == pytest.approx(
                (offset + MANUAL_OFFSET[0], MANUAL_OFFSET[1]))
            assert not scene._items_by_id[hidden_id]._label.isVisible()
            assert _curve_count(scene) == (1 if offset == 120 else 0)
            _assert_uncached_equivalence(canvas, monkeypatch)
        if cancel == "escape":
            QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
        else:
            canvas.refresh()
        QApplication.processEvents()
        assert scene._connected_drag is None
        assert _identity(controller) == baseline
        assert _assert_uncached_equivalence(canvas, monkeypatch) == saved_display
        assert _curve_count(scene) == 1
        scene.finish_connected_drag()  # A late finish cannot publish preview.
        assert _identity(controller) == baseline
    finally:
        canvas.close()
        QApplication.processEvents()


def test_real_legacy_page_preview_out_and_back_keeps_full_display_and_document(app, monkeypatch):
    from test_ui_connected_drag import _begin

    controller = ProjectEditorController(load_project(LEGACY))
    canvas = _show(controller)
    try:
        assert len(canvas.scene._items_by_id) == 157
        route, point, delta = _begin(canvas, "route")
        baseline = _identity(controller)
        saved_display = _assert_uncached_equivalence(canvas, monkeypatch)
        for factor in (0.5, 1.0, 1.0, 0.0):
            canvas.scene.update_connected_drag(point + delta * factor)
            QApplication.processEvents()
            assert _identity(controller) == baseline
            preview = canvas.scene._connected_drag.preview_routes
            assert preview
            if factor:
                assert any(row.waypoints != controller.diagram.routes[row.id].waypoints for row in preview)
            _assert_uncached_equivalence(canvas, monkeypatch)
        canvas.scene.cancel_connected_drag()
        QApplication.processEvents()
        assert _assert_uncached_equivalence(canvas, monkeypatch) == saved_display
        canvas.scene.finish_connected_drag()
        assert _identity(controller) == baseline
        assert not controller.diagram.validate_targets(controller.model)
    finally:
        canvas.close()
        QApplication.processEvents()
