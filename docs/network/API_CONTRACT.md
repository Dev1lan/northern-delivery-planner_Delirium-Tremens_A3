# API-контракт для фронтенда · сетевое ядро A3

Схема сетевого результата — `3.0.0`. Документ описывает HTTP-сервер из `network/web.py` и вызовы Python для backend сайта. Старый линейный расчёт v1/v2 имеет отдельные контракты.

## Запуск и общие правила

```powershell
python -m network.web
```

Локальный адрес по умолчанию — `http://127.0.0.1:8765`. Можно указать `--port` и загрузить ранее рассчитанный набор через `--bundle path/to/bundle.json`. Сервер слушает loopback. Демо использует запросы с того же origin. Для отдельного фронтенда нужен серверный прокси на том же origin либо свой backend с настроенным CORS. Текущий HTTP-сервер не предоставляет CORS, авторизацию, пользовательские сессии или хранилище планов.

- Ответы API — JSON в UTF-8 с `Cache-Control: no-store`. Для POST с телом передавайте `Content-Type: application/json`.
- Время — ISO 8601 без часового пояса, например `2026-07-01T04:30:00`. Все даты сценария используют одну временную шкалу. Для сетевого расчёта передавайте целые секунды. В JS учитывайте, что автоматический разбор строк без смещения зависит от браузерного часового пояса.
- Тоннаж в тоннах, входной тоннаж кратен `0.001` т. Расстояние в км, глубина и осадка в м, длительности по суффиксам `_seconds` или `_hours`. Деньги v2-тарифов в ответе — десятичные строки, валюта в `financial_evaluation.currency`. `DEMO` обозначает условную валюту.
- Идентификаторы сравниваются как строки. Названия в `scenario.ports` одновременно служат ключами портов. Для URL с кириллицей используйте `URLSearchParams`.
- Успешный ответ — `200`. Неизвестный путь для GET/POST возвращает `404 {"error":"Unknown endpoint"}`. Обработанные ошибки запроса возвращают `400 {"error":"..."}`. Текст ошибки диагностический и может меняться. Клиенту также следует обработать сетевые сбои и ответы вне JSON. `200` допускает недовоз, невыполненный минимум и отсутствие улучшения.

## HTTP-ручки

| Метод и путь | Вход | Ответ |
|---|---|---|
| `GET /api/demo` | Без параметров | Текущий полный пакет расчётов. |
| `POST /api/plan` | Без тела и query | Синхронно заново рассчитывает встроенный сценарий и публикует новый пакет. После загрузки через `--bundle` повторный расчёт использует встроенные входы. |
| `GET /api/state` | `moment` обязательно, `variant` по умолчанию `C` | Положения всех судов и состояние всех портов на выбранное время. |
| `GET /api/port-load` | `port`, `start`, `end` обязательно, `variant=C` по умолчанию | Статистика известного порта за период или JSON `null`, если причалы для порта не моделируются. |
| `GET /api/explain` | `voyage` обязательно, `variant=C` по умолчанию | Маршрут, срок, аудит и цена рейса. Номер начинается с 1 и действует только внутри выбранного плана. |
| `GET /api/compare` | `from`, `to` обязательно | Числовые разницы `to − from`. |
| `GET /api/improvements` | Без параметров | Результаты заранее рассчитанных изменений относительно C. |
| `POST /api/what-if` | `{"preset":0}` или `{"change":{...}}`, до 65 536 байт | Пересчитывает одно изменение относительно C и возвращает полный изменённый результат с разницей метрик. Исходный пакет остаётся прежним. |

`variant`, `from`, `to` принимают ключи объекта `plans`, обычно `A`, `B`, `C`, `D`. `D` присутствует, если расчёт нашёл улучшение. Имена стратегий из `strategies` здесь не используются. Статические маршруты демо — `GET /`, `GET /app.js`, `GET /style.css`.

### Пакет расчётов

