# Карта текущего расчётного ядра

## Что сегодня является чем

| Слой | Главные сущности | Источник истины | Реально используется расчётом |
|---|---|---|---|
| Объектная модель | `ElectricalModel`, equipment, ports, nodes, connections, states, logical lines, line sections | Да | Через адаптер |
| Активная топология | `TopologyEngine`, `TopologySnapshot` | Производное от Domain | Да, для валидации и напряжений перед адаптером |
| Будущая расчётная модель | `CalculationProjection` | Производное от Domain + topology | Нет, только контроль/сериализационные проверки ID |
| Compatibility adapter | `adapt_to_calculation`, `CalculationTrace` | Нет | Да, создаёт фактический вход ядра |
| Текущее численное ядро | mutable `core.Network`, `ShortCircuitSolver`, protection modules | Нет | Да |
| Результаты | `ProjectResult`, `ProtectionResult`, trace steps | Производное от `core.Network` | Да, показывается GUI |
| Графика | `DiagramDocument`, pages, representations, routes | Только внешний вид | Нет |

## Инвентаризация функций ТКЗ

| Функция | Точка входа | Алгоритм | Тесты | Источник формулы | Статус | Ограничения |
|---|---|---|---|---|---|---|
| Трёхфазное металлическое КЗ в электрическом узле | `ShortCircuitSolver.at()` | Положительно-последовательная Ybus, `I=U/(√3·|Zth|)` | обязательные примеры 1–6 | В профиле источник не задан | Реализовано частично; простые примеры проверены | Пример 6 не проходит; нет коэффициента напряжения и provenance |
| Двухфазное КЗ | `ShortCircuitSolver.at().i2` | `Iк2=0,866·Iк3` | strict xfail `AUD-SC-002` | Профиль-заглушка | Выдаёт неподтверждённое приближение | Нет сети обратной последовательности; коэффициент округлён |
| Однофазное КЗ | — | — | — | — | Отсутствует | Поле `z_loop` не подключено к solver-у |
| Двухфазное КЗ на землю | — | — | — | — | Отсутствует | Нет отрицательной и нулевой последовательностей |
| Сеть прямой последовательности | `branch_impedance()`, `ShortCircuitSolver` | Комплексные R1/X1 в legacy-полях `r0/x0` | аналитические и Ybus-тесты | Не задан | Реализовано частично | Названия legacy-полей вводят в заблуждение; voltage model не подтверждена |
| Сеть обратной последовательности | `CalculationProjection.negative_sequence` | Только хранение параметров в будущей проекции | projection tests | — | Не используется расчётом | Адаптер не передаёт R2/X2 в `core.Network` |
| Сеть нулевой последовательности | `CalculationProjection.zero_sequence` | Только хранение параметров в будущей проекции | projection tests | — | Не используется расчётом | Адаптер не передаёт R0/X0; режим нейтрали не участвует в ТКЗ |
| Ток в существующем узле | `ShortCircuitSolver.at(node_id)` | Диагональ Zbus | примеры 1, 3, 4, 5 | Не задан | Реализовано частично | Обесточенный узел даёт `KeyError`; результата со статусом/provenance нет |
| Точка КЗ внутри линии | `LineFaultLocation` | Только адресация по физическому расстоянию | `test_stage25_fault_locations.py` | — | Подготовлена, но расчёт отсутствует | Временное разбиение ветви и запуск solver-а не реализованы |
| Токи по ветвям | `distribution_factor()`, `Context.current_through()` | Передаточный коэффициент Zbus | пример 2, пример 6 | Не задан | Реализованы только модули | `abs(d)` уничтожает знак и фазу; идеальная перемычка в кольце неоднозначна |
| Напряжения узлов при КЗ | — | — | — | — | Отсутствуют | Zbus строится, но вектор напряжений и результат по узлам не формируются |
| Вклады отдельных источников | Косвенно через `distribution_factor()` ветви источника | Модуль доли общего тока | два источника в примере 5 | Не задан | Частично, отдельного результата нет | Нет комплексного вклада, угла, направления и ID источника в результате |
| Ударный ток | — | — | — | — | Отсутствует | Не вычисляется коэффициент κ/пиковая составляющая |
| Начальная периодическая составляющая | `ScResult.i3` | Статическая модель с `x″d` генератора | простые примеры | Не задан | Частично | Явный тип результата `Ik″` и коэффициент напряжения отсутствуют |
| Апериодическая составляющая | — | — | — | — | Отсутствует | Нет постоянной времени и временной зависимости |
| Ток в момент отключения | — | — | — | — | Отсутствует | Время выключателя хранится, но в ТКЗ не применяется |
| Интеграл Джоуля / термически эквивалентный ток | — | — | — | — | Отсутствует | Нет временной кривой тока |
| Отключающая способность | — | — | — | — | Отсутствует | Паспортные токи реклоузера хранятся, но не сравниваются |
| Термическая и динамическая стойкость | — | — | — | — | Отсутствует | Параметры оборудования не участвуют в проверках |
| Дуговое КЗ 0,4 кВ | — | — | — | — | Отсутствует | Рассчитывается только металлическое КЗ |
| Подпитка от двигателей | `Load.motor_share` (только хранение) | — | strict xfail `AUD-SC-006` | — | Отсутствует без предметного предупреждения | Полученный ток выглядит полным, хотя вклад двигателей исключён |
| Несколько источников | Общая Ybus, несколько ветвей `GRID→node` | Параллельные импедансы к общей ЭДС | пример 5 и topology audit | Не задан | Реализовано для одинаковой нормированной фазы/ЭДС | Разные ЭДС, углы и комплексные вклады источников не моделируются |

## 1. Domain kernel

