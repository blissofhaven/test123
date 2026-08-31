# -*- coding: utf-8 -*-
"""Воспроизводимое измерение редактора Этапа 3 на 1024 объектах.

Обычный тестовый прогон не должен падать из-за фоновой нагрузки, частоты CPU
или особенностей виртуальной машины. Поэтому этот сценарий:

* всегда проверяет функциональные инварианты и целостность;
* выполняет прогрев и несколько повторов;
* сообщает median и p95 вместе с данными среды;
* по умолчанию считает бюджеты предупреждениями;
* делает бюджеты обязательными только с явным флагом ``--enforce``.

Запуск из корня проекта::

    python -B tests/benchmark_stage3_editor.py
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6 import __version__ as PYSIDE_VERSION  # noqa: E402
from PySide6.QtCore import qVersion  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentationId,
    PageId,
)
from rza_calc.domain.electrical import ElectricalModel  # noqa: E402
from rza_calc.editor import EditorMode, ProjectEditorController  # noqa: E402
from rza_calc.gui.editor_panels import EditorWorkspaceWidget  # noqa: E402
from rza_calc.io.project import load_project, save_project  # noqa: E402


OBJECT_COUNT = 1024
EXAMPLE = (
    PROJECT_ROOT
    / "rza_calc"
    / "examples"
    / "gtes_sever.json"
)

# Широкие инженерные ориентиры для обычного современного ПК. Они не являются
# скрытыми CI-таймаутами: default run только сообщает превышение.
BUDGETS_SECONDS = {
    "create_1024": 2.0,
    "workspace_open_1024": 2.5,
    "select_1024": 0.30,
    "group_move_1024": 0.50,
    "zoom_100": 1.0,
    "pan_100": 1.0,
    "save_1024": 2.0,
    "load_1024": 3.0,
}
MEMORY_BUDGET_MB = 250.0


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _minimal_controller(token: str) -> tuple[_Project, ProjectEditorController]:
    project = _Project(
        ElectricalModel.with_builtins("Проверка производительности"),
        DiagramDocument.create(
            "Большая схема",
            (DiagramPage(PageId(f"page.benchmark.{token}"), "Основная схема"),),
            document_id=DiagramDocumentId(f"diagram.benchmark.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )
    return project, ProjectEditorController(project)


def _populate_1024(
    controller: ProjectEditorController,
) -> tuple[GraphicalRepresentationId, ...]:
    # Пример может быть сохранён в режиме анализа. Нагрузочный стенд явно
    # открывает редактирование вместо неявного обхода защит контроллера.
    controller.set_mode(EditorMode.EDIT)
    first = controller.add_equipment(
        "builtin.load",
        "Нагрузка 1",
        x=0.0,
        y=0.0,
    )
    ids = [first.representation_id]
    while len(ids) < OBJECT_COUNT:
        controller.copy(ids)
        result = controller.paste(
            offset_x=40.0 + len(ids),
            offset_y=20.0,
        )
        ids.extend(result.representation_ids)
    assert len(ids) == OBJECT_COUNT
    return tuple(ids)


def _measure(action: Callable[[], Any], runs: int) -> tuple[list[float], Any]:
    samples: list[float] = []
    last: Any = None
    for _ in range(runs):
        started = time.perf_counter()
        last = action()
        samples.append(time.perf_counter() - started)
    return samples, last


def _summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    rank = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95 + 0.5)))
    return {
        "samples_s": [round(item, 6) for item in samples],
        "median_s": round(statistics.median(samples), 6),
        "p95_s": round(ordered[rank], 6),
    }


def _working_set_bytes() -> int | None:
    """Текущий working set на Windows без дополнительной зависимости psutil."""
    if os.name != "nt":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(
        process,
        ctypes.byref(counters),
        counters.cb,
    )
    return int(counters.working_set_size) if ok else None


def _representation_signature(document: DiagramDocument) -> tuple[Any, ...]:
    return tuple(
        (
            item.id.value,
            item.target_id.value,
            item.page_id.value,
            item.x,
            item.y,
            item.rotation_deg,
            item.label,
            item.extensions,
        )
        for item in document.representations.values()
    )


def run_benchmark(runs: int = 5) -> dict[str, Any]:
    if runs < 3:
        raise ValueError("Для устойчивой медианы нужно не менее трёх прогонов.")
    app = QApplication.instance() or QApplication(["stage3-benchmark"])
    app.processEvents()
    metrics: dict[str, dict[str, Any]] = {}

    create_index = 0

    def create_large() -> tuple[_Project, ProjectEditorController, tuple[GraphicalRepresentationId, ...]]:
        nonlocal create_index
        project, controller = _minimal_controller(f"run{create_index}")
        create_index += 1
        ids = _populate_1024(controller)
        assert len(project.diagram.representations) == OBJECT_COUNT
        assert len(project.electrical_model.equipment) == OBJECT_COUNT
        return project, controller, ids

    samples, large = _measure(create_large, runs)
    metrics["create_1024"] = _summary(samples)
    project, controller, ids = large
    connectivity = project.electrical_model.connectivity_signature()
    electrical_revision = project.electrical_model.revision

    # Прогрев Qt и шрифтов не попадает в измеряемые открытия.
    warmup = EditorWorkspaceWidget(controller)
    warmup.resize(1500, 900)
    warmup.show()
    app.processEvents()
    warmup.close()
    warmup.deleteLater()
    app.processEvents()

    open_samples: list[float] = []
    workspace: EditorWorkspaceWidget | None = None
    for _ in range(runs):
        if workspace is not None:
            workspace.close()
            workspace.deleteLater()
            app.processEvents()
        started = time.perf_counter()
        workspace = EditorWorkspaceWidget(controller)
        workspace.resize(1500, 900)
        workspace.show()
        app.processEvents()
        assert len(workspace.scene._items_by_id) == OBJECT_COUNT
        open_samples.append(time.perf_counter() - started)
    metrics["workspace_open_1024"] = _summary(open_samples)
    assert workspace is not None

    def select_all() -> None:
        workspace.scene.clearSelection()
        workspace.scene.select_representations(ids)
        app.processEvents()
        assert len(workspace.scene.selected_representation_ids()) == OBJECT_COUNT

    samples, _ = _measure(select_all, runs)
    metrics["select_1024"] = _summary(samples)

    controller.set_snap(False)
    journal_before_moves = len(controller.journal)
    move_index = 0

    def move_group() -> None:
        nonlocal move_index
        direction = 1.0 if move_index % 2 == 0 else -1.0
        move_index += 1
        controller.move_representations(ids, 20.0 * direction, 20.0 * direction)
        workspace.refresh()
        app.processEvents()

    samples, _ = _measure(move_group, runs)
    metrics["group_move_1024"] = _summary(samples)
    assert len(controller.journal) == journal_before_moves + runs
    assert project.electrical_model.revision == electrical_revision
    assert project.electrical_model.connectivity_signature() == connectivity

    representations_before_view = dict(project.diagram.representations)

    def zoom_100() -> None:
        for _ in range(50):
            workspace.view.zoom_in()
            workspace.view.zoom_out()
        app.processEvents()

    samples, _ = _measure(zoom_100, runs)
    metrics["zoom_100"] = _summary(samples)

    pan_index = 0

    def pan_100() -> None:
        nonlocal pan_index
        horizontal = workspace.view.horizontalScrollBar()
        vertical = workspace.view.verticalScrollBar()
        for index in range(100):
            horizontal.setValue(horizontal.value() + (1 if index % 2 == 0 else -1))
            vertical.setValue(vertical.value() + (1 if index % 3 else -1))
        pan_index += 1
        state = workspace.view.viewport_state()
        controller.set_view(state.zoom, state.center_x, state.center_y)
        app.processEvents()

    samples, _ = _measure(pan_100, runs)
    metrics["pan_100"] = _summary(samples)
    assert pan_index == runs
    assert project.electrical_model.revision == electrical_revision
    assert dict(project.diagram.representations) == representations_before_view
    assert project.electrical_model.connectivity_signature() == connectivity

    # Сохранение/загрузка измеряются на настоящем ProjectData v6, а не на
    # облегчённой тестовой оболочке контроллера.
    persisted_project = load_project(EXAMPLE)
    persisted_controller = ProjectEditorController(persisted_project)
    equipment_before = set(persisted_project.electrical_model.equipment)
    ports_before = set(persisted_project.electrical_model.ports)
    persisted_ids = _populate_1024(persisted_controller)
    added_equipment_ids = {
        persisted_project.diagram.representations[item].target_id
        for item in persisted_ids
    }
    added_port_ids = set(persisted_project.electrical_model.ports) - ports_before
    assert len(added_equipment_ids) == OBJECT_COUNT
    assert not (added_equipment_ids & equipment_before)
    assert len(added_port_ids) == OBJECT_COUNT
    expected_equipment_ids = set(persisted_project.electrical_model.equipment)
    expected_port_ids = set(persisted_project.electrical_model.ports)
    expected_representations = _representation_signature(persisted_project.diagram)
    expected_connectivity = persisted_project.electrical_model.connectivity_signature()
    with tempfile.TemporaryDirectory(prefix="rza-stage3-benchmark-") as temporary:
        directory = Path(temporary)
        # Прогреваем JSON codec, расчётную проекцию и файловый кэш отдельно от
        # измеряемых прогонов.
        warmup_target = directory / "large-warmup.json"
        save_project(warmup_target, persisted_project)
        warmup_reopened = load_project(warmup_target)
        assert len(warmup_reopened.diagram.representations) == OBJECT_COUNT
        save_samples: list[float] = []
        load_samples: list[float] = []
        sizes: list[int] = []
        for index in range(runs):
            target = directory / f"large-{index}.json"
            started = time.perf_counter()
            save_project(target, persisted_project)
            save_samples.append(time.perf_counter() - started)
            sizes.append(target.stat().st_size)
            started = time.perf_counter()
            reopened = load_project(target)
            load_samples.append(time.perf_counter() - started)
            assert len(reopened.diagram.representations) == OBJECT_COUNT
            assert _representation_signature(reopened.diagram) == expected_representations
            assert reopened.electrical_model.connectivity_signature() == expected_connectivity
            assert set(persisted_ids) == set(reopened.diagram.representations)
            assert set(reopened.electrical_model.equipment) == expected_equipment_ids
            assert set(reopened.electrical_model.ports) == expected_port_ids
            assert added_equipment_ids <= set(reopened.electrical_model.equipment)
            assert added_port_ids <= set(reopened.electrical_model.ports)

        metrics["save_1024"] = _summary(save_samples)
        metrics["load_1024"] = _summary(load_samples)
        file_size_bytes = int(statistics.median(sizes))

    # Учёт памяти выполняется отдельным проходом, потому что tracemalloc
    # существенно замедляет строгий JSON codec и исказил бы latency-метрики.
    working_set_before = _working_set_bytes()
    tracemalloc.start()
    memory_project, memory_controller = _minimal_controller("memory")
    memory_ids = _populate_1024(memory_controller)
    memory_workspace = EditorWorkspaceWidget(memory_controller)
    memory_workspace.resize(1500, 900)
    memory_workspace.show()
    app.processEvents()
    assert len(memory_workspace.scene._items_by_id) == len(memory_ids) == OBJECT_COUNT
    _, python_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    working_set_after = _working_set_bytes()
    working_set_delta = (
        None
        if working_set_before is None or working_set_after is None
        else max(0, working_set_after - working_set_before)
    )

    warnings: list[str] = []
    for name, budget in BUDGETS_SECONDS.items():
        value = float(metrics[name]["median_s"])
        if value > budget:
            warnings.append(
                f"{name}: median {value:.3f} с превышает ориентир {budget:.3f} с"
            )
    memory_delta_mb = (
        None if working_set_delta is None else working_set_delta / (1024 * 1024)
    )
    if memory_delta_mb is not None and memory_delta_mb > MEMORY_BUDGET_MB:
        warnings.append(
            f"working set: {memory_delta_mb:.1f} МБ превышает ориентир {MEMORY_BUDGET_MB:.1f} МБ"
        )

    workspace.close()
    workspace.deleteLater()
    memory_workspace.close()
    memory_workspace.deleteLater()
    app.processEvents()

    return {
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "pyside": PYSIDE_VERSION,
            "qt": qVersion(),
            "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        },
        "object_count": OBJECT_COUNT,
        "runs": runs,
        "metrics": metrics,
        "project_file_size_bytes": file_size_bytes,
        "python_peak_memory_mb": round(python_peak / (1024 * 1024), 3),
        "working_set_delta_mb": (
            None if memory_delta_mb is None else round(memory_delta_mb, 3)
        ),
        "budgets": {
            "seconds": BUDGETS_SECONDS,
            "working_set_delta_mb": MEMORY_BUDGET_MB,
            "policy": (
                "По умолчанию превышения являются предупреждениями; "
                "--enforce предназначен для калиброванной контрольной машины."
            ),
        },
        "warnings": warnings,
        "functional_checks": {
            "stable_object_count": True,
            "one_history_entry_per_group_move": True,
            "move_did_not_change_electrical_revision": True,
            "zoom_pan_did_not_change_electrical_revision": True,
            "save_load_preserved_ids_coordinates_and_connectivity": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Измерить редактор Этапа 3 на 1024 графических объектах."
    )
    parser.add_argument("--runs", type=int, default=5, help="Число повторов, минимум 3.")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Вернуть ненулевой код при превышении ориентиров.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Дополнительно сохранить отчёт в JSON.",
    )
    args = parser.parse_args(argv)
    report = run_benchmark(args.runs)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.json is not None:
        args.json.write_text(output + "\n", encoding="utf-8")
    return 1 if args.enforce and report["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
