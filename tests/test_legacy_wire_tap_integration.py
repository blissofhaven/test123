"""Real oilfield controller taps preserve every saved view and protection data."""
from pathlib import Path

import pytest

from rza_calc.core.engine import run
from rza_calc.domain.catalog import CatalogCategoryId, CatalogEntry, CatalogEntryId, CatalogOrigin
from rza_calc.domain.diagram import DiagramRouteKind, RouteAnchorKind
from rza_calc.domain.electrical import DataConfirmation, EquipmentTypeId, LineKind, thaw_json
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import EditorCommandError, NodeTarget, PhysicalLineInput, ProjectEditorController
from rza_calc.editor.history import ProjectMemento, UserCatalogMemento
from rza_calc.editor.legacy_line_split import REVIEW_MARKER, SPLIT_MARKER
from rza_calc.io.project import load_project, save_project


OILFIELD = Path(__file__).parents[1] / "rza_calc/examples/oilfield_gtes.json"
REVIEW_CODE = "line_protection_zone_review_required"


def _state(project):
    return (electrical_model_fingerprint(project.electrical_model), project.diagram,
            project.catalog_snapshots, UserCatalogMemento.capture(project.user_catalog))


def _setup(legacy_id, operation):
    project = load_project(OILFIELD)
    controller = ProjectEditorController(project)
    equipment = next(row for row in controller.model.equipment.values()
                     if row.extensions.get("legacy_calculation", {}).get("legacy_id") == legacy_id)
    views = tuple(route for route in controller.diagram.routes.values()
                  if route.equipment_id == equipment.id)
    assert len(views) == (2 if legacy_id == "cp01_in1_line" else 1)
    assert len({route.page_id for route in views}) == len(views)
    selected = views[-1]  # The second, independently positioned cp01 view.
    first, last = selected.waypoints[0], selected.waypoints[-1]
    x, y = first.x + (last.x - first.x) * .37, first.y + (last.y - first.y) * .37
    old_node = controller.model.node_for_port(equipment.port_ids[0])
    source = None
    if operation != "port":
        node = controller.add_electrical_node("Узел проверочной отпайки", x=x + 200, y=y,
            voltage_class_id=old_node.declared_voltage_class_id, page_id=selected.page_id)
        source = NodeTarget(node.node_id, node.representation_id)
    else:
        assert selected.end_anchor.kind is RouteAnchorKind.EQUIPMENT_PORT
        source = selected.end_anchor.target_port_id
        assert source is not None and controller.model.connection_for_port(source) is not None
    return project, controller, equipment, views, source, (x, y)


def _entry():
    return CatalogEntry(CatalogEntryId.new(), CatalogOrigin.USER, CatalogCategoryId("lines"),
        "Тестовая проектная КЛ", EquipmentTypeId("builtin.line_section.cable"), 1,
        properties={"conductor_mark": "Тестовая проектная КЛ", "r1_ohm_per_km": .23,
                    "x1_ohm_per_km": .08}, source="Данные независимой регрессии")


def _perform(controller, equipment, views, source, point, operation, *, entry=None, bad=False):
    selected = views[-1]
    x, y = point
    if bad:
        # A late geometry failure, after draft splitting and catalog proposal.
        x += 3
        y += 3
    args = dict(page_id=selected.page_id, tap_route_id=selected.id, tap_x=x, tap_y=y)
    offset = 1_000_000 if equipment.properties["legacy_payload"]["length_km"] > 1 else 50_000
    if operation == "node":
        return controller.connect_node_to_tap(source, equipment.id, offset, **args)
    if operation == "port":
        return controller.reconnect_port_to_tap(source, equipment.id, offset,
            source_representation_id=selected.end_anchor.representation_id, **args)
    return controller.create_tap(equipment.id, offset, "Проверочная отходящая КЛ", LineKind.CABLE,
        source, physical=PhysicalLineInput(120_000, DataConfirmation.CONFIRMED,
            {"parallel_count": 2}, DataConfirmation.CONFIRMED),
        catalog_entry=entry, remember_catalog_entry=True, **args)


