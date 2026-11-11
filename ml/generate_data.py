"""
Разметка сценариев результатами матмодели Case A3.

  python generate_data.py synthetic --families 260 --variants 2   # обучающие данные
  python generate_data.py archive                                  # архив data/scenarios + demo (проверочные)

Для каждого сценария запускаются FCFS, EARLIEST_DUE, OPTIMIZED через
optimizer.simulate (внутри — аудит validate.audit; при нарушении расчёт
падает). Метка — по целевой функции матмодели, руками не назначается.
Запуск можно прерывать: готовые семейства дописываются в CSV и пропускаются.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from multiprocessing import Pool
from pathlib import Path

import a3_bridge as B

HERE = Path(__file__).resolve().parent
META = ["scenario_id", "family_id", "source", "provenance"]
LABELS = [f"{m}__{s}" for s in B.STRATEGIES for m in ("on_time_t", "total_t", "trips", "violations")] + \
         ["best_strategy", "tied_strategies", "tie_on_tonnage", "full_tie", "best_on_time_t", "total_t"]
FIELDS = META + B.ALL_FEATURES + LABELS


def _row(s, family_id, source):
    row = dict(scenario_id=s["id"], family_id=family_id, source=source, provenance="synthetic")
    row.update(B.extract_features(s))
    row.update(B.run_all_strategies(s))
    return row


def _syn_job(args):
    seed, idx, n_var = args
    base = B.read_config()
    return [_row(s, f"syn_{idx:05d}", "generated") for s in B.generate_family(seed, idx, n_var, base)]


def _arc_job(folder):
    s = B.load_scenario_folder(folder)
    name = Path(folder).name
    # семейство = реплика + внешнее условие (варианты глубины/сроков — одна исходная задача)
    parts = name.split("_")
    fam = "_".join(parts[:-2]) if len(parts) > 3 else name
    return [_row(s, f"arc_{fam}", "archive")]


def _run(jobs, fn, out, workers):
    done = set()
    if out.exists():
        with out.open(encoding="utf-8") as f:
            done = {r["family_id"] for r in csv.DictReader(f)}
    new = not out.exists()
    with out.open("a", encoding="utf-8", newline="") as f, Pool(workers) as p:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        todo = [j for j, fam in jobs if fam not in done]
        for k, rows in enumerate(p.imap_unordered(fn, todo), 1):
            w.writerows(rows)
            f.flush()
            if k % 10 == 0:
                print(f"{k}/{len(todo)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["synthetic", "archive"])
    ap.add_argument("--families", type=int, default=260)
    ap.add_argument("--variants", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20261110)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", type=Path, default=HERE / "data")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.mode == "synthetic":
        jobs = [((a.seed, i, a.variants), f"syn_{i:05d}") for i in range(a.families)]
        _run(jobs, _syn_job, a.out / "dataset_synthetic.csv", a.workers)
        (a.out / "DATA_PROVENANCE.json").write_text(json.dumps(dict(
            provenance="synthetic", seed=a.seed, families=a.families, variants=a.variants,
            generator="generate.make_cargo / make_conditions матмодели; параметры варьирует a3_bridge.RANGES (сценарные допущения)",
            label_source="optimizer.simulate, алгоритм 2.1.0; метка по (вовремя, всего, −рейсы)",
            ranges=B.RANGES), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        root = B.A3_ROOT / "data"
        folders = sorted(p for p in (root / "scenarios").glob("*") if (p / "cargo.csv").exists())
        if (root / "demo/scenario/cargo.csv").exists():
            folders.append(root / "demo/scenario")
        jobs = []
        for p in folders:
            parts = p.name.split("_")
            fam = "_".join(parts[:-2]) if len(parts) > 3 else p.name
            jobs.append((str(p), f"arc_{fam}"))
        # несколько сценариев на семейство: пропуск по scenario_id, а не по family_id
        out = a.out / "dataset_archive.csv"
        done = set()
        if out.exists():
            with out.open(encoding="utf-8") as f:
                done = {r["scenario_id"] for r in csv.DictReader(f)}
        todo = [j for j, _ in jobs if Path(j).name not in done]
        new = not out.exists()
        with out.open("a", encoding="utf-8", newline="") as f, Pool(a.workers) as p:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            for k, rows in enumerate(p.imap_unordered(_arc_job, todo), 1):
                w.writerows(rows); f.flush()
                if k % 10 == 0:
                    print(f"{k}/{len(todo)}", flush=True)


if __name__ == "__main__":
    main()
