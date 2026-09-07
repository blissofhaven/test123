"""Sequence inputs survive the real project and adapter boundaries."""
import json

import pytest

from rza_calc.adapters.legacy_calculation import adapt_to_calculation
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, LineBranch, Network, Node, SourceBranch, TransformerBranch
from rza_calc.domain import ProjectStructure
from rza_calc.domain.electrical import (
    DataConfirmation, ElectricalModel, ElectricalNode, ElectricalNodeId,
    LineConstructionSegment, LineConstructionSegmentId, LineKind,
    OperatingState, OperatingStateId, VoltageClassId,
)
from rza_calc.io.project import FORMAT_VERSION, ProjectData, load_project, save, save_project


U10 = VoltageClassId("builtin.voltage.ac.10kv")
SEQUENCE_FIELDS = (
    "r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv",
    "negative_sequence_equal_positive", "zero_sequence_connection", "sequence_phase_shift_deg",
)
LINE_SEQUENCE_FIELDS = (
    "r2_ohm_per_km", "x2_ohm_per_km", "r0_ohm_per_km", "x0_ohm_per_km",
)


def _model():
    model = ElectricalModel.with_builtins("Sequence input")
    for name in ("a", "b"):
        model.add_node(ElectricalNode(
            ElectricalNodeId(f"node.{name}"), name, declared_voltage_class_id=U10,
        ))
    model.add_operating_state(OperatingState(OperatingStateId("mode.normal"), "Normal"))
    return model


def _line(model, *, properties=None, segments=None):
    _, section, _ = model.create_logical_line(
        "Sequence line", LineKind.CABLE,
        ElectricalNodeId("node.a"), ElectricalNodeId("node.b"),
        sum(item.length_mm for item in segments) if segments else 1_000_000,
        inherited_properties=properties or {}, construction_segments=segments,
    )
    return section


def _branch(model, section):
    adapted = adapt_to_calculation(model)
    branch_id = adapted.trace.domain_equipment_to_legacy[section.equipment_id.value][0]
    return adapted.network.branches[branch_id]


def _roundtrip(tmp_path, model):
    adaptation = adapt_to_calculation(model)
    project = ProjectData(
        adaptation.network, Methodology.load(), {"name": model.name},
        ProjectStructure(), FORMAT_VERSION, model,
    )
    path = tmp_path / "sequence-project.json"
    save_project(path, project)
    assert json.loads(path.read_text(encoding="utf-8"))["format_version"] == FORMAT_VERSION
    return load_project(path).electrical_model


def _segment(name, length, properties):
    return LineConstructionSegment(
        LineConstructionSegmentId(f"segment.{name}"), LineKind.CABLE, length, properties,
        length_confirmation=DataConfirmation.CONFIRMED,
        impedance_confirmation=DataConfirmation.CONFIRMED,
    )


def test_legacy_sequence_equivalents_survive_v2_import_and_v7_roundtrip(tmp_path):
    net = Network("Explicit equivalents")
    for node in (Node("hv", "HV", 110), Node("lv", "LV", 10), Node("end", "End", 10)):
        net.add_node(node)
    net.add_branch(SourceBranch(
        "source", "Source", GRID, "hv", s_kz_max=1000, s_kz_min=500,
        r2_ohm=0.7, x2_ohm=7.0, r0_ohm=1.8, x0_ohm=12.0,
        sequence_reference_kv=115, zero_sequence_connection="to_ground",
    ))
    net.add_branch(TransformerBranch(
        "transformer", "Transformer", "hv", "lv", s_nom=16000,
        u_hv=110, u_lv=10, uk=10.5, negative_sequence_equal_positive=True,
        r0_ohm=0.4, x0_ohm=3.0, sequence_reference_kv=10.5,
        zero_sequence_connection="to_ground", sequence_phase_shift_deg=30,
    ))
    net.add_branch(LineBranch(
        "line", "Line", "lv", "end", length_km=1.2, r0=0.1, x0=0.2,
        r2_ohm_per_km=0.11, x2_ohm_per_km=0.22,
        r0_ohm_per_km=0.8, x0_ohm_per_km=0.9, zero_sequence_connection="series",
    ))
    legacy_path = tmp_path / "legacy.json"
    save(legacy_path, net)
    project = load_project(legacy_path)
    canonical_path = tmp_path / "canonical.json"
    save_project(canonical_path, project)
    loaded = load_project(canonical_path)
    for branch_id, expected in net.branches.items():
        actual = loaded.network.branches[branch_id]
        for key in SEQUENCE_FIELDS + (LINE_SEQUENCE_FIELDS if isinstance(expected, LineBranch) else ()):
            assert getattr(actual, key) == getattr(expected, key)
    assert loaded.network.branches["line"].r0 == 0.1
    assert loaded.network.branches["line"].x0 == 0.2


