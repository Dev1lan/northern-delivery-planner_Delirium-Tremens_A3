"""Run comparable network scenarios and preserve their inputs and audited results."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import platform
import re
from time import perf_counter

from jsonschema import Draft202012Validator

from experiments.run_comparison import read_json, sha256, write_csv, write_json
from network import VERSION
from network.adapter import audit_result, run_scenario
from network.models import Limits, parse_scenario
from network.replanning import replan
from network.scheduler import REPOSITION, RETURN_EMPTY, SOCIAL, STRATEGIES


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'data' / 'network_demo'
SCHEMA = ROOT / 'result.v3.schema.json'


def run(paths, output, *, strategies=(*STRATEGIES, RETURN_EMPTY), limits=None,
        tariffs=None, revision=None, replan_demo=False):
    limits = limits or Limits()
    if not strategies or len(set(strategies)) != len(strategies) or set(strategies) - {*STRATEGIES, RETURN_EMPTY}:
        raise ValueError('Select distinct supported strategies')
    scenarios = {}
    for path in paths:
        raw = read_json(path)
        scenario = parse_scenario(raw)
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', scenario.id) or scenario.id in scenarios:
            raise ValueError(f'Unsafe or duplicate scenario ID: {scenario.id}')
        scenarios[scenario.id] = raw
    if not scenarios:
        raise ValueError('No scenarios selected')
    if replan_demo and ('h_replan' not in scenarios or REPOSITION not in strategies):
        raise ValueError('Replanning demo requires h_replan and NETWORK_REPOSITION')
    definition = read_json(SCHEMA)
    Draft202012Validator.check_schema(definition)
    validator = Draft202012Validator(definition)
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'inputs'
    inputs.mkdir()
    for sid, raw in scenarios.items():
        write_json(inputs / f'{sid}.json', raw)
    if tariffs is not None:
        write_json(output / 'tariffs.json', tariffs)
    sources = [*sorted((ROOT / 'network').glob('*.py')), SCHEMA, Path(__file__),
               *(ROOT / name for name in ('generate.py', 'scenario_validation.py', 'social_requirements.py',
                                          'result_adapter.py', 'experiments/run_comparison.py'))]
    demo_update = DEMO / 'updates' / 'h_replan.json'
    if replan_demo:
        sources.append(demo_update)
        write_json(output / 'replan_input.json', read_json(demo_update))
    write_json(output / 'manifest.json', {'algorithm_version': VERSION, 'revision': revision,
               'python': platform.python_version(), 'platform': platform.platform(), 'data_status': 'SYNTHETIC',
               'strategies': list(strategies), 'parameters': asdict(limits),
               'source_sha256': {p.relative_to(ROOT).as_posix(): sha256(p) for p in sources},
               'input_sha256': {p.name: sha256(p) for p in sorted(inputs.glob('*.json'))},
               'tariffs_sha256': sha256(output / 'tariffs.json') if tariffs is not None else None,
               'replanning_input_sha256': sha256(output / 'replan_input.json') if replan_demo else None})
    metrics_rows, comparisons = [], []
    for sid, raw in sorted(scenarios.items()):
        scenario_output = output / sid
        scenario_output.mkdir()
        results = {}
        for strategy in strategies:
            start = perf_counter()
            result = run_scenario(raw, strategy, limits=limits, tariffs=tariffs)
            duration = perf_counter() - start
            validator.validate(result)
            independent = audit_result(result)
            if independent['schedule_audit']['status'] != 'PASSED':
                raise ValueError(f'{sid}/{strategy} failed independent replay: {independent}')
            results[strategy] = result
            write_json(scenario_output / f'{strategy.lower()}.json', result)
            financial = result['financial_evaluation']
            metrics_rows.append({'scenario_id': sid, 'strategy': strategy, **result['metrics'],
                                 'requirements_status': result['requirements_status'],
                                 'feasibility_status': result['feasibility_status'],
                                 'physical_violations': 0, 'cost': financial['total_cost'] if financial else None,
                                 'currency': financial['currency'] if financial else None,
                                 'compute_seconds': round(duration, 6)})
        if REPOSITION in results:
            for baseline in (SOCIAL, RETURN_EMPTY):
                if baseline not in results:
                    continue
                a, b = results[baseline]['metrics'], results[REPOSITION]['metrics']
                keys = ('delivered_on_time_t', 'delivered_total_t', 'empty_km', 'social_deficit_t')
                comparisons.append({'scenario_id': sid, 'baseline': baseline, 'candidate': REPOSITION,
                                    **{f'delta_{k}': round(b[k] - a[k], 6) for k in keys},
                                    'selected_repositioning': results[REPOSITION]['reposition_comparison']['selected_repositioning']})
        if replan_demo and sid == 'h_replan':
            config = read_json(demo_update)
            revised = {**raw, 'edges': [dict(e, condition={**e['condition'], **config['edge_updates'].get(e['edge_id'], {})})
                                       for e in raw['edges']], 'cargo': [*raw['cargo'], *config['new_cargo']]}
            outcome = replan(results[REPOSITION], revised, config['as_of'], resource_updates=config['resource_updates'],
                             delivered_t=config['delivered_t'], remaining_t=config['remaining_t'], limits=limits)
            if outcome['status'] != 'REPLANNED':
                raise ValueError(f'Expected the demo to replan: {outcome["reason"]}')
            validator.validate(outcome['remaining_plan'])
            if audit_result(outcome['remaining_plan'])['schedule_audit']['status'] != 'PASSED':
                raise ValueError('Replanned suffix failed independent replay')
            write_json(scenario_output / 'replanned.json', outcome)
    write_csv(output / 'metrics.csv', metrics_rows)
    write_csv(output / 'comparison.csv', comparisons)
    summary = {'scenarios': len(scenarios), 'schedules': len(metrics_rows), 'physical_violations': 0,
               'replanning_demos': int(replan_demo), 'repositioning_selections': sum(
                   bool(row['repositioning_voyages']) for row in metrics_rows if row['strategy'] == REPOSITION)}
    write_json(output / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, action='append', help='Repeat for several inputs. Default: network demo suite')
    parser.add_argument('--strategy', choices=(*STRATEGIES, RETURN_EMPTY), action='append')
    parser.add_argument('--limits', type=Path)
    parser.add_argument('--tariffs', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--replan-demo', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.scenario or sorted(DEMO.glob('*.json')), args.output,
                 strategies=tuple(args.strategy or (*STRATEGIES, RETURN_EMPTY)),
                 limits=Limits(**read_json(args.limits)) if args.limits else None,
                 tariffs=read_json(args.tariffs) if args.tariffs else None,
                 revision=args.revision, replan_demo=args.replan_demo)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
