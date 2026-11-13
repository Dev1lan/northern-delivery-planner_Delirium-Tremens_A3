"""Compare two audited planning runs with identical search limits and time policy."""

import argparse
from dataclasses import replace
from collections import Counter
import json
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from network.models import Limits
from network.search_policy import SearchPolicy

from .dataset import DEFAULT_LIMITS, cases, core_version, label, synthetic
from .model import DEFAULT_ARTIFACT, NetworkModel
from .planning import plan


def benchmark(artifact_path, *, seed=37, families=42, variants=2, unseen_limit=12):
    model = NetworkModel.load(artifact_path)
    limits = Limits(**model.meta["limits"])
    policy = SearchPolicy(**model.meta["policy"])
    runs = []
    for raw, tariffs, family, _, split in cases(seed, families, variants):
        if split != "test":
            continue
        if family == "unseen" and sum(r["family"] == family for r in runs) >= unseen_limit:
            continue
        pair = {}
        for enabled in (False, True) if len(runs) % 2 == 0 else (True, False):
            start = perf_counter()
            result, advice = plan(raw, tariffs=tariffs, limits=limits, policy=policy,
                                  use_ml=enabled, allow_uncertain=True, artifact_path=artifact_path)
            elapsed = perf_counter() - start
            metrics = result["metrics"]
            finance = result["financial_evaluation"]
            pair["assisted" if enabled else "ordinary"] = {
                "seconds": elapsed, "strategy": result["strategy"],
                "on_time_t": metrics["delivered_on_time_t"], "total_t": metrics["delivered_total_t"],
                "social_deficit_t": metrics["social_deficit_t"],
                "requirements_met": sum(r["satisfied"] for r in result["requirements"]),
                "cost": float(finance["total_cost"]) if finance else None,
                "audit": result["schedule_audit"]["status"], "termination": result["termination_reason"],
                "forecast_status": advice["status"],
            }
        runs.append(dict(pair, scenario=raw["scenario_id"], family=family))
    summary = {}
    for mode in ("ordinary", "assisted"):
        rows = [run[mode] for run in runs]
        summary[mode] = {
            "audited_pass": sum(r["audit"] == "PASSED" for r in rows),
            "median_seconds": median(r["seconds"] for r in rows),
            "total_seconds": sum(r["seconds"] for r in rows),
            "on_time_t": sum(r["on_time_t"] for r in rows),
            "total_t": sum(r["total_t"] for r in rows),
            "social_deficit_t": sum(r["social_deficit_t"] for r in rows),
            "requirements_met": sum(r["requirements_met"] for r in rows),
            "priced_cost": sum(r["cost"] for r in rows if r["cost"] is not None),
            "forecast_statuses": dict(Counter(r["forecast_status"] for r in rows)),
            "termination": dict(Counter(r["termination"] for r in rows)),
        }
    summary["strategy_changes"] = sum(r["ordinary"]["strategy"] != r["assisted"]["strategy"] for r in runs)
    return {"limits": model.meta["limits"], "policy": model.meta["policy"], "assisted_allows_uncertain": True, "summary": summary, "runs": runs}


def budget_sensitivity():
    """Audit the same scenarios under two deterministic search profiles."""
    pairs = []
    version = core_version()
    families = ("line", "branched", "basins", "large", "unseen")
    wider = replace(DEFAULT_LIMITS, max_actions=44, max_states=150)
    for index, family in enumerate(families, 1):
        seed = 37 + index * 100000
        raw, _ = synthetic(seed, family)
        ordinary = label(raw, None, family, seed, "test", version=version)
        increased = label(raw, None, family, seed, "test", version=version, limits=wider)
        for before, after in zip(ordinary, increased, strict=True):
            delta = after["targets"]["on_time_share"] - before["targets"]["on_time_share"]
            pairs.append({"family": family, "strategy": before["strategy"], "share_delta": delta})
    return {"pairs": pairs, "changed": sum(abs(p["share_delta"]) > 1e-9 for p in pairs),
            "mean_absolute_delta": mean(abs(p["share_delta"]) for p in pairs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = benchmark(args.artifact)
    report["budget_sensitivity"] = budget_sensitivity()
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
