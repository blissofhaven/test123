# Аудит топологии и её передачи в расчёт

Дата: 27 августа 2026 года. Статус: независимый аудит фактического кода, без
исправления production-модулей.

## Итог

Каноническая топология является полноценным графом и уже корректно выражает
узлы произвольной степени, отпайки, параллельные ветви, кольца, несколько
источников, острова и отключаемые реклоузеры. Графика электрическую связь не
создаёт.

Но контур **revision -> topology cache -> derived Network -> GUI result** не
гарантирует свежесть. Найдены два взаимосвязанных дефекта высокого уровня,
один критический дефект GUI-расчёта и один средний дефект проверки границы
registry.

## Что подтверждено положительно

| Область | Фактическая реализация | Подтверждение |
|---|---|---|
| Узел произвольной степени | Любое число `Connection` может указывать на один `ElectricalNode`; ограничение один-к-одному относится к порту, а не узлу | `rza_calc/domain/electrical.py:260-287`, `468-511`; `test_full_graph_keeps_parallel_links_ring_and_two_real_sources` |
| Полный граф | Union-find формирует компоненты без направления parent/child | `rza_calc/topology/engine.py:1190-1368` |
| Параллельные ветви | `links_between` и `neighbors` возвращают отдельные link ID | `rza_calc/topology/model.py:842-925`; аудиторский положительный тест |
| Кольца | Компонента хранит `cycle_rank`; feeder view сохраняет `non_tree_link_ids` | `rza_calc/topology/model.py:488-528`, `1047-1093` |
| Несколько источников | `TopologySource` хранит настоящий `EquipmentId`; источники компоненты не заменяются `GRID` | `rza_calc/topology/model.py:455-485`, `488-528` |
| Острова | Каждая активная компонента имеет собственный набор источников и состояние energization | `rza_calc/topology/model.py:488-528`, `rza_calc/topology/engine.py:1312-1365` |
| Отпайка | `split_line_section` создаёт реальный узел и две секции; `create_tap_line` присоединяет боковую линию к тому же узлу | `rza_calc/domain/electrical.py:2158-2320`, `2321-2368` |
| Удаление отпайки | Отдельная операция с контролируемым обратным объединением | `rza_calc/domain/electrical.py:3507-3665` |
| Реклоузер | Отдельный built-in type с двумя портами и switch capability; вставка/удаление — транзакции модели | `rza_calc/domain/electrical.py:956-994`, `2373-2572`, `2574-2738` |
| Состояние реклоузера | OPEN/CLOSED изменяет активный link, проекцию и режим legacy `Network` | `rza_calc/domain/electrical.py:3737-3759`; `test_recloser_state_reaches_projection_adapter_and_core_network` |
| Графическая изоляция | Перемещение representation меняет только `DiagramDocument` | `rza_calc/editor/controller.py:2777-2858`; `test_graphical_move_does_not_change_domain_or_topology` |
| Пересечение линий | Только маршруты без общего canonical node не соединяются | `tests/test_stage25_graphical_isolation.py:133-181`, `tests/test_stage4_requirements.py:309-371` |

## Матрица §16 — коммутационные аппараты

В таблице различены каноническая объектная модель, производная топология и
фактический вход legacy-ядра. «Отдельная ветвь» означает самостоятельный
`TopologyLink`/`CalculationBranch`, а не графический знак. Нулевое
сопротивление означает именно фактическое допущение текущего `TieBranch`, а не
подтверждённую паспортную модель аппарата.

