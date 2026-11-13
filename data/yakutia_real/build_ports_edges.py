"""Порты и рёбра сети Якутии из официальных участков (АТ-442-р) и русел OpenStreetMap.

Километр от устья берётся из норматива, если порт — граница участка или имеет отметку
«р. Лена, N км». Для пристаней внутри участка — путь по руслу OSM от ближайшего
официального порта, умноженный на поправку «официальная длина / длина OSM» на этом
же отрезке реки. Рёбра «порт — порт» идут между соседними портами на реке; глубина —
минимум гарантированных глубин участков на пути, окно — позднее открытие и раннее закрытие.

    python data/yakutia_real/build_ports_edges.py
"""
import csv
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'map'))
import geo_resolver as G  # noqa: E402

RIVERS = ROOT / 'map' / 'geo' / 'rivers'
GEO = json.loads((ROOT / 'yakutia' / 'geo' / 'yakutia_geo.json').read_text(encoding='utf-8'))
GEO_PORTS = {p['id']: p for p in GEO['ports']}
NORM = 'АТ-442-р'

# Оси рек: километр от устья (Лена — от Быкова Мыса, Яна — от мыса Уэдей,
# Индигирка — от в/п Немков, Колыма — от п. Черский). Строки норматива: (строка, км верх, км низ).
SEG = {
    'Лена': [(1365, 3626, 3318), (1366, 3318, 2868), (1367, 2868, 2579), (1368, 2579, 2245), (1369, 2245, 1868),
             (1370, 1868, 1639), (1371, 1639, 1454), (1372, 1454, 1239), (1373, 1239, 875), (1374, 875, 108),
             (1375, 108, 1)],
    'Алдан': [(1382, 1214, 850), (1383, 850, 511), (1384, 511, 405), (1385, 405, 0)],
    'Вилюй': [(1390, 746, 572), (1391, 572, 523), (1392, 523, 338), (1393, 338, 128), (1394, 128, 0)],
    'Яна': [(1397, 729, 626), (1398, 626, 346), (1399, 346, 220), (1400, 220, 22), (1401, 22, 0)],
    'Индигирка': [(1404, 855, 604), (1405, 604, 190), (1406, 190, 0)],
    'Колыма': [(1410, 1022, 863), (1411, 863, 519), (1412, 519, 166), (1413, 166, 0)],
}
OSM_FILE = {'Лена': 'лена', 'Алдан': 'алдан', 'Вилюй': 'вилюй', 'Яна': 'яна', 'Индигирка': 'индигирка', 'Колыма': 'колыма'}
# Порты: (id, река, км или None, (якорь, второй якорь для поправки), направление от якоря: −1 вниз / +1 вверх, примечание)
PORTS = [
    ('Осетрово', 'Лена', 3626, None, None, 'г. Усть-Кут, начало участка 1365'),
    ('Киренск', 'Лена', 3318, None, None, 'граница участков 1365/1366'),
    ('Витим', 'Лена', 2867, None, None, 'нефтебаза, строка 1455: «р. Лена, 2867 км»'),
    ('Пеледуй', 'Лена', None, ('Витим', 'Ленск'), -1, ''),
    ('Ленск', 'Лена', 2655, None, None, 'нефтебаза, строка 1456: «р. Лена, 2655 км»'),
    ('Олёкминск', 'Лена', None, ('Ленск', 'Якутск'), -1, ''),
    ('Покровск', 'Лена', None, ('Якутск', 'Ленск'), 1, ''),
    ('Нижний Бестях', 'Лена', 1653.5, None, None, 'терминал, строка 1453, правый берег напротив Якутска'),
    ('Якутск', 'Лена', 1639, None, None, 'речной порт, строка 1452: «р. Лена, 1639 км»'),
    ('Жатай', 'Лена', 1625, None, None, 'затон судоремзавода, строка 1458: «р. Лена, 1625 км»'),
    ('Устье Алдана', 'Лена', 1454, None, None, 'граница участков 1371/1372'),
    ('Сангар', 'Лена', None, ('Якутск', 'Жиганск'), -1, ''),
    ('Устье Вилюя', 'Лена', 1239, None, None, 'граница участков 1372/1373'),
    ('Жиганск', 'Лена', 875, None, None, 'граница участков 1373/1374'),
    ('Кюсюр', 'Лена', None, ('Жиганск', 'Якутск'), -1, ''),
    ('Быков Мыс', 'Лена', 1, None, None, 'конец участка 1375, выход в море Лаптевых'),
    ('Усть-Мая', 'Алдан', 850, None, None, 'устье р. Мая, граница участков 1382/1383'),
    ('Эльдикан', 'Алдан', None, ('Усть-Мая', 'Джебарики-Хая'), -1, ''),
    ('Джебарики-Хая', 'Алдан', 511, None, None, 'граница участков 1383/1384; угольный разрез'),
    ('Хандыга', 'Алдан', None, ('Джебарики-Хая', 'Усть-Мая'), -1, ''),
    ('Сунтар', 'Вилюй', 746, None, None, 'начало участка 1390'),
    ('Нюрба', 'Вилюй', 572, None, None, 'граница участков 1390/1391'),
    ('Верхневилюйск', 'Вилюй', None, ('Нюрба', 'Вилюйск'), -1, ''),
    ('Вилюйск', 'Вилюй', 338, None, None, 'строка 1392: «г. Вилюйск, 338 км»'),
    ('Кысыл-Сыр', 'Вилюй', None, ('Вилюйск', 'Хатырык-Хомо'), -1, ''),
    ('Хатырык-Хомо', 'Вилюй', 128, None, None, 'граница участков 1393/1394'),
    ('Батагай', 'Яна', 729, None, None, 'начало участка 1397'),
    ('Усть-Куйга', 'Яна', 346, None, None, 'граница участков 1398/1399'),
    ('Нижнеянск', 'Яна', 22, None, None, 'граница участков 1400/1401'),
    ('Белая Гора', 'Индигирка', 604, None, None, 'граница участков 1404/1405'),
    ('Чокурдах', 'Индигирка', 190, None, None, 'граница участков 1405/1406'),
    ('Зырянка', 'Колыма', 863, None, None, 'граница участков 1410/1411'),
    ('Среднеколымск', 'Колыма', 519, None, None, 'граница участков 1411/1412'),
    ('Колымское', 'Колыма', 166, None, None, 'граница участков 1412/1413'),
    ('Зелёный Мыс', 'Колыма', 0, None, None, 'порт у п. Черский, конец участка 1413; принят равным Черскому'),
    ('Тикси', 'море Лаптевых', None, None, None, 'морской порт; не внутренний водный путь'),
]
JUNCTIONS = {'Алдан': 'Устье Алдана', 'Вилюй': 'Устье Вилюя'}


