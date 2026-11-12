"""Синтетический сетевой сценарий «Северный завоз по Якутии» для ядра network 3.0.0.

Граф строится из геоосновы (yakutia/geo/yakutia_geo.json): каждый участок
даёт два направленных ребра. Календарь глубин, флот, заявки и тарифы —
СИНТЕТИЧЕСКИЕ, подобраны правдоподобно для демонстрации механики сети:
перегон судов между реками через море, обратная загрузка, общий флот.

    python yakutia/build_scenario.py            # data/yakutia/*.json

Выход:
  data/yakutia/yakutia_network.json  — вход ядра (формат data/network_demo)
  data/yakutia/limits.json           — пределы поиска для сезона
  data/yakutia/tariffs_demo.json     — тарифы-заглушки для финансового блока
  data/yakutia/fleet_cards.json      — карточки судов (поля под реальные данные)
"""

import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO = ROOT / 'yakutia' / 'geo' / 'yakutia_geo.json'
OUT = ROOT / 'data' / 'yakutia'
YEAR = 2026
START, END = date(YEAR, 5, 25), date(YEAR, 10, 15)


def d(m, day):
    return date(YEAR, m, day)


# Режим участка: окно навигации и глубина по опорным датам (м).
# Источник: синтетика по качественному описанию гидрологии (подъём в половодье,
# межень в августе—сентябре, мелкие участки Яны и Вилюя, ледовое окно моря).
REGIME = {
    'Лена-верх': {'open': (d(5, 25), d(10, 5)), 'depth': [(d(5, 25), 3.2), (d(7, 1), 2.6), (d(8, 20), 1.9), (d(10, 5), 2.1)]},
    'Лена': {'open': (d(6, 1), d(10, 10)), 'depth': [(d(6, 1), 4.0), (d(7, 15), 3.2), (d(9, 1), 2.6), (d(10, 10), 2.7)]},
    'Лена-низ': {'open': (d(6, 15), d(10, 1)), 'depth': [(d(6, 15), 4.0), (d(8, 15), 3.0), (d(10, 1), 2.8)]},
    'Алдан': {'open': (d(6, 1), d(10, 1)), 'depth': [(d(6, 1), 3.0), (d(7, 20), 2.2), (d(9, 1), 1.7), (d(10, 1), 1.9)]},
    'Вилюй': {'open': (d(6, 1), d(9, 25)), 'depth': [(d(6, 1), 2.6), (d(7, 10), 1.8), (d(8, 20), 1.3), (d(9, 25), 1.5)]},
    'Яна': {'open': (d(6, 10), d(9, 30)), 'depth': [(d(6, 10), 1.8), (d(7, 15), 1.5), (d(8, 20), 1.1), (d(9, 30), 0.95)]},
    'Индигирка': {'open': (d(6, 10), d(9, 30)), 'depth': [(d(6, 10), 2.6), (d(7, 20), 2.1), (d(9, 1), 1.6), (d(9, 30), 1.6)]},
    'Колыма': {'open': (d(6, 5), d(10, 1)), 'depth': [(d(6, 5), 3.0), (d(7, 20), 2.5), (d(9, 1), 2.0), (d(10, 1), 2.1)]},
    'дельта': {'open': (d(7, 1), d(9, 30)), 'depth': [(d(7, 1), 3.0), (d(9, 30), 2.6)]},
    'море': {'open': (d(7, 20), d(9, 25)), 'depth': [(d(7, 20), 6.0), (d(9, 25), 6.0)]},
}
UPPER_LENA = {'Осетрово', 'Киренск', 'Ленск'}
LOWER_LENA = {'Жиганск', 'Кюсюр'}


def regime_of(link):
    if link['kind'] == 'sea':
        return 'море'
    if link['kind'] == 'delta':
        return 'дельта'
    if link['river'] == 'Лена':
        if link['a'] in UPPER_LENA and link['b'] in UPPER_LENA | {'Олёкминск'}:
            return 'Лена-верх'
        if link['b'] in LOWER_LENA:
            return 'Лена-низ'
        return 'Лена'
    return link['river']


