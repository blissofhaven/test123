# -*- coding: utf-8 -*-
"""Численные границы независимого эталона и production-решателя ТКЗ.

Проверки не исправляют алгоритмы этапа 4.4. Обычные тесты подтверждают
наблюдаемое корректное поведение или явно документируют численный порог,
а строгий ``xfail`` фиксирует уже подтверждённый дефект входной границы.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from rza_calc.core.context import Context
from rza_calc.core.engine import CalculationInputError, run
from rza_calc.core.impedance import source_impedance
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import (
    GRID,
    LineBranch,
    Mode,
    Network,
    Node,
    SourceBranch,
    TransformerBranch,
)
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.io.project import load, save

from .reference_solver import (
    GROUND,
    ReferenceBranch,
    ReferenceYBusSolver,
    per_unit_fault_current_ka,
)
from .test_benchmark_cases import (
    _example_1_network,
    _example_2_network,
    _example_3_network,
    _example_4_network,
    _example_5_network,
    _example_6_network,
)


STRICT = {"rel": 1e-9, "abs": 1e-12}


def _line_network(
    impedance: complex,
    *,
    source_power_mva: float = 100.0,
    name: str = "Численная граница",
) -> Network:
    network = Network(name)
    network.add_node(Node("bus", "Шины источника", 10.0))
    network.add_node(Node("fault", "Точка КЗ", 10.0, kind="point"))
    network.add_branch(SourceBranch(
        "source",
        "Система",
        GRID,
        "bus",
        s_kz_max=source_power_mva,
        s_kz_min=source_power_mva,
    ))
    network.add_branch(LineBranch(
        "line",
        "Контрольная ветвь",
        "bus",
        "fault",
        length_km=1.0,
        r0=float(impedance.real),
        x0=float(impedance.imag),
    ))
    network.add_mode(Mode("normal", "Нормальный режим"))
    return network


@pytest.mark.parametrize(
    "impedance",
    (
        complex(math.nan, 0.0),
        complex(math.inf, 0.0),
        complex(0.0, -math.inf),
    ),
    ids=("NaN", "+Infinity", "-Infinity"),
)
def test_reference_branch_rejects_non_finite_impedance(impedance: complex) -> None:
    """Независимый эталон не допускает NaN/Infinity до сборки Ybus."""

    with pytest.raises(ValueError, match="конечным"):
        ReferenceBranch("bad-z", GROUND, "fault", impedance)


@pytest.mark.parametrize(
    ("base_power_mva", "fault_voltage_kv"),
    (
        (0.0, 10.0),
        (-1.0, 10.0),
        (math.nan, 10.0),
        (math.inf, 10.0),
        (100.0, 0.0),
        (100.0, -10.0),
        (100.0, math.nan),
        (100.0, math.inf),
    ),
    ids=(
        "Sб=0",
        "Sб<0",
        "Sб=NaN",
        "Sб=Infinity",
        "Uб=0",
        "Uб<0",
        "Uб=NaN",
        "Uб=Infinity",
    ),
)
def test_reference_per_unit_rejects_invalid_bases(
    base_power_mva: float,
    fault_voltage_kv: float,
) -> None:
    """Ноль, отрицательное и нечисловое значение базиса блокируются явно."""

    with pytest.raises(ValueError, match="положительными"):
        per_unit_fault_current_ka(
            base_power_mva=base_power_mva,
            fault_voltage_kv=fault_voltage_kv,
            total_impedance_pu=complex(0.1, 0.4),
        )


@pytest.mark.parametrize(
    "total_impedance_pu",
    (
        complex(math.nan, 0.0),
        complex(math.inf, 0.0),
        complex(0.0, -math.inf),
    ),
    ids=("Z*=NaN", "Z*=+Infinity", "Z*=-Infinity"),
)
def test_reference_per_unit_rejects_non_finite_impedance(
    total_impedance_pu: complex,
) -> None:
    """NaN/Infinity в ZΣ* блокируются до вычисления тока."""

    with pytest.raises(ValueError, match="конечным"):
        per_unit_fault_current_ka(
            base_power_mva=100.0,
            fault_voltage_kv=10.0,
            total_impedance_pu=total_impedance_pu,
        )


def test_reference_rejects_zero_impedance_before_division() -> None:
    """Нулевое Z не должно превращаться в необработанное деление на ноль."""

    with pytest.raises(ValueError, match="Нулевые ветви"):
        ReferenceBranch("zero", GROUND, "fault", 0.0j)

    with pytest.raises(ValueError, match="равно нулю"):
        per_unit_fault_current_ka(
            base_power_mva=100.0,
            fault_voltage_kv=10.0,
            total_impedance_pu=0.0j,
        )


def test_legacy_core_validation_rejects_infinite_source_data() -> None:
    """Infinity обязан блокироваться так же, как NaN, ещё до построения Ybus."""

    network = _line_network(complex(0.1, 0.4), source_power_mva=math.inf)

    problems = network.validate()

    assert any(
        "конеч" in problem.lower() or "inf" in problem.lower()
        for problem in problems
    ), f"ожидалась ошибка конечности Sкз; фактически {problems!r}"


def test_zero_source_value_stops_before_division() -> None:
    """Sкз=0 не вызывает ZeroDivisionError внутри формулы сопротивления."""

    network = _line_network(complex(0.1, 0.4), source_power_mva=0.0)

    with pytest.raises(ValueError, match="положительн"):
        ShortCircuitSolver(
            network,
            network.modes["normal"],
            Methodology.load(),
        )


def test_finite_source_input_overflow_is_not_returned_as_a_current() -> None:
    """Крайнее конечное Sкз переполняет Zс, но обычный ток не выдаётся."""

    network = _line_network(complex(0.1, 0.4), source_power_mva=1.0e-320)
    source = network.branches["source"]
    assert isinstance(source, SourceBranch)
    with pytest.raises(ValueError, match="переполн"):
        source_impedance(source, Methodology.load(), 10.0)

    with pytest.raises(ValueError, match="переполн"):
        ShortCircuitSolver(
            network,
            network.modes["normal"],
            Methodology.load(),
        )


def test_zero_transformer_power_is_blocked_before_impedance_division() -> None:
    """Публичный ``run`` отсекает Sном=0 до формулы Zт с делением на Sном."""

    network = Network("Нулевая мощность трансформатора")
    network.add_node(Node("hv", "ВН", 10.0))
    network.add_node(Node("lv", "НН", 0.4))
    network.add_branch(SourceBranch(
        "source",
        "Система",
        GRID,
        "hv",
        s_kz_max=100.0,
        s_kz_min=100.0,
    ))
    network.add_branch(TransformerBranch(
        "transformer",
        "Трансформатор",
        "hv",
        "lv",
        s_nom=0.0,
        u_hv=10.0,
        u_lv=0.4,
        uk=6.0,
    ))
    network.add_mode(Mode("normal", "Нормальный режим"))

    with pytest.raises(CalculationInputError, match="не задана Sном"):
        run(network, Methodology.load())


@pytest.mark.parametrize(
    ("impedance_ohm", "is_merged"),
    (
        (0.0, True),
        (5.0e-13, True),
        (2.0e-12, False),
    ),
    ids=("Z=0", "Z ниже порога", "Z выше порога"),
)
def test_production_zero_impedance_threshold_is_reproducible(
    impedance_ohm: float,
    is_merged: bool,
) -> None:
    """Фиксируется фактическая граница объединения узлов ``1e-12 Ом``."""

    network = _line_network(complex(impedance_ohm, 0.0))
    solver = ShortCircuitSolver(
        network,
        network.modes["normal"],
        Methodology.load(),
    )

    assert (solver._rep["bus"] == solver._rep["fault"]) is is_merged
    assert ("line" not in solver.branch_z) is is_merged
    assert math.isfinite(solver.at("fault").i3)

    if impedance_ohm:
        reference = ReferenceYBusSolver(
            ("fault",),
            (ReferenceBranch("line", GROUND, "fault", impedance_ohm),),
        )
        assert reference.thevenin_impedance("fault") == pytest.approx(
            impedance_ohm,
            rel=0.0,
            abs=1e-30,
        )


def test_very_large_impedance_produces_finite_small_current() -> None:
    """Большое конечное Z остаётся конечной ветвью и сверяется с эталоном."""

    impedance = complex(1.0e9, 4.0e9)
    network = _line_network(
        impedance,
        source_power_mva=1.0e18,
        name="Очень большое сопротивление",
    )
    production = ShortCircuitSolver(
        network,
        network.modes["normal"],
        Methodology.load(),
    ).at("fault").i3
    reference = ReferenceYBusSolver(
        ("fault",),
        (ReferenceBranch("line", GROUND, "fault", impedance),),
    ).fault_current_ka("fault", 10.5)

    assert 0.0 < production < 1e-8
    assert math.isfinite(production)
    assert production == pytest.approx(reference, rel=1e-9, abs=1e-18)


@pytest.mark.parametrize(
    "impedance_ratio",
    (2.0, 1.0e6),
    ids=("отношение Z=2", "отношение Z=1e6"),
)
def test_unequal_parallel_branches_match_independent_ybus(
    impedance_ratio: float,
) -> None:
    """Обычные и сильно неравные параллели делят ток по проводимостям."""

    first_z = complex(0.1, 0.4)
    second_z = first_z * impedance_ratio
    reference = ReferenceYBusSolver(
        ("fault",),
        (
            ReferenceBranch("first", GROUND, "fault", first_z),
            ReferenceBranch("second", GROUND, "fault", second_z),
        ),
    )

    network = Network("Неравные параллельные ветви")
    network.add_node(Node("bus", "Шины источника", 10.0))
    network.add_node(Node("fault", "Точка КЗ", 10.0))
    network.add_branch(SourceBranch(
        "source",
        "Идеальный источник",
        GRID,
        "bus",
        s_kz_max=1.0e18,
        s_kz_min=1.0e18,
    ))
    for branch_id, impedance in (("first", first_z), ("second", second_z)):
        network.add_branch(LineBranch(
            branch_id,
            branch_id,
            "bus",
            "fault",
            length_km=1.0,
            r0=impedance.real,
            x0=impedance.imag,
        ))
    mode = network.add_mode(Mode("normal", "Нормальный режим"))
    solver = ShortCircuitSolver(network, mode, Methodology.load())

    expected_total = reference.fault_current_ka("fault", 10.5)
    actual_total = solver.at("fault").i3
    assert actual_total == pytest.approx(expected_total, **STRICT)
    expected_factors = {
        "first": impedance_ratio / (impedance_ratio + 1.0),
        "second": 1.0 / (impedance_ratio + 1.0),
    }
    for branch_id, expected_factor in expected_factors.items():
        reference_factor = abs(reference.branch_transfer_factor(branch_id, "fault"))
        actual_factor = solver.distribution_factor(network.branches[branch_id], "fault")
        assert reference_factor == pytest.approx(expected_factor, **STRICT)
        assert actual_factor == pytest.approx(reference_factor, **STRICT)


def test_complex_mesh_matches_independent_ybus_and_kirchhoff() -> None:
    """Четырёхузловая mesh-сеть сверяется по Zth и токам двух путей к КЗ."""

    source_power_mva = 2205.0
    impedances = {
        "source": complex(0.0, 10.5**2 / source_power_mva),
        "bus-a": complex(0.08, 0.25),
        "bus-b": complex(0.12, 0.35),
        "a-b": complex(0.20, 0.60),
        "a-fault": complex(0.10, 0.30),
        "b-fault": complex(0.09, 0.28),
    }
    reference = ReferenceYBusSolver(
        ("bus", "a", "b", "fault"),
        (
            ReferenceBranch("source", GROUND, "bus", impedances["source"]),
            ReferenceBranch("bus-a", "bus", "a", impedances["bus-a"]),
            ReferenceBranch("bus-b", "bus", "b", impedances["bus-b"]),
            ReferenceBranch("a-b", "a", "b", impedances["a-b"]),
            ReferenceBranch("a-fault", "a", "fault", impedances["a-fault"]),
            ReferenceBranch("b-fault", "b", "fault", impedances["b-fault"]),
        ),
    )

    network = Network("Независимый mesh-пример")
    for node_id in ("bus", "a", "b", "fault"):
        network.add_node(Node(node_id, node_id, 10.0))
    network.add_branch(SourceBranch(
        "source",
        "Система",
        GRID,
        "bus",
        s_kz_max=source_power_mva,
        s_kz_min=source_power_mva,
    ))
    for branch_id, node_from, node_to in (
        ("bus-a", "bus", "a"),
        ("bus-b", "bus", "b"),
        ("a-b", "a", "b"),
        ("a-fault", "a", "fault"),
        ("b-fault", "b", "fault"),
    ):
        impedance = impedances[branch_id]
        network.add_branch(LineBranch(
            branch_id,
            branch_id,
            node_from,
            node_to,
            length_km=1.0,
            r0=impedance.real,
            x0=impedance.imag,
        ))
    mode = network.add_mode(Mode("normal", "Нормальный режим"))
    solver = ShortCircuitSolver(network, mode, Methodology.load())

    expected_z = reference.thevenin_impedance("fault")
    expected_i = reference.fault_current_ka("fault", 10.5)
    actual = solver.at("fault")
    assert actual.z_th == pytest.approx(expected_z, **STRICT)
    assert actual.i3 == pytest.approx(expected_i, **STRICT)

    incoming = {
        branch_id: reference.branch_transfer_factor(branch_id, "fault")
        for branch_id in ("a-fault", "b-fault")
    }
    assert sum(incoming.values(), start=0.0j) == pytest.approx(-1.0 + 0.0j, **STRICT)
    for branch_id, expected_factor in incoming.items():
        actual_factor = solver.distribution_factor(
            network.branches[branch_id], "fault"
        )
        assert actual_factor == pytest.approx(abs(expected_factor), **STRICT)


def test_reference_solver_rejects_singular_ybus() -> None:
    """Полностью изолированный узел даёт явную диагностику вырожденной Ybus."""

    reference = ReferenceYBusSolver(("island",), ())
    assert math.isinf(reference.condition_number)
    with pytest.raises(ValueError, match="вырождена"):
        reference.thevenin_impedance("island")


def test_production_solver_rejects_non_passive_line_before_ybus() -> None:
    """Недопустимое отрицательное Z блокируется до сборки Ybus."""

    network = Network("Вырожденная Ybus")
    network.add_node(Node("bus", "Шины", 10.0))
    network.add_node(Node("fault", "Точка КЗ", 10.0))
    network.add_branch(SourceBranch(
        "source",
        "Система",
        GRID,
        "bus",
        s_kz_max=100.0,
        s_kz_min=100.0,
    ))
    network.add_branch(LineBranch(
        "positive",
        "+Z",
        "bus",
        "fault",
        length_km=1.0,
        r0=0.1,
        x0=0.4,
    ))
    network.add_branch(LineBranch(
        "negative",
        "-Z",
        "bus",
        "fault",
        length_km=1.0,
        r0=-0.1,
        x0=-0.4,
    ))
    mode = network.add_mode(Mode("normal", "Нормальный режим"))

    with pytest.raises(ValueError, match="неотрицательн"):
        ShortCircuitSolver(network, mode, Methodology.load())


def test_near_singular_case_matches_reference_with_known_condition_number() -> None:
    """Почти вырожденная малая схема остаётся сверяемой по независимому решению."""

    source_z = complex(0.0, 1.0)
    line_z = complex(1.0e12, 0.0)
    reference = ReferenceYBusSolver(
        ("bus", "fault"),
        (
            ReferenceBranch("source", GROUND, "bus", source_z),
            ReferenceBranch("line", "bus", "fault", line_z),
        ),
    )
    assert reference.condition_number > 1.0e11
    expected = reference.fault_current_ka("fault", 10.5)

    # Uср(10 кВ)=10,5 кВ, поэтому Sкз=110,25 МВА задаёт |Zс|=1 Ом.
    network = _line_network(
        line_z,
        source_power_mva=110.25,
        name="Почти вырожденная Ybus",
    )
    solver = ShortCircuitSolver(
        network,
        network.modes["normal"],
        Methodology.load(),
    )
    actual = solver.at("fault").i3

    assert np.all(np.isfinite(solver._Z))
    assert actual == pytest.approx(expected, rel=1e-6, abs=1e-18)


def test_two_nearly_ideal_sources_match_independent_ybus() -> None:
    """Два очень мощных источника учитываются параллельно без NaN/Infinity."""

    source_power_mva = 1.0e12
    source_z = complex(0.0, 10.5**2 / source_power_mva)
    line_z = complex(0.1, 0.4)
    reference = ReferenceYBusSolver(
        ("bus", "fault"),
        (
            ReferenceBranch("source-a", GROUND, "bus", source_z),
            ReferenceBranch("source-b", GROUND, "bus", source_z),
            ReferenceBranch("line", "bus", "fault", line_z),
        ),
    )

    network = Network("Два почти идеальных источника")
    network.add_node(Node("bus", "Шины", 10.0))
    network.add_node(Node("fault", "Точка КЗ", 10.0))
    for source_id in ("source-a", "source-b"):
        network.add_branch(SourceBranch(
            source_id,
            source_id,
            GRID,
            "bus",
            s_kz_max=source_power_mva,
            s_kz_min=source_power_mva,
        ))
    network.add_branch(LineBranch(
        "line",
        "Линия",
        "bus",
        "fault",
        length_km=1.0,
        r0=line_z.real,
        x0=line_z.imag,
    ))
    mode = network.add_mode(Mode("normal", "Нормальный режим"))
    solver = ShortCircuitSolver(network, mode, Methodology.load())
    actual = solver.at("fault").i3
    expected = reference.fault_current_ka("fault", 10.5)

    assert {"source-a", "source-b"}.issubset(solver.branch_z)
    assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12)


def _fault_signature(solver: ShortCircuitSolver, node_id: str) -> tuple[object, ...]:
    try:
        result = solver.at(node_id)
    except KeyError as exc:
        return ("не запитан", str(exc))
    return (
        result.node_id,
        result.regime,
        result.z_th.real,
        result.z_th.imag,
        result.i3,
        result.i2,
    )


def _control_example_signature(
    example_id: str,
    network: Network,
    methodology: Methodology,
) -> tuple[object, ...]:
    """Только численные выходы обязательного примера, без времени/UUID."""

    if example_id == "1":
        solver = ShortCircuitSolver(network, network.modes["normal"], methodology)
        return (_fault_signature(solver, "fault"),)
    if example_id == "2":
        solver = ShortCircuitSolver(network, network.modes["normal"], methodology)
        return (
            _fault_signature(solver, "fault"),
            solver.distribution_factor(network.branches["a"], "fault"),
            solver.distribution_factor(network.branches["b"], "fault"),
        )
    if example_id == "3":
        solver = ShortCircuitSolver(network, network.modes["normal"], methodology)
        return (_fault_signature(solver, "lv"),)
    if example_id == "4":
        solver = ShortCircuitSolver(network, network.modes["normal"], methodology)
        return tuple(
            _fault_signature(solver, node_id) for node_id in ("j1", "j2", "j3")
        )
    if example_id == "5":
        values: list[object] = []
        for mode_id in ("both", "one", "none"):
            solver = ShortCircuitSolver(network, network.modes[mode_id], methodology)
            values.append((mode_id, _fault_signature(solver, "fault")))
            if mode_id == "both":
                values.extend(
                    solver.distribution_factor(network.branches[branch_id], "fault")
                    for branch_id in ("path_1", "path_2")
                )
        return tuple(values)
    if example_id == "6":
        context = Context(network, methodology)
        mode = network.modes["audit"]
        solver = context.solvers[mode.id]
        branch_currents = tuple(
            context.current_through(mode, network.branches[branch_id], "n04")[0]
            for branch_id in ("l10", "l110", "g1")
        )
        return (_fault_signature(solver, "n04"), *branch_currents)
    raise AssertionError(f"Неизвестный контрольный пример {example_id}")


@pytest.mark.parametrize(
    ("example_id", "builder"),
    (
        ("1", _example_1_network),
        ("2", _example_2_network),
        ("3", _example_3_network),
        ("4", _example_4_network),
        ("5", _example_5_network),
        ("6", _example_6_network),
    ),
    ids=("пример 1", "пример 2", "пример 3", "пример 4", "пример 5", "пример 6"),
)
def test_control_example_repeat_and_save_reload_are_identical(
    example_id: str,
    builder,
    tmp_path,
) -> None:
    """Каждый пример 1–6 повторяется и переживает закрытый JSON round-trip."""

    methodology = Methodology.load()
    network = builder()
    first = _control_example_signature(example_id, network, methodology)
    repeated = _control_example_signature(example_id, network, methodology)
    assert repeated == first

    project_path = tmp_path / f"audit-example-{example_id}.json"
    save(project_path, network, {"name": f"Контрольный пример {example_id}"})
    loaded, loaded_methodology, _ = load(project_path)
    after_reload = _control_example_signature(
        example_id,
        loaded,
        loaded_methodology,
    )
    assert after_reload == first
