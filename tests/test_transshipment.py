from copy import deepcopy
from pathlib import Path
import json
import unittest

from jsonschema import Draft202012Validator

from network.adapter import audit_result, run_scenario
from network.scheduler import COST
from network.search_policy import SearchPolicy
from tests.test_costs import tariffs
from tests.test_network import load


def transfer_scenario():
    raw = load('a_chain')
    raw['cargo'] = [dict(raw['cargo'][0], destination_port='C', volume_t=150)]
    raw['edges'] = [e for e in raw['edges'] if e['edge_id'] in ('AB', 'BC')]
    raw['tugs'].append(dict(raw['tugs'][0], vessel_id='SEA', current_port='B',
                           draft_empty_m=1.2, vessel_class='COAST', navigation_permits=[
                               {'area': 'TEST', 'status': 'SCENARIO_ASSUMPTION', 'source': 'fixture'}]))
    raw['barges'].append(dict(raw['barges'][0], vessel_id='SEA_B', current_port='B',
                             capacity_t=80, vessel_class='COAST',
                             navigation_permits=raw['tugs'][1]['navigation_permits']))
    pair = {'tug_id': 'SEA', 'barge_id': 'SEA_B'}
    raw['compatible_pairs'].append(pair)
    raw['edges'][0]['condition'] = {'usable_depth_m': 1.2, 'max_allowed_draft_m': 1.05}
    raw['edges'][1].update(water_type='SEA', navigation_area='TEST', allowed_pairs=[pair])
    raw['port_operations'] = {'B': {'berths': [{'berth_id': 'B1', 'operations': ['LOADING', 'UNLOADING'],
                                                'handling_tph': {'coal': 100}}]}}
    raw['transshipment_ports'] = {'B': {'cargo_types': ['coal'], 'directions': ['RIVER_TO_SEA'],
                                        'handling_seconds': 600, 'storage_capacity_t': 80,
                                        'transfer_cost_per_t': 3, 'currency': 'DEMO'}}
    raw['edges'].append(dict(raw['edges'][0], edge_id='BA', origin_port='B', destination_port='A'))
    raw['edges'].append(dict(raw['edges'][1], edge_id='CB', origin_port='C', destination_port='B'))
    return raw


