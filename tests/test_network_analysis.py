import unittest
from copy import deepcopy
import json
from pathlib import Path

from network.adapter import run_scenario
from network.analysis import change_scenario, diagnose, explain_voyage, find_improvements, port_at, port_load, positions_at

DEMO = Path(__file__).resolve().parents[1] / 'data' / 'yakutia_demo'


def read(name):
    return json.loads((DEMO / f'{name}.json').read_text(encoding='utf-8-sig'))


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = read('yakutia_stress')
        cls.tariffs = read('tariffs_base')
        cls.result = run_scenario(cls.raw, tariffs=cls.tariffs)

    def test_counterfactual_audits_improvement_and_deterioration(self):
        original = deepcopy(self.raw)
        report = find_improvements(self.raw, self.result, read('improvements'), tariffs=self.tariffs)
        self.assertEqual(self.raw, original)
        best, _, worse = report['alternatives']
        self.assertEqual(best['change']['kind'], 'add_berth')
        self.assertEqual(best['delta']['delivered_on_time_t'], 65)
        self.assertEqual(best['result']['schedule_audit']['status'], 'PASSED')
        self.assertFalse(worse['improved'])
        self.assertEqual(worse['change']['kind'], 'close_berth')

    def test_port_timeline_and_explanation_use_exported_events(self):
        queue = next(e for e in self.result['events'] if e['state'] == 'PORT_QUEUE')
        instant = queue['start']
        snapshot = port_at(self.result, queue['origin_port'], instant)
        self.assertIn(queue['tug_id'], snapshot['queued_tugs'])
        self.assertIn('OCCUPIED', [b['status'] for b in snapshot['berths']])
        report = port_load(self.result, queue['origin_port'], self.raw['start'], self.raw['end'])
        self.assertEqual(report, self.result['port_operations'][queue['origin_port']])
        voyage = explain_voyage(self.result, queue['voyage_id'])
        self.assertEqual(voyage['tug_id'], queue['tug_id'])
        self.assertFalse(voyage['voyage_level_alternatives_calculated'])
        transit = next(e for e in self.result['events'] if e['state'] == 'EMPTY_TRANSIT')
        self.assertEqual(positions_at(self.result, transit['start'])[transit['tug_id']]['edge_id'], transit['edge_id'])

    def test_diagnosis_distinguishes_proof_and_unknown(self):
        report = diagnose(self.result)
        self.assertTrue(report['queues'])
        self.assertIn('UNDETERMINED', {e['kind'] for c in report['cargo'] for e in c['evidence']})
        closed = deepcopy(self.raw)
        closed['edges'] = [e for e in closed['edges'] if e['destination_port'] != 'Чокурдах']
        failed = diagnose(run_scenario(closed))
        self.assertIn({'kind': 'PROVEN', 'code': 'NO_DIRECTED_PATH'},
                      [e for c in failed['cargo'] for e in c['evidence']])

    def test_rejects_using_a_different_scenario_for_baseline(self):
        different = change_scenario(self.raw, {'kind': 'marine_reposition', 'enabled': False})
        with self.assertRaisesRegex(ValueError, 'Baseline input differs'):
            find_improvements(different, self.result, [])
