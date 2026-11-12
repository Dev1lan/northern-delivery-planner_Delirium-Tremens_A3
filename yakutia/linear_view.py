"""Линейная матмодель 2.1.0 (одна река) → данные той же страницы сети.

Берёт данные карты, которые собирает map/build_map.py (результаты контракта 1.0.0,
глубины по дням, прогноз ML), и переводит их в формат network-view/1, который
рисует yakutia/network.html. Ничего не пересчитывает: рейсы, метрики и прогноз —
как в исходных данных. Финансовая оценка — по тем же статьям, что network.financial,
и по тем же демо-ставкам (ставки условные).
"""

import json
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

HERE = Path(__file__).resolve().parent
GEO = HERE / 'geo' / 'yakutia_geo.json'
TARIFFS = HERE.parent / 'data' / 'yakutia' / 'tariffs_demo.json'
LABELS = {'OPTIMIZED': 'Оптимизатор', 'EARLIEST_DUE': 'По сроку', 'FCFS': 'По очереди'}
HINTS = {'OPTIMIZED': 'Перебирает варианты рейсов и выбирает лучший по тоннам в срок',
         'EARLIEST_DUE': 'Сначала везёт заявки с самым ранним сроком',
         'FCFS': 'Везёт заявки по порядку готовности'}
ORDER = ['OPTIMIZED', 'EARLIEST_DUE', 'FCFS']
CARGO_RU = {'coal': 'уголь', 'crude_oil': 'нефть', 'food': 'продовольствие', 'food_extended': 'продовольствие (доп.)',
            'fuel': 'топливо', 'general': 'генгруз'}


