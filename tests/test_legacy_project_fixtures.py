"""Removed built-in schemes remain byte-identical regression inputs."""
import hashlib
from pathlib import Path

import pytest

from rza_calc.io import project as project_io
from rza_calc import cli


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/legacy_projects"
ORIGINAL_SHA256 = {
    "energoraion.json": "6bebd1052a76a17070393bf46ceaa776141d0bc58a3c6884f6e993547bee2215",
    "energoraion_v1.json": "1dd4a016c22cf6dceba69700088826ecdfba6410d19550c412aa55f6acaad3ae",
    "four_fault_types.json": "23dda51e4d928fb55ac9f4b9d5362ef0f870ecbd5adc357cb117b1d4c1f77073",
    "gtes_sever.json": "e51a23e41141a260f892117781e1d1eceb8056a118ed32a0a93b7add4182f0e8",
    "gtes_sever_v1.json": "ac88723c7ebb6dbafea40cd857c33e2848bc75e1f7718b382fd0c24f9677af86",
    "ps_promyshlennaya.json": "6ee1a3a97478ca76599012069892d06f97ade8111b38332cbd2e4f569c15bcbe",
    "ps_promyshlennaya_v1.json": "111ec3ebcb71469b3144b74ac584ee29e501570c02171eb540dbbd824d5c3e24",
    "ps_severnaya.json": "263c089c133873deda9dd9f32174f47da58ff95a317e823c0caecc76c8378428",
    "ps_severnaya_v1.json": "140067751b657df9dec4ef691ede03e8b7e9570860144e5f7b55c5c14c9012f9",
}


@pytest.mark.parametrize("filename", tuple(ORIGINAL_SHA256))
def test_old_scheme_is_unchanged_and_loadable_from_fixture_directory(filename):
    path = FIXTURES / filename
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ORIGINAL_SHA256[filename]
    loaded = project_io.load_project(path)
    assert loaded.electrical_model.equipment
    assert not loaded.calculation_blockers
    assert not (ROOT / "rza_calc/examples" / filename).exists()


def test_only_authorized_training_schemes_are_exposed_as_builtin_examples():
    assert {p.name for p in (ROOT / "rza_calc/examples").glob("*.json")} == {
        "oilfield_gtes.json", "compact_training.json",
    }
    assert {p.name for p in FIXTURES.glob("*.json")} == set(ORIGINAL_SHA256)


def test_original_relative_methodology_reference_resolves_to_exact_profile_copy():
    assert (ROOT / "tests/fixtures/data/methodology_default.json").read_bytes() == (
        ROOT / "rza_calc/data/methodology_default.json").read_bytes()


@pytest.mark.parametrize("alias", ("example", "пример"))
def test_cli_builtin_alias_selects_oilfield(alias, monkeypatch):
    seen = []

    class SelectedProject(Exception):
        pass

    def inspect_load(path):
        seen.append(path)
        raise SelectedProject

    monkeypatch.setattr(project_io, "load_project", inspect_load)
    with pytest.raises(SelectedProject):
        cli.main([alias, "check"])
    assert seen == [ROOT / "rza_calc/examples/oilfield_gtes.json"]
