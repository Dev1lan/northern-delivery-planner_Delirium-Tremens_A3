"""Эксперименты с морским флотом на реальном сценарии Якутии 2026.

Одинаковые заявки, лимиты (scenario/limits.json), тарифы v2 и бюджет поиска
(RESEARCH, recover_undelivered, BUDGET с на эксперимент) у всех вариантов. Стратегия NETWORK_REPOSITION.
Каждый итоговый план проходит независимый аудит network.adapter.audit_result.

    python3 data/yakutia_real/sea_experiments.py            # все эксперименты, ~6 минут
    python3 data/yakutia_real/sea_experiments.py E0 E1P     # выборочно
    python3 data/yakutia_real/sea_experiments.py E0S E1S E1PS E2S   # с социальными минимумами → sea_experiments_social.json

Итог: scenario/sea_experiments_report.json.
"""
import copy
import gzip
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_deep_variant import add_sea_fleet, deepen, load, sea_cargo  # noqa: E402
from build_social_variant import with_social  # noqa: E402
from network.adapter import audit_result, build_result, decode_voyages, run_scenario  # noqa: E402
from network.models import Limits, parse_scenario  # noqa: E402
from network.scheduler import Plan  # noqa: E402
from network.search_policy import SearchPolicy  # noqa: E402

HERE = Path(__file__).resolve().parent / "scenario"
LIMITS = Limits(**json.loads((HERE / "limits.json").read_text(encoding="utf-8")))
TARIFFS = json.loads((ROOT / "data/yakutia/tariffs_v2.json").read_text(encoding="utf-8"))
BUDGET = 45
STRATEGY = "NETWORK_REPOSITION"
RESULTS = HERE / "sea_results"
SAVE = ("E0", "E1", "E1P", "E2", "E2S", "E1S", "E1PS", "E0S", "E0A", "E1A", "E2A", "E1AS")   # полные результаты v3 для сайта и повторного аудита
FLEET_COST = ("UNKNOWN: подтверждённых ставок аренды или содержания судов река—море нет; "
              "в тарифах v2 только часы работы, топливо и сборы")


def settings():
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = None
    return {"core_version": __import__("network").VERSION, "repo_commit": commit, "strategy": STRATEGY,
            "budget_seconds": BUDGET, "policy": {"mode": "RESEARCH", "time_seconds": BUDGET, "recover_undelivered": True},
            "limits": "data/yakutia_real/scenario/limits.json", "tariffs": "data/yakutia/tariffs_v2.json",
            "two_pass_split": "20 с морские заявки + 25 с остальное"}


def finance(raw, result, used):
    """Структура стоимости: эксплуатация, альтернативная доставка недовоза, дополнительный флот, итог."""
    fe = result["financial_evaluation"] or {}
    by_unit = {k: float(v) for k, v in (fe.get("cost_by_unit") or {}).items()}
    # старый контракт: недовоз — статья alternative_delivery внутри total_cost; новый (cost_basis =
    # PLANNED_OPERATIONS): total_cost — только операции, оценка недовоза — estimated_undelivered_cost
    alternative = by_unit.pop("alternative_delivery", None)
    if alternative is None:
        alternative = float(fe.get("estimated_undelivered_cost") or 0.0)
    operating = round(sum(by_unit.values()), 2)
    added = sorted(x["vessel_id"] for x in raw["tugs"] + raw["barges"] if x.get("what_if") == "added_vessel")
    added_used = [v for v in added if v in used]
    known = round(operating + alternative, 2)
    return {"currency": fe.get("currency"), "tariff_status": fe.get("source_status"),
            "operating_rub": operating, "operating_by_item_rub": {k: round(v, 2) for k, v in by_unit.items()},
            "alternative_delivery_rub": round(alternative, 2),
            "extra_fleet": {"vessels": len(added), "vessels_used": len(added_used),
                            "cost_rub": None if added_used else 0.0,
                            "status": FLEET_COST if added_used else "NOT_APPLICABLE: дополнительный флот не задействован"},
            "total_known_rub": known, "total_rub": None if added_used else known}


def policy(seconds, sea=False):
    """sea=True — распределение морского флота внутри ядра (SearchPolicy.sea_fleet_allocation, ветка
    feature/sea-fleet-allocation); варианты с суффиксом A."""
    return SearchPolicy(mode="RESEARCH", time_seconds=seconds, recover_undelivered=True, sea_fleet_allocation=sea)


def plan(raw, seconds=BUDGET, sea=False):
    return run_scenario(raw, STRATEGY, limits=LIMITS, tariffs=TARIFFS, search_policy=policy(seconds, sea))


def kind(name):
    """P — два прохода вне ядра, A — распределение морского флота в ядре, иначе обычное планирование."""
    base = name[:-1] if name.endswith("S") else name
    return base[-1] if base[-1] in "PA" and base not in ("R_ALL", "R_NOSEA") else ""


