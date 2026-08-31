# -*- coding: utf-8 -*-
"""
Мини-раннер на случай, когда pytest недоступен (офлайн-машина, чистая
установка Python). На машине с pytest тесты запускаются обычным `pytest -q`.
"""
from __future__ import annotations

import inspect
import math
import re
import sys
import tempfile
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

if "pytest" not in sys.modules:
    stub = types.ModuleType("pytest")

    def fixture(fn=None, **kw):
        def wrap(f):
            f.__is_fixture__ = True
            return f
        return wrap(fn) if fn else wrap

    class _Raises:
        def __init__(self, exc, match=None):
            self.exc = exc
            self.match = match
            self.value = None
        def __enter__(self): return self
        def __exit__(self, t, v, tb):
            if t is None:
                raise AssertionError(f"ожидалось исключение {self.exc.__name__}, его не было")
            if not issubclass(t, self.exc):
                return False
            if self.match is not None and re.search(self.match, str(v)) is None:
                raise AssertionError(
                    f"текст исключения {str(v)!r} не соответствует {self.match!r}"
                )
            self.value = v
            return True

    class _Approx:
        def __init__(self, expected, rel=1e-12, abs=1e-12):
            self.expected, self.rel, self.abs = expected, rel, abs
        def __eq__(self, actual):
            return math.isclose(actual, self.expected, rel_tol=self.rel, abs_tol=self.abs)

    stub.fixture = fixture
    stub.raises = lambda exc, match=None: _Raises(exc, match)
    stub.approx = lambda expected, rel=1e-12, abs=1e-12: _Approx(expected, rel, abs)
    stub.skip = lambda msg="": (_ for _ in ()).throw(AssertionError("skip: " + msg))
    sys.modules["pytest"] = stub

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_calculation_baseline  # noqa: E402
import test_control_examples  # noqa: E402
import test_core  # noqa: E402
import test_demo_diagram  # noqa: E402
import test_electrical  # noqa: E402
import test_domain  # noqa: E402
import test_integration  # noqa: E402
import test_gui_view_model  # noqa: E402
import test_project_format  # noqa: E402
import test_safety  # noqa: E402
import test_stage1_domain  # noqa: E402
import test_roadmap_consistency  # noqa: E402
import test_stage1_adapter_extensions  # noqa: E402
import test_stage1_catalog  # noqa: E402
import test_stage1_project_v4  # noqa: E402
import test_stage1_project_v3  # noqa: E402
import test_stage1_recloser_topology  # noqa: E402
import test_stage1_symbols  # noqa: E402
import test_stage1_taps_reclosers  # noqa: E402
import test_stage2_project_v5  # noqa: E402
import test_stage2_topology_connectivity  # noqa: E402
import test_stage2_topology_queries_legacy_scale  # noqa: E402
import test_stage2_topology_state_cache  # noqa: E402
import test_stage2_topology_voltage  # noqa: E402
import test_stage25_lines_history  # noqa: E402
import test_stage25_fault_locations  # noqa: E402
import test_stage25_graphical_isolation  # noqa: E402
import test_stage25_projection_diagram_catalog  # noqa: E402
import test_stage25_project_v6  # noqa: E402
import test_stage3_project_commands  # noqa: E402
import test_stage3_editor_gui  # noqa: E402
import test_stage3_requirements  # noqa: E402
import test_stage4_domain_connections  # noqa: E402
import test_stage4_editor_interaction  # noqa: E402
import test_stage4_integration  # noqa: E402
import test_stage4_project_v7  # noqa: E402
import test_stage4_requirements  # noqa: E402
import test_stage4_saved_route_editing  # noqa: E402
import test_stage4_validation  # noqa: E402
import test_diagram_endpoint_index  # noqa: E402
import test_history_validation_cache  # noqa: E402


class _MonkeyPatch:
    def __init__(self):
        self._changes = []

    def setattr(self, target, name, value):
        self._changes.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, value in reversed(self._changes):
            setattr(target, name, value)
        self._changes.clear()


def main() -> int:
    modules = (test_core, test_domain, test_electrical, test_integration,
               test_gui_view_model,
               test_project_format, test_safety, test_stage1_domain,
               test_stage1_adapter_extensions, test_stage1_catalog,
               test_stage1_project_v4,
               test_stage1_project_v3,
               test_stage1_recloser_topology, test_stage1_symbols,
               test_stage1_taps_reclosers,
               test_stage2_project_v5,
               test_stage2_topology_connectivity,
               test_stage2_topology_queries_legacy_scale,
               test_stage2_topology_state_cache,
               test_stage2_topology_voltage,
               test_stage25_lines_history,
               test_stage25_fault_locations,
               test_stage25_graphical_isolation,
               test_stage25_projection_diagram_catalog,
               test_stage25_project_v6,
               test_stage3_project_commands,
               test_stage3_editor_gui,
               test_stage3_requirements,
               test_stage4_domain_connections,
               test_stage4_editor_interaction,
               test_stage4_integration,
               test_stage4_project_v7,
               test_stage4_requirements,
               test_stage4_saved_route_editing,
               test_stage4_validation,
               test_diagram_endpoint_index,
               test_history_validation_cache,
               test_demo_diagram,
               test_calculation_baseline,
               test_control_examples,
               test_roadmap_consistency)
    fixtures = {name: fn for module in modules for name, fn in vars(module).items()
                if callable(fn) and getattr(fn, "__is_fixture__", False)}
    tests = [(f"{module.__name__}.{name}", fn)
             for module in modules
             for name, fn in sorted(vars(module).items())
             if name.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in tests:
        kwargs = {}
        monkeypatches = []
        for p in inspect.signature(fn).parameters:
            if p == "tmp_path":
                kwargs[p] = Path(tempfile.mkdtemp())
            elif p == "monkeypatch":
                patcher = _MonkeyPatch()
                monkeypatches.append(patcher)
                kwargs[p] = patcher
            elif p in fixtures:
                kwargs[p] = fixtures[p]()
            else:
                raise RuntimeError(f"нет фикстуры '{p}' для теста {name}")
        try:
            fn(**kwargs)
            passed += 1
            print(f"  [OK] {name}")
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"  [FAIL] {name}")
        finally:
            for patcher in reversed(monkeypatches):
                patcher.undo()
    print()
    for name, tb in failed:
        print("-" * 70)
        print(name)
        print(tb)
    print(f"Пройдено: {passed}/{len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
