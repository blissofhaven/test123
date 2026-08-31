# -*- coding: utf-8 -*-
"""Изоляция графической геометрии от электрической топологии Этапа 2.5."""
from __future__ import annotations

from rza_calc.calculation import CalculationProjectionBuilder
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RoutePoint,
)
from rza_calc.domain.electrical import (
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    LineKind,
    LogicalLineId,
    PortId,
    VoltageClassId,
)
from rza_calc.io.electrical_model import electrical_model_to_dict
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.graphical.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _line(
    model: ElectricalModel,
    token: str,
    first: ElectricalNode,
    second: ElectricalNode,
):
    return model.create_logical_line(
        f"Линия {token}",
        LineKind.OVERHEAD,
        first.id,
        second.id,
        1_000,
        logical_line_id=LogicalLineId(f"line.graphical.{token}"),
        section_equipment_id=EquipmentId(f"equipment.graphical.{token}"),
        port_ids_by_role={
            "from": PortId(f"port.graphical.{token}.from"),
            "to": PortId(f"port.graphical.{token}.to"),
        },
        connection_ids=(
            ConnectionId(f"connection.graphical.{token}.from"),
            ConnectionId(f"connection.graphical.{token}.to"),
        ),
    )


def _page() -> DiagramPage:
    return DiagramPage(PageId("page.graphical.main"), "Общая схема")


def test_rerouted_route_point_does_not_change_electrical_or_derived_graphs() -> None:
    model = ElectricalModel.with_builtins("Изменение графического поворота")
    first = _node(model, "reroute.first")
    second = _node(model, "reroute.second")
    _, section, _ = _line(model, "reroute", first, second)
    page = _page()
    representation_id = GraphicalRepresentationId("representation.graphical.reroute")
    original_route = (
        RoutePoint(0.0, 0.0),
        RoutePoint(40.0, 0.0),
        RoutePoint(40.0, 100.0),
    )
    representation = GraphicalRepresentation(
        representation_id,
        page.id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=section.equipment_id,
        symbol_key="линия",
        route_points=original_route,
    )
    document = DiagramDocument.create(
        "Однолинейная схема",
        (page,),
        (representation,),
        document_id=DiagramDocumentId("diagram.graphical.reroute"),
    )
    document.require_valid_targets(model)

    model_before = electrical_model_to_dict(model)
    model_revision_before = model.revision
    connectivity_before = model.connectivity_signature()
    topology_before = TopologyEngine().compile(model).semantic_signature()
    calculation_before = (
        CalculationProjectionBuilder().build(model).semantic_fingerprint()
    )
    changed_route = (
        RoutePoint(0.0, 0.0),
        RoutePoint(75.0, 0.0),
        RoutePoint(75.0, 100.0),
    )

    rerouted = document.rerouted_representation(
        representation_id,
        changed_route,
    )
    rerouted.require_valid_targets(model)

    assert rerouted.revision == document.revision + 1
    assert rerouted.representations[representation_id].route_points == changed_route
    assert document.representations[representation_id].route_points == original_route
    assert model.revision == model_revision_before
    assert electrical_model_to_dict(model) == model_before
    assert model.connectivity_signature() == connectivity_before
    assert TopologyEngine().compile(model).semantic_signature() == topology_before
    assert (
        CalculationProjectionBuilder().build(model).semantic_fingerprint()
        == calculation_before
    )


def test_crossing_graphical_routes_do_not_create_electrical_connection() -> None:
    model = ElectricalModel.with_builtins("Графическое пересечение")
    first_a = _node(model, "cross.first_a")
    first_b = _node(model, "cross.first_b")
    second_a = _node(model, "cross.second_a")
    second_b = _node(model, "cross.second_b")
    _, first_section, _ = _line(model, "cross.first", first_a, first_b)
    _, second_section, _ = _line(model, "cross.second", second_a, second_b)
    page = _page()
    horizontal_route = (RoutePoint(0.0, 50.0), RoutePoint(100.0, 50.0))
    vertical_route = (RoutePoint(50.0, 0.0), RoutePoint(50.0, 100.0))
    first_representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.graphical.cross.first"),
        page.id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=first_section.equipment_id,
        symbol_key="линия",
        route_points=horizontal_route,
    )
    second_representation = GraphicalRepresentation(
        GraphicalRepresentationId("representation.graphical.cross.second"),
        page.id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=second_section.equipment_id,
        symbol_key="линия",
        route_points=vertical_route,
    )
    document = DiagramDocument.create(
        "Пересекающиеся линии",
        (page,),
        (first_representation, second_representation),
        document_id=DiagramDocumentId("diagram.graphical.cross"),
    )
    document.require_valid_targets(model)

    # Пересечение (50, 50) лежит строго внутри обоих графических отрезков.
    assert horizontal_route[0].y == horizontal_route[1].y == 50.0
    assert vertical_route[0].x == vertical_route[1].x == 50.0
    assert horizontal_route[0].x < vertical_route[0].x < horizontal_route[1].x
    assert vertical_route[0].y < horizontal_route[0].y < vertical_route[1].y

    snapshot = TopologyEngine().compile(model)
    assert len(snapshot.components) == 2
    assert snapshot.component_by_node[first_a.id] == snapshot.component_by_node[first_b.id]
    assert snapshot.component_by_node[second_a.id] == snapshot.component_by_node[second_b.id]
    assert snapshot.component_by_node[first_a.id] != snapshot.component_by_node[second_a.id]
    assert snapshot.links_between(first_a.id, second_a.id) == ()
    assert snapshot.links_between(first_b.id, second_b.id) == ()
    assert len(model.connections) == 4
    assert model.connectivity_signature() == (
        (f"port.graphical.cross.first.from", first_a.id.value),
        (f"port.graphical.cross.first.to", first_b.id.value),
        (f"port.graphical.cross.second.from", second_a.id.value),
        (f"port.graphical.cross.second.to", second_b.id.value),
    )
    assert model.validate_integrity() == []
