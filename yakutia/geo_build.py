"""Геооснова сети рек Якутии для карты и сетевого сценария.

Берёт линии рек и берега из Natural Earth (общественное достояние),
проецирует их в километры (азимутальная равновеликая проекция с центром
130° в. д., 65° с. ш.), привязывает порты к руслам и режет русло на участки
между соседними портами. Морские переходы между устьями задаются опорными
точками и являются схемой, а не лоцией.

    python yakutia/geo_build.py --ne <каталог с ne_10m_*.geojson>

Результат: yakutia/geo/yakutia_geo.json. Длины участков приближённые:
линия Natural Earth сглаживает меандры, реальный путь по фарватеру длиннее.
"""

import argparse
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'geo' / 'yakutia_geo.json'
LON0, LAT0 = 130.0, 65.0
BBOX = (98.0, 54.0, 168.0, 76.5)
R = 6371.0

# Реки: имя в Natural Earth → русское название, вес линии на карте.
RIVERS = {
    'Lena': ('Лена', 3), 'Aldan': ('Алдан', 2), 'Vilyuy': ('Вилюй', 2), 'Yana': ('Яна', 2),
    'Indigirka': ('Индигирка', 2), 'Kolyma': ('Колыма', 2), 'Olenëk': ('Оленёк', 1),
    'Anabar': ('Анабар', 1), 'Olëkma': ('Олёкма', 1), 'Vitim': ('Витим', 1), 'Maya': ('Мая', 1),
    'Alazeya': ('Алазея', 1), 'Omolon': ('Омолон', 1), 'Moma': ('Мома', 1), 'Adycha': ('Адыча', 1),
    'Markha': ('Марха', 1), 'Uchur': ('Учур', 1), 'Bykovskaya Protoka': ('Быковская протока', 1),
    "Bol’shoy Anyuy": ('Большой Анюй', 1), 'Selennyakh': ('Селеннях', 1), 'Tyung': ('Тюнг', 1),
}

# Порты: id, река, широта, долгота. Координаты населённых пунктов приближённые.
PORTS = [
    ('Осетрово', 'Lena', 56.80, 105.73), ('Киренск', 'Lena', 57.78, 108.11),
    ('Ленск', 'Lena', 60.72, 114.92), ('Олёкминск', 'Lena', 60.37, 120.43),
    ('Покровск', 'Lena', 61.48, 129.13), ('Якутск', 'Lena', 62.03, 129.73),
    ('Устье Алдана', 'Lena', 63.44, 129.55), ('Сангар', 'Lena', 63.92, 127.47),
    ('Устье Вилюя', 'Lena', 64.37, 126.41), ('Жиганск', 'Lena', 66.77, 123.37),
    ('Кюсюр', 'Lena', 70.68, 127.36),
    ('Эльдикан', 'Aldan', 60.80, 135.18), ('Хандыга', 'Aldan', 62.65, 135.57),
    ('Нюрба', 'Vilyuy', 63.28, 118.33), ('Вилюйск', 'Vilyuy', 63.75, 121.62),
    ('Батагай', 'Yana', 67.66, 134.64), ('Усть-Куйга', 'Yana', 70.01, 135.55),
    ('Нижнеянск', 'Yana', 71.44, 136.07),
    ('Белая Гора', 'Indigirka', 68.53, 146.19), ('Чокурдах', 'Indigirka', 70.62, 147.90),
    ('Зырянка', 'Kolyma', 65.73, 150.90), ('Среднеколымск', 'Kolyma', 67.46, 153.70),
    ('Зелёный Мыс', 'Kolyma', 68.75, 161.38),
    ('Тикси', 'sea', 71.64, 128.87),
]
KIND = {'Якутск': 'hub', 'Осетрово': 'hub', 'Тикси': 'sea', 'Устье Алдана': 'junction', 'Устье Вилюя': 'junction'}

# Участки по руслу: соседние порты вниз по течению (первый выше по течению).
RIVER_CHAINS = {
    'Lena': ['Осетрово', 'Киренск', 'Ленск', 'Олёкминск', 'Покровск', 'Якутск', 'Устье Алдана', 'Сангар',
             'Устье Вилюя', 'Жиганск', 'Кюсюр'],
    'Aldan': ['Эльдикан', 'Хандыга', 'Устье Алдана'],
    'Vilyuy': ['Нюрба', 'Вилюйск', 'Устье Вилюя'],
    'Yana': ['Батагай', 'Усть-Куйга', 'Нижнеянск'],
    'Indigirka': ['Белая Гора', 'Чокурдах'],
    'Kolyma': ['Зырянка', 'Среднеколымск', 'Зелёный Мыс'],
}

