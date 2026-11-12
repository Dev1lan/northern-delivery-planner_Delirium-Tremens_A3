import copy
import unittest

from experiments.tiny_exact import setup
from generate import dt
from optimizer import _candidate, _improve, compare_strategies, simulate
from social_requirements import evaluate, parse_requirements
from validate import audit


def minimum(amount=0.001, deadline='2026-06-10T03:00:00', category='food'):
    return dict(requirement_id=category, cargo_types=[category], required_t=amount,
                deadline=deadline, basis='delivered_by_deadline', source_status='SCENARIO_ASSUMPTION')


class SocialPriorityTests(unittest.TestCase):
    def setUp(self):
        self.scenario = setup('deadlines')
        self.scenario['cargo'] = self.scenario['cargo'][:1]
        self.scenario['cargo'][0].update(cargo_type='food', volume_t=0.003)
        self.scenario['units'][0].update(capacity_t=0.003, reference_capacity_t=0.003,
                                        cargo_type_allowed=['food', 'coal'])

    def calculate(self, requirements):
        s = self.scenario
        result = simulate(s, 'SOCIAL_PRIORITY', requirements=requirements)
        audit(s['config'], s['cargo'], s['units'], s['conditions'], result['trips'])
        return result

    def test_smaller_load_meets_exact_deadline(self):
        result = self.calculate([minimum()])
        self.assertEqual(result['requirements_status'], 'SATISFIED')
        self.assertEqual(result['feasibility_status'], 'FEASIBLE')
        self.assertEqual(result['trips'][0]['volume_t'], 0.001)
        self.assertEqual(result['trips'][0]['unloading_end'], '2026-06-10T03:00:00')

    def test_arrival_does_not_count_and_one_kg_shortfall_is_unknown(self):
        requirements = [minimum(0.002, '2026-06-10T04:00:00')]
        legacy = simulate(self.scenario, 'FCFS', requirements=requirements)
        self.assertEqual(legacy['trips'][0]['arrival'], '2026-06-10T04:00:00')
        self.assertEqual(legacy['requirements'][0]['delivered_by_deadline_t'], 0)
        result = self.calculate(requirements)
        self.assertEqual(result['requirements'][0]['deficit_t'], 0.001)
        self.assertEqual(result['feasibility_status'], 'UNKNOWN')

    def test_multiple_categories_and_shared_tug(self):
        s = self.scenario
        s['cargo'].append(dict(s['cargo'][0], cargo_id='coal', cargo_type='coal', volume_t=0.001))
        s['units'].append(dict(s['units'][0], unit_id='u2', barge_id='b2'))
        requirements = [minimum(), minimum(deadline='2026-06-10T09:00:00', category='coal')]
        result = self.calculate(requirements)
        self.assertEqual(result['requirements_status'], 'SATISFIED')
        trips = sorted(result['trips'], key=lambda t: t['loading_start'])
        self.assertTrue(all(a['released_at'] <= b['loading_start'] for a, b in zip(trips, trips[1:])))
        self.assertEqual(compare_strategies(s, ('SOCIAL_PRIORITY', 'OPTIMIZED'), requirements=requirements)['SOCIAL_PRIORITY'], result)

    def test_shallow_water_yields_unknown_without_proof(self):
        for row in self.scenario['conditions']:
            row.update(usable_depth_m=0.1, max_allowed_draft_m=0.0)
        result = self.calculate([minimum()])
        self.assertEqual(result['trips'], [])
        self.assertEqual(result['feasibility_status'], 'UNKNOWN')

    def test_absent_fleet_and_excess_demand_are_proven_infeasible(self):
        result = self.calculate([minimum(0.004)])
        self.assertEqual(result['feasibility_status'], 'INFEASIBLE')
        self.assertEqual(result['requirements'][0]['deficit_lower_bound_t'], 0.001)
        self.scenario['units'] = []
        result = self.calculate([minimum()])
        self.assertEqual(result['requirements'][0]['reason'], 'INSUFFICIENT_DEMAND_WITH_COMPATIBLE_FLEET')
        self.assertEqual(result['feasibility_status'], 'INFEASIBLE')

    def test_unspecified_requirements_and_legacy_fallback(self):
        result = self.calculate([])
        self.assertEqual(result['requirements_status'], 'NOT_CONFIGURED')
        self.assertEqual(result['feasibility_status'], 'NOT_CHECKED')
        self.assertEqual(result['trips'], simulate(self.scenario, 'OPTIMIZED')['trips'])
        self.assertEqual(self.calculate([minimum(deadline='2026-06-11')])['selected_plan_strategy'], 'FCFS')

    def test_local_search_preserves_food_minimum(self):
        s = copy.deepcopy(self.scenario)
        s['cargo'][0].update(due_date='2026-06-10T01:00:00', volume_t=0.001)
        s['cargo'].append(dict(s['cargo'][0], cargo_id='coal', cargo_type='coal', due_date='2026-06-10T04:00:00'))
        s['_conditions'] = {(r['date'], r['segment_id']): r for r in s['conditions']}
        trip = _candidate(s, s['units'][0], s['cargo'][0], 0.001, dt(s['config']['start']))
        trip['trip_id'] = 'TR00001'
        self.assertEqual(_improve(s, [trip])[0]['cargo_type'], 'coal')
        minima = parse_requirements([minimum()], s)
        protected = _improve(s, [trip], minima)
        self.assertEqual(protected, [trip])
        self.assertEqual(evaluate(minima, s, protected)['requirements_status'], 'SATISFIED')

    def test_invalid_requirements(self):
        cases = ([minimum(0.0009)], [minimum(float('nan'))], [minimum(-1)],
                 [minimum(deadline='not-a-date')], [minimum(category='missing')],
                 [minimum(), minimum()], [minimum(), dict(minimum(), requirement_id='other')])
        for requirements in cases:
            with self.subTest(requirements=requirements), self.assertRaises(ValueError):
                parse_requirements(requirements, self.scenario)
