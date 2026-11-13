"""Train compact multi-output models and serve forecasts without loading pickle."""

from dataclasses import asdict
import json
import math
from pathlib import Path

import numpy as np

from network.models import parse_scenario

from .dataset import DEFAULT_LIMITS, DEFAULT_POLICY, core_version, strategies
from .features import extract

FORMAT = 1
STRATEGIES = strategies({})
OUTPUTS = ("total_share", "on_time_share", "social_failed", "deficit_share", "log_cost_per_t")
DEFAULT_ARTIFACT = Path(__file__).with_name("artifacts") / "network_model.npz"


def design(features, names, names_of_strategies, mean, scale):
    x = (np.array([features[n] for n in names], dtype=float) - mean) / scale
    rows = np.column_stack((np.broadcast_to(x, (len(STRATEGIES), len(x))), np.eye(len(STRATEGIES))))
    return rows[[STRATEGIES.index(s) for s in names_of_strategies]], x


def forward(artifact, x):
    if artifact["kind"] == "mean":
        return x[:, -len(STRATEGIES):] @ artifact["weights"]
    if artifact["kind"] == "ridge":
        return np.column_stack((x, np.ones(len(x)))) @ artifact["weights"]
    for i in range(2):
        x = np.maximum(0, x @ artifact[f"w{i}"] + artifact[f"b{i}"])
    return x @ artifact["w2"] + artifact["b2"]


def bounded(values):
    values = values.copy()
    values[:, :4] = np.clip(values[:, :4], 0.0, 1.0)
    values[:, 1] = np.minimum(values[:, 0], values[:, 1])
    values[:, 4] = np.maximum(values[:, 4], 0.0)
    return values


def rank_key(values, *, has_social, priced):
    """Social risk and expected shortfall first, then service and price.

    Round the forecast bins to avoid treating noise as a strict improvement.
    """
    social = (round(values[2], 1), round(values[3], 1)) if has_social else ()
    return (*social, -round(values[1], 2), -round(values[0], 2), values[4] if priced else 0)


class NetworkModel:
    def __init__(self, artifact):
        self.artifact = artifact
        self.meta = json.loads(artifact["meta"].item())

    @classmethod
    def load(cls, path=DEFAULT_ARTIFACT):
        with np.load(path, allow_pickle=False) as data:
            return cls(dict(data))

    def applicability(self, z):
        closest = float(np.min(np.linalg.norm(self.artifact["train_z"] - z, axis=1)))
        return closest, closest <= self.meta["ood_threshold"]

    def predict(self, raw, *, tariffs=None, limits=DEFAULT_LIMITS, policy=DEFAULT_POLICY):
        reply = {"kind": "network_forecast", "scenario_id": raw["scenario_id"],
                 "requires_optimizer_verification": True, "forecasts": {}, "strategy_order": []}
        if self.meta["format"] != FORMAT or self.meta["core"] != core_version():
            return dict(reply, status="INCOMPATIBLE_CORE")
        if asdict(limits) != self.meta["limits"] or asdict(policy) != self.meta["policy"]:
            return dict(reply, status="UNSUPPORTED_SEARCH_BUDGET")
        if tariffs is not None and tariffs["currency"] not in self.meta["currencies"]:
            return dict(reply, status="UNSUPPORTED_CURRENCY")
        parsed = parse_scenario(raw)
        features = extract(parsed, tariffs)
        names_of_strategies = strategies(tariffs)
        x, z = design(features, self.meta["features"], names_of_strategies,
                      self.artifact["mean"], self.artifact["scale"])
        distance, supported = self.applicability(z)
        reply["applicability"] = {"distance": distance, "threshold": self.meta["ood_threshold"]}
        if not supported:
            return dict(reply, status="OUT_OF_DISTRIBUTION")
        demand = features["demand_t"]
        social = bool(parsed.requirements and self.meta["social_trained"])
        priced = bool(tariffs is not None and self.meta["cost_trained"])
        predictions = bounded(forward(self.artifact, x))
        order = sorted(zip(names_of_strategies, predictions, strict=True),
                       key=lambda pair: rank_key(pair[1], has_social=social, priced=priced))
        for strategy, values in order:
            total, timely, risk, deficit, cost = map(float, values)
            radius = self.meta["interval_radii"].get(strategy)
            lo, hi = (max(0, timely-radius), min(1, timely+radius)) if radius is not None else (0, 1)
            reply["forecasts"][strategy] = {
                "delivered_total_t": total*demand, "delivered_on_time_t": timely*demand,
                "delivered_on_time_pct": 100*timely if demand else 0.0,
                "undelivered_t": (1-total)*demand,
                "on_time_t_interval90": [lo*demand, hi*demand],
                "social_failure_probability": risk if social else None,
                "social_deficit_t": deficit*features["social_minimum_t"] if social else None,
                "estimated_cost": math.expm1(min(cost, self.meta["max_log_cost"]))*demand if priced else None,
                "currency": tariffs["currency"] if priced else None,
            }
        uncertain = any(self.meta["interval_radii"].get(s, 1) > .30 for s in names_of_strategies)
        can_rank = ((not parsed.requirements or social) and (tariffs is None or priced))
        return dict(reply, status="HIGH_UNCERTAINTY" if uncertain else "AVAILABLE",
                    strategy_order=[s for s, _ in order] if can_rank else [])


def forecast(raw, *, artifact_path=DEFAULT_ARTIFACT, enabled=True, **kwargs):
    if enabled:
        try:
            model = NetworkModel.load(artifact_path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError):
            return {"kind": "network_forecast", "scenario_id": raw["scenario_id"],
                    "status": "INVALID_ARTIFACT", "forecasts": {}, "strategy_order": [],
                    "requires_optimizer_verification": True}
        else:
            return model.predict(raw, **kwargs)
    return {"kind": "network_forecast", "scenario_id": raw["scenario_id"],
            "status": "MODEL_UNAVAILABLE" if enabled else "DISABLED",
            "forecasts": {}, "strategy_order": [], "requires_optimizer_verification": True}
