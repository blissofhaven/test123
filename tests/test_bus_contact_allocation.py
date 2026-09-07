"""The allocated fraction and its visible endpoint must be one decision."""
from dataclasses import FrozenInstanceError
import math

import pytest

from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.bus_contacts import allocate_bus_contact
from rza_calc.editor.orientation import bus_anchor_geometry
from test_bus_connection_spacing import _connect, _load, _setup


@pytest.mark.parametrize('rotation', (0, 90, 180, 270))
@pytest.mark.parametrize('vertical', (False, True))
def test_allocated_contact_is_distinct_and_exact_for_every_bus_orientation(rotation, vertical):
    width, height = (12, 160) if vertical else (160, 12)
    controller, bus = _setup(width=width, height=height, rotation=rotation)
    first = _connect(controller, bus, _load(controller, 150))
    diagram = controller.diagram
    model = electrical_model_fingerprint(controller.model)
    rep = diagram.representations[bus.representation_id]
    result = allocate_bus_contact(diagram, rep, width=width, height=height, requested=.5)
    x, y, direction = bus_anchor_geometry(width=width, height=height, rotation=rotation,
        center_x=rep.x, center_y=rep.y, fraction=result.fraction)
    old = diagram.routes[first.route_id].waypoints[-1]
    assert (result.x, result.y, result.direction) == (x, y, direction)
    assert math.hypot(result.x-old.x, result.y-old.y) == pytest.approx(10)
    assert result.fraction == pytest.approx(.5-10/160)
    assert controller.diagram is diagram
    assert electrical_model_fingerprint(controller.model) == model
    with pytest.raises(FrozenInstanceError):
        result.fraction = .5


def test_free_point_and_explicitly_excluded_own_route_are_preserved():
    controller, bus = _setup()
    first = _connect(controller, bus, _load(controller, 150))
    rep = controller.diagram.representations[bus.representation_id]
    own = allocate_bus_contact(controller.diagram, rep, width=160, height=12,
        requested=.5, exclude_route_ids=(first.route_id,))
    assert own.fraction == .5
    free = allocate_bus_contact(controller.diagram, rep, width=160, height=12, requested=.7)
    assert free.fraction == .7
    bulk = allocate_bus_contact(controller.diagram, rep, width=160, height=12,
        requested=.5, merge_tolerance=20)
    assert bulk.fraction == .375


@pytest.mark.parametrize('field,value', [('width', 0), ('height', float('nan')),
    ('requested', True), ('merge_tolerance', -1)])
def test_invalid_contact_geometry_is_rejected(field, value):
    controller, bus = _setup()
    args = dict(width=160, height=12, requested=.5)
    args[field] = value
    with pytest.raises(ValueError):
        allocate_bus_contact(controller.diagram, controller.diagram.representations[bus.representation_id], **args)
