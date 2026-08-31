# -*- coding: utf-8 -*-
"""Контракт Qt-независимой collision-модели этапа UI-UX-1."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
)
from rza_calc.domain.electrical import (
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentInstance,
    EquipmentTypeId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor.collision import (
    CollisionCategory,
    CollisionKind,
    DiagramCollisionService,
    OrientedRectangle,
    geometry_for_equipment_preview,
    geometry_for_representation,
)
from rza_calc.editor.validation import (
    ProjectDiagnosticSeverity,
    ProjectValidationService,
)


PAGE = PageId("page.ui.collision")


@dataclass
class _Fixture:
    model: ElectricalModel
    representations: list[GraphicalRepresentation]

    @classmethod
    def create(cls) -> "_Fixture":
        return cls(ElectricalModel.with_builtins("Коллизии UI"), [])

    def equipment(
        self,
        type_id: str,
        name: str,
        *,
        token: str,
        x: float,
        y: float,
        width: float = 80.0,
        height: float = 50.0,
        rotation_deg: float = 0.0,
        placed: bool = True,
        page_id: PageId = PAGE,
    ) -> tuple[EquipmentInstance, GraphicalRepresentation]:
        properties = {"rated_current_a": 630.0} if type_id == "builtin.recloser" else None
        equipment, _ = self.model.create_equipment(
            type_id,
            name,
            properties=properties,
            voltage_class_by_group=(
                {"main": VoltageClassId("builtin.voltage.ac.10kv")}
                if type_id == "builtin.recloser"
                else None
            ),
            normal_position=(
                SwitchPosition.CLOSED
                if type_id == "builtin.recloser"
                else None
            ),
        )
        representation = GraphicalRepresentation(
            GraphicalRepresentationId(f"representation.ui.collision.{token}"),
            page_id,
            RepresentationTargetKind.EQUIPMENT,
            equipment_id=equipment.id,
            x=x,
            y=y,
            rotation_deg=rotation_deg,
            symbol_key=type_id,
            label=name,
            extensions={
                "stage3_graphics": {
                    "width": width,
                    "height": height,
                }
            },
        )
        if placed:
            self.representations.append(representation)
        return equipment, representation

    def node(
        self,
        name: str,
        *,
        token: str,
        x: float,
        y: float,
        symbol_key: str = "electrical_node",
        width: float = 24.0,
        height: float = 24.0,
        placed: bool = True,
    ) -> GraphicalRepresentation:
        node = ElectricalNode(
            ElectricalNodeId(f"node.ui.collision.{token}"),
            name,
        )
        self.model.add_node(node)
        representation = GraphicalRepresentation(
            GraphicalRepresentationId(f"representation.ui.collision.{token}"),
            PAGE,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node.id,
            x=x,
            y=y,
            symbol_key=symbol_key,
            label=name,
            extensions={
                "stage3_graphics": {
                    "width": width,
                    "height": height,
                }
            },
        )
        if placed:
            self.representations.append(representation)
        return representation

    def diagram(self, *extra_pages: DiagramPage) -> DiagramDocument:
        pages = (DiagramPage(PAGE, "Основная схема"), *extra_pages)
        return DiagramDocument.create(
            "Проверка коллизий",
            pages,
            self.representations,
            document_id=DiagramDocumentId("diagram.ui.collision"),
        )


def test_shapes_are_separate_and_semantic_ports_survive_rotation() -> None:
    fixture = _Fixture.create()
    equipment, representation = fixture.equipment(
        "builtin.transformer_2w",
        "Т1",
        token="transformer",
        x=20.0,
        y=40.0,
        width=100.0,
        height=60.0,
        rotation_deg=90.0,
    )

    geometry = geometry_for_representation(representation, fixture.model)

    assert geometry.category is CollisionCategory.EQUIPMENT
    assert geometry.body_collision_shape is not None
    assert geometry.routing_obstacle_shape is not None
    assert geometry.visual_bounds != geometry.selection_shape
    assert geometry.body_collision_shape != geometry.routing_obstacle_shape
    assert geometry.selection_shape.width > geometry.visual_bounds.width
    assert geometry.routing_obstacle_shape.width > geometry.body_collision_shape.width
    assert geometry.orientation_quarter_turns == 1
    assert tuple(zone.role for zone in geometry.port_connection_zones) == ("hv", "lv")
    assert tuple(zone.port_id for zone in geometry.port_connection_zones) == equipment.port_ids
    assert geometry.port_connection_zones[0].shape.center_y < geometry.anchor.y
    assert geometry.port_connection_zones[1].shape.center_y > geometry.anchor.y


def test_spatial_index_returns_only_local_candidates() -> None:
    fixture = _Fixture.create()
    for index in range(80):
        fixture.equipment(
            "builtin.load",
            f"Нагрузка {index}",
            token=f"load-{index}",
            x=float(index * 300),
            y=0.0,
        )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)
    selected = service.geometries[
        GraphicalRepresentationId("representation.ui.collision.load-40")
    ]

    candidates = service.index.query(PAGE, selected.body_collision_shape)

    assert tuple(item.representation_id for item in candidates) == (
        selected.representation_id,
    )


def test_group_placement_and_resize_preflight_are_atomic_and_non_mutating() -> None:
    fixture = _Fixture.create()
    _, first = fixture.equipment(
        "builtin.load",
        "Нагрузка 1",
        token="atomic-first",
        x=100.0,
        y=100.0,
        width=80.0,
        height=50.0,
    )
    fixture.equipment(
        "builtin.load",
        "Нагрузка 2",
        token="atomic-second",
        x=220.0,
        y=100.0,
        width=80.0,
        height=50.0,
    )
    _, candidate = fixture.equipment(
        "builtin.load",
        "Копия",
        token="atomic-candidate",
        x=200.0,
        y=100.0,
        width=80.0,
        height=50.0,
        placed=False,
    )
    diagram = fixture.diagram()
    service = DiagramCollisionService(diagram, fixture.model)
    representations_before = diagram.representations

    placement = service.check_placements(
        (geometry_for_representation(candidate, fixture.model),)
    )
    resize = service.check_resize(first.id, 240.0, 50.0)

    assert not placement.allowed
    assert placement.conflicts
    assert not resize.allowed
    assert resize.conflicts
    assert diagram.representations == representations_before
    assert diagram.representations[first.id] == first


def test_preview_geometry_does_not_create_domain_equipment_or_ports() -> None:
    fixture = _Fixture.create()
    definition = fixture.model.equipment_type(
        EquipmentTypeId("builtin.circuit_breaker"), 1
    )
    equipment_before = tuple(fixture.model.equipment)
    ports_before = tuple(fixture.model.ports)
    revision_before = fixture.model.revision

    preview = geometry_for_equipment_preview(
        definition,
        page_id=PAGE,
        x=10.0,
        y=20.0,
        rotation_deg=90.0,
        display_name="Выключатель preview",
    )

    assert preview.orientation_quarter_turns == 1
    assert tuple(item.role for item in preview.port_connection_zones) == ("a", "b")
    assert all(item.port_id is None for item in preview.port_connection_zones)
    assert tuple(fixture.model.equipment) == equipment_before
    assert tuple(fixture.model.ports) == ports_before
    assert fixture.model.revision == revision_before


def test_placement_checks_body_and_zoom_independent_safe_gap() -> None:
    fixture = _Fixture.create()
    fixture.equipment(
        "builtin.circuit_breaker", "QF1", token="existing", x=0.0, y=0.0
    )
    _, close_representation = fixture.equipment(
        "builtin.recloser",
        "Реклоузер 1",
        token="candidate-close",
        x=90.0,
        y=0.0,
        placed=False,
    )
    _, far_representation = fixture.equipment(
        "builtin.recloser",
        "Реклоузер 2",
        token="candidate-far",
        x=110.0,
        y=0.0,
        placed=False,
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)

    close = service.check_placement(
        geometry_for_representation(close_representation, fixture.model)
    )
    far = service.check_placement(
        geometry_for_representation(far_representation, fixture.model)
    )

    assert close.allowed is False
    assert close.conflicts[0].kind is CollisionKind.SAFE_CLEARANCE
    assert "недостаточный зазор" in close.message
    assert far.allowed is True


def test_line_and_plain_connection_node_do_not_become_hard_bodies() -> None:
    fixture = _Fixture.create()
    fixture.equipment("builtin.load", "Нагрузка", token="load", x=0.0, y=0.0)
    _, line_representation = fixture.equipment(
        "builtin.line",
        "Графическая линия",
        token="line",
        x=0.0,
        y=0.0,
        placed=False,
    )
    node_representation = fixture.node(
        "Точка соединения",
        token="node",
        x=0.0,
        y=0.0,
        placed=False,
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)

    line = geometry_for_representation(line_representation, fixture.model)
    node = geometry_for_representation(node_representation, fixture.model)

    assert line.category is CollisionCategory.LINE
    assert line.body_collision_shape is None
    assert node.category is CollisionCategory.ELECTRICAL_NODE
    assert node.body_collision_shape is None
    assert service.check_placement(line).allowed is True
    assert service.check_placement(node).allowed is True


def test_equipment_can_touch_bus_only_through_port_zone() -> None:
    fixture = _Fixture.create()
    fixture.node(
        "Шины 10 кВ",
        token="bus",
        x=0.0,
        y=0.0,
        symbol_key="busbar",
        width=240.0,
        height=12.0,
    )
    _, connected_representation = fixture.equipment(
        "builtin.load",
        "Нагрузка у шин",
        token="load-port",
        x=0.0,
        y=25.0,
        placed=False,
    )
    _, invalid_representation = fixture.equipment(
        "builtin.load",
        "Нагрузка на шинах",
        token="load-body",
        x=0.0,
        y=0.0,
        placed=False,
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)

    connected = service.check_placement(
        geometry_for_representation(connected_representation, fixture.model)
    )
    invalid = service.check_placement(
        geometry_for_representation(invalid_representation, fixture.model)
    )

    assert connected.allowed is True
    assert invalid.allowed is False
    assert invalid.conflicts[0].kind is CollisionKind.BODY_OVERLAP


def test_group_move_is_atomic_and_checks_only_objects_outside_group() -> None:
    fixture = _Fixture.create()
    _, first = fixture.equipment(
        "builtin.circuit_breaker", "QF1", token="first", x=0.0, y=0.0
    )
    _, second = fixture.equipment(
        "builtin.recloser", "Р1", token="second", x=120.0, y=0.0
    )
    fixture.equipment(
        "builtin.transformer_2w", "Т1", token="obstacle", x=240.0, y=0.0
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)
    before = tuple(
        service.geometries[item].anchor
        for item in (first.id, second.id)
    )

    rejected = service.check_move((first.id, second.id), 60.0, 0.0)
    accepted = service.check_move((first.id, second.id), -20.0, 0.0)

    assert rejected.allowed is False
    assert len(rejected.proposed_geometries) == 2
    assert accepted.allowed is True
    assert tuple(
        service.geometries[item].anchor
        for item in (first.id, second.id)
    ) == before


def test_group_move_keeps_legacy_internal_layout_without_false_blocking() -> None:
    fixture = _Fixture.create()
    _, first = fixture.equipment(
        "builtin.circuit_breaker", "QF1", token="group-old-first", x=0.0, y=0.0
    )
    _, second = fixture.equipment(
        "builtin.circuit_breaker", "QF2", token="group-old-second", x=20.0, y=0.0
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)

    result = service.check_move((first.id, second.id), 100.0, 40.0)

    assert service.legacy_overlaps()
    assert result.allowed is True
    assert result.conflicts == ()


def test_rotation_that_creates_collision_is_rejected_without_mutation() -> None:
    fixture = _Fixture.create()
    _, wide = fixture.equipment(
        "builtin.transformer_2w",
        "Т широкий",
        token="wide",
        x=0.0,
        y=0.0,
        width=160.0,
        height=40.0,
    )
    fixture.equipment(
        "builtin.circuit_breaker",
        "QF сверху",
        token="above",
        x=0.0,
        y=90.0,
        width=40.0,
        height=40.0,
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)

    result = service.check_rotation(wide.id, 90.0)

    assert result.allowed is False
    assert result.conflicts[0].kind is CollisionKind.BODY_OVERLAP
    assert service.geometries[wide.id].rotation_deg == 0.0
    assert result.proposed_geometries[0].rotation_deg == 90.0


def test_existing_overlap_can_be_reduced_but_not_worsened() -> None:
    fixture = _Fixture.create()
    _, first = fixture.equipment(
        "builtin.circuit_breaker", "QF1", token="old-first", x=0.0, y=0.0
    )
    fixture.equipment(
        "builtin.recloser", "Р1", token="old-second", x=30.0, y=0.0
    )
    service = DiagramCollisionService(fixture.diagram(), fixture.model)

    improving = service.check_move((first.id,), -10.0, 0.0)
    worsening = service.check_move((first.id,), 10.0, 0.0)

    assert improving.allowed is True
    assert worsening.allowed is False


def test_legacy_overlap_becomes_problem_without_coordinate_or_model_change() -> None:
    fixture = _Fixture.create()
    _, first = fixture.equipment(
        "builtin.transformer_2w", "Т1", token="legacy-first", x=10.0, y=20.0
    )
    _, second = fixture.equipment(
        "builtin.transformer_2w", "Т2", token="legacy-second", x=30.0, y=20.0
    )
    diagram = fixture.diagram()
    electrical_revision = fixture.model.revision
    connectivity = fixture.model.connectivity_signature()
    coordinates = {
        item.id: (item.x, item.y) for item in diagram.representations.values()
    }

    diagnostics = ProjectValidationService().validate(fixture.model, diagram)

    warning = next(
        item for item in diagnostics if item.code == "diagram.equipment_overlap"
    )
    assert warning.severity is ProjectDiagnosticSeverity.WARNING
    assert warning.object_id == first.id.value
    assert second.id.value in warning.related_ids
    assert warning.page_id == PAGE
    assert warning.representation_id == first.id
    assert "оставлены без изменений" in warning.message
    assert fixture.model.revision == electrical_revision
    assert fixture.model.connectivity_signature() == connectivity
    assert diagram == fixture.diagram()
    assert {
        item.id: (item.x, item.y) for item in diagram.representations.values()
    } == coordinates


def test_objects_on_different_pages_never_collide() -> None:
    fixture = _Fixture.create()
    other_page = PageId("page.ui.collision.other")
    fixture.equipment(
        "builtin.transformer_2w", "Т1", token="page-one", x=0.0, y=0.0
    )
    fixture.equipment(
        "builtin.transformer_2w",
        "Т2",
        token="page-two",
        x=0.0,
        y=0.0,
        page_id=other_page,
    )
    service = DiagramCollisionService(
        fixture.diagram(DiagramPage(other_page, "Другая страница")),
        fixture.model,
    )

    assert service.legacy_overlaps() == ()


def test_oriented_rectangle_treats_edge_touch_as_allowed() -> None:
    first = OrientedRectangle(0.0, 0.0, 20.0, 20.0)
    touching = OrientedRectangle(20.0, 0.0, 20.0, 20.0)
    overlapping = OrientedRectangle(19.0, 0.0, 20.0, 20.0)

    assert first.intersects(touching) is False
    assert first.intersects(overlapping) is True
    assert first.overlap_depth(overlapping) == pytest.approx(1.0)