| Поле | Содержимое |
|---|---|
| `manifest` | `algorithm_version`, `admission_rules`, `data_status`, `python`, `parameters`, `scenario_hash`, `options_hash`, `tariff_hashes`. |
| `source_inputs` | `scenario`, массив `options`, тарифы `tariffs.low/base/high`. |
| `labels`, `plans` | Подписи и объекты результата v3 по ключу варианта. Выбор в UI строится по `plans`. |
| `comparisons` | Массив `{from,to,delta}` для соседних рассчитанных вариантов. |
| `strategies` | Результаты v3 для пяти стратегий на входах C и базовых тарифах. |
| `tariff_sensitivity` | `low/base/high → стратегия → результат v3` для `NETWORK_REPOSITION` и `NETWORK_COST`. |
| `diagnoses` | `вариант → {cargo,queues}`. |
| `improvements`, `chosen_change` | Отчёт об альтернативах и объект выбранного изменения либо `null`. |

A — без морского порожнего перегона и без ограничения причала. B — с перегоном, без ограничения причала. C — с перегоном и причалом. D — с выбранным улучшением. Текущий набор синтетический.

### Состояние судов и портов

```text
GET /api/state?variant=C&moment=2026-07-01T00%3A00%3A00
```

`moment` лежит внутри `[scenario.start, scenario.end]`. Ответ содержит `vessels` по `vessel_id` и `ports` по имени порта.

- Судно — `{vessel_id,port,state,edge_id,progress}`. `state` принимает `IDLE`, `UNAVAILABLE`, `WAITING`, `PORT_QUEUE`, `LOADING`, `UNLOADING`, `EMPTY_TRANSIT`, `LOADED_TRANSIT`. В движении `port=null`, `edge_id` указывает на направленное ребро, `progress` — доля времени перехода от 0 до 1. В порту `edge_id` и `progress` равны `null`. Активность события определяется полуинтервалом `[start,end)`.
- Порт — `{port,time,vessels,queued_tugs,berths,capacity_modelled}`. `vessels` содержит буксиры и баржи, `queued_tugs` — только ID буксиров. Для отображения размера очереди используйте `queued_tugs.length`.
- Причал — `{berth_id,status,operation,expected_release}`. Статусы `OPEN`, `OCCUPIED`, `CLOSED`. `operation` содержит текущее событие или `null`. Начальная внешняя бронь даёт `OCCUPIED` и время освобождения при `operation=null`. Если `capacity_modelled=false`, загрузка причалов неизвестна.

`GET /api/port-load` ограничивает запрошенный период границами сценария. Пустое пересечение даёт `400`. Ответ содержит `waiting_seconds`, `peak_queue`, `mean_wait_seconds`, `handled_voyages`, `loaded_t`, `unloaded_t`, `mean_operation_seconds`, `berths[]`. Для каждого причала доступны `berth_id`, `open_seconds`, `busy_seconds`, `utilization` (доля или `null`) и `reservations[]` с `start/end`. Занятость включает начальные брони. Тоннаж считается по операциям, завершившимся в `(start,end]`. Средние могут быть `null`.

### Рейс и сравнение

`GET /api/explain?variant=C&voyage=1` возвращает `voyage_id`, `tug_id`, `barge_id`, `departure_port`, `pickup`, `destination`, `loaded`, `empty`, `unloading_end`, `due_at`, `on_time`, `checked_constraints`, `audit`, `cost`, `plan_level_comparison`, `voyage_level_alternatives_calculated`. `cost` — `{voyage_id,total_cost,cost_by_item}` или `null`. Сравнение перегонки относится ко всему плану. В текущем ответе `voyage_level_alternatives_calculated=false`.

`GET /api/compare?from=C&to=D` в демонстрационном наборе возвращает:

