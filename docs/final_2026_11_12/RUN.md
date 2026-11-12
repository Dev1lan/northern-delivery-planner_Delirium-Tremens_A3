# Воспроизведение расчётов 2.2.0

Используйте CPython 3.12.14 и зависимости `requirements.txt`. Команды ниже выполняются из корня Python-репозитория. Укажите новый путь вывода при повторном запуске.

```powershell
python -m unittest discover -s tests -p 'test*.py'
python -m experiments.demo_check
python -m experiments.run_comparison --examples --output results/final_2026_11_12/examples
python -m experiments.run_comparison --archive-root 'JURY_SUBMISSION/Расчёты' --requirements data/demo/social_requirements.json --verify-history --workers 4 --output results/final_2026_11_12/archive
python -m experiments.fleet_analysis --scenario 'JURY_SUBMISSION/Расчёты/data/scenarios/r1_baseline_central_central' --assumptions data/demo/fleet_assumptions.json --requirements data/demo/social_requirements.json --workers 4 --output results/final_2026_11_12/fleet
```

Передайте один или несколько `--scenario <папка CSV>` для произвольной выборки и повторяйте `--strategy` для выбора стратегий. `--revision` позволяет указать Git-коммит при запуске. Манифест в любом случае содержит версию алгоритма, Python и SHA-256 исходников и снимков. Для точного повтора используйте `--snapshot-root <каталог прежнего запуска>` с новым `--output`. Проверка исторических результатов сравнивает полные объекты, а не только итоговые тонны. `summary.json` создаётся после успешного завершения всех сценариев.

## JSON для расчётных потребителей

```powershell
python result_adapter.py --scenario data/demo/scenario --strategy SOCIAL_PRIORITY --contract-version 2.0.0 --requirements data/demo/social_requirements.json
```

По умолчанию адаптер отдаёт прежний v1 для FCFS, EDF (`EARLIEST_DUE`) и OPTIMIZED. Новый результат проверяется по `result.v2.schema.json`. `input_fingerprint` хэширует фактические заявки, конфигурацию, календарь и составы флота. `requirements_fingerprint` хэширует проверенные минимумы. Сравнивайте одинаковые отпечатки внутри одного сценария.

`schedule_audit` подтверждает физическую проверку. `requirements_status` относится к выполнению минимумов в этом плане. `feasibility_status` показывает, удалось ли получить план, выполняющий все минимумы (`FEASIBLE`), доказать невозможность (`INFEASIBLE`), или поиск оставил дефицит без доказательства (`UNKNOWN`). Без требований возвращается `NOT_CHECKED`. Ошибка входа или аудита прерывает расчёт. Статус `UNKNOWN` относится к результату конкретной стратегии. Другой план на тех же входах может дать свидетельство выполнимости.

Минимумы задаются JSON-списком, см. `data/demo/social_requirements.json`. Категории групп не пересекаются. Тонны задаются с точностью 0,001 т. `basis=delivered_by_deadline` использует `deadline` и окончание разгрузки. Для сезонного `delivered_total` задайте `deadline: null`. `deficit_t` относится к представленному плану. `deficit_lower_bound_t` — доказанная нижняя граница по оптимистичной оценке доступного тоннажа. `selected_plan_strategy` указывает, какой вариант выбран при сравнении с прежними стратегиями.

## Экспорт для финансовой модели

Сценарный анализ пишет `finance/<вариант>--<стратегия>.json`. Для отдельного расписания используйте:

```powershell
python -m finance_export --input results/final_2026_11_12/fleet/inputs/PLUS_UNIT.json --result results/final_2026_11_12/fleet/scenarios/PLUS_UNIT.json --strategy SOCIAL_PRIORITY --output finance_plus_unit.json
```

Экспорт сверяет отпечаток и повторяет физический аудит. В нём есть груз по категориям, рейсы, километры в грузу и порожнём, секунды погрузки, разгрузки и занятости каждого физического ресурса, своевременный тоннаж, недовоз и список невыполненных минимумов. Для стоимостной оценки передайте `--tariffs` в экспорт или расчёт флота. Формат содержит `currency` и `rates`. Поддерживаются ключи `loaded_km`, `empty_km`, `loading_hour`, `unloading_hour`, `barge_hour`, `tug_hour`, `trip`. Включайте только согласованные тарифы и проверяйте, что составляющие расходов не дублируются. Суммы округляются до сотых на уровне тарифной статьи, а сценарий флота показывает разницу со своим исходным вариантом. Без тарифов экспорт содержит только физические величины.

## Дашборд

В отдельной ветке `final/a3-v2` откройте вкладку «Социальные минимумы». В неё уже экспортированы проверочные и флотские сценарии. Для другого набора выполните `python tools/export_v2.py --run ID "Название группы" <каталог прогона> --output <новая папка>` в React-репозитории и укажите подготовленные данные в `public/data/v2`. Прежняя вкладка сравнения продолжает читать v1. Вся демонстрация работает на готовых JSON.
