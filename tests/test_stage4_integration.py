# -*- coding: utf-8 -*-
"""Сквозной сценарий Этапа 4 на канонической электрической модели."""
from __future__ import annotations

import json
from pathlib import Path

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    PageId,
    RouteWaypoint,
    RouteWaypointId,
    RouteWaypointSource,
)
from rza_calc.domain.electrical import (
    DataConfirmation,
    ElectricalModel,
    LineKind,
    OperatingState,
    OperatingStateId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.model import ProjectStructure
from rza_calc.editor.controller import (
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.io.diagram import diagram_to_dict
from rza_calc.io.electrical_model import electrical_model_to_dict
from rza_calc.io.project import load_project, save_project
from rza_calc.topology import TopologyEngine


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "tests" / "fixtures" / "legacy_projects" / "gtes_sever.json"
U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _physical(length_mm: int) -> PhysicalLineInput:
    return PhysicalLineInput(
        length_mm,
        DataConfirmation.CONFIRMED,
        {"r1_ohm_per_km": 0.4, "x1_ohm_per_km": 0.3},
        DataConfirmation.CONFIRMED,
    )


def _points(token: str, *coordinates: tuple[float, float]) -> tuple[RouteWaypoint, ...]:
    return tuple(
        RouteWaypoint(
            RouteWaypointId(f"waypoint.stage4.integration.{token}.{index}"),
            x,
            y,
            source=RouteWaypointSource.USER,
            pinned=True,
        )
        for index, (x, y) in enumerate(coordinates)
    )


def _fresh_project():
    project = load_project(EXAMPLE)
    project.electrical_model = ElectricalModel.with_builtins(
        "Сквозной проект Этапа 4"
    )
    page = DiagramPage(PageId("page.stage4.integration"), "Основная схема")
    project.diagram = DiagramDocument.create(
        "Сквозная однолинейная схема",
        (page,),
        document_id=DiagramDocumentId("diagram.stage4.integration"),
    )
    project.structure = ProjectStructure()
    project.catalog_snapshots = ProjectCatalogSnapshots()
    project.refresh_calculation_view(force=True)
    return project


def _canonical_top_level_lists(payload: dict) -> dict:
    """Сравнивать содержимое, не делая порядок словарных коллекций контрактом."""
    result = dict(payload)
    for key, value in result.items():
        if isinstance(value, list):
            result[key] = sorted(
                value,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
    return result


def test_stage4_complete_editing_roundtrip(tmp_path: Path) -> None:
    project = _fresh_project()
    controller = ProjectEditorController(project)

    # Источник 1 -> шина -> выключатель -> физическая магистраль.
    source_1 = controller.add_equipment(
        "builtin.external_grid",
        "Источник 1",
        x=-360.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
    )
    breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-1",
        x=0.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    bus = controller.add_electrical_node(
        "Шины 10 кВ",
        x=-200.0,
        y=0.0,
        voltage_class_id=U10,
        symbol_key="bus_horizontal",
    )
    far_end = controller.add_electrical_node(
        "Конец магистрали",
        x=900.0,
        y=0.0,
        voltage_class_id=U10,
    )
    line_start = controller.add_electrical_node(
        "Начало ВЛ",
        x=150.0,
        y=0.0,
        voltage_class_id=U10,
    )
    controller.connect_port_to_node(source_1.port_ids[0], bus.node_id)
    controller.connect_port_to_node(breaker.port_ids[0], bus.node_id)
    controller.connect_port_to_node(breaker.port_ids[1], line_start.node_id)
    main = controller.create_physical_line(
        "ВЛ-10 кВ №1",
        LineKind.OVERHEAD,
        NodeTarget(line_start.node_id),
        NodeTarget(far_end.node_id),
        physical=_physical(12_000_000),
    )

    # Первая отпайка до реклоузера; Undo/Redo восстанавливает те же ID.
    branch_ends = tuple(
        controller.add_electrical_node(
            f"КТП-{index + 1}",
            x=500.0 + index * 140.0,
            y=220.0,
            voltage_class_id=U10,
        )
        for index in range(3)
    )
    first_tap = controller.create_tap(
        main.section_id,
        2_000_000,
        "Отпайка к КТП-1",
        LineKind.CABLE,
        NodeTarget(branch_ends[0].node_id),
        physical=_physical(500_000),
        tap_x=260.0,
        tap_y=0.0,
    )
    controller.undo()
    assert first_tap.branch_section_id not in controller.model.line_sections
    controller.redo()
    assert first_tap.branch_section_id in controller.model.line_sections

    # Реклоузер расположен дальше по основной линии и создаёт два разных узла.
    inserted = controller.insert_recloser(
        first_tap.second_section_id,
        2_000_000,
        "Реклоузер Р-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
        x=420.0,
        y=0.0,
    )
    controller.undo()
    assert first_tap.second_section_id in controller.model.line_sections
    assert inserted.recloser_id not in controller.model.equipment
    controller.redo()
    assert inserted.recloser_id in controller.model.equipment
    assert inserted.left_node_id != inserted.right_node_id

    # Ещё две отпайки за реклоузером; основная ВЛ продолжена до дальнего конца.
    second_tap = controller.create_tap(
        inserted.right_section_id,
        2_000_000,
        "Отпайка к КТП-2",
        LineKind.CABLE,
        NodeTarget(branch_ends[1].node_id),
        physical=_physical(600_000),
        tap_x=600.0,
        tap_y=0.0,
    )
    third_tap = controller.create_tap(
        second_tap.second_section_id,
        2_000_000,
        "Отпайка к КТП-3",
        LineKind.CABLE,
        NodeTarget(branch_ends[2].node_id),
        physical=_physical(700_000),
        tap_x=760.0,
        tap_y=0.0,
    )
    assert all(
        tap.tap_node_id in controller.model.electrical_nodes
        for tap in (first_tap, second_tap, third_tap)
    )

    # Второй источник подключён с дальнего конца через отдельный выключатель.
    source_2 = controller.add_equipment(
        "builtin.external_grid",
        "Источник 2",
        x=1180.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
    )
    backup_breaker = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-резерв",
        x=1040.0,
        y=0.0,
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.OPEN,
    )
    controller.connect_ports(source_2.port_ids[0], backup_breaker.port_ids[0])
    controller.connect_port_to_node(backup_breaker.port_ids[1], far_end.node_id)

    # Два независимых физических участка пересекаются только графически.
    cross_left = controller.add_electrical_node(
        "Пересечение A", x=0.0, y=500.0, voltage_class_id=U10
    )
    cross_right = controller.add_electrical_node(
        "Пересечение B", x=240.0, y=500.0, voltage_class_id=U10
    )
    cross_top = controller.add_electrical_node(
        "Пересечение C", x=120.0, y=380.0, voltage_class_id=U10
    )
    cross_bottom = controller.add_electrical_node(
        "Пересечение D", x=120.0, y=620.0, voltage_class_id=U10
    )
    horizontal = controller.create_physical_line(
        "Графическое пересечение 1",
        LineKind.OVERHEAD,
        NodeTarget(cross_left.node_id),
        NodeTarget(cross_right.node_id),
        physical=_physical(1_000_000),
        route_waypoints=_points("horizontal", (0.0, 500.0), (240.0, 500.0)),
    )
    controller.create_physical_line(
        "Графическое пересечение 2",
        LineKind.CABLE,
        NodeTarget(cross_top.node_id),
        NodeTarget(cross_bottom.node_id),
        physical=_physical(1_000_000),
        route_waypoints=_points("vertical", (120.0, 380.0), (120.0, 620.0)),
    )
    crossing_topology = TopologyEngine().compile(controller.model)
    assert not crossing_topology.has_path(cross_left.node_id, cross_top.node_id)

    # Редактирование маршрута меняет только Diagram Model.
    electrical_before_reroute = controller.model.connectivity_signature()
    topology_before_reroute = crossing_topology.semantic_signature()
    controller.reroute_diagram_route(
        horizontal.route_id,
        _points(
            "horizontal.rerouted",
            (0.0, 500.0),
            (80.0, 500.0),
            (80.0, 540.0),
            (240.0, 540.0),
        ),
    )
    assert controller.model.connectivity_signature() == electrical_before_reroute
    assert (
        TopologyEngine().compile(controller.model).semantic_signature()
        == topology_before_reroute
    )

    # Открытый реклоузер отделяет путь, но питание со второй стороны учитывается.
    without_backup_id = OperatingStateId("state.stage4.integration.no_backup")
    with_backup_id = OperatingStateId("state.stage4.integration.with_backup")
    project.electrical_model.add_operating_state(OperatingState(
        without_backup_id,
        "Реклоузер отключён, резерв недоступен",
        {
            inserted.recloser_id: SwitchPosition.OPEN,
            backup_breaker.equipment_id: SwitchPosition.OPEN,
        },
    ))
    project.electrical_model.add_operating_state(OperatingState(
        with_backup_id,
        "Реклоузер отключён, резерв включён",
        {
            inserted.recloser_id: SwitchPosition.OPEN,
            backup_breaker.equipment_id: SwitchPosition.CLOSED,
        },
    ))
    without_backup = TopologyEngine().compile(project.electrical_model, without_backup_id)
    with_backup = TopologyEngine().compile(project.electrical_model, with_backup_id)
    assert not without_backup.has_path(inserted.left_node_id, inserted.right_node_id)
    assert without_backup.is_energized(inserted.left_node_id)
    assert not without_backup.is_energized(inserted.right_node_id)
    assert with_backup.is_energized(inserted.left_node_id)
    assert with_backup.is_energized(inserted.right_node_id)
    assert not with_backup.has_path(inserted.left_node_id, inserted.right_node_id)

    # Полный JSON round-trip сохраняет электрические и графические ID и режимы.
    project.refresh_calculation_view(force=True)
    model_before = electrical_model_to_dict(project.electrical_model)
    diagram_before = diagram_to_dict(project.diagram)
    target = tmp_path / "stage4-integrated-project.json"
    save_project(target, project)
    reopened = load_project(target)

    assert _canonical_top_level_lists(
        electrical_model_to_dict(reopened.electrical_model)
    ) == _canonical_top_level_lists(model_before)
    assert _canonical_top_level_lists(
        diagram_to_dict(reopened.diagram)
    ) == _canonical_top_level_lists(diagram_before)
    assert (
        TopologyEngine().compile(
            reopened.electrical_model, with_backup_id
        ).semantic_signature()
        == with_backup.semantic_signature()
    )
    assert not [
        issue
        for issue in reopened.electrical_model.validate_integrity()
        if issue.severity == "error"
    ]
    assert not reopened.diagram.validate_targets(reopened.electrical_model)
