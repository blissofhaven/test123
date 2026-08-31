# -*- coding: utf-8 -*-
"""Диагностический стенд производительности текущего ядра ТКЗ.

Скрипт не является функциональным тестом и не подключается к приложению.
Он строит синтетическую цепочку непосредственно в ``core.Network`` и измеряет:

* формирование расчётной модели;
* общую сборку ``ShortCircuitSolver``;
* время плотного обращения матрицы ``numpy.linalg.inv``;
* остаток времени на топологию и заполнение матрицы;
* расчёт КЗ во всех узлах;
* расчёт токораспределения по ветвям;
* повторную сборку после переключения одного аппарата;
* рабочий набор процесса и оценку памяти плотной комплексной матрицы.

По умолчанию выполняются размеры 10, 100 и 1000 узлов. Размер 5000 не
запускается автоматически: одна плотная матрица complex128 такого порядка
занимает около 381 МиБ, а обращение требует одновременно несколько матриц и
кубического времени. Явный запуск допускается только с ``--include-5000``.

Пример безопасного запуска из корня проекта::

    python -B tests/calculation_audit/benchmark_kernel.py --runs 1

Этот стенд использует закрытые диагностические атрибуты решателя только для
измерения. Он не является рекомендуемым публичным API будущего ядра.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import platform
import statistics
import sys
import tracemalloc
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterator

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rza_calc.core.short_circuit as short_circuit_module  # noqa: E402
from rza_calc.core.methodology import Methodology  # noqa: E402
from rza_calc.core.model import (  # noqa: E402
    GRID,
    LineBranch,
    Mode,
    Network,
    Node,
    SourceBranch,
    TieBranch,
)
from rza_calc.core.short_circuit import ShortCircuitSolver  # noqa: E402
from rza_calc.io.project import FORMAT_VERSION  # noqa: E402


COMPLEX_BYTES = np.dtype(np.complex128).itemsize


@dataclass(frozen=True)
class Measurement:
    node_count: int
    branch_count: int
    matrix_order_closed: int
    matrix_order_after_switch: int
    model_formation_s: float
    solver_total_s: float
    matrix_inverse_s: float
    topology_and_matrix_assembly_s: float
    all_fault_nodes_s: float
    branch_currents_s: float
    recalc_after_switch_s: float
    recalc_matrix_inverse_s: float
    dense_matrix_mib: float
    conservative_dense_working_set_mib: float
    python_tracemalloc_peak_mib: float
    working_set_before_mib: float | None
    working_set_after_mib: float | None
    working_set_delta_mib: float | None
    fault_current_checksum_ka: float
    branch_current_checksum_ka: float


def _working_set_bytes() -> int | None:
    """Вернуть текущий working set на Windows без зависимости ``psutil``."""
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


def _available_physical_memory_bytes() -> int | None:
    """Вернуть доступную физическую память Windows для защитного порога."""
    if os.name != "nt":
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.length = ctypes.sizeof(status)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return int(status.available_physical) if ok else None


def _mib(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024.0 * 1024.0), 3)


def _dense_matrix_bytes(order: int) -> int:
    return order * order * COMPLEX_BYTES


def _conservative_dense_working_bytes(order: int) -> int:
    # Y, обратная матрица и как минимум одна рабочая копия LAPACK.
    return 3 * _dense_matrix_bytes(order)


def build_chain_network(node_count: int) -> tuple[Network, Mode, Mode]:
    """Построить связную цепь и параллельный нулевой коммутационный путь."""
    if node_count < 2:
        raise ValueError("Число узлов должно быть не меньше двух.")

    network = Network(f"Диагностическая цепочка, {node_count} узлов")
    for index in range(node_count):
        network.add_node(Node(f"n{index}", f"Узел {index}", 10.0, kind="point"))

    network.add_branch(SourceBranch(
        id="source",
        name="Внешняя система",
        node_from=GRID,
        node_to="n0",
        s_kz_max=500.0,
        s_kz_min=300.0,
        x_r_ratio=10.0,
    ))
    for index in range(node_count - 1):
        network.add_branch(LineBranch(
            id=f"line{index}",
            name=f"Линия {index}",
            node_from=f"n{index}",
            node_to=f"n{index + 1}",
            line_type="overhead",
            length_km=0.1,
            r0=0.12,
            x0=0.40,
            n_parallel=1,
        ))

    switch_index = min(node_count - 2, max(0, node_count // 2 - 1))
    switch_id = "switch.middle"
    network.add_branch(TieBranch(
        id=switch_id,
        name="Контрольный коммутационный аппарат",
        node_from=f"n{switch_index}",
        node_to=f"n{switch_index + 1}",
        normally_closed=True,
    ))
    closed = Mode("mode.closed", "Аппарат включён", system="max")
    opened = Mode(
        "mode.opened",
        "Аппарат отключён",
        states={switch_id: False},
        system="max",
    )
    network.add_mode(closed)
    network.add_mode(opened)
    problems = network.validate()
    if problems:
        raise ValueError("Синтетическая сеть не прошла проверку:\n- " + "\n- ".join(problems))
    return network, closed, opened


@contextmanager
def _timed_inverse(samples: list[float]) -> Iterator[None]:
    """Измерить настоящий ``numpy.linalg.inv`` без изменения результата."""
    original: Callable[[Any], Any] = short_circuit_module.np.linalg.inv

    def wrapper(matrix: Any) -> Any:
        started = perf_counter()
        try:
            return original(matrix)
        finally:
            samples.append(perf_counter() - started)

    short_circuit_module.np.linalg.inv = wrapper
    try:
        yield
    finally:
        short_circuit_module.np.linalg.inv = original


def measure_once(node_count: int, methodology: Methodology) -> Measurement:
    gc.collect()
    working_before = _working_set_bytes()
    tracemalloc.start()
    try:
        started = perf_counter()
        network, closed, opened = build_chain_network(node_count)
        model_formation = perf_counter() - started

        inverse_samples: list[float] = []
        started = perf_counter()
        with _timed_inverse(inverse_samples):
            solver = ShortCircuitSolver(network, closed, methodology)
        solver_total = perf_counter() - started
        inverse_time = inverse_samples[-1]

        started = perf_counter()
        fault_checksum = sum(
            solver.at(node_id).i3 for node_id in sorted(network.nodes)
        )
        all_faults = perf_counter() - started

        fault_node = f"n{node_count - 1}"
        fault_current = solver.at(fault_node).i3
        started = perf_counter()
        branch_checksum = 0.0
        for branch_id in sorted(solver.branch_z):
            branch = network.branches[branch_id]
            branch_checksum += solver.distribution_factor(
                branch, fault_node
            ) * fault_current
        branch_currents = perf_counter() - started

        switched_inverse_samples: list[float] = []
        started = perf_counter()
        with _timed_inverse(switched_inverse_samples):
            switched_solver = ShortCircuitSolver(network, opened, methodology)
        recalc_after_switch = perf_counter() - started
        switched_inverse = switched_inverse_samples[-1]

        _, peak = tracemalloc.get_traced_memory()
        working_after = _working_set_bytes()
        dense_bytes = _dense_matrix_bytes(len(solver._idx))
        conservative_bytes = _conservative_dense_working_bytes(len(solver._idx))
        return Measurement(
            node_count=node_count,
            branch_count=len(network.branches),
            matrix_order_closed=len(solver._idx),
            matrix_order_after_switch=len(switched_solver._idx),
            model_formation_s=model_formation,
            solver_total_s=solver_total,
            matrix_inverse_s=inverse_time,
            topology_and_matrix_assembly_s=max(0.0, solver_total - inverse_time),
            all_fault_nodes_s=all_faults,
            branch_currents_s=branch_currents,
            recalc_after_switch_s=recalc_after_switch,
            recalc_matrix_inverse_s=switched_inverse,
            dense_matrix_mib=float(_mib(dense_bytes)),
            conservative_dense_working_set_mib=float(_mib(conservative_bytes)),
            python_tracemalloc_peak_mib=float(_mib(peak)),
            working_set_before_mib=_mib(working_before),
            working_set_after_mib=_mib(working_after),
            working_set_delta_mib=(
                None
                if working_before is None or working_after is None
                else _mib(max(0, working_after - working_before))
            ),
            fault_current_checksum_ka=fault_checksum,
            branch_current_checksum_ka=branch_checksum,
        )
    finally:
        tracemalloc.stop()


def _summary(measurements: list[Measurement]) -> dict[str, Any]:
    rows = [asdict(item) for item in measurements]
    timing_fields = (
        "model_formation_s",
        "solver_total_s",
        "matrix_inverse_s",
        "topology_and_matrix_assembly_s",
        "all_fault_nodes_s",
        "branch_currents_s",
        "recalc_after_switch_s",
        "recalc_matrix_inverse_s",
    )
    medians = {
        field: round(statistics.median(float(row[field]) for row in rows), 6)
        for field in timing_fields
    }
    return {
        "runs": rows,
        "median_s": medians,
    }


def run_benchmark(
    sizes: list[int],
    runs: int,
    *,
    include_5000: bool,
) -> dict[str, Any]:
    if runs < 1:
        raise ValueError("Число повторов должно быть не меньше одного.")
    normalized = sorted(set(sizes))
    if any(size < 2 for size in normalized):
        raise ValueError("Каждый размер должен быть не меньше двух узлов.")
    if 5000 in normalized and not include_5000:
        normalized.remove(5000)
    if include_5000 and 5000 not in normalized:
        normalized.append(5000)
        normalized.sort()

    available = _available_physical_memory_bytes()
    estimate_5000 = _conservative_dense_working_bytes(5000)
    results: list[dict[str, Any]] = []
    methodology = Methodology.load()
    for size in normalized:
        if size == 5000 and available is not None and available < 2 * estimate_5000:
            results.append({
                "node_count": 5000,
                "status": "пропущено по защите памяти",
                "available_physical_memory_mib": _mib(available),
                "conservative_dense_working_set_mib": _mib(estimate_5000),
                "reason_ru": (
                    "Доступной физической памяти меньше двойной консервативной "
                    "оценки плотного обращения матрицы."
                ),
            })
            continue
        try:
            measurements = [measure_once(size, methodology) for _ in range(runs)]
        except Exception as exc:  # отчёт должен сохранить точную ошибку размера
            results.append({
                "node_count": size,
                "status": "ошибка",
                "error_type": type(exc).__name__,
                "error_ru": str(exc),
            })
        else:
            results.append({
                "node_count": size,
                "status": "измерено",
                **_summary(measurements),
            })

    if not include_5000:
        results.append({
            "node_count": 5000,
            "status": "не запускалось по умолчанию",
            "one_dense_complex128_matrix_mib": _mib(_dense_matrix_bytes(5000)),
            "conservative_dense_working_set_mib": _mib(estimate_5000),
            "available_physical_memory_mib": _mib(available),
            "reason_ru": (
                "Текущее ядро обращает плотную матрицу целиком; операция имеет "
                "квадратичную память и кубическое время. Для явного запуска "
                "используйте --include-5000."
            ),
        })

    return {
        "audit_stage": "4.4",
        "kind": "диагностический benchmark текущего ядра ТКЗ",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "project_format_version": FORMAT_VERSION,
        },
        "methodology": {
            "name": methodology.name,
            "is_placeholder": methodology.is_placeholder,
            "warning": methodology.warning,
        },
        "runs_per_size": runs,
        "results": results,
        "measurement_limitations_ru": [
            "Фаза сборки включает обход активной топологии и заполнение плотной матрицы.",
            "Время numpy.linalg.inv измеряется обёрткой без изменения численного результата.",
            "tracemalloc не гарантирует полный учёт нативной памяти NumPy/LAPACK.",
            "Стенд строит core.Network напрямую и не измеряет Domain-to-adapter projection.",
            "Результаты зависят от процессора, BLAS, нагрузки системы и числа потоков.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Измерить производительность текущего плотного ядра ТКЗ."
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[10, 100, 1000],
        help="Размеры моделей; по умолчанию: 10 100 1000.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Число повторов каждого размера; по умолчанию 1.",
    )
    parser.add_argument(
        "--include-5000",
        action="store_true",
        help=(
            "Явно разрешить тяжёлое обращение плотной матрицы 5000×5000. "
            "Может занять много минут и памяти."
        ),
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Дополнительно сохранить машиночитаемый отчёт по указанному пути.",
    )
    args = parser.parse_args(argv)
    report = run_benchmark(
        list(args.sizes),
        args.runs,
        include_5000=args.include_5000,
    )
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    print(output)
    if args.json is not None:
        args.json.write_text(output + "\n", encoding="utf-8")
    return 1 if any(item.get("status") == "ошибка" for item in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
