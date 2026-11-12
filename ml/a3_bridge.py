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
import math
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
# «Жидкостная» оценка провозной способности к срокам: грубый посуточный баланс
# «сколько тонн флот успевает привезти до дедлайна» при трёх порядках обслуживания.
# Считается только из входов (без запуска стратегий матмодели, без расписания):
# рейсы не дискретны, очереди и занятость ресурсов не моделируются. Это признак
# для нейросети, а не результат расчёта.
FLUID_FEATURES = [
    "fluid_on_time_share_smart",   # сначала партии, которые ещё успевают к сроку (по сроку)
    "fluid_on_time_share_edf",     # строго по сроку
    "fluid_on_time_share_fcfs",    # по дате готовности
    "fluid_total_share",           # доля спроса, вывозимая до закрытия (любой порядок ≈)
]
V1_FEATURES = BASE_FEATURES + EXTRA_FEATURES          # набор первой сборки (для сравнения)
ALL_FEATURES = V1_FEATURES + FLUID_FEATURES


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
        **fluid_features(s),
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


def fluid_features(s: Dict) -> Dict[str, float]:
    c, cargo, units, cond = s["config"], s["cargo"], s["units"], s["conditions"]
    start, end1 = dt(c["start"]), dt(c["end"]) + timedelta(days=1)
    n_days = (end1 - start).days
    segs = [seg["segment_id"] for seg in c["segments"]]
    length = sum(seg["distance_km"] for seg in c["segments"])
    # допустимая осадка по дням (минимум по участкам); None — закрыто/нет данных
    by_day: Dict[int, Dict[str, float]] = {}
    closed_from = n_days
    for r in cond:
        d = (dt(r["date"]) - start).days
        if d < 0 or d >= n_days:
            continue
        if int(r["is_closed"]):
            closed_from = min(closed_from, d)
        by_day.setdefault(d, {})[r["segment_id"]] = min(float(r["max_allowed_draft_m"]),
                                                        float(r["usable_depth_m"]) - c["underkeel_m"])
    allowed = [min(by_day[d].values()) if d in by_day and all(g in by_day[d] for g in segs) and d < closed_from
               else None for d in range(n_days)]
    lots = [dict(q=float(x["volume_t"]), ready=(dt(x["ready_date"]) - start).total_seconds() / 86400,
                 due=(dt(x["due_date"]) - start).total_seconds() / 86400, typ=x["cargo_type"]) for x in cargo]
    total = max(sum(l["q"] for l in lots), 1e-9)
    # посуточная производительность каждого состава: (т/сут, сутки до конца выгрузки)
    plan = []
    for u in units:
        k, ref = float(u["capacity_t"]), float(u.get("reference_capacity_t", u["capacity_t"]))
        span = float(u["draft_full_m"]) - float(u["draft_empty_m"])
        a = max(0.0, (dt(u["available_from"]) - start).total_seconds() / 86400)
        b = min(float(closed_from), (dt(u["available_to"]) - start).total_seconds() / 86400)
        rows = []
        for d in range(n_days):
            al = allowed[d]
            if al is None or d < a or al < float(u["tug_draft_m"]) or al < float(u["draft_empty_m"]):
                rows.append((0.0, 0.0)); continue
            load = k if span <= 0 else min(k, max(0.0, (al - float(u["draft_empty_m"])) / span * ref))
            hand = c["handling_fixed_hours"] + load / float(u["handling_tph"])
            up = length / float(u["speed_upstream_kmh"])
            trip = (2 * hand + up + length / float(u["speed_downstream_kmh"])) / 24
            if load <= 0 or d + trip > b:
                rows.append((0.0, 0.0)); continue
            rows.append((load / trip, (2 * hand + up) / 24))
        plan.append((set(u["cargo_type_allowed"]), rows))

    def run(policy):
        rem = [l["q"] for l in lots]
        on_time = moved = 0.0
        for d in range(n_days):
            for types, rows in plan:
                cap, lead = rows[d]
                if cap <= 0:
                    continue
                idx = [i for i, l in enumerate(lots) if rem[i] > 1e-9 and l["ready"] < d + 1 and l["typ"] in types]
                if policy == "fcfs":
                    idx.sort(key=lambda i: lots[i]["ready"])
                elif policy == "edf":
                    idx.sort(key=lambda i: lots[i]["due"])
                else:
                    idx.sort(key=lambda i: (lots[i]["due"] < d + lead, lots[i]["due"]))
                for i in idx:
                    if cap <= 1e-9:
                        break
                    x = min(cap, rem[i])
                    rem[i] -= x; cap -= x; moved += x
                    if d + lead <= lots[i]["due"]:
                        on_time += x
        return on_time / total, moved / total

    smart, tot = run("smart")
    return {"fluid_on_time_share_smart": round(smart, 4), "fluid_on_time_share_edf": round(run("edf")[0], 4),
            "fluid_on_time_share_fcfs": round(run("fcfs")[0], 4), "fluid_total_share": round(tot, 4)}


