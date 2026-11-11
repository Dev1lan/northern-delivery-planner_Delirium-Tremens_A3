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
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import log_loss
from sklearn.model_selection import GroupShuffleSplit
from sklearn.neural_network import MLPClassifier, MLPRegressor

from a3_bridge import STRATEGIES
from model_common import (Preprocessor, KnnOOD, ood_flags, FEATURE_SETS,
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


def train_share_mlp(Xtr, Ytr, Xva, Yva, seed):
    """MLP-регрессор 2 скрытых слоя, ReLU. Данных мало (сотни сценариев), поэтому
    L-BFGS + L2; ширина и сила регуляризации выбираются по val-выборке."""
    best, best_m, best_cfg = 1e9, None, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for h in (32, 64):
            for alpha in (0.1, 1.0, 3.0):
                m = MLPRegressor(hidden_layer_sizes=(h, h), activation="relu", solver="lbfgs",
                                 alpha=alpha, max_iter=3000, random_state=seed).fit(Xtr, Ytr)
                vl = float(np.abs(np.clip(m.predict(Xva), 0, 1) - Yva).mean())
                if vl < best:
                    best, best_m, best_cfg = vl, m, dict(hidden=h, alpha=alpha, val_mae=round(vl, 4))
    return best_m, best_cfg


def shares(df):
    return df[SHARE_COLS].values / np.maximum(df["total_t"].values[:, None], 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data/dataset_synthetic.csv"))
    ap.add_argument("--archive", default=str(HERE / "data/dataset_archive.csv"))
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

    share_mlp, share_cfg = train_share_mlp(Xtr, shares(tr), Xva, shares(va), a.seed)
    share_rf = RandomForestRegressor(n_estimators=300, min_samples_leaf=2, random_state=a.seed).fit(Xtr, shares(tr))

    names = np.array(STRATEGIES)
    predictors = {
        "dummy_most_frequent": lambda X: names[dummy.predict(X)],
        "random_forest": lambda X: names[rf.predict(X)],
        "mlp": lambda X: names[proba(X).argmax(1)],
    }

    def share_report(d, X):
        Y = shares(d)
        mae = lambda P: {s: round(float(v), 4) for s, v in zip(STRATEGIES, np.abs(P - Y).mean(0))}
        return dict(mlp_mae=mae(np.clip(share_mlp.predict(X), 0, 1)), random_forest_mae=mae(share_rf.predict(X)),
                    train_mean_mae=mae(np.tile(shares(tr).mean(0), (len(d), 1))))

    def block(d, X):
        out = {k: evaluate(d, f(X)) for k, f in predictors.items()}
        out["label_distribution"] = d["best_strategy"].value_counts().to_dict()
        out["share_ties_on_tonnage"] = round(float(d["tie_on_tonnage"].mean()), 4)
        out["on_time_share_forecast"] = share_report(d, X)
        flags, reasons = ood_flags(pre, knn, d, X)
        out["ood_flag_rate"] = round(float(flags.mean()), 4)
        out["top_ood_reasons"] = pd.Series([r.split("=")[0].split(" ")[0] for rs in reasons for r in rs]
                                           ).value_counts().head(5).to_dict()
        out["violations_total"] = int(sum(d[f"violations__{s}"].sum() for s in STRATEGIES))
        return out

    metrics = dict(
        disclaimer=("Синтетические сценарии; метки и тоннаж — матмодель Case A3 (алгоритм 2.1.0, эвристика, не глобальный "
                    "оптимум). Не реальные диспетчерские решения и не факты о Яне. Модель не готова к эксплуатации."),
        data=dict(file=os.path.basename(a.data), n_scenarios=int(len(df)), n_families=int(df.family_id.nunique()),
                  split="70/15/15 по family_id", n_train=len(tr), n_val=len(va), n_test=len(te),
                  label_distribution_train=tr["best_strategy"].value_counts().to_dict()),
        features=feats, backend=backend,
        mlp=dict(hidden=hidden, layers=2, activation="ReLU", loss="CrossEntropy", optimizer="Adam", **info),
        on_time_share_mlp=dict(layers=2, activation="ReLU", solver="L-BFGS", **share_cfg),
        test=block(te, Xte),
    )
    if os.path.exists(a.archive):
        arc = pd.read_csv(a.archive)
        metrics["archive_check"] = dict(
            note="архивные сценарии проекта (data/scenarios + demo); в обучении не участвовали",
            **block(arc, pre.transform(arc)))
    metrics["train_seconds"] = round(time.time() - t0, 1)

    out = Path(a.out)
    save_json(dict(preprocessor=pre.to_dict(), strategies=STRATEGIES, backend=backend, hidden=hidden,
                   feature_set=a.features), out / "model_meta.json")
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
    print(json.dumps({k: metrics[k] for k in ("data", "mlp", "test", "archive_check") if k in metrics},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
