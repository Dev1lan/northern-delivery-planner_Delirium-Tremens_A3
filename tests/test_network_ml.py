import json
from dataclasses import asdict
from pathlib import Path
import unittest
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("Network ML tests require NumPy")

from ml.network_ml.dataset import DEFAULT_LIMITS, DEFAULT_POLICY, cases, core_version, label, synthetic
from ml.network_ml.features import extract
from ml.network_ml.model import FORMAT, STRATEGIES, NetworkModel, forecast
from ml.network_ml.planning import plan
from ml.network_ml.train import fit_mlp, read_rows, usable
from network.models import parse_scenario
from network.scheduler import REPOSITION


class NetworkMLTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.tariffs = synthetic(300123, "basins")

    def model(self, raw, tariffs, *, radius=.1, threshold=1):
        features = extract(parse_scenario(raw), tariffs)
        names = sorted(features)
        return NetworkModel(dict(kind="mean", mean=np.array([features[n] for n in names]),
                                 scale=np.ones(len(names)), train_z=np.zeros((1, len(names))),
                                 weights=np.tile([1.5, 1.7, .4, .2, 6.0], (len(STRATEGIES), 1)),
                                 meta=np.array(json.dumps(dict(
                                     format=FORMAT, core=core_version(), features=names,
                                     limits=asdict(DEFAULT_LIMITS), policy=asdict(DEFAULT_POLICY),
                                     currencies=["RUB"], social_trained=True, cost_trained=True, max_log_cost=12,
                                     ood_threshold=threshold, interval_radii=dict.fromkeys(STRATEGIES, radius))))))

    def test_variable_topologies(self):
        shapes = set()
        for family in ("line", "basins", "large", "unseen"):
            raw, tariffs = synthetic(41, family)
            parsed = parse_scenario(raw)
            shapes.add((len(parsed.ports), len(parsed.edges)))
            reply = self.model(raw, tariffs).predict(raw, tariffs=tariffs)
            self.assertEqual(reply["status"], "AVAILABLE")
            self.assertTrue(reply["requires_optimizer_verification"])
            self.assertNotIn("voyages", reply)
            demand = sum(c.kg for c in parsed.cargo.values())/1000
            for metrics in reply["forecasts"].values():
                self.assertLessEqual(metrics["delivered_on_time_t"], metrics["delivered_total_t"])
                self.assertLessEqual(metrics["delivered_total_t"], demand)
        self.assertEqual(len(shapes), 4)

    def test_optional_targets_and_uncertainty(self):
        self.raw["social_requirements"] = []
        model = self.model(self.raw, None, radius=.45)
        reply = model.predict(self.raw)
        self.assertEqual(reply["status"], "HIGH_UNCERTAINTY")
        self.assertTrue(reply["strategy_order"])
        for row in reply["forecasts"].values():
            self.assertIsNone(row["social_failure_probability"])
            self.assertIsNone(row["estimated_cost"])
        unfamiliar, _ = synthetic(17, "large")
        self.assertEqual(model.predict(unfamiliar)["status"], "OUT_OF_DISTRIBUTION")

    def test_disabled_missing_model_and_safe_planning(self):
        with patch.object(NetworkModel, "load", side_effect=FileNotFoundError):
            self.assertEqual(forecast(self.raw)["status"], "MODEL_UNAVAILABLE")
            self.assertEqual(forecast(self.raw, enabled=False)["status"], "DISABLED")
            result, _ = plan(self.raw, use_ml=True)
        self.assertEqual(result["strategy"], REPOSITION)
        self.assertEqual(result["schedule_audit"]["status"], "PASSED")

    def test_ml_only_selects_strategy(self):
        with patch("ml.network_ml.planning.forecast", return_value={
            "status": "AVAILABLE", "strategy_order": ["NETWORK_FCFS"]}):
            result, _ = plan(self.raw, use_ml=True)
        self.assertEqual(result["strategy"], "NETWORK_FCFS")
        self.assertEqual(result["schedule_audit"]["status"], "PASSED")

    def test_incompatible_search_profile_and_kernel(self):
        from dataclasses import replace

        model = self.model(self.raw, None)
        limits = replace(DEFAULT_LIMITS, max_actions=DEFAULT_LIMITS.max_actions + 1)
        self.assertEqual(model.predict(self.raw, limits=limits)["status"], "UNSUPPORTED_SEARCH_BUDGET")
        model.meta["core"] = {"contract": "unknown"}
        self.assertEqual(model.predict(self.raw)["status"], "INCOMPATIBLE_CORE")

    def test_label_provenance_and_leakage(self):
        rows = list(label(self.raw, None, "basins", 300123, "train", version=core_version()))
        self.assertTrue(all(usable(r) for r in rows))
        self.assertFalse(usable(dict(rows[0], termination="TIME_LIMIT")))
        self.assertFalse(usable(dict(rows[0], audit={"status": "FAILED"})))
        for bad, expected in ((dict(rows[1], split="test"), "leakage"),
                              (dict(rows[1], core={"contract": "unknown"}), "Mixed kernels")):
            payload = "\n".join(json.dumps(row) for row in (rows[0], bad))
            with patch.object(Path, "read_text", return_value=payload):
                with self.assertRaisesRegex(ValueError, expected):
                    read_rows(Path("labels.jsonl"))
        splits = {}
        for _, _, family, seed, split in cases(37, 7, 2):
            if seed is not None:
                self.assertEqual(splits.setdefault(seed, split), split)
            if family == "unseen":
                self.assertEqual(split, "test")

    def test_seed_reproducibility(self):
        try:
            import torch
        except ImportError:
            self.skipTest("Training requires PyTorch")
        self.assertEqual(synthetic(57, "basins", 1), synthetic(57, "basins", 1))
        x = np.arange(24, dtype=float).reshape(8, 3)/24
        y = np.column_stack((x[:, :2], x[:, :2], x[:, 0]))
        first = fit_mlp(x, y, x, y, 37)
        second = fit_mlp(x, y, x, y, 37)
        np.testing.assert_array_equal(first["w2"], second["w2"])


if __name__ == "__main__":
    unittest.main()
