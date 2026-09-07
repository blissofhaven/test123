"""Visible cross-page navigation and line placement use real Qt entry points."""
from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from rza_calc.domain.diagram import GraphicalRepresentationId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.gui.editor_scene import EditorCanvas
from test_stage4_editor_interaction import _controller, _U10


@pytest.fixture(scope="module", autouse=True)
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture
def linked_canvas():
    controller = _controller()
    first = next(iter(controller.diagram.pages))
    source = controller.add_electrical_node("На лист 2", x=0, y=0, voltage_class_id=_U10,
                                          symbol_key="busbar", width=160, height=12)
    second = controller.create_page("Лист 2")
    original = controller.diagram.representations[source.representation_id]
    target = replace(original, id=GraphicalRepresentationId.new(), page_id=second,
                     x=840, y=-320, label="На лист 1",
                     extensions={**original.extensions, "linked_page_id": first.value})
    source_rep = replace(original, extensions={**original.extensions, "linked_page_id": second.value})
    controller._project.diagram = replace(controller.diagram, representations={
        **controller.diagram.representations, source_rep.id: source_rep, target.id: target,
    })
    # The fixture describes a loaded two-page project, before its edit history.
    controller = ProjectEditorController(controller._project)
    canvas = EditorCanvas(controller)
    canvas.resize(1000, 700)
    canvas.show()
    canvas.show_page(first)
    canvas.view.actual_size()
    canvas.view.centerOn(0, 0)
    canvas.view.setFocus()
    QApplication.processEvents()
    yield canvas, source_rep, target
    canvas.close()
    QApplication.processEvents()


