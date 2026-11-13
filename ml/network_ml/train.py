"""Обучение сетевого прогноза: все итоговые метрики ядра по каждой стратегии и выбор стратегии.

Главная цель — доля тоннажа в срок (по ней выбирается стратегия). Остальные метрики (тонны всего, критичные,
рейсы, пробеги, стоимость и т. д.) — отдельные модели из EXTRA_TARGETS на тех же признаках.

Вход — data/dataset.csv из build_dataset.py. В обучение идут только строки с аудитом PASSED текущей версии ядра.
TIME_LIMIT остаётся в данных, помечен признаком `time_limited`, а метрики считаются отдельно для всех строк
и для строк, завершившихся без лимита времени.

Проверка — по сценариям (все стратегии одного сценария либо в обучении, либо в проверке):
- случайные 20% сценариев каждой базы;
- «leave-one-base-out»: обучение на двух базах, проверка на третьей — перенос на другую сеть.

Сравнение — с простой физической оценкой без обучения (`fluid_feasible_share`).

    ml/.venv/bin/python ml/network_ml/train.py
"""
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from network import VERSION  # noqa: E402

STRATEGIES = ("NETWORK_REPOSITION", "NETWORK_SOCIAL", "NETWORK_GREEDY", "NETWORK_FCFS", "NETWORK_COST")
SEEDS = (0, 1, 2, 3, 4)

# Остальные итоговые метрики ядра. Ключ — имя в ответе predict.py (как в result.metrics / financial_evaluation),
# значение — (колонка датасета, масштаб). Цель обучения — величина, делённая на масштаб сценария: так стратегии,
# которые почти ничего не везут (нули), и сети разного размера ложатся в одну шкалу.
# late_tonne_hours не прогнозируется: на отложенных сценариях ошибка больше самой величины.
EXTRA_TARGETS = {
    "delivered_total_t": ("total_t", "demand"),
    "critical_on_time_t": ("critical_on_time_t", "demand"),
    "completed_voyages": ("voyages", "voyages"),
    "repositioning_voyages": ("repositioning_voyages", "voyages"),
    "loaded_km": ("loaded_km", "voyage_km"),
    "empty_km": ("empty_km", "voyage_km"),
    "tonne_km": ("tonne_km", "tonne_km"),
    "resource_busy_hours": ("busy_hours", "fleet_hours"),
    # полная стоимость = затраты на перевозку + цена недовоза; прогнозируются слагаемые, сумму собирает predict.py
    "operating_cost": ("operating_cost_rub", "demand"),
    "undelivered_penalty": ("undelivered_penalty_rub", "demand"),
    "empty_transition_cost": ("empty_cost_rub", "voyage_km"),
}


def scales(f):
    """Масштабы сценария из признаков (f — словарь или DataFrame без префикса f_)."""
    maximum = np.maximum
    voyages = maximum(1.0, f["demand_t"] / maximum(1e-9, f["capacity_t"] / maximum(1, f["n_barges"])))
    km = maximum(1.0, f["mean_path_km"])
    return {"demand": f["demand_t"], "voyages": voyages, "voyage_km": voyages * km, "tonne_km": f["demand_t"] * km,
            "fleet_hours": f["season_days"] * 24 * (f["n_tugs"] + f["n_barges"])}


def frame_scales(df):
    return scales({c[2:]: df[c] for c in df.columns if c.startswith("f_")})


def target_values(df, column, scale):
    return pd.to_numeric(df[column], errors="coerce") / frame_scales(df)[scale]


def fit_target(df, feats, column, scale):
    y = target_values(df, column, scale)
    ok = y.notna()
    return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                         l2_regularization=1.0, random_state=0).fit(design(df[ok], feats), y[ok])


def evaluate_target(model, df, feats, column, scale):
    """Ошибка одной метрики на отложенных сценариях: в своих единицах и как доля суммы факта (WAPE)."""
    y = target_values(df, column, scale)
    d, y = df[y.notna()], y[y.notna()].to_numpy(float)
    pred = np.clip(model.predict(design(d, feats)), 0, None)
    size = frame_scales(d)[scale].to_numpy(float)
    true_nat = y * size
    err = np.abs(pred - y) * size
    return {"rows": int(len(d)), "scale": scale, "mae": round(float(err.mean()), 2),
            "mean_true": round(float(true_nat.mean()), 2),
            "wape": round(float(err.sum() / max(1e-9, np.abs(true_nat).sum())), 4),
            # полуширина интервала ≈80% в долях масштаба
            "q80_abs_residual": round(float(np.quantile(np.abs(pred - y), 0.8)), 5)}


def load(path):
    df = pd.read_csv(path)
    df = df[(df["audit"] == "PASSED") & (df["core_version"] == VERSION)].copy()
    feats = [c for c in df.columns if c.startswith("f_")]
    df["operating_cost_rub"] = (pd.to_numeric(df["cost_rub"], errors="coerce")
                                - pd.to_numeric(df["undelivered_penalty_rub"], errors="coerce"))
    return df, feats


def design(df, feats):
    X = df[feats].copy()
    for s in STRATEGIES:
        X[f"s_{s}"] = (df["strategy"] == s).astype(float)
    return X


def fit(df, feats):
    X, y = design(df, feats), df["on_time_share"].clip(0, 1)
    return [HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                          l2_regularization=1.0, random_state=s).fit(X, y) for s in SEEDS]


def predict(models, df, feats):
    X = design(df, feats)
    p = np.stack([m.predict(X) for m in models])
    return p.mean(0).clip(0, 1), p.std(0)


