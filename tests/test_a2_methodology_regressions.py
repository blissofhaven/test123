"""A2.1: typed claims, trusted choices and durable, self-contained snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from rza_calc.core.engine import CalculationInputError, run
from rza_calc.core.fingerprint import methodology_fingerprint
from rza_calc.core.methodology import (
    APPROVAL_FIELDS, CONFIRMED, PROJECT, Methodology, MethodologyError,
    MethodologySnapshot,
)
from rza_calc.io.project import load


EXAMPLE = Path(__file__).resolve().parents[1] / "rza_calc/examples/ps_severnaya.json"
NON_TEXT = [None, False, True, 0, 12, 1.25, [], ["source"], {}, {"name": "source"}]


def approved_profile() -> Methodology:
    methodology = Methodology.load()
    methodology.data["status"] = PROJECT
    methodology.data["approval"] = {
        "approved_by": "Инженер Иванов И. И.",
        "approved_on": "2026-08-31",
        "object": "ПС Северная",
    }
    for path in methodology.DOCUMENTED:
        node = methodology._node(path)
        target = node if "source" in node else node["meta"]
        target["source_status"] = CONFIRMED
    return methodology


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


@pytest.mark.parametrize("value", NON_TEXT + ["", "  "])
@pytest.mark.parametrize("field", APPROVAL_FIELDS)
def test_project_approval_requires_real_nonempty_text(field, value):
    methodology = approved_profile()
    methodology.data["approval"][field] = value
    errors = methodology.blocking_errors()
    assert any(f"approval.{field}" in error for error in errors)


@pytest.mark.parametrize("value", NON_TEXT + ["", "  "])
@pytest.mark.parametrize("path", ["mtz.k_ots", "short_circuit.meta", "ct.meta"])
def test_named_sources_cannot_be_null_or_stringified_containers(path, value):
    methodology = Methodology.load()
    methodology._node(path)["source"] = value
    errors = methodology.blocking_errors()
    expected_path = path.removesuffix(".meta")
    assert any(expected_path in error and "источник" in error for error in errors)


def test_null_approval_blocks_the_actual_calculation():
    net, _, _ = load(EXAMPLE)
    methodology = approved_profile()
    methodology.data["approval"] = dict.fromkeys(APPROVAL_FIELDS)
    with pytest.raises(CalculationInputError, match="approval.approved_by"):
        run(net, methodology)


def test_valid_project_claim_still_calculates_and_roundtrips():
    net, _, _ = load(EXAMPLE)
    methodology = approved_profile()
    assert not methodology.blocking_errors()
    calculated = run(net, methodology)
    assert calculated.all_results()
    snapshot = calculated.calculation_case.methodology_snapshot
    restored = MethodologySnapshot.from_dict(snapshot.as_dict()).to_methodology()
    assert restored.status == PROJECT
    assert not restored.requires_approval
    assert not restored.blocking_errors()
    assert methodology_fingerprint(restored) == methodology_fingerprint(methodology)


@pytest.mark.parametrize("value", NON_TEXT + ["garbage", "NAMEPLATE", " nameplate "])
def test_implementation_enum_cannot_be_authorized_by_its_own_options(value):
    methodology = Methodology.load()
    methodology.data["to"]["i_nom_basis"].update(value=value, options=[value])
    with pytest.raises(MethodologyError):
        methodology.text("to.i_nom_basis")
    assert methodology.blocking_errors()


@pytest.mark.parametrize("options", [None, [], ["garbage"]])
def test_unknown_variant_is_rejected_even_without_meaningful_options(options):
    methodology = Methodology.load()
    methodology.data["to"]["i_nom_basis"].update(value="garbage", options=options)
    assert methodology.blocking_errors()


def test_options_cannot_offer_an_unimplemented_variant_even_if_not_selected():
    methodology = Methodology.load()
    methodology.data["to"]["i_nom_basis"]["options"].append("garbage")
    assert methodology.blocking_errors()


@pytest.mark.parametrize("value", ["nameplate", "stage_average"])
def test_implemented_choices_work_without_options_and_with_a_valid_subset(value):
    methodology = Methodology.load()
    node = methodology.data["to"]["i_nom_basis"]
    node.update(value=value, options=[value])
    assert not methodology.blocking_errors()
    assert methodology.text("to.i_nom_basis") == value
    del node["options"]
    assert not methodology.blocking_errors()


def test_snapshot_keeps_full_future_metadata_and_exact_fingerprint():
    methodology = Methodology.load()
    methodology.data["future_extension"] = {
        "nested": [{"nullable": None, "bool": True, "integer": 3,
                    "decimal": 3.0, "negative_zero": -0.0,
                    "unicode": "Источник Ω"}],
        "empty": [],
    }
    before = copy.deepcopy(methodology.data)
    snapshot = methodology.snapshot()
    wire = json.loads(json.dumps(snapshot.as_dict(), ensure_ascii=False, allow_nan=False))
    assert wire["schema_version"] == 1
    assert wire["profile_json"] == canonical(before)
    assert wire["profile_sha256"] == hashlib.sha256(wire["profile_json"].encode("utf-8")).hexdigest()
    restored_snapshot = MethodologySnapshot.from_dict(wire)
    restored = restored_snapshot.to_methodology()
    assert restored.data == before
    assert restored.path is None
    assert methodology_fingerprint(restored) == methodology_fingerprint(methodology)
    assert restored_snapshot.as_dict() == snapshot.as_dict()


def test_snapshot_is_deeply_immutable_and_rehydration_is_independent():
    methodology = Methodology.load()
    methodology.data["future_extension"] = {"items": [{"value": 42}]}
    snapshot = methodology.snapshot()
    saved_json = snapshot.profile_json
    saved_references = snapshot.references
    with pytest.raises(FrozenInstanceError):
        snapshot.profile_json = "{}"
    with pytest.raises(TypeError):
        snapshot.references[0] = ("fake", "fake")
    with pytest.raises(FrozenInstanceError):
        snapshot.entry("mtz.k_ots").value = 999
    with pytest.raises(FrozenInstanceError):
        snapshot.entry("mtz.k_ots").provenance.source = "fake"
    methodology.data["future_extension"]["items"][0]["value"] = 999
    methodology.data["references"]["ПУЭ"] = "new source"
    methodology.data["mtz"]["k_ots"]["value"] = 999
    detached = snapshot.as_dict()
    detached["entries"][0]["value"] = 111
    detached["references"]["ПУЭ"] = "export mutation"
    first = snapshot.to_methodology()
    first.data["future_extension"]["items"][0]["value"] = 444
    second = snapshot.to_methodology()
    assert second.data["future_extension"]["items"][0]["value"] == 42
    assert snapshot.profile_json == saved_json
    assert snapshot.references == saved_references
    assert snapshot.value("mtz.k_ots") == 1.1


def test_restore_does_not_read_any_installed_or_external_profile(monkeypatch):
    snapshot = Methodology.load().snapshot()

    def forbidden_load(*args, **kwargs):
        raise AssertionError("snapshot restoration must never load a profile")

    monkeypatch.setattr(Methodology, "load", forbidden_load)
    restored = MethodologySnapshot.from_dict(snapshot.as_dict()).to_methodology()
    assert restored.k("mtz.k_ots") == snapshot.value("mtz.k_ots")


@pytest.mark.parametrize("field", [
    "schema_version", "profile_json", "profile_sha256", "profile_name",
    "profile_id", "status", "entries", "u_avg", "ct_scale", "ct_round_mode",
    "approval", "references",
])
def test_missing_snapshot_fields_are_not_filled_from_defaults(field):
    payload = Methodology.load().snapshot().as_dict()
    del payload[field]
    with pytest.raises(ValueError):
        MethodologySnapshot.from_dict(payload)


@pytest.mark.parametrize("version", [None, False, True, "1", 1.0, 0, 2, [], {}])
def test_snapshot_version_is_explicit_and_strictly_typed(version):
    payload = Methodology.load().snapshot().as_dict()
    payload["schema_version"] = version
    with pytest.raises(ValueError):
        MethodologySnapshot.from_dict(payload)


@pytest.mark.parametrize("raw", [None, {}, [], "{}", "[]", "null", "{",
                                 '{"x":NaN}', '{"x":Infinity}',
                                 '{"x":1,"x":2}'])
def test_malformed_embedded_profile_is_rejected(raw):
    payload = Methodology.load().snapshot().as_dict()
    payload["profile_json"] = raw
    with pytest.raises(ValueError):
        MethodologySnapshot.from_dict(payload)


@pytest.mark.parametrize("field, replacement", [
    ("profile_name", "changed"), ("profile_id", "changed"), ("status", PROJECT),
    ("entries", []), ("u_avg", {"10": 11}), ("ct_scale", [999]),
    ("ct_round_mode", "nearest"), ("approval", {"approved_by": "fake"}),
    ("references", {"ПУЭ": "fake"}), ("profile_sha256", "0" * 64),
])
def test_mirrored_fields_cannot_disagree_with_canonical_profile(field, replacement):
    payload = Methodology.load().snapshot().as_dict()
    payload[field] = replacement
    with pytest.raises(ValueError, match="не согласованы"):
        MethodologySnapshot.from_dict(payload)


def test_coefficient_tampering_and_extra_payload_fields_are_rejected():
    payload = Methodology.load().snapshot().as_dict()
    payload["entries"][0]["value"] = 999
    with pytest.raises(ValueError):
        MethodologySnapshot.from_dict(payload)
    payload = Methodology.load().snapshot().as_dict()
    payload["future_snapshot_version_field"] = "not silently ignored"
    with pytest.raises(ValueError):
        MethodologySnapshot.from_dict(payload)


def test_tampering_with_unknown_profile_metadata_is_detected():
    methodology = Methodology.load()
    methodology.data["future_extension"] = {"value": 1}
    payload = methodology.snapshot().as_dict()
    profile = json.loads(payload["profile_json"])
    profile["future_extension"]["value"] = 2
    payload["profile_json"] = canonical(profile)
    with pytest.raises(ValueError, match="не согласованы"):
        MethodologySnapshot.from_dict(payload)


def test_to_methodology_rechecks_a_manually_inconsistent_snapshot():
    snapshot = Methodology.load().snapshot()
    altered = replace(snapshot, references=(("ПУЭ", "altered"),))
    with pytest.raises(ValueError):
        altered.to_methodology()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                  {1: "key must not silently become a string"},
                                  (1, 2), {1, 2}, object()])
def test_non_json_or_nonfinite_future_metadata_cannot_enter_snapshot(value):
    methodology = Methodology.load()
    methodology.data["future_extension"] = value
    with pytest.raises(ValueError):
        methodology.snapshot()
