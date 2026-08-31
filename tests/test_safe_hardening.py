# -*- coding: utf-8 -*-
"""Регрессии безопасного усиления legacy-расчётного пути после аудита 4.4."""
from __future__ import annotations

import copy
import math

import pytest

from rza_calc.core.engine import CURRENT, STALE, run
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID,
    GeneratorBranch,
    LineBranch,
    Mode,
    Network,
    Node,
    SourceBranch,
    TransformerBranch,
)
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.io.project import load, save
from rza_calc.version import ALGORITHM_VERSION, APPLICATION_VERSION, KERNEL_VERSION


def _network() -> Network:
    net = Network("Safe hardening")
    net.add_node(Node("bus", "Шины 10 кВ", 10.0))
    net.add_node(Node("fault", "Точка КЗ", 10.0, kind="point"))
    net.add_branch(SourceBranch(
        "source", "Система", GRID, "bus",
        s_kz_max=100.0, s_kz_min=80.0,
    ))
    net.add_branch(LineBranch(
        "line", "Линия", "bus", "fault",
        length_km=1.0, r0=0.1, x0=0.4,
    ))
    net.add_mode(Mode("normal", "Нормальный режим"))
    return net


def test_result_has_versioned_calculation_case_and_preliminary_status() -> None:
    net = _network()
    meth = Methodology.load()

    result = run(net, meth)

    assert result.calculation_case is not None
    assert result.calculation_case.application_version == APPLICATION_VERSION
    assert result.calculation_case.kernel_version == KERNEL_VERSION
    assert result.calculation_case.algorithm_version == ALGORITHM_VERSION
    assert len(result.calculation_case.model_fingerprint) == 64
    assert len(result.calculation_case.methodology_fingerprint) == 64
    assert result.preliminary
    assert any("ПРЕДВАРИТЕЛЬНЫЙ" in warning for warning in result.warnings)


def test_result_freshness_tracks_electrical_and_methodology_changes() -> None:
    net = _network()
    meth = Methodology.load()
    result = run(net, meth)

    assert result.freshness_for(net, meth) == CURRENT
    assert result.is_current_for(net, meth)

    net.branches["line"].length_km = 2.0
    assert result.freshness_for(net, meth) == STALE

    net.branches["line"].length_km = 1.0
    changed_meth = Methodology.from_dict(copy.deepcopy(meth.data))
    changed_meth.data["short_circuit"]["temp_factor_min"]["value"] = 1.25
    assert result.freshness_for(net, changed_meth) == STALE


def test_source_with_two_inputs_requires_explicit_primary_mode() -> None:
    net = _network()
    source = net.branches["source"]
    assert isinstance(source, SourceBranch)
    source.i_kz_max = 1.0

    assert any("Выберите один первичный параметр" in item for item in net.validate())

    source.input_mode_max = "power"
    assert not any("режима max" in item and "Выберите" in item for item in net.validate())
    solver = ShortCircuitSolver(net, net.modes["normal"], Methodology.load())
    assert math.isfinite(solver.at("fault").i3)


def test_complex_branch_current_preserves_orientation() -> None:
    forward = _network()
    forward_solver = ShortCircuitSolver(
        forward, forward.modes["normal"], Methodology.load()
    )
    forward_current = forward_solver.branch_current_complex(
        forward.branches["line"], "fault"
    )

    reverse = Network("Reverse")
    reverse.add_node(Node("bus", "Шины 10 кВ", 10.0))
    reverse.add_node(Node("fault", "Точка КЗ", 10.0, kind="point"))
    reverse.add_branch(SourceBranch(
        "source", "Система", GRID, "bus",
        s_kz_max=100.0, s_kz_min=80.0,
    ))
    reverse.add_branch(LineBranch(
        "line", "Линия", "fault", "bus",
        length_km=1.0, r0=0.1, x0=0.4,
    ))
    reverse.add_mode(Mode("normal", "Нормальный режим"))
    reverse_solver = ShortCircuitSolver(
        reverse, reverse.modes["normal"], Methodology.load()
    )
    reverse_current = reverse_solver.branch_current_complex(
        reverse.branches["line"], "fault"
    )

    assert reverse_current == pytest.approx(-forward_current, rel=1e-12, abs=1e-12)
    assert abs(reverse_current) == pytest.approx(abs(forward_current), rel=1e-12)


def test_new_voltage_and_source_semantics_survive_roundtrip(tmp_path) -> None:
    net = _network()
    net.nodes["fault"].calculation_base_kv = 10.0
    net.nodes["fault"].prefault_voltage_kv = 10.4
    source = net.branches["source"]
    assert isinstance(source, SourceBranch)
    source.i_kz_max = 5.0
    source.input_mode_max = "power"
    source.voltage_kv = 10.5

    target = tmp_path / "safe-roundtrip.json"
    save(target, net, {"name": net.name})
    loaded, _, _ = load(target)

    assert loaded.nodes["fault"].calculation_base_kv == 10.0
    assert loaded.nodes["fault"].prefault_voltage_kv == 10.4
    loaded_source = loaded.branches["source"]
    assert isinstance(loaded_source, SourceBranch)
    assert loaded_source.input_mode_max == "power"
    assert loaded_source.voltage_kv == 10.5


def test_generator_rated_voltage_is_distinct_from_network_class() -> None:
    net = Network("Класс сети и паспорт генератора")
    net.add_node(Node("bus", "Шины класса 10 кВ", 10.0))
    generator = net.add_branch(GeneratorBranch(
        "g1",
        "Генератор 10,5 кВ",
        GRID,
        "bus",
        s_nom=25_000.0,
        u_nom=10.5,
        xd2=0.2,
    ))

    assert net.node("bus").u_nom == 10.0
    assert generator.u_nom == 10.5
    assert net.protection_side_u(generator) == 10.5


def test_negative_two_winding_uk_cannot_masquerade_as_internal_star_leg() -> None:
    net = Network("Недопустимый пользовательский 2W")
    net.add_node(Node("hv", "Шины 10 кВ", 10.0))
    net.add_node(Node("lv", "Шины 0,4 кВ", 0.4))
    net.add_branch(SourceBranch(
        "source",
        "Система",
        GRID,
        "hv",
        s_kz_max=100.0,
        s_kz_min=80.0,
    ))
    net.add_branch(TransformerBranch(
        "fake-leg",
        "Обычный 2W с ложным признаком",
        "hv",
        "lv",
        s_nom=1_000.0,
        u_hv=10.0,
        u_lv=0.4,
        uk=-6.0,
        internal_star_leg=True,
    ))
    net.add_mode(Mode("normal", "Нормальный режим"))

    assert any(
        "отрицательное Uк допустимо только для служебного луча 3W" in item
        for item in net.validate()
    )


def test_damaged_numeric_id_is_reported_instead_of_attribute_error() -> None:
    net = Network("Повреждённый ID")
    net.add_node(Node(123, "Шины", 10.0))  # type: ignore[arg-type]

    problems = net.validate()

    assert any("ID должен быть непустой строкой" in item for item in problems)
