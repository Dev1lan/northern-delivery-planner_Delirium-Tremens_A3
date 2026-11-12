"""Ручки контракта ядра v3 (docs/network/API_CONTRACT.md) поверх заданий app/server.py."""

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("A3_ML_DISABLED", "1")

try:
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(ROOT / "app"))
    import server
except (ImportError, RuntimeError) as exc:  # нет fastapi/httpx — ручки приложения не проверяем
    server = None
    SKIP = f"app dependencies missing: {exc}"

COMPARE_KEYS = {"delivered_on_time_t", "delivered_total_t", "critical_on_time_t", "social_deficit_t", "empty_km",
                "undelivered_t", "completed_voyages", "resource_busy_hours", "port_waiting_seconds",
                "satisfied_requirements", "cost", "cost_per_t_on_time"}


@unittest.skipIf(server is None, SKIP if server is None else "")
class NetworkContractEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        r = cls.client.post("/api/network/jobs", json={"builtin": "yakutia_stress"})
        assert r.status_code == 202, r.text
        cls.job = r.json()["job_id"]
        deadline = time.time() + 300
        while time.time() < deadline:
            status = cls.client.get(f"/api/network/jobs/{cls.job}").json()
            if status["status"] != "running":
                break
            time.sleep(0.5)
        assert status["status"] == "done", status
        cls.base = f"/api/network/jobs/{cls.job}"
        cls.scenario = cls.client.get("/api/network/sample/stress").json()

    def get(self, path, **params):
        return self.client.get(self.base + path, params=params)

    def test_plans_include_cost_search_with_v2_tariffs(self):
        plans = self.get("/plans").json()
        self.assertIn("NETWORK_COST", plans["strategies"])
        self.assertIn(plans["best"], plans["strategies"])

    def test_state_matches_contract_shape(self):
        state = self.get("/state", moment=self.scenario["start"]).json()
        self.assertEqual(set(state["vessels"]), {v["vessel_id"] for v in self.scenario["tugs"] + self.scenario["barges"]})
        self.assertEqual(set(state["ports"]), set(self.scenario["ports"]))
        vessel = next(iter(state["vessels"].values()))
        self.assertEqual(set(vessel), {"vessel_id", "port", "state", "edge_id", "progress"})
        port = state["ports"]["Нижнеянск"]
        self.assertEqual(set(port), {"port", "time", "vessels", "queued_tugs", "berths", "capacity_modelled"})
        self.assertTrue(port["capacity_modelled"])
        self.assertEqual(set(port["berths"][0]), {"berth_id", "status", "operation", "expected_release"})

    def test_state_outside_scenario_is_400_error(self):
        r = self.get("/state", moment="1999-01-01T00:00:00")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_port_load(self):
        load = self.get("/port-load", port="Нижнеянск", start=self.scenario["start"], end=self.scenario["end"]).json()
        for key in ("waiting_seconds", "peak_queue", "handled_voyages", "loaded_t", "unloaded_t", "berths"):
            self.assertIn(key, load)
        self.assertIsNone(self.get("/port-load", port="Тикси", start=self.scenario["start"],
                                   end=self.scenario["end"]).json())

    def test_explain_and_unknown_voyage(self):
        voyage = self.get("/explain", voyage=1, variant="NETWORK_COST").json()
        self.assertEqual(voyage["voyage_id"], 1)
        self.assertEqual(voyage["audit"], "PASSED")
        self.assertIsNotNone(voyage["cost"])
        self.assertEqual(self.get("/explain", voyage=999).status_code, 400)

    def test_diagnose(self):
        self.assertEqual(set(self.get("/diagnose").json()), {"cargo", "queues"})

    def test_compare_keys_and_unknown_variant(self):
        delta = self.get("/compare", **{"from": "NETWORK_FCFS", "to": "NETWORK_REPOSITION"}).json()
        self.assertEqual(set(delta), COMPARE_KEYS)
        r = self.get("/compare", **{"from": "C", "to": "NETWORK_REPOSITION"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_what_if_preset_is_stored_as_variant(self):
        presets = self.get("/presets").json()
        self.assertEqual(presets[0]["kind"], "add_berth")
        r = self.client.post(self.base + "/what-if", json={"preset": 0, "base": "NETWORK_REPOSITION"})
        self.assertEqual(r.status_code, 200, r.text)
        report = r.json()
        self.assertEqual(set(report) - {"variant", "base"}, {"baseline_fingerprint", "improvement_found", "alternatives"})
        item = report["alternatives"][0]
        self.assertEqual(set(item), {"change", "delta", "diagnosis", "result", "improved", "explanation"})
        key = report["variant"]
        self.assertIn(key, [w["variant"] for w in self.get("/plans").json()["whatif"]])
        self.assertEqual(self.get("/compare", **{"from": "NETWORK_REPOSITION", "to": key}).json(), item["delta"])
        self.assertEqual(self.get("/state", moment=self.scenario["start"], variant=key).status_code, 200)

    def test_what_if_rejects_bad_change(self):
        for body in ({"preset": 99}, {"change": "x"}, {"change": {"kind": "nope"}}):
            r = self.client.post(self.base + "/what-if", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("error", r.json())


if __name__ == "__main__":
    unittest.main()