| Аппарат | Реализация | Отдельная топологическая ветвь | Сопротивление в текущем расчёте | `CLOSED` | `OPEN` | Объект сохраняется | Передача в расчётный адаптер | Риск старого результата | Статус и доказательство |
|---|---|---|---|---|---|---|---|---|---|
| Выключатель | Канонический `builtin.circuit_breaker`, `behavior_key="switch"`, два порта | Да: один `TopologyLink` и одна ветвь проекции | Собственной проверенной модели Z нет; адаптер создаёт `TieBranch` с `Z=0` | Link/ветвь активны | Link/ветвь неактивны | Да; положение хранится отдельно в `OperatingState` | Да, как `TieBranch`; состояние попадает в `Mode.states` при свежем адаптировании | Есть: после команды редактора `ProjectViewModel` может продолжить считать старый `self.net` (`AUD-TOP-003`) | **Частично проверено**: общая switch-реализация подтверждена кодом и сценарием `test_stage4_complete_editing_roundtrip`; отдельного аналитического теста Z обычного canonical-выключателя нет |
| Разъединитель | Канонический `builtin.disconnector`, тот же `behavior_key="switch"`, отдельный символ | По общему handler — да | Как у выключателя: в legacy-входе `TieBranch`, `Z=0` | По общему handler активен | По общему handler неактивен | Да | Архитектурно да, тем же handler `switch` | Тот же риск `AUD-TOP-003` | **Частично проверено**: создание, два порта и соединение проверены (`test_switch_library_defaults_create_breaker_disconnector_and_recloser`, `test_controller_connects_ports_atomically_and_undo_restores_draft`), но отдельного теста OPEN/CLOSED → adapter → ТКЗ для разъединителя нет |
| Реклоузер | Канонический отдельный тип `builtin.recloser`, `behavior_key="recloser"`, два порта | Да, самостоятельный link/branch вида `RECLOSER` | Паспортные токи сохраняются, но собственное Z не рассчитывается; legacy-представление — нулевой `TieBranch` | Путь существует | Только этот путь разорван; сам аппарат не удаляется | Да; это проверено отдельным тестом | Да; OPEN/CLOSED проверены одновременно в projection и legacy `Network` | Есть тот же общий риск устаревшего `self.net`/результата | **Проверено для топологии и адаптера**: `test_recloser_state_reaches_projection_adapter_and_core_network`, `test_recloser_is_distinct_stateful_equipment_and_switching_never_deletes_it`; паспортное Z отсутствует |
| Секционный выключатель | Отдельного canonical type ID/роли «СВ» нет. В новой модели это может быть обычный `builtin.circuit_breaker` между секциями; в legacy-ядре есть специальный `TieBranch` | Да для generic breaker; legacy `TieBranch` также является отдельной ветвью | `TieBranch`: ровно `Z=0` (`rza_calc/core/model.py:213-217`) | Секции объединяются | Секции разделяются, но каждая может питаться от собственного источника | Да | **Compat-only** для специальной семантики `TieBranch`; canonical adapter превращает обычный breaker в тот же DTO | Есть общий риск `AUD-TOP-003` | **Частично/compat-only**: состояния и расчёт параллельных трансформаторов проверены legacy-тестами `test_bus_coupler_open_both_sections_fed_separately`, `test_section_is_fed_through_closed_bus_coupler`, `test_sc_parallel_transformers_via_tie`; отдельная canonical-сущность СВ отсутствует |
| Предохранитель | Электрический built-in type, topology handler и adapter handler отсутствуют; строка категории в старой структурной модели не создаёт электрического поведения | Нет реализации | Не определено | Не определено | Не определено | Неприменимо | Не передаётся как отдельный электрический объект | Проверять нечего до появления модели | **Не реализовано и не проверено** |

Итог по §16: выключатель, разъединитель и реклоузер используют корректную
графовую модель коммутационной ветви, но только реклоузер имеет отдельный
канонический тип и специальное сквозное тестовое покрытие состояния. Специальная
семантика секционного выключателя сейчас находится в legacy-слое, а
предохранителя в электрической модели нет.

## Матрица §17 — отпайки

Статус «проверено» ниже не переносится автоматически с Domain Model на ток КЗ:
структурный тест узлов и участков не является проверкой численного результата.