def load_scenario_folder(folder, config_path=DEFAULT_CONFIG) -> Dict:
    """Каталог сценария в формате проекта (cargo.csv, units.csv, conditions.csv)."""
    return load(Path(folder), read_config(config_path))


def pick_best(metrics_by_strategy: Dict[str, Dict]) -> Dict:
    """Лучшая стратегия по целевой функции матмодели 2.1.0 (лексикографически: тоннаж
    вовремя, общий тоннаж, меньше рейсов); при полном равенстве — более простая
    (порядок STRATEGIES). metrics_by_strategy: {стратегия: metrics результата}."""
    names = [st for st in STRATEGIES if st in metrics_by_strategy]
    key = lambda st: (metrics_by_strategy[st]["delivered_on_time_t"], metrics_by_strategy[st]["delivered_total_t"],
                      -metrics_by_strategy[st]["completed_trips"])
    top = max(key(st) for st in names)
    tied = [st for st in names if key(st) == top]
    return dict(best_strategy=tied[0], tied_strategies=tied,
                on_time_t={st: metrics_by_strategy[st]["delivered_on_time_t"] for st in names})


def export_scenario(s: Dict, folder) -> Path:
    """Записать сценарий (в т.ч. сгенерированный) в формат проекта: cargo.csv, units.csv,
    conditions.csv и config.json — чтобы его можно было посчитать через result_adapter."""
    from generate import CARGO_FIELDS, UNIT_FIELDS, write_csv
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    write_csv(folder / "cargo.csv", [{k: x.get(k, "") for k in CARGO_FIELDS} for x in s["cargo"]], CARGO_FIELDS)
    write_csv(folder / "units.csv", [{k: ("|".join(u[k]) if k == "cargo_type_allowed" else u.get(k, ""))
                                      for k in UNIT_FIELDS} for u in s["units"]], UNIT_FIELDS)
    write_csv(folder / "conditions.csv", s["conditions"])
    # матмодель требует объявленные итоги спроса рядом со сценарием
    for name, key in (("expected_cargo_totals.csv", "cargo_type"), ("expected_plan_totals.csv", "plan_id")):
        tot: Dict[str, float] = {}
        for x in s["cargo"]:
            tot[x[key]] = tot.get(x[key], 0.0) + float(x["volume_t"])
        write_csv(folder / name, [{key: k, "volume_t": round(v, 3)} for k, v in sorted(tot.items())], [key, "volume_t"])
    (folder / "config.json").write_text(json.dumps(s["config"], ensure_ascii=False, indent=1), encoding="utf-8")
    return folder


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
    demand_scale=(0.03, 1.8),      # в архиве: 1.0 и 1.3; малые значения — «флота хватает»
    capacity_scale=(0.5, 1.3),     # в архиве: 1.0 и 0.8
    close_early_days=(0, 30),      # в архиве: 0 и 10
    depth_offset=(-0.3, 0.3),      # в архиве: −0.15, 0, +0.15
    timing=(-15, 15),              # в архиве: −10, 0, +10
    due_lo=(5, 20), due_span=(5, 25),   # в архиве: [12, 30]
    speed_scale=(0.8, 1.25),
    handling_scale=(0.7, 1.5),
    extra_dry=(-1, 2), extra_oil=(0, 1),   # число сухогрузных/наливных составов относительно конфигурации
    oil_share_scale=(0.5, 2.0),
    batch_max_t=(150, 6000),       # верхняя граница размера партии (лог-равномерно); в конфигурации 400,
                                   # в demo партии крупные — отсюда число партий ~10…300
    route_scale=(0.7, 1.4),        # множитель длины участков маршрута (в конфигурации 324 км)
)
LOG_UNIFORM = {"batch_max_t"}