# Выход к морю и морские переходы: (от, до, опорные точки lat/lon между ними).
SEA_LINKS = [
    ('Кюсюр', 'Тикси', [(71.20, 127.75), (71.75, 128.30)], 'delta'),
    ('Тикси', 'Нижнеянск', [(71.95, 130.60), (72.05, 133.60), (71.80, 135.60)], 'sea'),
    ('Нижнеянск', 'Чокурдах', [(72.10, 138.50), (72.25, 143.50), (71.85, 149.00), (71.30, 150.20)], 'sea'),
    ('Чокурдах', 'Зелёный Мыс', [(71.30, 150.20), (71.20, 154.50), (70.30, 159.50), (69.55, 161.30)], 'sea'),
]
# Где встречаются притоки и Лена: устье притока совпадает с узлом на Лене.
MOUTHS = {'Aldan': 'Устье Алдана', 'Vilyuy': 'Устье Вилюя'}


def project(lon, lat):
    """Азимутальная равновеликая проекция Ламберта, километры, y вверх."""
    l, p = math.radians(lon - LON0), math.radians(lat)
    p0 = math.radians(LAT0)
    k = math.sqrt(2 / (1 + math.sin(p0) * math.sin(p) + math.cos(p0) * math.cos(p) * math.cos(l)))
    x = R * k * math.cos(p) * math.sin(l)
    y = R * k * (math.cos(p0) * math.sin(p) - math.sin(p0) * math.cos(p) * math.cos(l))
    return round(x, 2), round(y, 2)


def hav(a, b):
    (lo1, la1), (lo2, la2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(d))


def length(line):
    return sum(hav(line[i], line[i + 1]) for i in range(len(line) - 1))


def rdp(points, eps):
    if len(points) < 3:
        return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy) or 1e-9
    best, index = 0.0, 0
    for i in range(1, len(points) - 1):
        x, y = points[i]
        d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm
        if d > best:
            best, index = d, i
    if best <= eps:
        return [points[0], points[-1]]
    return rdp(points[:index + 1], eps)[:-1] + rdp(points[index:], eps)


def lines_of(geometry):
    if geometry['type'] == 'LineString':
        return [geometry['coordinates']]
    if geometry['type'] == 'MultiLineString':
        return geometry['coordinates']
    return []


def inside(line):
    return any(BBOX[0] <= x <= BBOX[2] and BBOX[1] <= y <= BBOX[3] for x, y in line)


def merge(parts):
    """Склеить куски одной реки в одну линию (по ближайшим концам)."""
    parts = [list(p) for p in parts if len(p) > 1]
    if not parts:
        return []
    line = parts.pop(max(range(len(parts)), key=lambda i: len(parts[i])))
    while parts:
        best = None
        for i, p in enumerate(parts):
            for rev_p in (False, True):
                q = p[::-1] if rev_p else p
                for at_end in (True, False):
                    d = hav(line[-1], q[0]) if at_end else hav(q[-1], line[0])
                    if best is None or d < best[0]:
                        best = (d, i, q, at_end)
        d, i, q, at_end = best
        parts.pop(i)
        if d > 60:
            continue
        line = line + q if at_end else q + line
    return line


def snap(line, lat, lon):
    return min(range(len(line)), key=lambda i: hav(line[i], (lon, lat)))


def orient_downstream(line, chain, ports):
    first, last = ports[chain[0]], ports[chain[-1]]
    if snap(line, first[0], first[1]) > snap(line, last[0], last[1]):
        line = line[::-1]
    return line


def clip_ring(ring, box):
    """Отсечь многоугольник прямоугольником (Сазерленд — Ходжмен), lon/lat."""
    x0, y0, x1, y1 = box

    def clip(points, inside, cross):
        out = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def at_x(x):
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / ((b[0] - a[0]) or 1e-12))

    def at_y(y):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / ((b[1] - a[1]) or 1e-12), y)

    pts = [tuple(p) for p in ring]
    for inside, cross in ((lambda p: p[0] >= x0, at_x(x0)), (lambda p: p[0] <= x1, at_x(x1)),
                          (lambda p: p[1] >= y0, at_y(y0)), (lambda p: p[1] <= y1, at_y(y1))):
        if not pts:
            break
        pts = clip(pts, inside, cross)
    return pts


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))['features']


