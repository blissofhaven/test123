"""Two-position route geometry must preserve ports and avoid their own bodies."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.electrical import ElectricalModel, VoltageClassId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor import ProjectEditorController
from rza_calc.editor.orientation import port_anchor_by_id


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


@pytest.mark.parametrize("type_id", (
    "builtin.circuit_breaker", "builtin.transformer_2w", "builtin.transformer_3w",
))
@pytest.mark.parametrize("angle", (90, 180))
def test_all_terminals_keep_identity_and_outward_route_after_rotation(type_id, angle):
    page = DiagramPage(PageId("page.route-rotation"), "Схема")
    controller = ProjectEditorController(_Project(
        ElectricalModel.with_builtins("Поворот"),
        DiagramDocument.create("Схема", (page,)), ProjectCatalogSnapshots(),
    ))
    added = controller.add_equipment(
        type_id, "Аппарат", x=200, y=200, width=100, height=60,
        rotation_deg=180 if angle == 90 else 90,
        voltage_class_by_group={"main": VoltageClassId("builtin.voltage.ac.10kv")}
        if type_id == "builtin.circuit_breaker" else {
            "hv": VoltageClassId("builtin.voltage.ac.110kv"),
            "lv": VoltageClassId("builtin.voltage.ac.10kv"),
            **({"mv": VoltageClassId("builtin.voltage.ac.35kv")} if type_id.endswith("3w") else {}),
        },
    )
    route_ids = []
    for index, port_id in enumerate(added.port_ids):
        node = controller.add_electrical_node(
            f"Узел {index}", x=-200, y=-200 + index * 250,
            voltage_class_id=controller.model.port_voltage_class(port_id),
        )
        result = controller.connect_port_to_node(port_id, node.node_id)
        route_ids.append(result.route_id)
    before = (
        electrical_model_fingerprint(controller.model),
        controller.model.revision, dict(controller.model.ports),
        dict(controller.model.connections), dict(controller.model.electrical_nodes),
    )
    history_count = len(controller.journal)
    original = {identifier: controller.diagram.routes[identifier] for identifier in route_ids}
    controller.rotate_representation(added.representation_id, angle)
    assert len(controller.journal) == history_count + 1
    assert before == (
        electrical_model_fingerprint(controller.model),
        controller.model.revision, dict(controller.model.ports),
        dict(controller.model.connections), dict(controller.model.electrical_nodes),
    )
    equipment = controller.model.equipment[added.equipment_id]
    definition = controller.model.equipment_type(equipment.type_id, equipment.type_version)
    width, height = (60, 100) if angle == 90 else (100, 60)
    left, right = 200 - width / 2, 200 + width / 2
    top, bottom = 200 - height / 2, 200 + height / 2
    for port_id, route_id in zip(added.port_ids, route_ids):
        route = controller.diagram.routes[route_id]
        assert route.start_anchor == original[route_id].start_anchor
        assert route.end_anchor == original[route_id].end_anchor
        anchor = port_anchor_by_id(
            equipment, definition, port_id, width=100, height=60,
            rotation=angle, center_x=200, center_y=200,
        )
        first, second = route.waypoints[:2]
        assert (first.x, first.y) == (anchor.x, anchor.y)
        dx, dy = second.x - first.x, second.y - first.y
        outward = {
            "left": dx < 0 and dy == 0, "right": dx > 0 and dy == 0,
            "up": dy < 0 and dx == 0, "down": dy > 0 and dx == 0,
        }
        assert outward[anchor.direction.value]
        for first, second in zip(route.waypoints, route.waypoints[1:]):
            assert (first.x == second.x) != (first.y == second.y)
            if first.x == second.x:
                assert not (
                    left < first.x < right
                    and max(min(first.y, second.y), top) < min(max(first.y, second.y), bottom)
                )
            else:
                assert not (
                    top < first.y < bottom
                    and max(min(first.x, second.x), left) < min(max(first.x, second.x), right)
                )
    controller.undo()
    assert {identifier: controller.diagram.routes[identifier] for identifier in route_ids} == original


@pytest.mark.parametrize("shift", (False, True))
def test_preview_key_toggles_actual_auto_oriented_axis(shift):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from test_ui_interaction_gui import _app, _controller, _physical_line, _show_canvas

    _app()
    controller = _controller("actual-auto-axis")
    _physical_line(controller, (0, 100), (400, 100))
    canvas = _show_canvas(controller)
    try:
        canvas.view.begin_placement({
            "target_kind": "equipment", "type_id": "builtin.recloser", "name": "Реклоузер",
        })
        canvas.view._cursor_scene_pos = QPointF(200, 100)
        canvas.view._update_equipment_drop_feedback(
            canvas.view._placement_payload, QPointF(200, 100),
        )
        assert canvas.view._placement_rotation_deg == 180
        before = electrical_model_fingerprint(controller.model), controller.model.revision, tuple(controller.journal)
        QTest.keyClick(canvas.view, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier)
        QApplication.processEvents()
        assert canvas.view._placement_rotation_deg == 90
        assert canvas.view.tool_state.preview_rotation_deg == 90
        assert canvas.view._placement_payload["graphics"]["orientation_mode"] == "manual"
        assert (electrical_model_fingerprint(controller.model), controller.model.revision, tuple(controller.journal)) == before
    finally:
        canvas.close()
