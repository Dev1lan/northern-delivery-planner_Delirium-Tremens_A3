"""Berth calendars and deterministic cargo operation reservations."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil

from network.access import provenance
from scenario_validation import ident, number


@dataclass(frozen=True)
class Berth:
    id: str
    operations: frozenset
    rates: dict
    fixed_seconds: dict
    windows: tuple
    occupied: tuple
    max_draft: float | None
    usable_depth: float | None
    barge_classes: frozenset


def parse_ports(raw, ports, timestamp, start, end, status):
    result = {}
    seen = set()

    def intervals(rows):
        items = tuple(sorted((timestamp(r['start'], 'berth start'), timestamp(r['end'], 'berth end'))
                             for r in rows))
        if any(a >= b for a, b in items) or any(b > c for (_, b), (c, _) in zip(items, items[1:])):
            raise ValueError('Berth intervals must be positive and non-overlapping')
        return items

    for port, profile in raw.items():
        if port not in ports:
            raise ValueError(f'Unknown port in port_operations: {port}')
        provenance(profile, status)
        berths = []
        for row in profile['berths']:
            provenance(row, status)
            key = ident(row['berth_id'], 'berth_id')
            if key in seen:
                raise ValueError(f'Duplicate berth {key}')
            seen.add(key)
            operations = frozenset(row['operations'])
            if not operations or operations - {'LOADING', 'UNLOADING'}:
                raise ValueError(f'Invalid berth operations: {key}')
            raw_rates = row['handling_tph']
            raw_rates = raw_rates if isinstance(raw_rates, dict) else {'*': raw_rates}
            rates = {ident(k, 'cargo type'): number(v, 'handling_tph', positive=True) for k, v in raw_rates.items()}
            fixed = {op: ceil(number(row.get(field, 0), field, nonnegative=True) * 3600)
                     for op, field in [('LOADING', 'loading_fixed_hours'), ('UNLOADING', 'unloading_fixed_hours')]}
            windows = intervals(row['working_windows']) if 'working_windows' in row else ((start, end),)
            depth = row.get('max_allowed_draft_m')
            usable = row.get('usable_depth_m')
            berths.append(Berth(key, operations, rates, fixed, windows,
                                intervals(row.get('initial_occupancy', [])),
                                number(depth, 'berth draft', nonnegative=True) if depth is not None else None,
                                number(usable, 'usable_depth_m', nonnegative=True) if usable is not None else None,
                                frozenset(row.get('allowed_barge_classes', []))))
        result[port] = tuple(sorted(berths, key=lambda b: b.id))
    return result


def service_duration(scenario, berth, operation, barge, cargo_type, kg, draft):
    if berth is None:
        return timedelta(seconds=scenario.fixed_handling_seconds + ceil(3.6 * kg / barge.handling_tph))
    rate = berth.rates.get(cargo_type, berth.rates.get('*'))
    if (operation not in berth.operations or rate is None
            or berth.barge_classes and barge.access.vessel_class not in berth.barge_classes
            or berth.usable_depth is not None and draft + scenario.underkeel > berth.usable_depth + 1e-6
            or berth.max_draft is not None and draft > berth.max_draft + 1e-6):
        return None
    return timedelta(seconds=berth.fixed_seconds[operation] + ceil(3.6 * kg / min(rate, barge.handling_tph)))


def slots(scenario, port, operation, barge, cargo_type, kg, draft, ready, cutoff, bookings):
    if port not in scenario.berths:
        end = ready + service_duration(scenario, None, operation, barge, cargo_type, kg, draft)
        return [(None, ready, end)] if end <= cutoff else []
    choices = []
    for berth in scenario.berths[port]:
        duration = service_duration(scenario, berth, operation, barge, cargo_type, kg, draft)
        if duration is None:
            continue
        occupied = sorted((*berth.occupied, *bookings.get(berth.id, ())))
        for opens, closes in berth.windows:
            start = max(ready, opens)
            for a, b in occupied:
                if a >= start + duration:
                    break
                if b > start:
                    start = b
            end = start + duration
            if end <= min(closes, cutoff):
                choices.append((berth.id, start, end))
                break
    return sorted(choices, key=lambda slot: (slot[2], slot[1], slot[0]))


def reservations(move):
    if move.loading_berth is not None:
        yield move.loading_berth, (move.load_start, move.departure)
    if move.unloading_berth is not None:
        yield move.unloading_berth, (move.unload_start, move.unloading_end)


def port_report(scenario, events, start=None, end=None):
    start = scenario.start if start is None else max(start, scenario.start)
    end = scenario.end if end is None else min(end, scenario.end)
    if start >= end:
        raise ValueError('Report period must overlap the scenario')
    result = {}
    for port, berths in scenario.berths.items():
        local = [event for event in events if event['origin_port'] == port]
        operations = [event for event in local if 'berth_id' in event]
        report = []
        waits = 0
        for berth in berths:
            planned = [e for e in operations if e['berth_id'] == berth.id]
            reserved = [*berth.occupied, *((datetime.fromisoformat(e['start']), datetime.fromisoformat(e['end']))
                                          for e in planned)]
            capacity_seconds = sum(max(0, (min(b, end) - max(a, start)).total_seconds()) for a, b in berth.windows)
            busy_seconds = sum(max(0, (min(b, y, end) - max(a, x, start)).total_seconds())
                               for a, b in berth.windows for x, y in reserved)
            report.append({'berth_id': berth.id, 'open_seconds': int(capacity_seconds), 'busy_seconds': int(busy_seconds),
                           'utilization': busy_seconds / capacity_seconds if capacity_seconds else None,
                           'reservations': [{'start': a.isoformat(), 'end': b.isoformat()} for a, b in sorted(reserved)]})
        handled = [e for e in operations if start < datetime.fromisoformat(e['end']) <= end]
        queue_changes = []
        for event in local:
            if event['state'] == 'PORT_QUEUE':
                a = max(start, datetime.fromisoformat(event['start']))
                b = min(end, datetime.fromisoformat(event['end']))
                if a < b:
                    waits += (b - a).total_seconds()
                    queue_changes.extend(((a, 1), (b, -1)))
        queue = peak = 0
        for _, change in sorted(queue_changes):
            queue += change
            peak = max(peak, queue)
        result[port] = {'waiting_seconds': int(waits), 'peak_queue': peak,
                        'mean_wait_seconds': waits / (len(queue_changes) / 2) if queue_changes else None,
                        'handled_voyages': len({e['voyage_id'] for e in handled}),
                        'loaded_t': round(sum(e['volume_t'] for e in handled if e['state'] == 'LOADING'), 3),
                        'unloaded_t': round(sum(e['volume_t'] for e in handled if e['state'] == 'UNLOADING'), 3),
                        'mean_operation_seconds': (sum((datetime.fromisoformat(e['end']) - datetime.fromisoformat(e['start']))
                                                       .total_seconds() for e in handled) / len(handled)) if handled else None,
                        'berths': report}
    return result