# Стресс-режимы из ТЗ: значения намеренно ЗА пределами RANGES, остальные
# параметры — из RANGES. Используются только для проверки (в обучение не идут).
# «Изменение числа пунктов доставки» в линейной цепочке матмодели (один пункт
# назначения) без правки матмодели не задаётся; ближайшие аналоги —
# few_large_lots (число партий) и long_route (длина маршрута).
REGIMES = dict(
    fleet_deficit=dict(capacity_scale=(0.3, 0.45), extra_dry=(-1, -1), extra_oil=(0, 0)),
    fleet_surplus=dict(capacity_scale=(1.4, 1.8), extra_dry=(3, 5), extra_oil=(1, 2)),
    demand_growth=dict(demand_scale=(2.0, 3.0)),
    shallow_bar=dict(depth_offset=(-0.5, -0.35)),
    short_navigation=dict(close_early_days=(35, 50)),
    few_large_lots=dict(batch_max_t=(9000, 20000)),
    long_route=dict(route_scale=(1.6, 2.0)),
)


def _family(rng: random.Random, base: Dict, regime: str = None) -> Dict:
    R = dict(RANGES)
    if regime:
        R.update(REGIMES[regime])

    def u(k):
        lo, hi = R[k]
        return math.exp(rng.uniform(math.log(lo), math.log(hi))) if k in LOG_UNIFORM else rng.uniform(lo, hi)
    i = lambda k: rng.randint(*R[k])
    lo = i("due_lo")
    if regime and "demand_scale" in REGIMES[regime]:
        demand = u("demand_scale")
    else:
        demand = u("demand_scale") if rng.random() < 0.6 else rng.uniform(0.03, 0.4)
    return dict(demand_scale=demand,
                capacity_scale=u("capacity_scale"), close_early_days=i("close_early_days"),
                depth_offset=u("depth_offset"), timing=i("timing"), due=[lo, lo + i("due_span")],
                speed_scale=u("speed_scale"), handling_scale=u("handling_scale"),
                extra_dry=i("extra_dry"), extra_oil=i("extra_oil"), oil_share_scale=u("oil_share_scale"),
                batch_max_t=int(round(u("batch_max_t"))), route_scale=u("route_scale"))


def build_scenario(base: Dict, fam: Dict, cargo_seed: int, sid: str) -> Dict:
    c = copy.deepcopy(base)
    c["due_days_after_ready"] = fam["due"]
    # параметры, которых не было в первой сборке: по умолчанию — как в конфигурации
    c["batch_max_t"] = max(41, int(fam.get("batch_max_t", c["batch_max_t"])))
    for seg in c["segments"]:
        seg["distance_km"] = round(seg["distance_km"] * fam.get("route_scale", 1.0), 3)
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


def generate_family(seed: int, fam_idx: int, n_variants: int, base: Dict, regime: str = None) -> List[Dict]:
    if regime == "universal":
        return generate_universal_family(seed, fam_idx, n_variants)
    tag = f"{regime}_" if regime else "syn_"
    salt = 0 if not regime else 1 + sorted(REGIMES).index(regime)
    rng = random.Random(seed * 1_000_003 + fam_idx + salt * 7_777_777)
    fam = _family(rng, base, regime)
    return [build_scenario(base, fam, seed * 7919 + fam_idx * 31 + v + salt * 104_729, f"{tag}{fam_idx:05d}_v{v}")
            for v in range(n_variants)]


