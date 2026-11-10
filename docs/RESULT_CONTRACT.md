# Контракт результата Case A3 — версия 1.0.0

Это предлагаемый общий контракт для алгоритма, дашборда и ML-разработчика; согласование с другими участниками отдельно не подтверждено. Машинное описание полей: `result.schema.json` (JSON Schema Draft 2020-12). Адаптер `result_adapter.py` вызывает существующий оптимизатор и его аудит, не меняет алгоритм и исходные сценарии.

## Запуск

Python 3.10+, только стандартная библиотека. Входной каталог содержит `cargo.csv`, `units.csv`, `conditions.csv` и контрольные суммы `expected_cargo_totals.csv` / `expected_plan_totals.csv` (если их не задаёт архивный манифест); формат и правила — в `docs/SCENARIO_INPUTS.md`. Параметры маршрута берутся из `scenario_config.json` или явно указанного JSON-файла.

```python
from result_adapter import run_scenario

result = run_scenario(
    'data/scenarios/r1_baseline_central_central',
    strategy='OPTIMIZED',
    scenario_id='yana_001',
)
print(result['metrics']['delivered_on_time_t'])
print(result['trips'])
```

Путь к каталогу сценария разрешается относительно текущей рабочей папки. Если приложение запускается из другой папки, передавайте абсолютный путь. `scenario_id` необязателен: по умолчанию используется имя папки; явное значение заменяет только идентификатор в результате. `config_path` необязателен, по умолчанию — `scenario_config.json` рядом с адаптером. `candidate_limit=None` означает полный просмотр кандидатов; положительное целое задаёт лимит для OPTIMIZED. Поддерживаемые стратегии: `FCFS`, `EARLIEST_DUE`, `OPTIMIZED`.

CLI из каталога проекта:

```powershell
python result_adapter.py --scenario data/scenarios/r1_baseline_central_central --strategy OPTIMIZED --scenario-id yana_001 --output result.json
python result_adapter.py --scenario data/scenarios/r1_baseline_central_central --strategy FCFS
```

Без `--output` JSON записывается в stdout в UTF-8; сообщения об ошибках и ненулевой код завершения означают неуспешный расчёт. С `--output` родительский каталог должен существовать. При вызове из Python ошибки входа или аудита передаются как исключения. Адаптер не возвращает успешный объект с вымышленными метриками, если расчёт прервался.

## Форма ответа

Объект всегда содержит `schema_version`, `scenario_id`, `strategy`, `metrics`, `trips`. Пример ниже показывает только пустой результат и не является измерением:

```json
{
  "schema_version": "1.0.0",
  "scenario_id": "yana_001",
  "strategy": "OPTIMIZED",
  "metrics": {
    "delivered_on_time_t": 0.0,
    "delivered_total_t": 0.0,
    "undelivered_t": 0.0,
    "completed_trips": 0,
    "violations": 0,
    "vessel_idle_days": 0.0,
    "fleet_utilization": 0.0,
    "average_delivery_days": 0.0,
    "delivered_by_type_t": {},
    "on_time_by_type_t": {}
  },
  "trips": []
}
```

Тоннаж измеряется в тоннах, `completed_trips` — число рейсов, `vessel_idle_days` — суммарные сутки простоя доступных физических ресурсов (барж и буксиров), `fleet_utilization` — доля доступного времени ресурсов, `average_delivery_days` — среднее время от готовности до завершения разгрузки, взвешенное по доставленной массе. При отсутствии доставок среднее время равно 0, как в симуляторе. Две карты в `metrics` содержат тоннаж по типам груза; известный тип без доставки имеет значение 0, при отсутствии заявок карты пусты.

`violations = 0` означает, что расписание прошло внутренний аудит модели. При ошибке аудита выполнение прерывается; это поле не является счётчиком найденных и оставленных в расписании ошибок и не подтверждает безопасность реального плавания.

Каждый элемент `trips` содержит `trip_id`, `unit_id`, `barge_id`, `tug_id`, `cargo_id`, `cargo_type`, `origin`, `destination`, `volume_t`, `loading_start`, `departure`, `arrival`, `unloading_end`, `released_at`, `due_date`, `on_time`, `loaded_draft_m`, `data_status`, `source_url` и `events`. `on_time` истинно, если завершение разгрузки не позже срока; `released_at` — возвращение состава и освобождение ресурсов. `loaded_draft_m` указан в метрах. `events` — массив объектов `{direction, segment_id, start, end}`; направление — `upstream` или `downstream`. Внутреннее поле-строка `events_json` наружу не передаётся.

Все даты — ISO-строки без часового пояса, в единой временной базе исходного сценария; адаптер не переводит их в UTC. Пустая строка `source_url` означает отсутствие указанной ссылки. Модель планирует при известном сценарии глубин, а не прогнозирует фактическую обстановку.

Контракт 1.0.0 стабилизирует перечисленные поля и их смысл; совместимые/несовместимые изменения должны сопровождаться согласованием и изменением версии схемы. Результат — JSON-совместимый `dict`; типы и обязательные поля определены в схеме. Ограничения модели и расчётные допущения описаны в `README.md` и `docs/SCENARIO_INPUTS.md`.
