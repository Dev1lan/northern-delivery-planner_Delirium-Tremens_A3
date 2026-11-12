import copy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator, ValidationError

from experiments.run_network import DEMO, SCHEMA, run
from network.adapter import audit_result, decode_voyages, run_scenario
from network.financial import evaluate_schedule
from network.models import Limits, parse_scenario, timestamp
from network.replanning import replan
from network.scheduler import FCFS, GREEDY, REPOSITION, RETURN_EMPTY, SOCIAL
from network.validator import audit


def load(name):
    return json.loads((DEMO / f'{name}.json').read_text(encoding='utf-8'))


class RoutingTests(unittest.TestCase):
    def test_abc_uses_the_same_pair_and_stays_in_c(self):
        result = run_scenario(load('a_chain'))
        first, second = result['voyages']
        self.assertEqual((first['cargo_id'], second['cargo_id']), ('AB', 'BC'))
        self.assertEqual((first['tug_id'], first['barge_id']), (second['tug_id'], second['barge_id']))
        self.assertEqual(first['unloading_end'], second['load_start'])
        self.assertEqual(result['metrics']['delivered_on_time_t'], 180)
        self.assertEqual(result['metrics']['empty_km'], 0)
        self.assertEqual({v['current_port'] for v in result['final_resources']}, {'C'})

    def test_single_cargo_can_cross_two_edges_without_reloading(self):
        raw = load('a_chain')
        raw['cargo'] = [dict(raw['cargo'][0], destination_port='C')]
        result = run_scenario(raw, FCFS)
        self.assertEqual([e['edge_id'] for e in result['voyages'][0]['loaded']], ['AB', 'BC'])
        self.assertEqual([e['origin_port'] for e in result['events'] if e['state'] == 'LOADING'], ['A'])
        self.assertEqual(result['metrics']['tonne_km'], 2200)

    def test_backhaul_beats_return_empty_policy(self):
        raw = load('b_backhaul')
        self.assertEqual(run_scenario(raw)['metrics']['delivered_on_time_t'], 180)
        old = run_scenario(raw, RETURN_EMPTY)
        self.assertEqual(old['metrics']['delivered_on_time_t'], 100)
        self.assertEqual(old['metrics']['empty_km'], 10)

    def test_reposition_is_linked_to_the_abc_chain_and_adds_tonnage(self):
        raw = load('c_reposition_chain')
        local = run_scenario(raw, SOCIAL)
        result = run_scenario(raw, REPOSITION)
        self.assertEqual(local['metrics']['delivered_on_time_t'], 50)
        self.assertEqual(result['metrics']['delivered_on_time_t'], 180)
        first, second = result['voyages']
        self.assertEqual([e['edge_id'] for e in first['empty']], ['CB', 'BA'])
        self.assertEqual([v['cargo_id'] for v in (first, second)], ['AB', 'BC'])
        self.assertEqual(first['tug_id'], second['tug_id'])
        self.assertFalse(second['empty'])
        self.assertEqual(result['reposition_comparison']['delta_on_time_t'], 130)
        self.assertTrue(result['reposition_comparison']['selected_repositioning'])
        self.assertEqual(result['reposition_comparison']['decision_reason'], 'IMPROVES_ON_TIME_TONNES')
        self.assertEqual(run_scenario(raw, REPOSITION), result)

    def test_late_or_equivalent_reposition_is_rejected(self):
        raw = load('d_reposition_rejected')
        for km in (80, 1):
            with self.subTest(km=km):
                next(e for e in raw['edges'] if e['edge_id'] == 'DA')['distance_km'] = km
                result = run_scenario(raw)
                self.assertEqual(result['metrics']['delivered_on_time_t'], 40)
                self.assertEqual(result['voyages'][0]['tug_id'], 'T_LOCAL')
                self.assertFalse(result['reposition_comparison']['selected_repositioning'])

    def test_social_minimum_overrides_total_tonnes(self):
        raw = load('e_social_tradeoff')
        greedy = run_scenario(raw, GREEDY)
        social = run_scenario(raw, SOCIAL)
        self.assertEqual(greedy['metrics']['delivered_on_time_t'], 100)
        self.assertEqual(greedy['metrics']['social_deficit_t'], 30)
        self.assertEqual(greedy['feasibility_status'], 'UNKNOWN')
        self.assertEqual(social['requirements_status'], 'SATISFIED')
        self.assertEqual(social['metrics']['delivered_on_time_t'], 30)
        raw['social_requirements'][0]['required_t'] = 30.001
        impossible = run_scenario(raw)
        self.assertEqual(impossible['feasibility_status'], 'INFEASIBLE')
        self.assertEqual(impossible['requirements'][0]['deficit_lower_bound_t'], .001)

    def test_partial_loads_keep_exact_balance(self):
        raw = load('a_chain')
        raw['cargo'] = raw['cargo'][:1]
        raw['barges'][0]['capacity_t'] = 60
        raw['tugs'].append(dict(raw['tugs'][0], vessel_id='T2'))
        raw['barges'].append(dict(raw['barges'][0], vessel_id='B2'))
        raw['compatible_pairs'].append({'tug_id': 'T2', 'barge_id': 'B2'})
        result = run_scenario(raw)
        self.assertEqual(sorted(v['volume_t'] for v in result['voyages']), [40, 60])
        self.assertEqual(result['cargo_status'][0]['remaining_t'], 0)

    def test_wait_for_reopening_and_check_all_days_crossed(self):
        raw = load('a_chain')
        raw['end'] = '2026-07-03T04:00:00'
        for vessel in (*raw['tugs'], *raw['barges']):
            vessel.update(available_at='2026-07-01T23:30:00', available_to=raw['end'])
        raw['cargo'] = [dict(raw['cargo'][0], volume_t=5, due_at=raw['end'])]
        raw['edges'][0]['daily_conditions'] = {'2026-07-02': {'is_closed': True}}
        result = run_scenario(raw, FCFS)
        self.assertEqual(result['voyages'][0]['loaded'][0]['depart'], '2026-07-03T00:00:00')
        self.assertTrue(any(e['state'] == 'WAITING' for e in result['events']))
        move, = decode_voyages(result['voyages'])
        leg = replace(move.loaded[0], depart=timestamp('2026-07-01T23:33:00', 'start'),
                      arrive=timestamp('2026-07-02T00:33:00', 'end'))
        fake = replace(move, loaded=(leg,), unloading_end=timestamp('2026-07-02T00:36:00', 'end'))
        report = audit(parse_scenario(raw), [fake])
        self.assertEqual(report['schedule_audit']['violations'][0]['code'], 'UNSAFE_PASSAGE')

    def test_draft_and_missing_directed_path(self):
        self.assertEqual(run_scenario(load('f_blocked'))['voyages'], [])
        raw = load('a_chain')
        raw['edges'] = raw['edges'][:2]
        raw['cargo'] = [dict(raw['cargo'][0], origin_port='C', destination_port='A')]
        for vessel in (*raw['tugs'], *raw['barges']):
            vessel['current_port'] = 'C'
        self.assertEqual(run_scenario(raw)['voyages'], [])

    def test_action_limits_are_respected(self):
        result = run_scenario(load('a_chain'), limits=Limits(max_actions=1, max_states=1))
        self.assertLessEqual(len(result['voyages']), 1)
        self.assertEqual(result['termination_reason'], 'SEARCH_LIMIT')