def build(ne_dir):
    ne = Path(ne_dir)
    rivers_raw = {}
    for f in load(ne / 'ne_10m_rivers_lake_centerlines.geojson'):
        name = (f['properties'] or {}).get('name')
        if name in RIVERS and f['geometry']:
            rivers_raw.setdefault(name, []).extend(l for l in lines_of(f['geometry']) if inside(l))
    ports = {p[0]: (p[2], p[3]) for p in PORTS}
    river_lines = {n: merge(parts) for n, parts in rivers_raw.items()}

    links = []
    for river, chain in RIVER_CHAINS.items():
        line = orient_downstream(river_lines[river], chain, ports)
        river_lines[river] = line
        idx = [snap(line, *ports[p]) for p in chain]
        if river in MOUTHS:  # устье притока — его последняя точка
            idx[-1] = len(line) - 1
        for (a, ia), (b, ib) in zip(zip(chain, idx), zip(chain[1:], idx[1:])):
            seg = [(ports[a][1], ports[a][0])] + line[ia:ib + 1] + [(ports[b][1], ports[b][0])]
            links.append({'a': a, 'b': b, 'river': RIVERS[river][0], 'kind': 'river', 'down': b, 'geo': seg})
    for a, b, via, kind in SEA_LINKS:
        seg = [(ports[a][1], ports[a][0])] + [(lo, la) for la, lo in via] + [(ports[b][1], ports[b][0])]
        links.append({'a': a, 'b': b, 'river': 'Лена (дельта)' if kind == 'delta' else 'Море Лаптевых / Восточно-Сибирское',
                      'kind': kind, 'down': b if kind == 'delta' else None, 'geo': seg})
    for link in links:
        link['km'] = round(length(link['geo']))
        link['xy'] = [project(lo, la) for lo, la in rdp(link.pop('geo'), 0.01)]

    land = []
    box = (BBOX[0] - 8, BBOX[1] - 4, BBOX[2] + 8, BBOX[3] + 4)
    land_file = ne / 'ne_50m_land.geojson'
    if land_file.exists():
        for f in load(land_file):
            g = f['geometry']
            polys = [g['coordinates']] if g['type'] == 'Polygon' else g['coordinates']
            for poly in polys:
                ring = clip_ring(poly[0], box)
                if len(ring) >= 4:
                    half = len(ring) // 2  # кольцо упрощаем двумя половинами: у замкнутой линии RDP вырождается
                    ring = rdp(ring[:half + 1], 0.02)[:-1] + rdp(ring[half:] + [ring[0]], 0.02)[:-1]
                    if len(ring) >= 4:
                        land.append([project(*p) for p in ring])
    coast = []
    for f in load(ne / 'ne_10m_coastline.geojson'):
        for l in lines_of(f['geometry']):
            if inside(l):
                pts = [(x, y) for x, y in l if BBOX[0] - 6 <= x <= BBOX[2] + 6 and BBOX[1] - 3 <= y <= BBOX[3] + 3]
                pts = rdp(pts, 0.03)
                if len(pts) > 2 and length(pts) > 40:
                    coast.append([project(*p) for p in pts])
    rivers = [{'name': RIVERS[n][0], 'weight': RIVERS[n][1], 'navigable': n in RIVER_CHAINS,
               'xy': [project(*p) for p in rdp(l, 0.02)]}
              for n, l in river_lines.items() if l]
    labels = [{'text': 'Море Лаптевых', 'kind': 'sea', 'xy': project(126.0, 74.6)},
              {'text': 'Восточно-Сибирское море', 'kind': 'sea', 'xy': project(154.0, 73.2)}]
    for n, l in river_lines.items():
        if l and RIVERS[n][1] >= 2:
            mid = l[int(len(l) * (0.42 if n == 'Lena' else 0.5))]
            labels.append({'text': RIVERS[n][0], 'kind': 'river', 'xy': project(*mid)})
    port_rows = [{'id': pid, 'river': RIVERS.get(r, ('Море',))[0], 'lat': la, 'lon': lo, 'xy': project(lo, la),
                  'kind': KIND.get(pid, 'port')} for pid, r, la, lo in PORTS]
    return {
        'schema': 'yakutia-geo/1',
        'projection': {'type': 'lambert_azimuthal_equal_area', 'lon0': LON0, 'lat0': LAT0, 'units': 'km'},
        'source': 'Natural Earth 10m (общественное достояние); координаты портов приближённые; '
                  'морские переходы — схема по опорным точкам',
        'length_note': 'Длины по линии Natural Earth, без меандров; реальный путь по фарватеру длиннее',
        'rivers': rivers, 'coast': coast, 'land': land, 'ports': port_rows, 'links': links, 'labels': labels,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ne', required=True, help='каталог с файлами Natural Earth 10m')
    ap.add_argument('--out', default=str(OUT))
    a = ap.parse_args()
    data = build(a.ne)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    for l in data['links']:
        print(f"{l['a']:>14} → {l['b']:<14} {l['km']:>5} км  {l['kind']}")
    print('реки:', len(data['rivers']), 'берег:', len(data['coast']), 'размер КБ:', Path(a.out).stat().st_size // 1024)


if __name__ == '__main__':
    main()
