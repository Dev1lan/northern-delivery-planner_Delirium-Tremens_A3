"""
Обучение ML-модуля на результатах матмодели Case A3.

  python train.py                    # бэкенд auto: PyTorch, если установлен, иначе sklearn
  python train.py --backend torch    # создаёт model.pt

Данные: data/dataset_synthetic.csv (обучение/валидация/тест, разбиение по
семействам) и data/dataset_archive.csv (архивные сценарии проекта —
только проверка, в обучении не участвуют).

Модели:
  * классификатор стратегии: MLP 2 скрытых слоя, ReLU, CrossEntropy, Adam;
    baseline: DummyClassifier и RandomForest;
  * прогноз доли тоннажа вовремя по каждой стратегии: MLP-регрессор
    (baseline: среднее по train и RandomForest).
"""
from __future__ import annotations

import argparse
import copy
import os
import time
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestClassifier, RandomForestRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import log_loss
from sklearn.model_selection import GroupShuffleSplit
from sklearn.neural_network import MLPClassifier, MLPRegressor

from a3_bridge import STRATEGIES
from model_common import (Preprocessor, KnnOOD, ood_flags, FEATURE_SETS, ShareEnsemble,
                          torch_available, build_torch_mlp, save_json)

HERE = Path(__file__).resolve().parent
S2I = {s: i for i, s in enumerate(STRATEGIES)}
SHARE_COLS = [f"on_time_t__{s}" for s in STRATEGIES]


def split_by_family(df, seed):
    a, rest = next(GroupShuffleSplit(1, test_size=0.30, random_state=seed).split(df, groups=df["family_id"]))
    r = df.iloc[rest]
    b, c = next(GroupShuffleSplit(1, test_size=0.50, random_state=seed).split(r, groups=r["family_id"]))
    tr, va, te = df.iloc[a], r.iloc[b], r.iloc[c]
    fams = [set(x.family_id) for x in (tr, va, te)]
    assert not (fams[0] & fams[1] or fams[0] & fams[2] or fams[1] & fams[2]), "утечка семейств"
    return [x.reset_index(drop=True) for x in (tr, va, te)]


def evaluate(df, pred):
    chosen = np.array([df[f"on_time_t__{s}"].iloc[i] for i, s in enumerate(pred)])
    best, total = df["best_on_time_t"].values, df["total_t"].values
    regret = best - chosen
    pct = 100 * regret / np.maximum(total, 1e-9)
    tie_ok = np.array([p in str(t).split("|") for p, t in zip(pred, df["tied_strategies"])])
    return dict(n=int(len(df)),
                accuracy=round(float((pred == df["best_strategy"].values).mean()), 4),
                accuracy_tie_aware=round(float(tie_ok.mean()), 4),
                tonnage_vs_best_ratio=round(float(chosen.sum() / max(best.sum(), 1e-9)), 4),
                regret_t_mean=round(float(regret.mean()), 1),
                regret_t_max=round(float(regret.max()), 1),
                regret_pct_of_demand_mean=round(float(pct.mean()), 3),
                share_regret_over_1pct_demand=round(float((pct > 1).mean()), 4))


def train_torch(Xtr, ytr, Xva, yva, hidden, seed, max_epochs=600, patience=40):
    import torch
    import torch.nn as nn
    torch.manual_seed(seed)
    model = build_torch_mlp(Xtr.shape[1], hidden)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)
    lossf = nn.CrossEntropyLoss()
    Xt, yt = torch.tensor(Xtr), torch.tensor(ytr, dtype=torch.long)
    Xv, yv = torch.tensor(Xva), torch.tensor(yva, dtype=torch.long)
    best, state, bad, ep = 1e9, None, 0, 0
    g = torch.Generator().manual_seed(seed)
    for ep in range(max_epochs):
        model.train()
        perm = torch.randperm(len(Xt), generator=g)
        for i in range(0, len(Xt), 64):
            idx = perm[i:i + 64]
            opt.zero_grad()
            lossf(model(Xt[idx]), yt[idx]).backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            vl = float(lossf(model(Xv), yv))
        if vl < best - 1e-4:
            best, state, bad = vl, copy.deepcopy(model.state_dict()), 0
        else:
            bad += 1
            if bad >= patience:
                break
    model.load_state_dict(state)
    model.eval()

    def proba(X):
        with torch.no_grad():
            return torch.softmax(model(torch.tensor(X)), dim=1).numpy()
    return model, proba, dict(best_val_loss=round(best, 4), epochs=ep + 1)


