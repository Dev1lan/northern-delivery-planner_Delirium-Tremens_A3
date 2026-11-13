"""Сетевой ML-прогноз: все метрики по стратегиям согласованы между собой."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ML = ROOT / 'ml' / 'network_ml'
READY = all(importlib.util.find_spec(m) for m in ('sklearn', 'joblib', 'pandas')) and (ML / 'model.joblib').exists()


@unittest.skipUnless(READY, 'нет зависимостей ML или обученной модели')
class NetworkForecastTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('network_ml_predict_test', ML / 'predict.py')
        cls.predict = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.predict)
        cls.raw = json.loads((ROOT / 'data/yakutia/yakutia_network.json').read_text(encoding='utf-8'))
        cls.demand = sum(c['volume_t'] for c in cls.raw['cargo'])

    def test_all_metrics_are_consistent(self):
        out = self.predict.forecast(self.raw)
        self.assertEqual(out['status'], 'OK')
        self.assertEqual(out['money'], 'OK')
        for strategy, row in out['by_strategy'].items():
            m = row['metrics']
            for name, v in m.items():
                self.assertLessEqual(v['lo'], v['value'] + 0.1, (strategy, name))
                self.assertLessEqual(v['value'], v['hi'] + 0.1, (strategy, name))
                self.assertGreaterEqual(v['lo'], 0, (strategy, name))
            self.assertAlmostEqual(m['delivered_on_time_t']['value'], row['on_time_t'], delta=0.1)
            self.assertGreaterEqual(m['delivered_total_t']['value'], m['delivered_on_time_t']['value'])
            self.assertLessEqual(m['delivered_total_t']['value'], self.demand + 0.1)
            self.assertLessEqual(m['critical_on_time_t']['value'], m['delivered_on_time_t']['value'] + 0.1)
            self.assertAlmostEqual(m['undelivered_t']['value'], self.demand - m['delivered_total_t']['value'], delta=0.2)
            self.assertAlmostEqual(m['total_cost']['value'],
                                   m['operating_cost']['value'] + m['undelivered_penalty']['value'], delta=0.2)

    def test_other_tariffs_drop_money(self):
        out = self.predict.forecast(self.raw, {'tariff_version': 'other'})
        self.assertEqual(out['money'], 'SKIPPED_OTHER_TARIFFS')
        for row in out['by_strategy'].values():
            self.assertFalse(set(self.predict.MONEY) & set(row['metrics']))
            self.assertIn('empty_km', row['metrics'])


if __name__ == '__main__':
    unittest.main()
