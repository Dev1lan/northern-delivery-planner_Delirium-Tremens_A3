"""Reproduce the synthetic Yakutia demonstration and validate exported plans."""

import argparse
import csv
from dataclasses import asdict
from itertools import chain
import json
from pathlib import Path
import platform
from time import perf_counter

from network import VERSION
from network.adapter import run_scenario
from network.analysis import change_scenario, checked, compare_results, diagnose, find_improvements
from network.models import Limits
from network.scheduler import COST, REPOSITION, STRATEGIES
from result_adapter import fingerprint

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'yakutia_demo'
LABELS = {'A': 'Без морской перегонки', 'B': 'С перегонкой, без ограничения причала',
          'C': 'С перегонкой и причалом', 'D': 'С выбранным улучшением'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def inputs():
    return (read(DATA / 'yakutia_stress.json'), read(DATA / 'improvements.json'),
            {tier: read(DATA / f'tariffs_{tier}.json') for tier in ('low', 'base', 'high')})


def calculate(*, limits=None):
    limits = limits or Limits()
    raw, options, tariffs = inputs()
    base_rates = tariffs['base']
    unrestricted = change_scenario(raw, {'kind': 'unlimited_port', 'port': 'Нижнеянск'})
    scenarios = {'A': change_scenario(unrestricted, {'kind': 'marine_reposition', 'enabled': False}),
                 'B': unrestricted, 'C': raw}
    plans = {key: run_scenario(value, limits=limits, tariffs=base_rates) for key, value in scenarios.items()}
    alternatives = find_improvements(raw, plans['C'], options, limits=limits, tariffs=base_rates)
    best = next((a for a in alternatives['alternatives'] if a['improved']), None)
    if best:
        plans['D'] = best['result']
    comparisons = [{'from': a, 'to': b, 'delta': compare_results(plans[a], plans[b])}
                   for a, b in zip(plans, list(plans)[1:])]
    strategies = {strategy: plans['C'] if strategy == REPOSITION else
                  run_scenario(raw, strategy, limits=limits, tariffs=base_rates) for strategy in (*STRATEGIES, COST)}
    sensitivity = {tier: {strategy: run_scenario(raw, strategy, limits=limits, tariffs=rate)
                          for strategy in (REPOSITION, COST)}
                   for tier, rate in tariffs.items()}
    return {
        'manifest': {'algorithm_version': VERSION, 'admission_rules': 'explicit-marine-admission-v1',
                     'data_status': raw['data_status'], 'python': platform.python_version(),
                     'parameters': asdict(limits), 'scenario_hash': fingerprint(raw),
                     'options_hash': fingerprint(options),
                     'tariff_hashes': {key: fingerprint(rate) for key, rate in tariffs.items()}},
        'source_inputs': {'scenario': raw, 'options': options, 'tariffs': tariffs},
        'labels': LABELS, 'plans': plans, 'comparisons': comparisons,
        'strategies': strategies, 'tariff_sensitivity': sensitivity,
        'diagnoses': {key: diagnose(result) for key, result in plans.items()},
        'improvements': alternatives, 'chosen_change': best['change'] if best else None,
    }


def bundle_results(bundle):
    return chain(bundle['plans'].values(), bundle['strategies'].values(),
                 (a['result'] for a in bundle['improvements']['alternatives']),
                 *(plans.values() for plans in bundle['tariff_sensitivity'].values()))


def write(path, value):
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')


def main(argv=None):
    from jsonschema import Draft202012Validator

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    started = perf_counter()
    bundle = calculate()
    schema = read(ROOT / 'result.v3.schema.json')
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for result in bundle_results(bundle):
        validator.validate(result)
        checked(result)
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output / 'bundle.json', bundle)
    with (args.output / 'comparison.csv').open('x', newline='', encoding='utf-8-sig') as output:
        writer = csv.writer(output)
        writer.writerow(['variant', 'on_time_t', 'delivered_t', 'critical_on_time_t', 'social_deficit_t',
                         'undelivered_t', 'empty_km', 'port_waiting_seconds', 'total_cost', 'currency', 'audit'])
        for key, result in bundle['plans'].items():
            m, f = result['metrics'], result['financial_evaluation']
            writer.writerow([key, m['delivered_on_time_t'], m['delivered_total_t'], m['critical_on_time_t'],
                             m['social_deficit_t'], m['undelivered_t'], m['empty_km'],
                             sum(p['waiting_seconds'] for p in result.get('port_operations', {}).values()),
                             f['total_cost'], f['currency'], result['schedule_audit']['status']])
    write(args.output / 'run_info.json', {'elapsed_seconds': round(perf_counter() - started, 3),
                                         'bundle_fingerprint': fingerprint(bundle)})
    print(f'Validated {sum(1 for _ in bundle_results(bundle))} result records: {args.output}')


if __name__ == '__main__':
    main()