class TransshipmentTests(unittest.TestCase):
    def test_two_batches_count_only_at_final_port(self):
        raw = transfer_scenario()
        direct = run_scenario(raw, search_policy=SearchPolicy(disable_transshipment=True))
        plan = run_scenario(raw, tariffs=tariffs({'loaded_fuel': 1}))
        self.assertEqual(direct['metrics']['delivered_total_t'], 0)
        self.assertEqual(plan['metrics']['delivered_total_t'], 150)
        self.assertEqual(plan['cargo_status'][0]['remaining_t'], 0)
        self.assertEqual(len({v['batch_id'] for v in plan['voyages']}), 2)
        self.assertEqual(plan['financial_evaluation']['transshipment_cost'], '450.00')
        self.assertEqual(audit_result(plan)['schedule_audit']['status'], 'PASSED')
        schema = Path(__file__).resolve().parents[1] / 'result.v3.schema.json'
        Draft202012Validator(json.loads(schema.read_text())).validate(plan)

    def test_closed_sea_or_incompatible_terminal_and_barge(self):
        for reason in ('window', 'terminal', 'barge'):
            with self.subTest(reason=reason):
                raw = transfer_scenario()
                if reason == 'window':
                    raw['edges'][1]['navigation_windows'] = []
                elif reason == 'terminal':
                    raw['transshipment_ports']['B']['cargo_types'] = ['food']
                else:
                    raw['cargo'][0]['cargo_type'] = 'fuel'
                    raw['barges'][0]['cargo_types_allowed'] = ['fuel']
                    raw['transshipment_ports']['B']['cargo_types'] = ['fuel']
                    raw['port_operations']['B']['berths'][0]['handling_tph'] = {'fuel': 100}
                self.assertEqual(run_scenario(raw)['metrics']['delivered_total_t'], 0)

    def test_audit_rejects_early_use_duplicate_and_overload(self):
        good = run_scenario(transfer_scenario())
        first = next(v for v in good['voyages'] if v['stage'] == 1)
        for reason, code in [('early', 'TRANSFER_BEFORE_READY'), ('duplicate', 'DUPLICATE_TRANSFER_STAGE'),
                             ('mass', 'INVALID_TRANSFER_CHAIN'), ('storage', 'TRANSFER_STORAGE_CAPACITY')]:
            with self.subTest(reason=reason):
                bad = deepcopy(good)
                second = next(v for v in bad['voyages'] if v['batch_id'] == first['batch_id'] and v['stage'] == 2)
                if reason == 'early':
                    second['load_start'] = first['unloading_end']
                elif reason == 'duplicate':
                    bad['voyages'].append(dict(second))
                elif reason == 'mass':
                    second['volume_t'] += 1
                else:
                    duplicate = [dict(v, batch_id='FAKE') for v in bad['voyages']
                                 if v['batch_id'] == first['batch_id']]
                    bad['voyages'].extend(duplicate)
                self.assertEqual(audit_result(bad)['schedule_audit']['violations'][0]['code'], code)

    def test_cost_keeps_transfer_service_floor(self):
        raw = transfer_scenario()
        prices = tariffs({'loaded_fuel': 1})
        plan = run_scenario(raw, COST, tariffs=prices)
        self.assertEqual(plan['cost_service_floor']['on_time_t'], 150)
        self.assertEqual(plan['metrics']['delivered_on_time_t'], 150)
        self.assertEqual(audit_result(plan)['schedule_audit']['status'], 'PASSED')

    def test_audit_detects_resource_overlap_and_total_overcommit(self):
        raw = transfer_scenario()
        raw['transshipment_ports']['B'].pop('storage_capacity_t')
        raw['transshipment_ports']['B']['storage_assumption'] = 'UNLIMITED'
        good = run_scenario(raw)
        batch = good['voyages'][0]['batch_id']
        duplicate = [dict(v, batch_id='FAKE') for v in good['voyages'] if v['batch_id'] == batch]
        bad = deepcopy(good)
        bad['voyages'][1:1] = duplicate
        violations = audit_result(bad)['schedule_audit']['violations']
        self.assertEqual(violations[0]['code'], 'RESOURCE_BUSY')
        bad = deepcopy(good)
        raw['cargo'][0]['volume_t'] -= 1
        from result_adapter import fingerprint
        bad['scenario'] = raw
        bad['input_fingerprint'] = fingerprint(raw)
        self.assertEqual(audit_result(bad)['schedule_audit']['violations'][0]['code'], 'CARGO_BALANCE')

    def test_cost_prefers_cheaper_direct_when_service_is_equal(self):
        raw = transfer_scenario()
        raw['cargo'][0]['volume_t'] = 80
        raw['edges'][0]['condition'] = {'usable_depth_m': 2, 'max_allowed_draft_m': 1.8}
        raw['tugs'][1]['current_port'] = raw['barges'][1]['current_port'] = 'A'
        plan = run_scenario(raw, COST, tariffs=tariffs({'loaded_fuel': 1}))
        self.assertEqual(plan['metrics']['delivered_on_time_t'], 80)
        self.assertTrue(all('stage' not in v for v in plan['voyages']))
        self.assertEqual(audit_result(plan)['schedule_audit']['status'], 'PASSED')


    def test_storage_capacity_and_transfer_currency_are_explicit(self):
        raw = transfer_scenario()
        raw['transshipment_ports']['B']['currency'] = 'RUB'
        with self.assertRaisesRegex(ValueError, 'currency'):
            run_scenario(raw, COST, tariffs=tariffs())
        raw = transfer_scenario()
        raw['transshipment_ports']['B'].pop('storage_capacity_t')
        with self.assertRaisesRegex(ValueError, 'UNLIMITED'):
            run_scenario(raw)

    def test_history_with_alternative_charge_remains_auditable(self):
        from network.adapter import decode_voyages
        from network.costs import CostModel
        from network.models import parse_scenario

        raw = transfer_scenario()
        prices = tariffs({'loaded_fuel': 1})
        prices['alternative_delivery'] = {'coal': {'unit': 'tonne', 'value': 2, 'source': 'fixture',
                                                   'source_status': 'SYNTHETIC'}}
        result = run_scenario(raw, tariffs=prices, search_policy=SearchPolicy(disable_transshipment=True))
        self.assertEqual(result['financial_evaluation']['total_cost'], '0.00')
        self.assertEqual(result['financial_evaluation']['estimated_undelivered_cost'], '300.00')
        result['financial_evaluation'] = CostModel(parse_scenario(raw), prices).breakdown(
            decode_voyages(result['voyages']), legacy=True)
        self.assertEqual(result['financial_evaluation']['total_cost'], '300.00')
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')

    def test_receiving_capacity_can_split_an_unlimited_storage_batch(self):
        raw = transfer_scenario()
        terminal = raw['transshipment_ports']['B']
        terminal.pop('storage_capacity_t')
        terminal['storage_assumption'] = 'UNLIMITED'
        result = run_scenario(raw)
        self.assertEqual(sorted(b['volume_t'] for b in result['transshipment_batches']), [70.0, 80.0])
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')

    def test_sea_to_river_uses_the_declared_direction(self):
        raw = transfer_scenario()
        raw['tugs'][0]['current_port'] = raw['barges'][0]['current_port'] = 'B'
        raw['tugs'][1]['current_port'] = raw['barges'][1]['current_port'] = 'C'
        raw['cargo'][0].update(origin_port='C', destination_port='A', volume_t=50)
        raw['transshipment_ports']['B']['directions'] = ['SEA_TO_RIVER']
        result = run_scenario(raw)
        self.assertEqual(result['metrics']['delivered_on_time_t'], 50)
        self.assertEqual([v['tug_id'] for v in result['voyages']], ['SEA', 'T1'])
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