```json
{
  "delivered_on_time_t": 65.0,
  "delivered_total_t": 15.0,
  "critical_on_time_t": 0.0,
  "social_deficit_t": 0.0,
  "empty_km": 0.0,
  "undelivered_t": -15.0,
  "completed_voyages": 0,
  "resource_busy_hours": -9.057222,
  "port_waiting_seconds": -17520,
  "satisfied_requirements": 0,
  "cost": "-896.05",
  "cost_per_t_on_time": "-21.07"
}
```

Денежные разницы доступны, когда оба расчёта имеют `price_status=PRICED`, одну валюту и одинаковые параметры тарифов. Иначе возвращается `null`. `cost_per_t_on_time` также требует ненулевой своевременный тоннаж в обоих вариантах.

### «Что, если?» и диагностика

```javascript
const response = await fetch("/api/what-if", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ change: { kind: "marine_reposition", enabled: false } })
});
const payload = await response.json();
if (!response.ok) throw new Error(payload.error);
const changedPlan = payload.alternatives[0].result;
```

`preset` — индекс с 0 в `source_inputs.options`. Передавайте либо его, либо один `change`. В текущем наборе это добавление причала, повышение скорости обработки и закрытие причала. Альтернативный план приходит непосредственно в ответе. Для него не создаётся ключ `variant` и его нельзя запросить через `/api/state` или `/api/explain` этого сервера.

| `change.kind` | Остальные поля |
|---|---|
| `marine_reposition` | `enabled: boolean`. Переключает только порожний подход по морскому ребру. |
| `unlimited_port` | `port` с заданным профилем. Снимает ограничение причалов, оставляя обработку по ставке баржи. |
| `add_berth` | `port`, `berth` в формате сценария. |
| `close_berth` | `port`, `berth_id`. |
| `handling_tph` | `port`, `berth_id`, `value` — положительное число или словарь «тип груза → тонн в час». |
| `close_edge` | `edge_id`. Закрывает указанное направление. |
| `cargo` | `cargo_id`, `volume_t` и/или `due_at`. |
| `move_vessel` | `vessel_id`, `port`. Меняет стартовую точку во входах сценария. |
| `remove_vessel` | `vessel_id`. Удаляет также совместимые пары с этим судном. |
| `add_vessel` | `fleet` (`tugs` или `barges`), `vessel`, опционально `compatible_pairs`. Для морского ребра дополнительно нужен допуск пары в самом ребре. |

Обе ручки альтернатив возвращают `{baseline_fingerprint,improvement_found,alternatives}`. Элемент массива — `{change,delta,diagnosis,result,improved,explanation}`. `explanation` содержит `physical_audit`, `delta`, `unmet_requirements`, `comparison_scope`. Ранжирование учитывает число невыполненных минимумов, дефицит, своевременный и общий тоннаж, сопоставимую цену и порожний пробег. Валидная альтернатива может ухудшить результат.

Диагностика `{cargo,queues}` показывает по заявке `cargo_id`, `cargo_type`, `delivered_t`, `remaining_t`, `late_t`, `evidence[]`. Элемент доказательства содержит `kind` и `code`, при необходимости `late_t` или `expired_t`. Коды:

- `PROVEN` — `NO_DIRECTED_PATH`, `NO_COMPATIBLE_PAIR`, `NO_ADMITTED_PATH`, `NO_PATH_WITH_NAVIGATION_WINDOWS`, `READY_AFTER_SEASON`, `EXPIRED_BEFORE_DISPATCH`.
- `OBSERVED` — `LATE_UNLOADING`, `EXPIRED_BY_UNLOADING`. Очереди отдельно в `queues[]` как `{port,seconds,kind:"OBSERVED"}`.
- `UNDETERMINED` — `RESIDUAL_WITHOUT_PROVEN_CAUSE`.

Наблюдаемая очередь сама по себе не доказывает причину недовоза. Для оценки её влияния используйте сравнение сценариев.

## Контракт нового ядра v3

