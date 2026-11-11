"""
Проверка ML-модуля на данных проекта:  python check_contract.py [--limit N]

Для каждого архивного сценария (data/scenarios/*, data/demo/scenario):
  1. ML даёт рекомендацию, матмодель считает расписание (predict.run_recommended);
  2. ответ проверяется по result.schema.json (JSON Schema Draft 2020-12);
  3. проверяется согласованность: сумма рейсов = метрики, on_time пересчитан,
     violations = 0, JSON без NaN;
  4. метрики сверяются с прямым вызовом матмодели (data/dataset_archive.csv).
Дополнительно: сверка с сохранёнными результатами проекта
(results/comparison_final.csv, results/demo/*.json).
Итог пишется в contract_check.json.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import a3_bridge as B
from predict import run_recommended, validate_contract

HERE = Path(__file__).resolve().parent


def consistency(result):
    m, trips = result["metrics"], result["trips"]
    errs = []
    if len(trips) != m["completed_trips"]:
        errs.append("число рейсов")
    if abs(sum(t["volume_t"] for t in trips) - m["delivered_total_t"]) > 0.01:
        errs.append("сумма рейсов != delivered_total_t")
    if abs(sum(t["volume_t"] for t in trips if t["on_time"]) - m["delivered_on_time_t"]) > 0.01:
        errs.append("сумма on_time != delivered_on_time_t")
    if any(t["on_time"] != (datetime.fromisoformat(t["unloading_end"]) <= datetime.fromisoformat(t["due_date"])) for t in trips):
        errs.append("флаг on_time")
    if m["violations"] != 0:
        errs.append("violations != 0")
    if any("events_json" in t for t in trips):
        errs.append("events_json наружу")
    json.dumps(result, allow_nan=False)
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    root = B.A3_ROOT
    folders = sorted(p for p in (root / "data/scenarios").glob("*") if (p / "cargo.csv").exists())
    if (root / "data/demo/scenario/cargo.csv").exists():
        folders.append(root / "data/demo/scenario")
    if a.limit:
        folders = folders[:a.limit - 1] + folders[-1:]
    truth = {}
    arc = HERE / "data/dataset_archive.csv"
    if arc.exists():
        with arc.open(encoding="utf-8") as f:
            truth = {r["scenario_id"]: r for r in csv.DictReader(f)}

    rows, problems = [], []
    for k, p in enumerate(folders, 1):
        result, rec = run_recommended(p)            # внутри — validate_contract
        errs = consistency(result)
        t = truth.get(p.name)
        if t:
            st = result["strategy"]
            if abs(float(t[f"on_time_t__{st}"]) - result["metrics"]["delivered_on_time_t"]) > 1e-6:
                errs.append("метрики расходятся с прямым вызовом матмодели")
        rows.append(dict(scenario_id=p.name, action=rec["action"], strategy_run=result["strategy"],
                         recommended=rec["recommended_strategy"], confidence=rec["confidence"],
                         on_time_t=result["metrics"]["delivered_on_time_t"],
                         best_strategy=t["best_strategy"] if t else None,
                         best_on_time_t=float(t["best_on_time_t"]) if t else None, errors=errs))
        if errs:
            problems.append((p.name, errs))
        if k % 20 == 0:
            print(f"{k}/{len(folders)}", flush=True)

    # сверка с сохранёнными результатами проекта
    saved = {"comparison_final_rows": 0, "comparison_final_mismatch": 0}
    cf = root / "results/comparison_final.csv"
    mapping = {"baseline": "baseline_central_central", "high_demand": "demand_plus30_central_central",
               "short_navigation": "navigation_minus10_central_central", "shallow": "baseline_shallow_central"}
    if cf.exists() and truth:
        with cf.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["variant"] in mapping:
                    t = truth.get(f"r{r['replicate']}_{mapping[r['variant']]}")
                    if t:
                        saved["comparison_final_rows"] += 1
                        if abs(float(t[f"on_time_t__{r['strategy']}"]) - float(r["delivered_on_time_t"])) > 1e-6:
                            saved["comparison_final_mismatch"] += 1
    demo_ok = None
    demo = root / "results/demo/optimized.json"
    if demo.exists():
        from result_adapter import run_scenario
        ref = json.loads(demo.read_text(encoding="utf-8"))
        validate_contract(ref)
        demo_ok = run_scenario(root / "data/demo/scenario", strategy="OPTIMIZED",
                               scenario_id=ref["scenario_id"]) == ref
    saved["demo_optimized_identical_to_saved"] = demo_ok

    with_truth = [r for r in rows if r["best_on_time_t"] is not None]
    summary = dict(
        scenarios_checked=len(rows),
        schema_valid=len(rows),                      # run_recommended падает при невалидном ответе
        consistency_errors=len(problems),
        actions={x: sum(r["action"] == x for r in rows) for x in sorted({r["action"] for r in rows})},
        strategies_run={x: sum(r["strategy_run"] == x for r in rows) for x in sorted({r["strategy_run"] for r in rows})},
        matches_best_of_three=sum(abs(r["on_time_t"] - r["best_on_time_t"]) < 1e-6 for r in with_truth),
        compared_with_best=len(with_truth),
        saved_results=saved, problems=problems[:20], rows=rows)
    (HERE / "contract_check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=1))
    if problems or saved["comparison_final_mismatch"] or demo_ok is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
