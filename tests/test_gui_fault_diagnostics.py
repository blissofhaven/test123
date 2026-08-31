"""Console-only fault diagnostics must not disturb an existing handler."""
from __future__ import annotations

import builtins
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rza_calc.gui import app


def _handler(monkeypatch, *, enabled=False):
    calls = []
    monkeypatch.setattr(app.faulthandler, "is_enabled", lambda: enabled)
    monkeypatch.setattr(app.faulthandler, "enable", lambda **kwargs: calls.append(kwargs))
    return calls


def test_existing_fault_handler_is_not_reconfigured(monkeypatch):
    calls = _handler(monkeypatch, enabled=True)
    monkeypatch.setattr(app.sys, "stderr", object())
    app._enable_fault_diagnostics()
    assert calls == []


def test_disabled_handler_uses_existing_stderr_for_all_threads(monkeypatch):
    calls = _handler(monkeypatch)
    stream = io.StringIO()
    monkeypatch.setattr(app.sys, "stderr", stream)
    app._enable_fault_diagnostics()
    assert calls == [{"file": stream, "all_threads": True}]


def test_pythonw_without_stderr_does_not_enable_handler(monkeypatch):
    calls = _handler(monkeypatch)
    monkeypatch.setattr(app.sys, "stderr", None)
    app._enable_fault_diagnostics()
    assert calls == []


@pytest.mark.parametrize("error", (
    AttributeError("no fileno"),
    io.UnsupportedOperation("fileno"),
    ValueError("I/O operation on closed file"),
    OSError(9, "Bad file descriptor"),
))
def test_unsupported_stderr_does_not_break_launch(monkeypatch, error):
    _handler(monkeypatch)
    def unsupported(**kwargs):
        assert kwargs == {"file": sys.stderr, "all_threads": True}
        raise error
    monkeypatch.setattr(app.faulthandler, "enable", unsupported)
    app._enable_fault_diagnostics()


@pytest.mark.parametrize("error", (RuntimeError("handler setup failed"), TypeError("unexpected bug")))
def test_unexpected_handler_errors_are_not_swallowed(monkeypatch, error):
    _handler(monkeypatch)
    def broken(**kwargs):
        raise error
    monkeypatch.setattr(app.faulthandler, "enable", broken)
    with pytest.raises(type(error), match=str(error)):
        app._enable_fault_diagnostics()


def test_main_enables_diagnostics_before_importing_qt(monkeypatch):
    calls = _handler(monkeypatch)
    original_import = builtins.__import__
    def import_without_qt(name, *args, **kwargs):
        if name.startswith("PySide6"):
            assert calls == [{"file": sys.stderr, "all_threads": True}]
            raise ImportError("Qt intentionally unavailable in this test")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_without_qt)
    assert app.main([]) == 2


def test_real_handler_can_dump_stack_to_existing_console_without_loading_qt():
    # An isolated interpreter avoids replacing pytest's own active handler.
    # No crash, GUI, log file or project write is needed to verify the channel.
    code = """
import faulthandler
import io
import sys
from rza_calc.gui.app import _enable_fault_diagnostics
faulthandler.disable()
console = sys.stderr
closed = io.StringIO()
closed.close()
for unavailable in (None, io.StringIO(), closed, object()):
    sys.stderr = unavailable
    _enable_fault_diagnostics()
    assert not faulthandler.is_enabled()
sys.stderr = console
_enable_fault_diagnostics()
assert faulthandler.is_enabled()
sys.stderr = object()
_enable_fault_diagnostics()
assert faulthandler.is_enabled()
sys.stderr = console
assert not any(name.startswith('PySide6') for name in sys.modules)
def safe_stack_snapshot():
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
safe_stack_snapshot()
print('diagnostics-enabled-without-Qt')
"""
    environment = dict(os.environ)
    environment.pop("PYTHONFAULTHANDLER", None)
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "diagnostics-enabled-without-Qt" in result.stdout
    assert "safe_stack_snapshot" in result.stderr
