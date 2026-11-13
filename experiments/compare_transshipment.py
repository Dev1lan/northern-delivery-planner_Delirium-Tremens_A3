"""Compare transfer search on published E0/E1/E2 with an explicit terminal overlay."""

import argparse
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
from time import perf_counter

from network.adapter import audit_result, run_scenario
from result_adapter import fingerprint


def summarize(raw, result, elapsed):
    sea = {edge['edge_id'] for edge in raw['edges'] if edge.get('water_type') == 'SEA'}
    batches = {v['batch_id'] for v in result['voyages'] if 'batch_id' in v
               and any(leg['edge_id'] in sea for leg in v['loaded'])}
    direct = [v for v in result['voyages'] if 'stage' not in v]
    expiry = {cargo['cargo_id']: cargo.get('expires_at') for cargo in raw['cargo']}

    def usable(voyage):
        deadline = expiry[voyage['cargo_id']]
        return deadline is None or voyage['unloading_end'] <= deadline

    delivered_sea = sum(v['volume_t'] for v in direct
                        if usable(v) and any(leg['edge_id'] in sea for leg in v['loaded']))
    delivered_sea += sum(v['volume_t'] for v in result['voyages'] if v.get('stage') == 2
                         and v['batch_id'] in batches and usable(v))
    finance = result['financial_evaluation']
    return {'seconds': round(elapsed, 3), 'input_fingerprint': result['input_fingerprint'],
            'termination_reason': result['termination_reason'], 'metrics': result['metrics'],
            'sea_delivered_t': round(delivered_sea, 3), 'direct_voyages': len(direct),
            'transfer_batches': sum(v.get('stage') == 2 for v in result['voyages']),
            'fleet': sorted({v[key] for v in result['voyages'] for key in ('tug_id', 'barge_id')}),
            'planned_cost': finance['total_cost'],
            'operating_cost': str(Decimal(finance['total_cost']) - Decimal(finance.get('transshipment_cost', '0'))),
            'transshipment_cost': finance.get('transshipment_cost'),
            'estimated_undelivered_cost': finance['estimated_undelivered_cost'],
            'cargo_status': result['cargo_status'], 'audit': audit_result(result)['schedule_audit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference_script', type=Path)
    parser.add_argument('--terminal-overlay', type=Path)
    parser.add_argument('--case', choices=('E0', 'E1', 'E2'), action='append')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=45)
    args = parser.parse_args()
    path = args.reference_script.resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('reference_sea_experiments', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'budget_seconds': args.seconds, 'tariff_fingerprint': fingerprint(module.TARIFFS), 'cases': {}}
    overlay = json.loads(args.terminal_overlay.read_text(encoding='utf-8-sig')) if args.terminal_overlay else {}
    for name in args.case or ('E0', 'E1', 'E2'):
        raw = module.variant(name)
        for field in ('port_operations', 'transshipment_ports'):
            if field in overlay:
                raw[field] = {**raw.get(field, {}), **deepcopy(overlay[field])}
        base_policy = module.policy(args.seconds)
        policy = replace(base_policy, disable_transshipment=True)
        rows = {}
        for label, options in [('direct', policy), ('transfer', base_policy)]:
            started = perf_counter()
            plan = run_scenario(raw, module.STRATEGY, limits=module.LIMITS,
                                tariffs=module.TARIFFS, search_policy=options)
            row = summarize(raw, plan, perf_counter() - started)
            if row['audit']['status'] != 'PASSED':
                raise ValueError(f'{name}/{label}: {row["audit"]}')
            rows[label] = row
            (args.output / f'{name}_{label}.json').write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
            print(name, label, row['metrics']['delivered_on_time_t'], row['sea_delivered_t'],
                  row['transfer_batches'], row['seconds'], flush=True)
        if rows['direct']['input_fingerprint'] != rows['transfer']['input_fingerprint']:
            raise ValueError('Inputs differ')
        report['cases'][name] = rows
    (args.output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
