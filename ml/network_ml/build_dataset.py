"""Набор данных для сетевого ML: возмущённые сценарии Якутии, посчитанные сетевым ядром всеми стратегиями.

Метки ставит только ядро (network.batch.run_jobs), каждый план проходит независимый аудит
(network.adapter.audit_result). Для каждой строки записываются версия ядра, коммит, политика поиска, лимиты,
причина завершения и статус аудита: в обучение идут только PASSED, TIME_LIMIT помечается отдельно.

Базы:
- syn  — синтетическая сеть data/yakutia/yakutia_network.json (17 заявок, 9 буксиров);
- real — реальный сценарий 2026 (data/yakutia_real), подвыборка заявок и флота, чтобы расчёт шёл секунды;
- deep — то же на варианте «углублённые реки» (E1).

    ml/.venv/bin/python ml/network_ml/build_dataset.py --per-base 80 --workers 7
"""
import argparse
import copy
import csv
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "data/yakutia_real"))

from features import features  # noqa: E402
from network import VERSION  # noqa: E402
from network.adapter import audit_result  # noqa: E402
from network.batch import PlanningJob, run_jobs  # noqa: E402
from network.models import Limits  # noqa: E402
from network.search_policy import SearchPolicy  # noqa: E402

STRATEGIES = ("NETWORK_REPOSITION", "NETWORK_SOCIAL", "NETWORK_GREEDY", "NETWORK_FCFS", "NETWORK_COST")
TARIFFS = json.loads((ROOT / "data/yakutia/tariffs_v2.json").read_text(encoding="utf-8"))


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def bases():
    from build_deep_variant import add_sea_fleet, deepen, load
    deep = load()
    deepen(deep, "paths")
    add_sea_fleet(deep)
    real_limits = read(ROOT / "data/yakutia_real/scenario/limits.json")
    return {
        "syn": (read(ROOT / "data/yakutia/yakutia_network.json"), read(ROOT / "data/yakutia/limits.json"), 4),
        "real": (load(), real_limits, 6),
        "deep": (deep, real_limits, 6),
    }


def iso(d):
    return d.isoformat(timespec="seconds")


def perturb(base, rng, name):
    """Случайное, но допустимое изменение сценария. Возвращает (сценарий, параметры)."""
    s = copy.deepcopy(base)
    p = {}
    small = name != "syn"
    p["cargo_frac"] = rng.uniform(0.15, 0.4) if small else rng.uniform(0.6, 1.0)
    p["fleet_frac"] = rng.uniform(0.25, 0.6) if small else rng.uniform(0.5, 1.0)
    p["volume_scale"] = rng.uniform(0.5, 1.6)
    p["date_shift_days"] = rng.randint(-10, 10)
    p["depth_scale"] = rng.uniform(0.85, 1.15)
    cargo = [c for c in s["cargo"] if rng.random() < p["cargo_frac"]] or [rng.choice(s["cargo"])]
    start, end = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
    shift = timedelta(days=p["date_shift_days"])
    for c in cargo:
        c["volume_t"] = round(max(1.0, c["volume_t"] * p["volume_scale"]), 3)
        ready = min(max(datetime.fromisoformat(c["ready_at"]) + shift, start), end)
        due = min(max(datetime.fromisoformat(c["due_at"]) + shift, ready), end)
        c["ready_at"], c["due_at"] = iso(ready), iso(due)
    s["cargo"] = cargo
    keep = lambda rows: [r for r in rows if rng.random() < p["fleet_frac"]] or [rng.choice(rows)]
    s["tugs"], s["barges"] = keep(s["tugs"]), keep(s["barges"])
    tugs, barges = {t["vessel_id"] for t in s["tugs"]}, {b["vessel_id"] for b in s["barges"]}
    s["compatible_pairs"] = [q for q in s["compatible_pairs"] if q["tug_id"] in tugs and q["barge_id"] in barges]
    for e in s["edges"]:
        if e.get("allowed_pairs"):
            e["allowed_pairs"] = [q for q in e["allowed_pairs"] if q["tug_id"] in tugs and q["barge_id"] in barges]
        if e.get("water_type") == "SEA":
            continue
        for d in (e.get("daily_conditions") or {}).values():
            if not d.get("is_closed"):
                d["max_allowed_draft_m"] = round(d["max_allowed_draft_m"] * p["depth_scale"], 3)
                d["usable_depth_m"] = round(d["usable_depth_m"] * p["depth_scale"], 3)
    s["social_requirements"] = []
    return s, p


def commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-base", type=int, default=80)
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", default=str(HERE / "data" / "dataset.csv"))
    ap.add_argument("--resume", action="store_true", help="дописать недостающие строки в существующий файл")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    rev = commit()
    rows_meta, jobs = [], []
    for name, (base, limits, seconds) in bases().items():
        for i in range(a.per_base):
            raw, params = perturb(base, rng, name)
            raw["scenario_id"] = f"{name}_{i:03d}"
            feats = features(raw)
            policy = SearchPolicy(mode="STANDARD", time_seconds=seconds)
            for s in STRATEGIES:
                jobs.append(PlanningJob(raw, s, limits=Limits(**limits), tariffs=TARIFFS, search_policy=policy))
                rows_meta.append(dict(scenario_id=raw["scenario_id"], base=name, strategy=s, params=params,
                                      features=feats, policy=dict(mode="STANDARD", time_seconds=seconds),
                                      limits=limits))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    started, fields, n_ok, done = time.time(), None, 0, 0
    if a.resume and out.exists():
        # сценарии строятся детерминированно по seed, поэтому уже посчитанные строки — префикс списка заданий
        with out.open(newline="", encoding="utf-8") as fh:
            old = list(csv.DictReader(fh))
        if any((r["scenario_id"], r["strategy"]) != (m["scenario_id"], m["strategy"])
               for r, m in zip(old, rows_meta)):
            raise SystemExit("--resume: существующий файл построен с другими параметрами")
        done, n_ok, fields = len(old), sum(r["audit"] == "PASSED" for r in old), list(old[0]) if old else None
    with out.open("a" if done else "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields) if done else None
        for k, (meta, run) in enumerate(zip(rows_meta[done:], run_jobs(jobs[done:], workers=a.workers)), done + 1):
            r = run.result
            try:
                audit = audit_result(r)["schedule_audit"]["status"]
            except Exception as exc:  # noqa: BLE001 — непроверяемый результат не идёт в обучение
                audit = f"ERROR:{type(exc).__name__}"
            m = r["metrics"]
            fin = r.get("financial_evaluation") or {}
            row = dict(scenario_id=meta["scenario_id"], base=meta["base"], strategy=meta["strategy"],
                       core_version=VERSION, algorithm_version=r["algorithm_version"], repo_commit=rev,
                       input_fingerprint=r["input_fingerprint"], policy=json.dumps(meta["policy"]),
                       limits=json.dumps(meta["limits"], sort_keys=True), termination=r["termination_reason"],
                       time_limited=int(r["termination_reason"] == "TIME_LIMIT"), audit=audit,
                       compute_seconds=round(run.compute_seconds, 3), demand_t=m["demand_t"],
                       on_time_t=m["delivered_on_time_t"], total_t=m["delivered_total_t"],
                       on_time_share=m["delivered_on_time_t"] / m["demand_t"] if m["demand_t"] else 0.0,
                       voyages=m["completed_voyages"],
                       cost_rub=fin.get("total_cost"),
                       # все итоговые метрики ядра и денежные итоги — метки для прогноза «все метрики до расчёта»
                       undelivered_t=m["undelivered_t"], critical_on_time_t=m["critical_on_time_t"],
                       loaded_km=m["loaded_km"], empty_km=m["empty_km"], tonne_km=m["tonne_km"],
                       late_tonne_hours=m["late_tonne_hours"], busy_hours=m["resource_busy_hours"],
                       repositioning_voyages=m["repositioning_voyages"],
                       undelivered_penalty_rub=fin.get("undelivered_penalty"),
                       empty_cost_rub=fin.get("empty_transition_cost"),
                       params=json.dumps(meta["params"]),
                       **{f"f_{key}": v for key, v in meta["features"].items()})
            if w is None:
                fields = list(row)
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
            w.writerow(row)
            n_ok += audit == "PASSED"
            if k % 50 == 0:
                fh.flush()
                print(f"{k}/{len(jobs)} за {time.time() - started:.0f} с, аудит PASSED {n_ok}", flush=True)
    (out.parent / "DATASET_PROVENANCE.json").write_text(json.dumps({
        "created": datetime.now().isoformat(timespec="seconds"), "core_version": VERSION, "repo_commit": rev,
        "rows": len(jobs), "audit_passed": n_ok, "per_base": a.per_base, "seed": a.seed,
        "strategies": STRATEGIES, "tariffs": "data/yakutia/tariffs_v2.json",
        "labels": "network.batch.run_jobs → все поля metrics и итоги financial_evaluation; audit_result на каждой строке",
        "note": "Сценарии — возмущения синтетической и реальной сети Якутии; это обучающие варианты, не факты.",
        "seconds": round(time.time() - started, 1)}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(out, f"{time.time() - started:.0f} с")


if __name__ == "__main__":
    main()
