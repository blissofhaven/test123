"""Independent bus attachments, shared preview geometry and explicit repair."""
from dataclasses import dataclass, replace
import json
import math

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument, DiagramPage, PageId, RouteWaypoint, RouteWaypointId,
)
from rza_calc.domain.electrical import ElectricalModel, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.bus_connections import BUS_ATTACHMENT_MERGE_TOLERANCE
from rza_calc.editor.controller import EditorCommandError, NodeTarget, ProjectEditorController
from rza_calc.editor.orientation import bus_anchor_geometry
from rza_calc.io.diagram import diagram_from_dict, diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_from_dict, electrical_model_to_dict


PAGE = PageId("page.spacing")
U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _setup(*, width=160, height=12, rotation=0):
    project = _Project(
        ElectricalModel.with_builtins("Spacing"),
        DiagramDocument.create("Spacing", (DiagramPage(PAGE, "Main"),)),
        ProjectCatalogSnapshots(),
    )
    controller = ProjectEditorController(project)
    bus = controller.add_electrical_node(
        "Bus", page_id=PAGE, symbol_key="busbar", width=width, height=height,
        rotation_deg=rotation, x=400, y=0, voltage_class_id=U10,
    )
    return controller, bus


def _load(controller, x):
    return controller.add_equipment(
        "builtin.load", f"Load {x}", page_id=PAGE, x=x, y=240,
        voltage_class_by_group={"main": U10},
    )


def _geometry(route):
    return tuple((point.x, point.y, point.pinned) for point in route.waypoints)


def _bus_point(controller, bus, fraction):
    representation = controller.diagram.representations[bus.representation_id]
    width, height = controller._equipment_symbol_size(representation, None)
    x, y, _ = bus_anchor_geometry(
        width=width, height=height, rotation=representation.rotation_deg,
        center_x=representation.x, center_y=representation.y, fraction=fraction,
    )
    return x, y


def _connect(controller, bus, apparatus, fraction=.5):
    representation = controller.diagram.representations[apparatus.representation_id]
    port = controller._port_anchor_geometry(controller.model, representation, apparatus.port_ids[0])
    x, y = _bus_point(controller, bus, fraction)
    points = [(port.x, port.y), (port.x, y), (x, y)]
    points = [point for index, point in enumerate(points) if not index or point != points[index - 1]]
    return controller.connect_port_to_node(
        apparatus.port_ids[0], bus.node_id, page_id=PAGE,
        source_representation_id=apparatus.representation_id,
        node_representation_id=bus.representation_id, target_anchor_key=str(fraction),
        route_waypoints=tuple(RouteWaypoint(RouteWaypointId.new(), *point) for point in points),
    )


def _save_coincident_fixture(controller, bus, routes, fraction=.5):
    """Emulate an already saved old drawing; never call the new allocator."""
    x, y = _bus_point(controller, bus, fraction)
    values = dict(controller.diagram.routes)
    for identifier in routes:
        route = values[identifier]
        values[identifier] = replace(
            route, end_anchor=replace(route.end_anchor, anchor_key=str(fraction)),
            waypoints=(*route.waypoints[:-1], replace(route.waypoints[-1], x=x, y=y)),
        )
    controller._project.diagram = replace(controller.diagram, routes=values)
    return ProjectEditorController(controller._project)


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
@pytest.mark.parametrize("vertical", (False, True))
def test_new_independent_taps_have_distinct_exact_bus_endpoints(rotation, vertical):
    controller, bus = _setup(width=12 if vertical else 160, height=160 if vertical else 12, rotation=rotation)
    first = _connect(controller, bus, _load(controller, 150))
    original = controller.diagram.routes[first.route_id]
    second = _connect(controller, bus, _load(controller, 650))
    allocated = controller.diagram.routes[second.route_id]
    assert controller.diagram.routes[first.route_id] == original
    a, b = original.waypoints[-1], allocated.waypoints[-1]
    assert math.hypot(a.x - b.x, a.y - b.y) >= 20 - 1e-8
    assert (b.x, b.y) == _bus_point(controller, bus, float(allocated.end_anchor.anchor_key))
    assert allocated.end_anchor.electrical_node_id == original.end_anchor.electrical_node_id == bus.node_id
    assert len(controller.model.connections) == 2
    assert not controller.diagram.validate_targets(controller.model)


@pytest.mark.parametrize("fraction", (0.0, 1.0))
def test_free_bus_ends_are_preserved_for_side_connections(fraction):
    controller, bus = _setup()
    connection = _connect(controller, bus, _load(controller, 150), fraction)
    route = controller.diagram.routes[connection.route_id]
    assert float(route.end_anchor.anchor_key) == fraction
    assert (route.waypoints[-1].x, route.waypoints[-1].y) == _bus_point(controller, bus, fraction)


