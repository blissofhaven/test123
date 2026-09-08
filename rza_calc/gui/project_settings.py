"""Persistent project choice for an explicit interactive application session."""
from __future__ import annotations

from pathlib import Path
import os

from PySide6.QtCore import QSettings


class ProjectSettings:
    """Use stable names, independent of QApplication's global metadata."""

    LAST_PROJECT_KEY = "startup/last_project_path"
    RECENT_PROJECTS_KEY = "startup/recent_project_paths"
    AUTO_OPEN_KEY = "startup/auto_open_last_project"
    RECENT_LIMIT = 12

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings if settings is not None else QSettings("RZA Calc", "РЗА-Про")

    def last_project_path(self) -> Path | None:
        value = self._settings.value(self.LAST_PROJECT_KEY, "")
        return Path(value) if isinstance(value, str) and value.strip() else None

    def remember_project(self, path: str | Path) -> bool:
        resolved = Path(path).expanduser().resolve()
        recent = self._unique_paths([resolved, *self.recent_project_paths()])
        self._settings.setValue(self.LAST_PROJECT_KEY, str(resolved))
        self._settings.setValue(self.RECENT_PROJECTS_KEY, [str(item) for item in recent])
        self._settings.sync()
        return self._settings.status() == self._settings.Status.NoError

    @classmethod
    def _unique_paths(cls, paths) -> tuple[Path, ...]:
        result: list[Path] = []
        seen: set[str] = set()
        for value in paths:
            if not isinstance(value, (str, Path)) or not str(value).strip():
                continue
            try:
                path = Path(value).expanduser().resolve()
            except (OSError, ValueError, RuntimeError):
                continue
            key = os.path.normcase(str(path))
            if key not in seen:
                seen.add(key)
                result.append(path)
                if len(result) == cls.RECENT_LIMIT:
                    break
        return tuple(result)

    def recent_project_paths(self) -> tuple[Path, ...]:
        """Read legacy history without rewriting settings or opening projects."""
        raw = self._settings.value(self.RECENT_PROJECTS_KEY, [])
        rows = raw if isinstance(raw, (list, tuple)) else []
        last = self.last_project_path()
        return self._unique_paths(([last] if last is not None else []) + list(rows))

    def auto_open_last_project(self) -> bool:
        value = self._settings.value(self.AUTO_OPEN_KEY, False)
        return value is True or (isinstance(value, str) and value.casefold() == "true")

    def set_auto_open_last_project(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise TypeError("Автоматическое открытие должно быть логическим значением.")
        self._settings.setValue(self.AUTO_OPEN_KEY, enabled)
        self._settings.sync()
        return self._settings.status() == self._settings.Status.NoError
