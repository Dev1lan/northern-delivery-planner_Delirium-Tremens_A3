"""Export physical schedule quantities for independently supplied tariffs."""

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path

from generate import dt
from validate import audit


def quantities(scenario, result):
    """Build financial inputs from a v2 result and its scenario snapshot."""
    from result_adapter import fingerprint

    if result['schema_version'] != '2.0.0' or result['scenario_id'] != scenario['id']:
        raise ValueError('Expected v2 result for the supplied scenario')
    if result['input_fingerprint'] != fingerprint({k: scenario[k] for k in ('config', 'cargo', 'units', 'conditions')}):
        raise ValueError('Scenario fingerprint differs from schedule input')
    distances = {s['segment_id']: s['distance_km'] for s in scenario['config']['segments']}
    voyages = []
    seconds_by_resource = defaultdict(int)
    audited = []
    for trip in result['trips']:
        audited.append({**trip, 'events_json': json.dumps(trip['events'])})
        km = defaultdict(float)
        for event in trip['events']:
            km[event['direction']] += distances[event['segment_id']]
        start, departure, arrival, unloaded, released = (dt(trip[key]) for key in
            ('loading_start', 'departure', 'arrival', 'unloading_end', 'released_at'))
        busy = int((released - start).total_seconds())
        seconds_by_resource['barge', trip['barge_id']] += busy
        seconds_by_resource['tug', trip['tug_id']] += busy
        voyages.append({**{k: trip[k] for k in ('trip_id', 'unit_id', 'barge_id', 'tug_id', 'cargo_type', 'volume_t',
                                                 'origin', 'destination', 'loading_start', 'released_at', 'on_time')},
                        'loaded_km': km['upstream'], 'empty_km': km['downstream'],
                        'loading_seconds': int((departure - start).total_seconds()),
                        'unloading_seconds': int((unloaded - arrival).total_seconds()), 'busy_seconds': busy})
    delivered, on_time, _, _ = audit(scenario['config'], scenario['cargo'], scenario['units'], scenario['conditions'], audited)
    demand = sum(float(c['volume_t']) for c in scenario['cargo'])
    metrics = result['metrics']
    if (round(delivered, 3), round(on_time, 3), round(demand - delivered, 3), len(voyages)) != (
            metrics['delivered_total_t'], metrics['delivered_on_time_t'], metrics['undelivered_t'], metrics['completed_trips']):
        raise ValueError('Financial export metrics do not match the audited trips')
    return {'schema_version': '1.0.0', 'data_status': 'SYNTHETIC', 'scenario_id': result['scenario_id'],
            'strategy': result['strategy'], 'input_fingerprint': result['input_fingerprint'],
            'demand_t': round(demand, 3), 'on_time_t': metrics['delivered_on_time_t'],
            'undelivered_t': metrics['undelivered_t'], 'cargo_delivered_t': metrics['delivered_by_type_t'],
            'cargo_on_time_t': metrics['on_time_by_type_t'], 'requirements_status': result['requirements_status'],
            'feasibility_status': result['feasibility_status'],
            'unmet_requirements': [r for r in result['requirements'] if not r['satisfied']],
            'voyages': voyages, 'resources': [{'kind': kind, 'resource_id': rid, 'busy_seconds': seconds}
                for (kind, rid), seconds in sorted(seconds_by_resource.items())]}


def evaluate_cost(report, tariffs):
    """Charge only explicitly provided rates; string amounts preserve currency precision."""
    if not isinstance(tariffs, dict) or not isinstance(tariffs.get('currency'), str) or not tariffs['currency'].strip():
        raise ValueError('Tariffs require a currency')
    rates = tariffs.get('rates')
    voyages = report['voyages']
    unit_totals = {
        'loaded_km': sum(Decimal(str(v['loaded_km'])) for v in voyages),
        'empty_km': sum(Decimal(str(v['empty_km'])) for v in voyages),
        'loading_hour': sum(v['loading_seconds'] for v in voyages) / Decimal(3600),
        'unloading_hour': sum(v['unloading_seconds'] for v in voyages) / Decimal(3600),
        'barge_hour': sum(r['busy_seconds'] for r in report['resources'] if r['kind'] == 'barge') / Decimal(3600),
        'tug_hour': sum(r['busy_seconds'] for r in report['resources'] if r['kind'] == 'tug') / Decimal(3600),
        'trip': len(voyages),
    }
    if not isinstance(rates, dict) or not rates or set(rates) - unit_totals.keys():
        raise ValueError(f'Use one or more supported tariff units: {", ".join(unit_totals)}')
    charges = {}
    for key, value in rates.items():
        try:
            rate = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f'Invalid tariff for {key}') from exc
        if not rate.is_finite() or rate < 0:
            raise ValueError(f'Tariff for {key} must be finite and nonnegative')
        charges[key] = (rate * unit_totals[key]).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'currency': tariffs['currency'], 'tariffs': tariffs, 'cost_by_unit': {k: str(v) for k, v in charges.items()},
            'total_cost': str(sum(charges.values(), Decimal('0.00')))}


def main():
    from experiments.run_comparison import read_json, write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Scenario snapshot from comparison inputs')
    parser.add_argument('--result', type=Path, required=True, help='Comparison scenario result bundle')
    parser.add_argument('--strategy', required=True)
    parser.add_argument('--tariffs', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = quantities(read_json(args.input)['scenario'], read_json(args.result)['results'][args.strategy])
    if args.tariffs:
        report['economics'] = evaluate_cost(report, read_json(args.tariffs))
    write_json(args.output, report)


if __name__ == '__main__':
    main()