| № | Обязательный сценарий | Статус | Что фактически доказано | Ограничение/непроверенная часть |
|---:|---|---|---|---|
| 1 | Одна отпайка | **Проверено для модели** | Один реальный узел имеет предыдущий и следующий участки магистрали и боковую ветвь; `test_12_create_one_tap`, `test_repeated_split_supports_one_two_and_three_or_more_taps` | Отдельный контрольный расчёт КЗ на боковой ветви не выполнялся |
| 2 | Три отпайки | **Проверено для модели** | Три разных узла созданы; магистраль превращается в четыре электрические ветви; `test_13_create_three_taps`, `test_14_three_taps_split_main_line_into_four_branches` | Нет одного аналитического ТКЗ-примера с тремя настоящими боковыми ветвями |
| 3 | Отпайка от отпайки | **Проверено для модели** | Вложенная отпайка создаётся на `branch_section_id` первой отпайки и образует новый узел степени 3; `test_16_tap_can_be_created_from_an_existing_tap_line` | Ток КЗ во вложенной боковой ветви не проверен |
| 4 | КЗ до отпайки | **Не проверено как сценарий отпайки** | Пример 4 подтверждает отсечение последовательных участков после точки КЗ | В примере 4 нет боковой ветви и узла степени 3; он не доказывает КЗ до настоящей отпайки |
| 5 | КЗ на первой отпайке | **Не проверено** | Структурно первая боковая ветвь доходит до собственного конечного узла | Нет теста `ShortCircuitSolver.at(...)` для узла первой боковой ветви |
| 6 | КЗ на второй отпайке | **Не проверено** | Структурно несколько боковых ветвей сохраняются в графе | Нет теста ТКЗ на второй боковой ветви и сравнения состава сопротивлений путей |
| 7 | КЗ в конце основной линии | **Частично** | `test_example_4_production_three_serial_sections` проверяет сумму только предшествующих последовательных Z до `j3`; `test_15_main_line_continues_after_every_tap` доказывает непрерывность настоящей магистрали | Эти проверки находятся в разных моделях; КЗ в конце линии с реальными боковыми отпайками не рассчитано |
| 8 | Удаление отпайки | **Проверено для целостности модели** | Боковая линия удаляется, магистраль сохраняется; совместимые части могут атомарно объединяться; `test_17_remove_tap_deletes_side_branch_but_keeps_main_line`, `test_18_remove_tap_can_merge_compatible_main_sections` | Ток КЗ до/после удаления не сравнивался |
| 9 | Сохранение и загрузка | **Частично** | Native round-trip сохраняет ID, связи, маршруты и `semantic_signature`; `test_32_save_load_preserves_routes_ids_and_topology`, `test_stage4_complete_editing_roundtrip` | Нет сравнения численных токов КЗ до/после round-trip проекта с настоящими отпайками |
| 10 | Перемещение графической точки без изменения электрического расчёта | **Частично** | Электрическая revision и topology fingerprint/signature не меняются; `test_graphical_move_does_not_change_domain_or_topology`, `test_dragging_user_waypoint_changes_only_diagram_route_and_one_history_entry` | Численный `ProjectResult` до/после перемещения напрямую не сравнивался; общего механизма актуальности результата нет |

**Принципиальная граница:** реализованный в аудиторских тестах «пример 4» — это
три **последовательных** участка `source_bus → j1 → j2 → j3`. В нём нет ни
одной боковой линии, поэтому называть его расчётной проверкой «линии с тремя
отпайками» нельзя. В частности, КЗ на первой и второй боковых ветвях остаются
непроверенными.

## Матрица §18 — параллели, кольца и несколько источников

