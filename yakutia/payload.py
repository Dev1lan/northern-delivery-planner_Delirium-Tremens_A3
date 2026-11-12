"""Сетевое ядро 3.0.0 → данные для карты сети.

Запускает стратегии сетевого ядра (network.adapter.run_scenario) без изменений,
выбирает лучшую по порядку целей самого ядра (тонны вовремя, тонны всего,
меньше порожнего пробега, меньше рейсов) и собирает компактные данные для
страницы: геометрия, участки с календарём глубин, суда, рейсы, события,
финансы. Ничего не пересчитывает: все числа — из результата v3.

    python yakutia/payload.py --scenario data/yakutia/yakutia_network.json --out yakutia/build/yakutia.json

Порты, которых нет в геооснове, раскладываются условной схемой; участки,
которых нет в геооснове, рисуются прямой линией.
"""

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.adapter import run_scenario  # noqa: E402
from network.models import Limits  # noqa: E402

GEO = ROOT / 'yakutia' / 'geo' / 'yakutia_geo.json'
STRATEGIES = ['NETWORK_REPOSITION', 'NETWORK_SOCIAL', 'NETWORK_GREEDY', 'NETWORK_FCFS']
COST = 'NETWORK_COST'  # поиск по цене: только с тарифами v2 (tariff_version = 2)
LABELS = {'NETWORK_REPOSITION': 'С перегоном', 'NETWORK_SOCIAL': 'Соцминимумы',
          'NETWORK_GREEDY': 'Жадная', 'NETWORK_FCFS': 'По очереди', COST: 'По цене'}
HINTS = {'NETWORK_REPOSITION': 'Ищет план целиком и разрешает порожний перегон судна к заявке в другом порту',
         'NETWORK_SOCIAL': 'Ищет план целиком: сначала минимумы по критическим грузам, затем тонны вовремя',
         'NETWORK_GREEDY': 'Каждый раз берёт рейс с лучшей производительностью, без перегона',
         'NETWORK_FCFS': 'Заявки по порядку готовности, без перегона',
         COST: 'Как «С перегоном», но при равных тоннах выбирает план с меньшей стоимостью по тарифам'}


def default_strategies(tariffs):
    """Четыре базовые стратегии; с тарифами v2 — ещё поиск по цене."""
    return STRATEGIES + ([COST] if tariffs and tariffs.get('tariff_version') == 2 else [])
STATE = {'LOADING': 'load', 'LOADED_TRANSIT': 'sail', 'UNLOADING': 'unload', 'EMPTY_TRANSIT': 'empty',
         'WAITING': 'wait'}


def _dt(s):
    return datetime.fromisoformat(s)


def _score(m):
    return (m['delivered_on_time_t'], m['delivered_total_t'], -m['empty_km'], -m['completed_voyages'])


def _schematic(ports, edges):
    """Условная раскладка портов без координат: пружинная модель, км."""
    n = len(ports)
    pos = {p: [600 * math.cos(2 * math.pi * i / n), 600 * math.sin(2 * math.pi * i / n)] for i, p in enumerate(ports)}
    pairs = {tuple(sorted((e['origin_port'], e['destination_port']))) for e in edges}
    for step in range(300):
        force = {p: [0.0, 0.0] for p in ports}
        for i, a in enumerate(ports):
            for b in ports[i + 1:]:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                d2 = dx * dx + dy * dy + 1
                f = 9e4 / d2
                force[a][0] += f * dx; force[a][1] += f * dy
                force[b][0] -= f * dx; force[b][1] -= f * dy
        for a, b in pairs:
            dx, dy = pos[b][0] - pos[a][0], pos[b][1] - pos[a][1]
            d = math.hypot(dx, dy) or 1
            f = (d - 350) * 0.05
            force[a][0] += f * dx / d; force[a][1] += f * dy / d
            force[b][0] -= f * dx / d; force[b][1] -= f * dy / d
        cool = 1 - step / 300
        for p in ports:
            fx, fy = force[p]
            m = math.hypot(fx, fy) or 1
            pos[p][0] += fx / m * min(m, 40) * cool
            pos[p][1] += fy / m * min(m, 40) * cool
    return {p: [round(v[0], 1), round(v[1], 1)] for p, v in pos.items()}


