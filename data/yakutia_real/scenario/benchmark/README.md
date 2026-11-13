# Бенчмарк ядра: реальная сеть Якутии

Три сценария формата ядра 3.0.0 (`network.adapter.run_scenario`), собранные из открытых данных (`data/yakutia_real`). Реки, длины, гарантированные глубины, окна навигации и заявки реальные; флот — допущение по проектам ЛОРП. Лимиты поиска — `limits.json` (как у `data/yakutia/limits.json`, но `max_wait_days = 14`).

| Файл | Буксиров | Барж | Пар | Заявок | Тонн |
|---|---|---|---|---|---|
| `yakutia_real_full.json` | 52 | 93 | 874 | 93 | 729 895 |
| `yakutia_real_reduced.json` | 52 | 93 | 874 | 62 | 399 887 |
| `yakutia_real_small.json` | 16 | 29 | 41 | 62 | 399 887 |

`reduced` — одинаковые «откуда — куда — груз» объединены, промышленный транзит Усть-Кут → Ленск/Витим убран. Сеть у всех одна: 36 портов, 70 рёбер, окно сценария 10.05–31.10.2026. Цель — `NETWORK_REPOSITION` за 1–2 минуты на `reduced`.

## Запуск

```python
import json, time
from network.adapter import run_scenario, audit_result
from network.models import Limits

raw = json.load(open('data/yakutia_real/scenario/benchmark/yakutia_real_small.json'))
limits = Limits(**json.load(open('data/yakutia_real/scenario/benchmark/limits.json')))
t = time.time()
plan = run_scenario(raw, 'NETWORK_REPOSITION', limits=limits)
print(time.time() - t, plan['termination_reason'], plan['metrics']['completed_voyages'],
      audit_result(plan)['schedule_audit']['status'])
```

## Замеры (MacBook, Python 3.14, без кэша)

| Сценарий | Стратегия | Время | Итог |
|---|---|---|---|
| `small` | NETWORK_REPOSITION | 159 с | SEARCH_LIMIT, 31 рейс, 37 189 т |
| `small`, первые 17 заявок по объёму | NETWORK_REPOSITION | 15 с | 11 рейсов, 661 т |
| `reduced` | NETWORK_REPOSITION | > 4 мин | — |
| `full` | NETWORK_FCFS / NETWORK_GREEDY | 3 / 7 с | 8 рейсов (флот без перегона не доходит до груза) |
| первая версия `full`: без `navigation_windows`, флот доступен с 10.05, 2147 пар | NETWORK_REPOSITION | 17 725 с (4 ч 55 мин) | SEARCH_LIMIT, 31 рейс, 30 396 т |

## Где уходит время (cProfile)

Почти всё — поиск маршрутов `network/routing.py`:

- `paths` — на 62 заявки и 41 пару около 830 тыс. вызовов, из них уникальных по входу (порт, порт, время готовности, срок, осадка, скорость буксира) ~15 тыс.
- `_earliest_leg` — подбор дня выхода на плечо, перебирает дни до `max_wait_days`.
- `passage_open` → `generate.check_passage` — проверка по каждому дню перехода с `isoformat`/`fromisoformat` на каждом шаге: ~31 млн вызовов, уникальных ~116 тыс. 71% проверок — отказ (осадка больше допустимой при постоянной гарантированной глубине: ждать следующего дня бессмысленно, но ядро пробует до `max_wait_days`).

## Итог

Ядро ускорено в ветке `perf/network-core-optimization` (см. `docs/NETWORK_PERFORMANCE.md`), влито в `future/llm`. Временный кэш на нашей стороне (`yakutia/core_speedup.py`) удалён: ядро кэширует то же самое внутри.

Замер на ускоренном ядре (этот Mac, Python 3.14):

| Сценарий | Политика | Лимиты | «С перегоном» | Итог |
|---|---|---|---|---|
| `reduced` | без политики | `limits.json` из этой папки | 3,3 с | 31 рейс, 30 396 т |
| `full` | DEMO, 8 пар, 64 проверки | те же | 5,1 с | 31 рейс, 46 760 т |
| `full` | STANDARD, 8 пар, 64 проверки | `max_actions` 1000, `max_states` 1200 | 32,5 с | 233 рейса, 150 668 т |

Потолок в 31 рейс давал `max_states = 120`: при ширине луча 4 это около 30 уровней поиска. Лимиты сайта — `data/yakutia_real/scenario/limits.json` и `search_policy.json`.
