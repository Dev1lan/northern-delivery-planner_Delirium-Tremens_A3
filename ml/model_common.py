"""Общие части для train.py и predict.py: признаки, нормализация, OOD, MLP."""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

import numpy as np

from a3_bridge import BASE_FEATURES, V1_FEATURES, ALL_FEATURES, STRATEGIES

# Признаки с большим разбросом масштаба логарифмируем перед стандартизацией
LOG_FEATURES = {"cargo_total_t", "total_capacity_t", "cargo_count", "vessel_count",
                "demand_to_throughput"}

FEATURE_SETS = {"base": BASE_FEATURES, "v1": V1_FEATURES, "extended": ALL_FEATURES}


class Preprocessor:
    def __init__(self, features: List[str]):
        self.features = list(features)
        self.mean = None
        self.std = None
        self.lo = None   # границы обучающего распределения (сырые значения)
        self.hi = None
        self.knn_threshold = None

    # ---- преобразование -------------------------------------------------
    def _raw(self, rows) -> np.ndarray:
        if isinstance(rows, dict):
            rows = [rows]
        if hasattr(rows, "to_dict"):
            rows = rows.to_dict("records")
        X = np.array([[float(r[f]) for f in self.features] for r in rows], dtype=np.float64)
        return X

    def _log(self, X: np.ndarray) -> np.ndarray:
        X = X.copy()
        for j, f in enumerate(self.features):
            if f in LOG_FEATURES:
                X[:, j] = np.log1p(np.maximum(X[:, j], 0.0))
        return X

    def fit(self, rows):
        R = self._raw(rows)
        L = self._log(R)
        self.mean = L.mean(0)
        self.std = L.std(0) + 1e-9
        # границы: 0.5–99.5 перцентиль + 5% ширины диапазона
        q0, q1 = np.percentile(R, 0.5, axis=0), np.percentile(R, 99.5, axis=0)
        pad = 0.05 * (q1 - q0)
        self.lo, self.hi = q0 - pad, q1 + pad
        return self

    def transform(self, rows) -> np.ndarray:
        return ((self._log(self._raw(rows)) - self.mean) / self.std).astype(np.float32)

    # ---- выход за обучающее распределение -------------------------------
    def range_violations(self, rows) -> List[List[str]]:
        R = self._raw(rows)
        out = []
        for r in R:
            bad = [f"{f}={v:g} вне [{lo:g}; {hi:g}]"
                   for f, v, lo, hi in zip(self.features, r, self.lo, self.hi)
                   if v < lo or v > hi]
            out.append(bad)
        return out

    # ---- сериализация -----------------------------------------------------
    def to_dict(self) -> Dict:
        return dict(features=self.features, mean=self.mean.tolist(), std=self.std.tolist(),
                    lo=self.lo.tolist(), hi=self.hi.tolist(), knn_threshold=self.knn_threshold,
                    log_features=sorted(LOG_FEATURES & set(self.features)))

    @staticmethod
    def from_dict(d: Dict) -> "Preprocessor":
        p = Preprocessor(d["features"])
        p.mean, p.std = np.array(d["mean"]), np.array(d["std"])
        p.lo, p.hi = np.array(d["lo"]), np.array(d["hi"])
        p.knn_threshold = d["knn_threshold"]
        return p


class KnnOOD:
    """Детектор «совместного» выхода за распределение: расстояние до
    ближайшего обучающего сценария в нормированном пространстве."""

    def __init__(self, Xtrain: np.ndarray):
        from sklearn.neighbors import NearestNeighbors
        self.X = Xtrain
        self.nn = NearestNeighbors(n_neighbors=1).fit(Xtrain)

    def dist(self, X: np.ndarray) -> np.ndarray:
        return self.nn.kneighbors(X, return_distance=True)[0][:, 0]


