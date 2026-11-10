import copy
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from generate import read_csv, check_passage, simulate as legacy_simulate
from optimizer import load, simulate
from scenario_validation import ScenarioValidationError, validate_folder, validate_inputs

ROOT=Path(__file__).resolve().parent.parent
DEMO=ROOT/'data/demo/scenario'

class ScenarioInputTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
        self.cargo=read_csv(DEMO/'cargo.csv'); self.units=read_csv(DEMO/'units.csv'); self.conditions=read_csv(DEMO/'conditions.csv')
    def run_validation(self):
        return validate_inputs(self.config,self.cargo,self.units,self.conditions)
    def test_complete_and_totals_declared(self):
        r=validate_folder(DEMO,self.config)
        self.assertTrue(r['category_totals_checked']); self.assertTrue(r['plan_totals_checked'])
        self.assertEqual(r['cargo_by_type_t']['coal'],677)
        no_totals=self.run_validation()
        self.assertFalse(no_totals['category_totals_checked']); self.assertFalse(no_totals['plan_totals_checked'])
    def test_missing_calendar_is_error_not_closed(self):
        row=self.conditions.pop(0)
        with self.assertRaisesRegex(ScenarioValidationError,'Missing condition.*segment'):
            self.run_validation()
        event={'segment_id':row['segment_id'],'start':row['date']+'T12:00:00','end':row['date']+'T13:00:00'}
        with self.assertRaisesRegex(ValueError,'Missing condition.*not closure'):
            check_passage(event,.1,{},.15)
        s=load(DEMO,self.config); s['conditions'].pop(0)
        with self.assertRaisesRegex(ScenarioValidationError,'Missing condition'):
            simulate(s,'FCFS')
        with self.assertRaisesRegex(ScenarioValidationError,'Missing condition'):
            legacy_simulate(self.config, [], [], self.conditions, 'baseline')
    def test_explicit_closed_is_valid(self):
        self.conditions[0]['is_closed']='1'; self.run_validation()
    def test_missing_declared_totals_fails_folder_validation(self):
        real_exists=Path.exists
        def without_category_totals(path):
            return False if path.name=='expected_cargo_totals.csv' else real_exists(path)
        with patch.object(Path,'exists',without_category_totals):
            with self.assertRaisesRegex(ScenarioValidationError,'missing expected_cargo_totals'):
                validate_folder(DEMO,self.config)
    def test_calendar_key_must_be_iso_date(self):
        self.conditions[0]['date'] += 'T00:00:00'
        with self.assertRaisesRegex(ScenarioValidationError,'YYYY-MM-DD'): self.run_validation()
    def test_invalid_windows(self):
        self.config['end']='2026-01-01'
        with self.assertRaisesRegex(ScenarioValidationError,'end precedes'): self.run_validation()
        self.config['end']='2026-09-30'; self.units[0]['available_to']=self.units[0]['available_from']
        with self.assertRaisesRegex(ScenarioValidationError,'available_to'): self.run_validation()
    def test_invalid_numbers(self):
        for key,value in [('speed_upstream_kmh','0'),('handling_tph','-1'),('capacity_t','NaN')]:
            with self.subTest(key=key):
                saved=self.units[0][key]; self.units[0][key]=value
                with self.assertRaises(ScenarioValidationError): self.run_validation()
                self.units[0][key]=saved
    def test_ids_and_links(self):
        self.cargo[1]['cargo_id']=self.cargo[0]['cargo_id']
        with self.assertRaisesRegex(ScenarioValidationError,'duplicate cargo_id'): self.run_validation()
        self.cargo[1]['cargo_id']='different'; self.cargo[0]['plan_id']='UNKNOWN'
        with self.assertRaisesRegex(ScenarioValidationError,'unknown plan_id'): self.run_validation()
        self.cargo[0]['plan_id']='P_OIL'
        with self.assertRaisesRegex(ScenarioValidationError,'plan/category mismatch'): self.run_validation()
    def test_invalid_balance(self):
        with self.assertRaisesRegex(ScenarioValidationError,'category totals'):
            validate_inputs(self.config,self.cargo,self.units,self.conditions,expected_by_type={'coal':1})
        totals=validate_folder(DEMO,self.config)['cargo_by_plan_t']; totals['P_COAL']+=1
        with self.assertRaisesRegex(ScenarioValidationError,'plan totals P_COAL'):
            validate_inputs(self.config,self.cargo,self.units,self.conditions,expected_by_plan=totals)
    def test_duplicate_and_unknown_segment(self):
        self.conditions.append(copy.deepcopy(self.conditions[0]))
        with self.assertRaisesRegex(ScenarioValidationError,'duplicate condition'): self.run_validation()
        self.conditions.pop(); self.conditions[0]['segment_id']='UNKNOWN'
        with self.assertRaisesRegex(ScenarioValidationError,'unknown segment'): self.run_validation()

if __name__=='__main__': unittest.main()