def depth_on(points, day):
    if day <= points[0][0]:
        return points[0][1]
    for (d0, v0), (d1, v1) in zip(points, points[1:]):
        if d0 <= day <= d1:
            return round(v0 + (v1 - v0) * (day - d0).days / max(1, (d1 - d0).days), 2)
    return points[-1][1]


def edges(geo):
    out = []
    for link in geo['links']:
        reg = REGIME[regime_of(link)]
        opened, closed = reg['open']
        daily = {}
        day = START
        while day <= END:
            if day < opened or day > closed:
                daily[day.isoformat()] = {'is_closed': True}
            else:
                depth = depth_on(reg['depth'], day)
                daily[day.isoformat()] = {'is_closed': False, 'usable_depth_m': depth,
                                          'max_allowed_draft_m': round(depth - 0.2, 2)}
            day += timedelta(days=1)
        base = {'is_closed': True, 'usable_depth_m': 0.0, 'max_allowed_draft_m': 0.0}
        pairs = [(link['a'], link['b']), (link['b'], link['a'])]
        for origin, dest in pairs:
            direction = 'downstream' if link.get('down') == dest else 'upstream'
            out.append({'edge_id': f'{origin}→{dest}', 'origin_port': origin, 'destination_port': dest,
                        'distance_km': link['km'], 'direction': direction, 'condition': base,
                        'daily_conditions': daily})
    return out


# Флот: тип → (осадка порожнем, скорость вверх/вниз), класс для карточки.
TUG_TYPES = {
    'river': {'draft_empty_m': 0.9, 'speed_upstream_kmh': 9, 'speed_downstream_kmh': 14,
              'class': 'речной толкач (класс «О»)', 'power_hp': 800, 'sea_allowed': False},
    'shallow': {'draft_empty_m': 0.6, 'speed_upstream_kmh': 8, 'speed_downstream_kmh': 12,
                'class': 'мелкосидящий толкач (класс «Р»)', 'power_hp': 450, 'sea_allowed': False},
    'river_sea': {'draft_empty_m': 1.6, 'speed_upstream_kmh': 10, 'speed_downstream_kmh': 13,
                  'class': 'буксир-толкач река—море (класс «М-СП»)', 'power_hp': 1320, 'sea_allowed': True},
}
BARGE_TYPES = {
    'dry2000': {'capacity_t': 2000, 'draft_empty_m': 0.5, 'draft_full_m': 2.4, 'handling_tph': 250,
                'cargo_types_allowed': ['coal', 'general', 'food'], 'class': 'сухогрузная баржа-площадка'},
    'oil1500': {'capacity_t': 1500, 'draft_empty_m': 0.5, 'draft_full_m': 2.3, 'handling_tph': 300,
                'cargo_types_allowed': ['fuel'], 'class': 'нефтеналивная баржа'},
    'dry600': {'capacity_t': 600, 'draft_empty_m': 0.35, 'draft_full_m': 1.05, 'handling_tph': 120,
               'cargo_types_allowed': ['coal', 'general', 'food', 'concentrate'], 'class': 'мелкосидящая баржа'},
    'oil500': {'capacity_t': 500, 'draft_empty_m': 0.35, 'draft_full_m': 1.0, 'handling_tph': 150,
               'cargo_types_allowed': ['fuel'], 'class': 'мелкосидящий танкер-площадка'},
    'sea1200': {'capacity_t': 1200, 'draft_empty_m': 0.8, 'draft_full_m': 2.5, 'handling_tph': 200,
                'cargo_types_allowed': ['coal', 'general', 'food', 'fuel', 'concentrate'],
                'class': 'баржа река—море'},
}
# (id, тип, порт базирования)
TUGS = [('Т-101', 'river', 'Осетрово'), ('Т-102', 'river', 'Осетрово'), ('Т-103', 'river', 'Осетрово'),
        ('Т-201', 'river', 'Якутск'), ('Т-202', 'shallow', 'Якутск'),
        ('М-301', 'river_sea', 'Тикси'), ('М-302', 'river_sea', 'Тикси'),
        ('Я-401', 'shallow', 'Нижнеянск'), ('К-501', 'river', 'Зелёный Мыс')]