def ood_flags(pre: Preprocessor, knn: KnnOOD, rows, X) -> Tuple[np.ndarray, List[List[str]]]:
    reasons = pre.range_violations(rows)
    d = knn.dist(X)
    flags = []
    for i, r in enumerate(reasons):
        if d[i] > pre.knn_threshold:
            r.append(f"комбинация признаков далеко от обучающих (dist={d[i]:.2f} > {pre.knn_threshold:.2f})")
        flags.append(bool(r))
    return np.array(flags), reasons


# ---------------------------------------------------------------------------
# MLP на PyTorch (основной вариант по ТЗ)
# ---------------------------------------------------------------------------
def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def build_torch_mlp(n_in: int, hidden: int, n_out: int = len(STRATEGIES)):
    import torch.nn as nn
    return nn.Sequential(
        nn.Linear(n_in, hidden), nn.ReLU(),
        nn.Linear(hidden, hidden), nn.ReLU(),
        nn.Linear(hidden, n_out),
    )


def save_json(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Прогноз доли тоннажа вовремя: ансамбль MLP-регрессоров + калиброванный интервал
# ---------------------------------------------------------------------------
class ShareEnsemble:
    """Прогноз доли тоннажа вовремя по каждой стратегии.

    Точечный прогноз — среднее нескольких MLPRegressor с разными seed.
    Если задана base (столбцы жидкостной оценки), сети учат ПОПРАВКУ к ней:
    прогноз = жидкостная оценка + поправка сети. Иначе сети предсказывают долю
    напрямую (так работает набор признаков первой сборки).

    Интервал — conformalized quantile regression: градиентный бустинг на
    квантили той же цели + поправка q, подобранная на val-выборке так, чтобы
    интервал накрывал заданную долю сценариев. Это эмпирическая калибровка на
    синтетике, а не гарантия для реальных условий."""

    def __init__(self, models, coverage=0.9, base=None):
        self.models = list(models)
        self.coverage = coverage
        self.base = base         # None или dict(idx=[...], mean=[...], std=[...]) — столбцы X с жидкостной оценкой
        self.q_models = None     # [(lo_model, hi_model)] по стратегиям
        self.q = None

    def base_of(self, X):
        if self.base is None:
            return 0.0
        idx = self.base["idx"]
        return np.asarray(X, dtype=np.float64)[:, idx] * np.asarray(self.base["std"]) + np.asarray(self.base["mean"])

    def target(self, X, Y):
        return Y - self.base_of(X)

    def predict(self, X):
        raw = np.mean([m.predict(X) for m in self.models], axis=0)
        return np.clip(self.base_of(X) + raw, 0, 1)

    def _raw_interval(self, X):
        b = self.base_of(X)
        lo = np.column_stack([a.predict(X) for a, _ in self.q_models]) + b
        hi = np.column_stack([c.predict(X) for _, c in self.q_models]) + b
        return np.minimum(lo, hi), np.maximum(lo, hi)

    def fit_interval(self, Xtr, Ytr, Xva, Yva, seed=0):
        from sklearn.ensemble import HistGradientBoostingRegressor as GB
        a = (1 - self.coverage) / 2
        T = self.target(Xtr, Ytr)
        mk = lambda q: GB(loss="quantile", quantile=q, max_iter=300, learning_rate=0.05,
                          min_samples_leaf=10, l2_regularization=1.0, random_state=seed)
        self.q_models = [(mk(a).fit(Xtr, T[:, j]), mk(1 - a).fit(Xtr, T[:, j])) for j in range(T.shape[1])]
        lo, hi = self._raw_interval(Xva)
        score = np.maximum(lo - Yva, Yva - hi)
        n = len(score)
        level = min(1.0, np.ceil((n + 1) * self.coverage) / n)
        self.q = np.quantile(score, level, axis=0, method="higher")
        return self

    def predict_interval(self, X):
        mu = self.predict(X)
        lo, hi = self._raw_interval(X)
        # точечный прогноз всегда внутри интервала
        return mu, np.clip(np.minimum(lo - self.q, mu), 0, 1), np.clip(np.maximum(hi + self.q, mu), 0, 1)
