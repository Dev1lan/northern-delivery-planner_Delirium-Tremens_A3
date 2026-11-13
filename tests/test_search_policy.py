from pathlib import Path
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from network.adapter import audit_result, run_scenario
from network.search_policy import SearchPolicy


class SearchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(Path('data/yakutia_demo/yakutia_stress.json').read_text(encoding='utf-8-sig'))

    def test_timed_stop_returns_audited_prefix_and_schema_accepts_it(self):
        from network import scheduler
        original = scheduler.Scheduler.check_budget

        def stop_after_first_dispatch_move(planner):
            if planner.stats['generated_states']:
                raise scheduler.TimeLimitReached
            original(planner)

        with patch.object(scheduler.Scheduler, 'check_budget', stop_after_first_dispatch_move):
            result = run_scenario(self.raw, search_policy=SearchPolicy(mode='FAST'))
        self.assertEqual(result['termination_reason'], 'TIME_LIMIT')
        self.assertEqual(result['search_diagnostics']['budget_seconds'], 5)
        self.assertTrue(result['voyages'])
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
        schema = json.loads(Path('result.v3.schema.json').read_text())
        Draft202012Validator(schema).validate(result)

    def test_clock_deadline_and_empty_plan(self):
        with patch('network.scheduler.perf_counter', side_effect=(0, 2)):
            result = run_scenario(self.raw, search_policy=SearchPolicy(mode='RESEARCH', time_seconds=1))
        self.assertEqual(result['termination_reason'], 'TIME_LIMIT')
        self.assertFalse(result['voyages'])
        self.assertNotEqual(result['feasibility_status'], 'INFEASIBLE')
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')

    def test_optional_assignment_shortlist_is_visible(self):
        full = run_scenario(self.raw, search_policy=SearchPolicy(mode='RESEARCH'))
        short = run_scenario(self.raw, search_policy=SearchPolicy(
            top_k_pairs_per_cargo=1, max_candidates_per_state=2, max_detailed_checks_per_iteration=1))
        self.assertEqual(full['search_diagnostics']['heuristic_pruned'], 0)
        self.assertGreater(short['search_diagnostics']['heuristic_pruned'], 0)
        self.assertLess(short['search_diagnostics']['detailed_checks'], full['search_diagnostics']['detailed_checks'])
        self.assertEqual(audit_result(short)['schedule_audit']['status'], 'PASSED')
        self.assertEqual(short['input_fingerprint'], full['input_fingerprint'])
        repeat = run_scenario(self.raw)
        full.pop('search_diagnostics')
        self.assertEqual(full, repeat)

    def test_interrupt_propagates(self):
        with patch('network.scheduler.Scheduler.check_budget', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_scenario(self.raw, search_policy=SearchPolicy(mode='DEMO'))
