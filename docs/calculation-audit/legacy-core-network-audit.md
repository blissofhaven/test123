# Аудит legacy `core.Network`

## Вердикт

`core.Network` не является канонической моделью проекта. Это рабочий mutable DTO
старого расчётного ядра, который создаётся заново адаптером. Он способен хранить
произвольный неориентированный граф, параллельные ветви и матрично считать
замкнутые сети. При этом он теряет часть семантики canonical domain: отдельные
электрические компоненты источников, полный набор последовательностей,
постоянные canonical ID и provenance результата.

## Что умеет `Network`

`Network` объявлен в `rza_calc/core/model.py:310-439`. Его topology helpers:

- активность ветви — `branch_conducting`
  (`rza_calc/core/model.py:441-453`);
- полный двусторонний adjacency — `adjacency`
  (`rza_calc/core/model.py:455-465`);
- достижимость от `GRID` — `energized_nodes`
  (`rza_calc/core/model.py:467-477`);
- фактическая ориентация и признак кольца — `orient`
  (`rza_calc/core/model.py:479-504`);
- зона за ветвью — `downstream_nodes`
  (`rza_calc/core/model.py:506-534`);
- параллельная группа — `parallel_group/group_zone`
  (`rza_calc/core/model.py:536-575`).

Это не чистое дерево. Параллельные и кольцевые ветви физически остаются в
adjacency.

## Численный решатель

`ShortCircuitSolver` строит узловую матрицу проводимостей для всех активных
ветвей, достижимых от `GRID` (`rza_calc/core/short_circuit.py:84-106`). Идеальные
перемычки объединяются union-find, остальные импедансы входят в Y-матрицу
(`rza_calc/core/short_circuit.py:108-150`), после чего матрица обращается
(`rza_calc/core/short_circuit.py:151-158`).

Следствие: обычные кольца и параллельные импедансные ветви поддерживаются
численно. Токораспределение по ветви вычисляется из Zbus
(`rza_calc/core/short_circuit.py:180-210`).

Подтверждённое ограничение: ток через идеальную ветвь нулевого сопротивления в
замкнутом контуре считается математически неоднозначным и вызывает
`CurrentDistributionError` (`rza_calc/core/short_circuit.py:190-205`). Это не
ошибка обнаружения кольца, а граница модели идеального аппарата. Для расчёта
тока по такому аппарату нужен ненулевой эквивалент либо отдельная модель
токораспределения.

## Где теряется семантика Domain Model

### 1. Все источники подключаются к одному `GRID`

Каждый `source` и `generator` адаптер превращает в ветвь `GRID -> node`
(`rza_calc/adapters/legacy_calculation.py:1685-1693`). В canonical
`TopologySnapshot` две независимые питаемые компоненты остаются двумя
компонентами и хранят реальные source equipment ID
(`rza_calc/topology/model.py:455-528`). В legacy graph обе достижимы через один
служебный `GRID`.

Для узловой матрицы общий reference сам по себе не доказывает ошибку тока КЗ:
между независимыми узлами нет off-diagonal проводимости. Но graph queries
`energized_nodes`, `orient`, поиск upstream/downstream и отчёт об островах уже
не могут восстановить исходное разделение и принадлежность к источнику.

### 2. Только прямая последовательность линии

Богатая проекция имеет `R1/X1`, `R2/X2`, `R0/X0`
(`rza_calc/calculation/projection.py:124-175`). Legacy adapter переносит
canonical `r1/x1` в исторически названные поля DTO `r0/x0`
(`rza_calc/adapters/legacy_calculation.py:779-797`). `r2/x2` и canonical `r0/x0`
до `core.Network` не доходят.

Поэтому текущий solver нельзя считать готовым расчётом прямой, обратной и
нулевой последовательностей, даже если контейнеры для них существуют в
`CalculationProjection`.

### 3. Два независимых преобразователя

`CalculationProjectionBuilder` (`rza_calc/calculation/projection.py:428-733`) и
`adapt_to_calculation` (`rza_calc/adapters/legacy_calculation.py:1415-1900`)
оба читают Domain Model, но первый не питает второй. Фактическая численная
цепочка проходит только через legacy adapter. Это удваивает правила отображения
трёхобмоточных трансформаторов, аппаратов, линий и состояний.

### 4. Trace не доходит до результата

Адаптер формирует двусторонние карты ID в `CalculationTrace`
(`rza_calc/adapters/legacy_calculation.py:102-129`) и возвращает их в
`AdaptationResult` (`rza_calc/adapters/legacy_calculation.py:131-134`).
`ProjectData` хранит `network` и diagnostics, но не trace
(`rza_calc/io/project.py:501-533`). `ProjectResult` тоже не имеет canonical
provenance (`rza_calc/core/engine.py:27-32`).

### 5. Нет токена свежести результата

`run` принимает лишь `Network` и `Methodology`
(`rza_calc/core/engine.py:46-65`). Результат не содержит identity/revision или
fingerprint входной сети. GUI поэтому не может формально отличить актуальный
результат от результата старого `Network`.

## Поведение кольцевых зон защит

При кольце `Network.orient` возвращает `ring=True`, а `downstream_nodes`
возвращает пустое множество (`rza_calc/core/model.py:498-516`). Это безопаснее,
чем ошибочно объявить один путь радиальным, но означает, что алгоритмы,
основанные на «зоне за защитой», неполны для meshed network. Их используют
`Context.fault_points`, `load_current`, upstream protection и ОЗЗ
(`rza_calc/core/context.py:72-145`, `rza_calc/core/protections/ozz.py:93`).

Численный ток КЗ в узле кольца может быть рассчитан, но построение радиальных
зон, назначение селективности и ток через идеальную перемычку требуют отдельной
проверенной методики.

## Практическая классификация

| Возможность | Статус legacy core |
|---|---|
| Произвольный граф ветвей | Есть |
| Параллельные импедансные ветви | Есть |
| Кольцевая Y-матрица | Есть |
| Реклоузер OPEN/CLOSED | Есть как `TieBranch` + mode state |
| Альтернативное питание | Электрическая проводимость есть; source provenance теряется |
| Независимые питаемые острова | Численно имеют общий reference; canonical component identity потеряна |
| Ток по идеальному аппарату в кольце | Не определён, явная ошибка |
| R2/X2 и R0/X0 | Не входят в текущий solver |
| Привязка результата к canonical ID | Не доведена до `ProjectResult` |
| Проверка свежести результата | Отсутствует |

## Рекомендуемая граница дальнейшей работы

Не расширять legacy DTO новыми параллельными догадками. Сначала нужен один
официальный compiler из canonical equipment + `TopologySnapshot` в единое
расчётное представление, затем адаптация существующих формул к нему. На переходе
необходимо сохранять model revision, topology fingerprint, registry fingerprint
и trace canonical ID вплоть до `ProjectResult`.

Граница аудита: выводы о структуре и потере данных подтверждены кодом и
in-memory тестами. Точность формул и нормативная применимость не оценивались.

