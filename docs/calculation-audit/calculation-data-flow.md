# Фактический путь расчётных данных

Дата независимого аудита: 27 августа 2026 года.

## Краткий вывод

Источником электрической связности действительно является `ElectricalModel`, а
не холст. Однако до численного ядра существуют **два параллельных производных
представления**:

```text
ElectricalModel
    ├─> TopologyEngine ─> TopologySnapshot
    │                         └─> CalculationProjectionBuilder
    │                                  └─> CalculationProjection
    │                                      (контроль ID и будущая модель)
    │
    └─> adapt_to_calculation(TopologySnapshot)
              └─> legacy core.Network
                        └─> core.engine.run
                                  └─> ProjectResult
                                            └─> ProjectViewModel / GUI

DiagramDocument ──────── только графика и ссылки на canonical ID
```

Важная граница: `CalculationProjection` сейчас **не является входом численного
расчёта**. В `rza_calc/io/project.py:303-328` она строится для проверки
детерминированных ID при загрузке/сохранении, тогда как реальный расчёт получает
`core.Network` через `adapt_to_calculation` (`rza_calc/io/project.py:157-183`,
`rza_calc/gui/view_model.py:311-325`).

## 1. Каноническая объектная модель

Файл `rza_calc/domain/electrical.py` прямо объявляет модель канонической и
отделённой от SVG/формул (`rza_calc/domain/electrical.py:1-7`). Основные записи:

- оборудование — `EquipmentInstance` (`rza_calc/domain/electrical.py:399-452`);
- порт — `PortInstance` (`rza_calc/domain/electrical.py:456-464`);
- электрический узел — `ElectricalNode`
  (`rza_calc/domain/electrical.py:468-490`);
- подключение одного порта к узлу — `Connection`
  (`rza_calc/domain/electrical.py:494-511`);
- разреженный режим — `OperatingState`
  (`rza_calc/domain/electrical.py:515-573`);
- логическая линия — `LogicalLine`
  (`rza_calc/domain/electrical.py:577-634`);
- конструктивный участок — `LineConstructionSegment`
  (`rza_calc/domain/electrical.py:638-723`);
- физическая ветвь линии — `LineSection`
  (`rza_calc/domain/electrical.py:727-812`).

`ElectricalModel` владеет всеми хранилищами (`rza_calc/domain/electrical.py:1070-1214`).
Каждая штатная мутация вызывает `_changed` и увеличивает `revision`
(`rza_calc/domain/electrical.py:1205-1214`). Связность образуется только через
`connect_port`, `connect_ports` и `reconnect_port`
(`rza_calc/domain/electrical.py:3104-3245`).

## 2. Активная топология

`TopologyEngine.compile` снимает из модели detached view, разрешает выбранный
режим и создаёт неизменяемый `TopologySnapshot`
(`rza_calc/topology/engine.py:286-322`). Ключ кэша состоит из:

```text
(ElectricalModel.revision, state_signature, registry.fingerprint)
```

Это видно в `rza_calc/topology/engine.py:293-305`. Сам снимок хранит ID модели,
ревизию, fingerprint реестра поведения, порты, узлы, активные связи, источники,
компоненты и зоны напряжения (`rza_calc/topology/model.py:636-744`).

Полный граф строится по активным терминалам оборудования, без `parent -> child`.
Путь ищется обычным обходом графа (`rza_calc/topology/model.py:998-1045`).
Даже производное представление фидера возвращает отдельно древесные и
недревесные связи (`rza_calc/topology/model.py:1047-1093`), поэтому кольцо не
теряется при построении вида фидера.

## 3. Каноническая расчётная проекция

`CalculationProjectionBuilder` — односторонний compiler
(`rza_calc/calculation/projection.py:428-437`). В проекции есть:

- обычные и внутренние расчётные узлы, включая звезду трансформатора, внутреннюю
  ЭДС и землю (`rza_calc/calculation/projection.py:55-59`);
- расчётные ветви (`rza_calc/calculation/projection.py:199-243`);
- типизированные `R1/X1`, `R2/X2`, `R0/X0`
  (`rza_calc/calculation/projection.py:124-175`);
- три ветви и внутренний узел для трёхобмоточного трансформатора
  (`rza_calc/calculation/projection.py:653-690`).

Проекция неизменяема и производна от объектной модели, но численное ядро её не
потребляет. Это создаёт риск расхождения двух преобразователей: projection
registry и legacy adapter.