| Сценарий | Статус | Фактический результат | Доказательство | Граница проверки |
|---|---|---|---|---|
| Две одинаковые параллельные линии | **Проверено в расчётном ядре** | Получено `Zэкв=Z/2`, общий ток делится поровну; обе ветви учитываются Ybus | `test_example_2_production_parallel_branches` и независимый `test_example_2_reference_parallel_branches` | Нет одного теста, который сначала создаёт обе линии native-редактором, затем адаптирует и сравнивает ТКЗ |
| Две неравные параллельные линии | **Проверено в расчётном ядре** | Токи делятся по проводимостям (`2/3` и `1/3`), а не поровну и не последовательно | `test_unequal_parallel_branches_match_independent_ybus` | Та же сквозная native-граница |
| Два параллельных трансформатора | **Проверено в legacy-ядре, частично сквозно** | При замкнутом СВ в симметричном примере в расчёт входит `Zт/2`, ток на объединённых секциях одинаков и выше, чем при разомкнутом СВ | `tests/test_core.py::test_sc_parallel_transformers_via_tie` | Нет отдельного native Domain → adapter → solver эталона с двумя canonical-трансформаторами |
| Два источника | **Проверено** | Каноническая компонента сохраняет оба настоящих `EquipmentId`; solver складывает вклады двух путей, отключение одного оставляет вклад второго | `test_full_graph_keeps_parallel_links_ring_and_two_real_sources`, `test_example_5_two_sources_and_recloser` | В legacy adapter источники сводятся к служебному `GRID`, поэтому обратная трассировка вкладов требует `CalculationTrace` |
| Источник с каждой стороны линии/участка | **Частично** | Топология правильно сохраняет питание дальней части альтернативным источником при открытом реклоузере | `test_multiple_taps_after_open_recloser_use_alternative_source`, `test_stage4_complete_editing_roundtrip`, `test_island_with_local_generator_stays_energized` | Нет отдельного аналитического ТКЗ-теста одной физической линии, питаемой строго с двух концов, с проверкой направлений токов |
| Кольцевая/mesh-сеть | **Проверено по отдельным слоям** | Canonical graph сохраняет цикл; legacy solver решает полную Ybus и совпадает с независимым Zth и законом Кирхгофа | `test_full_graph_keeps_parallel_links_ring_and_two_real_sources`, `test_open_recloser_in_meshed_ring_keeps_supply_via_alternative_path`, `test_complex_mesh_matches_independent_ybus_and_kirchhoff` | Нет одного теста полного native round-trip кольца с последующим ТКЗ |
| Кольцо + два источника + реклоузер | **Частично сквозно** | При открытом реклоузере остаётся альтернативный путь; источники не превращают граф в дерево. Отдельный solver-пример подтверждает два источника через два реклоузера | `test_full_graph_keeps_parallel_links_ring_and_two_real_sources`, `test_example_5_two_sources_and_recloser` | Численный тест объединяет два источника и реклоузеры, но не кольцо; canonical-тест объединяет все три признака только на уровне топологии |
| Секционный выключатель замкнут | **Проверено в legacy-ядре** | Секции образуют одну компоненту; секция без собственного ввода питается через СВ; два трансформатора работают параллельно | `test_section_is_fed_through_closed_bus_coupler`, `test_two_independent_sources_form_two_components` (состояние `SV=True`), `test_sc_parallel_transformers_via_tie` | Специального canonical type «секционный выключатель» нет |
| Секционный выключатель разомкнут | **Проверено в legacy-ядре** | Две питаемые секции остаются разными компонентами, ток через СВ равен нулю; при расчёте КЗ работает только трансформатор своей секции | `test_bus_coupler_open_both_sections_fed_separately`, `test_two_independent_sources_form_two_components`, `test_sc_single_transformer` | Специального canonical type «секционный выключатель» нет |

Таким образом, ядро не ограничено поиском первого пути: параллели и сложная
mesh-сеть решаются матрично. Главный оставшийся пробел этой группы — не
математика Ybus сама по себе, а отсутствие единых сквозных native-тестов для
некоторых комбинаций Domain → topology → adapter → solver.

## Матрица §19 — электрические острова

| Обязательный сценарий | Статус | Что фактически происходит | Доказательство | Ограничение |
|---|---|---|---|---|
| Участок без источника | **Проверено по отдельным слоям** | Canonical topology сохраняет компоненту, возвращает пустой список источников, `is_energized=False` и предупреждение `component_without_source`; solver не выдаёт `0 кА`, а отклоняет запрос как «не запитан» | `test_source_less_island_is_preserved_and_reported_as_warning`, `tests/calculation_audit/test_input_integrity.py::test_unenergized_island_is_not_reported_as_zero_current` | Топологический и solver-тесты построены на разных входных моделях |
| Участок отключён аппаратом | **Проверено** | При OPEN link остаётся в snapshot как неактивный, питающая и отключённая части становятся разными компонентами; незапитанная сторона получает пустой набор источников | `test_radial_switch_closed_and_open_change_only_active_connectivity`, `test_recloser_uses_the_same_graph_rules_as_a_real_switch`, `test_topology_diagnostics_use_only_selected_operating_state_and_name_it` | Численный ток КЗ этого exact canonical-сценария отдельно не сверялся |
| Изолированная нагрузка | **Не проверено как отдельный сценарий** | Общая модель компоненты без источника должна примениться и к узлу нагрузки | Не найден отдельный тест с canonical `builtin.load`/legacy `Load` на изолированном узле | Нельзя подменять проверку произвольного пустого узла проверкой реальной нагрузки |
| Два независимых острова | **Частично** | Две части имеют разные `connected_component_id` и собственные наборы источников; замыкание СВ объединяет их | `test_two_independent_sources_form_two_components`, `test_open_switch_can_have_independently_energized_sources_on_both_sides` | Отдельные численные токи КЗ для обеих компонент в одном тесте не сравнивались |
| Остров с генератором | **Проверено для энергизации, частично для ТКЗ** | Локальный генератор считается настоящим источником своей компоненты; отделённый участок остаётся под напряжением | `test_local_generator_energizes_its_island_without_an_external_grid`, `test_island_with_local_generator_stays_energized` | Нет отдельного аналитического сравнения тока КЗ именно этого острова с ручным эталоном |
| Остров без генератора | **Проверено** | Компонента сохраняется, помечается незапитанной, а обращение за током КЗ завершается диагностической ошибкой вместо обычного числа `0 кА` | `test_source_less_island_is_preserved_and_reported_as_warning`, `test_unenergized_island_is_not_reported_as_zero_current` | Фактический текст solver-а — «узел не запитан», а не дословная требуемая фраза «Расчёт невозможен: точка КЗ не связана с активным источником» |
| Восстановление связи после включения аппарата | **Проверено** | CLOSED снова объединяет компоненты и восстанавливает источники/путь; в расчётном примере включение одного реклоузера восстанавливает конечный ток КЗ | `test_radial_switch_closed_and_open_change_only_active_connectivity`, `test_open_switch_can_have_independently_energized_sources_on_both_sides`, `test_example_5_two_sources_and_recloser` | Общий дефект свежести `AUD-TOP-003` остаётся для переключения через canonical editor и старый GUI `self.net` |

