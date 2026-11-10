"""Deterministic scheduling on the archive's linear-route model."""
import argparse
import copy
import csv
import json
from datetime import timedelta
from pathlib import Path
from generate import dt, iso, load_scenario, trial_trip, best_load, write_csv, TRIP_FIELDS
from validate import audit

STRATEGIES = ('FCFS', 'EARLIEST_DUE', 'OPTIMIZED')


def load(folder, config):
    cargo, units, conditions = load_scenario(folder)
    return {'id': Path(folder).name, 'config': config, 'cargo': cargo,
            'units': units, 'conditions': conditions}


def _candidate(s, u, x, qty, start):
    c = s['config']
    if x['cargo_type'] not in u['cargo_type_allowed'] or x['origin'] != c['origin'] or x['destination'] != c['destination']:
        return None
    cm = s['_conditions']
    # End of model window; each unit's own availability is checked by trial_trip.
    close = dt(c['end']) + timedelta(days=1)
    return trial_trip(c, u, x, qty, start, cm, close)


def _best_load(s, u, x, qty, start):
    if x['cargo_type'] not in u['cargo_type_allowed'] or x['origin'] != s['config']['origin'] or x['destination'] != s['config']['destination']:
        return None
    return best_load(s['config'], u, x, qty, start, s['_conditions'], dt(s['config']['end']) + timedelta(days=1))


def _objective(trips, cargo):
    by_id = {x['cargo_id']: x for x in cargo}
    ontime = sum(float(t['volume_t']) for t in trips if dt(t['unloading_end']) <= dt(by_id[t['cargo_id']]['due_date']))
    total = sum(float(t['volume_t']) for t in trips)
    return (round(ontime, 3), round(total, 3), -len(trips))


def _audit(s, trips):
    return audit(s['config'], s['cargo'], s['units'], s['conditions'], trips)


def _improve(s, trips):
    """First-improvement neighborhood; every proposal is independently audited.

    Shift by one day, reassign to another unit, or exchange two start slots.
    Only strict improvement of (on-time tonnes, total tonnes, -trip count) is accepted.
    """
    units = {u['unit_id']: u for u in s['units']}
    cargo = {x['cargo_id']: x for x in s['cargo']}
    best = trips[:]
    for _ in range(2):
        changed = False
        for i, old in enumerate(best):
            x = cargo[old['cargo_id']]
            # Moving an already-on-time trip alone cannot improve this objective.
            if dt(old['unloading_end']) <= dt(x['due_date']):
                continue
            options = [(units[old['unit_id']], dt(old['loading_start']) + timedelta(days=d)) for d in (-1, 1)]
            options += [(u, dt(old['loading_start'])) for u in s['units'] if u['unit_id'] != old['unit_id']]
            for u, start in options:
                t = _candidate(s, u, x, float(old['volume_t']), start)
                if not t:
                    continue
                t['trip_id'] = old['trip_id']
                proposal = best[:]; proposal[i] = t
                try:
                    _audit(s, proposal)
                except AssertionError:
                    continue
                if _objective(proposal, s['cargo']) > _objective(best, s['cargo']):
                    best = proposal; changed = True; break
            if not changed:
                # Replace the cargo carried in this slot, retaining its amount.
                for other in s['cargo']:
                    used = sum(float(t['volume_t']) for t in best if t['cargo_id'] == other['cargo_id'])
                    if (other['cargo_id'] == old['cargo_id'] or float(other['volume_t']) - used + 1e-6 < float(old['volume_t'])
                            or dt(other['ready_date']) > dt(old['loading_start']) or dt(other['due_date']) < dt(old['loading_start'])):
                        continue
                    t = _candidate(s, units[old['unit_id']], other, float(old['volume_t']), dt(old['loading_start']))
                    if not t:
                        continue
                    t['trip_id'] = old['trip_id']
                    proposal = best[:]; proposal[i] = t
                    try:
                        _audit(s, proposal)
                    except AssertionError:
                        continue
                    if _objective(proposal, s['cargo']) > _objective(best, s['cargo']):
                        best = proposal; changed = True; break
            if changed:
                break
        if not changed:
            # Swap two start slots, rebuilding both trips and auditing the whole schedule.
            for i in range(len(best)):
                for j in range(i + 1, len(best)):
                    a, b = best[i], best[j]
                    if a['unit_id'] != b['unit_id']:
                        continue
                    if dt(a['unloading_end']) <= dt(cargo[a['cargo_id']]['due_date']) and dt(b['unloading_end']) <= dt(cargo[b['cargo_id']]['due_date']):
                        continue
                    ta = _candidate(s, units[a['unit_id']], cargo[a['cargo_id']], float(a['volume_t']), dt(b['loading_start']))
                    tb = _candidate(s, units[b['unit_id']], cargo[b['cargo_id']], float(b['volume_t']), dt(a['loading_start']))
                    if not ta or not tb:
                        continue
                    ta['trip_id'], tb['trip_id'] = a['trip_id'], b['trip_id']
                    proposal = best[:]; proposal[i], proposal[j] = ta, tb
                    try:
                        _audit(s, proposal)
                    except AssertionError:
                        continue
                    if _objective(proposal, s['cargo']) > _objective(best, s['cargo']):
                        best = proposal; changed = True; break
                if changed:
                    break
        if not changed:
            break
    return best


