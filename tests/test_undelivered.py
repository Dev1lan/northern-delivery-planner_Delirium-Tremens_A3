import unittest

from network.adapter import audit_result, decode_voyages, run_scenario
from network.insertion import FixedPlan, improve_fixed_plan
from network.models import Limits, parse_scenario
from network.scheduler import COST, REPOSITION, Scheduler
from network.search_policy import SearchPolicy
from tests.test_costs import tariffs
from tests.test_network import load


class UndeliveredTests(unittest.TestCase):
    def test_idle_pair_provides_missing_voyage_without_conflict(self):
        raw = load('a_chain')
        raw['tugs'].append(dict(raw['tugs'][0], vessel_id='T2', current_port='B'))
        raw['barges'].append(dict(raw['barges'][0], vessel_id='B2', current_port='B'))
        raw['compatible_pairs'].append(dict(tug_id='T2', barge_id='B2'))
        raw['barges'][0]['cargo_types_allowed'] = ['coal']
        raw['barges'][1]['cargo_types_allowed'] = ['food']
        raw['cargo'][1]['cargo_type'] = 'food'
        s = parse_scenario(raw)
        limits = Limits(max_actions=1)
        original = decode_voyages(run_scenario(raw, limits=limits)['voyages'])
        plan, detail = improve_fixed_plan(s, original, limits)
        self.assertTrue(detail['improved'])
        self.assertEqual(len(plan), 2)
        self.assertEqual(set(original) & set(plan), set(original))
        self.assertEqual(plan[-1].tug_id, 'T2')
        self.assertFalse(FixedPlan(s, plan, limits).accepts(original[0]))

    def test_idle_barge_without_compatible_tug_cannot_help(self):
        raw = load('a_chain')
        raw['barges'].append(dict(raw['barges'][0], vessel_id='IDLE'))
        s = parse_scenario(raw)
        options = FixedPlan(s, (), Limits()).candidates(s.cargo['AB'], s.cargo['AB'].kg)
        self.assertTrue(all(v.barge_id != 'IDLE' for v in options))

    def test_candidate_diversity_includes_another_base(self):
        raw = load('a_chain')
        raw['tugs'].extend(dict(raw['tugs'][0], vessel_id=f'T{i}') for i in range(2, 7))
        raw['barges'].extend(dict(raw['barges'][0], vessel_id=f'B{i}') for i in range(2, 7))
        raw['compatible_pairs'].extend(dict(tug_id=f'T{i}', barge_id=f'B{i}') for i in range(2, 7))
        raw['tugs'][-1]['current_port'] = raw['barges'][-1]['current_port'] = 'B'
        raw['barges'][-1]['cargo_types_allowed'] = ['food']
        raw['cargo'][1].update(volume_t=10, cargo_type='food')
        s = parse_scenario(raw)
        limits = Limits(candidate_limit=4)
        baseline = Scheduler(s, limits)
        improved = Scheduler(s, limits, policy=SearchPolicy(recover_undelivered=True))
        self.assertEqual({v.cargo_id for v in baseline.candidates(baseline.initial(), REPOSITION)}, {'AB'})
        self.assertIn('BC', {v.cargo_id for v in improved.candidates(improved.initial(), REPOSITION)})

    def test_cost_search_keeps_service_and_explicit_social_minimum(self):
        raw = load('a_chain')
        raw['cargo'][1]['cargo_type'] = 'food'
        raw['social_requirements'] = [{'requirement_id': 'FOOD', 'cargo_types': ['food'], 'required_t': 60,
                                       'deadline': raw['cargo'][1]['due_at'], 'basis': 'delivered_by_deadline',
                                       'source_status': 'SYNTHETIC'}]
        result = run_scenario(raw, COST, tariffs=tariffs({'tug_operation': 20}),
                              search_policy=SearchPolicy(recover_undelivered=True))
        self.assertEqual(result['requirements_status'], 'SATISFIED')
        floor = result['cost_service_floor']
        self.assertGreaterEqual(result['metrics']['delivered_on_time_t'], floor['on_time_t'])
        self.assertGreaterEqual(result['metrics']['delivered_total_t'], floor['total_t'])
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
        raw['social_requirements'] = []
        self.assertEqual(run_scenario(raw, COST, tariffs=tariffs())['requirements_status'], 'NOT_CONFIGURED')


    def test_local_repair_preserves_satisfied_minimum(self):
        raw = load('a_chain')
        raw['cargo'][0].update(cargo_id='FOOD', cargo_type='food', volume_t=30)
        raw['cargo'][1].update(cargo_id='COAL', origin_port='A', destination_port='C', volume_t=100)
        raw['social_requirements'] = [{'requirement_id': 'FOOD', 'cargo_types': ['food'], 'required_t': 30,
                                       'deadline': raw['cargo'][0]['due_at'], 'basis': 'delivered_by_deadline',
                                       'source_status': 'SYNTHETIC'}]
        scenario = parse_scenario(raw)
        limits = Limits(max_actions=1)
        original = decode_voyages(run_scenario(raw, limits=limits)['voyages'])
        self.assertEqual(original[0].cargo_id, 'FOOD')
        repaired, details = improve_fixed_plan(scenario, original, limits)
        self.assertGreater(details['audited_proposals'], 0)
        self.assertIn(original[0], repaired)

    def test_recovery_timeout_keeps_an_auditable_plan(self):
        from unittest.mock import patch
        from network.scheduler import TimeLimitReached

        raw = load('a_chain')
        def stop(planner):
            if planner.stats['generated_states']:
                raise TimeLimitReached

        with patch.object(Scheduler, 'check_budget', stop):
            result = run_scenario(raw, REPOSITION, search_policy=SearchPolicy(recover_undelivered=True))
        self.assertEqual(result['termination_reason'], 'TIME_LIMIT')
        self.assertEqual(result['feasibility_status'], 'NOT_CHECKED')
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