BARGES = [('Б-11', 'dry2000', 'Осетрово'), ('Б-12', 'dry2000', 'Осетрово'), ('Н-13', 'oil1500', 'Осетрово'),
          ('Н-14', 'oil1500', 'Осетрово'),
          ('Б-21', 'dry600', 'Якутск'), ('Н-22', 'oil500', 'Якутск'), ('Б-23', 'dry2000', 'Якутск'),
          ('М-31', 'sea1200', 'Тикси'), ('М-32', 'sea1200', 'Тикси'),
          ('Б-41', 'dry600', 'Нижнеянск'), ('Б-51', 'dry600', 'Зелёный Мыс')]

# Заявки: (id, тип, откуда, куда, т, готовность, срок, критическая категория)
CARGO = [
    ('Z01', 'fuel', 'Осетрово', 'Якутск', 4500, d(5, 25), d(7, 20), None),
    ('Z02', 'fuel', 'Осетрово', 'Ленск', 1500, d(5, 25), d(7, 1), None),
    ('Z03', 'general', 'Осетрово', 'Олёкминск', 1500, d(5, 25), d(7, 25), None),
    ('Z04', 'food', 'Осетрово', 'Якутск', 1200, d(5, 25), d(7, 10), 'food'),
    ('Z05', 'general', 'Якутск', 'Хандыга', 1200, d(6, 1), d(8, 15), None),
    ('Z06', 'fuel', 'Якутск', 'Вилюйск', 500, d(6, 1), d(7, 31), None),
    ('Z07', 'fuel', 'Якутск', 'Жиганск', 500, d(6, 10), d(8, 20), None),
    ('Z08', 'coal', 'Сангар', 'Якутск', 2000, d(6, 5), d(9, 10), None),
    ('Z09', 'general', 'Якутск', 'Тикси', 2000, d(6, 20), d(8, 10), None),
    ('Z10', 'coal', 'Тикси', 'Нижнеянск', 1200, d(7, 20), d(8, 31), None),
    ('Z11', 'food', 'Тикси', 'Усть-Куйга', 300, d(7, 20), d(8, 20), 'food'),
    ('Z12', 'fuel', 'Тикси', 'Чокурдах', 1200, d(7, 20), d(9, 5), None),
    ('Z13', 'general', 'Тикси', 'Зелёный Мыс', 1000, d(7, 20), d(9, 15), None),
    ('Z14', 'general', 'Зелёный Мыс', 'Среднеколымск', 500, d(6, 10), d(8, 20), None),
    ('Z15', 'concentrate', 'Усть-Куйга', 'Тикси', 600, d(8, 1), d(9, 20), None),
    ('Z16', 'general', 'Нижнеянск', 'Батагай', 400, d(6, 15), d(8, 1), None),
    ('Z17', 'food', 'Якутск', 'Хандыга', 300, d(6, 1), d(7, 15), 'food'),
]
CARGO_RU = {'fuel': 'топливо', 'coal': 'уголь', 'general': 'генгруз', 'food': 'продовольствие',
            'concentrate': 'концентрат (обратный груз)'}

# Тарифы-заглушки: ставки ядра network.financial. Значения условные.
TARIFFS = {'currency': 'RUB', 'source_status': 'DEMO_PLACEHOLDER — условные ставки до финансового ядра',
           'rates': {'loaded_km': 180, 'empty_km': 120, 'loading_hour': 6000, 'unloading_hour': 6000,
                     'waiting_hour': 2500, 'tug_busy_hour': 9000, 'barge_busy_hour': 1500}}

LIMITS = {'beam_width': 4, 'candidate_limit': 8, 'max_actions': 80, 'max_states': 120,
          'max_route_expansions': 150, 'max_hops': 14, 'max_wait_days': 20, 'route_options': 2,
          'repair_attempts': 1, 'repair_states': 10}


def iso(day, end=False):
    return f'{day.isoformat()}T{"23:59:59" if end else "00:00:00"}'


