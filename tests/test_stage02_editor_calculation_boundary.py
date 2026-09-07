"""Editor commands preserve the Stage 1 boundary between drawing and calculation."""
from __future__ import annotations

import pytest

from rza_calc.domain.electrical import DataConfirmation, LineKind, thaw_json
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import NodeTarget, PhysicalLineInput, PortTarget
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import save_project
from test_stage01_result_freshness import opened


@pytest.mark.parametrize("kind", (LineKind.OVERHEAD, LineKind.CABLE))
def test_inserting_unconfirmed_line_invalidates_snapshot_and_roundtrips_without_invented_data(tmp_path, kind):
    vm, controller = opened()
    snapshot = vm.require_current_result()
    fault_before = vm.fault_rows()[0].iabc_ka
    fingerprint_before = electrical_model_fingerprint(controller.model)
    equipment = next(row for row in controller.model.equipment.values()
                     if thaw_json(row.properties.get("legacy_payload", {})).get("kind") == "line")
    source = controller.model.port_by_role(equipment.id, "from")
    original_node = controller.model.connection_for_port(source.id).electrical_node_id
    connections_before = dict(controller.model.connections)
    journal_before = len(controller.journal)

    inserted = controller.create_physical_line(
        "Новый участок без паспортных данных", kind,
        PortTarget(source.id), NodeTarget(original_node),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED),
        split_shared_node=True,
    )

    assert len(controller.journal) == journal_before + 1
    assert controller.model.connection_for_port(source.id).electrical_node_id != original_node
    for connection_id, connection in connections_before.items():
        if connection.port_id != source.id:
            assert controller.model.connections[connection_id] == connection
    controller.diagram.require_valid_targets(controller.model)
    section = controller.model.line_sections[inserted.section_id]
    assert section.length_mm is None
    assert vm.current_result is None
    assert all(row.iabc_ka is None for row in vm.fault_rows())
    assert not vm.recalculate(), "Drawing length must not turn into an electrical length"

    path = tmp_path / "inserted-line.json"
    save_project(path, vm.project, methodology_file=str(vm.project.methodology.path))
    reopened = ProjectViewModel.open(path)
    assert reopened.current_result is None
    assert reopened.project.electrical_model.line_sections[inserted.section_id].length_mm is None
    assert reopened.project.electrical_model.connectivity_signature() == controller.model.connectivity_signature()
    reopened.project.diagram.require_valid_targets(reopened.project.electrical_model)

    # A failed recalculation deliberately clears the old current answer. Undo
    # must restore the exact inputs, after which a new run restores its numbers.
    controller.undo()
    assert electrical_model_fingerprint(controller.model) == fingerprint_before
    assert snapshot.is_current_for(vm.net, vm.project.methodology)
    assert vm.recalculate()
    assert vm.fault_rows()[0].iabc_ka == pytest.approx(fault_before)
    controller.redo()
    assert vm.current_result is None
    assert all(row.iabc_ka is None for row in vm.fault_rows())


def test_geometry_only_route_move_preserves_four_fault_result_and_electrical_identity():
    vm, controller = opened()
    snapshot = vm.require_current_result()
    route = next(row for row in controller.diagram.routes.values() if len(row.waypoints) >= 2)
    identity_before = electrical_model_fingerprint(controller.model)
    connections_before = dict(controller.model.connections)
    results_before = {}
    for fault in ("3ph", "2ph", "1ph_g", "2ph_g"):
        vm.select_fault_type(fault)
        results_before[fault] = vm.fault_rows()[0].iabc_ka
    controller.move_route_segment(route.id, 0, dx=40, dy=60)
    moved = controller.diagram.routes[route.id]
    assert moved.waypoints != route.waypoints
    assert moved.start_anchor == route.start_anchor and moved.end_anchor == route.end_anchor
    assert electrical_model_fingerprint(controller.model) == identity_before
    assert dict(controller.model.connections) == connections_before
    for fault, expected in results_before.items():
        vm.select_fault_type(fault)
        assert vm.current_result is snapshot
        assert vm.fault_rows()[0].iabc_ka == expected
    controller.undo()
    controller.redo()
    assert vm.current_result is snapshot
