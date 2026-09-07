# -*- coding: utf-8 -*-
"""Приёмочная матрица 35 обязательных unit-тестов Этапа 4."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
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
    AC_POWER,
    DataConfirmation,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    EquipmentTypeDefinition,
    EquipmentTypeId,
    LineConstructionSegment,
    LineConstructionSegmentId,
    LineKind,
    LogicalLineId,
    OperatingState,
    OperatingStateId,
    PortDefinition,
    PortKindId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor.controller import (
    EditorCommandError,
    NewNodeTarget,
    NodeTarget,
    PhysicalLineInput,
    PortTarget,
    ProjectEditorController,
)
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_to_dict
from rza_calc.io.project import load_project, save_project
from rza_calc.topology import TopologyEngine


U10 = VoltageClassId("builtin.voltage.ac.10kv")
U35 = VoltageClassId("builtin.voltage.ac.35kv")
ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json"


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project(token: str) -> _Project:
    page = DiagramPage(PageId(f"page.stage4.requirements.{token}"), "Основная схема")
    return _Project(
        ElectricalModel.with_builtins("Приёмка Этапа 4"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,),
            document_id=DiagramDocumentId(f"diagram.stage4.requirements.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )


def _controller(token: str) -> tuple[_Project, ProjectEditorController]:
    project = _project(token)
    return project, ProjectEditorController(project)


def _node(
    controller: ProjectEditorController,
    name: str,
    *,
    voltage: VoltageClassId | None = U10,
    symbol_key: str = "electrical_node",
) -> ElectricalNodeId:
    return controller.add_electrical_node(
        name,
        voltage_class_id=voltage,
        symbol_key=symbol_key,
    ).node_id


def _physical(
    length_mm: int | None = 12_000_000,
    *,
    confirmed: bool = True,
) -> PhysicalLineInput:
    state = (
        DataConfirmation.CONFIRMED
        if confirmed and length_mm is not None
        else DataConfirmation.UNCONFIRMED
    )
    return PhysicalLineInput(
        length_mm,
        state,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3}
        if confirmed
        else {},
        DataConfirmation.CONFIRMED
        if confirmed
        else DataConfirmation.UNCONFIRMED,
    )


def _line(
    controller: ProjectEditorController,
    name: str,
    start: ElectricalNodeId,
    finish: ElectricalNodeId,
    *,
    length_mm: int | None = 12_000_000,
    confirmed: bool = True,
    kind: LineKind = LineKind.OVERHEAD,
):
    return controller.create_physical_line(
        name,
        kind,
        NodeTarget(start),
        NodeTarget(finish),
        physical=_physical(length_mm, confirmed=confirmed),
    )


def _all_domain_ids(model: ElectricalModel) -> list[str]:
    result = [
        *(item.value for item in model.voltage_classes),
        *(item.value for item, _ in model.equipment_types),
        *(item.value for item in model.equipment),
        *(item.value for item in model.ports),
        *(item.value for item in model.electrical_nodes),
        *(item.value for item in model.connections),
        *(item.value for item in model.operating_states),
        *(item.value for item in model.logical_lines),
    ]
    result.extend(
        segment.id.value
        for section in model.line_sections.values()
        for segment in section.construction_segments
    )
    result.extend(sorted({
        line.feeder_id.value
        for line in model.logical_lines.values()
        if line.feeder_id is not None
    }))
    return result


# 1. Соединение двух допустимых портов.
def test_01_connect_two_compatible_ports() -> None:
    project, controller = _controller("01")
    first = controller.add_equipment("builtin.external_grid", "Источник", voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Нагрузка", voltage_class_by_group={"main": U10})

    result = controller.connect_ports(first.port_ids[0], second.port_ids[0])

    assert result.node_id in project.electrical_model.electrical_nodes
    assert len(result.connection_ids) == 2
    assert result.route_id in project.diagram.routes


# 2. Отказ при несовместимых портах.
def test_02_reject_incompatible_port_kinds_atomically() -> None:
    project = _project("02")
    other_kind = PortKindId("user.port.other")
    definition = EquipmentTypeDefinition(
        EquipmentTypeId("user.type.other_port"),
        1,
        "Другой электрический тип",
        "load",
        (PortDefinition("terminal", "Вывод", other_kind, voltage_group="main"),),
    )
    project.electrical_model.register_equipment_type(definition)
    controller = ProjectEditorController(project)
    first = controller.add_equipment("builtin.load", "Нагрузка AC", voltage_class_by_group={"main": U10})
    second = controller.add_equipment(definition.id, "Другой порт", voltage_class_by_group={"main": U10})
    before = project.electrical_model.connectivity_signature()

    with pytest.raises(EditorCommandError):
        controller.connect_ports(first.port_ids[0], second.port_ids[0])

    assert project.electrical_model.connectivity_signature() == before


# 3. Уточнение заказчика 31.08: существующий неизвестный порт не подключается
# молча. Доменные импорт/черновики остаются совместимыми отдельным контрактом.
def test_03_unknown_port_takes_the_class_from_its_first_connection() -> None:
    """Правило изменено решением заказчика 31.08.2026.

    Прежде соединение с выводом без класса ОТКЛОНЯЛОСЬ, и аппарат, только что
    взятый из палитры, нельзя было довести до шины, пока не выберешь класс
    руками. Теперь пустое поле заполняется классом того, к чему ведут: он и
    так однозначно следует из соединения.

    Утверждения об атомарности не ослаблены, а перенесены на случай, где
    определить класс НЕЛЬЗЯ (оба конца неизвестны): там отказ прежний и
    проект по-прежнему не меняется ни на бит. Второй половиной правила —
    «заданный класс не подменяется» — занимается
    tests/test_placement_and_voltage.py.
    """
    project, controller = _controller("03")
    known = controller.add_equipment(
        "builtin.external_grid",
        "Источник 10 кВ",
        voltage_class_by_group={"main": U10},
    )
    unknown = controller.add_equipment("builtin.load", "Нагрузка")
    controller.connect_ports(known.port_ids[0], unknown.port_ids[0])
    assert dict(
        project.electrical_model.equipment[unknown.equipment_id].voltage_class_by_group
    ) == {"main": U10}
    assert len(project.electrical_model.connections) == 2


def test_03b_two_unknown_ports_are_still_rejected_atomically() -> None:
    """Подставлять неизвестное вместо неизвестного нечем — отказ прежний."""
    project, controller = _controller("03b")
    first = controller.add_equipment("builtin.load", "A")
    second = controller.add_equipment("builtin.load", "B")
    compatibility = controller.validate_connection(
        first.port_ids[0], PortTarget(second.port_ids[0])
    )
    before = project.electrical_model.connectivity_signature()
    diagram_before = project.diagram
    journal_before = controller.journal
    revision_before = project.electrical_model.revision

    with pytest.raises(EditorCommandError, match="напряжение.*не определено"):
        controller.connect_ports(first.port_ids[0], second.port_ids[0])

    assert not compatibility.valid and compatibility.severity == "error"
    assert compatibility.effective_voltage_id is None
    assert project.electrical_model.connectivity_signature() == before
    assert project.electrical_model.revision == revision_before
    assert project.diagram == diagram_before
    assert controller.journal == journal_before
    assert not project.electrical_model.connections


# 4. Отказ при конфликте 10 кВ и 35 кВ.
def test_04_reject_10kv_to_35kv_connection() -> None:
    project, controller = _controller("04")
    first = controller.add_equipment(
        "builtin.load", "10 кВ", voltage_class_by_group={"main": U10}
    )
    second = controller.add_equipment(
        "builtin.load", "35 кВ", voltage_class_by_group={"main": U35}
    )
    before = project.electrical_model.connectivity_signature()

    with pytest.raises(EditorCommandError):
        controller.connect_ports(first.port_ids[0], second.port_ids[0])

    assert project.electrical_model.connectivity_signature() == before


# 5. Завершение ветви на свободном месте создаёт узел.
def test_05_finish_on_free_place_creates_node_and_representation() -> None:
    project, controller = _controller("05")
    load = controller.add_equipment("builtin.load", "Нагрузка", voltage_class_by_group={"main": U10})

    result = controller.finish_port_on_new_node(
        load.port_ids[0],
        NewNodeTarget("Свободный конец", 320.0, 140.0, voltage_class_id=U10),
    )

    assert result.node_id in project.electrical_model.electrical_nodes
    assert project.diagram.representations_for_node(result.node_id)
    assert result.route_id in project.diagram.routes


# 6. Переподключение конца изменяет узел, но сохраняет объект ветви.
def test_06_reconnect_endpoint_preserves_equipment_port_and_connection_ids() -> None:
    project, controller = _controller("06")
    first_node = _node(controller, "Первый")
    second_node = _node(controller, "Второй")
    third_node = _node(controller, "Третий")
    breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "Q1",
        voltage_class_by_group={"main": U10},
    )
    controller.connect_port_to_node(breaker.port_ids[0], first_node)
    controller.connect_port_to_node(breaker.port_ids[1], second_node)
    old = project.electrical_model.connection_for_port(breaker.port_ids[1])
    assert old is not None

    result = controller.reconnect_port(breaker.port_ids[1], NodeTarget(third_node))
    current = project.electrical_model.connection_for_port(breaker.port_ids[1])

    assert result.equipment_id == breaker.equipment_id
    assert current is not None and current.id == old.id
    assert current.electrical_node_id == third_node


# 7. Перемещение точки маршрута не изменяет топологию.
def test_07_route_waypoint_move_does_not_change_topology() -> None:
    project, controller = _controller("07")
    first = controller.add_equipment("builtin.external_grid", "Источник", x=0, y=0, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Нагрузка", x=160, y=80, voltage_class_by_group={"main": U10})
    connected = controller.connect_ports(first.port_ids[0], second.port_ids[0])
    assert connected.route_id is not None
    topology_before = project.electrical_model.connectivity_signature()
    route = project.diagram.routes[connected.route_id]
    moved = (
        replace(route.waypoints[0], x=route.waypoints[0].x + 20.0),
        RouteWaypoint(RouteWaypointId.new(), route.waypoints[0].x + 20.0, route.waypoints[-1].y),
        route.waypoints[-1],
    )

    controller.reroute_diagram_route(connected.route_id, moved)

    assert project.electrical_model.connectivity_signature() == topology_before


# 8. Пересечение линий не создаёт узел.
def test_08_graphical_crossing_does_not_create_electrical_node() -> None:
    model = ElectricalModel.with_builtins("Пересечение")
    node_a = ElectricalNode(ElectricalNodeId("node.stage4.cross.a"), "A")
    node_b = ElectricalNode(ElectricalNodeId("node.stage4.cross.b"), "B")
    model.add_node(node_a)
    model.add_node(node_b)
    page = DiagramPage(PageId("page.stage4.cross"), "Схема")
    reps = (
        GraphicalRepresentation(
            GraphicalRepresentationId("representation.cross.a1"),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_a.id,
        ),
        GraphicalRepresentation(
            GraphicalRepresentationId("representation.cross.a2"),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_a.id,
        ),
        GraphicalRepresentation(
            GraphicalRepresentationId("representation.cross.b1"),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_b.id,
        ),
        GraphicalRepresentation(
            GraphicalRepresentationId("representation.cross.b2"),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_b.id,
        ),
    )

    def route(token: str, node_id: ElectricalNodeId, start: int, finish: int, points):
        return DiagramRoute(
            DiagramRouteId(f"route.cross.{token}"),
            page.id,
            DiagramRouteKind.NODE_CONNECTION,
            RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, reps[start].id, node_id),
            RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE, reps[finish].id, node_id),
            electrical_node_id=node_id,
            waypoints=tuple(
                RouteWaypoint(RouteWaypointId(f"waypoint.cross.{token}.{index}"), x, y)
                for index, (x, y) in enumerate(points)
            ),
        )

    diagram = DiagramDocument.create(
        "Схема",
        (page,),
        reps,
        routes=(
            route("horizontal", node_a.id, 0, 1, ((-50, 0), (50, 0))),
            route("vertical", node_b.id, 2, 3, ((0, -50), (0, 50))),
        ),
    )

    assert len(model.electrical_nodes) == 2
    assert model.connectivity_signature() == ()
    assert diagram.validate_targets(model) == ()


# 9. Явная точка соединения создаёт узел.
def test_09_explicit_port_connection_creates_one_node() -> None:
    project, controller = _controller("09")
    first = controller.add_equipment("builtin.load", "Нагрузка 1", voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Нагрузка 2", voltage_class_by_group={"main": U10})
    before = len(project.electrical_model.electrical_nodes)

    connected = controller.connect_ports(first.port_ids[0], second.port_ids[0])

    assert len(project.electrical_model.electrical_nodes) == before + 1
    assert connected.node_id in project.electrical_model.electrical_nodes


# 10. Несекционированная шина остаётся одним узлом.
def test_10_unsplit_bus_remains_one_electrical_node() -> None:
    project, controller = _controller("10")
    bus = _node(controller, "Шина 10 кВ", symbol_key="busbar")
    loads = [controller.add_equipment(
        "builtin.load", f"Нагрузка {index}", x=x, y=160,
        voltage_class_by_group={"main": U10},
    ) for index, x in enumerate((-180, -60, 60, 180))]
    for load in loads:
        controller.connect_port_to_node(load.port_ids[0], bus)

    assert {
        project.electrical_model.connection_for_port(load.port_ids[0]).electrical_node_id
        for load in loads
    } == {bus}


# 11. Подключение в разных местах одной шины использует один ID узла.
def test_11_different_graphical_bus_anchors_use_same_node_id() -> None:
    project, controller = _controller("11")
    bus = controller.add_electrical_node(
        "Шина",
        symbol_key="busbar",
        width=400.0,
        height=12.0,
        voltage_class_id=U10,
    )
    first = controller.add_equipment("builtin.load", "Слева", x=-160, y=100, voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.load", "Справа", x=160, y=100, voltage_class_by_group={"main": U10})
    one = controller.connect_port_to_node(first.port_ids[0], bus.node_id)
    two = controller.connect_port_to_node(second.port_ids[0], bus.node_id)

    assert one.node_id == two.node_id == bus.node_id
    assert len(project.diagram.representations_for_node(bus.node_id)) == 1


def _tap_chain(
    token: str,
    count: int,
) -> tuple[_Project, ProjectEditorController, object, list[object]]:
    project, controller = _controller(token)
    start = _node(controller, "Начало")
    finish = _node(controller, "Конец")
    main = _line(
        controller,
        "Основная ВЛ",
        start,
        finish,
        length_mm=(count + 1) * 1_000_000,
    )
    section_id = main.section_id
    taps = []
    for index in range(count):
        target = NewNodeTarget(
            f"Конец отпайки {index + 1}",
            200.0 + index * 80.0,
            160.0,
            voltage_class_id=U10,
        )
        tap = controller.create_tap(
            section_id,
            1_000_000,
            f"Отпайка {index + 1}",
            LineKind.CABLE,
            target,
            physical=_physical(500_000),
        )
        taps.append(tap)
        section_id = tap.second_section_id
    return project, controller, main, taps


def _section_endpoints(
    model: ElectricalModel,
    section_id: EquipmentId,
) -> tuple[ElectricalNodeId, ElectricalNodeId]:
    first = model.connection_for_port(model.port_by_role(section_id, "from").id)
    second = model.connection_for_port(model.port_by_role(section_id, "to").id)
    assert first is not None and second is not None
    return first.electrical_node_id, second.electrical_node_id


def _raw_node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.stage4.raw.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


# 12. Создание одной отпайки.
def test_12_create_one_tap() -> None:
    project, _, main, taps = _tap_chain("12", 1)
    tap = taps[0]
    main_line = project.electrical_model.logical_lines[main.logical_line_id]

    assert main_line.section_equipment_ids == (
        tap.first_section_id,
        tap.second_section_id,
    )
    assert tap.branch_logical_line_id in project.electrical_model.logical_lines
    assert len([
        item
        for item in project.electrical_model.connections.values()
        if item.electrical_node_id == tap.tap_node_id
    ]) == 3


# 13. Создание трёх отпаек.
def test_13_create_three_taps() -> None:
    project, _, _, taps = _tap_chain("13", 3)

    assert len(taps) == 3
    assert len({item.tap_node_id for item in taps}) == 3
    assert all(
        item.branch_logical_line_id in project.electrical_model.logical_lines
        for item in taps
    )


# 14. После трёх отпаек основная линия состоит из четырёх ветвей.
def test_14_three_taps_split_main_line_into_four_branches() -> None:
    project, _, main, _ = _tap_chain("14", 3)

    assert len(
        project.electrical_model.logical_lines[
            main.logical_line_id
        ].section_equipment_ids
    ) == 4


# 15. Основная линия продолжается после каждой отпайки.
def test_15_main_line_continues_after_every_tap() -> None:
    project, _, main, taps = _tap_chain("15", 3)
    model = project.electrical_model
    section_ids = model.logical_lines[main.logical_line_id].section_equipment_ids
    endpoints = [_section_endpoints(model, item) for item in section_ids]

    assert all(
        left[1] == right[0]
        for left, right in zip(endpoints, endpoints[1:])
    )
    assert {item.tap_node_id for item in taps} == {
        item[1] for item in endpoints[:-1]
    }


# 16. Отпайка от отпайки.
def test_16_tap_can_be_created_from_an_existing_tap_line() -> None:
    project, controller, _, taps = _tap_chain("16", 1)
    first = taps[0]

    nested = controller.create_tap(
        first.branch_section_id,
        250_000,
        "Вложенная отпайка",
        LineKind.OVERHEAD,
        NewNodeTarget("Конец вложенной отпайки", 400.0, 300.0, voltage_class_id=U10),
        physical=_physical(300_000),
    )

    assert nested.main_logical_line_id == first.branch_logical_line_id
    assert len([
        item
        for item in project.electrical_model.connections.values()
        if item.electrical_node_id == nested.tap_node_id
    ]) == 3


# 17. Удаление отпайки.
def test_17_remove_tap_deletes_side_branch_but_keeps_main_line() -> None:
    model = ElectricalModel.with_builtins("Удаление отпайки")
    start = _raw_node(model, "17.start")
    finish = _raw_node(model, "17.finish")
    branch_end = _raw_node(model, "17.branch")
    line, section, _ = model.create_logical_line(
        "Основная линия",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        4_000_000,
        voltage_class_id=U10,
        inherited_properties={"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
    )
    split, branch, _, _ = model.create_tap_line(
        section.equipment_id,
        2_000_000,
        "Отпайка",
        LineKind.CABLE,
        branch_end.id,
        500_000,
        branch_inherited_properties={"r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.1},
    )

    model.remove_line_tap(
        line.id,
        split.tap_node_id,
        branch_line_ids=(branch.id,),
        collapse=False,
    )

    assert branch.id not in model.logical_lines
    assert line.id in model.logical_lines
    assert split.tap_node_id in model.electrical_nodes


# 18. Объединение совместимых частей основной линии.
def test_18_remove_tap_can_merge_compatible_main_sections() -> None:
    model = ElectricalModel.with_builtins("Схлопывание отпайки")
    start = _raw_node(model, "18.start")
    finish = _raw_node(model, "18.finish")
    branch_end = _raw_node(model, "18.branch")
    line, section, _ = model.create_logical_line(
        "Основная линия",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        8_000_000,
        voltage_class_id=U10,
        inherited_properties={"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
    )
    split, branch, _, _ = model.create_tap_line(
        section.equipment_id,
        3_000_000,
        "Отпайка",
        LineKind.CABLE,
        branch_end.id,
        500_000,
        branch_inherited_properties={"r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.1},
    )

    model.remove_line_tap(
        line.id,
        split.tap_node_id,
        branch_line_ids=(branch.id,),
        collapse=True,
    )

    restored = model.logical_lines[line.id]
    assert len(restored.section_equipment_ids) == 1
    assert model.line_sections[restored.section_equipment_ids[0]].length_mm == 8_000_000
    assert split.tap_node_id not in model.electrical_nodes


# 19. Отказ от автоматического объединения несовместимых линий.
def test_19_incompatible_main_sections_are_not_merged() -> None:
    model = ElectricalModel.with_builtins("Несовместимое схлопывание")
    start = _raw_node(model, "19.start")
    finish = _raw_node(model, "19.finish")
    branch_end = _raw_node(model, "19.branch")
    line, section, _ = model.create_logical_line(
        "Основная линия",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        8_000_000,
        voltage_class_id=U10,
        inherited_properties={"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
    )
    split, branch, _, _ = model.create_tap_line(
        section.equipment_id,
        3_000_000,
        "Отпайка",
        LineKind.CABLE,
        branch_end.id,
        500_000,
        branch_inherited_properties={"r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.1},
    )
    model.set_section_override(split.first_section_id, "conductor_mark", "АС-70")
    before = electrical_model_to_dict(model)

    with pytest.raises(DomainInvariantError):
        model.remove_line_tap(
            line.id,
            split.tap_node_id,
            branch_line_ids=(branch.id,),
            collapse=True,
        )

    assert electrical_model_to_dict(model) == before


def _segmented_line() -> tuple[ElectricalModel, object, object]:
    model = ElectricalModel.with_builtins("Конструктивные участки")
    start = _raw_node(model, "segments.start")
    finish = _raw_node(model, "segments.finish")
    segments = (
        LineConstructionSegment(
            LineConstructionSegmentId("segment.stage4.first"),
            LineKind.OVERHEAD,
            2_000_000,
            {"conductor_mark": "АС-70", "r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
        ),
        LineConstructionSegment(
            LineConstructionSegmentId("segment.stage4.second"),
            LineKind.OVERHEAD,
            3_000_000,
            {"conductor_mark": "АС-95", "r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.15},
        ),
    )
    line, section, _ = model.create_logical_line(
        "Составная ВЛ",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        5_000_000,
        voltage_class_id=U10,
        construction_segments=segments,
    )
    return model, line, section


# 20. Разделение конструктивного участка по длине.
def test_20_split_construction_segment_by_physical_length() -> None:
    model, _, section = _segmented_line()

    split = model.split_line_section(section.equipment_id, 3_000_000)
    left = model.line_sections[split.first_section_id]
    right = model.line_sections[split.second_section_id]

    assert [item.length_mm for item in left.construction_segments] == [2_000_000, 1_000_000]
    assert [item.length_mm for item in right.construction_segments] == [2_000_000]
    assert left.construction_segments[-1].properties["conductor_mark"] == "АС-95"
    assert right.construction_segments[0].properties["conductor_mark"] == "АС-95"


# 21. Сохранение суммы длин после разделения.
def test_21_split_preserves_total_physical_length() -> None:
    model, _, section = _segmented_line()

    split = model.split_line_section(section.equipment_id, 3_000_000)

    assert (
        model.line_sections[split.first_section_id].length_mm
        + model.line_sections[split.second_section_id].length_mm
        == 5_000_000
    )


def _positive_sequence_resistance(model: ElectricalModel, section_id: EquipmentId) -> float:
    section = model.line_sections[section_id]
    return sum(
        model.effective_line_construction_segment_properties(section_id, item.id)[
            "r1_ohm_per_km"
        ]
        * item.length_mm
        / 1_000_000.0
        for item in section.construction_segments
        if item.length_mm is not None
    )


# 22. Сохранение суммы подтверждённых сопротивлений.
def test_22_split_preserves_confirmed_impedance_sum() -> None:
    model, _, section = _segmented_line()
    original = _positive_sequence_resistance(model, section.equipment_id)

    split = model.split_line_section(section.equipment_id, 3_000_000)
    separated = _positive_sequence_resistance(
        model, split.first_section_id
    ) + _positive_sequence_resistance(model, split.second_section_id)

    assert separated == pytest.approx(original)


# 23. Блокировка автоматического деления неподтверждённого общего сопротивления.
def test_23_unconfirmed_impedance_stays_unconfirmed_and_blocks_adapter() -> None:
    model = ElectricalModel.with_builtins("Неподтверждённое сопротивление")
    start = _raw_node(model, "23.start")
    finish = _raw_node(model, "23.finish")
    _, section, _ = model.create_logical_line(
        "Линия без подтверждённого сопротивления",
        LineKind.OVERHEAD,
        start.id,
        finish.id,
        10_000_000,
        voltage_class_id=U10,
        impedance_confirmation=DataConfirmation.UNCONFIRMED,
    )

    split = model.split_line_section(section.equipment_id, 4_000_000)
    adaptation = adapt_to_calculation(model)

    assert all(
        segment.impedance_confirmation is DataConfirmation.UNCONFIRMED
        for item in (split.first_section_id, split.second_section_id)
        for segment in model.line_sections[item].construction_segments
    )
    assert any(item.code == "line_data_unconfirmed" for item in adaptation.diagnostics)
    assert all(
        branch.calculation_block_reason
        for branch in adaptation.network.branches.values()
    )


def _inserted_recloser(token: str):
    project, controller = _controller(token)
    start = _node(controller, "Начало")
    finish = _node(controller, "Конец")
    main = _line(
        controller,
        "ВЛ с реклоузером",
        start,
        finish,
        length_mm=10_000_000,
    )
    inserted = controller.insert_recloser(
        main.section_id,
        4_000_000,
        "Реклоузер Р-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
    )
    return project, controller, start, finish, main, inserted


def _open_state(model: ElectricalModel, recloser_id: EquipmentId, token: str) -> OperatingStateId:
    state = OperatingState(
        OperatingStateId(f"state.stage4.{token}"),
        "Реклоузер отключён",
        {recloser_id: SwitchPosition.OPEN},
    )
    model.add_operating_state(state)
    return state.id


# 24. Вставка реклоузера в линию.
def test_24_insert_recloser_into_physical_line() -> None:
    project, _, _, _, main, inserted = _inserted_recloser("24")

    assert main.section_id not in project.electrical_model.line_sections
    assert inserted.recloser_id in project.electrical_model.equipment
    assert inserted.left_section_id in project.electrical_model.line_sections
    assert inserted.right_section_id in project.electrical_model.line_sections
    assert project.electrical_model.equipment[
        inserted.recloser_id
    ].type_id == EquipmentTypeId("builtin.recloser")


# 25. После вставки реклоузера созданы два разных узла.
def test_25_insert_recloser_creates_two_distinct_nodes() -> None:
    project, _, _, _, _, inserted = _inserted_recloser("25")

    assert inserted.left_node_id != inserted.right_node_id
    assert inserted.left_node_id in project.electrical_model.electrical_nodes
    assert inserted.right_node_id in project.electrical_model.electrical_nodes
    equipment = project.electrical_model.equipment[inserted.recloser_id]
    assert {
        project.electrical_model.connection_for_port(item).electrical_node_id
        for item in equipment.port_ids
    } == {inserted.left_node_id, inserted.right_node_id}


# 26. Реклоузер включён — путь существует.
def test_26_closed_recloser_keeps_path() -> None:
    project, _, _, _, _, inserted = _inserted_recloser("26")

    topology = TopologyEngine().compile(project.electrical_model)

    assert topology.has_path(inserted.left_node_id, inserted.right_node_id)


# 27. Реклоузер отключён — данный путь разорван.
def test_27_open_recloser_breaks_its_path() -> None:
    project, _, _, _, _, inserted = _inserted_recloser("27")
    state_id = _open_state(project.electrical_model, inserted.recloser_id, "27.open")

    topology = TopologyEngine().compile(project.electrical_model, state_id)

    assert not topology.has_path(inserted.left_node_id, inserted.right_node_id)


# 28. Альтернативное питание после отключения реклоузера.
def test_28_open_recloser_preserves_supply_from_alternative_source() -> None:
    project, controller, start, finish, _, inserted = _inserted_recloser("28")
    first = controller.add_equipment(
        "builtin.external_grid",
        "Источник 1",
        voltage_class_by_group={"main": U10},
    )
    second = controller.add_equipment(
        "builtin.external_grid",
        "Источник 2",
        voltage_class_by_group={"main": U10},
    )
    controller.connect_port_to_node(first.port_ids[0], start)
    controller.connect_port_to_node(second.port_ids[0], finish)
    state_id = _open_state(project.electrical_model, inserted.recloser_id, "28.open")

    topology = TopologyEngine().compile(project.electrical_model, state_id)

    assert topology.is_energized(inserted.left_node_id)
    assert topology.is_energized(inserted.right_node_id)
    assert topology.is_energized(finish)
    assert not topology.has_path(inserted.left_node_id, inserted.right_node_id)


# 29. Реклоузер на отпайке.
def test_29_recloser_can_be_inserted_on_tap_branch() -> None:
    project, controller, _, taps = _tap_chain("29", 1)
    tap = taps[0]

    inserted = controller.insert_recloser(
        tap.branch_section_id,
        250_000,
        "Реклоузер на отпайке",
        properties={"rated_current_a": 400.0, "rated_voltage_v": 10_000},
    )

    assert inserted.recloser_id in project.electrical_model.equipment
    assert inserted.left_logical_line_id == tap.branch_logical_line_id
    assert inserted.left_node_id != inserted.right_node_id


# 30. Undo создания отпайки полностью восстанавливает исходную линию.
def test_30_undo_tap_restores_original_line_and_ids() -> None:
    project, controller = _controller("30")
    start = _node(controller, "Начало")
    finish = _node(controller, "Конец")
    branch_end = _node(controller, "Конец отпайки")
    main = _line(controller, "Основная ВЛ", start, finish, length_mm=5_000_000)
    before_model = electrical_model_to_dict(project.electrical_model)
    before_diagram = diagram_to_dict(project.diagram)

    controller.create_tap(
        main.section_id,
        2_000_000,
        "Отпайка",
        LineKind.CABLE,
        NodeTarget(branch_end),
        physical=_physical(400_000),
    )
    controller.undo()

    assert electrical_model_to_dict(project.electrical_model) == before_model
    assert diagram_to_dict(project.diagram) == before_diagram
    assert main.section_id in project.electrical_model.line_sections


# 31. Undo вставки реклоузера полностью восстанавливает исходную линию.
def test_31_undo_recloser_insertion_restores_original_line() -> None:
    project, controller = _controller("31")
    start = _node(controller, "Начало")
    finish = _node(controller, "Конец")
    main = _line(controller, "Основная ВЛ", start, finish, length_mm=8_000_000)
    before_model = electrical_model_to_dict(project.electrical_model)
    before_diagram = diagram_to_dict(project.diagram)

    controller.insert_recloser(
        main.section_id,
        3_000_000,
        "Реклоузер",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
    )
    controller.undo()

    assert electrical_model_to_dict(project.electrical_model) == before_model
    assert diagram_to_dict(project.diagram) == before_diagram
    assert main.section_id in project.electrical_model.line_sections


# 32. Сохранение и загрузка сохраняют маршруты и топологию.
def test_32_save_load_preserves_routes_ids_and_topology(tmp_path: Path) -> None:
    project, _, _, _, _, _ = _inserted_recloser("32")
    persisted = load_project(EXAMPLE)
    persisted.electrical_model = project.electrical_model
    persisted.diagram = project.diagram
    persisted.catalog_snapshots = project.catalog_snapshots
    persisted.refresh_calculation_view(force=True)
    topology_before = TopologyEngine().compile(
        persisted.electrical_model
    ).semantic_signature()
    route_ids = tuple(persisted.diagram.routes)
    waypoint_ids = tuple(
        point.id
        for route in persisted.diagram.routes.values()
        for point in route.waypoints
    )
    target = tmp_path / "stage4-roundtrip.json"

    save_project(target, persisted)
    reopened = load_project(target)

    assert TopologyEngine().compile(
        reopened.electrical_model
    ).semantic_signature() == topology_before
    assert tuple(reopened.diagram.routes) == route_ids
    assert tuple(
        point.id
        for route in reopened.diagram.routes.values()
        for point in route.waypoints
    ) == waypoint_ids


# 33. Все ID остаются уникальными.
def test_33_all_stage4_ids_are_globally_unique() -> None:
    project, _, _, _, _, _ = _inserted_recloser("33")
    domain_ids = _all_domain_ids(project.electrical_model)
    diagram_ids = [
        project.diagram.id.value,
        *(item.value for item in project.diagram.pages),
        *(item.value for item in project.diagram.representations),
        *(item.value for item in project.diagram.routes),
        *(
            point.id.value
            for route in project.diagram.routes.values()
            for point in route.waypoints
        ),
    ]

    assert len(domain_ids) == len(set(domain_ids))
    assert len(diagram_ids) == len(set(diagram_ids))
    assert set(domain_ids).isdisjoint(diagram_ids)


# 34. Старый core.Network обновляется только через адаптер.
def test_34_editor_has_no_direct_dependency_on_legacy_core_network() -> None:
    import rza_calc.editor.controller as controller_module

    source = inspect.getsource(controller_module)

    assert "rza_calc.core" not in source
    assert "from ..core" not in source
    assert "core.Network" not in source
    assert callable(adapt_to_calculation)


# 35. Неподтверждённая линия не попадает в старый расчёт как достоверная.
def test_35_unconfirmed_line_is_not_trusted_by_legacy_calculation() -> None:
    project, controller = _controller("35")
    start = _node(controller, "Начало")
    finish = _node(controller, "Конец")
    line = _line(
        controller,
        "Неподтверждённая ВЛ",
        start,
        finish,
        length_mm=None,
        confirmed=False,
    )

    adaptation = adapt_to_calculation(project.electrical_model)
    assert len(adaptation.network.branches) == 1
    branch = next(iter(adaptation.network.branches.values()))

    assert branch.length_km == 0.0
    assert branch.calculation_block_reason
    assert any(item.code == "line_data_unconfirmed" for item in adaptation.diagnostics)