def _epoch(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def _project(lon, lat, lon0=130.0, lat0=65.0, r=6371.0):
    lam, phi, phi0 = math.radians(lon - lon0), math.radians(lat), math.radians(lat0)
    k = math.sqrt(2 / (1 + math.sin(phi0) * math.sin(phi) + math.cos(phi0) * math.cos(phi) * math.cos(lam)))
    return [round(r * k * math.cos(phi) * math.sin(lam), 2),
            round(r * k * (math.cos(phi0) * math.sin(phi) - math.sin(phi0) * math.cos(phi) * math.cos(lam)), 2)]


def _at_km(line, line_km, target):
    """Точка на линии русла на расстоянии target км от начала: (индекс, [lat, lon])."""
    for i in range(1, len(line_km)):
        if line_km[i] >= target:
            f = (target - line_km[i - 1]) / max(1e-9, line_km[i] - line_km[i - 1])
            a, b = line[i - 1], line[i]
            return i, [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
    return len(line) - 1, line[-1]


def _price(totals, resources, tariffs):
    q = {'loaded_km': Decimal(str(totals['loaded_km'])), 'empty_km': Decimal(str(totals['empty_km'])),
         'loading_hour': Decimal(totals['loading_seconds']) / 3600,
         'unloading_hour': Decimal(totals['unloading_seconds']) / 3600,
         'waiting_hour': Decimal(totals['waiting_seconds']) / 3600,
         'tug_busy_hour': Decimal(sum(r['busy_seconds'] for r in resources)) / 3600,
         'barge_busy_hour': Decimal(sum(r['busy_seconds'] for r in resources)) / 3600}
    cost = {k: (Decimal(str(tariffs['rates'][k])) * v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            for k, v in q.items()}
    return {'currency': tariffs['currency'], 'source_status': tariffs['source_status'], 'rates': tariffs['rates'],
            'cost_by_unit': {k: str(v) for k, v in cost.items()}, 'total_cost': str(sum(cost.values(), Decimal('0')))}


def convert(p, tariffs=None, base_geo=None):
    """p — данные map/build_map.build_payload со стратегиями; результат — network-view/1."""
    if tariffs is None and TARIFFS.exists():
        tariffs = json.loads(TARIFFS.read_text(encoding='utf-8'))
    if base_geo is None and GEO.exists():
        base_geo = json.loads(GEO.read_text(encoding='utf-8'))
    route, nav = p['route'], p['navigation']
    t0 = _epoch(nav['start'])
    hours = lambda e: round((e - t0) / 3600, 3)
    nodes, segs, L = route['nodes'], route['segments'], route['length_km']
    geo = route.get('geo')

    # порты и линии участков: по руслу OSM (узлы пропорционально км конфигурации) или схемой
    ports, link_xy = [], {}
    if geo and geo.get('line'):
        line, lkm = geo['line'], geo['line_km']
        scale = lkm[-1] / max(1e-9, L)
        pos = {}
        for n in nodes:
            pos[n['id']] = _at_km(line, lkm, n['km'] * scale)
        for g in segs:
            (ia, pa), (ib, pb) = pos[g['from_node']], pos[g['to_node']]
            pts = [pa] + line[ia:ib] + [pb]
            link_xy[g['id']] = [_project(lo, la) for la, lo in pts]
        for n in nodes:
            la, lo = pos[n['id']][1]
            ports.append({'id': n['id'], 'xy': _project(lo, la), 'river': geo.get('river', ''), 'known': True})
        source = f"Русло: {geo.get('attribution', 'OpenStreetMap')}; фон: Natural Earth"
    else:
        for i, n in enumerate(nodes):
            ports.append({'id': n['id'], 'xy': [n['km'], 40 * math.sin(i)], 'river': '', 'known': False})
        source = 'Условная схема маршрута: русла в библиотеке нет'
    for q in ports:
        q['kind'] = 'hub' if q['id'] == route['origin'] else 'port'

    river = (geo or {}).get('river', '') or 'Маршрут'
    days = p['days']
    d0 = datetime.fromisoformat(nav['start']).date()
    offset = (datetime.fromisoformat(days[0]).date() - d0).days if days else 0
    links, index = [], {}
    for g in segs:
        c = p['conditions'][g['id']]
        depth = [None] * offset + [None if cl else d for d, cl in zip(c['depth_m'], c['closed'])]
        draft = [None] * offset + [None if cl else a for a, cl in zip(c['allowed_draft_m'], c['closed'])]
        index[g['id']] = len(links)
        links.append({'id': len(links), 'a': g['from_node'], 'b': g['to_node'], 'km': g['km'], 'kind': 'river',
                      'river': river, 'xy': link_xy.get(g['id']), 'depth': depth, 'draft': draft,
                      'edges': {f"{g['from_node']}>{g['to_node']}": {'id': g['id'], 'dir': 'upstream'},
                                f"{g['to_node']}>{g['from_node']}": {'id': g['id'], 'dir': 'downstream'}}})

    origin, dest = route['origin'], route['destination']
    end_h = hours(_epoch(nav['end']) + 86400)
    fleet = [{'vessel_id': u['unit_id'], 'kind': 'tug', 'current_port': origin, 'capacity_t': u['capacity_t'],
              'draft_empty_m': u['draft_empty_m'], 'draft_full_m': u['draft_full_m'],
              'tug_draft_m': u['tug_draft_m'], 'cargo_types_allowed': u['cargo_types'],
              'card': {'class': 'состав: буксир + баржа', 'home_port': origin, 'sea_allowed': False,
                       'project': None, 'built': None, 'operator': None, 'registry_id': None}}
             for u in p['units']]

    views = {}
    for st, r in p['strategies'].items():
        events, voyages, busy = [], [], {}
        for i, t in enumerate(r['trips'], 1):
            u = t['unit_id']
            events.append([i, u, None, t['cargo_id'], 'load', origin, origin, hours(t['loading_start']),
                           hours(t['departure']), None, 0, t['volume_t']])
            for sid, direction, a, b in t['events']:
                g = segs[index[sid]]
                fr, to = (g['from_node'], g['to_node']) if direction == 1 else (g['to_node'], g['from_node'])
                events.append([i, u, None, t['cargo_id'] if direction == 1 else None,
                               'sail' if direction == 1 else 'back', fr, to, hours(a), hours(b), index[sid], g['km'],
                               t['volume_t'] if direction == 1 else 0])
                if direction == 1 and to == dest:
                    events.append([i, u, None, t['cargo_id'], 'unload', dest, dest, hours(t['arrival']),
                                   hours(t['unloading_end']), None, 0, t['volume_t']])
            busy[u] = busy.get(u, 0) + (t['released_at'] - t['loading_start'])
            voyages.append({'voyage_id': i, 'tug_id': u, 'barge_id': '', 'cargo_id': t['cargo_id'],
                            'cargo_type': t['cargo_type'], 'origin_port': origin, 'destination_port': dest,
                            'volume_t': t['volume_t'], 'on_time': t['on_time'], 'late_seconds': 0,
                            'loaded_km': L, 'empty_km': L, 'tonne_km': t['volume_t'] * L,
                            'transit_seconds': (t['arrival'] - t['departure']) + (t['released_at'] - t['unloading_end']),
                            'loading_seconds': t['departure'] - t['loading_start'],
                            'unloading_seconds': t['unloading_end'] - t['arrival'], 'waiting_seconds': 0})
        events.sort(key=lambda e: (e[7], e[0]))
        m = r['metrics']
        horizon_s = int(end_h * 3600)
        resources = [{'vessel_id': u['vessel_id'], 'vessel_type': 'unit', 'busy_seconds': busy.get(u['vessel_id'], 0),
                      'idle_seconds': max(0, horizon_s - busy.get(u['vessel_id'], 0))} for u in fleet]
        totals = {'loaded_km': sum(v['loaded_km'] for v in voyages), 'empty_km': sum(v['empty_km'] for v in voyages),
                  'tonne_km': sum(v['tonne_km'] for v in voyages),
                  'transit_seconds': sum(v['transit_seconds'] for v in voyages),
                  'loading_seconds': sum(v['loading_seconds'] for v in voyages),
                  'unloading_seconds': sum(v['unloading_seconds'] for v in voyages), 'waiting_seconds': 0,
                  'undelivered_t': m['undelivered_t'], 'late_tonne_hours': 0, 'social_deficit_t': 0}
        metrics = {'demand_t': p['cargo']['total_t'], 'delivered_on_time_t': m['delivered_on_time_t'],
                   'delivered_total_t': m['delivered_total_t'], 'undelivered_t': m['undelivered_t'],
                   'loaded_km': totals['loaded_km'], 'empty_km': totals['empty_km'],
                   'completed_voyages': m['completed_trips'], 'repositioning_voyages': 0,
                   'violations': m.get('violations', 0), 'fleet_utilization': m.get('fleet_utilization')}
        views[st] = {'label': LABELS.get(st, st), 'hint': HINTS.get(st, ''), 'seconds': None,
                     'metrics': metrics, 'termination_reason': 'SEARCH_FINISHED',
                     'audit': {'status': 'PASSED' if r.get('schema_validated') and not m.get('violations') else
                               ('FAILED' if m.get('violations') else 'NOT_CHECKED'), 'violations': []},
                     'requirements_status': 'NOT_CONFIGURED', 'cargo_status': [],
                     'final_resources': [{'vessel_id': u['vessel_id'], 'current_port': origin} for u in fleet],
                     'reposition_comparison': None,
                     'financial': _price(totals, resources, tariffs) if tariffs else None,
                     'financial_totals': totals, 'resources': resources, 'voyages': voyages, 'events': events,
                     'warnings': []}
    best = p.get('default_strategy') or next(iter(views))
    cargo = [{'cargo_id': c['id'], 'cargo_type': c['type'], 'origin_port': origin, 'destination_port': dest,
              'volume_t': c['volume_t'], 'priority': 0, 'critical_category': 'food' if 'food' in c['type'] else None,
              'ready_h': hours(_epoch(c['ready'])), 'due_h': hours(_epoch(c['due']))} for c in p['cargo']['lots']]

    in_yakutia = bool(geo) and base_geo is not None and all(
        96 <= lo <= 170 and 54 <= la <= 77 for la, lo in (geo['line'][0], geo['line'][-1]))
    back = base_geo if in_yakutia else {}
    rivers = list(back.get('rivers', []))
    for c in (geo or {}).get('context', []):
        if c.get('kind') == 'river' and len(c.get('pts', [])) > 1:
            xy = [_project(lo, la) for la, lo in c['pts']]
            part = [xy[0]]
            for a, b in zip(xy, xy[1:]):          # склейка кусков OSM даёт скачки — режем линию на них
                if math.hypot(b[0] - a[0], b[1] - a[1]) > 12:
                    if len(part) > 1:
                        rivers.append({'name': '', 'weight': 1, 'navigable': False, 'xy': part})
                    part = []
                part.append(b)
            if len(part) > 1:
                rivers.append({'name': '', 'weight': 1, 'navigable': False, 'xy': part})
    if geo and geo.get('line'):
        rivers.append({'name': river, 'weight': 2, 'navigable': True,
                       'xy': [_project(lo, la) for la, lo in geo['line'][::3]]})
    return {
        'schema': 'network-view/1', 'kind': 'linear',
        'core': {'schema_version': '1.0.0', 'algorithm_version': '2.1.0 (одна река)'},
        'title': p['title'], 'scenario_id': p['scenario_id'], 'data_status': 'SYNTHETIC',
        'start': nav['start'] + 'T00:00:00', 'end': nav['end'] + 'T23:59:59', 'underkeel': nav['underkeel_m'],
        'horizon_h': end_h,
        'geo': {'rivers': rivers, 'coast': back.get('coast', []), 'land': back.get('land', []),
                'labels': [x for x in back.get('labels', []) if x['kind'] == 'sea'], 'source': source,
                'length_note': f"Длины участков — из конфигурации сценария ({L:g} км); русло на карте — для наглядности",
                'schematic_ports': [] if geo else [n['id'] for n in nodes]},
        'ports': ports, 'links': links, 'fleet': fleet, 'cargo': cargo, 'cargo_types_ru': CARGO_RU,
        'tariffs': {k: tariffs[k] for k in ('currency', 'source_status', 'rates')} if tariffs else None,
        'strategies': views, 'order': [s for s in ORDER if s in views], 'best': best,
        'ml': p.get('ml'), 'disclaimer': p.get('disclaimer'),
    }


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('payload', help='JSON от map/build_map.py --data-out')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    data = convert(json.loads(Path(a.payload).read_text(encoding='utf-8')))
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
