"""Keep the agreed repository layout usable without Git metadata in a ZIP."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_FILES = (
    "AUDIT-KERNEL.md",
    "CHANGES-FIX-PASS.md",
    "CHANGES-SYMBOLS.md",
    "ROADMAP-REVIEW-D2.md",
    "SAFE_HARDENING.txt",
    "rza-calc-0.3-safe-hardening-defect-status.md",
    "rza-calc-0.3-safe-hardening-manifest.txt",
    "rza-calc-0.3-safe-hardening-report.md",
    "rza-calc-0.3-safe-hardening.patch",
)
TOOLS = (
    "autolayout.py",
    "build_demo_network.py",
    "build_substation_demo.py",
    "rebuild_demo.py",
    "update_baseline.py",
)


def test_historical_materials_are_together_in_archive():
    for name in HISTORICAL_FILES:
        assert (ROOT / "docs" / "archive" / name).is_file(), name
        assert not (ROOT / name).exists(), name
    assert (ROOT / "docs" / "archive" / "README.md").is_file()


def test_development_tools_have_no_obsolete_root_copies():
    for name in TOOLS:
        assert (ROOT / "tools" / name).is_file(), name
        assert not (ROOT / ("tools_" + name)).exists(), name
    assert (ROOT / "tools" / "README.md").is_file()


def test_active_status_points_to_relocated_tools_and_guides():
    status = json.loads((ROOT / "docs" / "roadmap" / "status.json").read_text(encoding="utf-8"))
    assert status["baseline"]["tool"] == "tools/update_baseline.py"
    # Status v2 no longer appoints a historical agent roster. The active
    # baseline guard lives in repository-wide instructions, also shipped in ZIPs.
    assert status["schema"] == "rza-roadmap-status/2"
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert status["baseline"]["tool"] in instructions
    assert "Без `--yes` запись не разрешена" in instructions
    for key in ("tools", "historical_root_materials", "report"):
        assert (ROOT / status["repository_layout"][key]).is_file()

    def values(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from values(child)
        elif isinstance(value, list):
            for child in value:
                yield from values(child)
        elif isinstance(value, str):
            yield value

    for value in values(status):
        if value.startswith("tools/") and value.endswith(".py"):
            assert (ROOT / value).is_file(), value


def test_repository_wide_instructions_and_git_rules_stay_at_root():
    # .git itself is deliberately not required: source ZIPs do not contain it.
    for name in ("AGENTS.md", ".gitignore", ".gitattributes"):
        assert (ROOT / name).is_file(), name
