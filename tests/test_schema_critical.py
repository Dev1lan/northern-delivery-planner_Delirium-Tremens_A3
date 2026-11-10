import copy
import json
import unittest
from pathlib import Path
from jsonschema import Draft202012Validator
from result_adapter import run_scenario
from critical_cargo import compare_critical_cargo

ROOT=Path(__file__).resolve().parent.parent
class SchemaAndCriticalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema=json.loads((ROOT/'result.schema.json').read_text(encoding='utf-8-sig'))
        Draft202012Validator.check_schema(schema)
        cls.validator=Draft202012Validator(schema)
        cls.fcfs=run_scenario(ROOT/'data/demo/scenario',strategy='FCFS')
        cls.optimized=run_scenario(ROOT/'data/demo/scenario',strategy='OPTIMIZED')
    def test_actual_results_match_json_schema(self):
        for result in (self.fcfs,self.optimized):
            with self.subTest(strategy=result['strategy']):
                self.assertEqual(list(self.validator.iter_errors(result)),[])
    def test_real_validator_rejects_invalid_results(self):
        self.assertTrue(self.fcfs['trips'])
        bad=[]
        x=copy.deepcopy(self.fcfs); del x['scenario_id']; bad.append(x)
        x=copy.deepcopy(self.fcfs); x['metrics']['delivered_total_t']=-1; bad.append(x)
        x=copy.deepcopy(self.fcfs); x['trips'][0]['on_time']='yes'; bad.append(x)
        x=copy.deepcopy(self.fcfs); x['strategy']='UNKNOWN'; bad.append(x)
        x=copy.deepcopy(self.fcfs); x['trips'][0]['events'][0]['direction']='sideways'; bad.append(x)
        for i,x in enumerate(bad):
            with self.subTest(case=i): self.assertTrue(list(self.validator.iter_errors(x)))
    def test_critical_cargo_warning_and_missing(self):
        a=copy.deepcopy(self.fcfs); b=copy.deepcopy(self.optimized)
        for result in (a,b):
            result['metrics']['delivered_by_type_t']['food']=10.0
            result['metrics']['on_time_by_type_t']['food']=10.0
        b['metrics']['delivered_by_type_t']['food']=3.0; b['metrics']['on_time_by_type_t']['food']=4.0
        result=compare_critical_cargo(a,b,['food','nonexistent'])
        self.assertEqual(len(result['warnings']),2)
        self.assertEqual(result['warnings'][0]['code'],'CRITICAL_CARGO_DECREASE')
        self.assertEqual(result['comparisons'][1]['status'],'not_present')
        b['scenario_id']='different'
        with self.assertRaisesRegex(ValueError,'different scenarios'): compare_critical_cargo(a,b)

if __name__=='__main__': unittest.main()
