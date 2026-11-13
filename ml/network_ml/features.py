"""Сетевые признаки сценария формата ядра 3.0.0 — считаются до планирования, без запуска стратегий.

Признаки описывают спрос, сеть, флот и физическую выполнимость: какая доля тонн вообще имеет путь, по которому
проходит хоть одна совместимая баржа с буксиром по осадке, сколько тонн требует моря, как далеко флот стоит
от грузов и грубую «жидкостную» оценку провозной способности. Расписание не строится.
"""
import heapq
import math
from collections import defaultdict
from datetime import datetime

TYPES = ("coal", "fuel", "crude_oil", "gas_condensate", "food", "general")


def _dt(s):
    return datetime.fromisoformat(s)


def _edge_stats(e, start, end):
    """Минимальная за открытые дни допустимая осадка, число открытых дней, первый и последний открытый день."""
    days = [(d, c) for d, c in (e.get("daily_conditions") or {}).items() if not c.get("is_closed")]
    if not days:
        cond = e.get("condition") or {}
        if cond.get("is_closed", True):
            return 0.0, 0, None, None
        return cond.get("max_allowed_draft_m", 0.0), (end - start).days + 1, start, end
    drafts = [c.get("max_allowed_draft_m", 0.0) for _, c in days]
    dates = sorted(d for d, _ in days)
    return max(drafts), len(days), _dt(dates[0]), _dt(dates[-1])


def _paths(edges, sources):
    """Кратчайшие расстояния от каждого источника (Дейкстра по длине), с рёбрами пути."""
    adj = defaultdict(list)
    for e in edges:
        adj[e["origin_port"]].append(e)
    out = {}
    for a in sources:
        best, prev, heap = {a: 0.0}, {}, [(0.0, a)]
        while heap:
            d, p = heapq.heappop(heap)
            if d > best[p]:
                continue
            for e in adj[p]:
                q, nd = e["destination_port"], d + e["distance_km"]
                if nd < best.get(q, math.inf):
                    best[q], prev[q] = nd, e
                    heapq.heappush(heap, (nd, q))
        out[a] = (best, prev)
    return out


def _route(tree, a, b):
    best, prev = tree[a]
    if b not in best:
        return None
    path, p = [], b
    while p != a:
        e = prev[p]
        path.append(e)
        p = e["origin_port"]
    return path[::-1]