def two_pass(raw, first_share=0.45):
    """Морские заявки первыми: сначала только они на всём флоте, затем остаток на освободившемся флоте.

    Ничего не запрещается: во втором проходе Р-153 доступны речным заявкам с момента и из порта,
    где закончилась их морская работа, а суда, не занятые морем, свободны весь сезон.
    """
    sea = sea_cargo(raw)
    first = dict(copy.deepcopy(raw), cargo=[c for c in raw["cargo"] if c["cargo_id"] in sea])
    a = plan(first, round(BUDGET * first_share))
    final = {r["vessel_id"]: r for r in a["final_resources"]}
    used = {v[k] for v in a["voyages"] for k in ("tug_id", "barge_id")}
    done = Counter()
    for v in a["voyages"]:
        done[v["cargo_id"]] += v["volume_t"]
    second = copy.deepcopy(raw)
    second["cargo"] = []
    for c in raw["cargo"]:
        left = round(c["volume_t"] - done[c["cargo_id"]], 3)
        if left > 0.001:
            second["cargo"].append(dict(c, volume_t=left))
    for v in second["tugs"] + second["barges"]:
        if v["vessel_id"] in used:
            v["current_port"] = final[v["vessel_id"]]["current_port"]
            v["available_at"] = final[v["vessel_id"]]["available_at"]
    b = plan(second, BUDGET - round(BUDGET * first_share))
    scenario = parse_scenario(raw)
    moves = decode_voyages(a["voyages"] + b["voyages"])
    merged = Plan(STRATEGY, moves, f'TWO_PASS:{a["termination_reason"]}+{b["termination_reason"]}',
                  a["expanded_states"] + b["expanded_states"])
    return build_result(raw, scenario, merged, LIMITS, TARIFFS)


def variant(name):
    if name.endswith("S"):
        return with_social(variant(name[:-1]))
    if name.endswith(("P", "A")) and name not in ("R_ALL", "R_NOSEA"):
        name = name[:-1]
    s = load()
    if name == "E0":
        return s
    if name in ("E1", "R_ALL", "R_NOSEA"):
        deepen(s, "paths")
        add_sea_fleet(s)
    elif name == "E2":
        deepen(s, "paths")
        add_sea_fleet(s)
        add_sea_fleet(s, tugs=16, dry=10, tank=6, bases=("Жатай", "Тикси"))
    elif name == "E3":
        deepen(s, "mouths")
        add_sea_fleet(s)
    if name in ("R_ALL", "R_NOSEA"):
        sea = sea_cargo(s)
        s["cargo"] = [c for c in s["cargo"] if c["cargo_id"] not in sea]
    if name == "R_NOSEA":
        drop = {t["vessel_id"] for t in s["tugs"] if t.get("navigation_permits")}
        s["tugs"] = [t for t in s["tugs"] if t["vessel_id"] not in drop]
        s["compatible_pairs"] = [p for p in s["compatible_pairs"] if p["tug_id"] not in drop]
    return s


TITLES = {
    "E0": "Основной сценарий (реальные глубины, 2 Р-153)",
    "E1": "Углублённые пути + 4 Р-153 (сайт)",
    "E1P": "E1, морские заявки первыми (два прохода)",
    "E2": "E1 + 16 Р-153, 10 барж 16800, 6 барж 16800Н1",
    "E2P": "E2, морские заявки первыми (два прохода)",
    "E3": "Углублены только устья + 4 Р-153",
    "R_ALL": "E1, только речные заявки, весь флот",
    "R_NOSEA": "E1, только речные заявки, без Р-153",
}


def title(name):
    if name.endswith("S"):
        return title(name[:-1]) + "; социальные минимумы № 326-р"
    if name.endswith("A") and name not in ("R_ALL", "R_NOSEA"):
        return title(name[:-1]) + "; ядро: распределение морского флота"
    return TITLES[name]