def train_sklearn(Xtr, ytr, Xva, yva, hidden, seed, max_epochs=600, patience=40):
    m = MLPClassifier(hidden_layer_sizes=(hidden, hidden), activation="relu", solver="adam",
                      alpha=1e-4, batch_size=64, learning_rate_init=1e-3, random_state=seed)
    classes = np.arange(len(STRATEGIES))
    best, best_m, bad, ep = 1e9, None, 0, 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        for ep in range(max_epochs):
            m.partial_fit(Xtr, ytr, classes=classes)
            vl = log_loss(yva, m.predict_proba(Xva), labels=classes)
            if vl < best - 1e-4:
                best, best_m, bad = vl, copy.deepcopy(m), 0
            else:
                bad += 1
                if bad >= patience:
                    break
    return best_m, best_m.predict_proba, dict(best_val_loss=round(best, 4), epochs=ep + 1)


def train_share_mlp(Xtr, Ytr, Xva, Yva, seed, base=None):
    """Ансамбль MLP-регрессоров (2 скрытых слоя, ReLU, L-BFGS + L2). Ширина и сила
    регуляризации выбираются по val-выборке. base — столбцы жидкостной оценки:
    тогда сеть учит поправку к ней (см. ShareEnsemble)."""
    probe = ShareEnsemble([], base=base)
    Ttr = probe.target(Xtr, Ytr)
    fit = lambda h, alpha, rs: MLPRegressor(hidden_layer_sizes=(h, h), activation="relu", solver="lbfgs",
                                            alpha=alpha, max_iter=3000, random_state=rs).fit(Xtr, Ttr)
    best, best_cfg = 1e9, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for h in (32, 64):
            for alpha in (0.1, 0.3, 1.0, 3.0):
                vl = float(np.abs(ShareEnsemble([fit(h, alpha, seed)], base=base).predict(Xva) - Yva).mean())
                if vl < best:
                    best, best_cfg = vl, dict(hidden=h, alpha=alpha, val_mae_single=round(vl, 4))
        # ансамбль из 5 сетей выбранной конфигурации (разные seed)
        ens = ShareEnsemble([fit(best_cfg["hidden"], best_cfg["alpha"], seed + k) for k in range(5)], base=base)
        ens.fit_interval(Xtr, Ytr, Xva, Yva, seed)
    best_cfg.update(ensemble_size=len(ens.models), interval_coverage_target=ens.coverage,
                    target=("поправка к жидкостной оценке" if base else "доля напрямую"),
                    interval_method="conformalized quantile regression (HistGradientBoosting + поправка по val)",
                    interval_conformal_q=[round(float(x), 4) for x in ens.q],
                    val_mae_ensemble=round(float(np.abs(ens.predict(Xva) - Yva).mean()), 4))
    return ens, best_cfg


