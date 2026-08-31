# -*- coding: utf-8 -*-
"""Измерительный стенд автоматических узлов на большой реальной модели.

Скрипт намеренно не задаёт порогов времени: он печатает фактический JSON и
Markdown-таблицу конкретного запуска, а успешность определяет только по
электрической, графической и каталожной целостности.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = Path(__file__).resolve().parent
for path in (ROOT, TESTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from benchmark_stage4_connections import (  # noqa: E402
    EXAMPLE,
    build_large_diagram,
    build_large_model,
)
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.electrical import (  # noqa: E402
    DataConfirmation,
    LineKind,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.domain.fingerprint import electrical_model_fingerprint  # noqa: E402
from rza_calc.domain.model import ProjectStructure  # noqa: E402
from rza_calc.editor.controller import (  # noqa: E402
    NodeTarget,
    PhysicalLineInput,
    ProjectEditorController,
)
from rza_calc.gui.editor_scene import (  # noqa: E402
    DiagramGraphicsScene,
    DiagramGraphicsView,
)
from rza_calc.io.project import load_project, save_project  # noqa: E402
from rza_calc.topology import TopologyEngine  # noqa: E402


U10 = VoltageClassId("builtin.voltage.ac.10kv")


def _tap_count(model) -> int:
    return sum(
        node.extensions.get("junction_kind") == "line_tap"
        for node in model.electrical_nodes.values()
    )


def _recloser_count(model) -> int:
    return sum(
        item.type_id.value == "builtin.recloser"
        for item in model.equipment.values()
    )


def _assert_integrity(project) -> None:
    errors = [
        item
        for item in project.electrical_model.validate_integrity()
        if item.severity == "error"
    ]
    if errors:
        raise AssertionError(
            "Электрическая модель повреждена:\n- "
            + "\n- ".join(item.message for item in errors)
        )
    project.diagram.require_valid_targets(project.electrical_model)
    catalog_errors = project.catalog_snapshots.validate_targets(
        project.electrical_model
    )
    if catalog_errors:
        raise AssertionError(
            "Каталожные ссылки повреждены:\n- " + "\n- ".join(catalog_errors)
        )


def _route_probe_point(route) -> QPointF:
    first, second = route.waypoints[0], route.waypoints[1]
    return QPointF((first.x + second.x) / 2.0, (first.y + second.y) / 2.0)


def _seconds(started: float) -> float:
    return round(perf_counter() - started, 6)


def run_benchmark(*, preview_iterations: int = 250) -> dict[str, object]:
    application = QApplication.instance() or QApplication([])
    del application

    started = perf_counter()
    model = build_large_model()
    diagram = build_large_diagram(model)
    # Старый benchmark рисует физический участок одновременно маршрутом и
    # отдельным equipment-представлением. Реальный редактор использует для
    # LineSection только маршрут; иначе штатный split справедливо оставляет
    # лишнее представление со ссылкой на удалённый исходный section.
    diagram = replace(
        diagram,
        representations={
            representation_id: representation
            for representation_id, representation in diagram.representations.items()
            if representation.equipment_id not in model.line_sections
        },
        revision=diagram.revision + 1,
    )
    build_seconds = _seconds(started)

    project = load_project(EXAMPLE)
    project.electrical_model = model
    project.diagram = diagram
    project.catalog_snapshots = ProjectCatalogSnapshots()
    project.structure = ProjectStructure()
    project.refresh_calculation_view(force=True)
    controller = ProjectEditorController(project)
    _assert_integrity(project)

    baseline = {
        "equipment": len(model.equipment),
        "connections": len(model.connections),
        "tap_nodes": _tap_count(model),
        "reclosers": _recloser_count(model),
        "routes": len(diagram.routes),
    }
    assert baseline["equipment"] == 1_000
    assert baseline["connections"] >= 1_500
    assert baseline["tap_nodes"] == 200
    assert baseline["reclosers"] == 50

    scene = DiagramGraphicsScene()
    started = perf_counter()
    scene.sync_document(project.diagram, project.electrical_model)
    scene_sync_seconds = _seconds(started)
    view = DiagramGraphicsView(scene)
    view.set_zoom(1.0)
    physical_route = next(
        route
        for route in project.diagram.routes.values()
        if route.equipment_id in project.electrical_model.line_sections
    )
    probe = _route_probe_point(physical_route)
    fingerprint_before_preview = electrical_model_fingerprint(
        project.electrical_model
    )
    model_revision_before_preview = project.electrical_model.revision
    diagram_before_preview = project.diagram
    journal_before_preview = tuple(controller.journal)
    started = perf_counter()
    for _ in range(preview_iterations):
        hit = scene.physical_route_at(probe)
        if hit is None:
            raise AssertionError("Preview hit-test не нашёл физическую линию.")
    preview_hit_seconds = _seconds(started)
    assert (
        electrical_model_fingerprint(project.electrical_model)
        == fingerprint_before_preview
    )
    assert project.electrical_model.revision == model_revision_before_preview
    assert project.diagram == diagram_before_preview
    assert tuple(controller.journal) == journal_before_preview

    main_line = next(
        line
        for line in project.electrical_model.logical_lines.values()
        if line.name == "Магистральная ВЛ"
    )
    main_sections = tuple(main_line.section_equipment_ids)
    tap_source_id = main_sections[150]
    split_source_id = main_sections[160]
    recloser_source_id = main_sections[170]
    branch_end = controller.add_electrical_node(
        "Конец измерительной отпайки",
        x=3_400.0,
        y=2_000.0,
        voltage_class_id=U10,
    )

    started = perf_counter()
    tap = controller.create_tap(
        tap_source_id,
        500_000,
        "Измерительная отпайка",
        LineKind.CABLE,
        NodeTarget(branch_end.node_id),
        physical=PhysicalLineInput(
            750_000,
            DataConfirmation.CONFIRMED,
            {"r1_ohm_per_km": 0.31, "x1_ohm_per_km": 0.09},
            DataConfirmation.CONFIRMED,
        ),
    )
    create_tap_seconds = _seconds(started)
    assert tap_source_id not in project.electrical_model.line_sections
    assert tap.tap_node_id in project.electrical_model.electrical_nodes
    _assert_integrity(project)

    started = perf_counter()
    split = controller.split_physical_line(split_source_id, 500_000)
    split_seconds = _seconds(started)
    assert split_source_id not in project.electrical_model.line_sections
    assert split.first_section_id in project.electrical_model.line_sections
    assert split.second_section_id in project.electrical_model.line_sections
    _assert_integrity(project)

    before_recloser = electrical_model_fingerprint(project.electrical_model)
    started = perf_counter()
    inserted = controller.insert_recloser(
        recloser_source_id,
        500_000,
        "Измерительный реклоузер",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
        normal_position=SwitchPosition.CLOSED,
    )
    insert_recloser_seconds = _seconds(started)
    assert inserted.recloser_id in project.electrical_model.equipment
    _assert_integrity(project)

    started = perf_counter()
    controller.undo()
    undo_seconds = _seconds(started)
    assert inserted.recloser_id not in project.electrical_model.equipment
    assert recloser_source_id in project.electrical_model.line_sections
    assert electrical_model_fingerprint(project.electrical_model) == before_recloser
    _assert_integrity(project)

    # Save измеряет только штатную запись уже согласованного расчётного вида.
    project.refresh_calculation_view(force=True)
    with tempfile.TemporaryDirectory(prefix="rza-auto-junctions-") as temp_dir:
        target = Path(temp_dir) / "automatic-junctions-large.json"
        started = perf_counter()
        save_project(target, project)
        save_seconds = _seconds(started)
        saved_bytes = target.stat().st_size

        started = perf_counter()
        reopened = load_project(target)
        open_seconds = _seconds(started)

    _assert_integrity(reopened)
    topology = TopologyEngine().compile(reopened.electrical_model)
    assert topology.model_revision == reopened.electrical_model.revision
    assert _recloser_count(reopened.electrical_model) == 50
    assert _tap_count(reopened.electrical_model) == 202
    assert electrical_model_fingerprint(reopened.electrical_model) == (
        electrical_model_fingerprint(project.electrical_model)
    )

    final_scale = {
        "equipment": len(reopened.electrical_model.equipment),
        "connections": len(reopened.electrical_model.connections),
        "tap_nodes": _tap_count(reopened.electrical_model),
        "reclosers": _recloser_count(reopened.electrical_model),
        "routes": len(reopened.diagram.routes),
        "saved_bytes": saved_bytes,
    }
    timings = {
        "build_large_model_and_diagram": build_seconds,
        "scene_sync": scene_sync_seconds,
        f"preview_hit_{preview_iterations}_iterations": preview_hit_seconds,
        "create_tap": create_tap_seconds,
        "split_physical_line": split_seconds,
        "insert_recloser": insert_recloser_seconds,
        "undo_recloser": undo_seconds,
        "save_project": save_seconds,
        "open_project": open_seconds,
    }
    return {
        "baseline": baseline,
        "final": final_scale,
        "timings_seconds": timings,
        "integrity": {
            "electrical": "ok",
            "diagram": "ok",
            "catalog_snapshots": "ok",
            "topology": "ok",
            "roundtrip_fingerprint": "identical",
            "hard_time_limits": False,
        },
    }


def print_report(report: dict[str, object]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print("| Операция | Время, с |")
    print("|---|---:|")
    timings = report["timings_seconds"]
    assert isinstance(timings, dict)
    for operation, seconds in timings.items():
        print(f"| {operation} | {float(seconds):.6f} |")


if __name__ == "__main__":
    result = run_benchmark()
    print_report(result)
