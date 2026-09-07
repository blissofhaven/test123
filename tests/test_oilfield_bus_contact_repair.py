"""All-sheet contact repair changes only the affected routes, not the model."""
from collections import defaultdict
from dataclasses import replace
from itertools import combinations
import math
from pathlib import Path

import pytest

from rza_calc.domain.diagram import RouteAnchorKind, RouteWaypointSource
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.io.project import load_project, save_project
import tools.oilfield_layout as layout


DEMO = Path(__file__).parents[1]/'rza_calc/examples/oilfield_gtes.json'


@pytest.fixture(scope='module')
def project():
    return load_project(DEMO)


@pytest.fixture
def original(project, monkeypatch):
    # Recreate the saved historical placement before the terminal repair pass.
    # This constructs fixture data in memory; it never writes the example.
    with monkeypatch.context() as patch:
        patch.setattr(layout, 'repair_bus_contact_geometry', lambda diagram, **_: diagram)
        return layout.build_layout(project, project.metadata['oilfield_demo'])


def bus_ends(diagram):
    rows = defaultdict(list)
    for route in diagram.routes.values():
        for start, anchor, point, adjacent in (
            (True, route.start_anchor, route.waypoints[0], route.waypoints[1]),
            (False, route.end_anchor, route.waypoints[-1], route.waypoints[-2])):
            if anchor.kind is RouteAnchorKind.BUS:
                rows[anchor.representation_id].append((route, start, point, adjacent))
    return rows


def shared_slots(diagram):
    return sum(1 for rows in bus_ends(diagram).values()
               for first, second in combinations(rows, 2)
               if math.hypot(first[2].x-second[2].x, first[2].y-second[2].y) < 1e-7)


def positive_overlaps(diagram):
    by_page_axis = defaultdict(list)
    for route in diagram.routes.values():
        for a, b in zip(route.waypoints, route.waypoints[1:]):
            horizontal = math.isclose(a.y,b.y,abs_tol=1e-8)
            key = (route.page_id, horizontal, round(a.y if horizontal else a.x, 8))
            lo, hi = sorted((a.x,b.x) if horizontal else (a.y,b.y))
            by_page_axis[key].append((lo,hi,route.id))
    return [(a[2],b[2]) for rows in by_page_axis.values() for a,b in combinations(rows,2)
            if a[2] != b[2] and min(a[1],b[1])-max(a[0],b[0]) > 1e-7]


def assert_clean_contacts(diagram):
    for bus_id, rows in bus_ends(diagram).items():
        bus = diagram.representations[bus_id]
        width = bus.extensions['stage3_graphics']['width']
        for _, _, point, adjacent in rows:
            assert point.y == pytest.approx(bus.y)
            assert bus.x-width/2 <= point.x <= bus.x+width/2
            # Section breaker leads reach the bus boundary along its axis.
            boundary = math.isclose(abs(point.x-bus.x),width/2,abs_tol=1e-7)
            if not boundary:
                assert adjacent.x == pytest.approx(point.x)
                assert abs(adjacent.y-point.y) >= 40-1e-7
        for first, second in combinations(rows,2):
            assert abs(first[2].x-second[2].x) >= 20-1e-7
    assert positive_overlaps(diagram) == []


def test_repair_is_bounded_to_128_routes_and_preserves_every_semantic_endpoint(original, project):
    before_fp = electrical_model_fingerprint(project.electrical_model)
    assert shared_slots(original) == 126
    assert len(positive_overlaps(original)) == 2
    repaired = layout.repair_bus_contact_geometry(original)
    assert_clean_contacts(repaired)
    changed = {key for key in original.routes if original.routes[key] != repaired.routes[key]}
    assert len(changed) == 128
    assert repaired.pages == original.pages
    assert repaired.representations == original.representations
    assert repaired.extensions == original.extensions
    assert set(repaired.routes) == set(original.routes)
    assert repaired.revision == original.revision+1
    moved_bus_ends = 0
    for identifier, old in original.routes.items():
        new = repaired.routes[identifier]
        assert new.id == old.id and new.equipment_id == old.equipment_id
        assert new.page_id == old.page_id and new.kind == old.kind
        assert new.electrical_node_id == old.electrical_node_id
        assert new.extensions == old.extensions
        assert new.waypoints[0].id == old.waypoints[0].id
        assert new.waypoints[-1].id == old.waypoints[-1].id
        for a,b in ((new.start_anchor,old.start_anchor),(new.end_anchor,old.end_anchor)):
            assert replace(a,anchor_key='') == replace(b,anchor_key='')
            moved_bus_ends += a.anchor_key != b.anchor_key
        if identifier not in changed:
            assert new is old
    assert moved_bus_ends == 126
    assert electrical_model_fingerprint(project.electrical_model) == before_fp
    assert not repaired.validate_targets(project.electrical_model)


def test_valid_axial_section_breaker_connections_are_untouched(original):
    repaired = layout.repair_bus_contact_geometry(original)
    axial = []
    for bus_id, rows in bus_ends(original).items():
        bus = original.representations[bus_id]
        half = bus.extensions['stage3_graphics']['width']/2
        for route, _, point, adjacent in rows:
            if adjacent.y == point.y == bus.y and abs(point.x-bus.x) == half:
                axial.append(route.id)
                assert repaired.routes[route.id] is route
    assert len(axial) == 136


def test_repair_is_deterministic_idempotent_and_refuses_pinned_bend_loss(original):
    first = layout.repair_bus_contact_geometry(original)
    assert layout.repair_bus_contact_geometry(original) == first
    assert layout.repair_bus_contact_geometry(first) is first
    changed = next(key for key in original.routes if original.routes[key] != first.routes[key])
    route = original.routes[changed]
    pinned = replace(route.waypoints[0],source=RouteWaypointSource.USER,pinned=True)
    routes = dict(original.routes)
    routes[changed] = replace(route,waypoints=(pinned,*route.waypoints[1:]))
    manual = replace(original,routes=routes)
    with pytest.raises(ValueError,match='Закреплённые изгибы'):
        layout.repair_bus_contact_geometry(manual)
    assert manual.routes[changed].waypoints[0] == pinned


def test_future_generator_and_saved_demo_have_independent_contacts(project):
    assert_clean_contacts(layout.build_layout(project,project.metadata['oilfield_demo']))
    assert_clean_contacts(project.diagram)


def test_repaired_diagram_roundtrip_retains_fingerprint_and_contact_ids(original, project, tmp_path):
    from copy import copy
    repaired_project = copy(project)
    repaired_project.diagram = layout.repair_bus_contact_geometry(original)
    before = electrical_model_fingerprint(project.electrical_model)
    output = tmp_path/'repaired-contacts.json'
    save_project(output,repaired_project)
    reopened = load_project(output)
    assert reopened.diagram == repaired_project.diagram
    assert electrical_model_fingerprint(reopened.electrical_model) == before
    assert_clean_contacts(reopened.diagram)
