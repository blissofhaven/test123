# ADR-003: односторонний legacy calculation adapter

- Статус: принято на Этапе 1, расширено границей TopologySnapshot на Этапе 2
- Дата: 2026-08-25

## Контекст

Существующий `core.Network` и расчётное ядро имеют значительное regression-покрытие, но их branches, синтетический `GRID`, служебная звезда Transformer3W и switch-флаги не являются достаточной физической Domain Model редактора. Немедленная замена ядра создала бы высокий риск потери расчётного поведения.

## Решение

Зависимость направлена только от канонической модели к legacy DTO:

```text
ElectricalModel --adapt_to_calculation()--> fresh core.Network
```

`adapt_to_calculation()` сначала проверяет domain-инварианты и точное соответствие фактических портов контракту выбранного `behavior_key`, затем создаёт новый `Network`, стабильный `CalculationTrace` и diagnostics. Неподдерживаемый behavior, лишний или отсутствующий расчётный порт, несовместимый kind, непредставимое состояние, конфликт ID или отсутствие явного напряжения являются ошибкой; адаптер не угадывает тип по имени и не молча отбрасывает объект или connection. Неинстанцированный optional port допустим, но любой созданный порт должен быть явно потреблён адаптером.

Обратной синхронизации изменённого `Network` нет. Отдельная операция `import_legacy_network()` разрешена только как детерминированный importer проектов v1/v2.

## Compatibility equipment

Legacy importer сохраняет старые объекты как определения `compat.rza_calc.*` с полным payload и markers. Это позволяет вновь получить исходные классы `core` и специальные данные `Mode`/Transformer3W без выдумывания самостоятельных физических аппаратов. Такие объекты помечены как compatibility-only и не являются целевой библиотекой оборудования.

## Последствия

- `ProjectData.network` после загрузки — свежий производный объект и
  автоматически обновляется при изменении revision канонической модели.
- Diagnostics этой адаптации сохраняются в `ProjectData.adapter_diagnostics`.
- Канонический `save_project()` игнорирует мутации этого объекта, заново
  адаптирует `electrical_model` и использует topology-propagated voltage.
- Legacy `save(net, ...)` остаётся отдельным API и пишет v2, потому что честное восстановление v3 из одного `Network` невозможно.
- `CalculationTrace` связывает domain nodes/equipment/ports с legacy IDs для будущей привязки результатов.
- `core.Network` можно сохранить и тестировать, не возвращая ему роль источника истины.

## Расширение Этапа 2

`adapt_to_calculation(model, topology_snapshot)` теперь принимает единый
проверенный `TopologySnapshot`, сверяет identity/revision, блокирует Error и
использует propagated nominal voltage. Вызов без snapshot сохранён как
совместимый мост и требует явное напряжение каждого узла. Сам адаптер не стал
topology engine: компоненты, источники, пути и voltage zones вычисляет только
`rza_calc.topology`.
