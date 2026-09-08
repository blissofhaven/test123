"""A node split allocates separate bus taps without moving existing taps."""
import pytest

from rza_calc.domain.diagram import RouteAnchorKind
from rza_calc.editor.controller import NodeTarget
from test_stage02_node_split import _fanout, _insert
from test_stage04_rotation_consistency import _share_segment


@pytest.mark.parametrize('physical', [False, True])
def test_retargeted_external_branches_get_distinct_bus_taps(physical):
    controller, bus, selected, _, _, _, unaffected, _ = _fanout(physical=physical)
    unchanged = controller.diagram.routes[unaffected.route_id]
    _insert(controller, selected.port_ids[0], NodeTarget(bus.node_id, bus.representation_id, '.25'))
    routes = list(controller.diagram.routes.values())
    taps = [anchor.anchor_key for route in routes
            for anchor in (route.start_anchor, route.end_anchor)
            if anchor.kind is RouteAnchorKind.BUS and anchor.representation_id == bus.representation_id]
    assert len(taps) == len(set(taps))
    assert all(not _share_segment(a, b) for i, a in enumerate(routes) for b in routes[i+1:])
    assert controller.diagram.routes[unaffected.route_id] == unchanged