def river(name):
    f = RIVERS / f"{OSM_FILE.get(name, '')}.json"
    return json.loads(f.read_text(encoding='utf-8')) if f.exists() else None


def osm_len(rv, a, b):
    if rv is None:
        return None
    r = G.route_on_river(rv, a, b)
    return r['osm_length_km'] if r else None


# Точек без населённого пункта в OSM: координата берётся из русла с явной пометкой.
COORD_OVERRIDE = {'Быков Мыс': (72.04209, 128.530257, 'ASSUMED: ближайшая к Тикси точка русла Лены в OSM, не координата посёлка')}


def coords(pid, rv):
    if pid in COORD_OVERRIDE:
        return COORD_OVERRIDE[pid]
    if rv and pid in rv.get('places', {}):
        p = rv['places'][pid]
        return p['lat'], p['lon'], 'OpenStreetMap'
    if pid in GEO_PORTS:
        return GEO_PORTS[pid]['lat'], GEO_PORTS[pid]['lon'], 'приближённо (yakutia_geo)'
    return None, None, 'нет'


def main():
    km = {p[0]: p[2] for p in PORTS}
    ports = []
    for pid, rname, k, anchors, sign, note in PORTS:
        rv = river(rname)
        lat, lon, csrc = coords(pid, rv)
        status, basis = ('REAL', f'{NORM}: {note}') if k is not None else ('ASSUMED', note)
        if k is None and anchors:
            a, b = anchors
            d_ap, d_ab = osm_len(rv, a, pid), osm_len(rv, a, b)
            if d_ap and d_ab:
                factor = abs(km[a] - km[b]) / d_ab
                k = round(km[a] + sign * d_ap * factor, 1)
                status = 'DERIVED'
                basis = (f'{a} ({km[a]} км) {"+" if sign > 0 else "−"} {d_ap:.1f} км по руслу OSM × поправка '
                         f'{factor:.3f} (официально {a}—{b} {abs(km[a] - km[b]):.0f} км, по OSM {d_ab:.1f} км)')
            else:
                basis = 'нет русла OSM: километр не определён'
        km[pid] = k
        ports.append(dict(port=pid, river=rname, lat=lat, lon=lon, coord_source=csrc,
                          river_km=k if k is not None else '', km_status=status, km_basis=basis))
    with open(HERE / 'ports.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(ports[0]))
        w.writeheader()
        w.writerows(ports)

    seg = {r['norm_row']: r for r in csv.DictReader(open(HERE / 'waterway_segments.csv', encoding='utf-8'))}
    edges = []
    for rname, rows in SEG.items():
        on = sorted([p for p in ports if p['river'] == rname and p['river_km'] != ''],
                    key=lambda p: -float(p['river_km']))
        if rname in JUNCTIONS:
            on.append(dict(port=JUNCTIONS[rname], river_km=0, km_status='REAL'))
        for up, down in zip(on, on[1:]):
            hi, lo = float(up['river_km']), float(down['river_km'])
            if hi == lo:
                continue
            used = [r for r in rows if r[1] > lo and r[2] < hi]
            depth = min(float(seg[str(r[0])]['guaranteed_depth_m']) for r in used)
            opened = max(seg[str(r[0])]['open_2026'] for r in used)
            closed = min(seg[str(r[0])]['close_2026'] for r in used)
            exact = up['km_status'] == 'REAL' and down['km_status'] == 'REAL'
            edges.append(dict(origin_port=up['port'], destination_port=down['port'], river=rname,
                              distance_km=round(hi - lo, 1), direction_origin_to_destination='downstream',
                              water_type='RIVER', min_guaranteed_depth_m=depth, open_2026=opened, close_2026=closed,
                              norm_rows=';'.join(str(r[0]) for r in used),
                              status='REAL' if exact else 'DERIVED',
                              note='сумма официальных участков' if exact else 'длина через пересчёт по руслу OSM'))
    # Морские переходы: по нормативу их нет. Длина — по дуге большого круга × 1,2 (обход берега), окно — по барам.
    pos = {p['port']: (p['lat'], p['lon']) for p in ports}

    def gc(a, b):
        (la1, lo1), (la2, lo2) = pos[a], pos[b]
        p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
        return 6371 * math.acos(min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))
    sea = [('Быков Мыс', 'Тикси', '07-10', '09-30', 'дельта Лены (строка 1375) и залив Неелова (строка 1376): Быков Мыс — Аргангастах 26 км'),
           ('Тикси', 'Нижнеянск', '07-20', '09-30', 'окно бара Яны (строки 1401–1402)'),
           ('Нижнеянск', 'Чокурдах', '08-01', '09-30', 'окна баров Яны и Индигирки (строки 1402, 1407)'),
           ('Чокурдах', 'Зелёный Мыс', '08-01', '09-30', 'окно бара Индигирки (строка 1407); бар Колымы в нормативе отсутствует')]
    for a, b, o, c, note in sea:
        if None in pos[a] or None in pos[b]:
            continue
        edges.append(dict(origin_port=a, destination_port=b, river='море', distance_km=round(gc(a, b) * 1.2),
                          direction_origin_to_destination='', water_type='SEA', min_guaranteed_depth_m='',
                          open_2026=f'2026-{o}', close_2026=f'2026-{c}', norm_rows='', status='ASSUMED',
                          note=f'длина: дуга большого круга × 1,2; окно: {note}'))
    with open(HERE / 'edges.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(edges[0]))
        w.writeheader()
        w.writerows(edges)
    print(f'портов {len(ports)} (без км: {sum(1 for p in ports if p["river_km"] == "")}), рёбер {len(edges)}')
    for p in ports:
        if p['km_status'] != 'REAL':
            print(f"  {p['port']:15} {p['river_km']!s:8} {p['km_status']:8} {p['km_basis'][:110]}")


if __name__ == '__main__':
    main()
