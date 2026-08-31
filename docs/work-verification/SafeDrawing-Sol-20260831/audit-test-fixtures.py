"""Compare existing test assertions structurally against the pre-edit ZIP."""
import ast
import json
from pathlib import Path
from zipfile import ZipFile

project = Path(r"C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening")
checkpoint = Path(r"C:\Users\shock\Documents\Codex\РЗА — резерв\2026-08-31-safe-drawing-before-174841\before-safe-drawing.zip")
policy_old = "test_03_unknown_port_inherits_effective_node_voltage"
policy_new = "test_03_explicit_unknown_port_connection_is_rejected_atomically"


def assertions(text):
    return {node.name: [ast.dump(child, include_attributes=False)
                        for child in ast.walk(node) if isinstance(child, ast.Assert)]
            for node in ast.parse(text).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


changed = []
with ZipFile(checkpoint) as archive:
    for entry in archive.infolist():
        relative = entry.filename.removeprefix("rza-calc-0.3-safe-hardening/")
        if not relative.startswith("tests/test_") or not relative.endswith(".py"):
            continue
        old, new = archive.read(entry), (project / relative).read_bytes()
        if old == new:
            continue
        before, after = assertions(old.decode("utf-8-sig")), assertions(new.decode("utf-8-sig"))
        changed_assertions = []
        for name, previous in before.items():
            if name == policy_old and relative == "tests/test_stage4_requirements.py":
                assert name not in after and policy_new in after
                changed_assertions.append({"old": name, "new": policy_new, "reason": "Explicit user policy: reject existing unknown endpoint"})
            else:
                assert name in after, (relative, "removed function", name)
                assert previous == after[name], (relative, "unexpected assertion change", name)
        changed.append({"file": relative, "assertion_changes": changed_assertions})
print(json.dumps({"existing_test_files_changed": changed,
                  "all_other_preexisting_assertions_identical": True}, ensure_ascii=False, indent=2))
