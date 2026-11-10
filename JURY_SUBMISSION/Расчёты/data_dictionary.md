# Словарь данных

CSV имеют UTF-8 с BOM, разделитель запятая, десятичный знак точка. Все массы — метрические тонны, длины маршрута — километры, осадки и ширины — метры, скорости — км/ч относительно берега. Время ISO 8601 без зоны, интерпретируется по Москве. Пустое поле означает UNKNOWN, не ноль. source_url у сценарного параметра может быть пустым: его источник — scenario_config.json.

Статусы: OBSERVED — наблюдение; NORMATIVE — план или норматив; HISTORICAL — справочная/историческая характеристика без подтверждённой доступности 2026; SYNTHETIC — сгенерированное/расчётное значение; ASSUMED — допущение; UNKNOWN — отсутствует. Общий data_status описывает природу записи; у смешанных записей поля дополнительно разъясняются в parameter_provenance.csv. Не считайте наличие URL доказательством всех полей строки.

## routes.csv

| Столбцы | Значение |
|---|---|
| segment_id | Уникальный ключ участка |
| from_node, to_node | Начало и конец по направлению рейса вверх; возврат проходит рёбра в обратном порядке |
| distance_km | Нормативная протяжённость |
| navigation_start, navigation_end | Плановые даты обслуживания обстановки; конец включительно |
| guaranteed_depth_m | Норматив 1,6 м до дноуглубления, не ежедневное измерение |
| guaranteed_after_dredging_m | Условный норматив после работ с 1 сентября |
| channel_width_m, turn_radius_m | Нормативная ширина и радиус закругления; сертификация состава не производится |
| pdf_page, pdf_row, note | Место проверки в исходном PDF и оговорка |
| source_url, data_status | Источник и тип записи |

## vessels.csv и units.csv

| Столбцы | Значение |
|---|---|
| vessel_id | Ключ вымышленного физического ресурса, отдельный для баржи и буксира |
| project | Каталожный проект баржи; у вымышленного буксира UNKNOWN |
| vessel_type | barge или tug; вспомогательные исторические суда в расписании не используются |
| cargo_type_allowed | Разрешённые в модели грузы через символ вертикальной черты; у буксира пусто, он груз сам не перевозит |
| capacity_t | Условный предел полезной загрузки баржи; у буксира 0 |
| draft_empty_m, draft_full_m | Условная осадка при 0 и reference_capacity_t; у буксира постоянная |
| speed_upstream_kmh, speed_downstream_kmh | Скорость состава относительно берега |
| available_from, available_to | Интервал доступности; возврат должен завершиться к available_to |
| unit_id, barge_id, tug_id | Ключ состава и его эксклюзивных ресурсов |
| reference_capacity_t | Базовая полезная нагрузка для линейной кривой осадки; сохраняется при стрессе capacity_t ×0,8 |
| tug_draft_m | Осадка буксира; проверяется наряду с осадкой баржи |
| handling_tph | Условная производительность на каждую операцию, т/ч |
| compatibility_status | ASSUMED_NOT_CERTIFIED: совместимость составов условна |
| source_url, data_status | Сценарный источник и статус; ссылка на каталог не приписана неизвестным осадкам |

## cargo.csv и cargo_plans.csv

| Столбцы | Значение |
|---|---|
| cargo_id | Уникальный ключ синтетической партии |
| cargo_type | coal, crude_oil, food, food_extended — несовместимые с произвольной заменой типы |
| origin, destination | Отправление и доставка; origin в плане пуст, в партии назначен сценарно |
| volume_t | Масса; сумма партий по plan_id равна плановому ориентиру × множитель спроса |
| ready_date, due_date | Сценарная готовность и крайний срок окончания разгрузки |
| priority | Условная приоритетность; применяется после раннего срока |
| plan_id, appendix | Ключ планового ориентира и номер приложения |
| beneficiary_district | Обеспечиваемый район; не заменяет пункт доставки |
| verification, plan_verification | Ограничение проверки исходного плана |
| origin_status, dates_status | Статусы именно этих полей |
| source_url, data_status | Ссылка на план и статус записи; ссылка не подтверждает даты партий |

## conditions.csv

| Столбцы | Значение |
|---|---|
| date, segment_id | Календарная дата и участок; составной уникальный ключ |
| usable_depth_m | Сценарная глубина участка |
| max_allowed_draft_m | Модельное ограничение глубина минус underkeel_m; не официальный допуск |
| is_closed | 1 — проход запрещён моделью, 0 — допускается при прочих проверках |
| restriction_reason, source_url, data_status | Причина, источник, статус |