def build():
    geo = json.loads(GEO.read_text(encoding='utf-8'))
    ports = [p['id'] for p in geo['ports']]
    start, end = iso(START), iso(END, True)
    tugs = [{'vessel_id': v, 'current_port': port, 'available_at': start, 'available_to': end,
             **{k: TUG_TYPES[t][k] for k in ('draft_empty_m', 'speed_upstream_kmh', 'speed_downstream_kmh')}}
            for v, t, port in TUGS]
    barges = [{'vessel_id': v, 'current_port': port, 'available_at': start, 'available_to': end,
               **{k: BARGE_TYPES[t][k] for k in ('capacity_t', 'draft_empty_m', 'draft_full_m', 'handling_tph',
                                                  'cargo_types_allowed')}}
              for v, t, port in BARGES]
    tug_kind = {v: t for v, t, _ in TUGS}
    pairs = []
    for t, tk, _ in TUGS:
        for b, bk, _ in BARGES:
            sea_tug, sea_barge = tk == 'river_sea', bk == 'sea1200'
            shallow_ok = tk != 'shallow' or bk in ('dry600', 'oil500')
            if sea_tug == sea_barge and shallow_ok:
                pairs.append({'tug_id': t, 'barge_id': b})
    cargo = [{'cargo_id': cid, 'cargo_type': ct, 'origin_port': o, 'destination_port': dst, 'volume_t': t,
              'ready_at': iso(r), 'due_at': iso(due, True), 'priority': 1 if crit else 0,
              'critical_category': crit, 'data_status': 'SYNTHETIC'}
             for cid, ct, o, dst, t, r, due, crit in CARGO]
    scenario = {'scenario_id': 'yakutia_2026', 'data_status': 'SYNTHETIC', 'start': start, 'end': end,
                'ports': ports, 'underkeel_m': 0.2, 'fixed_handling_seconds': 7200,
                'edges': edges(geo), 'tugs': tugs, 'barges': barges, 'compatible_pairs': pairs,
                'social_requirements': [], 'cargo': cargo}
    cards = []
    for v, t, port in TUGS:
        spec = TUG_TYPES[t]
        cards.append({'vessel_id': v, 'kind': 'tug', 'class': spec['class'], 'home_port': port,
                      'power_hp': spec['power_hp'], 'draft_empty_m': spec['draft_empty_m'],
                      'speed_upstream_kmh': spec['speed_upstream_kmh'],
                      'speed_downstream_kmh': spec['speed_downstream_kmh'], 'sea_allowed': spec['sea_allowed'],
                      'project': None, 'built': None, 'operator': None, 'registry_id': None,
                      'source': 'SYNTHETIC — поля project/built/operator/registry_id ждут данных реестра'})
    for v, t, port in BARGES:
        spec = BARGE_TYPES[t]
        cards.append({'vessel_id': v, 'kind': 'barge', 'class': spec['class'], 'home_port': port,
                      'capacity_t': spec['capacity_t'], 'draft_empty_m': spec['draft_empty_m'],
                      'draft_full_m': spec['draft_full_m'], 'handling_tph': spec['handling_tph'],
                      'cargo_types_allowed': spec['cargo_types_allowed'], 'sea_allowed': t == 'sea1200',
                      'project': None, 'built': None, 'operator': None, 'registry_id': None,
                      'source': 'SYNTHETIC — поля project/built/operator/registry_id ждут данных реестра'})
    return scenario, cards, tug_kind


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    scenario, cards, _ = build()

    def dump(name, data):
        (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')

    dump('yakutia_network.json', scenario)
    dump('limits.json', LIMITS)
    dump('tariffs_demo.json', TARIFFS)
    dump('fleet_cards.json', {'data_status': 'SYNTHETIC', 'cargo_types_ru': CARGO_RU, 'vessels': cards})
    print(f"портов {len(scenario['ports'])}, рёбер {len(scenario['edges'])}, буксиров {len(scenario['tugs'])}, "
          f"барж {len(scenario['barges'])}, пар {len(scenario['compatible_pairs'])}, заявок {len(scenario['cargo'])}, "
          f"спрос {sum(c['volume_t'] for c in scenario['cargo'])} т")


if __name__ == '__main__':
    main()