class AuditTests(unittest.TestCase):
    def test_wrong_port_and_two_resources_in_different_ports(self):
        raw = load('a_chain')
        result = run_scenario(raw)
        moves = decode_voyages(result['voyages'])
        report = audit(parse_scenario(raw), [moves[1]])
        self.assertEqual(report['schedule_audit']['violations'][0]['code'], 'WRONG_PORT')
        raw['barges'][0]['current_port'] = 'D'
        self.assertEqual(run_scenario(raw)['voyages'], [])
        report = audit(parse_scenario(raw), [moves[0]])
        self.assertEqual(report['schedule_audit']['violations'][0]['code'], 'DIFFERENT_PORTS')

    def test_shared_tug_and_shared_barge_cannot_be_double_booked(self):
        raw = load('g_shared_tug')
        result = run_scenario(raw)
        self.assertEqual(result['metrics']['delivered_on_time_t'], 100)
        move, = decode_voyages(result['voyages'])
        new_cargo = 'AB2' if move.cargo_id == 'AB1' else 'AB1'
        other_barge = 'B1' if move.barge_id == 'B2' else 'B2'
        fake = replace(move, barge_id=other_barge, cargo_id=new_cargo, kg=10000)
        self.assertEqual(audit(parse_scenario(raw), [move, fake])['schedule_audit']['violations'][0]['code'], 'RESOURCE_BUSY')
        raw['tugs'].append(dict(raw['tugs'][0], vessel_id='T2'))
        raw['compatible_pairs'].append({'tug_id': 'T2', 'barge_id': move.barge_id})
        fake = replace(move, tug_id='T2', cargo_id=new_cargo)
        self.assertEqual(audit(parse_scenario(raw), [move, fake])['schedule_audit']['violations'][0]['code'], 'RESOURCE_BUSY')

    def test_modified_edge_and_event_log_fail_audit(self):
        raw = load('a_chain')
        result = run_scenario(raw)
        result['events'][0]['destination_port'] = 'D'
        self.assertEqual(audit_result(result)['schedule_audit']['violations'][0]['code'], 'EVENT_LOG_MISMATCH')
        result['voyages'][0]['loaded'][0]['edge_id'] = 'UNKNOWN'
        self.assertEqual(audit_result(result)['schedule_audit']['violations'][0]['code'], 'UNKNOWN_EDGE')

    def test_v3_schema_and_financial_tariffs(self):
        definition = json.loads(SCHEMA.read_text(encoding='utf-8'))
        Draft202012Validator.check_schema(definition)
        validator = Draft202012Validator(definition)
        result = run_scenario(load('a_chain'))
        validator.validate(result)
        with self.assertRaises(ValidationError):
            validator.validate(dict(result, schema_version='2.0.0'))
        rates = {'loaded_km': 2, 'empty_km': 0, 'loading_hour': 0, 'unloading_hour': 0,
                 'waiting_hour': 0, 'tug_busy_hour': 0, 'barge_busy_hour': 0}
        tariffs = {'currency': 'TEST', 'source_status': 'SYNTHETIC', 'rates': rates}
        priced = run_scenario(load('a_chain'), tariffs=tariffs)
        validator.validate(priced)
        self.assertEqual(priced['financial_evaluation']['total_cost'], '44.00')
        fake = copy.deepcopy(result)
        fake['financial_inputs']['totals']['loaded_km'] = 0
        with self.assertRaisesRegex(ValueError, 'audited'):
            evaluate_schedule(fake, tariffs)
        rates.pop('empty_km')
        with self.assertRaisesRegex(ValueError, 'empty_km'):
            evaluate_schedule(result, tariffs)

    def test_runner_outputs_are_exclusive(self):
        with TemporaryDirectory(dir=SCHEMA.parent / 'results') as folder:
            output = Path(folder) / 'run'
            paths = [DEMO / 'a_chain.json']
            report = run(paths, output, strategies=(SOCIAL, REPOSITION))
            self.assertEqual(report['schedules'], 2)
            with self.assertRaises(FileExistsError):
                run(paths, output, strategies=(SOCIAL, REPOSITION))


