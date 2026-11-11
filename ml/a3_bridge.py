"""
Мост между ML-модулем и матмоделью Case A3 (optimizer.py / generate.py / validate.py).

ML-модуль НЕ содержит собственной физики: рейсы, осадки, глубины, аудит —
только из матмодели команды. Файлы матмодели не изменяются (их хэши
зафиксированы в algorithm_invariants.json).

Папка ml/ должна лежать внутри Case_a3/ (или задайте переменную окружения
A3_ROOT с путём к каталогу, где находятся optimizer.py и result_adapter.py).
"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
from datetime import timedelta
from pathlib import Path
from typing import Dict, List

A3_ROOT = Path(os.environ.get("A3_ROOT", Path(__file__).resolve().parent.parent))
if str(A3_ROOT) not in sys.path:
    sys.path.insert(0, str(A3_ROOT))

from generate import dt, iso, make_cargo, make_conditions  # noqa: E402
from optimizer import STRATEGIES as _STRATS, load, simulate  # noqa: E402

STRATEGIES = list(_STRATS)                 # FCFS, EARLIEST_DUE, OPTIMIZED
DEFAULT_CONFIG = A3_ROOT / "scenario_config.json"
URGENT_WINDOW_DAYS = 14                    # «срочная» партия: срок ≤ 14 сут после готовности [сценарное]

BASE_FEATURES = [
    "cargo_total_t", "cargo_count", "vessel_count", "total_capacity_t",
    "navigation_days", "route_length_km", "min_allowed_draft_m", "avg_trip_days",
    "urgent_cargo_share", "fleet_availability_ratio",
]
EXTRA_FEATURES = [
    "demand_to_throughput",     # спрос / грубая провозная способность флота
    "mean_allowed_draft_m",     # средняя допустимая осадка за открытую навигацию
    "draft_limited_load_frac",  # какую долю грузоподъёмности можно взять при средней глубине
    "mean_due_window_days",     # средний срок «готовность → дедлайн», взвешенный по тоннажу
    "mean_ready_fraction",      # когда в среднем готов груз (0 — начало, 1 — конец окна)
]
ALL_FEATURES = BASE_FEATURES + EXTRA_FEATURES


def read_config(path=DEFAULT_CONFIG) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------------------
# Признаки: только из входов сценария (cargo, units, conditions, config),
# до запуска любой стратегии
# ---------------------------------------------------------------------------
def extract_features(s: Dict) -> Dict[str, float]:
    c, cargo, units, cond = s["config"], s["cargo"], s["units"], s["conditions"]
    start, end1 = dt(c["start"]), dt(c["end"]) + timedelta(days=1)
    open_rows = [r for r in cond if not int(r["is_closed"]) and start <= dt(r["date"]) < end1]
    closed = [dt(r["date"]) for r in cond if int(r["is_closed"]) and dt(r["date"]) >= start]
    close = min(closed) if closed else end1
    nav_days = max((close - start).total_seconds() / 86400, 1e-9)
    allowed = [min(float(r["max_allowed_draft_m"]), float(r["usable_depth_m"]) - c["underkeel_m"]) for r in open_rows]
    min_allowed = min(allowed) if allowed else 0.0
    mean_allowed = sum(allowed) / len(allowed) if allowed else 0.0
    length = sum(seg["distance_km"] for seg in c["segments"])

    cap = sum(float(u["capacity_t"]) for u in units)
    trip_h, avail_h, thr, lim = [], 0.0, 0.0, []
    for u in units:
        k = float(u["capacity_t"])
        ref = float(u.get("reference_capacity_t", k))
        h = 2 * (c["handling_fixed_hours"] + k / float(u["handling_tph"])) \
            + length / float(u["speed_upstream_kmh"]) + length / float(u["speed_downstream_kmh"])
        trip_h.append((k, h))
        a = max(start, dt(u["available_from"]))
        b = min(close, dt(u["available_to"]))
        w = max(0.0, (b - a).total_seconds() / 3600)
        avail_h += w
        span = float(u["draft_full_m"]) - float(u["draft_empty_m"])
        frac = 1.0 if span <= 0 else (mean_allowed - float(u["draft_empty_m"])) / span * ref / max(k, 1e-9)
        frac = min(1.0, max(0.0, frac))
        lim.append((k, frac))
        thr += k * frac * w / h
    avg_trip = sum(k * h for k, h in trip_h) / max(cap, 1e-9) / 24 if units else 0.0
    lim_frac = sum(k * f for k, f in lim) / max(cap, 1e-9) if units else 0.0

    total = sum(float(x["volume_t"]) for x in cargo)
    urgent = wsum = rsum = 0.0
    for x in cargo:
        q = float(x["volume_t"])
        win = (dt(x["due_date"]) - dt(x["ready_date"])).total_seconds() / 86400
        urgent += q if win <= URGENT_WINDOW_DAYS else 0.0
        wsum += q * win
        rsum += q * (dt(x["ready_date"]) - start).total_seconds() / 86400 / nav_days
    t = max(total, 1e-9)
    return {
        "cargo_total_t": round(total, 3),
        "cargo_count": len(cargo),
        "vessel_count": len(units),
        "total_capacity_t": round(cap, 3),
        "navigation_days": round(nav_days, 3),
        "route_length_km": round(length, 3),
        "min_allowed_draft_m": round(min_allowed, 4),
        "avg_trip_days": round(avg_trip, 4),
        "urgent_cargo_share": round(urgent / t, 4),
        "fleet_availability_ratio": round(avail_h / (len(units) * nav_days * 24), 4) if units else 0.0,
        "demand_to_throughput": round(total / max(thr, 1e-9), 4) if units else 0.0,
        "mean_allowed_draft_m": round(mean_allowed, 4),
        "draft_limited_load_frac": round(lim_frac, 4),
        "mean_due_window_days": round(wsum / t, 3),
        "mean_ready_fraction": round(rsum / t, 4),
    }


def load_scenario_folder(folder, config_path=DEFAULT_CONFIG) -> Dict:
    """Каталог сценария в формате проекта (cargo.csv, units.csv, conditions.csv)."""
    return load(Path(folder), read_config(config_path))


# ---------------------------------------------------------------------------
# Разметка: все три стратегии матмодели + её же аудит и целевая функция
# ---------------------------------------------------------------------------
def run_all_strategies(s: Dict) -> Dict:
    """Правило метки = целевая функция матмодели 2.1.0 (лексикографически):
       1) тоннаж вовремя; 2) общий тоннаж; 3) меньше рейсов;
       4) при полном равенстве — более простая стратегия
          (FCFS < EARLIEST_DUE < OPTIMIZED), зафиксировано заранее.
    simulate() сам вызывает аудит и падает при нарушении ограничений."""
    s = dict(s)
    s.setdefault("candidate_limit", None)
    res = {st: simulate(s, st)["metrics"] for st in STRATEGIES}
    key = lambda st: (res[st]["delivered_on_time_t"], res[st]["delivered_total_t"], -res[st]["completed_trips"])
    top = max(key(st) for st in STRATEGIES)
    tied = [st for st in STRATEGIES if key(st) == top]
    best_ot = max(res[st]["delivered_on_time_t"] for st in STRATEGIES)
    tied_ot = [st for st in STRATEGIES if best_ot - res[st]["delivered_on_time_t"] < 1e-6]
    out = {}
    for st in STRATEGIES:
        out[f"on_time_t__{st}"] = res[st]["delivered_on_time_t"]
        out[f"total_t__{st}"] = res[st]["delivered_total_t"]
        out[f"trips__{st}"] = res[st]["completed_trips"]
        out[f"violations__{st}"] = res[st]["violations"]
    out.update(best_strategy=tied[0], tied_strategies="|".join(tied_ot),
               tie_on_tonnage=len(tied_ot) > 1, full_tie=len(tied) > 1,
               best_on_time_t=best_ot, total_t=round(sum(float(x["volume_t"]) for x in s["cargo"]), 3))
    return out


# ---------------------------------------------------------------------------
# Синтетические сценарии: генератор матмодели (make_cargo / make_conditions)
# с варьированием её же параметров. Все диапазоны — сценарные допущения.
# ---------------------------------------------------------------------------
RANGES = dict(
    demand_scale=(0.03, 1.5),      # в архиве: 1.0 и 1.3; малые значения — «флота хватает»
    capacity_scale=(0.6, 1.2),     # в архиве: 1.0 и 0.8
    close_early_days=(0, 25),      # в архиве: 0 и 10
    depth_offset=(-0.25, 0.25),    # в архиве: −0.15, 0, +0.15
    timing=(-15, 15),              # в архиве: −10, 0, +10
    due_lo=(5, 20), due_span=(5, 25),   # в архиве: [12, 30]
    speed_scale=(0.8, 1.25),
    handling_scale=(0.7, 1.5),
    extra_dry=(-1, 2), extra_oil=(0, 1),   # число сухогрузных/наливных составов относительно конфигурации
    oil_share_scale=(0.5, 2.0),
)


def _family(rng: random.Random, base: Dict) -> Dict:
    u = lambda k: rng.uniform(*RANGES[k])
    i = lambda k: rng.randint(*RANGES[k])
    lo = i("due_lo")
    return dict(demand_scale=u("demand_scale") if rng.random() < 0.6 else rng.uniform(0.03, 0.4),
                capacity_scale=u("capacity_scale"), close_early_days=i("close_early_days"),
                depth_offset=u("depth_offset"), timing=i("timing"), due=[lo, lo + i("due_span")],
                speed_scale=u("speed_scale"), handling_scale=u("handling_scale"),
                extra_dry=i("extra_dry"), extra_oil=i("extra_oil"), oil_share_scale=u("oil_share_scale"))


def build_scenario(base: Dict, fam: Dict, cargo_seed: int, sid: str) -> Dict:
    c = copy.deepcopy(base)
    c["due_days_after_ready"] = fam["due"]
    for p in c["plans"]:
        if p["cargo_type"] == "crude_oil":
            p["volume_t"] = round(p["volume_t"] * fam["oil_share_scale"], 3)
    close = dt(c["end"]) + timedelta(days=1 - fam["close_early_days"])
    dry = [u for u in c["units"] if "crude_oil" not in u["cargo_type_allowed"]]
    oil = [u for u in c["units"] if "crude_oil" in u["cargo_type_allowed"]]
    n_dry = max(1, len(dry) + fam["extra_dry"])
    n_oil = max(1, len(oil) + fam["extra_oil"])
    units = []
    for group, n, tag in ((dry, n_dry, "DRY"), (oil, n_oil, "OIL")):
        for k in range(n):
            u = copy.deepcopy(group[k % len(group)])
            u["unit_id"], u["barge_id"], u["tug_id"] = f"U_{tag}_{k+1}", f"B_{tag}_{k+1}", f"T_{tag}_{k+1}"
            u["reference_capacity_t"] = u["capacity_t"]
            u["capacity_t"] = u["capacity_t"] * fam["capacity_scale"]
            u["speed_upstream_kmh"] *= fam["speed_scale"]
            u["speed_downstream_kmh"] *= fam["speed_scale"]
            u["handling_tph"] *= fam["handling_scale"]
            u["available_from"], u["available_to"] = c["start"], iso(close)
            units.append(u)
    cargo = make_cargo(c, cargo_seed, fam["demand_scale"], fam["timing"])
    cargo = [x for x in cargo if x["volume_t"] > 0]
    conditions = make_conditions(c, close, fam["depth_offset"])
    return {"id": sid, "config": c, "cargo": cargo, "units": units,
            "conditions": conditions, "candidate_limit": None}


def generate_family(seed: int, fam_idx: int, n_variants: int, base: Dict) -> List[Dict]:
    rng = random.Random(seed * 1_000_003 + fam_idx)
    fam = _family(rng, base)
    return [build_scenario(base, fam, seed * 7919 + fam_idx * 31 + v, f"syn_{fam_idx:05d}_v{v}")
            for v in range(n_variants)]
