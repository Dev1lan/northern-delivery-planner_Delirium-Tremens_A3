"""Compare fleet additions using compatible, explicitly modeled physical resources."""

import argparse
from copy import deepcopy
import csv
from decimal import Decimal
from pathlib import Path

from experiments.run_comparison import read_json, run_experiment, write_csv, write_json
from finance_export import evaluate_cost, quantities
from optimizer import ALL_STRATEGIES, load
from result_adapter import DEFAULT_CONFIG
from scenario_validation import ident, validate_inputs


def fleet_variants(scenario, assumptions):
    if assumptions.get('source_status') != 'SCENARIO_ASSUMPTION':
        raise ValueError('New resources require an explicit SCENARIO_ASSUMPTION label')
    base = deepcopy(scenario)
    base['id'] = 'CURRENT_FLEET'
    units = {unit['unit_id']: unit for unit in base['units']}
    template = units.get(assumptions.get('compatible_template_unit_id'))
    if template is None:
        raise ValueError('Select an existing compatible template composition')
    barge = ident(assumptions.get('new_barge_id'), 'new_barge_id')
    tug = ident(assumptions.get('new_tug_id'), 'new_tug_id')
    existing_resources = {unit[key] for unit in units.values() for key in ('barge_id', 'tug_id')}
    if barge == tug or existing_resources.intersection((barge, tug)):
        raise ValueError('Additional physical resource IDs must be new and distinct')
    variants = {'CURRENT_FLEET': base}
    for name, barge_id, tug_id in (
        ('PLUS_BARGE', barge, template['tug_id']),
        ('PLUS_TUG', template['barge_id'], tug),
        ('PLUS_UNIT', barge, tug),
    ):
        if name in units:
            raise ValueError(f'Additional composition ID already exists: {name}')
        variant = deepcopy(base)
        variant['id'] = name
        variant['units'].append(dict(deepcopy(template), unit_id=name, barge_id=barge_id, tug_id=tug_id,
                                     data_status='SYNTHETIC'))
        variants[name] = variant
    if 'remove_unit_id' in assumptions:
        remove = assumptions['remove_unit_id']
        if remove not in units:
            raise ValueError(f'Unknown composition to remove: {remove}')
        reduced = deepcopy(base)
        reduced['id'] = 'REDUCED_FLEET'
        reduced['units'] = [unit for unit in reduced['units'] if unit['unit_id'] != remove]
        variants[reduced['id']] = reduced
    for variant in variants.values():
        validate_inputs(variant['config'], variant['cargo'], variant['units'], variant['conditions'])
    return variants


def run_fleet(scenario, assumptions, requirements, strategies, output, *, tariffs=None, workers=1, revision=None):
    variants = fleet_variants(scenario, assumptions)
    if tariffs is not None:
        evaluate_cost({'voyages': [], 'resources': []}, tariffs)
    run_experiment([(s, requirements, None) for s in variants.values()], strategies, output,
                   workers=workers, revision=revision, provenance=('experiments/fleet_analysis.py', 'finance_export.py'))
    write_json(output / 'fleet_assumptions.json', {'base_scenario': scenario['id'], **assumptions})
    (output / 'finance').mkdir()
    costs = {}
    for name, variant in variants.items():
        results = read_json(output / 'scenarios' / f'{name}.json')['results']
        for strategy, result in results.items():
            report = quantities(variant, result)
            if tariffs is not None:
                report['economics'] = evaluate_cost(report, tariffs)
                costs[name, strategy] = Decimal(report['economics']['total_cost'])
            write_json(output / 'finance' / f'{name}--{strategy}.json', report)
    with (output / 'metrics.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    baseline = {row['strategy']: row for row in rows if row['scenario_id'] == 'CURRENT_FLEET'}
    comparison = []
    notes = {
        'PLUS_BARGE': 'The added barge shares the compatible tug; both cannot sail concurrently.',
        'PLUS_TUG': 'The added tug shares the compatible barge; both cannot sail concurrently.',
        'PLUS_UNIT': 'Both resources are independent; effects depend on compatible demand and navigation windows.',
        'REDUCED_FLEET': 'One specified composition was removed; any shared physical resources remain in other compositions.',
    }
    for row in rows:
        name, strategy = row['scenario_id'], row['strategy']
        if name == 'CURRENT_FLEET':
            continue
        old = baseline[strategy]
        row_delta = {'scenario_id': name, 'strategy': strategy,
                           **{f'delta_{k}': round(float(row[k]) - float(old[k]), 6) for k in
                              ('delivered_on_time_t', 'delivered_total_t', 'social_deficit_t',
                               'fleet_utilization', 'completed_trips')},
                           'resource_interpretation': notes[name]}
        if tariffs is not None:
            row_delta['currency'] = tariffs['currency']
            row_delta['delta_cost'] = str(costs[name, strategy] - costs['CURRENT_FLEET', strategy])
        comparison.append(row_delta)
    write_csv(output / 'fleet_deltas.csv', comparison)
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--assumptions', type=Path, required=True)
    parser.add_argument('--requirements', type=Path, required=True)
    parser.add_argument('--tariffs', type=Path)
    parser.add_argument('--strategy', action='append', choices=ALL_STRATEGIES)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--revision')
    args = parser.parse_args()
    result = run_fleet(load(args.scenario, read_json(args.config)), read_json(args.assumptions),
                       read_json(args.requirements), tuple(args.strategy or ALL_STRATEGIES), args.output,
                       tariffs=read_json(args.tariffs) if args.tariffs else None,
                       workers=args.workers, revision=args.revision)
    print(f'{len(result)} fleet deltas against CURRENT_FLEET')


if __name__ == '__main__':
    main()