observed_conditions.csv сохраняет исходные локальные измерения отдельно: record_id — ключ; record_kind — тип; date — дата; segment/location — названия участка/поста/переката; metric — смысл; value/unit — число и единица; source_pdf_page — страница; note — оговорка; verification — способ проверки. water_level в cm_above_gauge_zero не конвертируется в usable_depth_m. draft_restriction_in_force и navigation_aids_closed являются событиями, не численной осадкой или абсолютным запретом.

## actuals.csv и условная декомпозиция

| Столбцы | Значение |
|---|---|
| as_of_date, area_id | Дата среза и охват всей реки |
| checkpoint | planned/shipped/crossed_bar/delivered; четыре самостоятельных показателя |
| tonnage_t | Опубликованная масса после пересчёта из тыс. т; точность источника около 10 т |
| allocation_id, stage | Ключ и стадия условной синтетической декомпозиции |
| ready_day_offset | Вымышленный индекс дня готовности 0–100 для разных разбиений; не применяется в рейсах |
| volume_t, note | Масса и ограничение интерпретации |
| source_url, data_status | Первичный источник/тип записи |

## trips_synthetic.csv и trips_baseline/optimized_heuristic.csv

| Столбцы | Значение |
|---|---|
| trip_id | Ключ только внутри одного расписания; сравнение требует scenario_id и strategy |
| unit_id, barge_id, tug_id | Назначенный состав и ресурсы |
| cargo_id, cargo_type, origin, destination, volume_t | Ссылка на партию, вид груза, узлы и перевезённая часть массы |
| loading_start, departure | Начало погрузки и выход |
| arrival, unloading_end | Прибытие и окончание выгрузки; доставка засчитывается после выгрузки |
| released_at | Возвращение в исходный узел; ресурс освобождён |
| loaded_draft_m | Рассчитанная осадка баржи при данной массе |
| events_json | JSON-массив: direction — направление, segment_id — участок, start/end — границы прохода. Все outbound-события, затем return-события |
| source_url, data_status | SYNTHETIC; не реальный судовой журнал |

## comparison.csv и scenario_manifest.csv

| Столбцы | Значение |
|---|---|
| scenario_id, replicate, external_seed | Ключ варианта, номер реализации и seed внешних входов |
| scenario, depth_variant, timing_variant | Вид стресса, профиль глубин и сдвиг готовности |
| strategy | FIFO baseline либо optimized_heuristic по ранним срокам; не доказанный оптимум |
| delivered_on_time_t | Масса, выгруженная к due_date |
| total_delivered_t, undelivered_t | Вся выгруженная масса и остаток спроса |
| completed_trips | Число завершённых полных оборотов |
| average_delivery_days | Среднее время от ready_date до выгрузки, взвешенное по доставленным тоннам; 0 при отсутствии доставки |
| vessel_idle_days | Сумма свободного времени всех отдельных барж и буксиров; ожидание/округление отправления до следующего дня включено |
| fleet_utilization | Занятое время / доступное время пар; эквивалентно доле отдельных ресурсов, так как каждая пара содержит два ресурса |
| capacity_scale, demand_scale, close | Множители и модельное время закрытия |
| data_status | SYNTHETIC |

## Справочные файлы

vessel_projects.csv: project/vessel_type — проект и тип; deadweight_t — дедвейт (не полезная нагрузка); length_m/beam_m — длина/ширина; draft_empty_m/draft_full_m/confirmed_2026_count — UNKNOWN; note/source_url/data_status — ограничения и происхождение.

historical_vessels.csv: vessel_name — историческое имя; as_of_date — дата публикации; project/capacity_t/draft_empty_m/draft_full_m/confirmed_2026_pilot_count — пустые неизвестные; vessel_type — установленный из текста тип или UNKNOWN; source_url/data_status/note — источник и оговорка.

source_file_hashes.csv: file — имя сохранённого PDF; sha256 — контрольная сумма; provenance — происхождение. parameter_provenance.csv: dataset — таблица; fields — поля через точку с запятой; status — тип данных; note — детализация.

manifest.json: seed — seed; config_sha256 — SHA256 исходного JSON; csv_sha256 — относительный путь → SHA256 каждого сгенерированного CSV. scenario_config.json содержит все параметры источников, графа и демонстрационных распределений; единицы соответствуют одноимённым полям CSV, handling_fixed_hours — часы, close_early_days/due_days_after_ready/timing_variants — дни, depth_variants/underkeel_m/depth_profile — метры.

sensitivity_results.csv использует метрики comparison.csv, replicate и data_status. variant — демонстрационный сдвиг параметра согласно sensitivity.py. Файл находится вне data, чтобы отделить основной воспроизводимый набор от дополнительного анализа.