def simulate(scenario, strategy):
    if strategy not in STRATEGIES:
        raise ValueError(f'Unknown strategy: {strategy}')
    s = dict(scenario)
    s['_conditions'] = {(r['date'], r['segment_id']): r for r in s['conditions']}
    c, cargo, units = s['config'], s['cargo'], s['units']
    remain = {x['cargo_id']: float(x['volume_t']) for x in cargo}
    # Resources, not just unit identifiers, are exclusive even if units share a tug.
    available = {r: dt(c['start']) for u in units for r in (u['barge_id'], u['tug_id'])}
    trips = []
    day = dt(c['start'])
    while day <= dt(c['end']):
        while True:
            choices = []
            for u in units:
                start0 = max(day, dt(u['available_from']), available[u['barge_id']], available[u['tug_id']])
                if start0 >= day + timedelta(days=1):
                    continue
                eligible = [x for x in cargo if remain[x['cargo_id']] >= .001 and dt(x['ready_date']) < day + timedelta(days=1)
                            and x['cargo_type'] in u['cargo_type_allowed'] and x['origin'] == c['origin'] and x['destination'] == c['destination']]
                if strategy == 'FCFS':
                    eligible.sort(key=lambda x: (x['ready_date'], x['cargo_id']))
                else:
                    eligible.sort(key=lambda x: (x['due_date'], -int(x['priority']), x['ready_date'], x['cargo_id']))
                # OPTIMIZED optionally caps the ranked search per unit/day.
                if strategy == 'OPTIMIZED':
                    limit = s.get('candidate_limit')
                    if limit is not None:
                        if limit < 1: raise ValueError('candidate_limit must be positive or None')
                        eligible = eligible[:limit]
                for rank, x in enumerate(eligible):
                    start = max(start0, dt(x['ready_date']))
                    t = _best_load(s, u, x, min(remain[x['cargo_id']], u['capacity_t']), start)
                    if not t:
                        continue
                    ontime = dt(t['unloading_end']) <= dt(x['due_date'])
                    if strategy == 'FCFS':
                        key = (x['ready_date'], x['cargo_id'], t['loading_start'], u['unit_id'])
                    elif strategy == 'EARLIEST_DUE':
                        key = (x['due_date'], -int(x['priority']), x['ready_date'], x['cargo_id'], t['loading_start'], u['unit_id'])
                    else:
                        hours = (dt(t['released_at']) - dt(t['loading_start'])).total_seconds() / 3600
                        key = (-int(ontime), -(float(t['volume_t']) / hours if ontime else 0), -float(t['volume_t']), x['due_date'], t['loading_start'], u['unit_id'], x['cargo_id'])
                    choices.append((key, t))
                    if strategy != 'OPTIMIZED':
                        break
            if not choices:
                break
            _, t = min(choices, key=lambda z: z[0])
            t['trip_id'] = f'TR{len(trips)+1:05}'
            trips.append(t)
            remain[t['cargo_id']] = round(remain[t['cargo_id']] - t['volume_t'], 3)
            for r in (t['barge_id'], t['tug_id']):
                available[r] = dt(t['released_at'])
        day += timedelta(days=1)
    if strategy == 'OPTIMIZED':
        trips = _improve(s, trips)
    delivered, ontime, busy, weighted = _audit(s, trips)
    moved = {x['cargo_id']: 0.0 for x in cargo}
    by_type = {}
    ontime_by_type = {}
    occupancy = {}
    for t in trips:
        moved[t['cargo_id']] += float(t['volume_t'])
        typ = t['cargo_type']
        by_type[typ] = by_type.get(typ, 0.0) + float(t['volume_t'])
        if dt(t['unloading_end']) <= dt(next(x['due_date'] for x in cargo if x['cargo_id'] == t['cargo_id'])):
            ontime_by_type[typ] = ontime_by_type.get(typ, 0.0) + float(t['volume_t'])
        for r in (t['barge_id'], t['tug_id']):
            occupancy.setdefault(r, []).append({'trip_id': t['trip_id'], 'start': t['loading_start'], 'end': t['released_at']})
    # Fleet availability is per physical resource, with shared-resource windows unioned.
    windows = {}
    for u in units:
        for r in (u['barge_id'], u['tug_id']):
            windows.setdefault(r, []).append((max(dt(c['start']), dt(u['available_from'])), min(dt(c['end']) + timedelta(days=1), dt(u['available_to']))))
    hours = 0
    for rows in windows.values():
        end = None
        for a, b in sorted(rows):
            if b <= a: continue
            if end is None or a > end: hours += (b-a).total_seconds()/3600; end = b
            elif b > end: hours += (b-end).total_seconds()/3600; end = b
    total = sum(float(x['volume_t']) for x in cargo)
    metrics = {'scenario_id': s['id'], 'strategy': strategy, 'delivered_on_time_t': round(ontime, 3),
               'delivered_total_t': round(delivered, 3), 'undelivered_t': round(total-delivered, 3),
               'completed_trips': len(trips), 'vessel_idle_days': round((hours-2*busy)/24, 6),
               'fleet_utilization': round(2*busy/hours, 6) if hours else 0,
               'average_delivery_days': round(weighted/delivered, 6) if delivered else 0, 'violations': 0}
    return {'metrics': metrics, 'trips': trips, 'delivered_by_cargo_t': {k: round(v, 3) for k,v in moved.items()},
            'delivered_by_type_t': {k: round(v, 3) for k, v in by_type.items()},
            'on_time_by_type_t': {k: round(v, 3) for k, v in ontime_by_type.items()},
            'remaining_by_cargo_t': {x['cargo_id']: round(float(x['volume_t'])-moved[x['cargo_id']], 3) for x in cargo}, 'fleet_occupancy': occupancy}


def export(results, out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    for result in results:
        name = result['metrics']['strategy'].lower()
        (out / f'{name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        write_csv(out / f'{name}_trips.csv', result['trips'], TRIP_FIELDS)
    write_csv(out / 'comparison.csv', [r['metrics'] for r in results])
    (out / 'comparison.json').write_text(json.dumps([r['metrics'] for r in results], ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scenario', type=Path, default=Path(__file__).parent / 'data/scenarios/r1_baseline_central_central')
    p.add_argument('--config', type=Path, default=Path(__file__).parent / 'scenario_config.json')
    p.add_argument('--candidate-limit', type=int, default=0, help='Maximum OPTIMIZED candidates per unit/day; 0 means unlimited')
    p.add_argument('--out', type=Path, default=Path(__file__).parent / 'results')
    a = p.parse_args()
    s = load(a.scenario, json.loads(a.config.read_text(encoding='utf-8')))
    s['candidate_limit'] = None if a.candidate_limit == 0 else a.candidate_limit
    results = [simulate(s, strategy) for strategy in STRATEGIES]
    export(results, a.out)
    print(json.dumps([r['metrics'] for r in results], ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
