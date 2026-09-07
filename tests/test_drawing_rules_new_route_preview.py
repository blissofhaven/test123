"""New wire/line gestures obey occupied routes while preserving crossings/taps."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QDialogButtonBox
from rza_calc.editor.controller import NodeTarget
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.editor_scene import EditorCanvas
from rza_calc.gui.line_parameters import LineParametersDialog
from test_stage4_editor_interaction import _controller, _U10


def _build(start=(0,0), end=(240,0)):
    app = QApplication.instance() or QApplication([])
    controller = _controller()
    left = controller.add_electrical_node("Чужой провод слева", x=60, y=0, voltage_class_id=_U10)
    right = controller.add_electrical_node("Чужой провод справа", x=180, y=0, voltage_class_id=_U10)
    controller.connect_from_node(NodeTarget(left.node_id,left.representation_id), NodeTarget(right.node_id,right.representation_id))
    occupied = next(iter(controller.diagram.routes.values()))
    first = controller.add_electrical_node("Начало", x=start[0], y=start[1], voltage_class_id=_U10)
    last = controller.add_electrical_node("Конец", x=end[0], y=end[1], voltage_class_id=_U10) if end else None
    canvas = EditorCanvas(controller)
    canvas.resize(1100,700)
    canvas.show()
    canvas.view.actual_size()
    canvas.view.centerOn(120,0)
    canvas.set_snap_enabled(False)
    canvas._save_viewport(canvas.view.viewport_state())
    QApplication.processEvents()
    return canvas, occupied, first, last


def _finish(canvas, kind, point):
    seen = []
    poll, watchdog = QTimer(canvas), QTimer(canvas)
    watchdog.setSingleShot(True)
    def choose():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget,QMenu) and widget.isVisible() and widget.title() == "Чем соединить":
                widget.setActiveAction(widget.actions()[0])
                seen.append("wire")
                QTest.keyClick(widget,Qt.Key.Key_Return)
                poll.stop()
                watchdog.stop()
                return
            if isinstance(widget,LineParametersDialog) and widget.isVisible():
                widget.mode_combo.setCurrentIndex(widget.mode_combo.findData("draft"))
                seen.append("draft")
                QTest.mouseClick(widget.buttons.button(QDialogButtonBox.StandardButton.Ok),Qt.MouseButton.LeftButton)
                poll.stop()
                watchdog.stop()
                return
    def abort():
        poll.stop()
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget,(QMenu,LineParametersDialog)) and widget.isVisible():
                widget.close()
    poll.timeout.connect(choose)
    watchdog.timeout.connect(abort)
    poll.start(1)
    watchdog.start(2500)
    QTest.mouseRelease(canvas.view.viewport(),Qt.MouseButton.LeftButton,pos=canvas.view.mapFromScene(point))
    poll.stop()
    watchdog.stop()
    assert seen == ["wire" if kind == "wire" else "draft"]


@pytest.mark.parametrize("kind", ["wire","cable","overhead"])
def test_new_route_preview_detours_existing_independent_conductor_and_matches_commit(kind):
    canvas, occupied, first, last = _build()
    controller = canvas.controller
    before,count = electrical_model_fingerprint(controller.model),len(controller.journal)
    if kind == "wire":
        canvas.activate_connection_tool()
    else:
        canvas.view.begin_placement({"target_kind":"physical_line","type_id":"physical_line."+kind})
    start,end = QPointF(0,0),QPointF(240,0)
    QTest.mousePress(canvas.view.viewport(),Qt.MouseButton.LeftButton,pos=canvas.view.mapFromScene(start))
    QTest.mouseMove(canvas.view.viewport(),canvas.view.mapFromScene(end))
    tool = canvas.scene._connection_tool if kind == "wire" else canvas.scene._physical_line_tool
    preview = tuple((p.x,p.y) for p in tool.preview_vertices)
    assert preview and not any(a[1] == b[1] == 0 and max(min(a[0],b[0]),60) < min(max(a[0],b[0]),180)
                               for a,b in zip(preview,preview[1:]))
    assert controller.diagram.routes[occupied.id] == occupied
    assert electrical_model_fingerprint(controller.model) == before
    errors=[]
    canvas.errorOccurred.connect(errors.append)
    _finish(canvas,kind,end)
    assert len(controller.journal) == count+1,errors
    added = next(r for r in controller.diagram.routes.values() if r.id != occupied.id)
    assert tuple((p.x,p.y) for p in added.waypoints) == preview
    assert controller.diagram.routes[occupied.id] == occupied
    assert not controller.diagram.validate_targets(controller.model)
    canvas.undo()
    assert electrical_model_fingerprint(controller.model) == before
    canvas.close()


@pytest.mark.parametrize("tap", [False,True])
def test_crossing_stays_independent_but_explicit_t_target_connects(tap):
    start,end = (120,120),(120,-120)
    canvas,occupied,first,last = _build(start, None if tap else end)
    controller = canvas.controller
    before,count = electrical_model_fingerprint(controller.model),len(controller.journal)
    canvas.activate_connection_tool()
    target = QPointF(120,0) if tap else QPointF(*end)
    QTest.mousePress(canvas.view.viewport(),Qt.MouseButton.LeftButton,pos=canvas.view.mapFromScene(QPointF(*start)))
    QTest.mouseMove(canvas.view.viewport(),canvas.view.mapFromScene(target))
    assert canvas.scene._connection_tool.preview_vertices
    assert not canvas.scene._connection_routing_error
    _finish(canvas,"wire",target)
    assert len(controller.journal) == count+1
    assert len(controller.diagram.routes) == (3 if tap else 2)
    node = controller.diagram.representations[first.representation_id].electrical_node_id
    occupied_node = controller.diagram.routes[occupied.id].electrical_node_id
    assert (node == occupied_node) is tap
    assert not controller.diagram.validate_targets(controller.model)
    canvas.undo()
    assert electrical_model_fingerprint(controller.model) == before
    canvas.close()


def test_port_reconnect_excludes_only_replaced_route_and_keeps_neighbour_blocking():
    canvas,occupied,first,last = _build()
    controller = canvas.controller
    load = controller.add_equipment("builtin.load","Переподключаемый",x=0,y=140,rotation_deg=90)
    connection = controller.connect_port_to_node(load.port_ids[0],first.node_id,
        source_representation_id=load.representation_id,node_representation_id=first.representation_id)
    replaced_id = connection.route_id
    canvas.refresh()
    port = canvas.scene._items_by_id[load.representation_id].port_item(load.port_ids[0])
    before,count = electrical_model_fingerprint(controller.model),len(controller.journal)
    end=QPointF(240,0)
    QTest.mousePress(canvas.view.viewport(),Qt.MouseButton.LeftButton,pos=canvas.view.mapFromScene(port.scenePos()))
    QTest.mouseMove(canvas.view.viewport(),canvas.view.mapFromScene(end))
    assert canvas.scene._connection_replaced_route_id == replaced_id
    assert canvas.scene._connection_tool._occupied_segments == tuple(
        (a.x,a.y,b.x,b.y) for a,b in zip(occupied.waypoints,occupied.waypoints[1:]))
    preview = tuple((p.x,p.y) for p in canvas.scene._connection_tool.preview_vertices)
    assert preview
    errors=[]
    canvas.errorOccurred.connect(errors.append)
    _finish(canvas,"wire",end)
    assert len(controller.journal) == count+1,errors
    assert controller.diagram.routes[occupied.id] == occupied
    assert controller.model.node_for_port(load.port_ids[0]).id == last.node_id
    route = next(r for r in controller.diagram.routes.values() if r.id != occupied.id)
    assert tuple((p.x,p.y) for p in route.waypoints) == preview
    canvas.undo()
    assert electrical_model_fingerprint(controller.model) == before
    canvas.close()