def _links(raw, geo):
    """Неориентированные участки сценария с геометрией и календарём глубин по дням."""
    gl = {tuple(sorted((l['a'], l['b']))): l for l in (geo or {}).get('links', [])}
    start = _dt(raw['start']).date()
    days = (_dt(raw['end']).date() - start).days + 1
    out, index = [], {}
    for e in raw['edges']:
        key = tuple(sorted((e['origin_port'], e['destination_port'])))
        if key not in index:
            g = gl.get(key)
            index[key] = len(out)
            out.append({'id': len(out), 'a': key[0], 'b': key[1], 'km': e['distance_km'],
                        'kind': g['kind'] if g else 'river', 'river': g['river'] if g else '',
                        'xy': (g['xy'] if g['a'] == key[0] else g['xy'][::-1]) if g else None,
                        'edges': {}, 'depth': None, 'draft': None})
        link = out[index[key]]
        link['edges'][f"{e['origin_port']}>{e['destination_port']}"] = {'id': e['edge_id'], 'dir': e['direction']}
        if link['depth'] is None:
            base = e['condition']
            daily = e.get('daily_conditions', {})
            depth, draft = [], []
            for i in range(days):
                day = datetime.fromordinal(start.toordinal() + i).date().isoformat()
                c = {**base, **daily.get(day, {})}
                closed = bool(c.get('is_closed'))
                depth.append(None if closed else round(float(c['usable_depth_m']), 2))
                draft.append(None if closed else round(float(c['max_allowed_draft_m']), 2))
            link['depth'], link['draft'] = depth, draft
    return out, index


def _strategy_view(result, t0, link_index, sea_links, cards):
    ev = []
    warnings = []
    for e in result['events']:
        a, b = e['origin_port'], e['destination_port']
        lid = link_index.get(tuple(sorted((a, b)))) if a != b else None
        ev.append([e['voyage_id'], e['tug_id'], e['barge_id'], e['cargo_id'], STATE.get(e['state'], e['state']),
                   a, b, round((_dt(e['start']) - t0).total_seconds() / 3600, 3),
                   round((_dt(e['end']) - t0).total_seconds() / 3600, 3), lid, e['km'], e['volume_t']])
        if lid in sea_links:
            for vid in (e['tug_id'], e['barge_id']):
                card = cards.get(vid)
                if card is not None and card.get('sea_allowed') is False:
                    warnings.append({'voyage': e['voyage_id'], 'vessel': vid, 'from': a, 'to': b,
                                     'code': 'RIVER_VESSEL_AT_SEA'})
    seen, uniq = set(), []
    for w in warnings:
        k = (w['voyage'], w['vessel'])
        if k not in seen:
            seen.add(k); uniq.append(w)
    fi = result['financial_inputs']
    voyages = [{k: v[k] for k in ('voyage_id', 'tug_id', 'barge_id', 'cargo_id', 'cargo_type', 'origin_port',
                                  'destination_port', 'volume_t', 'on_time', 'late_seconds', 'loaded_km', 'empty_km',
                                  'tonne_km', 'transit_seconds', 'loading_seconds', 'unloading_seconds',
                                  'waiting_seconds')} for v in fi['voyages']]
    return {'metrics': result['metrics'], 'termination_reason': result['termination_reason'],
            'audit': result['schedule_audit'], 'requirements_status': result['requirements_status'],
            'cargo_status': result['cargo_status'], 'final_resources': result['final_resources'],
            'reposition_comparison': result.get('reposition_comparison'),
            'financial': result.get('financial_evaluation'), 'financial_totals': fi['totals'],
            'resources': fi['resources'], 'voyages': voyages, 'events': ev, 'warnings': uniq}