# ---------------------------------------------------------------------------
# Универсальные сценарии: всё пространство входов, которое матмодель умеет
# считать без изменений кода — одна линейная цепочка «пункт отправления →
# пункт назначения» с произвольным числом и длиной участков, флотом, осадками,
# глубинами по участкам, грузами и сроками навигации.
# Все диапазоны — сценарные допущения ML-модуля, не данные о реальных реках.
# Ограничение по времени счёта: ожидаемое число рейсов за навигацию ≤ MAX_TRIPS
# (локальный поиск матмодели растёт быстрее, чем линейно, по числу рейсов).
# ---------------------------------------------------------------------------
UNIVERSAL = dict(
    nav_days=(60, 170), start_shift_days=(0, 45),
    n_segments=(1, 5), route_km=(60, 2500),             # лог-равномерно
    n_classes=(1, 3), units_per_class=(1, 4),
    capacity_t=(100, 3000),                             # лог-равномерно
    draft_empty_m=(0.3, 0.9), draft_span_m=(0.5, 2.2),
    speed_upstream_kmh=(5, 14), downstream_ratio=(1.2, 2.0),
    handling_tph=(15, 200),                             # лог-равномерно
    handling_fixed_hours=(1, 6), underkeel_m=(0.1, 0.3),
    depth_start_ratio=(0.9, 1.6), depth_end_ratio=(0.45, 1.3),   # глубина / (осадка в грузу + запас)
    segment_depth_jitter=(-0.12, 0.12), close_early_days=(0, 20),
    demand_to_capacity=(0.05, 3.0),                     # лог-равномерно: спрос / грубая провозная способность
    target_lots=(8, 220),                               # лог-равномерно
    ready_lo=(0.0, 0.3), ready_hi=(0.5, 0.98), due_lo=(3, 25), due_span=(3, 40),
)
MAX_TRIPS = 220
_CLASS_NAMES = ["bulk", "liquid", "food_general"]       # 'food' в названии даёт приоритет 3 в генераторе матмодели


def _universal_family(rng: random.Random) -> Dict:
    U = UNIVERSAL
    lu = lambda k: math.exp(rng.uniform(math.log(U[k][0]), math.log(U[k][1])))
    un = lambda k: rng.uniform(*U[k])
    ri = lambda k: rng.randint(*U[k])
    for _ in range(200):
        nav = ri("nav_days")
        length = lu("route_km")
        n_seg = ri("n_segments")
        w = [rng.uniform(0.3, 1.0) for _ in range(n_seg)]
        seg_km = [round(length * x / sum(w), 3) for x in w]
        fixed = round(un("handling_fixed_hours"), 2)
        classes, trips = [], 0.0
        for ci in range(ri("n_classes")):
            cap = round(lu("capacity_t"), 1)
            de = round(un("draft_empty_m"), 3)
            df = round(de + un("draft_span_m"), 3)
            up = un("speed_upstream_kmh")
            dn = up * un("downstream_ratio")
            tph = lu("handling_tph")
            units = []
            for _k in range(ri("units_per_class")):
                j = rng.uniform(0.85, 1.15)
                units.append(dict(capacity_t=cap, draft_empty_m=de, draft_full_m=df,
                                  tug_draft_m=round(min(df, rng.uniform(0.4, de + 0.6)), 3),
                                  speed_upstream_kmh=round(up * j, 3), speed_downstream_kmh=round(dn * j, 3),
                                  handling_tph=round(tph * rng.uniform(0.85, 1.15), 3)))
            trip_d = (2 * (fixed + cap / tph) + length / up + length / dn) / 24
            n_trips = len(units) * nav / trip_d
            trips += n_trips
            classes.append(dict(name=_CLASS_NAMES[ci], units=units, rough_capacity_t=cap * n_trips,
                                demand_jitter=rng.uniform(0.6, 1.6), n_plans=rng.randint(1, 2)))
        if 6 <= trips <= MAX_TRIPS:
            break
    else:
        raise RuntimeError("не удалось подобрать семейство в пределах MAX_TRIPS")
    lo = ri("due_lo")
    return dict(nav_days=nav, start_shift=ri("start_shift_days"), seg_km=seg_km, classes=classes,
                handling_fixed_hours=fixed, underkeel_m=round(un("underkeel_m"), 3),
                depth_start_ratio=un("depth_start_ratio"), depth_end_ratio=un("depth_end_ratio"),
                depth_mid=rng.uniform(0.25, 0.75), depth_mid_bump=rng.uniform(-0.15, 0.15),
                seg_jitter=[un("segment_depth_jitter") for _ in seg_km],
                close_early_days=ri("close_early_days"), demand_to_capacity=lu("demand_to_capacity"),
                target_lots=lu("target_lots"), ready=[round(un("ready_lo"), 3), round(un("ready_hi"), 3)],
                due=[lo, lo + ri("due_span")])


