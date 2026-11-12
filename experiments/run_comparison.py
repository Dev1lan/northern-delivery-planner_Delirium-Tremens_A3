"""Run reproducible comparisons with separate, non-overwriting outputs."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import csv
import hashlib
from itertools import combinations
import json
from pathlib import Path
import platform
from time import perf_counter

from jsonschema import Draft202012Validator

from optimizer import ALL_STRATEGIES, load, simulate
from result_adapter import ALGORITHM_VERSION, DEFAULT_CONFIG, format_result_v2
from social_requirements import parse_requirements
from experiments.verify_final_reproducibility import ALIAS


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'result.v2.schema.json'
SOURCES = ('optimizer.py', 'generate.py', 'validate.py', 'scenario_validation.py',
           'social_requirements.py', 'result_adapter.py', 'result.v2.schema.json',
           'experiments/run_comparison.py', 'experiments/social_examples.py',
           'experiments/verify_final_reproducibility.py', 'experiments/tiny_exact.py')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_tasks(archive, config, requirements, verify):
    with (archive / 'comparison_final.csv').open(encoding='utf-8-sig', newline='') as stream:
        identifiers = sorted({row['scenario_id'] for row in csv.DictReader(stream)})
    if verify and len(identifiers) != 114:
        raise ValueError('Historical regression requires all 114 scenarios')
    for sid in identifiers:
        rep, variant = sid.split('_', 1)
        folder = archive / 'data' / 'scenarios' / f'{rep}_{ALIAS.get(variant, variant)}'
        scenario = load(folder, config)
        scenario['id'] = sid
        if variant == 'scarce_fleet':
            scenario['units'] = scenario['units'][:1]
        if variant == 'excess_fleet':
            extra = deepcopy(scenario['units'])
            for unit in extra:
                for key in ('unit_id', 'barge_id', 'tug_id'):
                    unit[key] += '_EXTRA'
            scenario['units'].extend(extra)
        yield scenario, requirements, archive / 'final_results' / sid if verify else None


def calculate(task, strategies):
    scenario, requirements, historical = task
    schema = Draft202012Validator(read_json(SCHEMA))
    minima = parse_requirements(requirements, scenario)
    critical = {t for r in minima for t in r.cargo_types} or {'food', 'food_extended'}
    results, rows, checked = {}, [], 0
    for strategy in strategies:
        start = perf_counter()
        # Compare the original result shape directly to the archived JSON.
        internal = simulate(scenario, strategy, requirements=requirements)
        duration = perf_counter() - start
        if historical is not None and strategy in ('FCFS', 'OPTIMIZED'):
            baseline = read_json(historical / f'{strategy.lower()}.json')
            actual = {k: v for k, v in internal.items()
                      if k not in ('requirements', 'requirements_status', 'feasibility_status')}
            if actual != baseline:
                raise AssertionError(f'{scenario["id"]}/{strategy} differs from archived schedule or metrics')
            checked += 1
        result = format_result_v2(scenario, internal, requirements)
        schema.validate(result)
        results[strategy] = result
        metrics = result['metrics']
        rows.append({'scenario_id': scenario['id'], 'strategy': strategy,
                     'demand_t': round(sum(float(c['volume_t']) for c in scenario['cargo']), 3),
                     **{k: v for k, v in metrics.items() if not isinstance(v, dict)},
                     'critical_on_time_t': round(sum(metrics['on_time_by_type_t'].get(t, 0) for t in critical), 3),
                     'critical_total_t': round(sum(metrics['delivered_by_type_t'].get(t, 0) for t in critical), 3),
                     'social_deficit_t': round(sum(r['deficit_t'] for r in result['requirements']), 3),
                     'requirements_status': result['requirements_status'],
                     'feasibility_status': result['feasibility_status'], 'elapsed_seconds': round(duration, 6)})
    return scenario['id'], results, rows, checked


def write_csv(path, rows):
    with path.open('x', encoding='utf-8', newline='') as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def run_experiment(tasks, strategies, output, *, workers=1, revision=None, provenance=()):
    if not tasks or len({s['id'] for s, _, _ in tasks}) != len(tasks):
        raise ValueError('Experiment needs distinct scenario identifiers')
    for scenario, requirements, _ in tasks:
        sid = scenario['id']
        if not sid or not sid.isascii() or any(not (x.isalnum() or x in '_-') for x in sid):
            raise ValueError(f'Unsafe scenario identifier: {sid!r}')
        if requirements is None:
            raise ValueError('Use [] for an experiment without social requirements')
        parse_requirements(requirements, scenario)
    if not strategies or set(strategies) - set(ALL_STRATEGIES) or len(set(strategies)) != len(strategies):
        raise ValueError('Select distinct supported strategies')
    if workers < 1:
        raise ValueError('Workers must be positive')
    schema = read_json(SCHEMA)
    Draft202012Validator.check_schema(schema)
    output.mkdir(parents=True, exist_ok=False)
    for dirname in ('inputs', 'scenarios'):
        (output / dirname).mkdir()
    for scenario, requirements, _ in tasks:
        write_json(output / 'inputs' / f'{scenario["id"]}.json', {'scenario': scenario, 'requirements': requirements})
    write_json(output / 'manifest.json', {
        'algorithm_version': ALGORITHM_VERSION, 'revision': revision, 'python': platform.python_version(),
        'platform': platform.platform(), 'strategies': list(strategies), 'workers': workers,
        'data_status': 'SYNTHETIC', 'source_sha256': {name: sha256(ROOT / name) for name in (*SOURCES, *provenance)},
        'input_sha256': {p.name: sha256(p) for p in sorted((output / 'inputs').glob('*.json'))},
        'replay': 'python -m experiments.run_comparison --snapshot-root <this-output> --output <new-path>',
    })

    def collect(item):
        nonlocal checked
        sid, results, rows, verified = item
        for result in results.values():
            schema_validator.validate(result)
        write_json(output / 'scenarios' / f'{sid}.json', {'scenario_id': sid, 'results': results})
        per_scenario[sid] = rows
        checked += verified
        count = len(per_scenario)
        if count == 1 or count % 10 == 0 or count == len(tasks):
            print(f'{count}/{len(tasks)} scenarios audited', flush=True)

    schema_validator = Draft202012Validator(schema)
    per_scenario, checked = {}, 0
    if workers == 1:
        for task in tasks:
            collect(calculate(task, strategies))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(calculate, task, strategies) for task in tasks]
            for future in as_completed(futures):
                collect(future.result())
    rows = [row for _, group in sorted(per_scenario.items()) for row in group]
    write_csv(output / 'metrics.csv', rows)
    pairs = []
    for _, group in sorted(per_scenario.items()):
        for left, right in combinations(group, 2):
            pairs.append({'scenario_id': left['scenario_id'], 'baseline': left['strategy'], 'candidate': right['strategy'],
                          **{f'delta_{k}': round(right[k] - left[k], 6) for k in
                             ('delivered_on_time_t', 'delivered_total_t', 'undelivered_t', 'critical_on_time_t',
                              'critical_total_t', 'social_deficit_t', 'completed_trips', 'fleet_utilization')}})
    write_csv(output / 'pairs.csv', pairs)
    summary = {'scenarios': len(per_scenario), 'schedules': len(rows), 'historical_matches': checked,
               'physical_violations': sum(row['violations'] for row in rows), 'by_strategy': {}}
    for name in strategies:
        group = [row for row in rows if row['strategy'] == name]
        summary['by_strategy'][name] = {
            **{f'mean_{k}': round(sum(r[k] for r in group) / len(group), 3) for k in
               ('delivered_on_time_t', 'delivered_total_t', 'critical_on_time_t', 'critical_total_t', 'social_deficit_t')},
            **{status: sum(r['feasibility_status'] == status for r in group)
               for status in ('FEASIBLE', 'INFEASIBLE', 'UNKNOWN', 'NOT_CHECKED')},
        }
    write_json(output / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--archive-root', type=Path)
    group.add_argument('--scenario', type=Path, action='append', help='Scenario CSV folder; repeat to compare multiple folders')
    group.add_argument('--examples', action='store_true')
    group.add_argument('--snapshot-root', type=Path)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--requirements', type=Path)
    parser.add_argument('--strategy', action='append', choices=ALL_STRATEGIES)
    parser.add_argument('--verify-history', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--revision')
    args = parser.parse_args(argv)
    strategies = tuple(args.strategy or ALL_STRATEGIES)
    if args.verify_history and (not args.archive_root or not {'FCFS', 'OPTIMIZED'} <= set(strategies)):
        parser.error('--verify-history requires the archive and both FCFS and OPTIMIZED')
    if args.requirements and (args.examples or args.snapshot_root):
        parser.error('Examples and snapshots carry their own requirements')
    if args.snapshot_root:
        manifest = read_json(args.snapshot_root / 'manifest.json')
        strategies = tuple(args.strategy or manifest['strategies'])
        tasks = []
        for name, digest in manifest['input_sha256'].items():
            path = args.snapshot_root / 'inputs' / name
            if sha256(path) != digest:
                raise ValueError(f'Input snapshot changed: {path}')
            value = read_json(path)
            tasks.append((value['scenario'], value['requirements'], None))
    elif args.examples:
        from experiments.social_examples import examples
        tasks = [(s, r, None) for s, r in examples()]
    else:
        config = read_json(args.config)
        requirements = read_json(args.requirements) if args.requirements else []
        if args.archive_root:
            tasks = list(archive_tasks(args.archive_root, config, requirements, args.verify_history))
        else:
            tasks = [(load(path, config), requirements, None) for path in args.scenario]
    print(json.dumps(run_experiment(tasks, strategies, args.output, workers=args.workers, revision=args.revision), indent=2))


if __name__ == '__main__':
    main()
