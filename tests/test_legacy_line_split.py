"""Legacy taps preserve conductor data and never weaken a protection silently."""
from dataclasses import asdict, replace

import pytest

from rza_calc.adapters import adapt_to_calculation, import_legacy_network
from rza_calc.core.impedance import line_impedance
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import GRID, LineBranch, Mode, Network, Node, ProtectionSettings, SourceBranch
from rza_calc.core.short_circuit import ShortCircuitSolver
from rza_calc.domain.electrical import DomainInvariantError, EquipmentAvailability, OperatingState, OperatingStateId, thaw_json
from rza_calc.domain.history import ElectricalModelMemento
from rza_calc.editor.legacy_line_split import REVIEW_MARKER, SPLIT_MARKER, legacy_line_split_info, split_legacy_line
from tools.build_oilfield_demo import build_data


@pytest.fixture(scope="module")
def demo():
    network, manifest = build_data()
    return network, import_legacy_network(network)


def _equipment(model, legacy_id):
    return next(row for row in model.equipment.values()
                if row.extensions.get("legacy_calculation", {}).get("legacy_id") == legacy_id)


def _network():
    network = Network("Line tap")
    network.add_node(Node("S", "Шины", 10))
    network.add_node(Node("E", "Конец", 10))
    network.add_branch(SourceBranch("GRID1", "Источник", GRID, "S", s_kz_max=100, s_kz_min=80,
        r2_ohm=.1, x2_ohm=1, r0_ohm=.3, x0_ohm=1.5,
        sequence_reference_kv=10.5, zero_sequence_connection="series"))
    network.add_branch(LineBranch("L", "КЛ", "S", "E", length_km=2.5, r0=.24, x0=.09,
        n_parallel=2, r2_ohm_per_km=.26, x2_ohm_per_km=.11,
        r0_ohm_per_km=.8, x0_ohm_per_km=.33, zero_sequence_connection="series",
        ic_per_km=.7, prot=ProtectionSettings(mtz=False, to=False, ozz=False)))
    network.add_mode(Mode("max", "Максимум", system="max"))
    network.add_mode(Mode("min", "Минимум", system="min"))
    return network


def test_every_saved_demo_line_is_eligible_without_changes(demo):
    network, model = demo
    before = ElectricalModelMemento.capture(model)
    found = []
    for branch in network.branches.values():
        if not isinstance(branch, LineBranch):
            continue
        info = legacy_line_split_info(model, _equipment(model, branch.id).id)
        assert info.length_mm == round(branch.length_km * 1_000_000)
        found.append(info)
    assert len(found) == 244
    assert sum(info.protected for info in found) == 66
    assert ElectricalModelMemento.capture(model) == before


