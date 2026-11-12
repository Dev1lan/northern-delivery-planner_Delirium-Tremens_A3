import unittest
from copy import deepcopy

from network.adapter import audit_result, build_result, decode_voyages, run_scenario
from network.models import Limits, parse_scenario
from network.scheduler import Plan, REPOSITION

from test_network_analysis import read


class ExpiryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = read('yakutia_stress')
        cls.tariffs = read('tariffs_base')
        cls.original = run_scenario(cls.raw, tariffs=cls.tariffs)

    def test_scheduler_delivers_before_expiry_or_leaves_residual(self):
        raw = deepcopy(self.raw)
        target = raw['cargo'][0]
        target['expires_at'] = target['ready_at']
        result = run_scenario(raw)
        self.assertNotIn(target['cargo_id'], {v['cargo_id'] for v in result['voyages']})
        self.assertEqual(result['schedule_audit']['status'], 'PASSED')
        self.assertEqual(result['cargo_condition'][0]['usable_delivered_t'], 0)
        self.assertNotIn('cargo_condition', self.original)

    def test_replay_distinguishes_physically_delivered_from_usable(self):
        raw = deepcopy(self.raw)
        target = next(c for c in raw['cargo'] if c['cargo_type'] == 'food')
        target['expires_at'] = target['ready_at']
        moves = decode_voyages(self.original['voyages'])
        plan = Plan(REPOSITION, moves, 'SEARCH_COMPLETE', 0)
        result = build_result(raw, parse_scenario(raw), plan, Limits(), self.tariffs)
        condition = next(c for c in result['cargo_condition'] if c['cargo_id'] == target['cargo_id'])
        self.assertGreater(condition['expired_t'], 0)
        self.assertEqual(condition['physically_delivered_t'], condition['expired_t'])
        self.assertEqual(condition['usable_delivered_t'], 0)
        self.assertEqual(result['metrics']['social_deficit_t'], 55)
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
        condition['usable_delivered_t'] = condition['physically_delivered_t']
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'FAILED')