Нормативная схема результата — [result.v3.schema.json](../../result.v3.schema.json). Полный рабочий пример входа — [yakutia_stress.json](../../data/yakutia_demo/yakutia_stress.json). [Пример тарифов](../../data/yakutia_demo/tariffs_base.json) и [пример изменений](../../data/yakutia_demo/improvements.json) находятся рядом.

### Вход сценария

| Часть | Формат |
|---|---|
| Сценарий | `scenario_id`, `data_status`, `start`, `end`, `ports[]`, `edges[]`, `tugs[]`, `barges[]`, `compatible_pairs[]`, `cargo[]`. Опционально `underkeel_m`, `fixed_handling_seconds`, `allow_marine_reposition`, `social_requirements`, `cargo_types`, `port_operations`. |
| Ребро | `edge_id`, `origin_port`, `destination_port`, `distance_km`, `direction` (`upstream/downstream`), `condition` с `usable_depth_m`, `max_allowed_draft_m` и опциональным `is_closed`. Дополнительно `daily_conditions` по дате, `water_type` (`RIVER` по умолчанию, `SEA`, `CANAL`, `ESTUARY`), `navigation_windows[]`, `speed_limit_kmh`, `navigation_area`, `sea_clearance_required`, `allowed_vessel_classes[]`, `allowed_pairs[]`. Обратное направление задаётся своим ребром. Окно — `{start,end}`. Пустой список окон закрывает ребро. |
| Флот | Для обоих типов — `vessel_id`, `current_port`, `available_at`, `available_to`, `draft_empty_m`. Буксир — `speed_upstream_kmh`, `speed_downstream_kmh`. Баржа — `capacity_t`, `draft_full_m`, `handling_tph`, `cargo_types_allowed[]`. Пара — `{tug_id,barge_id}`. |
| Морской допуск | На судне `vessel_class` и `navigation_permits[]` с `{area,status,source}`. Для морского ребра требуются район и явная пара в `allowed_pairs`. Проверяются оба судна. `SCENARIO_ASSUMPTION` используется для синтетического сценария. Для `REAL/MIXED` морскому составу нужен `CONFIRMED` и несинтетические записи о судах. `UNVERIFIED` допуск не даёт. |
| Заявка | `cargo_id`, `cargo_type`, `origin_port`, `destination_port`, `volume_t`, `ready_at`, `due_at`. Опционально `priority`, `critical_category`, `expires_at`. |
| Причалы | `port_operations[port].berths[]`. Причал задаётся через `berth_id`, `operations[]` (`LOADING/UNLOADING`), `handling_tph` числом или по типам груза. Дополнительно `working_windows[]`, `initial_occupancy[]`, `loading_fixed_hours`, `unloading_fixed_hours`, `max_allowed_draft_m`, `usable_depth_m`, `allowed_barge_classes[]`. |
| Минимумы | `social_requirements[]` с `requirement_id`, `cargo_types[]`, `required_t`, `deadline`, `basis`, `source_status`. Для `delivered_by_deadline` нужен срок. Для `delivered_total` срок равен `null`. Категории разных минимумов не пересекаются. |

`data_status` принимает `SYNTHETIC`, `REAL`, `MIXED`. Для записей со статусом `REAL/MIXED` указывается `source`. Для смешанного сценария статус передавайте по каждой записи. Координаты, если они нужны карте, берутся из отдельно подготовленных геоданных. Граф расчёта использует заданные рёбра и расстояния.

### Тарифы v2

Объект `tariff_version: 2` содержит `currency`, `source_status`, `fuel_basis` (`km/hour`) и `rates`. Каждая ставка — `{value,unit,source,source_status}`. `value` неотрицателен, `source_status` ставки — `SYNTHETIC`, `REAL` или `ASSUMED`.

