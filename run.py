# -*- coding: utf-8 -*-
"""
Запуск из любой папки.

Обычный способ (python -m rza_calc ...) требует, чтобы терминал стоял
в папке программы. Этот файл сам добавляет свою папку в путь поиска,
поэтому работает откуда угодно:

    python "C:\\куда\\распаковали\\run.py" example table

В PowerShell файл можно просто перетащить мышью в окно — путь
подставится сам.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# чтобы вывод с рамками и кириллицей не ломался при перенаправлении в файл
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import numpy  # noqa: F401
except ImportError:
    print("Не установлен numpy. Выполните:\n\n    "
          + Path(sys.executable).name + " -m pip install numpy\n")
    raise SystemExit(2)

from rza_calc.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
