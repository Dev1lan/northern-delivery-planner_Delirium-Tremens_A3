"""Reproduce the small, synthetic demo using the published JSON contract."""
import argparse
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from result_adapter import run_scenario
from critical_cargo import compare_critical_cargo
from scenario_validation import validate_folder

ROOT=Path(__file__).resolve().parent.parent

def main():
    p=argparse.ArgumentParser(); p.add_argument('--write',action='store_true',help='Replace demo reference results')
    args=p.parse_args(); folder=ROOT/'data/demo/scenario'; out=ROOT/'results/demo'
    config=json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
    summary=validate_folder(folder,config)
    if not summary['category_totals_checked'] or not summary['plan_totals_checked']:
        raise ValueError('demo must have explicit category and plan totals')
    fcfs=run_scenario(folder,strategy='FCFS',scenario_id='demo')
    optimized=run_scenario(folder,strategy='OPTIMIZED',scenario_id='demo')
    schema=json.loads((ROOT/'result.schema.json').read_text(encoding='utf-8-sig'))
    Draft202012Validator.check_schema(schema); validator=Draft202012Validator(schema)
    validator.validate(fcfs); validator.validate(optimized)
    results={'fcfs.json':fcfs,'optimized.json':optimized,
             'critical_comparison.json':compare_critical_cargo(fcfs,optimized)}
    for name,value in results.items():
        path=out/name
        if args.write:
            out.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        else:
            existing=json.loads(path.read_text(encoding='utf-8-sig'))
            if existing!=value: raise AssertionError(f'Demo result differs from reference: {path}')
    print('PASS: scenario, JSON Schema, audit and reproducibility for FCFS and OPTIMIZED')
    print('Critical cargo warnings:',len(results['critical_comparison.json']['warnings']))
if __name__=='__main__': main()
