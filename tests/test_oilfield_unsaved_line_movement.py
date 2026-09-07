"""Recreate the user's unsaved QF + draft cable experiment on the oilfield."""
from pathlib import Path
import hashlib

from rza_calc.domain.electrical import DataConfirmation, LineKind, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import NodeTarget, PhysicalLineInput, PortTarget, ProjectEditorController
from rza_calc.io.project import load_project, save_project


def test_new_qf_cable_returns_to_a_short_connected_route_after_left_right_move(tmp_path):
    source = Path(__file__).resolve().parents[1] / "rza_calc/examples/oilfield_gtes.json"
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    controller = ProjectEditorController(load_project(source))
    bus = next(row for row in controller.diagram.representations.values()
               if row.label == "ЦП-3 Юг 110/35 кВ, 35 кВ, 1 СШ" and row.symbol_key == "busbar")
    voltage = VoltageClassId("builtin.voltage.ac.35kv")
    qf = controller.add_equipment("builtin.circuit_breaker", "Пробный выключатель",
        x=-1080, y=800, rotation_deg=90, page_id=bus.page_id,
        voltage_class_by_group={"main": voltage})
    line = controller.create_physical_line("Пробная КЛ", LineKind.CABLE,
        NodeTarget(bus.electrical_node_id, bus.id, "0.08"),
        PortTarget(qf.port_ids[0], qf.representation_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED), page_id=bus.page_id)
    start = controller.diagram.routes[line.route_id]
    fingerprint = electrical_model_fingerprint(controller.model)
    for dx in (-80, 160, -80):
        controller.move_representations((qf.representation_id,), dx, 0, bypass_snap=True)
        current = controller.diagram.routes[line.route_id]
        assert current.start_anchor == start.start_anchor
        assert current.end_anchor == start.end_anchor
        assert electrical_model_fingerprint(controller.model) == fingerprint
        assert all(not point.pinned for point in current.waypoints)
        controller.diagram.require_valid_targets(controller.model)
    result = controller.diagram.routes[line.route_id]
    assert len(result.waypoints) == 2
    assert result.waypoints[0] == start.waypoints[0]
    assert result.waypoints[-1] == start.waypoints[-1]
    destination = tmp_path / "experiment.json"
    save_project(destination, controller._project)
    restored = load_project(destination)
    assert restored.diagram.routes[line.route_id] == result
    assert electrical_model_fingerprint(restored.electrical_model) == fingerprint
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
