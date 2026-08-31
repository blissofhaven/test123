# Проверка safe-hardening

Среда независимой доработки:

- Linux 6.18;
- Python 3.13.5;
- NumPy 2.3.5;
- pytest 9.0.2;
- PySide6 в контейнере отсутствует, установить его без сетевого доступа не
  удалось.

## Пройдено

- `tests/test_core.py` + `tests/calculation_audit` +
  `tests/test_safe_hardening.py`: **114 passed**;
- все четыре файла аудита 4.4: **74 passed**, прежних strict-xfail после
  исправлений нет;
- базовые/domain/project/stage1: **135 passed**;
- stage2/stage2.5: **79 passed**;
- stage3/stage4 без Qt: **100 passed**;
- `test_gui_view_model.py`: **5 passed**;
- требования этапа 3 без двух Qt/offscreen-сценариев: **15 passed**.

Итого независимо пройдено: **448 не-GUI тестов**.

## Не выполнено в этой среде

44 теста требуют PySide6:

- `test_stage3_editor_gui.py`: 7;
- Qt-часть `test_stage3_requirements.py`: 2;
- `test_stage4_editor_interaction.py`: 23;
- `test_stage4_saved_route_editing.py`: 12.

`python tests/run_tests.py` также не стартует без PySide6, поскольку импортирует
GUI-набор. Это ограничение среды проверки, а не подтверждение прохождения или
падения GUI-функций. Их необходимо запустить на рабочем Windows-ПК после
`python -m pip install -r requirements.txt`.

## Benchmark после исправлений

Диагностический `benchmark_kernel.py --runs 3`:

| Узлов | Медиана solver, с | Пик tracemalloc, МиБ |
|---:|---:|---:|
| 10 | 0,002433 | 0,060 |
| 100 | 0,027888 | 0,850 |
| 1000 | 0,652421 | 50,368 |

Эти значения нельзя напрямую сравнивать с аудитом Windows/Python 3.14/NumPy
2.5.2: отличается ОС, Python, NumPy и BLAS. Архитектурное ограничение не
изменилось — плотная матрица требует O(N²) памяти, а полное обращение O(N³)
времени. 5000 узлов не запускались.