## 4. Адаптер в фактическое численное ядро

`adapt_to_calculation` создаёт новый одноразовый `core.Network`
(`rza_calc/adapters/legacy_calculation.py:1415-1423`). Прямое отображение типов
находится в `rza_calc/adapters/legacy_calculation.py:599-622`:

| Domain behavior | Legacy DTO |
|---|---|
| `source` | `SourceBranch` от `GRID` к узлу |
| `generator` | `GeneratorBranch` от `GRID` к узлу |
| `line` / `line_section` | `LineBranch` |
| `switch` / `recloser` | `TieBranch` |
| `transformer_2w` | `TransformerBranch` |
| `transformer_3w` | `Transformer3W` и служебные лучи |
| `load` | `Load` |

Каждый источник сводится к общему служебному `GRID`
(`rza_calc/adapters/legacy_calculation.py:1685-1693`). Для линии legacy DTO
получает только подтверждённую прямую последовательность: canonical
`r1_ohm_per_km/x1_ohm_per_km` записываются в исторические поля `r0/x0`
(`rza_calc/adapters/legacy_calculation.py:779-797`). Это осознанная
совместимость имён, но обратная и нулевая последовательности до численного
решателя не доходят.

Адаптер возвращает `CalculationTrace` со связями canonical ID -> legacy ID
(`rza_calc/adapters/legacy_calculation.py:102-133`,
`rza_calc/adapters/legacy_calculation.py:1883-1891`). В `ProjectData` поля для
этого trace нет (`rza_calc/io/project.py:501-533`), поэтому сквозная привязка
результата к canonical ID после адаптации теряется.

## 5. Численный расчёт и результат

Фактический вход `run` — mutable `core.Network`
(`rza_calc/core/engine.py:46-65`). `ShortCircuitSolver` выбирает активные ветви,
оставляет узлы, достижимые от `GRID`, объединяет идеальные перемычки и строит
матрицу проводимостей (`rza_calc/core/short_circuit.py:102-158`).

`ProjectResult` хранит только `Context`, результаты РЗА, пары селективности,
шаги и предупреждения (`rza_calc/core/engine.py:27-32`). В нём отсутствуют:

- `ElectricalModel` identity/revision;
- fingerprint `TopologySnapshot`;
- версия/identity `core.Network`;
- `CalculationTrace`;
- признак устаревания результата.

Следовательно, результат сам не способен доказать, к какой ревизии схемы он
относится.

## 6. Владение derived view и GUI

`ProjectData` хранит и canonical model, и derived `Network`. Автообновление
срабатывает только при неравенстве номера ревизии
(`rza_calc/io/project.py:535-577`). Это недостаточно при повторном использовании
номера ревизии после undo.

`ProjectViewModel` один раз копирует ссылку `project.network` в `self.net`
(`rza_calc/gui/view_model.py:143-154`) и затем вызывает `run(self.net, ...)`
(`rza_calc/gui/view_model.py:311-325`). Редактор и расчётная вкладка объединены в
одном окне (`rza_calc/gui/main_window.py:1136-1146`), но обработчики изменений
редактора не заменяют `vm.net` (`rza_calc/gui/main_window.py:1148-1164`).

Это подтверждённый сквозной разрыв свежести: `project.network` уже может быть
перестроен, а ручной пересчёт в GUI продолжает использовать старый объект.

## 7. Обратные зависимости и потери данных

| Граница | Фактическое состояние | Риск |
|---|---|---|
| Domain -> topology | Односторонняя, ID сохраняются | Низкий при монотонной revision |
| Domain -> CalculationProjection | Односторонняя, богатая sequence-модель | Не используется solver-ом |
| Domain -> legacy adapter | Второй независимый mapping | Дрейф относительно projection |
| topology snapshot -> projection/adapter | Сверяются identity/revision, не registry fingerprint | Возможна несовместимая семантика |
| adapter -> ProjectData | `Network` сохраняется, `CalculationTrace` отбрасывается | Нет end-to-end provenance |
| ProjectData -> GUI | GUI кэширует ссылку на `Network` | Возможен расчёт старой схемы |
| Diagram -> Domain | Только явные команды подключения меняют Domain | Корректно: геометрия не проводит ток |

## 8. Матрица переходов данных

Матрица перечисляет основной production-путь и отдельно отмечает богатую, но
неиспользуемую solver-ом проекцию. «Потери» означают семантику, которая была во
входе, но отсутствует или неразличима на следующем слое.

