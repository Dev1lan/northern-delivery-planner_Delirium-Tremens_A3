"""Общие части для train.py и predict.py: признаки, нормализация, OOD, MLP."""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

import numpy as np

from a3_bridge import BASE_FEATURES, ALL_FEATURES, STRATEGIES

# Признаки с большим разбросом масштаба логарифмируем перед стандартизацией
LOG_FEATURES = {"cargo_total_t", "total_capacity_t", "cargo_count", "vessel_count",
                "demand_to_throughput"}

FEATURE_SETS = {"base": BASE_FEATURES, "extended": ALL_FEATURES}


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