def _assert_all_views(controller, result, old_views, point, moved_port):
    model, diagram = controller.model, controller.diagram
    pieces = [row for row in diagram.routes.values()
              if row.equipment_id in {result.first_section_id, result.second_section_id}]
    assert len(pieces) == 2 * len(old_views)
    for old in old_views:
        first = diagram.routes[old.id]
        second, = (row for row in pieces if row.page_id == old.page_id
                   and row.equipment_id == result.second_section_id)
        assert first.equipment_id == result.first_section_id
        assert first.page_id == old.page_id
        assert first.extensions == second.extensions == old.extensions
        assert first.start_anchor.electrical_node_id == old.start_anchor.electrical_node_id
        assert second.end_anchor.electrical_node_id == old.end_anchor.electrical_node_id
        assert first.end_anchor.electrical_node_id == second.start_anchor.electrical_node_id == result.tap_node_id
        assert first.end_anchor.representation_id == second.start_anchor.representation_id
        expected = point if old.id == old_views[-1].id else (
            (old.waypoints[0].x + old.waypoints[-1].x) / 2,
            (old.waypoints[0].y + old.waypoints[-1].y) / 2)
        assert (first.waypoints[-1].x, first.waypoints[-1].y) == pytest.approx(expected)
        assert (second.waypoints[0].x, second.waypoints[0].y) == pytest.approx(expected)
        assert first.waypoints[0] == old.waypoints[0]
        if moved_port is None:
            assert second.waypoints[-1] == old.waypoints[-1]
        for route in (first, second):
            assert route.kind is DiagramRouteKind.EQUIPMENT_BRANCH
            for role, anchor in (("from", route.start_anchor), ("to", route.end_anchor)):
                assert anchor.branch_port_id == model.port_by_role(route.equipment_id, role).id
                assert model.node_for_port(anchor.branch_port_id).id == anchor.electrical_node_id
                assert diagram.representations[anchor.representation_id].page_id == route.page_id
                if anchor.target_port_id is not None:
                    assert model.node_for_port(anchor.target_port_id).id == anchor.electrical_node_id
        if moved_port is not None:
            # Both views must stop claiming the moved QF/load terminal is still
            # connected to the retained far end of the original conductor.
            assert second.end_anchor.target_port_id is None
            assert second.end_anchor.kind is RouteAnchorKind.ELECTRICAL_NODE
            tail = second.waypoints[-1]
            assert (tail.x, tail.y) != (old.waypoints[-1].x, old.waypoints[-1].y), (
                "Disconnected tail still touches the moved equipment terminal", old.page_id)
            for other in diagram.routes.values():
                if (other.page_id != second.page_id or other.kind is not DiagramRouteKind.NODE_CONNECTION
                        or other.electrical_node_id == second.end_anchor.electrical_node_id):
                    continue
                for a, b in zip(other.waypoints, other.waypoints[1:]):
                    on_segment = (min(a.x, b.x) - 1e-7 <= tail.x <= max(a.x, b.x) + 1e-7
                        and min(a.y, b.y) - 1e-7 <= tail.y <= max(a.y, b.y) + 1e-7
                        and abs((tail.x - a.x) * (b.y - a.y) - (tail.y - a.y) * (b.x - a.x)) < 1e-7)
                    assert not on_segment, "Disconnected tail visibly meets a wire belonging to another node"
        else:
            assert second.end_anchor.target_port_id == old.end_anchor.target_port_id
            assert second.end_anchor.representation_id == old.end_anchor.representation_id
        assert first.start_anchor == old.start_anchor
    assert not diagram.validate_targets(model)
    assert not [issue for issue in model.validate_integrity() if issue.severity == "error"]


