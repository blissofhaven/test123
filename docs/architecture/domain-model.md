# Каноническая электрическая Domain Model

## Назначение и граница

`rza_calc.domain.electrical.ElectricalModel` — канонический агрегат электрической части проекта. Он не импортирует `core`, не хранит координаты и SVG и не выполняет расчёт КЗ или алгоритмы топологии.

Агрегат владеет следующими коллекциями:

```text
ElectricalModel
├─ voltage_classes: VoltageClass
├─ equipment_types: EquipmentTypeDefinition
├─ equipment: EquipmentInstance
├─ ports: PortInstance
├─ electrical_nodes: ElectricalNode
├─ connections: Connection
├─ operating_states: OperatingState
├─ logical_lines: LogicalLine
└─ line_sections: LineSection, keyed by EquipmentId
```

Все публичные коллекции доступны как read-only `MappingProxyType`. Метаданные агрегата `name`, `revision`, `neutral` и `extensions` хранятся в private-полях и выдаются только read-only properties; `neutral` и `extensions` также глубоко заморожены. Записи — frozen dataclass, а вложенные JSON-значения копируются в неизменяемые `MappingProxyType`/tuple. Изменение модели выполняется только командами агрегата и при успешной операции один раз увеличивает `revision`, возвращая `ChangeSet`. Восстановление сохранённого revision через `_restore_revision()` является внутренней операцией loader, а не публичной командой редактирования.

## Стабильные ID

Для разных сущностей используются разные runtime-типы:

- `EquipmentId`, `PortId`, `ElectricalNodeId`, `ConnectionId`,
  `OperatingStateId`, `LogicalLineId`;
- `EquipmentTypeId`, `VoltageClassId`, `PortKindId`.

ID сериализуется одной строкой, но внутри Python нельзя подменить, например, `EquipmentId` значением типа `PortId`. `StableId.new()` использует UUID4 с префиксом вида сущности. `deterministic_id()` использует UUID5 и length-prefixed части. Legacy importer передаёт в неё исходный legacy ID, категорию и роль порта, поэтому созданные им ID не зависят от имени отображения или позиции записи в JSON.

Строковое значение ID глобально уникально между классами напряжения, типами
оборудования и экземплярами equipment/port/node/connection/state/logical line.
`LineSection` использует ID принадлежащего ему equipment и не вводит второй
параллельный ID. Повторные версии одного `EquipmentTypeId` являются версиями
одной идентичности и хранятся по ключу `(type_id, schema_version)`. Значение
`GRID` зарезервировано границей legacy-расчёта и запрещено для domain-объектов.
`CatalogEntryId` и `CatalogCategoryId` относятся к отдельному каталожному
агрегату.

## Классы напряжения

`VoltageClass` содержит `id`, целое положительное `nominal_voltage_v`, `display_name`, `system_kind` и `extensions`. Это расширяемый реестр, а не закрытый enum. `ElectricalModel.with_builtins()` регистрирует 220, 110, 35, 10, 6 и 0,4 кВ. Дополнительный класс можно зарегистрировать без изменения фундаментальных классов модели; одинаковое число вольт нельзя зарегистрировать под двумя ID.

Класс напряжения может быть явно объявлен у `ElectricalNode` и назначен группе портов в `EquipmentInstance.voltage_class_by_group`. Domain-команды отвергают неизвестные и несовместимые значения. Автоматического распространения напряжения по схеме пока нет.

## Тип и экземпляр оборудования

`EquipmentTypeDefinition` содержит:

- `id`, `schema_version`, `display_name`, `behavior_key`;
- `port_definitions: tuple[PortDefinition, ...]`;
- `property_definitions: tuple[PropertyDefinition, ...]`;
- `capabilities`, `allow_additional_properties`, `extensions`.

Роли портов и ключи свойств уникальны в пределах определения. `PortDefinition` задаёт `role`, `kind_id`, опциональную `voltage_group`, разрешённые классы напряжения, обязательность и `max_connections`. Нормализованная модель Этапа 1 допускает только `max_connections == 1`.

