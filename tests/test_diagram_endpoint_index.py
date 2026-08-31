# -*- coding: utf-8 -*-
"""Сохранение диагностики при индексировании концов физических ветвей."""
from __future__ import annotations

from dataclasses import replace

from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RouteWaypoint,
    RouteWaypointId,
)
from rza_calc.domain.electrical import (
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
)


def _branch_fixture():
    model = ElectricalModel.with_builtins("Индекс концов ветвей")
    nodes = tuple(
        ElectricalNode(
            ElectricalNodeId(f"node.diagram.index.{index}"),
            f"Узел {index}",
        )
        for index in range(3)
    )
    for node in nodes:
        model.add_node(node)
    equipment, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "QF для проверки трассы",
    )
    model.connect_port(equipment.port_ids[0], nodes[0].id)
    model.connect_port(equipment.port_ids[1], nodes[1].id)
    unrelated, _ = model.create_equipment(
        "builtin.circuit_breaker",
        "Независимая ветвь без графической трассы",
    )
    model.connect_port(unrelated.port_ids[0], nodes[0].id)
    model.connect_port(unrelated.port_ids[1], nodes[2].id)

    page = DiagramPage(PageId("page.diagram.index"), "Основная схема")
    representations = tuple(
        GraphicalRepresentation(
            GraphicalRepresentationId(
                f"representation.diagram.index.{index}"
            ),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node.id,
            x=float(index * 100),
            y=0.0,
        )
        for index, node in enumerate(nodes)
    )
    route = DiagramRoute(
        DiagramRouteId("route.diagram.index.branch"),
        page.id,
        DiagramRouteKind.EQUIPMENT_BRANCH,
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            representations[0].id,
            nodes[0].id,
            branch_port_id=equipment.port_ids[0],
        ),
        RouteEndpointAnchor(
            RouteAnchorKind.ELECTRICAL_NODE,
            representations[1].id,
            nodes[1].id,
            branch_port_id=equipment.port_ids[1],
        ),
        equipment_id=equipment.id,
        waypoints=(
            RouteWaypoint(
                RouteWaypointId("waypoint.diagram.index.0"),
                0.0,
                0.0,
            ),
            RouteWaypoint(
                RouteWaypointId("waypoint.diagram.index.1"),
                100.0,
                0.0,
            ),
        ),
    )
    document = DiagramDocument.create(
        "Однолинейная схема",
        (page,),
        representations,
        routes=(route,),
        document_id=DiagramDocumentId("diagram.endpoint.index"),
    )
    return model, document, route, equipment, nodes, representations


def test_endpoint_index_keeps_valid_branch_diagnostics_empty() -> None:
    model, document, *_ = _branch_fixture()

    assert document.validate_targets(model) == ()


def test_endpoint_index_preserves_diagnostic_order_for_wrong_endpoint() -> None:
    (
        model,
        document,
        route,
        equipment,
        nodes,
        representations,
    ) = _branch_fixture()
    wrong_end = replace(
        route.end_anchor,
        representation_id=representations[2].id,
        electrical_node_id=nodes[2].id,
    )
    broken_route = replace(route, end_anchor=wrong_end)
    document = replace(
        document,
        routes={broken_route.id: broken_route},
        revision=document.revision + 1,
    )

    assert document.validate_targets(model) == (
        f"Порт ветви '{equipment.port_ids[1]}' привязки конца трассы "
        f"'{route.id}' не подключён к указанному узлу '{nodes[2].id}'.",
        f"Трасса физической ветви '{route.id}' не совпадает с "
        "электрическими узлами оборудования.",
    )


def test_endpoint_index_uses_equipment_port_ids_for_corrupted_model() -> None:
    model, document, route, equipment, *_ = _branch_fixture()
    missing_port_id = equipment.port_ids[0]

    # Имитируем частично повреждённый старый проект: оборудование и Connection
    # ещё ссылаются на порт, но сама запись PortInstance потеряна. Прежний
    # O(routes * connections) алгоритм в этом случае не выдавал ложного
    # сообщения о несовпадении узлов физической ветви.
    model._ports.pop(missing_port_id)

    assert document.validate_targets(model) == (
        f"Привязка начала трассы '{route.id}' ссылается на удалённый "
        f"порт ветви '{missing_port_id}'.",
    )
