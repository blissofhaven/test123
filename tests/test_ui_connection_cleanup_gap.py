"""Bus crossings keep two fully light pixels even after restoring thin ink."""
from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QPoint, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from test_ui_direct_connections import canvas_factory, _controller, _state, U10


def _crossing(canvas_factory):
    controller = _controller()
    bus = controller.add_electrical_node("Шина", x=0, y=0, symbol_key="busbar_horizontal",
                                         width=500, height=12, voltage_class_id=U10)
    top = controller.add_equipment("builtin.circuit_breaker", "Начало", x=0, y=-200,
                                   voltage_class_by_group={"main": U10})
    bottom = controller.add_equipment("builtin.circuit_breaker", "Конец", x=80, y=200,
                                      voltage_class_by_group={"main": U10})
    controller.connect_ports(top.port_ids[-1], bottom.port_ids[0],
                             first_representation_id=top.representation_id,
                             second_representation_id=bottom.representation_id)
    canvas = canvas_factory(controller)
    route, = controller.diagram.routes.values()
    first, second = next((a, b) for a, b in zip(route.waypoints, route.waypoints[1:])
                         if a.x == b.x and min(a.y,b.y) < 0 < max(a.y,b.y))
    assert -250 < first.x < 250
    assert route.electrical_node_id != bus.node_id
    return canvas, bus, QPointF(first.x, 0)


@pytest.mark.parametrize("zoom", (.6, 1.0, 1.4, 2.0))
@pytest.mark.parametrize("selected", (False, True))
def test_bus_crossing_has_two_fully_light_pixels_on_both_edges(canvas_factory, zoom, selected):
    canvas, bus, crossing = _crossing(canvas_factory)
    before = _state(canvas)
    canvas.view.set_zoom(zoom)
    canvas.view.centerOn(crossing)
    canvas.scene.select_representations((bus.representation_id,) if selected else ())
    canvas.scene.set_grid(visible=False)
    for item in canvas.scene._label_owners():
        item._label.hide()
    QTest.mouseMove(canvas.view.viewport(), QPoint(8,8))
    QApplication.processEvents()
    image = canvas.view.viewport().grab().toImage()
    position = canvas.view.viewportTransform().map(crossing)
    cx, cy = round(position.x()), round(position.y())
    rows = math.ceil(11*zoom + 20)
    runs = []
    for side in (-1, 1):
        longest = current = 0
        for distance in range(rows):
            y = cy + side*distance
            light = all(min(image.pixelColor(x,y).getRgb()[:3]) >= 240 for x in range(cx-1,cx+2))
            current = current+1 if light else 0
            longest = max(longest,current)
        runs.append(longest)
    assert min(runs) >= 2, f"Fully background-colored rows, upper/lower: {runs}; zoom={zoom}, selected={selected}"
    assert _state(canvas) == before
    assert not canvas.scene._route_items_by_id[next(iter(canvas.controller.diagram.routes))]._bridge_nodes


def test_zoom_invalidates_gap_once_without_recursive_label_relayout(canvas_factory, monkeypatch):
    canvas, bus, crossing = _crossing(canvas_factory)
    before = _state(canvas)
    count = 0
    original = canvas.scene._refresh_route_bridges
    def counted():
        nonlocal count
        count += 1
        assert count < 5, "Zoom-to-label refresh must not recursively refresh bridges"
        return original()
    monkeypatch.setattr(canvas.scene, "_refresh_route_bridges", counted)
    canvas.view.set_zoom(.6)
    assert count == 1
    canvas.view.set_zoom(1.4)
    assert count == 2
    canvas.view.set_zoom(1.4)
    assert count == 2
    assert _state(canvas) == before
