"""Generate grouped network scenarios and independently replay their labels."""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import random

from network import VERSION
from network.adapter import audit_result
from network.batch import PlanningJob, run_jobs
from network.costs import UNITS
from network.models import Limits, parse_scenario
from network.scheduler import COST, FCFS, GREEDY, REPOSITION, SOCIAL
from network.search_policy import SearchPolicy
from result_adapter import fingerprint

from .features import extract

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIMITS = Limits(beam_width=3, candidate_limit=8, max_actions=22, max_states=75,
                        max_route_expansions=100, repair_attempts=2, repair_states=12)
DEFAULT_POLICY = SearchPolicy(top_k_pairs_per_cargo=5, max_candidates_per_state=8,
                              max_detailed_checks_per_iteration=45)


def core_version():
    paths = sorted((ROOT / "network").glob("*.py"))
    paths += [ROOT / name for name in ("optimizer.py", "result_adapter.py", "scenario_validation.py",
                                       "social_requirements.py")]
    digest = sha256()
    for path in paths:
        digest.update(path.name.encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return {"contract": VERSION, "sha256": digest.hexdigest()}


def strategies(tariffs):
    return (FCFS, GREEDY, SOCIAL, REPOSITION) + ((COST,) if tariffs is not None else ())


def synthetic(seed, family, variant=0):
    """Variants reuse topology but perturb the operating scenario."""
    shape = random.Random(seed)
    rng = random.Random((seed << 8) + variant)
    n = shape.randint(5, 11) if family != "large" else shape.randint(24, 37)
    if family == "line":
        n = shape.randint(3, 9)
    if family == "unseen":
        n = shape.randint(9, 19)
    ports = [f"{family}-{i}" for i in range(n)]
    if family in ("line", "basins", "large"):
        links = [(i - 1, i) for i in range(1, n)]
    elif family == "unseen":
        links = [(0, i) for i in range(1, n)] + [(i, i + 1) for i in range(1, n - 1, 2)]
    else:
        links = [(shape.randrange(i), i) for i in range(1, n)]
        links += [(i, (i + 2) % n) for i in range(0, n, 3)]
    start = datetime(2026, 7, 1)
    days = rng.randint(7, 25)
    end = start + timedelta(days=days)
    stamp = lambda days: (start + timedelta(days=days)).isoformat()
    fleet_count = rng.randint(2, 7) if family != "large" else rng.randint(12, 25)
    marine = family in ("basins", "large", "unseen")
    pairs = [{"tug_id": f"T{i}", "barge_id": f"B{j}"} for i in range(fleet_count)
             for j in range(fleet_count) if i == j or rng.random() < .12]
    approved = {i for i in range(fleet_count) if rng.random() < .65}
    tugs, barges = [], []
    for i in range(fleet_count):
        home = rng.choice(ports)
        access = {"vessel_class": "COASTAL" if i in approved else "INLAND",
                  "navigation_permits": [{"area": "COAST", "status": "SCENARIO_ASSUMPTION",
                                          "source": "Synthetic generator"}] if i in approved else []}
        base = dict(current_port=home, available_at=stamp(rng.choice([0, 0, 1, 2])),
                    available_to=end.isoformat(), **access)
        tugs.append(dict(base, vessel_id=f"T{i}", draft_empty_m=rng.uniform(.4, 1.0),
                         speed_upstream_kmh=rng.uniform(7, 13), speed_downstream_kmh=rng.uniform(10, 17)))
        barges.append(dict(base, vessel_id=f"B{i}", current_port=home if rng.random() < .8 else rng.choice(ports),
                           draft_empty_m=.45, draft_full_m=rng.uniform(1.0, 2.1),
                           capacity_t=rng.randrange(60, 430, 10), handling_tph=rng.randint(35, 110),
                           cargo_types_allowed=["food", "fuel"] if rng.random() < .75 else ["food"]))
    edges = []
    for index, (a, b) in enumerate(links):
        kind = ("SEA" if marine and index % 4 == 2 else
                rng.choice(("RIVER", "RIVER", "RIVER", "CANAL", "ESTUARY")))
        length = rng.randint(7, 155) * (2 if kind == "SEA" else 1)
        depth = rng.uniform(.8, 3.1)
        for side, (p, q) in enumerate(((a, b), (b, a))):
            if side and rng.random() < .12:
                continue
            row = dict(edge_id=f"E{index}-{side}", origin_port=ports[p], destination_port=ports[q],
                       direction="upstream" if not side else "downstream", distance_km=length,
                       water_type=kind, condition=dict(usable_depth_m=depth, max_allowed_draft_m=depth-.15,
                                                      is_closed=rng.random() < .06),
                       navigation_windows=[dict(start=stamp(rng.choice([0, 0, 1, 2])),
                                                end=stamp(days-rng.choice([0, 0, 1, 3])))])
            if kind == "SEA":
                row.update(navigation_area="COAST", allowed_vessel_classes=["COASTAL"],
                           allowed_pairs=[p for p in pairs
                                          if int(p["tug_id"][1:]) in approved and int(p["barge_id"][1:]) in approved])
            edges.append(row)
    load = rng.uniform(.45, 2.8)
    cargo = []
    for i in range(rng.randint(4, 15) if family != "large" else rng.randint(15, 30)):
        origin = rng.choice([v["current_port"] for v in tugs]) if rng.random() < .7 else rng.choice(ports)
        nearby = [e["destination_port"] for e in edges if e["origin_port"] == origin]
        destination = rng.choice(nearby or ports) if rng.random() < .7 else rng.choice(ports)
        if destination == origin:
            destination = ports[(ports.index(origin) + 1) % n]
        ready = rng.randint(0, days // 2)
        kind = rng.choice(["food", "food", "fuel"])
        cargo.append(dict(cargo_id=f"C{i}", cargo_type=kind, origin_port=origin, destination_port=destination,
                          volume_t=round(rng.uniform(45, 540)*load, 3), ready_at=stamp(ready),
                          due_at=stamp(min(days, ready+rng.uniform(.4, days/2))),
                          priority=int(kind == "food"), critical_category="food" if kind == "food" else None))
        cargo[-1]["due_at"] = datetime.fromisoformat(cargo[-1]["due_at"]).replace(microsecond=0).isoformat()
    requirements = []
    if rng.random() < .8 and any(c["cargo_type"] == "food" for c in cargo):
        requirements.append(dict(requirement_id="MIN", cargo_types=["food"],
                                 required_t=round(sum(c["volume_t"] for c in cargo if c["cargo_type"] == "food")
                                                  * rng.uniform(.15, .9), 3),
                                 deadline=stamp(days*.75), basis="delivered_by_deadline", source_status="SYNTHETIC"))
    raw = dict(scenario_id=f"{family}-{seed}-{variant}", data_status="SYNTHETIC", start=start.isoformat(),
               end=end.isoformat(), ports=ports, edges=edges, tugs=tugs, barges=barges, compatible_pairs=pairs,
               cargo=cargo, social_requirements=requirements, underkeel_m=.12,
               allow_marine_reposition=rng.random() < .85)
    rates = dict(zip(UNITS, (25, 20, 950, 400, 90, 90, 2400, 200, 15)))
    tariffs = None
    if rng.random() < .85:
        fuel_basis = rng.choice(["km", "hour"])
        tariffs = dict(tariff_version=2, currency="RUB", fuel_basis=fuel_basis, source_status="SYNTHETIC",
                       rates={k: dict(value=round(rates[k]*rng.uniform(.65, 1.55), 2),
                                      unit=fuel_basis if unit == "fuel_basis" else unit,
                                      source="Synthetic generator", source_status="SYNTHETIC")
                              for k, unit in UNITS.items()})
        if rng.random() < .55:
            tariffs["alternative_delivery"] = {
                kind: dict(value=rng.randint(300, 2200), unit="tonne",
                           source="Synthetic generator", source_status="SYNTHETIC")
                for kind in ("food", "fuel") if rng.random() < .8}
    return raw, tariffs


def cases(seed, families, variants):
    for family_index, family in enumerate(("line", "branched", "basins", "large", "unseen")):
        for i in range(families):
            group_seed = seed + (family_index + 1) * 100000 + i
            split = ("test" if family == "unseen" else
                     {0: "test", 1: "validation", 2: "calibration"}.get(i % 7, "train"))
            for variant in range(variants):
                raw, tariffs = synthetic(group_seed, family, variant)
                yield raw, tariffs, family, group_seed, split
    from .yana import network_fixture

    yield network_fixture(), None, "yana_pilot", None, "test"
    for path in sorted((ROOT / "data/network_demo").glob("*.json")):
        yield json.loads(path.read_text(encoding="utf-8")), None, "network_examples", None, "test"
    stress = ROOT / "data/yakutia_demo/yakutia_stress.json"
    real = ROOT / "data/yakutia_real/scenario/yakutia_real_2026_demo.json"
    site = ROOT / "data/yakutia"
    prices = json.loads((site / "tariffs_v2.json").read_text(encoding="utf-8-sig"))
    for path, tariffs in ((stress, None), (site / "yakutia_network.json", prices), (real, prices)):
        yield json.loads(path.read_text(encoding="utf-8-sig")), tariffs, path.stem, None, "test"


def label(raw, tariffs, family, seed, split, *, version, limits=DEFAULT_LIMITS,
          policy=DEFAULT_POLICY, workers=1):
    parsed = parse_scenario(raw)
    features = extract(parsed, tariffs)
    choices = strategies(tariffs)
    jobs = [PlanningJob(raw, strategy, limits, tariffs, policy) for strategy in choices]
    for run in run_jobs(jobs, workers=workers):
        result = run.result
        report = audit_result(result)
        passed = report["schedule_audit"]["status"] == "PASSED"
        target = None
        if passed:
            m = result["metrics"]
            priced = result["financial_evaluation"]
            target = dict(total_t=m["delivered_total_t"], on_time_t=m["delivered_on_time_t"],
                          on_time_share=m["delivered_on_time_t"] / m["demand_t"] if m["demand_t"] else 0.0,
                          undelivered_t=m["undelivered_t"],
                          social_failed=(any(not row["satisfied"] for row in report["requirements"])
                                         if parsed.requirements else None),
                          social_deficit_t=m["social_deficit_t"], cost=float(priced["total_cost"]) if priced else None)
        yield dict(scenario_id=raw["scenario_id"], family=family, seed=seed, split=split,
                   group=f"{family}:{seed}", input_fingerprint=fingerprint(raw),
                   configuration_fingerprint=fingerprint({k: v for k, v in raw.items() if k != "scenario_id"}),
                   tariff_fingerprint=fingerprint(tariffs) if tariffs is not None else None,
                   currency=tariffs["currency"] if tariffs is not None else None,
                   core=version, strategy=result["strategy"], limits=asdict(limits), policy=asdict(policy),
                   time_budget_s=policy.budget_seconds, termination=result["termination_reason"],
                   audit=report["schedule_audit"], targets=target, features=features,
                   compute_seconds=run.compute_seconds, expanded_states=result["expanded_states"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=37)
    parser.add_argument("--families", type=int, default=36)
    parser.add_argument("--variants", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args(argv)
    if args.families < 7 or args.variants < 1:
        parser.error("At least seven families of each topology and one variant are required")
    policy = SearchPolicy(**{**asdict(DEFAULT_POLICY), "time_seconds": args.timeout})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_suffix(".tmp")
    count = Counter()
    version = core_version()
    with temp.open("w", encoding="utf-8") as output:
        for index, case in enumerate(cases(args.seed, args.families, args.variants), 1):
            for row in label(*case, version=version, policy=policy, workers=args.workers):
                output.write(json.dumps(row, allow_nan=False) + "\n")
                count[(row["split"], row["termination"], row["audit"]["status"])] += 1
            if index % 25 == 0:
                print(f"Labeled {index} scenarios", flush=True)
    temp.replace(args.out)
    for key, value in sorted(count.items()):
        print(*key, value)


if __name__ == "__main__":
    main()
