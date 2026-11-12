"""Синтетический демо-сценарий «Лена: Якутск — Жиганск — Кюсюр» для загрузки в приложение.

Реальных рейсовых данных ЛОРП в открытом доступе нет (только годовые агрегаты), поэтому
масштабы взяты из публичных итогов, а рейсы, флот, сроки и глубины — предположения.
Запуск:  python demo_lena/build_lena.py [--search] [--out demo_lena/scenario]
"""
from __future__ import annotations
import argparse, json, random, sys, time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml"):
    sys.path.insert(0, str(p))
from generate import make_cargo, dt, iso            # noqa: E402
import a3_bridge as B                                # noqa: E402

START, END = "2026-06-01", "2026-09-30"

def build(seed=1, dem=1.0, due=(12, 26), depth_end=0.80, plan_scale=None):
    segs = [dict(segment_id="S1", from_node="Якутск", to_node="Жиганск", distance_km=710),
            dict(segment_id="S2", from_node="Жиганск", to_node="Кюсюр", distance_km=640)]
    def unit(tag, k, cap, allowed, df, de=0.5, v_loaded=11.0, v_back=8.0, tph=60):
        j = 1 + 0.04 * ((k * 7) % 5 - 2)
        return dict(unit_id=f"U_{tag}_{k}", barge_id=f"B_{tag}_{k}", tug_id=f"T_{tag}_{k}", project="SYNTHETIC",
                    capacity_t=cap, cargo_type_allowed=allowed, draft_empty_m=de, draft_full_m=df, tug_draft_m=1.1,
                    speed_upstream_kmh=round(v_loaded * j, 2), speed_downstream_kmh=round(v_back * j, 2),
                    handling_tph=tph, reference_capacity_t=cap, data_status="SYNTHETIC",
                    available_from=START + "T00:00:00", available_to="2026-10-01T00:00:00")
    units = [unit("DRY", k, 1800, ["coal", "food", "general"], 2.2) for k in (1, 2, 3, 4)]
    units += [unit("OIL", k, 1500, ["diesel"], 2.1, tph=90) for k in (1, 2)]
    units += [unit("MINI", 1, 600, ["food", "general"], 1.3, v_loaded=13, v_back=10, tph=40)]
    ps = plan_scale or {}
    plans = [dict(plan_id="P_COAL", cargo_type="coal", volume_t=ps.get("coal", 16000) * dem, source_url=""),
             dict(plan_id="P_DIESEL", cargo_type="diesel", volume_t=ps.get("diesel", 9000) * dem, source_url=""),
             dict(plan_id="P_FOOD", cargo_type="food", volume_t=ps.get("food", 2500) * dem, source_url=""),
             dict(plan_id="P_GEN", cargo_type="general", volume_t=ps.get("general", 5000) * dem, source_url="")]
    c = dict(seed=seed, origin="Якутск", destination="Кюсюр", start=START, end=END, segments=segs, plans=plans,
             units=units, underkeel_m=0.25, handling_fixed_hours=6, batch_max_t=700,
             ready_day_fraction_range=[0.02, 0.78], due_days_after_ready=list(due), plan_verification="SYNTHETIC")
    cargo = [x for x in make_cargo(c, seed, 1.0, 0) if x["volume_t"] > 0]
    start, end = dt(START), dt(END)
    n = (end - start).days + 1
    cond = []
    for d in range(n):
        day = start + timedelta(days=d)
        t = d / (n - 1)
        for s, base, shift in ((segs[0], 3.6, 0.0), (segs[1], 3.3, -0.12)):
            depth = base * (1 - (1 - depth_end) * t ** 1.15) + shift
            depth = round(depth, 3)
            cond.append(dict(date=day.date().isoformat(), segment_id=s["segment_id"], usable_depth_m=depth,
                             max_allowed_draft_m=round(depth - 0.25, 3), is_closed=int(d == n - 1) * 0,
                             restriction_reason="Scenario depth; not measured hydrology", source_url="", data_status="ASSUMED"))
    return {"id": "lena_demo", "config": c, "cargo": cargo, "units": units, "conditions": cond, "candidate_limit": None}

def score(s):
    t = time.time(); r = B.run_all_strategies(s)
    return r, time.time() - t

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default=str(Path(__file__).parent / "scenario"))
    ap.add_argument("--seed", type=int, default=1); ap.add_argument("--dem", type=float, default=1.0)
    ap.add_argument("--depth-end", type=float, default=0.80)
    a = ap.parse_args()
    s = build(a.seed, a.dem, depth_end=a.depth_end)
    r, el = score(s)
    print(f"lots={len(s['cargo'])} total={sum(x['volume_t'] for x in s['cargo']):.0f} t, {el:.0f}s")
    print({k: v for k, v in r.items() if k.startswith(('on_time', 'total'))})
