"""Геооснова карты для сценария на реальных данных.

Фон (реки, берег, суша, подписи) — из yakutia/geo/yakutia_geo.json (Natural Earth).
Порты — координаты из ports.csv (OpenStreetMap или приближённые). Линии участков —
русло OpenStreetMap между портами (map/geo/rivers); морские переходы — опорные точки
старой геоосновы, где пара портов та же, иначе прямая. Длины в подписи — из edges.csv.

    python data/yakutia_real/build_geo.py
"""
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'map'))
sys.path.insert(0, str(ROOT / 'yakutia'))
import geo_resolver as G  # noqa: E402
from geo_build import project, rdp  # noqa: E402

OSM = {'Лена': 'лена', 'Алдан': 'алдан', 'Вилюй': 'вилюй', 'Яна': 'яна', 'Индигирка': 'индигирка', 'Колыма': 'колыма'}
KIND = {'Якутск': 'hub', 'Осетрово': 'hub', 'Тикси': 'sea', 'Устье Алдана': 'junction', 'Устье Вилюя': 'junction',
        'Быков Мыс': 'junction'}
JUNCTION_RIVER = {'Устье Алдана': 'Лена', 'Устье Вилюя': 'Лена'}


def main():
    base = json.loads((ROOT / 'yakutia' / 'geo' / 'yakutia_geo.json').read_text(encoding='utf-8'))
    old_links = {tuple(sorted((l['a'], l['b']))): l for l in base['links']}
    ports = list(csv.DictReader(open(HERE / 'ports.csv', encoding='utf-8')))
    pos = {}
    out_ports = []
    for p in ports:
        lat, lon = float(p['lat']), float(p['lon'])
        xy = list(project(lon, lat))
        pos[p['port']] = xy
        out_ports.append({'id': p['port'], 'river': p['river'], 'lat': lat, 'lon': lon, 'xy': xy,
                          'kind': KIND.get(p['port'], 'port')})
    rivers = {}
    links = []
    for e in csv.DictReader(open(HERE / 'edges.csv', encoding='utf-8')):
        a, b = e['origin_port'], e['destination_port']
        key = tuple(sorted((a, b)))
        xy = None
        if e['water_type'] == 'SEA':
            old = old_links.get(key)
            if old and old['kind'] == 'sea':
                xy = old['xy'] if old['a'] == a else old['xy'][::-1]
                xy = [pos[a]] + xy[1:-1] + [pos[b]]
            kind, river = 'sea', 'Море Лаптевых / Восточно-Сибирское'
        else:
            kind, river = 'river', e['river']
            name = OSM.get(river)
            if name:
                rv = rivers.setdefault(name, json.loads((ROOT / 'map' / 'geo' / 'rivers' / f'{name}.json').read_text(encoding='utf-8')))
                # устья притоков и конец Лены — не населённые пункты: ищем путь по координатам порта
                places = dict(rv.get('places', {}))
                for pid in (a, b):
                    if pid not in places:
                        p = next(x for x in out_ports if x['id'] == pid)
                        places[pid] = {'lat': p['lat'], 'lon': p['lon']}
                route = G.route_on_river({**rv, 'places': places}, a, b)
                if route and route.get('line'):
                    pts = [list(project(lon, lat)) for lat, lon in route['line']]
                    xy = [pos[a]] + rdp(pts, 1.5)[1:-1] + [pos[b]]
                elif key in old_links and old_links[key]['xy']:   # русло OSM неполное (Яна выше Усть-Куйги)
                    old = old_links[key]
                    xy = old['xy'] if old['a'] == a else old['xy'][::-1]
                    xy = [pos[a]] + xy[1:-1] + [pos[b]]
        links.append({'a': a, 'b': b, 'river': river, 'kind': kind,
                      'down': b if e['direction_origin_to_destination'] == 'downstream' else None,
                      'km': round(float(e['distance_km'])), 'xy': xy or [pos[a], pos[b]]})
    geo = {**base, 'ports': out_ports, 'links': links,
           'source': base['source'].split(';')[0] + '; порты и участки — OpenStreetMap (ODbL) и норматив АТ-442-р',
           'length_note': 'Длины — официальные по нормативу АТ-442-р; у пристаней внутри участков — пересчёт по руслу OSM; морские — оценка'}
    out = HERE / 'scenario' / 'geo.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(geo, ensure_ascii=False), encoding='utf-8')
    straight = [f"{l['a']}—{l['b']}" for l in links if len(l['xy']) == 2]
    print(f'портов {len(out_ports)}, участков {len(links)}, прямых линий {len(straight)}: {straight}')


if __name__ == '__main__':
    main()