Итог по §19: базовая семантика островов корректна, и наиболее опасного
тихого результата `0 кА` для незапитанной точки не обнаружено. Пробелы —
отдельный тест реальной изолированной нагрузки, численный эталон КЗ острова с
локальным генератором и унификация пользовательского текста ошибки.

## Подтверждённые дефекты

### AUD-TOP-001 — коллизия кэша топологии после undo и новой ветви истории

Критичность: высокая.

`ProjectCommandHistory.undo/redo` передаёт `restore_revisions=True`
(`rza_calc/editor/history.py:401-426`). `_apply` сначала выполняет commit, затем
прямо возвращает `_revision` к номеру из memento
(`rza_calc/editor/history.py:300-315`). Это противоречит контракту другой
реализации истории, где revision «никогда не уменьшается»
(`rza_calc/domain/history.py:2-7`).

`TopologyEngine` кэширует по `(revision, state signature, registry fingerprint)`
внутри того же объекта модели (`rza_calc/topology/engine.py:270-305`). Содержимое
модели в ключ не входит. Последовательность:

1. подключить порт к узлу B и получить revision N;
2. скомпилировать snapshot;
3. undo возвращает revision N-1;
4. подключить тот же порт к узлу C и снова получить revision N;
5. `compile` возвращает snapshot ветви B из кэша.

Исполняемое доказательство: strict xfail
`tests/calculation_audit/test_topology_and_freshness.py::test_aud_top_001_topology_cache_must_not_alias_divergent_history`.

### AUD-TOP-002 — `ProjectData` не перестраивает `Network` при той же revision

Критичность: высокая.

`ProjectData.__getattribute__` и `refresh_calculation_view` сравнивают только
`electrical_model.revision` с `_calculation_view_revision`
(`rza_calc/io/project.py:535-577`). При сценарии AUD-TOP-001 номер совпадает,
поэтому derived `Network` остаётся от другой ветви истории.

Исполняемое доказательство: strict xfail
`tests/calculation_audit/test_topology_and_freshness.py::test_aud_top_002_project_data_must_refresh_same_revision_new_content`.

### AUD-TOP-003 — GUI пересчитывает сохранённый старый `self.net`

Критичность: критическая для пользовательского результата.

`ProjectViewModel.__init__` один раз выполняет `self.net = project.network`
(`rza_calc/gui/view_model.py:143-154`). Перед пересчётом проверка blockers может
перестроить `project.network`, но сам вызов остаётся `run(self.net, ...)`
(`rza_calc/gui/view_model.py:311-325`). В результате расчёт успешно завершается
на старом состоянии аппаратов/старой связности.

Основное окно создаёт `ProjectEditorController(vm.project)` и старую расчётную
вкладку рядом (`rza_calc/gui/main_window.py:1136-1146`), но сигнал завершения
команды редактора связан лишь с обновлением самого editor workspace
(`rza_calc/gui/editor_panels.py:657-668`). В `MainWindow` нет синхронизации
`vm.net` на электрическое изменение (`rza_calc/gui/main_window.py:1148-1164`).

Исполняемое доказательство: strict xfail
`tests/calculation_audit/test_topology_and_freshness.py::test_aud_top_003_view_model_result_must_use_current_project_network`.

