"""Replan the unstarted work while retaining the executed and committed itinerary."""

from collections import defaultdict
from network.access import admission_error
from copy import deepcopy

from network.adapter import audit_result, decode_voyages, run_scenario
from network.metrics import event_rows
from network.models import handling_time, loaded_draft, parse_scenario, sailing_time, timestamp
from network.routing import passage_open
from network.scheduler import REPOSITION, RETURN_EMPTY
from network.validator import replay, requirement_report
from result_adapter import fingerprint
from social_requirements import delivered_kg, kilograms


def replan(previous, revised_raw, as_of, *, resource_updates=None, delivered_t=None,
           remaining_t=None, strategy=REPOSITION, limits=None):
    """Freeze started voyages. Report unsafe in-flight changes for human resolution.

    Optional actual balances and moored positions are reconciled with executed
    voyages. A change of physical port requires evidence of a completed movement.
    """
    if previous['strategy'] == RETURN_EMPTY or strategy == RETURN_EMPTY:
        raise ValueError('Replan an operational network schedule, without the diagnostic return policy')
    if audit_result(previous)['schedule_audit']['status'] != 'PASSED':
        raise ValueError('The previous plan failed independent audit')
    original = parse_scenario(previous['scenario'])
    revised = parse_scenario(revised_raw)
    instant = timestamp(as_of, 'as_of')
    if not original.start <= instant < original.end or not revised.start <= instant < revised.end:
        raise ValueError('Replanning time must be within both scenarios')
    locked = tuple(v for v in decode_voyages(previous['voyages']) if v.start < instant)
    replayed = replay(original, locked)
    lock_events = sorted((row for i, move in enumerate(locked, 1) for row in event_rows(original, move, i)),
                         key=lambda row: (row['start'], row['voyage_id']))
    active = {row[resource]: row for row in lock_events if timestamp(row['start'], 'start') <= instant < timestamp(row['end'], 'end')
              for resource in ('tug_id', 'barge_id')}
    done = defaultdict(int)
    reserved = defaultdict(int)
    for move in locked:
        old, new = original.cargo[move.cargo_id], revised.cargo.get(move.cargo_id)
        if new is None or (old.type, old.origin, old.destination, old.ready, old.due) != (
                new.type, new.origin, new.destination, new.ready, new.due):
            raise ValueError(f'Locked cargo identity or dates changed: {move.cargo_id}')
        reserved[move.cargo_id] += move.kg
        if move.unloading_end <= instant:
            done[move.cargo_id] += move.kg
    if any(revised.cargo[key].kg < amount for key, amount in reserved.items()):
        raise ValueError('Revised demand is less than completed and committed cargo')
    if delivered_t is not None:
        reported = {key: kilograms(value) for key, value in delivered_t.items()}
        if {k: v for k, v in reported.items() if v} != dict(done):
            raise ValueError('Reported deliveries differ from completed unloadings; reconcile the execution log')
    residual = {c.id: c.kg - done.get(c.id, 0) for c in revised.cargo.values()}
    if remaining_t is not None and {k: kilograms(v) for k, v in remaining_t.items()} != residual:
        raise ValueError('Reported remaining demand does not balance against completed unloadings')

    revised_fleet = {**revised.tugs, **revised.barges}
    old_fleet = {**original.tugs, **original.barges}
    if original.tugs.keys() & revised.barges.keys() or original.barges.keys() & revised.tugs.keys():
        raise ValueError('A physical resource cannot change its type during replanning')
    observed = {}
    for key, vessel in {**old_fleet, **revised_fleet}.items():
        phase = active.get(key)
        location = replayed.locations.get(key, vessel.initial)
        if phase:
            observed[key] = {'state': phase['state'], 'edge_id': phase['edge_id'],
                             'current_port': None if phase['edge_id'] else phase['origin_port']}
        else:
            available_to = min(revised.end, vessel.available_to)
            state = 'IDLE' if key in revised_fleet and available_to > instant >= location.available_at else 'UNAVAILABLE'
            observed[key] = {'state': state, 'current_port': location.port, 'edge_id': None}

    output = {'schema_version': '3.0.0', 'as_of': instant.isoformat(), 'previous_input_fingerprint': previous['input_fingerprint'],
              'revised_input_fingerprint': fingerprint(revised_raw), 'locked_voyages': len(locked),
              'locked_events': lock_events, 'observed_resources': observed,
              'delivered_t': {k: v / 1000 for k, v in done.items()},
              'remaining_t': {k: v / 1000 for k, v in residual.items()}, 'remaining_plan': None}

    def intervention(code, detail):
        return {**output, 'status': 'REQUIRES_INTERVENTION', 'reason': code, 'detail': detail}

    updates = resource_updates or {}
    for key, update in updates.items():
        if key not in revised_fleet:
            raise ValueError(f'Unknown revised resource {key}')
        if 'current_port' in update and update['current_port'] != observed[key]['current_port']:
            return intervention('POSITION_MISMATCH', key)
        if 'edge_id' in update and update['edge_id'] != observed[key]['edge_id']:
            return intervention('POSITION_MISMATCH', key)
        if 'available_at' in update:
            ready = timestamp(update['available_at'], 'available_at')
            if key not in active and ready > instant:
                observed[key].update(state='UNAVAILABLE', available_at=ready.isoformat())

    for move in locked:
        if move.end <= instant:
            continue
        tug, barge = revised.tugs.get(move.tug_id), revised.barges.get(move.barge_id)
        if (tug is None or barge is None or (move.tug_id, move.barge_id) not in revised.pairs
                or move.end > min(tug.available_to, barge.available_to, revised.end)):
            return intervention('ONGOING_RESOURCE_UNAVAILABLE', move.cargo_id)
        if move.kg > barge.capacity_kg or revised.cargo[move.cargo_id].type not in barge.cargo_types:
            return intervention('ONGOING_LOAD_INCOMPATIBLE', move.cargo_id)
        handling = handling_time(revised, barge, move.kg)
        if ((move.departure > instant and move.departure - move.load_start != handling)
                or move.unloading_end - move.loaded[-1].arrive != handling):
            return intervention('ONGOING_HANDLING_CHANGED', move.cargo_id)
        for legs, kg in ((move.empty, 0), (move.loaded, move.kg)):
            draft = loaded_draft(tug, barge, kg)
            for leg in legs:
                if leg.arrive <= instant:
                    continue
                old_edge = original.edges[leg.edge_id]
                new_edge = revised.edges.get(leg.edge_id)
                if (new_edge is None or (old_edge.origin, old_edge.destination) != (new_edge.origin, new_edge.destination)
                        or sailing_time(new_edge, tug) != leg.arrive - leg.depart
                        or admission_error(revised, new_edge, tug, barge)
                        or not passage_open(revised, new_edge, max(instant, leg.depart), leg.arrive, draft)):
                    return intervention('ONGOING_PASSAGE_UNSAFE', leg.edge_id)

    suffix = deepcopy(revised_raw)
    suffix['start'] = instant.isoformat()
    # Existing resources follow the execution log; revised availability can delay them.
    available_ids = set()
    for key in ('tugs', 'barges'):
        resources = []
        for row in suffix[key]:
            rid = row['vessel_id']
            vessel = revised_fleet[rid]
            location = replayed.locations.get(rid, vessel.initial)
            delay = timestamp(updates.get(rid, {}).get('available_at', instant.isoformat()), 'available_at')
            release = max(instant, location.available_at, vessel.initial.available_at, delay)
            if release < min(revised.end, vessel.available_to):
                resources.append(dict(row, current_port=location.port, available_at=release.isoformat()))
                available_ids.add(rid)
        suffix[key] = resources
    suffix['compatible_pairs'] = [p for p in suffix['compatible_pairs']
                                  if p['tug_id'] in available_ids and p['barge_id'] in available_ids]
    for row in suffix['cargo']:
        row['volume_t'] = (revised.cargo[row['cargo_id']].kg - reserved[row['cargo_id']]) / 1000
    locked_rows = [{'cargo_type': revised.cargo[v.cargo_id].type, 'volume_t': v.kg / 1000,
                    'unloading_end': v.unloading_end.isoformat()} for v in locked]
    credited, _ = delivered_kg(revised.requirements, locked_rows)
    suffix['social_requirements'] = [{**r.as_dict(), 'required_t': max(0, r.required_kg - credited[r]) / 1000}
                                     for r in revised.requirements]
    result = run_scenario(suffix, strategy, limits=limits)
    combined = (*locked, *decode_voyages(result['voyages']))
    report = requirement_report(revised, combined)
    report['feasibility_status'] = result['feasibility_status']
    for item, remaining in zip(report['requirements'], result['requirements']):
        item['reason'] = remaining['reason']
        item['deficit_lower_bound_t'] = remaining['deficit_lower_bound_t']
    return {**output, 'status': 'REPLANNED', 'remaining_plan': result, 'projected_requirements': report}
