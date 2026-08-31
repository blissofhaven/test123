"""List every old test change against the verified input archive, without hiding renamed tests."""
import ast
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(r'C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening')
BEFORE = Path(r'C:\Users\shock\Documents\Codex\РЗА — резерв\2026-08-31-connection-cleanup-before-190103\before-connection-cleanup.zip')

def tests(source):
    return {node.name: node for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')}

def assertions(node):
    return [ast.dump(value, include_attributes=False) for value in ast.walk(node) if isinstance(value, ast.Assert)]

records = []
with ZipFile(BEFORE) as archive:
    for name in archive.namelist():
        relative = name.removeprefix('rza-calc-0.3-safe-hardening/')
        if not relative.startswith('tests/') or not relative.endswith('.py'):
            continue
        old = archive.read(name)
        current = (ROOT / relative).read_bytes()
        if old == current:
            continue
        old_tests, new_tests = tests(old.decode('utf-8-sig')), tests(current.decode('utf-8-sig'))
        records.append({'file': relative,
            'removed_or_renamed_test_functions': sorted(old_tests.keys() - new_tests.keys()),
            'added_or_renamed_test_functions': sorted(new_tests.keys() - old_tests.keys()),
            'assertions_changed': sorted(key for key in old_tests.keys() & new_tests.keys()
                                         if assertions(old_tests[key]) != assertions(new_tests[key])),
            'assertions_unchanged_count': sum(assertions(old_tests[key]) == assertions(new_tests[key])
                                             for key in old_tests.keys() & new_tests.keys())})
print(json.dumps({'changed_test_files': records}, ensure_ascii=False, indent=2))
