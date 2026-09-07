"""Persistent project choice for an explicit interactive application session."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings


class ProjectSettings:
    """Use stable names, independent of QApplication's global metadata."""

    LAST_PROJECT_KEY = "startup/last_project_path"

    def __init__(self) -> None:
        self._settings = QSettings("RZA Calc", "РЗА-Про")

    def last_project_path(self) -> Path | None:
        value = self._settings.value(self.LAST_PROJECT_KEY, "")
        return Path(value) if isinstance(value, str) and value.strip() else None

    def remember_project(self, path: str | Path) -> bool:
        self._settings.setValue(self.LAST_PROJECT_KEY, str(Path(path).resolve()))
        self._settings.sync()
        return self._settings.status() == self._settings.Status.NoError
