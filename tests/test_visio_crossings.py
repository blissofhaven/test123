"""Visio-style crossings are a display layer, not electrical connections."""
from __future__ import annotations

from dataclasses import dataclass
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument, DiagramPage, PageId, RouteWaypoint, RouteWaypointId,
    GraphicalRepresentation, GraphicalRepresentationId, RepresentationTargetKind,
)
from rza_calc.domain.electrical import ElectricalModel, VoltageClassId
from rza_calc.editor.controller import ProjectEditorController
from rza_calc.editor.line_bridges import BridgeWire, build_wire_displays
from rza_calc.editor.orthogonal_routing import RouteVertex
from rza_calc.gui.editor_scene import DiagramGraphicsScene, EditorCanvas
from rza_calc.gui.route_bridges import wire_display_path


def _crossing(*, reverse: bool = False, vertical_first: bool = False):
    flat = ((0.0, 0.0), (100.0, 0.0))
    upright = ((50.0, -50.0), (50.0, 50.0))
    if reverse:
        flat, upright = flat[::-1], upright[::-1]
    return (
        BridgeWire("b" if vertical_first else "a", flat, "flat"),
        BridgeWire("a" if vertical_first else "b", upright, "upright"),
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("vertical_first", [False, True])
def test_semicircle_direction_independent_of_waypoint_order(reverse, vertical_first):
    wires = _crossing(reverse=reverse, vertical_first=vertical_first)
    displays = build_wire_displays(wires)
    assert displays == build_wire_displays(wires[::-1])
    assert len(displays["a"].bridges) == 1
    jump = displays["a"].bridges[0]
    assert jump.radius == 6
    assert jump.horizontal is not vertical_first
    path = wire_display_path(displays["a"])
    assert path.currentPosition() == QPointF(*displays["a"].wire.points[-1])
    if vertical_first:
        assert path.boundingRect().right() == pytest.approx(56)
    else:
        assert path.boundingRect().top() == pytest.approx(-6)
    # No background mask: only the lower wire has a small render-only gap.
    assert len(displays["b"].gaps) == 1
    assert wires == _crossing(reverse=reverse, vertical_first=vertical_first)


def test_same_remote_node_identity_does_not_create_an_interior_junction():
    first, second = _crossing()
    second = BridgeWire(second.key, second.points, first.node_id)
    displays = build_wire_displays((first, second))
    assert displays["a"].bridges and not displays["b"].bridges
    assert not any(display.junctions for display in displays.values())
    assert displays["b"].gaps


def test_physical_branches_sharing_remote_end_nodes_still_cross_without_connection():
    wires = (
        BridgeWire("a", ((0, 0), (100, 0)), endpoint_node_ids=("n1", "n2")),
        BridgeWire("b", ((50, -50), (50, 50)), endpoint_node_ids=("n1", "n2")),
    )
    displays = build_wire_displays(wires)
    assert displays["a"].bridges
    assert not displays["a"].junctions


def test_actual_shared_endpoint_is_not_bridged():
    displays = build_wire_displays((
        BridgeWire("a", ((0, 0), (50, 0)), endpoint_node_ids=(None, "shared")),
        BridgeWire("b", ((50, 0), (50, 50)), endpoint_node_ids=("shared", None)),
    ))
    assert all(not display.bridges for display in displays.values())
    assert displays["a"].junctions == ((50, 0),)


def test_corner_uses_other_wire_and_close_crossings_merge_without_overlap():
    displays = build_wire_displays((
        BridgeWire("a", ((0, 0), (50, 0), (50, 30))),
        BridgeWire("b", ((50, -20), (50, 40))),
    ))
    assert not displays["a"].bridges
    assert len(displays["b"].bridges) == 1
    assert displays["b"].bridges[0].radius == 6
    close = build_wire_displays((
        BridgeWire("a", ((0, 0), (100, 0))),
        BridgeWire("b", ((50, -50), (50, 50))),
        BridgeWire("c", ((52, -50), (52, 50))),
    ))
    assert len(close["a"].bridges) == 1
    assert close["a"].bridges[0].center == (51, 0)
    assert close["a"].bridges[0].radius == 7
    assert len(close["b"].gaps) == len(close["c"].gaps) == 1


def test_jump_does_not_cover_an_existing_junction_and_bus_never_jumps():
    displays = build_wire_displays((
        BridgeWire("a", ((0, 0), (100, 0)), "n1"),
        # A genuine T endpoint, not a crossing of remote same-net interiors.
        BridgeWire("b", ((50, 0), (50, 50)), "n1"),
        BridgeWire("c", ((53, -50), (53, 50)), "n2"),
    ))
    jump = displays["a"].bridges[0]
    assert min(jump.start[0], jump.end[0]) > 50
    bus = build_wire_displays((
        BridgeWire("a-bus", ((0, 0), (100, 0)), "bus", bridge_allowed=False),
        BridgeWire("z-route", ((50, -50), (50, 50)), "other"),
    ))
    assert not bus["a-bus"].bridges
    assert bus["z-route"].bridges


def test_degenerate_and_diagonal_legacy_segments_are_not_rewritten():
    displays = build_wire_displays((
        BridgeWire("a", ((0, 0), (0, 0), (100, 100))),
        BridgeWire("b", ((50, -50), (50, 150))),
    ))
    assert all(not display.bridges for display in displays.values())
    assert displays["a"].wire.points == ((0, 0), (0, 0), (100, 100))
    with pytest.raises(ValueError, match="unique"):
        build_wire_displays((BridgeWire("a", ()), BridgeWire("a", ())))


_APP: QApplication | None = None


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _canvas_with_crossing():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    page = DiagramPage(PageId("page.visio.crossing"), "Crossings")
    controller = ProjectEditorController(_Project(
        ElectricalModel.with_builtins("Crossing test"),
        DiagramDocument.create("Crossing test", (page,)),
        ProjectCatalogSnapshots(),
    ))
    route_ids = []
    for number, points in enumerate((
        ((0, 0), (100, 0)), ((50, -50), (50, 50)),
    )):
        loads = [controller.add_equipment("builtin.load", f"Load {number}-{index}", x=x, y=y,
                         voltage_class_by_group={"main": VoltageClassId("builtin.voltage.ac.10kv")})
                 for index, (x, y) in enumerate(points)]
        connected = controller.connect_ports(
            loads[0].port_ids[0], loads[1].port_ids[0],
            route_waypoints=tuple(RouteWaypoint(RouteWaypointId(f"waypoint.cross.{number}.{index}"), x, y)
                                  for index, (x, y) in enumerate(points)),
            first_representation_id=loads[0].representation_id,
            second_representation_id=loads[1].representation_id,
        )
        route_ids.append(connected.route_id)
    return controller, EditorCanvas(controller), route_ids


def _has_curve(path):
    return any(path.elementAt(index).type == QPainterPath.ElementType.CurveToElement
               for index in range(path.elementCount()))


def test_scene_sync_and_preview_preserve_domain_ids_waypoints_selection_and_hit_path():
    controller, canvas, route_ids = _canvas_with_crossing()
    saved_document = controller.diagram
    fingerprint = controller.model.connectivity_signature()
    revision = controller.model.revision
    journal = len(controller.journal)
    items = [canvas.scene._route_items_by_id[route_id] for route_id in route_ids]
    assert sum(_has_curve(item._display_path) for item in items) == 1
    first = items[0]
    first.setSelected(True)
    hit_path = first.shape()
    original_path = QPainterPath(first._path)
    canvas.scene._refresh_route_bridges()
    assert first.isSelected() and first.shape() == hit_path
    assert first._path == original_path
    for item in items:
        assert item.boundingRect().contains(item._display_path.boundingRect().topLeft())
        assert item.boundingRect().contains(item._display_path.boundingRect().bottomRight())
    items[1].set_temporary_vertices((RouteVertex(150, -50), RouteVertex(150, 50)))
    assert not any(_has_curve(item._display_path) for item in items)
    items[1].clear_temporary_route()
    assert sum(_has_curve(item._display_path) for item in items) == 1
    assert controller.diagram is saved_document
    assert controller.model.revision == revision
    assert controller.model.connectivity_signature() == fingerprint
    assert len(controller.journal) == journal
    assert first.isSelected()
    canvas.close()


def test_physical_line_display_paths_use_local_coordinates_and_clear_after_move():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    model = ElectricalModel.with_builtins("Physical crossing")
    page = DiagramPage(PageId("page.visio.physical"), "Physical")
    representations = []
    for index, angle in enumerate((0, 90)):
        equipment, _ = model.create_equipment("builtin.line", f"Line {index}")
        representations.append(GraphicalRepresentation(
            GraphicalRepresentationId(f"representation.physical.{index}"),
            page.id, RepresentationTargetKind.EQUIPMENT,
            equipment_id=equipment.id, x=300, y=200, rotation_deg=angle,
            symbol_key="builtin.line", label=f"Line {index}",
            extensions={"stage3_graphics": {"width": 100, "height": 24}},
        ))
    document = DiagramDocument.create("Physical crossing", (page,), representations)
    scene = DiagramGraphicsScene()
    scene.sync_document(document, model)
    items = [scene._items_by_id[representation.id] for representation in representations]
    assert all(item._bridge_paths for item in items)
    assert sum(_has_curve(path) for item in items for path in item._bridge_paths.values()) == 1
    assert all(abs(path.boundingRect().center().x()) < 60 and abs(path.boundingRect().center().y()) < 60
               for item in items for path in item._bridge_paths.values())
    for item in items:
        assert all(item.boundingRect().contains(path.boundingRect().topLeft()) and
                   item.boundingRect().contains(path.boundingRect().bottomRight())
                   for path in item._bridge_paths.values())
    saved_ids = tuple(model.equipment)
    items[1].setPos(600, 200)
    scene._refresh_route_bridges()
    assert not any(item._bridge_paths for item in items)
    assert tuple(model.equipment) == saved_ids
    assert document.representations[representations[1].id].x == 300