| Переход | Файл, класс или функция | Вход | Выход | Единицы | Преобразование | Потери / неоднозначность | Округление | Направление | Опорное напряжение |
|---|---|---|---|---|---|---|---|---|---|
| JSON проекта → объект проекта | `io/project.py:load_project` | versioned JSON: electrical, diagram, methodology, режимы | `ProjectData` с `ElectricalModel`, `DiagramDocument`, `Methodology` и derived `Network` | Единицы соответствуют полям формата; автоматического общего SI-слоя нет | Миграции старых версий, восстановление typed ID, построение derived view | Legacy-проекты сохраняют данные внутри compatibility payload; происхождение единиц зависит от старой схемы | Числового округления не найдено | Порядок `from/to` восстанавливается из портов или legacy payload | Классы Domain хранят В; compatibility-поля могут уже хранить кВ |
| Холст → электрическая модель | команды `editor/controller.py`; `Connection` | Представления, выбранные порты и явная команда подключения | Порт → `ElectricalNodeId` | Графические `ед.`/°, электрических единиц нет | Только явная электрическая команда меняет connection | Пиксельная длина и повороты намеренно не передаются | Координаты могут привязываться к сетке; электрические числа не округляются | Направление линии задаётся ролями портов, не геометрией | Не вычисляется; совместимость классов проверяется по ID |
| `ElectricalModel` + режим → активная топология | `TopologyEngine.compile` | Оборудование, порты, connections, normal position, sparse mode | `TopologySnapshot`: активные links, components, sources, voltage zones | Числовые электрические параметры почти не участвуют; напряжение остаётся `VoltageClassId` | Разрешение состояния, доступности, проводимости, компонент и распространение класса напряжения | R/X, паспортные мощности и длины в snapshot не копируются; это топологический, не расчётный снимок | Нет | Сохраняются роли портов и пары узлов; обходы могут выбирать детерминированный путь | `VoltageResolution` хранит ID класса и evidence, но не отдельное U до КЗ |
| Domain + topology → богатая расчётная проекция | `CalculationProjectionBuilder.build` | Canonical equipment/properties, `TopologySnapshot` | Неизменяемые `CalculationNode`/`CalculationBranch`, R1/X1/R2/X2/R0/X0, G/B, внутренние узлы | Ом, См, мм, безразмерный ratio по именам полей | Наследование свойств разрешается в Domain; 3W → три луча; source/generator → внутренний EMF node | Формулы отсутствуют; численные параметры не агрегируются для solver; проекция не является входом `core.engine.run` | Нет | `from_node_id/to_node_id` и роли сохраняются | Узлы хранят `VoltageClassId`; трансформатор получает nominal ratio по номинальным В |
| Domain + topology → legacy DTO | `adapters/legacy_calculation.py:adapt_to_calculation` | Equipment, nodes, connections, line construction, активный snapshot | Mutable `core.Network` + временный `CalculationTrace` | В→кВ; мм→км; canonical Ом/км и А/км остаются погонными | Все источники → общий `GRID`; R1/X1 → исторические `r0/x0`; несколько подтверждённых сегментов → последовательный эквивалент; switch/recloser → `TieBranch` | R2/X2/R0/X0 и G/B не доходят до solver; большинство паспортных свойств реклоузера не участвуют; `CalculationTrace` затем не хранится в `ProjectData` | Для длины только точное масштабирование; общего округления нет | Портовые роли становятся `node_from/node_to`; реклоузер теряет самостоятельную электрическую модель сопротивления | Узел получает `nominal_voltage_v/1000`; propagated class берётся из snapshot при отсутствии declared class |
| `core.Network` + `Methodology` → сопротивления ветвей | `core/impedance.py:branch_impedance` | Source/generator/transformer/line DTO, `u_base`, режим max/min | Комплексное Z каждой активной ветви | Вход смешанный: кВ, МВА, кВА, кВт, %, о.е., Ом/км, км; выход Ом | Параметры элемента → R+jX; `Z′=Z·(Uср.б/Uср.эл)²` | Неявные роли напряжений; fallback справочника; нейтраль, группа соединения, R2/R0 и фактический tap не участвуют | Нет; `trace.fmt` влияет только на текст | Z симметрично; знак отрицательного луча 3W сохраняется | `Methodology.u_avg(stage)` и `u_avg(u_base)`, включая поиск ближайшей ступени |
| Состояние сети → набор расчётных ветвей | `Network.active_branches`, `Network.energized_nodes`, `ShortCircuitSolver._build` | `core.Network`, `Mode` | Только активные и запитанные ветви/узлы | Без дополнительной конверсии | OPEN/out-of-service исключаются; узлы за нулевым Z объединяются | Коммутационный аппарат не имеет собственного ненулевого Z; остров без источника исключается, а не получает I=0 | Порог объединения `|Z|<1e-12 Ом` | Ориентация остаётся в объектах ветвей | Все выбранные Z уже приводятся к одной `u_base` |
| Ветви → Ybus | `ShortCircuitSolver._build` | Комплексные Z, пары узлов | Плотная комплексная матрица Y | `1/Ом = См` | Стандартный узловой stamp `+y` на диагонали, `−y` во взаимных элементах | Разные ЭДС/углы источников не представлены: источники сходятся к общему `GRID` | Нет | Знак матричного штампа учитывает порядок концов, физическое направление тока ещё не выбирается | Единая базисная ступень `u_base` |
| Ybus → Zbus/Zth | `numpy.linalg.inv`, `ShortCircuitSolver.z_th/_z` | Плотная комплексная Y | Полная Zbus; диагональ Zth и взаимные Z | Ом на базисной ступени | Полное обращение матрицы | Не сохраняются condition number, оценка ошибки, исходная Y и версия численного backend | Нет | Взаимные элементы комплексные; наружу для точки используется диагональ | `u_base` |
| Zth → ток в точке | `ShortCircuitSolver.at` | Zth на базе, U класса точки, `k_two_phase` | `ScResult.i3/i2`, Zth точки, trace | Ом, кВ → кА | Обратное приведение `Zточки=Zбазы·(Uср.точки/Uср.базы)²`; `I3=Uср/(√3·|Z|)`; `I2=k2·I3` | Для тока удаляются комплексная фаза и направление; RMS/амплитуда не типизированы; I2 не использует Z2 | Нет | Только неотрицательные модули | `u_avg(node.u_nom)` и `u_avg(u_base)` |
| Zbus → ток через ветвь | `distribution_factor`, `Context.current_through` | Взаимные Z, Z ветви, I точки, сторона защиты | Модуль коэффициента d и ток через защиту в А | d безразмерный; кА→А | `d=(Zik−Zjk)/Zветви`, затем `abs(d)`; `I·1000·Uточки/Uзащиты` | Теряются знак, фаза и вклад каждого источника; нулевая ветвь в кольце неоднозначна | Нет | Ориентация вычисления существует до `abs`, затем исчезает | От `Uср` точки к `Uср` физического узла ТТ |
| Токи + нагрузки + профиль → РЗА | `core.engine.run`, `load_current`, `protections/*`, `selectivity` | Скаляры Iк, нагрузки, ТТ, коэффициенты и времена | `ProtectionResult`, пары селективности, протокол | А, с, безразмерные K | Формулы перечислены в `adjacent-formula-register.md` | Профиль-заглушка; неподтверждённый I2 и модуль d переходят в проверки чувствительности | Уставки могут округляться по шкале терминала; сравнение Δt имеет допуск `1e-9 с`; текст форматируется отдельно | Направление пытается определяться топологической иерархией, но не комплексным током | Токи пересчитываются к стороне защиты по `Uср` |
| `ProjectResult` → GUI | `ProjectViewModel.recalculate`, таблицы/протокол | `ProjectResult` и сохранённая `self.net` | Отображаемые строки, статусы и числа | кА/А/с согласно конкретному виджету | `fmt()` только для представления | Нет model revision, topology fingerprint, methodology snapshot и core version; `self.net` может быть устаревшей ссылкой | Только текстовое форматирование | В основном модули; фазоры не показываются | Использованная база и роли напряжений отдельно не выводятся |

Матрица подтверждает два разных вида потерь. Первая группа появляется намеренно
при переходе к топологии (физические параметры ей не нужны). Вторая группа
опасна для расчёта: богатые параметры проекции, происхождение данных,
последовательности, фазы, направления и fingerprint не доходят до численного
результата.

## 9. Граница подтверждения

Проверены фактический Python-код и безопасные in-memory сценарии. Не проверялись
достоверность формул ТКЗ, соответствие нормативной методике, численная точность
на промышленных контрольных примерах и интерактивная работа Qt GUI. Production-
код в рамках этого аудита не изменялся.
