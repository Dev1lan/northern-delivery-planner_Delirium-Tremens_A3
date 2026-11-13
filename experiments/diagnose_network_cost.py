"""Compare audited network plans on identical scenarios and tariffs."""

import argparse
from collections import Counter, defaultdict, deque
from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter

from network.adapter import audit_result, run_scenario
from network.models import Limits
from network.scheduler import COST, REPOSITION
from network.search_policy import SearchPolicy


def normalized(result):
    cargo = {c['cargo_id']: c for c in result['scenario']['cargo']}
    return [dict(v, origin_port=cargo[v['cargo_id']]['origin_port'],
                 destination_port=cargo[v['cargo_id']]['destination_port']) for v in result['voyages']]


def money(result):
    finance = result['financial_evaluation']
    rates = finance.get('tariff_parameters')
    if not rates:
        raise ValueError('Expected tariff version 2')
    by_item = finance['cost_by_unit']
    total = Decimal(finance['total_cost'])
    residual = Decimal(finance.get('undelivered_penalty', '0'))
    estimate = finance.get('estimated_undelivered_cost', str(residual))
    return {'currency': finance['currency'], 'price_status': finance['price_status'],
            'unpriced_undelivered_t': finance['unpriced_undelivered_t'],
            'operating_cost': str(total - residual), 'alternative_delivery': estimate,
            'loaded_fuel': by_item.get('loaded_fuel', '0.00'),
            'empty_transition': finance['empty_transition_cost'],
            'port_handling_and_calls': str(sum(Decimal(by_item.get(key, '0'))
                                           for key in ('loading', 'unloading', 'port_call'))),
            'fixed_fleet_cost': None, 'additional_fleet_cost': None,
            'total_cost': str(total - residual), 'by_item': by_item}


def compare(left, right):
    if left['input_fingerprint'] != right['input_fingerprint']:
        raise ValueError('Scenarios differ')
    if left['financial_evaluation']['tariff_parameters'] != right['financial_evaluation']['tariff_parameters']:
        raise ValueError('Tariffs differ')
    for result in (left, right):
        if audit_result(result)['schedule_audit']['status'] != 'PASSED':
            raise ValueError('Schedule audit failed')
    a, b = [Counter(json.dumps(v, sort_keys=True) for v in normalized(result)) for result in (left, right)]
    removed, added = [[json.loads(item) for item in items.elements()] for items in (a - b, b - a)]
    groups = defaultdict(deque)
    for move in added:
        groups[move['cargo_id'], move['volume_t']].append(move)
    changes, unmatched_removed = [], []
    for old in removed:
        options = groups[old['cargo_id'], old['volume_t']]
        if not options:
            unmatched_removed.append(old)
            continue
        new = options.popleft()
        fields = set(old) | set(new)
        changes.append({'before': old, 'after': new,
                        'changed_fields': sorted(key for key in fields if old.get(key) != new.get(key))})
    fields = ('strategy', 'metrics', 'requirements_status', 'requirements', 'termination_reason',
              'final_resources', 'parameters', 'search_diagnostics', 'cost_service_floor')
    return {'input_fingerprint': left['input_fingerprint'], 'audit': 'PASSED',
            'identical_voyages': a == b, 'identical_voyage_count': sum((a & b).values()),
            'removed_voyage_count': len(removed), 'added_voyage_count': len(added),
            'matched_changes': changes, 'unmatched_removed': unmatched_removed,
            'unmatched_added': [move for options in groups.values() for move in options],
            'cost_delta': str(Decimal(money(right)['total_cost']) - Decimal(money(left)['total_cost'])),
            'before': {**{key: left.get(key) for key in fields}, 'cost': money(left)},
            'after': {**{key: right.get(key) for key in fields}, 'cost': money(right)}}


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, payload):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budgets', type=float, nargs='+', default=[15, 45, 120])
    parser.add_argument('--reposition-budget', type=float, default=45)
    parser.add_argument('--recover-undelivered', action='store_true')
    parser.add_argument('--sea-fleet-allocation', action='store_true')
    args = parser.parse_args()
    initial = read(args.reference)
    raw = initial['scenario']
    tariffs = initial['financial_evaluation']['tariff_parameters']
    limits = Limits(**initial['parameters'])
    args.output.mkdir(parents=True, exist_ok=False)

    def run(label, strategy, seconds, seed=None):
        start = perf_counter()
        policy = SearchPolicy(time_seconds=seconds, recover_undelivered=args.recover_undelivered,
                              sea_fleet_allocation=args.sea_fleet_allocation)
        options = {'initial_plan': seed} if seed is not None else {}
        result = run_scenario(raw, strategy, limits=limits, tariffs=tariffs, search_policy=policy, **options)
        elapsed = perf_counter() - start
        write(args.output / f'{label}.json', result)
        print(label, result['metrics']['delivered_on_time_t'], result['financial_evaluation']['total_cost'],
              result['termination_reason'], round(elapsed, 3), flush=True)
        return result

    reference = run('reposition', REPOSITION, args.reposition_budget)
    for seconds in args.budgets:
        label = f'cost_{seconds:g}'
        result = run(label, COST, seconds)
        write(args.output / f'compare_{label}.json', compare(reference, result))
    seeded = run('cost_seeded', COST, args.reposition_budget, seed=reference)
    write(args.output / 'compare_cost_seeded.json', compare(reference, seeded))


if __name__ == '__main__':
    main()