def evaluate(df, pred, tag):
    d = df.assign(pred=pred)
    err = (d["pred"] - d["on_time_share"]).abs()
    out = {"rows": int(len(d)), "scenarios": int(d["scenario_id"].nunique()),
           "mae_share": round(float(err.mean()), 4),
           "mae_t": round(float((err * d["demand_t"]).mean()), 1),
           "r2": round(float(1 - ((d["pred"] - d["on_time_share"]) ** 2).sum()
                             / max(1e-12, ((d["on_time_share"] - d["on_time_share"].mean()) ** 2).sum())), 4)}
    nolim = d[d["time_limited"] == 0]
    if len(nolim):
        out["mae_share_without_time_limit"] = round(float((nolim["pred"] - nolim["on_time_share"]).abs().mean()), 4)
    lim = d[d["time_limited"] == 1]
    out["time_limited_rows"] = int(len(lim))
    if len(lim):
        out["mae_share_time_limited"] = round(float((lim["pred"] - lim["on_time_share"]).abs().mean()), 4)
    # выбор стратегии: доля сценариев, где выбранная по прогнозу стратегия лучшая, и потери тонн
    hits, regret, regret_share = 0, [], []
    for _, g in d.groupby("scenario_id"):
        best = g["on_time_t"].max()
        chosen = g.loc[g["pred"].idxmax()]
        hits += chosen["on_time_t"] >= best - 1e-6
        regret.append(best - chosen["on_time_t"])
        regret_share.append((best - chosen["on_time_t"]) / max(1e-9, chosen["demand_t"]))
    out["best_strategy_accuracy"] = round(hits / max(1, d["scenario_id"].nunique()), 4)
    out["mean_regret_t"] = round(float(np.mean(regret)), 1)
    out["mean_regret_share"] = round(float(np.mean(regret_share)), 4)
    return {tag: out}


def main():
    df, feats = load(HERE / "data" / "dataset.csv")
    rng = np.random.default_rng(2026)
    scen = df.groupby("base")["scenario_id"].unique()
    test = {s for ids in scen for s in rng.choice(ids, size=max(1, len(ids) // 5), replace=False)}
    tr, te = df[~df["scenario_id"].isin(test)], df[df["scenario_id"].isin(test)]
    report = {"core_version": VERSION, "rows_passed": int(len(df)), "features": feats,
              "time_limited_share": round(float(df["time_limited"].mean()), 4),
              "label": "metrics.delivered_on_time_t / demand_t по стратегии; только аудит PASSED"}
    models = fit(tr, feats)
    pred, _ = predict(models, te, feats)
    metrics = {}
    metrics |= evaluate(te, pred, "holdout_20pct_scenarios")
    metrics |= evaluate(te, te["f_fluid_feasible_share"].clip(0, 1).to_numpy(), "baseline_fluid_holdout")
    nolim_models = fit(tr[tr["time_limited"] == 0], feats)
    metrics |= evaluate(te, predict(nolim_models, te, feats)[0], "holdout_trained_without_time_limit")
    for base in sorted(df["base"].unique()):
        a, b = df[df["base"] != base], df[df["base"] == base]
        metrics |= evaluate(b, predict(fit(a, feats), b, feats)[0], f"leave_out_{base}")
    per_strategy = {}
    d = te.assign(pred=pred)
    for s, g in d.groupby("strategy"):
        per_strategy[s] = {"mae_share": round(float((g["pred"] - g["on_time_share"]).abs().mean()), 4),
                           "mean_true_share": round(float(g["on_time_share"].mean()), 4)}
    report["metrics"], report["per_strategy_holdout"] = metrics, per_strategy
    # остальные метрики ядра: проверка на тех же отложенных сценариях и перенос на третью базу
    resid_on_time = np.abs(pred - te["on_time_share"].clip(0, 1).to_numpy(float))
    all_metrics = {"delivered_on_time_t": {
        "rows": int(len(te)), "scale": "demand", "mae": metrics["holdout_20pct_scenarios"]["mae_t"],
        "mean_true": round(float(te["on_time_t"].mean()), 2),
        "wape": round(float((resid_on_time * te["demand_t"]).sum() / max(1e-9, te["on_time_t"].sum())), 4),
        "q80_abs_residual": round(float(np.quantile(resid_on_time, 0.8)), 4)}}
    transfer = {}
    for name, (column, kind) in EXTRA_TARGETS.items():
        all_metrics[name] = evaluate_target(fit_target(tr, feats, column, kind), te, feats, column, kind)
        for base in sorted(df["base"].unique()):
            a, b = df[df["base"] != base], df[df["base"] == base]
            transfer.setdefault(f"leave_out_{base}", {})[name] = evaluate_target(
                fit_target(a, feats, column, kind), b, feats, column, kind)["wape"]
    report["all_metrics_holdout"] = all_metrics
    report["all_metrics_transfer_wape"] = transfer
    final = fit(df, feats)
    extra = {name: {"model": fit_target(df, feats, column, kind), "scale": kind,
                    "q80": all_metrics[name]["q80_abs_residual"]}
             for name, (column, kind) in EXTRA_TARGETS.items()}
    lo, hi = df[feats].quantile(0.01), df[feats].quantile(0.99)
    joblib.dump({"models": final, "features": feats, "strategies": STRATEGIES, "core_version": VERSION,
                 "on_time_q80": all_metrics["delivered_on_time_t"]["q80_abs_residual"], "extra": extra,
                 "ood_lo": lo.to_dict(), "ood_hi": hi.to_dict()}, HERE / "model.joblib")
    (HERE / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=1))
    print(json.dumps(all_metrics, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
