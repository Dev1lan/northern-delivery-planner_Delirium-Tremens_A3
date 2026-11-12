"""Versioned interface for dashboards and ML clients; does not modify the solver."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from generate import dt
from optimizer import ALL_STRATEGIES, SOCIAL_PRIORITY, STRATEGIES, load, simulate
from social_requirements import parse_requirements

CONTRACT_VERSION = '1.0.0'
EXTENDED_CONTRACT_VERSION = '2.0.0'
ALGORITHM_VERSION = '2.2.0'
DEFAULT_CONFIG = Path(__file__).resolve().parent / 'scenario_config.json'
METRIC_FIELDS = ('delivered_on_time_t', 'delivered_total_t', 'undelivered_t',
                 'completed_trips', 'violations', 'vessel_idle_days',
                 'fleet_utilization', 'average_delivery_days')
TRIP_FIELDS = ('trip_id', 'unit_id', 'barge_id', 'tug_id', 'cargo_id', 'cargo_type',
               'origin', 'destination', 'volume_t', 'loading_start', 'departure',
               'arrival', 'unloading_end', 'released_at', 'loaded_draft_m',
               'data_status', 'source_url')


def run_scenario(scenario_path, *, config_path=DEFAULT_CONFIG, strategy='OPTIMIZED',
                 scenario_id=None, candidate_limit=None, contract_version=CONTRACT_VERSION,
                 requirements=None):
    """Run the audited simulator and return a JSON-serializable contract result.

    `scenario_path` contains scenario CSV files and declared demand totals.
    `candidate_limit=None` considers all eligible OPTIMIZED candidates.
    Invalid input or an audit failure raises an exception rather than a result.
    """
    if contract_version not in (CONTRACT_VERSION, EXTENDED_CONTRACT_VERSION):
        raise ValueError(f'Unknown contract version {contract_version!r}')
    extended = contract_version == EXTENDED_CONTRACT_VERSION
    supported = ALL_STRATEGIES if extended else STRATEGIES
    if strategy not in supported:
        raise ValueError(f'Unknown strategy {strategy!r}; choose from {supported}')
    if not extended and requirements is not None:
        raise ValueError('Social requirements need contract version 2.0.0')
    if candidate_limit is not None and (isinstance(candidate_limit, bool) or
                                        not isinstance(candidate_limit, int) or candidate_limit < 1):
        raise ValueError('candidate_limit must be a positive integer or None')
    if scenario_id is not None and (not isinstance(scenario_id, str) or not scenario_id.strip()):
        raise ValueError('scenario_id must be a non-empty string')
    config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    scenario = load(Path(scenario_path), config)
    if scenario_id is not None:
        scenario['id'] = scenario_id
    scenario['candidate_limit'] = candidate_limit
    if extended:
        requirements = [] if requirements is None else requirements
        internal = simulate(scenario, strategy, requirements=requirements)
        return format_result_v2(scenario, internal, requirements)
    return _format_result(scenario, simulate(scenario, strategy), strategy)


def _format_result(scenario, internal, strategy):
    by_cargo = {x['cargo_id']: x for x in scenario['cargo']}
    cargo_types = sorted({x['cargo_type'] for x in scenario['cargo']})
    metrics = {key: internal['metrics'][key] for key in METRIC_FIELDS}
    metrics['delivered_by_type_t'] = {key: internal['delivered_by_type_t'].get(key, 0.0) for key in cargo_types}
    metrics['on_time_by_type_t'] = {key: internal['on_time_by_type_t'].get(key, 0.0) for key in cargo_types}
    trips = []
    for row in internal['trips']:
        trip = {key: row[key] for key in TRIP_FIELDS}
        trip['due_date'] = by_cargo[row['cargo_id']]['due_date']
        trip['on_time'] = dt(row['unloading_end']) <= dt(trip['due_date'])
        trip['events'] = json.loads(row['events_json'])
        trips.append(trip)
    return {'schema_version': CONTRACT_VERSION, 'scenario_id': scenario['id'],
            'strategy': strategy, 'metrics': metrics, 'trips': trips}


def fingerprint(value):
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def format_result_v2(scenario, audited_result, requirements):
    """Serialize an audited optimizer result for the v2 interface."""
    strategy = audited_result['metrics']['strategy']
    result = _format_result(scenario, audited_result, strategy)
    minima = parse_requirements(requirements, scenario)
    result.update(
        schema_version=EXTENDED_CONTRACT_VERSION,
        algorithm_version=ALGORITHM_VERSION,
        input_fingerprint=fingerprint({key: scenario[key] for key in ('config', 'cargo', 'units', 'conditions')}),
        requirements_fingerprint=fingerprint([r.as_dict() for r in minima]),
        data_status='SYNTHETIC',
        schedule_audit={'status': 'PASSED', 'violations': audited_result['metrics']['violations']},
        requirements_status=audited_result['requirements_status'],
        feasibility_status=audited_result['feasibility_status'],
        requirements=audited_result['requirements'],
        termination_reason='HEURISTIC_COMPLETED' if strategy in ('OPTIMIZED', SOCIAL_PRIORITY) else 'DISPATCH_COMPLETED',
        selected_plan_strategy=audited_result.get('selected_plan_strategy', strategy),
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a scenario and emit the versioned result JSON.')
    parser.add_argument('--scenario', required=True, type=Path, help='Folder containing scenario CSV files')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--strategy', choices=ALL_STRATEGIES, default='OPTIMIZED')
    parser.add_argument('--contract-version', choices=(CONTRACT_VERSION, EXTENDED_CONTRACT_VERSION), default=CONTRACT_VERSION)
    parser.add_argument('--requirements', type=Path, help='JSON list of social minimums (v2 only)')
    parser.add_argument('--scenario-id', help='Optional external scenario identifier')
    parser.add_argument('--candidate-limit', type=int, help='Positive limit; omit for unlimited')
    parser.add_argument('--output', type=Path, help='Optional output file; otherwise write JSON to stdout')
    args = parser.parse_args(argv)
    requirements = json.loads(args.requirements.read_text(encoding='utf-8-sig')) if args.requirements else None
    result = run_scenario(args.scenario, config_path=args.config, strategy=args.strategy,
                          scenario_id=args.scenario_id, candidate_limit=args.candidate_limit,
                          contract_version=args.contract_version, requirements=requirements)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(payload, encoding='utf-8')
    else:
        sys.stdout.buffer.write(payload.encode('utf-8'))


if __name__ == '__main__':
    main()