### AUD-TOP-004 — не проверяется fingerprint реестра поведения

Критичность: средняя; повышается при подключении расширяемых handlers.

`TopologySnapshot.assert_compatible` умеет проверять `registry_signature`, но
только если вызывающая сторона передаст его
(`rza_calc/topology/model.py:1189-1207`). Ни
`CalculationProjectionBuilder.build` (`rza_calc/calculation/projection.py:553-557`),
ни `adapt_to_calculation` (`rza_calc/adapters/legacy_calculation.py:1426-1433`)
его не передают.

Воспроизведён snapshot пользовательского registry, где `line` объявлена
`PASSIVE`: snapshot содержит 0 links, но стандартный projection builder принимает
его и создаёт 1 активную расчётную ветвь.

Исполняемое доказательство: strict xfail
`tests/calculation_audit/test_topology_and_freshness.py::test_aud_top_004_projection_must_reject_foreign_topology_registry`.

## Матрица §21 — актуальность результата

У `ProjectResult` нет ни revision электрической модели, ни topology/model
fingerprint, ни идентификатора режима, ни отпечатка методики
(`rza_calc/core/engine.py:27-32`). Поэтому программа сейчас не умеет доказать,
что уже показанный результат соответствует текущим исходным данным. Обычное
увеличение `ElectricalModel.revision` помогает перестроить производный
`ProjectData.network`, но не маркирует старый `ProjectResult`, а дефекты
`AUD-TOP-001`...`AUD-TOP-003` показывают, что даже перестроение не гарантировано.

| Изменение | Проверка | Что меняется сейчас | Вывод об актуальности предыдущего результата |
|---|---|---|---|
| Электрическое соединение | **Проверено негативно**: `AUD-TOP-001`, `AUD-TOP-002`, `AUD-TOP-003` | Обычная команда увеличивает electrical revision; после undo и новой ветви истории возможна коллизия того же номера | **Требование не выполнено:** старый snapshot, `Network` и GUI-результат могут остаться внешне актуальными |
| Состояние аппарата | **Частично проверено**: `test_recloser_state_reaches_projection_adapter_and_core_network` подтверждает свежий путь; `AUD-TOP-003` — старый GUI-путь | Canonical `set_switch_position` меняет режим и revision; legacy GUI также умеет менять свой `Mode` и сразу пересчитывать | **Требование в целом не выполнено:** единого provenance/флага stale нет, а два пути изменения состояния не синхронизированы |
| Длина линии | **Проверено по коду, не проверено сквозным тестом stale** | `update_line_construction_segment`/замена сегментов увеличивают electrical revision; свежий adapter перечитывает длину | Старый `ProjectResult` не помечается; `ProjectViewModel.self.net` может остаться прежним |
| Марка провода или кабеля | **Проверено по коду, не проверено сквозным тестом stale** | `set_section_override` либо замена конструктивного сегмента увеличивают revision; свежий adapter формирует новый `LineBranch` | Автоматической инвалидизации уже показанного результата нет |
| Параметр трансформатора | **Проверено по коду, не проверено сквозным тестом stale** | `update_equipment_properties` увеличивает electrical revision; при свежем адаптировании DTO меняется | Старый результат не связан с revision/параметрами и может быть показан без признака устаревания |
| Параметр источника | **Проверено по коду, не проверено сквозным тестом stale** | Тот же общий путь изменения equipment properties и перестроения adapter | Автоматической инвалидизации результата нет |
| Режим сети | **Частично проверено** | Canonical operating state входит в topology/adapter; выбранный legacy `mode_id` и пользовательский `gui_custom` живут в `ProjectViewModel` | Результат не хранит ID/сигнатуру режима; `select_mode` сам по себе только меняет выбор, поэтому доказательства актуальности нет |
| Расчётное напряжение | **Проверено по коду, сквозной stale-тест отсутствует** | Значения `Methodology.u_avg` находятся вне `ElectricalModel.revision` | **Требование не выполнено:** изменение методики не сопоставляется с уже созданным `ProjectResult` |
| Настройка расчёта | **Проверено по архитектуре, отдельные мутации не покрыты** | Методика передаётся в `run(net, meth)`, но её версия/hash в результате не сохраняются | Предыдущий результат невозможно автоматически признать устаревшим |
| Справочная запись | **Частично проверено** | Изменение общего каталога намеренно не меняет сохранённый снимок проекта. Замена project snapshot/history отмечается как `catalog_changed`, но не увеличивает electrical revision | Для общего каталога отсутствие изменения старого проекта правильно; для реально заменённого снимка/привязки расчётной invalidation нет |
| Пользовательское переопределение | **Частично проверено** | Domain overrides (`set_section_override`, equipment properties) увеличивают revision; `CatalogBinding.parameter_overrides` хранится отдельно | Для Domain-пути действует лишь неполный revision-механизм; для отдельной catalog-binding правки единого расчётного fingerprint нет |
| Только цвет | **Не проверено отдельной командой/тестом** | Электрического поля цвета в canonical model нет; оформление относится к Diagram/UI | По разделению моделей результат меняться не должен, но работающего механизма статуса результата всё равно нет |
| Только символ | **Частично проверено архитектурно** | `GraphicalRepresentation.symbol_key` относится к `DiagramDocument`, не к электрической модели | Не должен инвалидировать ТКЗ; отдельного теста «результат до/после» нет |
| Только положение надписи | **Проверено структурно**: `test_node_busbar_resize_rotate_and_label_are_graphics_only` | Меняются representation/graphics и diagram revision, electrical revision остаётся прежней | Электрический вход не меняется; численный результат до/после отдельно не сравнивался |
| Только маршрут графической линии | **Проверено структурно**: `test_graphical_move_does_not_change_domain_or_topology`, `test_dragging_user_waypoint_changes_only_diagram_route_and_one_history_entry` | Меняются waypoints/diagram revision; connectivity и topology fingerprint сохраняются | Электрический результат инвалидироваться не должен; прямого сравнения `ProjectResult` нет |
| Только масштаб | **Проверено структурно**: `test_workspace_grid_snap_view_and_mode_are_serializable_diagram_data`, GUI-тест zoom | Масштаб хранится в workspace/diagram state и не меняет electrical revision | Электрический результат инвалидироваться не должен; отдельный stale-индикатор отсутствует |

