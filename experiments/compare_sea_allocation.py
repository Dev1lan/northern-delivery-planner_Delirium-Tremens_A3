"""Compare the published E1/E1P experiment and joint allocation on one E1 input."""

import argparse
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from time import perf_counter

from network.adapter import run_scenario
from result_adapter import fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference_script', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=45)
    args = parser.parse_args()
    if args.seconds < 3:
        parser.error('--seconds must be at least 3')
    path = args.reference_script.resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('published_sea_experiments', path)
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)
    experiment.BUDGET = args.seconds
    raw = experiment.variant('E1')
    baseline_fingerprint = fingerprint(raw)
    sea_cargos = experiment.sea_cargo(raw)
    cases = [
        ('E1', lambda: experiment.plan(raw, args.seconds)),
        ('E1P', lambda: experiment.two_pass(raw)),
        ('JOINT', lambda: run_scenario(raw, experiment.STRATEGY, limits=experiment.LIMITS,
                                      tariffs=experiment.TARIFFS,
                                      search_policy=replace(experiment.policy(args.seconds), sea_fleet_allocation=True))),
    ]
    args.output.mkdir(parents=True, exist_ok=False)
    rows = {}
    for label, calculate in cases:
        started = perf_counter()
        result = calculate()
        row = experiment.summarize('E1P' if label == 'E1P' else 'E1', raw, result, perf_counter() - started)
        row.pop('_used')
        row['name'] = label
        if label == 'JOINT':
            row['title'] = 'Joint allocation'
        if row['audit'] != 'PASSED' or result['input_fingerprint'] != baseline_fingerprint:
            raise ValueError(f'{label}: audit or scenario fingerprint mismatch')
        row['river_delivered_t'] = round(row['delivered_total_t'] - row['sea_delivered_t'], 3)
        row['empty_km'] = result['metrics']['empty_km']
        row['sea_cargo_deliveries_t'] = {cargo['cargo_id']: cargo['delivered_t'] for cargo in result['cargo_status']
                                         if cargo['cargo_id'] in sea_cargos}
        rows[label] = row
        (args.output / f'{label.lower()}.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        print(label, row['delivered_on_time_t'], row['sea_delivered_t'], row['seconds'], flush=True)
    report = {'input_fingerprint': baseline_fingerprint, 'tariff_fingerprint': fingerprint(experiment.TARIFFS),
              'reference_script_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'budget_seconds': args.seconds, 'runs': rows}
    (args.output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