@pytest.mark.parametrize("legacy_id", ["cp01_in1_line", "ktp01_f1_1_line"])
@pytest.mark.parametrize("operation", ["node", "port", "physical"])
def test_real_legacy_tap_all_views_atomic_roundtrip_and_protection(legacy_id, operation, tmp_path):
    project, controller, equipment, views, source, point = _setup(legacy_id, operation)
    before = _state(project)
    old_states = dict(controller.model.operating_states)
    old_connections = dict(controller.model.connections)
    old_payload = thaw_json(equipment.properties["legacy_payload"])
    old_count = len(controller.model.equipment)
    old_native_ids = set(controller.model.line_sections)
    original_to = controller.model.node_for_port(controller.model.port_by_role(equipment.id, "to").id).id
    source_connection = controller.model.connection_for_port(source) if operation == "port" else None
    entry = _entry() if operation == "physical" else None
    journal = len(controller.journal)

    result = _perform(controller, equipment, views, source, point, operation, entry=entry)
    assert len(controller.journal) == journal + 1
    assert result.main_logical_line_id is None
    assert result.first_section_id == result.removed_section_id == equipment.id
    assert len(controller.model.equipment) == old_count + (2 if operation == "physical" else 1)
    first = controller.model.equipment[equipment.id]
    second = controller.model.equipment[result.second_section_id]
    assert first.port_ids == equipment.port_ids
    assert set(old_connections) <= set(controller.model.connections)
    moved_ports = {controller.model.port_by_role(equipment.id, "to").id}
    if operation == "port":
        moved_ports.add(source)
    for key, connection in old_connections.items():
        if connection.port_id not in moved_ports:
            assert controller.model.connections[key] == connection
    for key, value in old_payload.items():
        if key != "length_km":
            assert thaw_json(first.properties["legacy_payload"][key]) == value
    assert first.properties["legacy_payload"]["length_km"] + second.properties["legacy_payload"]["length_km"] == pytest.approx(old_payload["length_km"])
    assert second.properties["legacy_payload"]["ct_ratio"] is None
    assert not any(second.properties["legacy_payload"]["prot"][key] for key in ("mtz", "to", "ozz"))
    assert first.extensions[SPLIT_MARKER]["original_to_node_id"] == original_to.value
    assert first.extensions[SPLIT_MARKER]["original_length_km"] == old_payload["length_km"]
    assert thaw_json(first.extensions[SPLIT_MARKER]["original_properties"]) == thaw_json(equipment.properties)
    assert controller.model.operating_states == old_states  # All five existing modes.
    if operation == "node":
        assert result.tap_node_id == source.node_id
    elif operation == "port":
        moved = controller.model.connection_for_port(source)
        assert moved.id == source_connection.id
        assert moved.electrical_node_id == result.tap_node_id
        assert moved.electrical_node_id != source_connection.electrical_node_id
    else:
        assert set(controller.model.line_sections) == old_native_ids | {result.branch_section_id}
        assert project.user_catalog.get(entry.id) == entry
        binding = project.catalog_snapshots.bindings[result.branch_section_id]
        assert binding is not None
        branch = controller.model.line_sections[result.branch_section_id]
        assert branch.length_mm == 120_000
        props = controller.model.effective_equipment_properties(result.branch_section_id)
        assert props["r1_ohm_per_km"] == .23 and props["parallel_count"] == 2
    if operation != "physical":
        assert set(controller.model.line_sections) == old_native_ids
    _assert_all_views(controller, result, views, point, source if operation == "port" else None)
    for key, row in before[1].representations.items():
        assert controller.diagram.representations[key] == row
    old_view_ids = {row.id for row in views}
    for key, route in before[1].routes.items():
        moved_lead = (operation == "port" and route.kind is DiagramRouteKind.NODE_CONNECTION
                      and any(anchor.target_port_id == source
                              for anchor in (route.start_anchor, route.end_anchor)))
        if key not in old_view_ids and not moved_lead:
            assert controller.diagram.routes[key] == route

    protected = legacy_id == "cp01_in1_line"
    blockers = [row for row in project.calculation_blockers if row.code == REVIEW_CODE]
    assert bool(blockers) is protected
    if protected:
        assert first.extensions[REVIEW_MARKER]["required"] is True
        assert first.extensions[REVIEW_MARKER]["original_to_node_id"] == original_to.value
        assert first.extensions[REVIEW_MARKER]["original_length_km"] == old_payload["length_km"]
        live_node_ids = {key.value for key in controller.model.electrical_nodes}
        assert set(first.extensions[REVIEW_MARKER]["split_node_ids"]) <= live_node_ids
        assert result.tap_node_id.value in first.extensions[REVIEW_MARKER]["split_node_ids"]
        assert blockers[0].object_id == equipment.id.value
        with pytest.raises(ValueError, match="зону защиты"):
            project.require_calculation_ready()
        with pytest.raises(ValueError, match="зону защиты"):
            run(project.network, project.methodology)
    else:
        assert REVIEW_MARKER not in first.extensions
        project.require_calculation_ready()

    committed = _state(project)
    path = tmp_path / "real-oilfield-tap.json"
    save_project(path, project)
    restored = load_project(path)
    assert _state(restored) == committed
    assert bool([row for row in restored.calculation_blockers if row.code == REVIEW_CODE]) is protected
    assert not restored.diagram.validate_targets(restored.electrical_model)
    controller.undo()
    assert _state(project) == before
    assert dict(controller.model.connections) == old_connections
    assert not any(row.code == REVIEW_CODE for row in project.calculation_blockers)
    controller.redo()
    assert _state(project) == committed
    _assert_all_views(controller, result, views, point, source if operation == "port" else None)


@pytest.mark.parametrize("operation", ["node", "port", "physical"])
def test_real_legacy_late_geometry_rejection_rolls_back_every_scope(operation):
    project, controller, equipment, views, source, point = _setup("cp01_in1_line", operation)
    before = ProjectMemento.capture(project)
    history = tuple(controller.journal)
    entry = _entry() if operation == "physical" else None
    with pytest.raises(EditorCommandError, match="не лежит"):
        _perform(controller, equipment, views, source, point, operation, entry=entry, bad=True)
    assert ProjectMemento.capture(project) == before
    assert tuple(controller.journal) == history
    assert REVIEW_MARKER not in controller.model.equipment[equipment.id].extensions
    if entry is not None:
        assert entry.id not in project.user_catalog.entries