def test_double_click_centers_highlights_and_back_restores_view_without_electrical_change(linked_canvas):
    canvas, source, target = linked_canvas
    controller = canvas.controller
    original = controller.diagram
    fingerprint, count = electrical_model_fingerprint(controller.model), len(controller.journal)
    point = canvas.view.mapFromScene(QPointF(source.x, source.y))
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    assert canvas.page_id == source.page_id
    assert canvas.scene.selected_representation_ids() == (source.id,)
    previous = canvas.view.viewport_state()
    QTest.mouseDClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    assert canvas.page_id == target.page_id
    assert canvas.scene.selected_representation_ids() == (target.id,)
    center = canvas.view.mapToScene(canvas.view.viewport().rect().center())
    assert center.x() == pytest.approx(target.x, abs=2)
    assert center.y() == pytest.approx(target.y, abs=2)
    assert canvas._navigation_highlight is not None and canvas._navigation_highlight.isVisible()
    assert canvas.back_button.isEnabled()
    QTest.mouseClick(canvas.back_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert canvas.page_id == source.page_id
    assert canvas.scene.selected_representation_ids() == (source.id,)
    restored = canvas.view.viewport_state()
    assert restored.zoom == previous.zoom
    assert restored.center_x == pytest.approx(previous.center_x, abs=2)
    assert restored.center_y == pytest.approx(previous.center_y, abs=2)
    assert not canvas.back_button.isEnabled()
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert len(controller.journal) == count
    assert dict(controller.diagram.representations) == dict(original.representations)
    assert dict(controller.diagram.routes) == dict(original.routes)


@pytest.mark.parametrize("problem", ("missing_page", "missing_target", "ambiguous"))
def test_invalid_link_is_informative_and_does_not_jump_or_change_history(linked_canvas, problem):
    canvas, source, target = linked_canvas
    diagram = canvas.controller.diagram
    rows, pages = dict(diagram.representations), dict(diagram.pages)
    if problem == "missing_page":
        rows[source.id] = replace(source, extensions={**source.extensions, "linked_page_id": "page.missing"})
    elif problem == "missing_target":
        del rows[target.id]
    else:
        duplicate = replace(target, id=GraphicalRepresentationId.new(), x=target.x + 200)
        rows[duplicate.id] = duplicate
    canvas.controller._project.diagram = replace(diagram, representations=rows, pages=pages)
    canvas.refresh()
    messages = []
    canvas.statusMessage.connect(messages.append)
    count = len(canvas.controller.journal)
    assert not canvas.navigate_linked_page(source.id)
    assert canvas.page_id == source.page_id
    assert messages and any(word in messages[-1].lower() for word in ("не найден", "несколько"))
    assert not canvas.back_button.isEnabled()
    assert len(canvas.controller.journal) == count


@pytest.mark.parametrize("kind", ("cable", "overhead"))
def test_line_palette_preview_is_a_point_and_can_start_on_a_bus(linked_canvas, kind):
    canvas, source, _ = linked_canvas
    payload = {"target_kind": "physical_line", "type_id": "physical_line." + kind}
    canvas.view.begin_placement(payload)
    point = QPointF(source.x + 40, source.y)
    canvas.view._cursor_scene_pos = point
    canvas.view._update_equipment_drop_feedback(payload, point)
    assert canvas.view._placement_valid
    # Paint only foreground: a line's start marker must not contain the old
    # 80x50 equipment rectangle or its full clearance rectangle.
    image = QImage(120, 100, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.translate(60 - point.x(), 50 - point.y())
    canvas.view.drawForeground(painter, canvas.view.sceneRect())
    painter.end()
    ink = [(x, y) for y in range(image.height()) for x in range(image.width())
           if image.pixelColor(x, y).alpha()]
    assert ink and max(abs(x - 60) for x, _ in ink) <= 16
    assert max(abs(y - 50) for _, y in ink) <= 16
    count = len(canvas.controller.journal)
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton,
                     pos=canvas.view.mapFromScene(point))
    QApplication.processEvents()
    assert canvas.scene.physical_line_active
    assert canvas.scene._physical_line_tool.source.target_id == source.electrical_node_id.value
    assert len(canvas.controller.journal) == count
    QTest.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert not canvas.scene.physical_line_active
    assert len(canvas.controller.journal) == count


@pytest.mark.parametrize("kind", ("cable", "overhead"))
def test_real_clicked_line_goal_and_dialog_commit_do_not_implicitly_pin(linked_canvas, kind):
    from rza_calc.gui.line_parameters import LineParametersDialog
    canvas, source, _ = linked_canvas
    errors = []
    canvas.errorOccurred.connect(errors.append)
    payload = {"target_kind": "physical_line", "type_id": "physical_line." + kind}
    canvas.view.begin_placement(payload)
    point = lambda x,y: canvas.view.mapFromScene(QPointF(x,y))
    count = len(canvas.controller.journal)
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point(40,0))
    QTest.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point(180,120))
    QTest.mouseMove(canvas.view.viewport(), point(300,180))
    assert canvas.scene._physical_line_tool.manual_vertices
    assert all(p.pinned for p in canvas.scene._physical_line_tool.manual_vertices)
    preview_points = [(p.x,p.y) for p in canvas.scene._physical_line_tool.preview_vertices]
    clicked_points = [(p.x,p.y) for p in canvas.scene._physical_line_tool.manual_vertices]
    seen = []
    poll, watchdog = QTimer(canvas), QTimer(canvas)
    watchdog.setSingleShot(True)
    def choose():
        dialog = next((w for w in QApplication.topLevelWidgets()
                       if isinstance(w, LineParametersDialog) and w.isVisible()), None)
        if dialog is None:
            return
        poll.stop()
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("draft"))
        seen.append("draft")
        QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)
        watchdog.stop()
    def timeout():
        poll.stop()
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, LineParametersDialog) and widget.isVisible():
                widget.reject()
    poll.timeout.connect(choose)
    watchdog.timeout.connect(timeout)
    poll.start(1)
    watchdog.start(2000)
    QTest.keyClick(canvas.view, Qt.Key.Key_Return)
    poll.stop()
    watchdog.stop()
    assert seen == ["draft"]
    assert len(canvas.controller.journal) == count + 1, errors
    route = next(iter(canvas.controller.diagram.routes.values()))
    assert any((a.x == b.x == 180 and min(a.y,b.y) <= 120 <= max(a.y,b.y)) or
               (a.y == b.y == 120 and min(a.x,b.x) <= 180 <= max(a.x,b.x))
               for a,b in zip(route.waypoints, route.waypoints[1:])), (clicked_points, preview_points, [(p.x,p.y) for p in route.waypoints])
    assert not any(p.pinned for p in route.waypoints)
    def compact(points):
        result=[]
        for point in points:
            while len(result)>1 and ((result[-2][0] == result[-1][0] == point[0]) or
                                    (result[-2][1] == result[-1][1] == point[1])):
                result.pop()
            result.append(point)
        return result
    assert compact([(p.x,p.y) for p in route.waypoints]) == compact(preview_points)
    assert not canvas.controller.diagram.validate_targets(canvas.controller.model)
    canvas.undo()
    assert not canvas.controller.diagram.routes
