# Topology Engine — реализованный контракт Этапа 2

Статус: Этап 2 реализован. Модуль `rza_calc.topology` не импортирует SVG,
координаты или `core.Network` и компилирует канонический `ElectricalModel` в
глубоко неизменяемый `TopologySnapshot`.

## Публичная граница

```python
snapshot = TopologyEngine().compile(model, state_id_or_state)
```

Допустимы зарегистрированный `OperatingStateId`, отдельный immutable
`OperatingState` и `NORMAL_STATE`. Компиляция неполной схемы возвращает
частичный snapshot с typed diagnostics; `snapshot.require_valid()` является
строгой границей и выбрасывает `TopologyCompilationError` при Error.

Snapshot содержит:

- исходную process-local identity модели, `model_revision`, registry signature
  и полностью разрешённое состояние;
- все реальные electrical node ID, подключения port → node и физические links;
- active connected components/islands, точные источники и energization;
- кольца, независимый cycle rank и группы параллельных links;
- номинальные voltage zones, evidence, UNKNOWN/RESOLVED/CONFLICT;
- Error/Warning/Info с typed ссылками на equipment/port/node/connection;
- запросы `neighbors`, `links_between`, `find_path`, `source_paths`,
  `feeder_tree`, `sources_for` и `voltage_zone_of`;
- детерминированные `semantic_signature()` и `topology_fingerprint`, включая
  точное соответствие каждого port/role своему electrical node.

Все mappings заморожены через `MappingProxyType`, вложенные коллекции — tuple
или frozenset. Snapshot не ссылается на изменяемые коллекции Domain Model.
`TopologyComponent.link_ids/equipment_ids` содержат только проводящие links
активной компоненты; физический OPEN/OUT_OF_SERVICE аппарат остаётся в общем
`snapshot.links`, но не приписывается сразу к двум разделённым островам.

## Два независимых графа

Topology Engine намеренно не смешивает две разные инженерные величины.

### Активная связность режима

`CLOSED` включает внутреннюю проводимость аппарата, `OPEN` оставляет сам
аппарат и его порты в snapshot, но разрывает путь. `OUT_OF_SERVICE` является
отдельным состоянием эксплуатации и также исключает оборудование из активной
топологии, не подменяя OPEN/CLOSED.

Источники не соединяются через искусственный `GRID`. Компонент питается, если
в нём есть хотя бы один активный внешний источник или генератор. Поэтому две
стороны открытого секционного аппарата могут независимо оставаться под
напряжением от разных источников.

Активный graph является multigraph/hypergraph:

- параллельные линии сохраняются отдельными links;
- общий electrical node имеет неограниченное число ветвей;
- двухобмоточный трансформатор связывает две стороны;
- трёхобмоточный трансформатор представлен одной звездой/hyperedge, а не
  ложным треугольником;
- cycle detection выполняется на expanded star graph, поэтому сам 3W
  трансформатор не создаёт фиктивное кольцо.

`feeder_tree()` возвращает только детерминированную BFS-проекцию. Для кольца
или нескольких параллельных путей `non_tree_link_ids` сохраняет хорды: API не
выдаёт такую сеть за радиальную и не угадывает направление мощности.

### Номинальные области напряжения

Номинальный класс напряжения — свойство физической сети, а не признак наличия
питания. Поэтому OPEN аппарат прекращает энергоснабжение, но не стирает
номинальные 10 кВ с отключённой стороны.

Evidence собирается из:

- `ElectricalNode.declared_voltage_class_id`;
- `EquipmentInstance.voltage_class_by_group`;
- `LogicalLine.voltage_class_id`;
- однозначного ограничения `allowed_voltage_class_ids`.

Линии, кабели, QF и реклоузеры объединяют same-voltage zone независимо от
текущего положения. Трансформатор является границей зон: 110/10 кВ находится
в одной active component, но в двух номинальных zones и не создаёт конфликт.
Если паспортные стороны трансформатора не заданы, ядро возвращает UNKNOWN и
не угадывает коэффициент по имени.

## Обработчики поведения

Проводимость определяется только явным `behavior_key` из immutable
`TopologyBehaviorRegistry`. Встроены handlers для source, generator, bus,
line, line_section, switch, recloser, transformer 2W/3W, load и всех
`legacy.*` compatibility behaviors.

Неизвестное поведение, неверные роли или kind порта, обязательный неподключённый
порт и неоднозначное положение являются блокирующими ошибками. Компилятор
никогда не угадывает электрический закон по имени, картинке или числу портов и
не создаёт ghost link для незавершённого оборудования.

Compatibility handlers сохраняют raw `SW:<branch>:from/to` и отдельные
состояния обмоток legacy 3W из extensions. Они нужны только для честного
чтения мигрированных v1/v2 проектов и явно отмечаются Info diagnostic.

## Кэш и детерминизм

Кэш ограничен и разделён по identity конкретного `ElectricalModel`. Ключ
включает revision, полное содержимое/способ выбора состояния и fingerprint
registry. Две разные модели с одинаковым revision или два detached state с
одинаковым ID, но разным содержимым, не сталкиваются.

Перед и после компиляции проверяется revision. Изменение aggregate во время
сборки даёт `ConcurrentTopologyMutationError`, смешанный snapshot не попадает
в кэш. Порядок вставки records, имена и будущие координаты диаграммы не меняют
электрическую semantic signature.

## Calculation boundary

`adapt_to_calculation(model, topology_snapshot)` проверяет identity/revision и
блокирующие diagnostics snapshot. При наличии snapshot адаптер использует
разрешённое номинальное напряжение вместо требования вручную повторять `Un` на
каждом узле. Service availability преобразуется в отдельную sparse map legacy
`Mode`; это позволяет выводить источник, линию, трансформатор или нагрузку из
работы, не превращая обычную линию в фиктивный выключатель.

`ProjectData.network` отслеживает revision канонической модели и автоматически
перестраивается при следующем обращении после изменения состояния или состава
оборудования. Перед адаптацией проверяются все зарегистрированные operating
states: один корректный режим не может скрыть другой режим с неопределённым
положением аппарата. Поэтому расчёт не получает старый или неоднозначный
производный режим.

Вызов без snapshot сохранён для обратной совместимости и по-прежнему требует
явный класс напряжения на каждом calculation node.

## Граница следующих этапов

Diagram Model, страницы, canvas и `BoundaryPort` ещё не реализованы. Будущие
межстраничные переходы должны ссылаться на тот же `ElectricalNodeId`, поэтому
Topology Engine уже компилирует весь `ElectricalModel` независимо от того, на
какой странице будет показан объект. Этап 2 не добавляет UI и не начинает
Этап 3.