def build_universal(fam: Dict, cargo_seed: int, sid: str) -> Dict:
    start = dt("2026-05-15") + timedelta(days=fam["start_shift"])
    end = start + timedelta(days=fam["nav_days"] - 1)
    nodes = [f"N{i}" for i in range(len(fam["seg_km"]) + 1)]
    segments = [dict(segment_id=f"S{i+1}", from_node=nodes[i], to_node=nodes[i + 1], distance_km=km)
                for i, km in enumerate(fam["seg_km"])]
    units, plans, total = [], [], 0.0
    for cl in fam["classes"]:
        for k, u in enumerate(cl["units"], 1):
            tag = cl["name"].upper()
            units.append(dict(u, unit_id=f"U_{tag}_{k}", barge_id=f"B_{tag}_{k}", tug_id=f"T_{tag}_{k}",
                              project="SYNTHETIC", cargo_type_allowed=[cl["name"]],
                              reference_capacity_t=u["capacity_t"], data_status="SYNTHETIC",
                              available_from=iso(start), available_to=iso(end + timedelta(days=1 - fam["close_early_days"]))))
        vol = max(40.0, cl["rough_capacity_t"] * fam["demand_to_capacity"] * cl["demand_jitter"])
        for j in range(cl["n_plans"]):
            v = round(vol / cl["n_plans"], 3)
            plans.append(dict(plan_id=f"P_{cl['name'].upper()}_{j+1}", cargo_type=cl["name"], volume_t=v, source_url=""))
            total += v
    batch_max = int(min(20000, max(41, round(2 * total / fam["target_lots"] - 40))))
    c = dict(seed=cargo_seed, origin=nodes[0], destination=nodes[-1], start=start.date().isoformat(),
             end=end.date().isoformat(), segments=segments, plans=plans, units=units,
             underkeel_m=fam["underkeel_m"], handling_fixed_hours=fam["handling_fixed_hours"],
             batch_max_t=batch_max, ready_day_fraction_range=fam["ready"], due_days_after_ready=fam["due"],
             plan_verification="SYNTHETIC")
    cargo = [x for x in make_cargo(c, cargo_seed, 1.0, 0) if x["volume_t"] > 0]
    # глубины: кусочно-линейный профиль в долях от «осадка в грузу + запас» (средневзвешенно по флоту)
    cap = sum(u["capacity_t"] for u in units)
    ref = sum(u["capacity_t"] * u["draft_full_m"] for u in units) / cap + fam["underkeel_m"]
    close = end + timedelta(days=1 - fam["close_early_days"])
    a, b, m = fam["depth_start_ratio"], fam["depth_end_ratio"], fam["depth_mid"]
    conditions = []
    for d in range(fam["nav_days"]):
        day = start + timedelta(days=d)
        t = d / max(fam["nav_days"] - 1, 1)
        bump = fam["depth_mid_bump"] * (t / m if t <= m else (1 - t) / (1 - m))
        for seg, jit in zip(segments, fam["seg_jitter"]):
            depth = round(max(0.0, ref * (a + (b - a) * t + bump + jit)), 6)
            conditions.append(dict(date=day.date().isoformat(), segment_id=seg["segment_id"], usable_depth_m=depth,
                                   max_allowed_draft_m=round(max(0.0, depth - fam["underkeel_m"]), 6),
                                   is_closed=int(day >= close),
                                   restriction_reason="Scenario closure" if day >= close else "Scenario depth; not measured hydrology",
                                   source_url="", data_status="ASSUMED"))
    return {"id": sid, "config": c, "cargo": cargo, "units": units, "conditions": conditions, "candidate_limit": None}


def generate_universal_family(seed: int, fam_idx: int, n_variants: int) -> List[Dict]:
    rng = random.Random(seed * 1_000_003 + fam_idx + 99_991_111)
    fam = _universal_family(rng)
    return [build_universal(fam, seed * 7919 + fam_idx * 31 + v + 5_000_011, f"uni_{fam_idx:05d}_v{v}")
            for v in range(n_variants)]
