import json
import unittest

from jsonschema import Draft202012Validator

from experiments.run_network import SCHEMA
from network.adapter import audit_result, run_scenario
from network.costs import UNITS
from network.scheduler import COST, FCFS, REPOSITION
from tests.test_ports import queued_scenario


def tariffs(values=None):
    values = values or {}
    return {'tariff_version': 2, 'currency': 'DEMO', 'source_status': 'SYNTHETIC', 'fuel_basis': 'km',
            'rates': {k: {'value': values.get(k, 0), 'unit': 'km' if unit == 'fuel_basis' else unit,
                          'source': 'Synthetic test rates', 'source_status': 'SYNTHETIC'}
                      for k, unit in UNITS.items()}}


class CostsTests(unittest.TestCase):
    def test_cost_search_chooses_cheaper_vessel_with_equal_delivery(self):
        raw = queued_scenario()
        raw['cargo'] = raw['cargo'][:1]
        raw.pop('port_operations')
        prices = tariffs({'tug_operation': 5})
        prices['by_vessel'] = {'T1': {'tug_operation': dict(prices['rates']['tug_operation'], value=50)}}
        baseline = run_scenario(raw, REPOSITION, tariffs=prices)
        result = run_scenario(raw, COST, tariffs=prices)
        self.assertEqual(baseline['voyages'][0]['tug_id'], 'T1')
        self.assertEqual(result['voyages'][0]['tug_id'], 'T2')
        self.assertEqual(result['metrics']['delivered_on_time_t'], 100)
        self.assertEqual(result['financial_evaluation']['total_cost'], '15.00')
        self.assertEqual(baseline['financial_evaluation']['total_cost'], '150.00')
        Draft202012Validator(json.loads(SCHEMA.read_text())).validate(result)

    def test_price_uses_actual_queue_and_explicit_rates(self):
        raw = queued_scenario()
        prices = tariffs({'waiting': 7})
        result = run_scenario(raw, FCFS, tariffs=prices)['financial_evaluation']
        self.assertEqual(result['cost_by_unit']['waiting'], '28.00')
        self.assertEqual(result['cost_by_port']['B'], '28.00')
        del prices['rates']['waiting']
        with self.assertRaisesRegex(ValueError, 'Missing tariffs'):
            run_scenario(raw, FCFS, tariffs=prices)

    def test_fuel_cannot_be_charged_twice(self):
        raw = queued_scenario()
        prices = tariffs({'loaded_fuel': 1})
        prices['tug_operation_includes_fuel'] = True
        with self.assertRaisesRegex(ValueError, 'Fuel would be counted twice'):
            run_scenario(raw, FCFS, tariffs=prices)

    def test_audit_recomputes_exported_financial_evaluation(self):
        result = run_scenario(queued_scenario(), COST, tariffs=tariffs({'waiting': 7}))
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
        result['financial_evaluation']['total_cost'] = '0.01'
        report = audit_result(result)['schedule_audit']
        self.assertEqual(report['status'], 'FAILED')
        self.assertIn('financial_evaluation', report['violations'][0]['detail'])
