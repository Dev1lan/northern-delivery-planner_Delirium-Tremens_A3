import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from generate import dt
from optimizer import load, simulate
from result_adapter import CONTRACT_VERSION, DEFAULT_CONFIG, METRIC_FIELDS, run_scenario

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / 'data/scenarios/r1_baseline_central_central'
if not SCENARIO.exists(): SCENARIO = ROOT / 'data/demo/scenario'

class ResultAdapterTests(unittest.TestCase):
    def test_both_strategies_preserve_metrics_and_events(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding='utf-8'))
        source = load(SCENARIO, config)
        for strategy in ('FCFS', 'OPTIMIZED'):
            with self.subTest(strategy=strategy):
                expected = simulate(source, strategy)
                result = run_scenario(SCENARIO, strategy=strategy)
                self.assertEqual(result['schema_version'], CONTRACT_VERSION)
                self.assertEqual(result['scenario_id'], SCENARIO.name)
                self.assertEqual(result['strategy'], strategy)
                for key in METRIC_FIELDS:
                    self.assertEqual(result['metrics'][key], expected['metrics'][key])
                self.assertEqual(len(result['trips']), result['metrics']['completed_trips'])
                self.assertTrue(result['trips'])
                self.assertAlmostEqual(sum(t['volume_t'] for t in result['trips']), result['metrics']['delivered_total_t'], places=3)
                self.assertAlmostEqual(sum(t['volume_t'] for t in result['trips'] if t['on_time']), result['metrics']['delivered_on_time_t'], places=3)
                for trip, original in zip(result['trips'], expected['trips']):
                    self.assertEqual(trip['events'], json.loads(original['events_json']))
                    self.assertEqual(trip['on_time'], dt(trip['unloading_end']) <= dt(trip['due_date']))
                    self.assertNotIn('events_json', trip)
                self.assertEqual(set(result['metrics']['delivered_by_type_t']), {x['cargo_type'] for x in source['cargo']})
                json.dumps(result, allow_nan=False)

    def test_empty_demand_and_validation(self):
        scenario = load(SCENARIO, json.loads(DEFAULT_CONFIG.read_text(encoding='utf-8')))
        scenario['cargo'] = []
        with patch('result_adapter.load', return_value=copy.deepcopy(scenario)):
            result = run_scenario(SCENARIO, scenario_id='empty')
        self.assertEqual(result['scenario_id'], 'empty')
        self.assertEqual(result['trips'], [])
        self.assertEqual(result['metrics']['delivered_total_t'], 0)
        self.assertEqual(result['metrics']['delivered_by_type_t'], {})
        for kwargs in ({'strategy':'INVALID'}, {'candidate_limit':0}, {'candidate_limit':True}, {'scenario_id':'  '}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_scenario(SCENARIO, **kwargs)

    def test_audit_failure_is_not_reported_as_success(self):
        with patch('result_adapter.simulate', side_effect=AssertionError('audit failed')):
            with self.assertRaisesRegex(AssertionError, 'audit failed'):
                run_scenario(SCENARIO)

    def test_cli_stdout_is_json_and_explicit_id(self):
        completed = subprocess.run([sys.executable, '-B', str(ROOT/'result_adapter.py'),
                                    '--scenario', str(SCENARIO), '--strategy', 'FCFS',
                                    '--scenario-id', 'yana_001'], check=True,
                                   capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(completed.stderr, '')
        output = json.loads(completed.stdout)
        self.assertEqual((output['scenario_id'], output['strategy']), ('yana_001', 'FCFS'))

    def test_schema_required_fields_match_result(self):
        schema = json.loads((ROOT/'result.schema.json').read_text(encoding='utf-8-sig'))
        result = run_scenario(SCENARIO, strategy='FCFS')
        self.assertEqual(set(result), set(schema['required']))
        self.assertEqual(set(result['metrics']), set(schema['properties']['metrics']['required']))
        self.assertEqual(set(result['trips'][0]), set(schema['$defs']['trip']['required']))
        self.assertEqual(set(result['trips'][0]['events'][0]), set(schema['$defs']['event']['required']))

if __name__ == '__main__': unittest.main()