`EquipmentInstance` содержит стабильный `id`, пару `type_id/type_version`, имя, упорядоченные `port_ids`, свойства, классы напряжения по группам, нормальное положение, примечание и расширения. Экземпляр не содержит `node_from/node_to`.

`PortInstance(id, equipment_id, role)` принадлежит ровно одному оборудованию. При добавлении агрегат атомарно проверяет владельца, состав и порядок `port_ids`, обязательные и допустимые роли, отсутствие повторов, существование типа и классов напряжения, свойства и глобальную уникальность всех ID.

Встроенный начальный реестр содержит external grid, generator, busbar,
connection point, прежние overhead line/cable, circuit breaker, transformer
2W/3W и load. Дополнение добавило отдельный `builtin.recloser` и физические
типы `builtin.line_section.overhead`/`builtin.line_section.cable`. Реклоузер
имеет собственный `behavior_key` и не является alias обычного выключателя.
Набор остаётся проверочным, а не полным отраслевым каталогом.

## Узлы и подключения

`ElectricalNode` — стабильная эквипотенциальная точка с `kind_id`, опциональным явно объявленным классом напряжения, именем, примечанием и расширениями.

`Connection(id, port_id, electrical_node_id, extensions)` — единственная
электрическая связь Этапа 1. Один порт может иметь не более одного такого
подключения. Многоточечный узел представляется несколькими подключениями разных
портов к одному `ElectricalNode`; число таких подключений у узла не ограничено
двумя. Поэтому один явный узел корректно соединяет четыре и более независимых
ветвей. Попарной сущности «провод от порта A к порту B» в Domain Model нет.

`connect_port()` подключает порт к существующему узлу. `connect_ports()` либо использует уже существующий узел одного из портов, либо атомарно создаёт новый узел и недостающие подключения. Два уже подключённых к разным узлам порта требуют явного `merge_nodes()`. Перед первой мутацией проверяются ссылки, типы ID, электрический kind, классы напряжения, конфликт peer-портов и все будущие ID.

## Логические линии, участки и отпайки

`LogicalLine(id, name, line_kind, voltage_class_id,
section_equipment_ids, inherited_properties, note, extensions)` — устойчивая
пользовательская идентичность линии. Она сама не проводит ток: электрическую
ветвь образуют упорядоченные двухполюсные equipment из
`section_equipment_ids`. `line_kind` имеет значения `overhead`/`cable`.

`LineSection(equipment_id, logical_line_id, length_mm, extensions)` связывает
ровно один equipment типа `builtin.line_section.overhead` или
`builtin.line_section.cable` с логической линией. `length_mm` — единственный
источник длины и всегда является положительным целым; свойства
`length_mm`/`length_km` в property bag запрещены.

Основные инварианты:

- линия содержит хотя бы один участок, без повторов;
- участки образуют непрерывную ориентированную цепочку `from → to` без
  скрытого цикла;
- тип участка соответствует `line_kind`, а класс напряжения — классу линии;
- запись `LineSection`, equipment и членство в `LogicalLine` образуют связь
  один-к-одному; orphan-записи запрещены;
- отпайка — обычный общий `ElectricalNode`. Визуальное пересечение без общего
  node электрической связи не создаёт.

Эффективные параметры участка разрешаются в порядке: defaults определения
типа → `LogicalLine.inherited_properties` → `EquipmentInstance.properties`.
Для изменения служат `set_line_inherited_property()`,
`set_section_override()`, `clear_section_override()` и
`set_section_length()`.

Составные команды работают через staged-копию и увеличивают revision один раз:

- `create_logical_line()` создаёт линию, первый участок, порты и подключения;
- `split_line_section()` заменяет один участок двумя новыми, сохраняет сумму
  длин и создаёт между ними явный узел отпайки;
