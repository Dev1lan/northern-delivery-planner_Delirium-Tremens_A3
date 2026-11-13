from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

from experiments.diagnose_network_cost import compare
from experiments.run_yakutia import inputs
from network.adapter import audit_result, run_scenario
from network.models import Limits, parse_scenario
from network.scheduler import COST, REPOSITION, Scheduler
from network.search_policy import SearchPolicy, TimeLimitReached
from network.simulator import voyages
from tests.test_costs import tariffs
from tests.test_ports import queued_scenario


class CostOptimizationTests(unittest.TestCase):
    def one_job(self):
        raw = queued_scenario()
        raw.pop('port_operations')
        raw['cargo'] = raw['cargo'][:1]
        prices = tariffs({'tug_operation': 5})
        prices['by_vessel'] = {'T1': {'tug_operation': dict(prices['rates']['tug_operation'], value=50)}}
        return raw, prices

    def test_exact_one_voyage_minimum_and_reuse_of_seed(self):
        raw, prices = self.one_job()
        reference = run_scenario(raw, REPOSITION, tariffs=prices)
        parsed = parse_scenario(raw)
        cargo = next(iter(parsed.cargo.values()))
        # One batch of 100 t, one direct edge, rates of 50 and 5 per hour.
        # At full load each feasible pair takes 3 h. Splitting increases the
        # aggregate transit time and neither pair is faster.
        exact_prices = {}
        for t, b in parsed.pairs:
            tug, barge = parsed.tugs[t], parsed.barges[b]
            candidates = voyages(parsed, tug, barge, cargo, cargo.kg, tug.initial.port,
                                 max(tug.initial.available_at, barge.initial.available_at), Limits())
            direct = [move for move in candidates if move.kg == cargo.kg and move.unloading_end <= cargo.due]
            self.assertTrue(direct)
            rate = Decimal(50 if t == 'T1' else 5)
            exact_prices[t] = min(rate * Decimal((move.end - move.start).total_seconds()) / 3600
                                  for move in direct)
        result = run_scenario(raw, COST, tariffs=prices, initial_plan=reference)
        diff = compare(reference, result)
        self.assertEqual(Decimal(result['financial_evaluation']['total_cost']), min(exact_prices.values()))
        self.assertEqual(result['cost_service_floor'], {'on_time_t': 100.0, 'total_t': 100.0})
        self.assertEqual(diff['matched_changes'][0]['changed_fields'], ['barge_id', 'tug_id'])
        self.assertEqual(diff['cost_delta'], '-135.00')

    def test_timed_reference_stop_still_runs_cost_phase(self):
        raw, prices = self.one_job()
        original = Scheduler._plan

        def expire_reference(planner, strategy):
            original(planner, strategy)
            raise TimeLimitReached

        with patch.object(Scheduler, '_plan', expire_reference):
            result = run_scenario(raw, COST, tariffs=prices, search_policy=SearchPolicy(time_seconds=5))
        diagnostics = result['search_diagnostics']
        self.assertTrue(diagnostics['cost_search_started'])
        self.assertGreater(diagnostics['cost_candidates'], 0)
        self.assertEqual(result['financial_evaluation']['total_cost'], '15.00')

    def test_timeout_keeps_seed_or_a_better_plan(self):
        raw, prices = self.one_job()
        reference = run_scenario(raw, REPOSITION, tariffs=prices)
        with patch.object(Scheduler, 'search', side_effect=TimeLimitReached):
            result = run_scenario(raw, COST, tariffs=prices, initial_plan=reference)
        self.assertEqual(result['termination_reason'], 'TIME_LIMIT')
        self.assertEqual(result['financial_evaluation']['total_cost'], '15.00')
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
        result['cost_service_floor']['on_time_t'] += 1
        self.assertEqual(audit_result(result)['schedule_audit']['violations'][0]['code'], 'COST_SERVICE_FLOOR')

    def test_equal_price_and_schedule_are_allowed(self):
        raw, _, prices = inputs()
        reference = run_scenario(raw, REPOSITION, tariffs=prices['base'])
        result = run_scenario(raw, COST, tariffs=prices['base'])
        diff = compare(reference, result)
        self.assertTrue(diff['identical_voyages'])
        self.assertEqual(diff['cost_delta'], '0.00')
        self.assertEqual(reference, run_scenario(raw, REPOSITION, tariffs=prices['base']))

    def test_initial_plan_checks_scenario_identity(self):
        raw, prices = self.one_job()
        reference = run_scenario(raw, REPOSITION, tariffs=prices)
        other = deepcopy(raw)
        other['cargo'][0]['volume_t'] += 1
        with self.assertRaisesRegex(ValueError, 'scenario differs'):
            run_scenario(other, COST, tariffs=prices, initial_plan=reference)

    def test_cheap_reassignment_with_a_busy_pair_is_rejected(self):
        raw, prices = self.one_job()
        raw['cargo'].append(dict(raw['cargo'][0], cargo_id='SECOND'))
        reference = run_scenario(raw, REPOSITION, tariffs=prices)
        result = run_scenario(raw, COST, tariffs=prices, initial_plan=reference)
        self.assertEqual(result['metrics']['delivered_on_time_t'], 200)
        self.assertEqual(len({voyage['tug_id'] for voyage in result['voyages']}), 2)
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')

    def test_cost_can_choose_a_slower_shorter_path_and_charges_empty_legs(self):
        raw, _ = self.one_job()
        raw['tugs'] = raw['tugs'][:1]
        raw['barges'] = raw['barges'][:1]
        raw['compatible_pairs'] = raw['compatible_pairs'][:1]
        raw['cargo'][0]['due_at'] = '2026-07-01T23:00:00'
        direct = next(edge for edge in raw['edges'] if edge['edge_id'] == 'AB')
        raw['edges'].extend([
            dict(direct, edge_id='AD', destination_port='D', distance_km=2, speed_limit_kmh=1),
            dict(direct, edge_id='DB', origin_port='D', distance_km=2, speed_limit_kmh=1),
        ])
        raw['tugs'][0]['current_port'] = raw['barges'][0]['current_port'] = 'B'
        prices = tariffs({'loaded_fuel': 10, 'empty_fuel': 3, 'repositioning': 2,
                          'loading': 1, 'unloading': 2, 'port_call': 5})
        reference = run_scenario(raw, REPOSITION, tariffs=prices)
        result = run_scenario(raw, COST, tariffs=prices, initial_plan=reference)
        self.assertEqual([leg['edge_id'] for leg in reference['voyages'][0]['loaded']], ['AB'])
        self.assertEqual([leg['edge_id'] for leg in result['voyages'][0]['loaded']], ['AD', 'DB'])
        self.assertEqual(result['financial_evaluation']['empty_transition_cost'], '50.00')
        self.assertEqual(result['financial_evaluation']['total_cost'], '400.00')
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')

    def test_alternative_delivery_cost_can_change_preferred_cargo(self):
        raw, _ = self.one_job()
        raw['cargo'].append(dict(raw['cargo'][0], cargo_id='FOOD', cargo_type='food'))
        prices = tariffs({'loaded_fuel': 1})
        prices['alternative_delivery'] = {
            'coal': dict(value=5, unit='tonne', source='test', source_status='SYNTHETIC'),
            'food': dict(value=15, unit='tonne', source='test', source_status='SYNTHETIC'),
        }
        limits = Limits(max_actions=1)
        reference = run_scenario(raw, REPOSITION, limits=limits, tariffs=prices)
        result = run_scenario(raw, COST, limits=limits, tariffs=prices, initial_plan=reference)
        self.assertEqual(reference['voyages'][0]['cargo_id'], 'AB')
        self.assertEqual(result['voyages'][0]['cargo_id'], 'FOOD')
        self.assertEqual(result['financial_evaluation']['total_cost'], '510.00')
        self.assertEqual(result['metrics']['delivered_on_time_t'], 100)
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
