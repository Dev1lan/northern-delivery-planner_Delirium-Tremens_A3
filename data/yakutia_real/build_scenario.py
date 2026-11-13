"""Сетевой сценарий ядра 3.0.0 из реальных таблиц data/yakutia_real.

Из таблиц берётся то, что реально: порты, рёбра, длины, гарантированные глубины и окна
навигации (норматив АТ-442-р), заявки с парами «откуда — куда» и тоннажем (распоряжение
№ 326-р), проекты судов и места их зимовки (ЛОРП). Чего в открытых данных нет — состав
флота по рекам, скорости, осадки, сроки заявок — задаётся допущениями ниже, и каждая
запись несёт поле provenance (REAL / DERIVED / ASSUMED) и source.

Режимы:
  demo   — сценарий помечен SYNTHETIC (так ядро допускает морские переходы по допущению
           сценария). Нужен, чтобы посчитать план целиком.
  strict — сценарий MIXED: реальные записи помечены REAL с источником, флот — SYNTHETIC.
           Ядро не пустит в море суда без подтверждённого допуска; это честно показывает,
           каких данных не хватает.

    python data/yakutia_real/build_scenario.py [--mode demo|strict] [--fleet-scale 1.0]
"""
import argparse
import csv
import heapq
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / 'scenario'
YEAR = 2026
START, END = date(YEAR, 5, 10), date(YEAR, 10, 31)
NORM = 'Росморречфлот, распоряжение № АТ-442-р от 30.12.2025, приложение (навигация 2026)'
PLAN = 'Распоряжение Правительства РС(Я) № 326-р от 15.05.2026'
LORP = 'Каталог флота ПАО «ЛОРП», lorp.ru/fleet; «Ленский водник» № 1 от 26.01.2026'

# Пункты плана 326-р → порт сети. Посёлки без своего порта в сети привязаны к ближайшему порту
# на той же реке (ASSUMED). None — пункт вне сети, груз в сценарий не идёт (видно в отчёте).
POINT = {
    'Угольный разрез Джебарики-Хая': 'Джебарики-Хая', 'Зырянский угольный разрез': 'Зырянка',
    'Сангарский угольный разрез': 'Сангар', 'Усть-Кут': 'Осетрово', 'Нижний Бестях, Ленск': 'Нижний Бестях',
    'Олекминск/Ленск/Жатай': 'Ленск', 'Черский': 'Зелёный Мыс', 'Олекминск': 'Олёкминск',
    'Витим, Ленск': 'Ленск', 'Сангар, Батамай': 'Сангар', 'Кюсюр, Сиктях': 'Кюсюр',
    'Кюпцы': 'Усть-Мая', 'Эжанцы': 'Усть-Мая', 'Белькачи': 'Усть-Мая', 'Усть-Миль': 'Усть-Мая',
    'Чагда': 'Усть-Мая', 'Кутана': 'Усть-Мая', 'Крест-Хальджай': 'Хандыга',
    'Куберганя': 'Белая Гора', 'Русское Устье': 'Чокурдах', 'Оленегорск': 'Чокурдах',
    'Кыстатыам': 'Жиганск', 'Бестях': 'Жиганск', 'Чапаево': 'Олёкминск', 'Мача': 'Олёкминск',
    'Токко': 'Олёкминск', 'Саныяхтах': 'Олёкминск', 'Батамай': 'Сангар', 'Дыгдал': 'Устье Алдана',
    'Хоринцы': 'Верхневилюйск', 'Кресты': 'Нижнеянск', 'Найба': 'Тикси', 'Таймылыр': None,
    'Амга': None, 'Юрюнг-Хая': None,
}
EXACT_NAMES = {'Олекминск', 'Черский', 'Витим, Ленск', 'Сангар, Батамай', 'Кюсюр, Сиктях', 'Усть-Кут',
               'Угольный разрез Джебарики-Хая', 'Зырянский угольный разрез', 'Сангарский угольный разрез',
               'Нижний Бестях, Ленск', 'Олекминск/Ленск/Жатай'}
