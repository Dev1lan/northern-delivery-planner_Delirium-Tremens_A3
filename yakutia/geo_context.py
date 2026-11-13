"""Фоновая география вокруг сети для карты: суша, берег, крупные реки и озёра.

Рисуется под сетью приглушённо, чтобы карта не обрывалась на рамке региона.
Проекция та же, что в geo_build.py (Ламберт, центр 130° в. д., 65° с. ш., км).
На расчёт не влияет: участки и длины берутся только из yakutia_geo.json.

    python yakutia/geo_context.py --ne <каталог с ne_10m_*.geojson и ne_50m_land.geojson>

Результат: yakutia/geo/context_geo.json.
"""

import argparse
import json
from pathlib import Path

from geo_build import clip_ring, lines_of, load, project, rdp

HERE = Path(__file__).resolve().parent
OUT = HERE / 'geo' / 'context_geo.json'
# Широкая рамка lon/lat: Таймыр — Чукотка, Байкал — полюс. Долготы за 180° сдвинуты на +360.
BOX = (60.0, 40.0, 200.0, 84.0)


def east(coords):
    """Чукотка за антимеридианом: если все точки в западном полушарии, сдвинуть на +360."""
    return [(x + 360, y) if all(p[0] < 0 for p in coords) else (x, y) for x, y in coords]


def in_box(p, pad=0.0):
    return BOX[0] - pad <= p[0] <= BOX[2] + pad and BOX[1] - pad <= p[1] <= BOX[3] + pad


def split_inside(line):
    """Куски линии внутри рамки."""
    out, cur = [], []
    for p in line:
        if in_box(p):
            cur.append(p)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [c for c in out if len(c) > 1]


def simplify_ring(ring, eps):
    half = len(ring) // 2  # у замкнутой линии RDP вырождается — упрощаем двумя половинами
    return rdp(ring[:half + 1], eps)[:-1] + rdp(ring[half:] + [ring[0]], eps)[:-1]


def polygons(features, eps, keep=lambda props: True):
    out = []
    for f in features:
        g = f['geometry']
        if not g or not keep(f['properties'] or {}):
            continue
        polys = [g['coordinates']] if g['type'] == 'Polygon' else g['coordinates']
        for poly in polys:
            ring = clip_ring(east([tuple(p) for p in poly[0]]), BOX)
            if len(ring) >= 4:
                ring = simplify_ring(ring, eps)
                if len(ring) >= 4:
                    out.append([project(*p) for p in ring])
    return out


def build(ne_dir):
    ne = Path(ne_dir)
    land = polygons(load(ne / 'ne_50m_land.geojson'), 0.03)
    lakes = polygons(load(ne / 'ne_10m_lakes.geojson'), 0.02, lambda p: (p.get('scalerank') or 99) <= 4)
    coast = []
    for f in load(ne / 'ne_10m_coastline.geojson'):
        for l in lines_of(f['geometry']):
            for part in split_inside(east([tuple(p) for p in l])):
                part = rdp(part, 0.03)
                if len(part) > 2:
                    coast.append([project(*p) for p in part])
    rivers = []
    for f in load(ne / 'ne_10m_rivers_lake_centerlines.geojson'):
        props = f['properties'] or {}
        rank = props.get('scalerank') or 99
        if rank > 6 or props.get('featurecla') not in ('River', 'Lake Centerline'):
            continue
        for l in lines_of(f['geometry']):
            for part in split_inside(east([tuple(p) for p in l])):
                part = rdp(part, 0.03)
                if len(part) > 1:
                    rivers.append({'w': 2 if rank <= 3 else 1, 'xy': [project(*p) for p in part]})
    return {
        'schema': 'yakutia-geo-context/1',
        'projection': {'type': 'lambert_azimuthal_equal_area', 'lon0': 130.0, 'lat0': 65.0, 'units': 'km'},
        'source': 'Natural Earth (общественное достояние)',
        'land': land, 'lakes': lakes, 'coast': coast, 'rivers': rivers,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ne', required=True, help='каталог с файлами Natural Earth')
    ap.add_argument('--out', default=str(OUT))
    a = ap.parse_args()
    data = build(a.ne)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('суша:', len(data['land']), 'озёра:', len(data['lakes']), 'берег:', len(data['coast']),
          'реки:', len(data['rivers']), 'размер КБ:', Path(a.out).stat().st_size // 1024)


if __name__ == '__main__':
    main()