def summarize(name, raw, result, seconds, base_used=None):
    audit = audit_result(result)
    sea_e = {e["edge_id"] for e in raw["edges"] if e.get("water_type") == "SEA"}
    sea_c = sea_cargo(raw)
    m, fe = result["metrics"], result["financial_evaluation"] or {}
    vs = result["voyages"]
    is_sea = [any(leg["edge_id"] in sea_e for leg in v["loaded"]) for v in vs]
    r153 = [(v, s) for v, s in zip(vs, is_sea) if v["tug_id"].endswith("Р-153")]
    used = {v[k] for v in vs for k in ("tug_id", "barge_id")}
    added = {x["vessel_id"] for x in raw["tugs"] + raw["barges"] if x.get("what_if") == "added_vessel"}
    left = Counter()
    for c in result["cargo_status"]:
        left["sea" if c["cargo_id"] in sea_c else "river"] += c["remaining_t"]
    penalty = fe.get("undelivered_penalty", fe.get("estimated_undelivered_cost"))
    total = fe.get("total_cost")
    if fe.get("cost_basis") == "PLANNED_OPERATIONS" and total is not None:   # прежний смысл: операции + недовоз
        total = round(float(total) + float(penalty or 0), 2)
    return {
        "name": name, "title": title(name), "seconds": round(seconds, 1),
        "scenario_fingerprint": result["input_fingerprint"], "core_version": result["algorithm_version"],
        "termination": result["termination_reason"],
        "demand_t": m["demand_t"], "delivered_total_t": m["delivered_total_t"],
        "delivered_on_time_t": m["delivered_on_time_t"], "undelivered_t": m["undelivered_t"],
        "sea_delivered_t": round(sum(v["volume_t"] for v, s in zip(vs, is_sea) if s), 3),
        "undelivered_sea_t": round(left["sea"], 3), "undelivered_river_t": round(left["river"], 3),
        "social": result["requirements_status"], "social_deficit_t": m["social_deficit_t"],
        "requirements": [{k: q.get(k) for k in ("requirement_id", "required_t", "satisfied", "deficit_t")}
                         for q in result["requirements"]],
        "critical_on_time_t": m["critical_on_time_t"],
        "voyages": len(vs), "repositioning_voyages": m.get("repositioning_voyages"),
        "r153_voyages": len(r153), "r153_tugs_used": len({v["tug_id"] for v, _ in r153}),
        "r153_tugs_total": sum(1 for t in raw["tugs"] if t["vessel_id"].endswith("Р-153")),
        "r153_river_t": round(sum(v["volume_t"] for v, s in r153 if not s), 3),
        "r153_sea_t": round(sum(v["volume_t"] for v, s in r153 if s), 3),
        "tugs_used": len(used & {t["vessel_id"] for t in raw["tugs"]}),
        "barges_used": len(used & {b["vessel_id"] for b in raw["barges"]}),
        "added_vessels": len(added), "added_vessels_used": len(used & added),
        "newly_used_vs_E0": None if base_used is None else len(used - base_used),
        "finance": finance(raw, result, used),
        "total_cost_rub": total, "alternative_delivery_rub": penalty,
        "operating_cost_rub": None if total is None else round(float(total) - float(penalty or 0), 2),
        "audit": audit["schedule_audit"]["status"], "audit_violations": len(audit["schedule_audit"]["violations"]),
        "_used": used,
    }


def main(names):
    rows = []
    base_used = None
    for name in names:
        raw = variant(name)
        t = time.time()
        k = kind(name)
        result = two_pass(raw) if k == "P" else plan(raw, sea=k == "A")
        row = summarize(name, raw, result, time.time() - t, base_used)
        if name in SAVE:
            RESULTS.mkdir(exist_ok=True)
            result["experiment"] = {k: row[k] for k in ("name", "title", "seconds", "termination", "audit")}
            with gzip.open(RESULTS / f"{name}.json.gz", "wt", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        if name.rstrip("S") == "E0":
            base_used = row["_used"]
        rows.append(row)
        print(f'{name}: {row["seconds"]} с, {row["delivered_on_time_t"]:.0f} т в срок, морем {row["sea_delivered_t"]:.0f} т, '
              f'Р-153 {row["r153_voyages"]} рейсов ({row["r153_river_t"]:.0f} т река / {row["r153_sea_t"]:.0f} т море), '
              f'аудит {row["audit"]}', flush=True)
    base = next((r for r in rows if r["name"].rstrip("S") == "E0"), None)
    for r in rows:
        r.pop("_used")
        if base and r is not base and r["operating_cost_rub"] is not None:
            dt = r["delivered_total_t"] - base["delivered_total_t"]
            dc = r["operating_cost_rub"] - base["operating_cost_rub"]
            r["extra_t_vs_E0"] = round(dt, 3)
            r["operating_cost_per_extra_t_rub"] = round(dc / dt, 2) if dt > 0 else None
    social = all(n.endswith("S") for n in names)
    out = HERE / (OUT_NAME or ("sea_experiments_social.json" if social else "sea_experiments_report.json"
                  if len(names) > 3 else f"sea_experiments_{'_'.join(names)}.json"))
    out.write_text(json.dumps({"settings": settings(), "experiments": rows}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print(out)


OUT_NAME = None

if __name__ == "__main__":
    if sys.argv[1:2] == ["--allocation"]:     # сравнение с распределением морского флота в ядре
        OUT_NAME = "sea_experiments_allocation.json"
        sys.argv = sys.argv[:1] + (sys.argv[2:] or ["E0", "E0A", "E1", "E1P", "E1A", "E2", "E2A", "E1S", "E1PS", "E1AS"])
    main(sys.argv[1:] or ["E0", "E1", "E1P", "E2", "E2P", "E3", "R_ALL", "R_NOSEA"])
