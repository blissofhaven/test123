"""Catch the Windows GUI launch failure introduced by LF-only repackaging."""
from pathlib import Path


def test_gui_batch_uses_crlf_without_bom():
    """Check bytes on every OS, including the packaging host without Qt.

    cmd.exe parsed this UTF-8 batch file in fragments after CRLF became LF.
    It even returned zero without opening the GUI, so exit status alone did
    not detect the failure. Read bytes: read_text() normalizes line endings.
    """
    launcher = Path(__file__).resolve().parent.parent / "ЗАПУСК.bat"
    data = launcher.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "Save ЗАПУСК.bat without a UTF-8 BOM"
    assert b"\r\n" in data, "Preserve Windows CRLF in ЗАПУСК.bat when repackaging"
    remaining = data.replace(b"\r\n", b"")
    assert b"\n" not in remaining and b"\r" not in remaining, (
        "ЗАПУСК.bat must not contain bare LF/CR line endings"
    )