def test_moving_coincident_endpoint_moves_only_selected_route_and_preview_matches_commit():
    controller, bus = _setup()
    first = _connect(controller, bus, _load(controller, 150))
    second = _connect(controller, bus, _load(controller, 650))
    controller = _save_coincident_fixture(controller, bus, (first.route_id, second.route_id))
    routes_before = dict(controller.diagram.routes)
    fingerprint = electrical_model_fingerprint(controller.model)
    ports = dict(controller.model.ports)
    preview = controller.preview_bus_attachment_move(first.route_id, at_start=False, fraction=.55)
    assert len(preview) == 1 and preview[0].id == first.route_id
    assert dict(controller.diagram.routes) == routes_before
    assert controller.move_bus_attachment(first.route_id, at_start=False, fraction=.55) == (first.route_id,)
    assert _geometry(controller.diagram.routes[first.route_id]) == _geometry(preview[0])
    assert controller.diagram.routes[second.route_id] == routes_before[second.route_id]
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert dict(controller.model.ports) == ports
    a, b = (controller.diagram.routes[identifier].waypoints[-1] for identifier in (first.route_id, second.route_id))
    # Проверяется правило, а не число. 31.08.2026 раздвижка сменилась: точка
    # больше не отодвигается на 20 единиц от каждой соседней, а только
    # перестаёт сливаться с ней. Константа берётся из модуля, чтобы проверка
    # не разошлась с правилом; отдельная строка ниже запрещает обнулить её
    # и тем самым тихо разрешить двум точкам нарисоваться друг на друге.
    assert BUS_ATTACHMENT_MERGE_TOLERANCE >= 10.0
    assert math.hypot(a.x - b.x, a.y - b.y) >= BUS_ATTACHMENT_MERGE_TOLERANCE - 1e-8


def test_reconnect_allocates_one_free_bus_position_and_preserves_other_route():
    controller, bus = _setup()
    first = _connect(controller, bus, _load(controller, 150))
    other_bus = controller.add_electrical_node("Other", page_id=PAGE, x=900, y=0, symbol_key="busbar", voltage_class_id=U10)
    apparatus = _load(controller, 650)
    old = _connect(controller, other_bus, apparatus)
    untouched = controller.diagram.routes[first.route_id]
    result = controller.reconnect_port(
        apparatus.port_ids[0], NodeTarget(bus.node_id, representation_id=bus.representation_id, anchor_key=".5"),
        page_id=PAGE, source_representation_id=apparatus.representation_id,
    )
    route = controller.diagram.routes[result.route_id]
    assert old.route_id not in controller.diagram.routes
    assert controller.diagram.routes[first.route_id] == untouched
    assert float(route.end_anchor.anchor_key) != .5
    assert (route.waypoints[-1].x, route.waypoints[-1].y) == _bus_point(controller, bus, float(route.end_anchor.anchor_key))
    assert controller.model.connection_for_port(apparatus.port_ids[0]).electrical_node_id == bus.node_id


def test_full_bus_rejects_new_connection_atomically_without_losing_port():
    # Ширина уменьшена с 20 до 10 единиц вместе со сменой правила раздвижки
    # 31.08.2026: точка больше не отодвигается от соседней на 20 единиц, а
    # сдвигается, только если попала в занятое место (ближе 10 единиц). Шина
    # шириной 20 единиц с этим правилом уже НЕ переполнена — между её концами
    # помещается третья точка ровно в 10 единицах от каждой. Изменён размер
    # шины в фикстуре, а не проверяемое утверждение: проверяется по-прежнему
    # атомарный отказ переполненной шины с тем же сообщением.
    controller, bus = _setup(width=10)
    _connect(controller, bus, _load(controller, 150), 0)
    _connect(controller, bus, _load(controller, 650), 1)
    third = _load(controller, 950)
    before_model, before_diagram = electrical_model_fingerprint(controller.model), controller.diagram
    history = len(controller.journal)
    with pytest.raises(EditorCommandError, match="Удлините шину"):
        _connect(controller, bus, third)
    assert electrical_model_fingerprint(controller.model) == before_model
    assert controller.diagram == before_diagram
    assert controller.model.connection_for_port(third.port_ids[0]) is None
    assert len(controller.journal) == history


def test_explicit_separation_preserves_unique_manual_positions_and_undo_json_ids():
    controller, bus = _setup(width=200)
    rows = [_connect(controller, bus, _load(controller, x), fraction).route_id
            for x, fraction in ((50, .5), (150, .5), (650, .5), (850, .9))]
    controller = _save_coincident_fixture(controller, bus, rows[:3])
    first_stable = min(rows[:3], key=lambda key: key.value)
    before = controller.diagram
    fingerprint = electrical_model_fingerprint(controller.model)
    history = len(controller.journal)
    changed = controller.separate_bus_attachments(PAGE)
    assert set(changed) == set(rows[:3]) - {first_stable}
    assert controller.diagram.routes[first_stable] == before.routes[first_stable]
    assert controller.diagram.routes[rows[-1]] == before.routes[rows[-1]]
    assert electrical_model_fingerprint(controller.model) == fingerprint
    assert len(controller.journal) == history + 1
    endpoints = [controller.diagram.routes[key].waypoints[-1] for key in rows]
    assert all(math.hypot(a.x - b.x, a.y - b.y) >= 20 - 1e-8
               for index, a in enumerate(endpoints) for b in endpoints[index + 1:])
    after = controller.diagram
    assert controller.separate_bus_attachments(PAGE) == ()
    assert controller.diagram == after
    controller.undo()
    assert controller.diagram.routes == before.routes
    assert electrical_model_fingerprint(controller.model) == fingerprint
    controller.redo()
    assert controller.diagram.routes == after.routes
    restored_model = electrical_model_from_dict(json.loads(json.dumps(electrical_model_to_dict(controller.model))))
    restored_diagram = diagram_from_dict(json.loads(json.dumps(diagram_to_dict(controller.diagram))), restored_model)
    assert restored_diagram == controller.diagram
    assert electrical_model_fingerprint(restored_model) == fingerprint


def test_distinct_old_manual_taps_are_not_moved_even_if_closer_than_new_spacing():
    controller, bus = _setup(width=200)
    first = _connect(controller, bus, _load(controller, 150), .5)
    second = _connect(controller, bus, _load(controller, 650), .7)
    controller = _save_coincident_fixture(controller, bus, (second.route_id,), .55)
    before = controller.diagram
    assert controller.separate_bus_attachments() == ()
    assert controller.diagram == before
