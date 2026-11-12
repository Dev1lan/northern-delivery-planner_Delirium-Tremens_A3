"""
Подбор геометрии маршрута по названию реки и пунктам.

Порядок работы:
  1) библиотека русел map/geo/rivers/*.json (работает без сети);
  2) если реки там нет и разрешён выход в сеть — запрос в OpenStreetMap (Overpass API),
     результат упрощается и сохраняется в библиотеку для следующих запусков.

Маршрут на реке находится так: пункты отправления и назначения ищутся по названию
среди населённых пунктов у реки, привязываются к ближайшей точке русла, и между ними
строится кратчайший путь по линиям реки. Геометрия нужна только для отображения:
длины участков в расчёте берутся из конфигурации сценария.

  python map/geo_resolver.py fetch "Лена" "Индигирка"     # скачать реки в библиотеку
  python map/geo_resolver.py pool                          # скачать набор главных рек, с повторами
  python map/geo_resolver.py list                          # что есть в библиотеке
  python map/geo_resolver.py route "Яна" "Нижнеянск" "Усть-Куйга"

Данные: © участники OpenStreetMap, лицензия ODbL.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
LIB = HERE / "geo" / "rivers"
ATTRIBUTION = "© OpenStreetMap"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter"]
MAX_SNAP_KM = 25.0          # пункт дальше от русла — считаем, что это не та река
PLACE_RADIUS_M = 12000

POOL = ["Лена", "Индигирка", "Колыма", "Алдан", "Вилюй", "Енисей", "Обь", "Иртыш", "Амур", "Волга", "Кама",
        "Северная Двина", "Печора", "Ангара"]

Pt = Tuple[float, float]


def norm(name: str) -> str:
    return re.sub(r"[\s\-–—]+", " ", str(name).casefold().replace("ё", "е")).strip()


def slug(name: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "_", norm(name)).strip("_")


def hav(a: Pt, b: Pt) -> float:
    r = 6371.0088
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
# Упрощение линий (Дуглас — Пекер), с сохранением общих точек разных линий
# ---------------------------------------------------------------------------
def _simplify(pts: List[Pt], tol_km: float, keep: set) -> List[Pt]:
    if len(pts) < 3:
        return pts
    kx = math.cos(math.radians(pts[0][0])) * 111.32
    xy = [(p[1] * kx, p[0] * 110.57) for p in pts]
    out = [False] * len(pts)
    out[0] = out[-1] = True
    for i, p in enumerate(pts):
        if p in keep:
            out[i] = True
    # обязательные точки делят линию на независимые куски — каждый упрощается отдельно
    # (раньше куски добавлялись в стек повторно, и на больших реках работа росла лавинообразно)
    fixed = [i for i, k in enumerate(out) if k]
    stack = list(zip(fixed, fixed[1:]))
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1e-12
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            d = abs((xy[i][0] - ax) * dy - (xy[i][1] - ay) * dx) / n
            if d > best:
                best, bi = d, i
        if best > tol_km:
            out[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, out) if k]


def simplify_ways(ways: List[List[Pt]], tol_km: float = 0.08) -> List[List[Pt]]:
    seen: Dict[Pt, int] = {}
    for w in ways:
        for p in set(w):
            seen[p] = seen.get(p, 0) + 1
    keep = {p for p, c in seen.items() if c > 1}
    return [_simplify(w, tol_km, keep) for w in ways]


# ---------------------------------------------------------------------------
# OpenStreetMap
# ---------------------------------------------------------------------------
def online_allowed() -> bool:
    return os.environ.get("A3_GEO_ONLINE", "1") != "0"


def overpass(query: str, timeout: int = 180, tries: int = 2) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for attempt in range(tries):
        for url in ENDPOINTS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "river-route-map/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:                       # занято, обрыв, не JSON — пробуем следующий сервер
                last = e
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"OpenStreetMap недоступен: {last}")


def _round(p) -> Pt:
    return (round(p["lat"], 6), round(p["lon"], 6))


def fetch_river(name: str) -> dict:
    """Скачать русло реки по названию. При нескольких реках с таким именем берётся самая длинная."""
    q = (f'[out:json][timeout:170];relation["waterway"="river"]["name"="{name}"];out geom;')
    rels = [e for e in overpass(q)["elements"] if e["type"] == "relation"]
    best, best_len = None, 0.0
    for rel in rels:
        ways = [[_round(p) for p in m["geometry"]] for m in rel.get("members", [])
                if m["type"] == "way" and m.get("geometry")]
        length = sum(hav(a, b) for w in ways for a, b in zip(w, w[1:]))
        if length > best_len:
            best, best_len = (rel, ways), length
    if not best:
        raise LookupError(f"В OpenStreetMap нет реки с названием «{name}»")
    rel, ways = best
    places: Dict[str, dict] = {}
    try:
        qp = (f'[out:json][timeout:170];relation({rel["id"]});way(r);'
              f'node(around:{PLACE_RADIUS_M})["place"~"^(city|town|village|hamlet|locality)$"]["name"];out;')
        for e in overpass(qp)["elements"]:
            t = e.get("tags", {})
            places.setdefault(t["name"], dict(lat=round(e["lat"], 5), lon=round(e["lon"], 5), kind=t.get("place"),
                                              population=int(t["population"]) if str(t.get("population", "")).isdigit() else None))
    except Exception:
        pass                                              # без пунктов река всё равно пригодится как фон
    return dict(name=name, osm_relation=rel["id"], length_km=round(best_len, 1), attribution=ATTRIBUTION,
                fetched=time.strftime("%Y-%m-%d"), ways=simplify_ways(ways), places=places, extras=[])


def save_river(river: dict) -> Path:
    LIB.mkdir(parents=True, exist_ok=True)
    path = LIB / f"{slug(river['name'])}.json"
    path.write_text(json.dumps(river, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def library() -> Dict[str, dict]:
    out = {}
    for f in sorted(LIB.glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            out[norm(r["name"])] = r
        except Exception:
            continue
    return out


def get_river(name: str, online: Optional[bool] = None) -> Optional[dict]:
    lib = library()
    if norm(name) in lib:
        return lib[norm(name)]
    if online if online is not None else online_allowed():
        try:
            river = fetch_river(name)
        except Exception:
            return None
        save_river(river)
        return river
    return None


# ---------------------------------------------------------------------------
# Маршрут по руслу
# ---------------------------------------------------------------------------
def _graph(river: dict):
    adj: Dict[Pt, List[Tuple[Pt, float]]] = {}
    for w in river["ways"]:
        pts = [tuple(p) for p in w]
        for a, b in zip(pts, pts[1:]):
            d = hav(a, b)
            adj.setdefault(a, []).append((b, d))
            adj.setdefault(b, []).append((a, d))
    return adj


def _place(river: dict, name: str) -> Optional[Pt]:
    for k, v in river.get("places", {}).items():
        if norm(k) == norm(name):
            return (v["lat"], v["lon"])
    return None


def _path(adj, a: Pt, b: Pt) -> Optional[List[Pt]]:
    dist, prev, heap = {a: 0.0}, {}, [(0.0, a)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == b:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    if b not in dist:
        return None
    out = [b]
    while out[-1] != a:
        out.append(prev[out[-1]])
    return out[::-1]


def route_on_river(river: dict, origin: str, destination: str) -> Optional[dict]:
    o, d = _place(river, origin), _place(river, destination)
    if not o or not d:
        return None
    adj = _graph(river)
    nodes = list(adj)
    so = min(nodes, key=lambda p: hav(p, o))
    sd = min(nodes, key=lambda p: hav(p, d))
    if hav(so, o) > MAX_SNAP_KM or hav(sd, d) > MAX_SNAP_KM:
        return None
    line = _path(adj, so, sd)
    if not line or len(line) < 2:
        return None
    km = [0.0]
    for a, b in zip(line, line[1:]):
        km.append(km[-1] + hav(a, b))
    lats, lons = [p[0] for p in line], [p[1] for p in line]
    pad_lat = max(0.12, 0.12 * (max(lats) - min(lats)))
    pad_lon = max(0.35, 0.35 * (max(lons) - min(lons)))
    box = (min(lats) - pad_lat, max(lats) + pad_lat, min(lons) - pad_lon, max(lons) + pad_lon)
    inside = lambda p: box[0] <= p[0] <= box[1] and box[2] <= p[1] <= box[3]
    context = [dict(kind="river", pts=w) for w in river["ways"] if any(inside(p) for p in w)]
    context += [dict(kind=x["kind"], name=x.get("name"), pts=x["pts"]) for x in river.get("extras", [])
                if any(inside(p) for p in x["pts"])]
    places = {k: v for k, v in river.get("places", {}).items() if inside((v["lat"], v["lon"]))}
    return dict(origin=origin, destination=destination, river=river["name"], attribution=river.get("attribution", ATTRIBUTION),
                osm_length_km=round(km[-1], 1), line=[list(p) for p in line], line_km=[round(x, 3) for x in km],
                context=context, places=places, bbox=box)


def resolve(config: dict, river_name: Optional[str] = None, online: Optional[bool] = None) -> Optional[dict]:
    """Геометрия для сценария: по названию реки (если задано) или перебором библиотеки по названиям пунктов."""
    origin, destination = config.get("origin"), config.get("destination")
    if not origin or not destination:
        return None
    river_name = river_name or config.get("river")
    if river_name:
        river = get_river(river_name, online)
        return route_on_river(river, origin, destination) if river else None
    for river in library().values():
        geo = route_on_river(river, origin, destination)
        if geo:
            return geo
    return None


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["fetch"]:
        for name in a[1:]:
            try:
                r = fetch_river(name)
                print(f"{name}: {r['length_km']} км, линий {len(r['ways'])}, точек {sum(len(w) for w in r['ways'])}, "
                      f"пунктов {len(r['places'])} -> {save_river(r).name}", flush=True)
            except Exception as e:
                print(f"{name}: не получилось — {e}", flush=True)
            time.sleep(3)
    elif a[:1] == ["pool"]:
        # терпеливая загрузка: пропускает готовые реки, между кругами ждёт; серверы OSM бывают перегружены
        names, rounds, pause = a[1:] or POOL, int(os.environ.get("A3_POOL_ROUNDS", "72")), int(os.environ.get("A3_POOL_PAUSE_S", "300"))
        for k in range(rounds):
            todo = [n for n in names if norm(n) not in library()]
            if not todo:
                break
            print(f"[{time.strftime('%H:%M')}] круг {k + 1}: осталось {len(todo)} — {', '.join(todo)}", flush=True)
            for name in todo:
                try:
                    r = fetch_river(name)
                    print(f"  {name}: {r['length_km']} км, точек {sum(len(w) for w in r['ways'])}, пунктов {len(r['places'])} "
                          f"-> {save_river(r).name}", flush=True)
                except Exception as e:
                    print(f"  {name}: не получилось — {str(e)[:90]}", flush=True)
                time.sleep(20)
            time.sleep(pause)
        print(f"готово: в библиотеке {len(library())} рек — {', '.join(r['name'] for r in library().values())}", flush=True)
    elif a[:1] == ["list"]:
        for r in library().values():
            print(f"{r['name']}: {r['length_km']} км, пунктов {len(r['places'])}, получено {r.get('fetched')}")
    elif a[:1] == ["route"] and len(a) == 4:
        g = route_on_river(get_river(a[1]) or {}, a[2], a[3]) if get_river(a[1]) else None
        print(f"{g['osm_length_km']} км по руслу, точек {len(g['line'])}" if g else "маршрут не найден")
    else:
        print(__doc__)