class ReplanningTests(unittest.TestCase):
    def setUp(self):
        self.raw = load('h_replan')
        self.previous = run_scenario(self.raw)
        self.revised = copy.deepcopy(self.raw)

    def test_close_remaining_route_add_new_request_and_keep_executed_leg(self):
        self.revised['edges'][1]['condition']['is_closed'] = True
        config = load('updates/h_replan')
        self.revised['cargo'].extend(config['new_cargo'])
        result = replan(self.previous, self.revised, config['as_of'], resource_updates=config['resource_updates'],
                        delivered_t=config['delivered_t'], remaining_t=config['remaining_t'])
        self.assertEqual(result['status'], 'REPLANNED')
        self.assertEqual(result['locked_events'], [e for e in self.previous['events'] if e['voyage_id'] == 1])
        self.assertEqual(result['delivered_t'], {'AB': 100})
        self.assertEqual(result['observed_resources']['T1']['state'], 'UNAVAILABLE')
        new, = result['remaining_plan']['voyages']
        self.assertEqual((new['cargo_id'], new['load_start']), ('BD_URGENT', '2026-07-01T04:30:00'))
        self.assertEqual(audit_result(result['remaining_plan'])['schedule_audit']['status'], 'PASSED')

    def test_started_voyage_is_reserved_or_requires_intervention(self):
        as_of = '2026-07-01T01:30:00'
        result = replan(self.previous, self.revised, as_of, resource_updates={'T1': {'edge_id': 'AB'}})
        self.assertEqual(result['locked_voyages'], 1)
        self.assertEqual(result['observed_resources']['T1']['state'], 'LOADED_TRANSIT')
        self.assertEqual(result['delivered_t'], {})
        self.assertEqual([v['cargo_id'] for v in result['remaining_plan']['voyages']], ['BC'])
        self.revised['edges'][0]['condition']['usable_depth_m'] = .25
        blocked = replan(self.previous, self.revised, as_of)
        self.assertEqual(blocked['status'], 'REQUIRES_INTERVENTION')
        self.assertEqual(blocked['reason'], 'ONGOING_PASSAGE_UNSAFE')
        self.assertIsNone(blocked['remaining_plan'])

    def test_no_remaining_jobs_and_position_disagreement(self):
        self.revised['edges'][1]['condition']['is_closed'] = True
        result = replan(self.previous, self.revised, '2026-07-01T04:00:00')
        self.assertEqual(result['remaining_plan']['voyages'], [])
        wrong = replan(self.previous, self.revised, '2026-07-01T04:00:00',
                       resource_updates={'T1': {'current_port': 'A'}})
        self.assertEqual(wrong['reason'], 'POSITION_MISMATCH')