FOOD_ORIGIN = 'Нижний Бестях'   # пункт отгрузки продовольствия в 326-р не указан — допущение
CARGO_TYPES = {'coal': 'dry', 'general': 'dry', 'food': 'dry', 'fuel': 'liquid', 'crude_oil': 'liquid',
               'gas_condensate': 'liquid'}
CRITICAL = {'food': 'food', 'fuel': 'fuel'}

# Флот (ASSUMED по составу, REAL по проектам). Транспортный флот ЛОРП летом 2026 — 130 единиц
# («Ленский водник» № 7); распределён по местам зимовки пропорционально («Ленский водник» № 1).
# Колыма — флот Колымской судоходной компании, данных нет: допущение.
# (база, река, [(проект буксира, шт)], [(проект баржи, шт)])
FLEET = [
    ('Осетрово', 'Лена', [('Р-33', 3)], [('МП1100', 3), ('81631', 3)]),
    ('Пеледуй', 'Лена', [('Р-33', 6), ('1741А', 4)], [('МП1100', 6), ('942', 4), ('81631', 6), ('16800Н1', 2)]),
    ('Олёкминск', 'Лена', [('Р-33', 2)], [('942', 2), ('81631', 2)]),
    ('Жатай', 'Лена', [('Р-33', 10), ('428.1', 4), ('1741А', 6)], [('МП1100', 10), ('942', 8), ('16800', 4), ('81631', 8), ('16800Н1', 4)]),
    ('Хандыга', 'Алдан', [('911Л', 2)], [('81216', 2), ('81462', 1)]),
    ('Усть-Куйга', 'Яна', [('Р14А', 2)], [('81216', 3), ('81462', 1)]),
    ('Нижнеянск', 'Яна', [('Р14А', 2), ('911Л', 1)], [('81216', 3), ('81462', 2)]),
    ('Белая Гора', 'Индигирка', [('Р14А', 3)], [('81216', 3), ('81462', 2)]),
    ('Чокурдах', 'Индигирка', [('911Л', 1)], [('81216', 1), ('81462', 1)]),
    ('Зырянка', 'Колыма', [('Р-33', 2)], [('942', 3), ('81631', 1)]),
    ('Зелёный Мыс', 'Колыма', [('Р-33', 2)], [('942', 2), ('81631', 2)]),
    ('Тикси', 'море Лаптевых', [('Р-153', 2)], [('16800', 3), ('16800Н1', 1)]),
]
# Допущения по проектам, которых нет в каталоге: скорость состава, осадка порожнем, осадка
# буксира, производительность обработки. Осадка в грузу — высота борта минус надводный борт
# (vessel_projects.csv), а где надводного борта нет — 0,85 высоты борта.
TUG_SPEED = {'Р-33': (10, 15), '1741А': (10, 15), '428.1': (11, 16), '911Л': (9, 14), 'Р14А': (9, 13), 'Р-153': (11, 15)}
BARGE_HANDLING = {'dry': 250, 'liquid': 300}
RIVER_SEA_TUGS = {'Р-153'}
RIVER_SEA_BARGES = {'16800'}


