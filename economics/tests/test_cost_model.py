import unittest

from economics.cost_model import calculate_trip_cost, evaluate_schedule, load_cost_config


class CostModelTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "currency": "RUB",
            "data_status": "ASSUMED",
            "fuel_price_per_l": 10,
            "fuel_consumption_lph_by_unit": {
                "U1": {"upstream": 10, "downstream": 5}
            },
            "daily_operating_cost_rub": {"tug": 2400, "barge": 1200},
            "handling_cost_per_ton_rub": {"loading": 100, "unloading": 80},
            "other_cost_per_trip_rub": 0,
        }
        self.trip = {
            "trip_id": "TR00001",
            "unit_id": "U1",
            "cargo_type": "food",
            "volume_t": 10,
            "loading_start": "2026-06-10T00:00:00",
            "released_at": "2026-06-11T00:00:00",
            "events": [
                {
                    "direction": "upstream",
                    "start": "2026-06-10T01:00:00",
                    "end": "2026-06-10T03:00:00",
                },
                {
                    "direction": "downstream",
                    "start": "2026-06-10T04:00:00",
                    "end": "2026-06-10T08:00:00",
                },
            ],
        }

    def test_trip_cost_counts_both_directions_and_round_trip_occupancy(self):
        row = calculate_trip_cost(self.trip, self.config)
        # Fuel: (2h*10 + 4h*5) L * 10 RUB/L = 400 RUB.
        self.assertEqual(row["fuel_l"], 40)
        self.assertEqual(row["cost_components"]["fuel"], 400)
        # 1 occupied day * (2400 + 1200) RUB.
        self.assertEqual(row["cost_components"]["tug_operation"], 2400)
        self.assertEqual(row["cost_components"]["barge_operation"], 1200)
        self.assertEqual(row["cost_components"]["loading"], 1000)
        self.assertEqual(row["cost_components"]["unloading"], 800)
        self.assertEqual(row["total_cost"], 5800)

    def test_schedule_cost_per_ton(self):
        result = {
            "schema_version": "1.0.0",
            "scenario_id": "test",
            "strategy": "FCFS",
            "metrics": {
                "delivered_total_t": 10,
                "delivered_on_time_t": 10,
                "completed_trips": 1,
            },
            "trips": [self.trip],
        }
        report = evaluate_schedule(result, self.config)
        self.assertEqual(report["total_cost"], 5800)
        self.assertEqual(report["cost_per_delivered_ton"], 580)

    def test_negative_fuel_price_is_rejected(self):
        bad = dict(self.config)
        bad["fuel_price_per_l"] = -1
        with self.assertRaises(ValueError):
            load_cost_config_from_dict_for_test(bad)


def load_cost_config_from_dict_for_test(config):
    # Keep validation path aligned with production JSON loader.
    import json
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return load_cost_config(path)


if __name__ == "__main__":
    unittest.main()
