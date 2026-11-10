import copy
import json
import unittest
from pathlib import Path
from optimizer import load, simulate, STRATEGIES
from validate import audit

ROOT = Path(__file__).resolve().parent
class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.s = load(ROOT/'data/scenarios/r1_baseline_central_central', json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8')))
        self.s['cargo'] = copy.deepcopy(self.s['cargo'][:2])
        for x in self.s['cargo']: x['volume_t'] = 10

    def test_deterministic_and_complete(self):
        for strategy in STRATEGIES:
            a = simulate(self.s, strategy)
            b = simulate(self.s, strategy)
            self.assertEqual(a, b)
            self.assertAlmostEqual(a['metrics']['delivered_total_t'] + a['metrics']['undelivered_t'], 20)
            audit(self.s['config'], self.s['cargo'], self.s['units'], self.s['conditions'], a['trips'])

    def test_closed_and_empty_fleet(self):
        s = copy.deepcopy(self.s)
        for c in s['conditions']: c['is_closed'] = 1
        for strategy in STRATEGIES:
            self.assertEqual(simulate(s, strategy)['metrics']['delivered_total_t'], 0)
        s = copy.deepcopy(self.s); s['units'] = []
        self.assertEqual(simulate(s, 'OPTIMIZED')['metrics']['delivered_total_t'], 0)

    def test_auditor_rejects_bad_schedules(self):
        trips = simulate(self.s, 'FCFS')['trips']
        self.assertTrue(trips)
        for mutation in ('mass','overlap','tug','readiness','draft'):
            bad = copy.deepcopy(trips)
            if mutation == 'mass': bad[0]['volume_t'] = 1e6
            if mutation == 'overlap': bad.append(dict(bad[0],trip_id='duplicate'))
            if mutation == 'tug': bad[0]['tug_id'] = 'wrong'
            if mutation == 'readiness': bad[0]['loading_start'] = '2020-01-01T00:00:00'
            if mutation == 'draft': bad[0]['loaded_draft_m'] = 99
            with self.assertRaises(AssertionError):
                audit(self.s['config'], self.s['cargo'], self.s['units'], self.s['conditions'], bad)

if __name__ == '__main__': unittest.main()