def test_all_protected_demo_lines_preserve_ct_payload_and_require_review(demo):
    network, original = demo
    count = 0
    for branch in network.branches.values():
        if not isinstance(branch, LineBranch) or branch.ct_ratio is None:
            continue
        model = original._transaction_copy()
        equipment = _equipment(model, branch.id)
        old = thaw_json(equipment.properties["legacy_payload"])
        info = legacy_line_split_info(model, equipment.id)
        result = split_legacy_line(model, equipment.id, info.length_mm // 3)
        first = model.equipment[result.first_section_id]
        second = model.equipment[result.second_section_id]
        first_data, second_data = first.properties["legacy_payload"], second.properties["legacy_payload"]
        assert first.id == equipment.id and first.port_ids == equipment.port_ids
        for key in old.keys() - {"length_km"}:
            assert thaw_json(first_data[key]) == old[key]
        assert first.extensions[REVIEW_MARKER]["required"] is True
        assert first.extensions[REVIEW_MARKER]["original_to_node_id"] == info.to_node_id.value
        assert second_data["ct_ratio"] is None and second_data["ct_node"] is None
        assert not any(second_data["prot"][key] for key in ("mtz", "to", "ozz"))
        assert first_data["length_km"] + second_data["length_km"] == pytest.approx(branch.length_km, rel=2e-15)
        assert model.node_for_port(result.first_from_port_id).id == info.from_node_id
        assert model.node_for_port(result.first_to_port_id).id == result.tap_node_id
        assert model.node_for_port(result.second_from_port_id).id == result.tap_node_id
        assert model.node_for_port(result.second_to_port_id).id == info.to_node_id
        count += 1
    assert count == 66


def test_unprotected_demo_split_keeps_all_five_modes_and_other_dto_data(demo):
    network, original = demo
    model = original._transaction_copy()
    equipment = _equipment(model, "ktp01_f1_1_line")
    original_payload = thaw_json(equipment.properties["legacy_payload"])
    before_modes = dict(model.operating_states)
    result = split_legacy_line(model, equipment.id, 50_000)
    adapted = adapt_to_calculation(model)
    assert not [issue for issue in adapted.diagnostics if issue.severity == "error"]
    assert dict(model.operating_states) == before_modes
    assert len(adapted.network.modes) == 5
    second_id = adapted.trace.domain_equipment_to_legacy[result.second_section_id.value][0]
    first = adapted.network.branches["ktp01_f1_1_line"]
    second = adapted.network.branches[second_id]
    methodology = Methodology.load()
    for mode_id, mode in network.modes.items():
        assert asdict(adapted.network.modes[mode_id]) == asdict(mode)
        old_z = line_impedance(network.branches[first.id], methodology, mode.system)[0]
        new_z = line_impedance(first, methodology, mode.system)[0] + line_impedance(second, methodology, mode.system)[0]
        assert new_z == pytest.approx(old_z, rel=1e-14)
    for key, branch in network.branches.items():
        if key != first.id:
            assert asdict(adapted.network.branches[key]) == asdict(branch)
    assert REVIEW_MARKER not in model.equipment[equipment.id].extensions
    assert original_payload["length_km"] == pytest.approx(first.length_km + second.length_km)


@pytest.mark.parametrize("fault", ["3ph", "2ph", "1ph_g", "2ph_g"])
@pytest.mark.parametrize("mode_id", ["max", "min"])
def test_split_preserves_original_endpoint_currents_for_each_fault_and_mode(fault, mode_id):
    from rza_calc.core.fault_types import FaultSpec, FaultType
    network = _network()
    model = import_legacy_network(network)
    source = _equipment(model, "L")
    result = split_legacy_line(model, source.id, 900_000)
    adapted = adapt_to_calculation(model).network
    methodology = Methodology.load()
    spec = FaultSpec(FaultType(fault))
    first = ShortCircuitSolver(network, network.modes[mode_id], methodology).fault_at("E", spec)
    second = ShortCircuitSolver(adapted, adapted.modes[mode_id], methodology).fault_at("E", spec)
    assert second.iabc_ka == pytest.approx(first.iabc_ka, rel=1e-11, abs=1e-12)
    assert second.vabc_kv == pytest.approx(first.vabc_kv, rel=1e-11, abs=1e-12)
    assert second.z012_ohm == pytest.approx(first.z012_ohm, rel=1e-11, abs=1e-12)
    assert result.first_section_id == source.id


def test_whole_line_availability_is_copied_to_both_pieces_in_both_state_forms():
    network = _network()
    network.modes["min"].availability["L"] = False
    model = import_legacy_network(network)
    source = _equipment(model, "L")
    result = split_legacy_line(model, source.id, 900_000)
    adapted = adapt_to_calculation(model)
    second_core_id = adapted.trace.domain_equipment_to_legacy[result.second_section_id.value][0]
    state = next(state for state in model.operating_states.values() if state.system == "min")
    assert state.availability[source.id] is EquipmentAvailability.OUT_OF_SERVICE
    assert state.availability[result.second_section_id] is EquipmentAvailability.OUT_OF_SERVICE
    assert state.extensions["legacy_calculation"]["availability"]["L"] is False
    assert state.extensions["legacy_calculation"]["availability"][second_core_id] is False
    assert not adapted.network.branch_conducting(adapted.network.branches["L"], adapted.network.modes["min"])
    assert not adapted.network.branch_conducting(adapted.network.branches[second_core_id], adapted.network.modes["min"])


@pytest.mark.parametrize("offset", [None, True, -1, 0, 2_500_000, 2_500_001, 1.2])
def test_bad_position_is_rejected_before_any_model_mutation(offset):
    model = import_legacy_network(_network())
    before = ElectricalModelMemento.capture(model)
    with pytest.raises(DomainInvariantError):
        split_legacy_line(model, _equipment(model, "L").id, offset)
    assert ElectricalModelMemento.capture(model) == before


@pytest.mark.parametrize("updates", [
    {"switchable": True}, {"normally_closed": False}, {"switch_with": "GRID1"},
    {"r0": None}, {"x0": -1}, {"length_km": 0}, {"length_km": 1.0000001},
    {"r2_ohm": .2}, {"sequence_phase_shift_deg": 0},
    {"ct_ratio": [100, 5], "ct_node": "E"},
    {"ct_ratio": [100, 5], "ct_node": None},
    {"prot": asdict(ProtectionSettings(mtz=True, to=True))},
    {"unexpected_property": 7},
])
def test_unsupported_legacy_inputs_reject_atomically(updates):
    model = import_legacy_network(_network())
    source = _equipment(model, "L")
    properties = thaw_json(source.properties)
    properties["legacy_payload"].update(updates)
    model.update_equipment_properties(source.id, properties)
    before = ElectricalModelMemento.capture(model)
    with pytest.raises(DomainInvariantError):
        split_legacy_line(model, source.id, 100_000)
    assert ElectricalModelMemento.capture(model) == before


def test_existing_protection_review_and_original_source_survive_repeated_split(demo):
    _, original = demo
    model = original._transaction_copy()
    source = _equipment(model, "cp01_in1_line")
    first = split_legacy_line(model, source.id, 2_000_000)
    initial_origin = model.equipment[source.id].extensions[SPLIT_MARKER]
    second = split_legacy_line(model, source.id, 1_000_000)
    current = model.equipment[source.id]
    assert current.extensions[SPLIT_MARKER] == initial_origin
    assert current.extensions[REVIEW_MARKER]["original_length_km"] == 17
    assert current.extensions[REVIEW_MARKER]["original_to_node_id"] == first.original_to_node_id.value
    assert current.extensions[REVIEW_MARKER]["split_node_ids"] == (first.tap_node_id.value, second.tap_node_id.value)
    adaptation = adapt_to_calculation(model)
    assert any(issue.code == "line_protection_zone_review_required" for issue in adaptation.diagnostics)
    assert adaptation.network.branches["cp01_in1_line"].calculation_block_reason


@pytest.mark.parametrize("at_end", [False, True])
def test_float_length_cannot_turn_a_positive_piece_into_zero_or_unchanged_total(at_end):
    network = _network()
    network.branches["L"].length_km = 1e20
    model = import_legacy_network(network)
    source = _equipment(model, "L")
    total = legacy_line_split_info(model, source.id).length_mm
    before = ElectricalModelMemento.capture(model)
    with pytest.raises(DomainInvariantError, match="точностью"):
        split_legacy_line(model, source.id, total - 1 if at_end else 1)
    assert ElectricalModelMemento.capture(model) == before


def test_failure_after_staged_reconnection_does_not_leak_partial_line(monkeypatch):
    from rza_calc.domain.electrical import ElectricalModel
    model = import_legacy_network(_network())
    source = _equipment(model, "L")
    before = ElectricalModelMemento.capture(model)
    def reject_tail(*args, **kwargs):
        raise DomainInvariantError("Вторая половина не прошла проверку")
    monkeypatch.setattr(ElectricalModel, "create_equipment", reject_tail)
    with pytest.raises(DomainInvariantError, match="Вторая половина"):
        split_legacy_line(model, source.id, 900_000)
    assert ElectricalModelMemento.capture(model) == before