def test_absent_sequence_data_stays_absent_after_roundtrip(tmp_path):
    model = _model()
    section = _line(model, properties={"r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2})
    loaded = _roundtrip(tmp_path, model)
    branch = _branch(loaded, section)
    for key in LINE_SEQUENCE_FIELDS + ("r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv", "zero_sequence_connection"):
        assert getattr(branch, key) is None
    assert branch.negative_sequence_equal_positive is False
    assert (branch.r0, branch.x0) == (0.1, 0.2)


def test_native_line_keeps_all_sequences_separate_and_retains_parallel_count(tmp_path):
    model = _model()
    section = _line(model, properties={
        "r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2, "parallel_count": 3,
        "r2_ohm_per_km": 0.12, "x2_ohm_per_km": 0.25,
        "r0_ohm_per_km": 0.6, "x0_ohm_per_km": 0.9,
        "zero_sequence_connection": "series",
    })
    branch = _branch(_roundtrip(tmp_path, model), section)
    assert branch.n_parallel == 3
    assert (branch.r0, branch.x0) == (0.1, 0.2)
    assert tuple(getattr(branch, key) for key in LINE_SEQUENCE_FIELDS) == (0.12, 0.25, 0.6, 0.9)
    assert branch.zero_sequence_connection == "series"


def test_native_source_absolute_equivalent_survives_project_roundtrip(tmp_path):
    model = _model()
    source, _ = model.create_equipment(
        "builtin.external_grid", "Source",
        voltage_class_by_group={"main": U10},
        properties={
            "s_kz_max": 250.0, "s_kz_min": 100.0,
            "r2_ohm": 0.02, "x2_ohm": 0.4,
            "r0_ohm": 0.03, "x0_ohm": 0.8,
            "sequence_reference_kv": 10.5,
            "zero_sequence_connection": "to_ground",
        },
    )
    model.connect_port(model.port_by_role(source.id, "terminal").id, ElectricalNodeId("node.a"))
    adapted = adapt_to_calculation(_roundtrip(tmp_path, model))
    branch = adapted.network.branches[adapted.trace.domain_equipment_to_legacy[source.id.value][0]]
    assert tuple(getattr(branch, name) for name in ("r2_ohm", "x2_ohm", "r0_ohm", "x0_ohm", "sequence_reference_kv")) == (0.02, 0.4, 0.03, 0.8, 10.5)
    assert branch.zero_sequence_connection == "to_ground"


@pytest.mark.parametrize("sequence", [2, 0])
def test_single_native_line_does_not_publish_half_a_sequence_pair(sequence):
    model = _model()
    section = _line(model, properties={
        "r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2,
        f"r{sequence}_ohm_per_km": 0.4,
    })
    branch = _branch(model, section)
    assert getattr(branch, f"r{sequence}_ohm_per_km") is None
    assert getattr(branch, f"x{sequence}_ohm_per_km") is None


def test_composite_sequences_use_each_segment_length_and_parallel_count(tmp_path):
    model = _model()
    segments = (
        _segment("first", 1_000_000, {
            "r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2, "parallel_count": 2,
            "r2_ohm_per_km": 0.2, "x2_ohm_per_km": 0.3,
            "r0_ohm_per_km": 0.6, "x0_ohm_per_km": 0.9,
            "zero_sequence_connection": "series",
        }),
        _segment("second", 500_000, {
            "r1_ohm_per_km": 0.3, "x1_ohm_per_km": 0.4, "parallel_count": 1,
            "r2_ohm_per_km": 0.5, "x2_ohm_per_km": 0.6,
            "r0_ohm_per_km": 1.2, "x0_ohm_per_km": 1.8,
            "zero_sequence_connection": "series",
        }),
    )
    section = _line(model, segments=segments)
    loaded = _roundtrip(tmp_path, model)
    branch = _branch(loaded, section)
    assert branch.n_parallel == 1
    assert branch.length_km == 1.5
    assert (branch.r0, branch.x0) == pytest.approx((0.2 / 1.5, 0.3 / 1.5))
    assert tuple(getattr(branch, key) for key in LINE_SEQUENCE_FIELDS) == pytest.approx((0.35 / 1.5, 0.45 / 1.5, 0.9 / 1.5, 1.35 / 1.5))
    assert branch.zero_sequence_connection == "series"
    assert loaded.line_section_for_equipment(section.equipment_id).construction_segments == segments


@pytest.mark.parametrize("missing_key", LINE_SEQUENCE_FIELDS)
def test_incomplete_composite_pair_is_not_inferred_from_other_segments(tmp_path, missing_key):
    properties = {
        "r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2,
        "r2_ohm_per_km": 0.11, "x2_ohm_per_km": 0.22,
        "r0_ohm_per_km": 0.8, "x0_ohm_per_km": 0.9,
    }
    incomplete = dict(properties)
    del incomplete[missing_key]
    model = _model()
    section = _line(model, segments=(
        _segment("complete", 500_000, properties),
        _segment("incomplete", 500_000, incomplete),
    ))
    loaded = _roundtrip(tmp_path, model)
    branch = _branch(loaded, section)
    sequence = missing_key[1]
    assert getattr(branch, f"r{sequence}_ohm_per_km") is None
    assert getattr(branch, f"x{sequence}_ohm_per_km") is None
    assert loaded.line_section_for_equipment(section.equipment_id).construction_segments[0].properties[missing_key] == properties[missing_key]


@pytest.mark.parametrize("second_equal,expected", [(True, True), (False, False), (None, False)])
def test_composite_equal_sequence_assumption_requires_every_segment(second_equal, expected):
    first = {"r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2, "negative_sequence_equal_positive": True}
    second = {"r1_ohm_per_km": 0.2, "x1_ohm_per_km": 0.3}
    if second_equal is not None:
        second["negative_sequence_equal_positive"] = second_equal
    model = _model()
    section = _line(model, segments=(_segment("one", 500_000, first), _segment("two", 500_000, second)))
    assert _branch(model, section).negative_sequence_equal_positive is expected


@pytest.mark.parametrize("first,second,expected", [
    (None, None, None),
    ("series", None, "series"),
    (None, "series", "series"),
    ("series", "series", "series"),
    ("blocked", "blocked", "blocked"),
    ("blocked", "series", "unsupported_composite"),
    ("series", "blocked", "unsupported_composite"),
    ("blocked", None, "unsupported_composite"),
    ("from_ground", "from_ground", "unsupported_composite"),
    ("to_ground", "to_ground", "unsupported_composite"),
    ("from_ground", "series", "unsupported_composite"),
])
def test_composite_zero_topology_is_never_lost_during_roundtrip(tmp_path, first, second, expected):
    properties = {
        "r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2,
        "r2_ohm_per_km": 0.11, "x2_ohm_per_km": 0.22,
        "r0_ohm_per_km": 0.8, "x0_ohm_per_km": 0.9,
    }
    rows = []
    for name, connection in (("first", first), ("second", second)):
        values = dict(properties)
        if connection is not None:
            values["zero_sequence_connection"] = connection
        rows.append(_segment(name, 500_000, values))
    model = _model()
    section = _line(model, segments=tuple(rows))
    branch = _branch(_roundtrip(tmp_path, model), section)
    assert branch.zero_sequence_connection == expected


@pytest.mark.parametrize("fault_kind", ["1ph_g", "2ph_g"])
def test_incompatible_composite_blocks_only_zero_sequence_faults(fault_kind):
    from rza_calc.core.fault_types import FaultSpec, FaultType
    from rza_calc.core.short_circuit import ShortCircuitSolver, ShortCircuitStatusError

    properties = {
        "r1_ohm_per_km": 0.1, "x1_ohm_per_km": 0.2,
        "r2_ohm_per_km": 0.11, "x2_ohm_per_km": 0.22,
        "r0_ohm_per_km": 0.8, "x0_ohm_per_km": 0.9,
    }
    model = _model()
    _line(model, segments=(
        _segment("barrier", 500_000, properties | {"zero_sequence_connection": "blocked"}),
        _segment("series", 500_000, properties | {"zero_sequence_connection": "series"}),
    ))
    adapted = adapt_to_calculation(model)
    net = adapted.network
    net.add_branch(SourceBranch(
        "source", "Source", GRID, adapted.trace.domain_node_to_legacy["node.a"],
        s_kz_max=250.0, s_kz_min=100.0,
        r2_ohm=0.02, x2_ohm=0.3, r0_ohm=0.03, x0_ohm=0.6,
        sequence_reference_kv=10.5, zero_sequence_connection="to_ground",
    ))
    solver = ShortCircuitSolver(net, next(iter(net.modes.values())), Methodology.load())
    fault_node = adapted.trace.domain_node_to_legacy["node.b"]
    for kind in (FaultType.THREE_PHASE, FaultType.LINE_LINE):
        result = solver.fault_at(fault_node, FaultSpec(kind))
        assert max(abs(value) for value in result.iabc_ka) > 0
    with pytest.raises(ShortCircuitStatusError) as captured:
        solver.fault_at(fault_node, FaultSpec(FaultType(fault_kind)))
    assert captured.value.code == "INVALID_INPUT"