def build_payload(raw, *, geo=None, limits=None, tariffs=None, cards=None, strategies=None, title=None,
                  workers=4, keep_results=None):
    geo = geo if geo is not None else json.loads(GEO.read_text(encoding='utf-8'))
    strategies = strategies or default_strategies(tariffs)
    t0 = _dt(raw['start'])
    geo_ports = {p['id']: p for p in geo.get('ports', [])}
    missing = [p for p in raw['ports'] if p not in geo_ports]
    schematic = _schematic(raw['ports'], raw['edges']) if len(missing) == len(raw['ports']) else {}
    if missing and not schematic:  # часть портов неизвестна: ставим рядом с центром масс известных
        known = [geo_ports[p]['xy'] for p in raw['ports'] if p in geo_ports]
        cx = sum(x for x, _ in known) / len(known); cy = sum(y for _, y in known) / len(known)
        for i, p in enumerate(missing):
            ang = 2 * math.pi * i / len(missing)
            schematic[p] = [round(cx + 300 * math.cos(ang), 1), round(cy + 300 * math.sin(ang), 1)]
    ports = []
    for p in raw['ports']:
        g = geo_ports.get(p)
        ports.append({'id': p, 'xy': g['xy'] if g else schematic[p], 'river': g['river'] if g else '',
                      'kind': g['kind'] if g else 'port', 'known': bool(g)})
    links, index = _links(raw, geo if not schematic or len(missing) < len(raw['ports']) else None)
    sea_links = {l['id'] for l in links if l['kind'] == 'sea'}
    card_map = {c['vessel_id']: c for c in (cards or {}).get('vessels', [])}
    fleet = []
    for kind, rows in (('tug', raw['tugs']), ('barge', raw['barges'])):
        for r in rows:
            card = card_map.get(r['vessel_id'], {})
            fleet.append({**{k: v for k, v in r.items() if k not in ('available_at', 'available_to')},
                          'kind': kind, 'card': card or None})

    def run(strategy):
        t = time.time()
        result = run_scenario(raw, strategy, limits=limits, tariffs=tariffs)
        return strategy, result, round(time.time() - t, 2)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        runs = list(pool.map(run, strategies))
    views, results = {}, {}
    for strategy, result, seconds in runs:
        view = _strategy_view(result, t0, index, sea_links, card_map)
        view.update(label=LABELS.get(strategy, strategy), hint=HINTS.get(strategy, ''), seconds=seconds)
        views[strategy] = view
        results[strategy] = result
    best = max(views, key=lambda s: _score(views[s]['metrics']))
    if keep_results is not None:
        keep_results.update(results)
    cargo = [{k: c[k] for k in ('cargo_id', 'cargo_type', 'origin_port', 'destination_port', 'volume_t',
                                'priority', 'critical_category')}
             | {'ready_h': round((_dt(c['ready_at']) - t0).total_seconds() / 3600, 2),
                'due_h': round((_dt(c['due_at']) - t0).total_seconds() / 3600, 2)}
             for c in raw['cargo']]
    return {
        'schema': 'network-view/1', 'core': {'schema_version': runs[0][1]['schema_version'],
                                             'algorithm_version': runs[0][1]['algorithm_version']},
        'title': title or raw['scenario_id'], 'scenario_id': raw['scenario_id'], 'data_status': raw['data_status'],
        'start': raw['start'], 'end': raw['end'], 'underkeel': raw.get('underkeel_m', 0),
        'horizon_h': round((_dt(raw['end']) - t0).total_seconds() / 3600, 2),
        'geo': {'rivers': geo.get('rivers', []) if not (schematic and len(missing) == len(raw['ports'])) else [],
                'coast': geo.get('coast', []) if not (schematic and len(missing) == len(raw['ports'])) else [],
                'land': geo.get('land', []) if not (schematic and len(missing) == len(raw['ports'])) else [],
                'labels': geo.get('labels', []) if not (schematic and len(missing) == len(raw['ports'])) else [],
                'source': geo.get('source', '') if len(missing) < len(raw['ports']) else 'Условная схема: координат портов нет',
                'length_note': geo.get('length_note', ''), 'schematic_ports': sorted(schematic)},
        'ports': ports, 'links': links, 'fleet': fleet, 'cargo': cargo,
        'cargo_types_ru': (cards or {}).get('cargo_types_ru', {}),
        'tariffs': {'currency': tariffs.get('currency'), 'source_status': tariffs.get('source_status'),
                    'rates': tariffs.get('rates')} if tariffs else None,
        'strategies': views, 'order': [s for s in STRATEGIES if s in views] + [s for s in views if s not in STRATEGIES],
        'best': best,
    }


TEMPLATE = ROOT / 'yakutia' / 'network.html'


def render_html(payload, app=False):
    text = TEMPLATE.read_text(encoding='utf-8')
    data = 'null' if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    return text.replace('/*__DATA__*/null', data, 1).replace('/*__APP__*/false', 'true' if app else 'false', 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--scenario', default=str(ROOT / 'data' / 'yakutia' / 'yakutia_network.json'))
    ap.add_argument('--limits', default=str(ROOT / 'data' / 'yakutia' / 'limits.json'))
    ap.add_argument('--tariffs', default=str(ROOT / 'data' / 'yakutia' / 'tariffs_v2.json'))
    ap.add_argument('--cards', default=str(ROOT / 'data' / 'yakutia' / 'fleet_cards.json'))
    ap.add_argument('--title', default='Северный завоз · Якутия')
    ap.add_argument('--out', default=str(ROOT / 'yakutia' / 'build' / 'yakutia.json'))
    ap.add_argument('--html', help='дополнительно собрать автономную страницу с данными внутри')
    ap.add_argument('--results-dir', help='сохранить полные результаты v3 по стратегиям (result_<STRATEGY>.json)')
    a = ap.parse_args()

    def read(p):
        return json.loads(Path(p).read_text(encoding='utf-8-sig')) if p and Path(p).is_file() else None

    lim = read(a.limits)
    t = time.time()
    kept = {}
    payload = build_payload(read(a.scenario), limits=Limits(**lim) if lim else None, tariffs=read(a.tariffs),
                            cards=read(a.cards), title=a.title, keep_results=kept)
    if a.results_dir:
        out_dir = Path(a.results_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, result in kept.items():
            (out_dir / f'result_{name}.json').write_text(
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    if a.html:
        Path(a.html).write_text(render_html(payload), encoding='utf-8')
    for s in payload['order']:
        m = payload['strategies'][s]['metrics']
        print(f"{s:20} в срок {m['delivered_on_time_t']:>8.0f} т  всего {m['delivered_total_t']:>8.0f} т  "
              f"порожний {m['empty_km']:>6.0f} км  рейсов {m['completed_voyages']:>3}  "
              f"предупр. {len(payload['strategies'][s]['warnings'])}  {payload['strategies'][s]['seconds']} с")
    print('лучшая:', payload['best'], '| размер КБ:', Path(a.out).stat().st_size // 1024,
          '| всего с:', round(time.time() - t, 1))


if __name__ == '__main__':
    main()