Итог по §21: разделение электрических и чисто графических изменений в
основном выполнено, но **механизма актуальности результата как такового нет**.
Положительные тесты свежей проекции не компенсируют отсутствие provenance в
`ProjectResult` и подтверждённый расчёт старого `ProjectViewModel.self.net`.

## Остальные архитектурные риски

1. `CalculationProjection` и legacy adapter независимо преобразуют одно и то же
   оборудование. Первый хранит все sequence-параметры, второй является входом
   реального solver-а. Расхождение может не обнаруживаться тестами одного слоя.
2. `CalculationTrace` создаётся, но не сохраняется в `ProjectData`; результат
   нельзя надёжно вернуть к canonical ID без повторного адаптирования.
3. `ProjectResult` не содержит revision/fingerprint исходной модели
   (`rza_calc/core/engine.py:27-32`). Даже после исправления `vm.net` нужен явный
   provenance и invalidation результата.
4. `electrical_model_identity` основан на `id(model)`
   (`rza_calc/topology/engine.py:87-92`), то есть пригоден только внутри процесса.
   Это не постоянный ID проекта.
5. Одна `LogicalLine` валидируется как упорядоченная цепочка и не может сама
   замыкаться в цикл (`rza_calc/domain/electrical.py:1758-1859`). Общий граф
   кольца поддерживает, но кольцо должно состоять из нескольких логических
   линий/аппаратов. Это ограничение модели маршрута, а не дерева топологии.

## Выполненные тесты

Существующий целевой набор:

```text
84 passed in 31.11s
```

Он включал topology connectivity/state/cache/voltage, recloser topology,
graphical isolation, calculation projection и Stage 4 integration/requirements.

Новый аудиторский набор:

```text
3 passed, 4 xfailed in 0.26s
```

Команда:

```text
python -B -m pytest -q -p no:cacheprovider \
  tests/calculation_audit/test_topology_and_freshness.py -rxX
```

Все `xfail` строгие и имеют постоянные ID `AUD-TOP-001` ... `AUD-TOP-004`.

## Граница заключения

Это доказательство архитектуры связности и передачи состояния, но не
валидация формул ТКЗ. Не выполнялись сравнение с EnergyCS, проверка по
эталонным токам КЗ, нагрузочное тестирование GUI и интерактивная визуальная
приёмка. Никакой production-файл в рамках аудита не изменён.