- Обязательные ставки: `loaded_fuel`, `empty_fuel` в единицах `fuel_basis`, `tug_operation`, `barge_operation`, `waiting` за `hour`, `loading`, `unloading` за `tonne`, `port_call` за `call`, `repositioning` за `km`.
- `by_vessel` может переопределить эксплуатацию и топливо. `by_port` может переопределить погрузку, выгрузку, портовый сбор и ожидание.
- `alternative_delivery[cargo_type]` — опциональный тариф за `tonne`. Если часть остатка не оценена, результат имеет `price_status=PARTIAL` и `unpriced_undelivered_t`.
- При `tug_operation_includes_fuel=true` отдельные ставки топлива должны быть нулевыми. Для `NETWORK_COST` обязательны тарифы v2.

### Результат

- Идентификация — `schema_version`, `algorithm_version`, `scenario_id`, `data_status`, `strategy`, `scenario`, `input_fingerprint`, `parameters`. Хэш относится к входному сценарию. Для повторения расчёта сохраните также стратегию, лимиты и тарифы.
- Поиск — `termination_reason` (`SEARCH_FINISHED` или `SEARCH_LIMIT`) и `expanded_states`. Результат с `SEARCH_LIMIT` можно показывать, если аудит пройден. Статус сообщает о завершении по лимиту.
- Аудит — `schedule_audit:{status,violations}`. Успешно рассчитанный результат содержит `PASSED`. Независимый повторный аудит может вернуть `FAILED` и нарушения `{voyage,code,detail}`.
- Минимумы — `requirements_status` (`NOT_CONFIGURED`, `SATISFIED`, `NOT_SATISFIED`) и `feasibility_status` (`NOT_CHECKED`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`). `INFEASIBLE` означает доказанный дефицит по верхней оценке, `UNKNOWN` — минимум в найденном плане не выполнен и доказательства невыполнимости нет. В `requirements[]` доступны `required_t`, `delivered_by_deadline_t`, `delivered_total_t`, `deficit_t`, `deficit_lower_bound_t`, `satisfied`, `reason` и входные поля требования.
- `metrics` — `demand_t`, `delivered_on_time_t`, `delivered_total_t`, `undelivered_t`, `critical_on_time_t`, `social_deficit_t`, `loaded_km`, `empty_km`, `tonne_km`, `late_tonne_hours`, `resource_busy_hours`, `resource_idle_hours`, `completed_voyages`, `repositioning_voyages`. Для очередей используйте `port_operations[port].waiting_seconds`.
- `cargo_status[]` — `cargo_id`, `cargo_type`, `delivered_t`, `remaining_t`. При наличии срока годности добавляется `cargo_condition[]` с `cargo_id`, `physically_delivered_t`, `usable_delivered_t`, `expired_t`.
- `voyages[]` — `tug_id`, `barge_id`, `cargo_id`, `volume_t`, `load_start`, `departure`, `unloading_end`, массивы переходов `empty`, `loaded`, `return_empty`. Переход — `{edge_id,depart,arrive}`. Для работы с причалами могут присутствовать `loading_berth`, `port_ready_at`, `unloading_berth`, `unloading_start`.
- `events[]` — `voyage_id`, `tug_id`, `barge_id`, `cargo_id`, `state`, `origin_port`, `destination_port`, `start`, `end`, `edge_id`, `km`, `volume_t`, опционально `berth_id`. `edge_id` и `cargo_id` могут быть `null`. Это журнал для карты и шкалы времени. `final_resources[]` описывает итоговое состояние флота.
- `financial_inputs` содержит физические объёмы работ по рейсам, судам и в сумме. `financial_evaluation` может быть `null`. При расчёте по тарифам v2 доступны `currency`, `total_cost`, `cost_per_t`, `cost_per_t_on_time`, `cost_by_unit`, `cost_by_voyage`, `cost_by_vessel`, `cost_by_port`, `undelivered_penalty`, `empty_transition_cost`, `price_status`, `unpriced_undelivered_t`, ставки и параметры тарифа. Стоимость на тонну равна `null`, если соответствующий тоннаж равен нулю.
- `reposition_comparison` — сравнение с планом без порожней перегонки либо `null`. `port_operations` и `cost_service_floor` — опциональные расширения для портов и поиска по цене.

## Вызовы ядра для backend сайта

HTTP-ручки выше работают со встроенным демо. Для произвольных входов, аудита сохранённого результата, собственного тарифного набора или перепланирования backend может импортировать следующие функции.

| Python | Вход и результат |
|---|---|
| `network.adapter.run_scenario(raw, strategy, *, limits=None, tariffs=None)` | JSON-словарь сценария → результат v3. По умолчанию `NETWORK_REPOSITION`. `limits` — объект `network.models.Limits`. |
| `network.adapter.audit_result(result)` | Повторно проверяет сохранённый результат. Возвращает аудит и статусы минимумов, при некорректной версии/идентичности входов может выбросить `ValueError`. |
| `network.analysis.positions_at(result, moment)`, `port_at(result, port, moment)`, `port_load(result, port, start, end)` | Данные карты и портов для переданного результата, включая план из «Что, если?». |
| `network.analysis.diagnose(result)`, `explain_voyage(result, voyage_id)`, `compare_results(base, alternative)` | Диагностика, детали рейса и разницы метрик. Сравнивайте планы с проверенным аудитом. |
| `network.analysis.change_scenario(raw, option)`, `find_improvements(raw, baseline_result, options, *, strategy, limits, tariffs)` | Копия сценария с одним изменением и отчёт по проверенным альтернативам. Базовый результат должен соответствовать `raw`. |
| `network.financial.evaluate_schedule(result, tariffs)` | Оценка проверенного плана по тарифам. Для изменения тарифа внутри оптимизации повторно вызовите `run_scenario` с `NETWORK_COST`. |
| `network.replanning.replan(previous, revised_raw, as_of, *, resource_updates=None, delivered_t=None, remaining_t=None, strategy, limits=None)` | Пересчёт будущих рейсов с фиксацией начатых. Возвращает `REPLANNED` с `remaining_plan` и `projected_requirements` либо `REQUIRES_INTERVENTION` с `reason`, `detail` и `remaining_plan=null`. Сохраняет `locked_events`, `locked_voyages`, `observed_resources`, фактические `delivered_t/remaining_t` и хэши. Текущая функция не принимает тарифы. |

Стратегии ядра — `NETWORK_FCFS`, `NETWORK_GREEDY`, `NETWORK_SOCIAL`, `NETWORK_REPOSITION`, `NETWORK_COST` и диагностическая `NETWORK_RETURN_EMPTY`. Для рабочего перепланирования используйте `NETWORK_REPOSITION` по умолчанию.

Пример вызова произвольного сценария на backend:

```python
import json
from pathlib import Path

from network.adapter import audit_result, run_scenario

scenario = json.loads(Path("data/yakutia_demo/yakutia_stress.json").read_text(encoding="utf-8-sig"))
tariffs = json.loads(Path("data/yakutia_demo/tariffs_base.json").read_text(encoding="utf-8-sig"))
plan = run_scenario(scenario, "NETWORK_COST", tariffs=tariffs)
if audit_result(plan)["schedule_audit"]["status"] != "PASSED":
    raise ValueError("Schedule audit failed")
```

### Старый линейный движок v1/v2

`result_adapter.run_scenario(scenario_path, *, config_path, strategy, contract_version, requirements, ...)` принимает папку CSV-сценария. В v1 доступны `FCFS`, `EARLIEST_DUE` и `OPTIMIZED`, в v2 дополнительно `SOCIAL_PRIORITY` и `requirements`. Схемы: [v1](../../result.schema.json), [v2](../../result.v2.schema.json). Для диспетчеризации по версии используйте `schema_version`. В старом контракте список рейсов — `trips`, в v3 — `voyages` и `events`. У локального HTTP-сервера v3 отсутствуют ручки для v1/v2, ML и импорта файлов.
