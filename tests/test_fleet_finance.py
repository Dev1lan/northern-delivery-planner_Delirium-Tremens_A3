import copy
import unittest

from experiments.fleet_analysis import fleet_variants
from experiments.social_examples import examples
from finance_export import evaluate_cost, quantities
from optimizer import simulate
from result_adapter import format_result_v2


class FleetFinanceTests(unittest.TestCase):
    def setUp(self):
        self.scenario, self.requirements = next((s, r) for s, r in examples() if s['id'] == 'food_tradeoff')
        template = self.scenario['units'][0]
        self.assumptions = dict(source_status='SCENARIO_ASSUMPTION', compatible_template_unit_id=template['unit_id'],
                                new_barge_id='NEW_BARGE', new_tug_id='NEW_TUG', remove_unit_id=template['unit_id'])

    def test_additions_are_real_resources_and_shared_partners_are_exclusive(self):
        original = copy.deepcopy(self.scenario)
        variants = fleet_variants(self.scenario, self.assumptions)
        self.assertEqual(self.scenario, original)
        self.assertEqual(variants['REDUCED_FLEET']['units'], [])
        totals = {name: simulate(s, 'OPTIMIZED')['metrics']['delivered_total_t'] for name, s in variants.items()}
        self.assertEqual(totals['PLUS_BARGE'], totals['CURRENT_FLEET'])
        self.assertEqual(totals['PLUS_TUG'], totals['CURRENT_FLEET'])
        self.assertGreater(totals['PLUS_UNIT'], totals['CURRENT_FLEET'])
        with self.assertRaises(ValueError):
            fleet_variants(self.scenario, dict(self.assumptions, new_tug_id=original['units'][0]['tug_id']))

    def test_financial_quantities_and_explicit_rates(self):
        s = self.scenario
        internal = simulate(s, 'OPTIMIZED', requirements=self.requirements)
        v2 = format_result_v2(s, internal, self.requirements)
        report = quantities(s, v2)
        voyage = report['voyages'][0]
        self.assertEqual((voyage['loaded_km'], voyage['empty_km']), (1, 1))
        self.assertEqual(voyage['loading_seconds'], 10800)
        self.assertEqual(voyage['unloading_seconds'], 10800)
        self.assertEqual([r['busy_seconds'] for r in report['resources']], [28800, 28800])
        self.assertEqual(len(report['unmet_requirements']), 1)
        costs = evaluate_cost(report, {'currency': 'TEST', 'rates': {'loaded_km': 5, 'tug_hour': 2}})
        self.assertEqual(costs['total_cost'], '21.00')
        with self.assertRaises(ValueError):
            evaluate_cost(report, {'currency': 'TEST', 'rates': {'tug_hour': float('nan')}})
        with self.assertRaises(ValueError):
            quantities(s, dict(v2, input_fingerprint='different'))
