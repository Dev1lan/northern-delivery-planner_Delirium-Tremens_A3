"""Measure repeatable synthetic stress changes and one-plan calculation times."""

import argparse
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from experiments.run_yakutia import ROOT, calculate, read, write
from network.adapter import run_scenario
from network.analysis import change_scenario, checked
from network.replanning import replan


def resilience(bundle):
    base = bundle['plans']['D']
    raw = base['scenario']
    day = raw['start'][:10]
    clock = lambda hour: day + 'T' + hour + ':00'
    shallow = deepcopy(raw)
    edge = next(e for e in shallow['edges'] if e['edge_id'] == 'BD')
    edge['condition'].update(usable_depth_m=0.7, max_allowed_draft_m=0.55)

    slow = change_scenario(raw, {'kind': 'handling_tph', 'port': 'Нижнеянск',
                                 'berth_id': 'NY-2', 'value': {'food': 15, 'coal': 20}})
    closure = deepcopy(raw)
    edge = next(e for e in closure['edges'] if e['edge_id'] == 'CB')
    edge['navigation_windows'] = [{'start': clock('05:00'), 'end': clock('12:00')}]

    urgent = deepcopy(raw)
    request = next(c for c in raw['cargo'] if c['cargo_type'] == 'food')
    urgent['cargo'].append(dict(request, cargo_id='urgent_synthetic', volume_t=40,
                                ready_at=clock('01:00'), due_at=clock('05:30')))
    cases = {
        'lower_depth': (shallow, clock('01:30'), {}),
        'delayed_fleet': (raw, clock('00:10'), {key: {'available_at': clock('04:00')}
                                              for key in ('T_LARGE', 'B_LARGE')}),
        'slower_port': (slow, clock('00:45'), {}),
        'marine_closure': (closure, clock('00:45'), {}),
        'urgent_cargo': (urgent, clock('01:30'), {}),
    }
    records = {}
    for key, (scenario, moment, updates) in cases.items():
        outcome = replan(base, scenario, moment, resource_updates=updates)
        record = {'as_of': moment, 'resource_updates': updates, 'input': scenario, 'outcome': outcome}
        if outcome['remaining_plan'] is not None:
            suffix = outcome['remaining_plan']
            checked(suffix)
            cargo = {c['cargo_id']: c for c in scenario['cargo']}
            completed = [e for e in outcome['locked_events'] if e['state'] == 'UNLOADING']
            timely = sum(e['volume_t'] for e in completed if e['end'] <= cargo[e['cargo_id']]['due_at'])
            locked_t = sum(e['volume_t'] for e in completed)
            total = locked_t + suffix['metrics']['delivered_total_t']
            timely += suffix['metrics']['delivered_on_time_t']
            record['projected'] = {'delivered_total_t': round(total, 3), 'on_time_t': round(timely, 3),
                                   'delta_on_time_t': round(timely - base['metrics']['delivered_on_time_t'], 3),
                                   'undelivered_t': round(sum(c['volume_t'] for c in cargo.values()) - total, 3)}
        records[key] = record
    return records


def benchmark(raw):
    scenarios = {'small_network': read(ROOT / 'data' / 'network_demo' / 'a_chain.json')}
    for multiplier in (1, 2, 3):
        scenario = deepcopy(raw)
        for copy in range(1, multiplier):
            scenario['cargo'].extend(dict(c, cargo_id=f"{c['cargo_id']}_copy_{copy}") for c in raw['cargo'])
        scenarios[f'synthetic_yakutia_x{multiplier}'] = scenario
    rows = []
    for label, scenario in scenarios.items():
        start = perf_counter()
        result = run_scenario(scenario)
        elapsed = perf_counter() - start
        checked(result)
        rows.append({'variant': label, 'jobs': len(scenario['cargo']),
                     'seconds': round(elapsed, 3), 'on_time_t': result['metrics']['delivered_on_time_t'],
                     'expanded_states': result['expanded_states'], 'termination_reason': result['termination_reason'],
                     'input_fingerprint': result['input_fingerprint']})
    return rows


def main(argv=None):
    from jsonschema import Draft202012Validator

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    bundle = calculate()
    records = resilience(bundle)
    schema = Draft202012Validator(read(ROOT / 'result.v3.schema.json'))
    for record in records.values():
        suffix = record['outcome']['remaining_plan']
        if suffix is not None:
            schema.validate(suffix)
    elapsed = benchmark(bundle['source_inputs']['scenario'])
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output / 'resilience.json', records)
    write(args.output / 'benchmark.json', elapsed)
    for key, record in records.items():
        outcome = record['outcome']
        print(key, outcome['status'], outcome.get('reason', ''), record.get('projected', ''))
    for row in elapsed:
        print(row['variant'], row['jobs'], row['seconds'], row['termination_reason'])


if __name__ == '__main__':
    main()