def shares(df):
    return df[SHARE_COLS].values / np.maximum(df["total_t"].values[:, None], 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data/dataset_synthetic.csv"))
    ap.add_argument("--archive", default=str(HERE / "data/dataset_archive.csv"))
    ap.add_argument("--stress", default=str(HERE / "data/dataset_stress.csv"))
    ap.add_argument("--universal", default=str(HERE / "data/dataset_universal.csv"),
                    help="универсальные сценарии (произвольные линейные маршруты); добавляются к обучению, если файл есть")
    ap.add_argument("--features", choices=list(FEATURE_SETS), default="extended")
    ap.add_argument("--backend", choices=["auto", "torch", "sklearn"], default="auto")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(HERE))
    a = ap.parse_args()
    t0 = time.time()
    backend = a.backend if a.backend != "auto" else ("torch" if torch_available() else "sklearn")
    if backend == "torch" and not torch_available():
        raise SystemExit("PyTorch не установлен: pip install torch (или --backend sklearn)")

    df = pd.read_csv(a.data)
    if os.path.exists(a.universal):
        df = pd.concat([df, pd.read_csv(a.universal)], ignore_index=True)
    assert set(df["provenance"]) == {"synthetic"}, "в обучающих данных не только синтетика — пометьте происхождение"
    for s in STRATEGIES:
        assert (df[f"violations__{s}"] == 0).all()
    feats = FEATURE_SETS[a.features]
    tr, va, te = split_by_family(df, a.seed)
    pre = Preprocessor(feats).fit(tr)
    Xtr, Xva, Xte = (pre.transform(x) for x in (tr, va, te))
    ytr, yva = tr["best_strategy"].map(S2I).values, va["best_strategy"].map(S2I).values
    knn = KnnOOD(Xtr)
    pre.knn_threshold = float(np.percentile(knn.dist(Xva), 99))

    dummy = DummyClassifier(strategy="most_frequent").fit(Xtr, ytr)
    rf = RandomForestClassifier(n_estimators=300, min_samples_leaf=2, random_state=a.seed).fit(Xtr, ytr)
    trainer = train_torch if backend == "torch" else train_sklearn
    cands = {h: trainer(Xtr, ytr, Xva, yva, h, a.seed) for h in (32, 64)}
    hidden = min(cands, key=lambda h: cands[h][2]["best_val_loss"])
    model, proba, info = cands[hidden]

    FLUID_OF = {"FCFS": "fluid_on_time_share_fcfs", "EARLIEST_DUE": "fluid_on_time_share_edf",
                "OPTIMIZED": "fluid_on_time_share_smart"}
    base = None
    if all(c in feats for c in FLUID_OF.values()):
        idx = [feats.index(FLUID_OF[s]) for s in STRATEGIES]
        base = dict(idx=idx, mean=[float(pre.mean[i]) for i in idx], std=[float(pre.std[i]) for i in idx])
    share_mlp, share_cfg = train_share_mlp(Xtr, shares(tr), Xva, shares(va), a.seed, base)
    share_rf = RandomForestRegressor(n_estimators=300, min_samples_leaf=2, random_state=a.seed).fit(Xtr, shares(tr))
    share_gb = [HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, min_samples_leaf=10,
                                              l2_regularization=1.0, random_state=a.seed).fit(Xtr, shares(tr)[:, j])
                for j in range(len(STRATEGIES))]
    gb_predict = lambda X: np.clip(np.column_stack([m.predict(X) for m in share_gb]), 0, 1)
    # «без ML»: сырая жидкостная оценка из признаков (порядок как у STRATEGIES)
    fluid_raw = (lambda d: d[[FLUID_OF[s] for s in STRATEGIES]].values) \
        if all(c in df.columns for c in FLUID_OF.values()) else None
    # абляция: та же сеть на признаках первой сборки (без жидкостной оценки)
    ablation = None
    if a.features == "extended":
        pre1 = Preprocessor(FEATURE_SETS["v1"]).fit(tr)
        abl_mlp, abl_cfg = train_share_mlp(pre1.transform(tr), shares(tr), pre1.transform(va), shares(va), a.seed)
        ablation = (pre1, abl_mlp)

    names = np.array(STRATEGIES)
    # Правило решения с учётом цены ошибки: отклоняться от штатного OPTIMIZED можно только при
    # уверенности сети >= порога. Порог подбирается на val по минимуму потерь тоннажа; при
    # равенстве берётся самый осторожный (больший). Порог > 1 означает «всегда OPTIMIZED».
    SAFE = "OPTIMIZED"

    def safe_pred(X, thr):
        P = proba(X)
        k = P.argmax(1)
        out = names[k].copy()
        out[(out != SAFE) & (P.max(1) < thr)] = SAFE
        return out

    grid = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.01]
    val_regret = {t: evaluate(va, safe_pred(Xva, t))["regret_t_mean"] for t in grid}
    safe_thr = max(t for t in grid if val_regret[t] == min(val_regret.values()))
    # Отклоняться от OPTIMIZED разрешаем, только если выигрыш на val устойчив: парный бутстрэп по
    # сценариям, 5-й перцентиль выигрыша в тоннах должен быть > 0. Иначе — всегда OPTIMIZED.
    gain_p05 = None
    if safe_thr <= 1.0:
        def regret_vec(pred):
            chosen = np.array([va[f"on_time_t__{s}"].iloc[i] for i, s in enumerate(pred)])
            return va["best_on_time_t"].values - chosen
        diff = regret_vec(safe_pred(Xva, 1.01)) - regret_vec(safe_pred(Xva, safe_thr))
        rng = np.random.default_rng(a.seed)
        boot = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
        gain_p05 = round(float(np.percentile(boot, 5)), 3)
        if gain_p05 <= 0:
            safe_thr = 1.01
    predictors = {
        "mlp_safe": lambda X: safe_pred(X, safe_thr),
        "dummy_most_frequent": lambda X: names[dummy.predict(X)],
        "random_forest": lambda X: names[rf.predict(X)],
        "mlp": lambda X: names[proba(X).argmax(1)],
    }

    def share_report(d, X, flags):
        Y = shares(d)
        mae = lambda P, m=slice(None): {s: round(float(v), 4) for s, v in zip(STRATEGIES, np.abs(P[m] - Y[m]).mean(0))}
        mu, lo, hi = share_mlp.predict_interval(X)
        cover = lambda m: {s: round(float(v), 3) for s, v in zip(STRATEGIES, ((Y[m] >= lo[m]) & (Y[m] <= hi[m])).mean(0))}
        width = lambda m: {s: round(float(v), 3) for s, v in zip(STRATEGIES, (hi[m] - lo[m]).mean(0))}
        out = dict(mlp_mae=mae(mu), random_forest_mae=mae(share_rf.predict(X)),
                   gradient_boosting_mae=mae(gb_predict(X)),
                   train_mean_mae=mae(np.tile(shares(tr).mean(0), (len(d), 1))),
                   mlp_rmse={s: round(float(v), 4) for s, v in zip(STRATEGIES, np.sqrt(((mu - Y) ** 2).mean(0)))},
                   mlp_abs_err_p90={s: round(float(v), 4) for s, v in zip(STRATEGIES, np.percentile(np.abs(mu - Y), 90, axis=0))},
                   interval90_coverage=cover(slice(None)), interval90_mean_width=width(slice(None)))
        if fluid_raw is not None:
            out["fluid_estimate_without_ml_mae"] = mae(fluid_raw(d))
        if ablation is not None:
            out["mlp_without_fluid_features_mae"] = mae(ablation[1].predict(ablation[0].transform(d)))
        acc = ~flags
        # то, что пользователь реально увидит: прогноз выдаётся только для сценариев в распределении
        if acc.any():
            out["accepted_n"] = int(acc.sum())
            out["accepted_mlp_mae"] = mae(mu, acc)
            out["accepted_interval90_coverage"] = cover(acc)
        if flags.any():
            out["flagged_n"] = int(flags.sum())
            out["flagged_mlp_mae"] = mae(mu, flags)
        return out

    def block(d, X):
        out = {k: evaluate(d, f(X)) for k, f in predictors.items()}
        out["label_distribution"] = d["best_strategy"].value_counts().to_dict()
        out["share_ties_on_tonnage"] = round(float(d["tie_on_tonnage"].mean()), 4)
        flags, reasons = ood_flags(pre, knn, d, X)
        out["on_time_share_forecast"] = share_report(d, X, flags)
        out["ood_flag_rate"] = round(float(flags.mean()), 4)
        out["top_ood_reasons"] = pd.Series([r.split("=")[0].split(" ")[0] for rs in reasons for r in rs]
                                           ).value_counts().head(5).to_dict()
        out["violations_total"] = int(sum(d[f"violations__{s}"].sum() for s in STRATEGIES))
        return out

    metrics = dict(
        disclaimer=("Синтетические сценарии; метки и тоннаж — матмодель Case A3 (алгоритм 2.1.0, эвристика, не глобальный "
                    "оптимум). Не реальные диспетчерские решения и не факты о Яне. Модель не готова к эксплуатации."),
        data=dict(file=os.path.basename(a.data) + (" + " + os.path.basename(a.universal) if os.path.exists(a.universal) else ""), n_scenarios=int(len(df)), n_families=int(df.family_id.nunique()),
                  split="70/15/15 по family_id", n_train=len(tr), n_val=len(va), n_test=len(te),
                  label_distribution_train=tr["best_strategy"].value_counts().to_dict()),
        features=feats, backend=backend,
        mlp=dict(hidden=hidden, layers=2, activation="ReLU", loss="CrossEntropy", optimizer="Adam", **info),
        decision_rule=dict(safe_strategy=SAFE, deviate_confidence_threshold=safe_thr,
                           val_regret_t_mean_by_threshold={str(t): v for t, v in val_regret.items()},
                           val_gain_vs_always_safe_t_p05=gain_p05,
                           note=("mlp_safe: стратегия сети берётся только при confidence >= порога, иначе OPTIMIZED; "
                                 "порог > 1 = всегда OPTIMIZED")),
        on_time_share_mlp=dict(layers=2, activation="ReLU", solver="L-BFGS", **share_cfg),
        test=block(te, Xte),
    )
    if te["source"].nunique() > 1:
        metrics["test_by_source"] = dict(
            note="тот же тест, отдельно по происхождению: generated — сценарии вокруг Яны, universal — произвольные маршруты",
            **{src: block(g.reset_index(drop=True), pre.transform(g)) for src, g in te.groupby("source")})
        metrics["data"]["n_by_source"] = df["source"].value_counts().to_dict()
    if os.path.exists(a.archive):
        arc = pd.read_csv(a.archive)
        metrics["archive_check"] = dict(
            note="архивные сценарии проекта (data/scenarios + demo); в обучении не участвовали",
            **block(arc, pre.transform(arc)))
    if os.path.exists(a.stress):
        st = pd.read_csv(a.stress)
        assert not (set(st.family_id) & set(df.family_id)), "стресс-семейства пересекаются с обучающими"
        metrics["stress_check"] = dict(
            note=("стресс-режимы ТЗ (a3_bridge.REGIMES), значения намеренно за пределами обучающих диапазонов; "
                  "в обучении не участвовали. Ожидаемое поведение: высокая доля OOD -> fallback к OPTIMIZED"),
            **{r: block(g.reset_index(drop=True), pre.transform(g)) for r, g in st.groupby("regime")})
    metrics["train_seconds"] = round(time.time() - t0, 1)

    out = Path(a.out)
    save_json(dict(preprocessor=pre.to_dict(), strategies=STRATEGIES, backend=backend, hidden=hidden,
                   feature_set=a.features, safe_strategy=SAFE, deviate_confidence_threshold=safe_thr),
              out / "model_meta.json")
    np.save(out / "ood_train_matrix.npy", Xtr)
    joblib.dump(share_mlp, out / "share_mlp.joblib")
    if backend == "torch":
        import torch
        torch.save(dict(state_dict=model.state_dict(), n_in=Xtr.shape[1], hidden=hidden, strategies=STRATEGIES),
                   out / "model.pt")
    else:
        joblib.dump(model, out / "model_sklearn_mlp.joblib")
    save_json(metrics, out / "metrics.json")
    import json
    print(json.dumps({k: metrics[k] for k in ("data", "mlp", "on_time_share_mlp", "test", "archive_check") if k in metrics},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