Файл: `rza_calc/domain/electrical.py`.

```text
EquipmentInstance
    └─ port_ids -> PortInstance
                      └─ Connection -> ElectricalNode

LogicalLine
    └─ ordered section_equipment_ids -> LineSection equipment
                                          └─ construction_segments[]

OperatingState
    ├─ positions[equipment_id] = OPEN/CLOSED
    └─ availability[equipment_id] = IN/OUT_OF_SERVICE
```

Ключевые строки: equipment/port/node/connection
`rza_calc/domain/electrical.py:399-511`; режим
`rza_calc/domain/electrical.py:515-573`; линия и конструктивные сегменты
`rza_calc/domain/electrical.py:577-812`.

## 2. Topology kernel

Файлы:

- `rza_calc/topology/handlers.py:18-73` — явные behavior laws;
- `rza_calc/topology/handlers.py:76-128` — immutable registry + SHA-256;
- `rza_calc/topology/engine.py:251-322` — compiler и cache;
- `rza_calc/topology/model.py:636-1228` — immutable snapshot и запросы.

Topology kernel отвечает на вопросы:

- какие порты подключены к какому electrical node;
- какие equipment links активны в выбранном режиме;
- какие узлы входят в одну компоненту;
- какие реальные источники питают компоненту;
- есть ли кольцо;
- какой класс напряжения разрешён/конфликтует;
- существует ли путь между узлами.

Он не содержит формул ТКЗ и не читает координаты холста.

## 3. Calculation projection kernel

Файл: `rza_calc/calculation/projection.py`.

Проекция содержит typed calculation nodes/branches, внутренние узлы ЭДС,
земли и звезды, коэффициенты трансформации и sequence impedance
(`rza_calc/calculation/projection.py:55-243`). Compiler находится в
`rza_calc/calculation/projection.py:428-733`.

Текущий статус: архитектурно правильное производное представление, но не вход
`ShortCircuitSolver`. Его фактические использования внутри приложения находятся
в `rza_calc/io/project.py:303-328`, `1939` и `2119` и относятся к проверке
стабильных ID/сохранению.

## 4. Legacy adaptation kernel

Файл: `rza_calc/adapters/legacy_calculation.py`.

```text
ElectricalModel + optional TopologySnapshot
    -> validate integrity and handlers
    -> create legacy Nodes
    -> map equipment to legacy branches/loads/Transformer3W
    -> map every OperatingState to core.Mode
    -> return Network + CalculationTrace + diagnostics
```

Главный entry point: `rza_calc/adapters/legacy_calculation.py:1415-1900`.
Таблица behavior: `rza_calc/adapters/legacy_calculation.py:599-622`.
Safe deterministic legacy IDs: `rza_calc/adapters/legacy_calculation.py:711-729`.

## 5. Numerical/protection kernel

Файлы:

- DTO и graph helpers — `rza_calc/core/model.py:24-625`;
- impedance/electrical model — `rza_calc/core/impedance.py` и
  `rza_calc/core/electrical.py`;
- nodal short-circuit solver — `rza_calc/core/short_circuit.py:84-271`;
- multi-mode context — `rza_calc/core/context.py:25-145`;
- orchestration — `rza_calc/core/engine.py:46-79`;
- protections — `rza_calc/core/protections/`;
- selectivity — `rza_calc/core/selectivity.py`.

Текущий численный объект — именно `core.Network`, а не `ElectricalModel` и не
`CalculationProjection`.

## 6. Project/GUI integration kernel

`ProjectData` одновременно владеет canonical `electrical_model` и derived
`network` (`rza_calc/io/project.py:501-533`). Его lazy refresh расположен в
`rza_calc/io/project.py:535-577`.

`ProjectViewModel` хранит отдельную ссылку `self.net`
(`rza_calc/gui/view_model.py:143-154`) и вызывает на ней `run`
(`rza_calc/gui/view_model.py:311-325`). Это текущая точка высокого риска
устаревания.

## Карта идентификаторов и freshness

| Объект | ID/revision сейчас | Где проверяется | Проблема |
|---|---|---|---|
| `ElectricalModel` | process object identity + integer `revision` | topology/compiler/ProjectData | editor undo может вернуть старый revision |
| `TopologySnapshot` | model identity, revision, registry signature, topology fingerprint | `assert_compatible` | callers не всегда передают registry signature |
| `CalculationProjection` | model revision + deterministic IDs + semantic fingerprint | project-format checks | не используется solver-ом |
| `core.Network` | только IDs DTO; общего revision/fingerprint нет | нигде | невозможно доказать свежесть объекта |
| `CalculationTrace` | canonical <-> legacy maps | только immediate adapter result/tests | не хранится в ProjectData/ProjectResult |
| `ProjectResult` | нет input provenance | нигде | старый результат выглядит актуальным |
| `DiagramDocument` | собственная revision | editor history | корректно отделена от electric revision |

## Подтверждённые разрывы карты

- `AUD-TOP-001`: rollback revision создаёт cache alias в `TopologyEngine`.
- `AUD-TOP-002`: тот же alias не даёт `ProjectData` перестроить `Network`.
- `AUD-TOP-003`: даже свежий `project.network` не заменяет `ProjectViewModel.net`.
- `AUD-TOP-004`: projection/adapter compatibility не требует совпадения topology
  registry fingerprint.

Исполняемые карточки находятся в
`tests/calculation_audit/test_topology_and_freshness.py`.

## Граница карты

Карта отражает фактические зависимости Python-кода на дату аудита. Она не
утверждает нормативную корректность расчётов ТКЗ/РЗА и не описывает ещё не
реализованные формулы последовательностей. Production-код не изменён.
