"""
Разметка сценариев результатами матмодели Case A3.

  python generate_data.py synthetic --families 260 --variants 2   # обучающие данные
  python generate_data.py archive                                  # архив data/scenarios + demo (проверочные)
  python generate_data.py stress --families 40                     # стресс-режимы a3_bridge.REGIMES (только проверка)
  python generate_data.py universal --families 1000                # произвольные линейные маршруты (a3_bridge.UNIVERSAL)
  python generate_data.py refeaturize [--archive-root <dir>]       # пересчитать только признаки (метки не трогаются)

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
META = ["scenario_id", "family_id", "source", "provenance", "regime"]
LABELS = [f"{m}__{s}" for s in B.STRATEGIES for m in ("on_time_t", "total_t", "trips", "violations")] + \
         ["best_strategy", "tied_strategies", "tie_on_tonnage", "full_tie", "best_on_time_t", "total_t"]
FIELDS = META + B.ALL_FEATURES + LABELS


def _row(s, family_id, source, regime="in_range"):
    row = dict(scenario_id=s["id"], family_id=family_id, source=source, provenance="synthetic", regime=regime)
    row.update(B.extract_features(s))
    row.update(B.run_all_strategies(s))
    return row


def _syn_job(args):
    seed, idx, n_var = args
    base = B.read_config()
    return [_row(s, f"syn_{idx:05d}", "generated") for s in B.generate_family(seed, idx, n_var, base)]


def _uni_job(args):
    seed, idx, n_var = args
    return [_row(s, f"uni_{idx:05d}", "universal", "universal")
            for s in B.generate_universal_family(seed, idx, n_var)]


def _stress_job(args):
    seed, regime, idx, n_var = args
    base = B.read_config()
    return [_row(s, f"{regime}_{idx:05d}", "stress", regime)
            for s in B.generate_family(seed, idx, n_var, base, regime)]


def _arc_job(folder):
    s = B.load_scenario_folder(folder)
    name = Path(folder).name
    # семейство = реплика + внешнее условие (варианты глубины/сроков — одна исходная задача)
    parts = name.split("_")
    fam = "_".join(parts[:-2]) if len(parts) > 3 else name
    return [_row(s, f"arc_{fam}", "archive")]


def _refeat_job(args):
    """Восстановить сценарий (детерминированно по seed) и пересчитать признаки. Стратегии не запускаются."""
    kind, key, seed, n_var = args
    if kind == "folder":
        s = B.load_scenario_folder(key)
        return [(Path(key).name, B.extract_features(s))]
    regime, idx = key
    return [(s["id"], B.extract_features(s))
            for s in B.generate_family(seed, idx, n_var, B.read_config(), regime)]


def _refeaturize(path, jobs, workers):
    """Перезаписывает столбцы признаков; прежние значения общих признаков должны совпасть
    с пересчитанными — это проверка, что восстановлен тот же самый сценарий."""
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with Pool(workers) as p:
        new = {sid: ft for res in p.imap_unordered(_refeat_job, jobs, chunksize=4) for sid, ft in res}
    for r in rows:
        ft = new[r["scenario_id"]]          # KeyError = сценарий не восстановлен, файл не трогаем
        for k, v in ft.items():
            if r.get(k) not in (None, ""):
                assert abs(float(r[k]) - v) <= 1e-6 * max(1.0, abs(v)), (r["scenario_id"], k, r[k], v)
        r.update(ft)
        r.setdefault("regime", "in_range")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{path.name}: {len(rows)} строк, признаки пересчитаны", flush=True)


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
    ap.add_argument("mode", choices=["synthetic", "archive", "stress", "universal", "refeaturize"])
    ap.add_argument("--archive-root", type=Path, default=None,
                    help="каталог с папками архивных сценариев (по умолчанию <A3_ROOT>/data/scenarios)")
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
    elif a.mode == "universal":
        jobs = [((a.seed, i, a.variants), f"uni_{i:05d}") for i in range(a.families)]
        _run(jobs, _uni_job, a.out / "dataset_universal.csv", a.workers)
        (a.out / "UNIVERSAL_PROVENANCE.json").write_text(json.dumps(dict(
            provenance="synthetic", seed=a.seed, families=a.families, variants=a.variants,
            generator="a3_bridge.build_universal: произвольная линейная цепочка участков, флот, глубины, грузы; "
                      "грузы — generate.make_cargo матмодели",
            label_source="optimizer.simulate, алгоритм 2.1.0; метка по (вовремя, всего, −рейсы)",
            ranges=B.UNIVERSAL, max_expected_trips=B.MAX_TRIPS,
            note="диапазоны — сценарные допущения ML-модуля, не данные о реальных реках"),
            ensure_ascii=False, indent=2), encoding="utf-8")
    elif a.mode == "refeaturize":
        uni = a.out / "dataset_universal.csv"
        if uni.exists():
            with uni.open(encoding="utf-8") as f:
                fams = sorted({r["family_id"] for r in csv.DictReader(f)})
            _refeaturize(uni, [("gen", ("universal", int(x.split("_")[-1])), a.seed, a.variants) for x in fams], a.workers)
        syn = a.out / "dataset_synthetic.csv"
        if syn.exists():
            with syn.open(encoding="utf-8") as f:
                fams = sorted({r["family_id"] for r in csv.DictReader(f)})
            _refeaturize(syn, [("gen", (None, int(x.split("_")[-1])), a.seed, a.variants) for x in fams], a.workers)
        stress = a.out / "dataset_stress.csv"
        if stress.exists():
            with stress.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            n_var = max(int(r["scenario_id"].rsplit("_v", 1)[1]) for r in rows) + 1
            keys = sorted({(r["regime"], int(r["family_id"].split("_")[-1])) for r in rows})
            _refeaturize(stress, [("gen", k, a.seed, n_var) for k in keys], a.workers)
        arc = a.out / "dataset_archive.csv"
        root = a.archive_root or B.A3_ROOT / "data/scenarios"
        if arc.exists() and root.exists():
            with arc.open(encoding="utf-8") as f:
                ids = [r["scenario_id"] for r in csv.DictReader(f)]
            demo = B.A3_ROOT / "data/demo/scenario"
            jobs = [("folder", str(demo if (demo.name == i and not (root / i).exists()) else root / i), 0, 0) for i in ids]
            _refeaturize(arc, jobs, a.workers)
        elif arc.exists():
            print(f"архив не найден ({root}); dataset_archive.csv не пересчитан — укажите --archive-root", flush=True)
    elif a.mode == "stress":
        jobs = [((a.seed, r, i, a.variants), f"{r}_{i:05d}") for r in B.REGIMES for i in range(a.families)]
        _run(jobs, _stress_job, a.out / "dataset_stress.csv", a.workers)
        (a.out / "STRESS_PROVENANCE.json").write_text(json.dumps(dict(
            provenance="synthetic", purpose="только проверка; в обучение не идёт", seed=a.seed,
            families_per_regime=a.families, variants=a.variants, regimes=B.REGIMES,
            note="значения режима намеренно за пределами обучающих диапазонов RANGES"),
            ensure_ascii=False, indent=2), encoding="utf-8")
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