- `create_tap_line()` атомарно выполняет split и создаёт от общего узла новую
  логическую линию;
- `remove_line_section()` удаляет участок; при удалении среднего участка
  разделяет оставшиеся части на две логические линии;
- `remove_logical_line(..., cascade=True)` удаляет её участки, но сохраняет
  электрические узлы;
- `remove_line_tap()` удаляет явно перечисленные линии отпайки и по умолчанию
  схлопывает два соседних одинаковых участка основной линии. Схлопывание
  запрещено при разных параметрах или оставшихся подключениях и целиком
  откатывается при ошибке; `collapse=False` сохраняет основной узел и участки.

## Рабочие состояния

`OperatingState` хранит две ортогональные sparse-map: `positions:
Mapping[EquipmentId, SwitchPosition]` (`OPEN`/`CLOSED`) и `availability:
Mapping[EquipmentId, EquipmentAvailability]` (`IN_SERVICE`/`OUT_OF_SERVICE`).
Отсутствие availability-записи означает `IN_SERVICE`. Состояние адресует
реальное оборудование, а не имя, branch ID или конец ветви.

Обычная команда `set_switch_position()` разрешена только типу с capability `switch.position`. Импортированные legacy-объекты могут иметь отдельную capability `legacy.switch.position`; она нужна для точного round-trip старых `Mode`, но не превращает составную legacy-ветвь в физически достоверный выключатель.

`set_equipment_availability()` разрешена только типу с capability
`equipment.availability`. Native источники, генераторы, нагрузки, линии,
коммутационные аппараты и трансформаторы её имеют; bus marker — нет.

## Удаление и целостность

- `remove_connection()` удаляет только указанное подключение.
- `remove_equipment(..., cascade=False)` запрещает удаление подключённого оборудования; `cascade=True` удаляет его подключения и порты, а также атомарно очищает нормализованные позиции и принадлежащие объекту compatibility-ключи legacy `Mode.states` (включая стороны выключателя и лучи Transformer3W).
- `remove_node(..., cascade=False)` запрещает удаление узла с подключениями;
  для обычного узла `cascade=True` удаляет подключения. Узел, участвующий в
  логической линии, удаляется только составной командой удаления/схлопывания
  отпайки.
- `merge_nodes()` переносит подключения и удаляет второй узел только после полной проверки совместимости.

`validate_integrity()` повторно проверяет cross-kind ID, `GRID`, ссылки, двустороннее владение equipment↔ports, роли, кратность подключения, kind/voltage, допустимость состояний и другие инварианты. Отклонённая команда не должна оставлять частичную мутацию.

## Compatibility equipment

При импорте v1/v2 создаются приватные определения `compat.rza_calc.*` для
source, generator, line, transformer 2W/3W, tie, generic branch и load. Они
сохраняют полный legacy payload и marker-данные, необходимые для обратимого
построения `core.Network`. Составная legacy-ветвь не раскладывается на
выдуманные физические QF/CT/линию. Compatibility equipment допустимо после
сохранения мигрированной модели в v5, но не является системным каталогом для
создания новых объектов пользователем.

## Каталожная граница

`SystemCatalog` и `UserCatalog` реализованы отдельно от `ElectricalModel`.
Запись каталога материализует параметры или независимый `EquipmentInstance`,
но только команда агрегата может зарегистрировать equipment и его порты.
`ProjectData` владеет embedded `UserCatalog`; системный каталог поставляется
приложением. Подробный контракт описан в [catalogs.md](catalogs.md).

## Переходные ограничения

`ProjectStructure` остаётся отдельной физико-навигационной структурой старого
приложения и сохраняется в project v6 ради совместимости. Она не владеет
электрической связностью. Единый `TopologyEngine` Этапа 2 компилирует
`ElectricalModel` в immutable `TopologySnapshot`. Data-only
`DiagramDocument` страниц и представлений уже входит в формат v6; экранный
canvas-редактор и пользовательские команды работы с ним остаются границей
Этапа 3.