def features(raw):
    start, end = _dt(raw["start"]), _dt(raw["end"])
    season = max(1.0, (end - start).total_seconds() / 86400)
    edges = raw["edges"]
    stats = {e["edge_id"]: _edge_stats(e, start, end) for e in edges}
    sea = {e["edge_id"] for e in edges if e.get("water_type") == "SEA"}
    cargo = raw["cargo"]
    demand = sum(c["volume_t"] for c in cargo) or 1.0
    tugs, barges = raw["tugs"], raw["barges"]
    tug_by, barge_by = {t["vessel_id"]: t for t in tugs}, {b["vessel_id"]: b for b in barges}
    pairs = [(tug_by[p["tug_id"]], barge_by[p["barge_id"]]) for p in raw["compatible_pairs"]
             if p["tug_id"] in tug_by and p["barge_id"] in barge_by]
    sea_ok = {(p["tug_id"], p["barge_id"]) for e in edges if e.get("water_type") == "SEA"
              for p in e.get("allowed_pairs", [])}

    sources = {c["origin_port"] for c in cargo} | {t["current_port"] for t in tugs}
    tree = _paths(edges, sources)

    f = {"season_days": season, "n_ports": len(raw["ports"]), "n_edges": len(edges),
         "sea_edge_share": len(sea) / max(1, len(edges)), "n_cargo": len(cargo), "demand_t": demand,
         "n_tugs": len(tugs), "n_barges": len(barges), "n_pairs": len(pairs),
         "pairs_per_tug": len(pairs) / max(1, len(tugs)),
         "capacity_t": sum(b["capacity_t"] for b in barges),
         "has_social": float(bool(raw.get("social_requirements")))}
    for t in TYPES:
        f[f"share_{t}"] = sum(c["volume_t"] for c in cargo if c["cargo_type"] == t) / demand
    f["share_critical"] = sum(c["volume_t"] for c in cargo if c.get("critical_category")) / demand
    f["capacity_per_demand"] = f["capacity_t"] / demand
    f["sea_pair_share"] = sum(1 for t, b in pairs if (t["vessel_id"], b["vessel_id"]) in sea_ok) / max(1, len(pairs))

    km_w = win_w = sea_t = nopath_t = full_t = part_t = slack_w = 0.0
    route_cache = {}
    for c in cargo:
        key = (c["origin_port"], c["destination_port"])
        if key not in route_cache:
            route_cache[key] = _route(tree, *key)
        path = route_cache[key]
        v = c["volume_t"]
        if not path:
            nopath_t += v
            continue
        km = sum(e["distance_km"] for e in path)
        draft = min(stats[e["edge_id"]][0] for e in path)
        opens = [stats[e["edge_id"]][2] for e in path]
        closes = [stats[e["edge_id"]][3] for e in path]
        window = 0.0
        if all(opens) and all(closes):
            lo = max(max(opens), _dt(c["ready_at"]))
            hi = min(min(closes), _dt(c["due_at"]))
            window = max(0.0, (hi - lo).total_seconds() / 86400)
        needs_sea = any(e["edge_id"] in sea for e in path)
        sea_t += v * needs_sea
        km_w += v * km
        win_w += v * window
        ok_full = ok_part = False
        slack = 0.0
        for t, b in pairs:
            if c["cargo_type"] not in b["cargo_types_allowed"]:
                continue
            if needs_sea and (t["vessel_id"], b["vessel_id"]) not in sea_ok:
                continue
            if t["draft_empty_m"] > draft or b["draft_empty_m"] > draft:
                continue
            ok_part = True
            speed = max(1.0, min(t["speed_upstream_kmh"], t["speed_downstream_kmh"]))
            transit_days = km / speed / 24
            if b["draft_full_m"] <= draft:
                ok_full = True
            slack = max(slack, min(1.0, window / max(0.1, transit_days)))
            if ok_full and slack >= 1.0:
                break
        slack_w += v * slack
        full_t += v * ok_full
        part_t += v * ok_part
    f.update(nopath_share=nopath_t / demand, sea_demand_share=sea_t / demand,
             mean_path_km=km_w / demand, mean_window_days=win_w / demand,
             draft_full_feasible_share=full_t / demand, draft_any_feasible_share=part_t / demand,
             window_slack=slack_w / demand)

    # Перегон: средняя дистанция от базы буксира до ближайшего порта погрузки.
    origins = {c["origin_port"] for c in cargo}
    dists = []
    for t in tugs:
        best = tree[t["current_port"]][0]
        d = min((best[o] for o in origins if o in best), default=None)
        if d is not None:
            dists.append(d)
    f["mean_reposition_km"] = sum(dists) / len(dists) if dists else 0.0
    f["tugs_without_reach"] = (len(tugs) - len(dists)) / max(1, len(tugs))

    # «Жидкостная» оценка: сколько тонн успевают сделать все баржи при среднем круге и среднем окне.
    speed = sum((t["speed_upstream_kmh"] + t["speed_downstream_kmh"]) / 2 for t in tugs) / max(1, len(tugs))
    cycle_days = max(0.5, 2 * f["mean_path_km"] / max(1.0, speed) / 24 + 2)
    usable = min(len(barges), len(tugs) * 2)
    mean_cap = f["capacity_t"] / max(1, len(barges))
    trips = max(0.0, f["mean_window_days"]) / cycle_days
    f["fluid_share"] = min(3.0, usable * mean_cap * trips / demand)
    f["fluid_feasible_share"] = min(f["draft_any_feasible_share"], f["fluid_share"])
    return {k: float(v) for k, v in f.items()}