def read(name):
    with open(HERE / name, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def num(v):
    """Число из ячейки; для диапазона «2,8–3,3» — нижняя граница."""
    s = str(v).replace(',', '.').split('–')[0].strip()
    try:
        return float(s)
    except ValueError:
        return None


def projects():
    out = {}
    for r in read('vessel_projects.csv'):
        key = r['project'].split(' ')[0]
        depth, free = num(r['depth_m']), num(r['freeboard_m'])
        full = num(r['draft_full_est_m'])
        if full is None and depth is not None:
            full = round(depth * 0.85, 2)
        out[key] = dict(r, depth=depth, freeboard=free, draft_full=full, dwt=num(r['deadweight_t']))
    return out


def iso(d, end=False):
    return f'{d.isoformat()}T{"23:59:59" if end else "00:00:00"}'


def build(mode='demo', fleet_scale=1.0, pairing='base', merge=True, include_transit=True):
    ports = read('ports.csv')
    edges_t = read('edges.csv')
    cargo_t = read('cargo_plan_2026.csv')
    proj = projects()
    real = mode == 'strict'
    status_real = 'REAL' if real else 'SYNTHETIC'

    def prov(kind, source, extra=None):
        row = {'data_status': status_real if kind != 'ASSUMED' else 'SYNTHETIC', 'provenance': kind, 'source': source}
        return {**row, **(extra or {})}

    port_ids = [p['port'] for p in ports]
    # Рёбра в обе стороны, календарь по дням: закрыто вне окна, глубина = гарантированная.
    edges = []
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    for e in edges_t:
        o, c = date.fromisoformat(e['open_2026']), date.fromisoformat(e['close_2026'])
        sea = e['water_type'] == 'SEA'
        depth = num(e['min_guaranteed_depth_m']) or 4.0   # море: глубина не ограничивает речные составы
        daily = {}
        for d in days:
            if d < o or d > c:
                daily[d.isoformat()] = {'is_closed': True}
            else:
                dep = depth
                rows = e['norm_rows'].split(';')
                if ('1399' in rows or '1400' in rows) and d < date(YEAR, 9, 1):
                    dep = min(dep, 1.6)       # прим. 16: Яна до дноуглубления
                if '1407' in rows and d < date(YEAR, 9, 1):
                    dep = min(dep, 1.3)       # прим. 17: бар Индигирки
                daily[d.isoformat()] = {'is_closed': False, 'usable_depth_m': dep, 'max_allowed_draft_m': round(dep - 0.2, 2)}
        for a, b, direction in ((e['origin_port'], e['destination_port'], 'downstream'),
                                (e['destination_port'], e['origin_port'], 'upstream')):
            row = {'edge_id': f'{a}→{b}', 'origin_port': a, 'destination_port': b,
                   'distance_km': float(e['distance_km']), 'direction': direction if not sea else 'downstream',
                   'condition': {'is_closed': True, 'usable_depth_m': 0.0, 'max_allowed_draft_m': 0.0},
                   'daily_conditions': daily,
                   # окно навигации явно: без него ядро на закрытом плече перебирает каждый день выхода
                   'navigation_windows': [{'start': f'{o.isoformat()}T00:00:00', 'end': f'{c.isoformat()}T23:59:59'}],
                   **prov('ASSUMED' if e['status'] == 'ASSUMED' else e['status'] if not real else 'REAL',
                          f'{NORM}, строки {e["norm_rows"]}' if e['norm_rows'] else 'морской переход: оценка по координатам')}
            if real and e['status'] == 'DERIVED':
                row['provenance'] = 'DERIVED'
            if sea:
                row.update(water_type='SEA', navigation_area='ARCTIC_COAST')
            edges.append(row)

    # Флот. Судно доступно с открытия навигации у своей базы (до этого оно во льду) — допущение
    # по нормативу: самое раннее открытие среди участков, примыкающих к порту базы.
    base_open = {}
    for e in edges_t:
        for pt in (e['origin_port'], e['destination_port']):
            base_open[pt] = min(base_open.get(pt, e['open_2026']), e['open_2026'])
    tugs, barges, cards = [], [], []
    end = iso(END, True)
    groups = []
    for base, river, tlist, blist in FLEET:
        gt, gb = [], []
        start = f'{base_open.get(base, START.isoformat())}T00:00:00'
        for pname, n in tlist:
            for i in range(max(1, round(n * fleet_scale))):
                p = proj[pname]
                vid = f'Б{len(tugs) + 1:03d}-{pname}'
                up, down = TUG_SPEED[pname]
                draft = round(p['depth'] - p['freeboard'], 2) if p['depth'] and p['freeboard'] else 1.2
                sea = pname in RIVER_SEA_TUGS
                row = {'vessel_id': vid, 'current_port': base, 'available_at': start, 'available_to': end,
                       'draft_empty_m': draft, 'speed_upstream_kmh': up, 'speed_downstream_kmh': down,
                       **prov('ASSUMED', f'{LORP}; экземпляр и базирование — допущение; скорость — допущение')}
                if sea:
                    row.update(vessel_class=p['class_rs'], navigation_permits=[{'area': 'ARCTIC_COAST',
                               'status': 'SCENARIO_ASSUMPTION' if not real else 'UNVERIFIED',
                               'source': f'класс Регистра проекта {pname} по каталогу ЛОРП; допуск не подтверждён'}])
                tugs.append(row); gt.append(vid)
                cards.append({'vessel_id': vid, 'kind': 'tug', 'class': f'буксир проекта {pname}, {p["class_rs"]}',
                              'home_port': base, 'power_hp': None, 'draft_empty_m': draft,
                              'speed_upstream_kmh': up, 'speed_downstream_kmh': down, 'sea_allowed': sea,
                              'project': pname, 'built': None, 'operator': 'ЛОРП' if river != 'Колыма' else None,
                              'registry_id': None, 'source': 'проект — каталог ЛОРП; экземпляр — допущение'})
        for pname, n in blist:
            for i in range(max(1, round(n * fleet_scale))):
                p = proj[pname]
                vid = f'Р{len(barges) + 1:03d}-{pname}'
                liquid = p['cargo'] == 'liquid'
                cap = p['dwt']
                types = [t for t, k in CARGO_TYPES.items() if k == ('liquid' if liquid else 'dry')]
                sea = pname in RIVER_SEA_BARGES
                row = {'vessel_id': vid, 'current_port': base, 'available_at': start, 'available_to': end,
                       'capacity_t': cap, 'draft_empty_m': round(min(0.5, p['draft_full'] * 0.3), 2),
                       'draft_full_m': p['draft_full'], 'handling_tph': BARGE_HANDLING['liquid' if liquid else 'dry'],
                       'cargo_types_allowed': types,
                       **prov('ASSUMED', f'{LORP}; дедвейт — каталог; осадки и обработка — допущение')}
                if sea:
                    row.update(vessel_class=p['class_rs'], navigation_permits=[{'area': 'ARCTIC_COAST',
                               'status': 'SCENARIO_ASSUMPTION' if not real else 'UNVERIFIED',
                               'source': f'класс Регистра проекта {pname} по каталогу ЛОРП; допуск не подтверждён'}])
                barges.append(row); gb.append(vid)
                cards.append({'vessel_id': vid, 'kind': 'barge', 'class': f'{p["type"].lower()} проекта {pname}, {p["class_rs"]}',
                              'home_port': base, 'capacity_t': cap, 'draft_empty_m': row['draft_empty_m'],
                              'draft_full_m': p['draft_full'], 'handling_tph': row['handling_tph'],
                              'cargo_types_allowed': types, 'sea_allowed': sea, 'project': pname, 'built': None,
                              'operator': 'ЛОРП' if river != 'Колыма' else None, 'registry_id': None,
                              'source': 'проект — каталог ЛОРП; экземпляр — допущение'})
        groups.append((base, river, gt, gb))
    # Совместимость: pairing='base' — буксир водит баржи своей базы (составы закреплены, как на практике);
    # pairing='river' — любые баржи той же реки. Река—море — только между собой.
    by_river = defaultdict(lambda: ([], []))
    for base, river, gt, gb in groups:
        key = base if pairing == 'base' else river
        by_river[key][0].extend(gt); by_river[key][1].extend(gb)
    pairs = []
    sea_t = {t['vessel_id'] for t in tugs if 'navigation_permits' in t}
    sea_b = {b['vessel_id'] for b in barges if 'navigation_permits' in b}
    for river, (ts, bs) in by_river.items():
        for t in ts:
            for b in bs:
                if (t in sea_t) == (b in sea_b):
                    pairs.append({'tug_id': t, 'barge_id': b})
    sea_pairs = [p for p in pairs if p['tug_id'] in sea_t]
    for e in edges:
        if e.get('water_type') == 'SEA':
            e['allowed_pairs'] = sea_pairs

    # Заявки: пункт плана → порт сети; срок = окно навигации на пути (самое позднее открытие,
    # самое раннее закрытие среди рёбер кратчайшего пути) — допущение.
    adj = defaultdict(list)
    for e in edges:
        adj[e['origin_port']].append(e)

    def window(a, b):
        dist = {a: 0}; prev = {}
        q = [(0, a)]
        while q:
            d, u = heapq.heappop(q)
            if u == b:
                break
            if d > dist.get(u, 1e18):
                continue
            for e in adj[u]:
                v = e['destination_port']; nd = d + e['distance_km']
                if nd < dist.get(v, 1e18):
                    dist[v] = nd; prev[v] = e; heapq.heappush(q, (nd, v))
        if b not in prev:
            return None
        path, u = [], b
        while u != a:
            path.append(prev[u]); u = prev[u]['origin_port']
        opens, closes = [], []
        for e in path:
            openday = [d for d, c in sorted(e['daily_conditions'].items()) if not c['is_closed']]
            opens.append(openday[0]); closes.append(openday[-1])
        return max(opens), min(closes), dist[b]

    cargo, dropped, mapped = [], [], []
    for i, c in enumerate(cargo_t, 1):
        origin = POINT.get(c['origin'], c['origin']) if c['origin'] else FOOD_ORIGIN
        dest = POINT.get(c['destination'], c['destination'])
        vol = float(c['volume_t'])
        if dest is None or origin is None or origin not in port_ids or dest not in port_ids or origin == dest:
            dropped.append((c['destination'], c['origin'], vol, 'пункт вне сети' if dest is None or origin is None else
                            'тот же порт' if origin == dest else 'нет порта'))
            continue
        w = window(origin, dest)
        if not w:
            dropped.append((c['destination'], c['origin'], vol, 'нет пути')); continue
        ready, due, km = w
        if c['destination'] not in port_ids and c['destination'] not in EXACT_NAMES:
            mapped.append((c['destination'], dest, vol))
        cargo.append({'cargo_id': f'Z{i:03d}', 'cargo_type': c['cargo_type'], 'origin_port': origin,
                      'destination_port': dest, 'volume_t': vol, 'ready_at': f'{ready}T00:00:00',
                      'due_at': f'{due}T23:59:59', 'priority': 1 if c['cargo_type'] in CRITICAL else 0,
                      'critical_category': CRITICAL.get(c['cargo_type']),
                      **prov('REAL' if c['origin'] and c['destination'] not in POINT or c['destination'] in EXACT_NAMES else 'DERIVED',
                             f'{PLAN}, приложение {c["appendix"]}: {c["origin"] or "пункт отгрузки не указан"} → {c["destination"]} ({c["district"]})',
                             {'plan_point': c['destination'], 'dates_provenance': 'ASSUMED: окно навигации на пути'})})
        if real:
            cargo[-1]['data_status'] = 'REAL'
    # Промышленный транзит Усть-Кут → Ленск/Витим (нефтепродукты и МТР для АЛРОСА и «Сургутнефтегаза»,
    # в т. ч. 49,7 тыс. т для жизнеобеспечения Ленского и Мирнинского улусов) — сотни рейсов по верхней Лене,
    # которые не относятся к арктическому завозу. По умолчанию остаётся; --exclude-transit убирает его.
    excluded = []
    if not include_transit:
        keep = []
        for c in cargo:
            if c['origin_port'] == 'Осетрово' and c['destination_port'] in ('Ленск', 'Витим'):
                excluded.append((c['plan_point'], c['cargo_type'], c['volume_t']))
            else:
                keep.append(c)
        cargo = keep
    # Одинаковые «откуда — куда — груз» (повторы после привязки посёлков к ближайшему порту) — одна заявка.
    if merge:
        groups = {}
        for c in cargo:
            key = (c['origin_port'], c['destination_port'], c['cargo_type'])
            if key not in groups:
                groups[key] = {**c, 'plan_point': [c['plan_point']], 'source': [c['source']]}
            else:
                g = groups[key]
                g['volume_t'] = round(g['volume_t'] + c['volume_t'], 3)
                g['plan_point'].append(c['plan_point']); g['source'].append(c['source'])
                if c['provenance'] == 'DERIVED':
                    g['provenance'] = 'DERIVED'
        cargo = []
        for i, g in enumerate(groups.values(), 1):
            g['cargo_id'] = f'Z{i:03d}'
            g['plan_point'] = '; '.join(dict.fromkeys(g['plan_point']))
            g['source'] = ' | '.join(g['source'])
            cargo.append(g)
    scenario = {'scenario_id': f'yakutia_real_2026_{mode}', 'data_status': 'MIXED' if real else 'SYNTHETIC',
                'start': iso(START), 'end': iso(END, True), 'ports': port_ids, 'underkeel_m': 0.2,
                'fixed_handling_seconds': 7200, 'edges': edges, 'tugs': tugs, 'barges': barges,
                'compatible_pairs': pairs, 'social_requirements': [], 'cargo': cargo}
    report = {'mode': mode, 'pairing': pairing, 'fleet_scale': fleet_scale, 'ports': len(port_ids), 'edges': len(edges), 'tugs': len(tugs), 'barges': len(barges),
              'pairs': len(pairs), 'cargo': len(cargo), 'cargo_t': round(sum(c['volume_t'] for c in cargo)),
              'dropped': dropped, 'mapped_to_nearest_port': mapped,
              'excluded_transit': excluded, 'excluded_transit_t': round(sum(x[2] for x in excluded)), 'merged': merge}
    return scenario, cards, report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mode', choices=['demo', 'strict'], default='demo')
    ap.add_argument('--fleet-scale', type=float, default=1.0)
    ap.add_argument('--pairing', choices=['base', 'river'], default='base')
    ap.add_argument('--no-merge', action='store_true', help='не объединять одинаковые «откуда — куда — груз»')
    ap.add_argument('--exclude-transit', action='store_true', help='убрать транзит Усть-Кут → Ленск/Витим')
    a = ap.parse_args()
    scenario, cards, report = build(a.mode, a.fleet_scale, a.pairing, not a.no_merge, not a.exclude_transit)
    OUT.mkdir(exist_ok=True)
    (OUT / f'yakutia_real_2026_{a.mode}.json').write_text(json.dumps(scenario, ensure_ascii=False), encoding='utf-8')
    (OUT / 'fleet_cards.json').write_text(json.dumps({'data_status': 'MIXED', 'cargo_types_ru': {
        'fuel': 'нефтепродукты', 'coal': 'уголь', 'general': 'МТР и стройматериалы', 'food': 'продовольствие',
        'crude_oil': 'сырая нефть', 'gas_condensate': 'газовый конденсат'}, 'vessels': cards}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    (OUT / f'report_{a.mode}.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print({k: v for k, v in report.items() if k not in ('dropped', 'mapped_to_nearest_port', 'excluded_transit')})
    print('не вошло:', [(d[0], d[2], d[3]) for d in report['dropped']])
    print('привязано к ближайшему порту:', len(report['mapped_to_nearest_port']), 'строк,',
          round(sum(m[2] for m in report['mapped_to_nearest_port'])), 'т')


if __name__ == '__main__':
    main()
