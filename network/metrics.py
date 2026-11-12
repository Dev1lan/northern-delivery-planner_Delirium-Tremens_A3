"""Describe the audited itinerary as events and measurable work quantities."""

from collections import defaultdict

from network.routing import distance


def seconds(start, end):
    return int((end - start).total_seconds())


def event_rows(scenario, move, index):
    cargo = scenario.cargo[move.cargo_id]
    rows = []

    def event(state, origin, destination, start, end, edge=None, kg=0):
        if start < end:
            rows.append({'voyage_id': index, 'tug_id': move.tug_id, 'barge_id': move.barge_id,
                         'cargo_id': cargo.id if kg else None, 'state': state, 'origin_port': origin,
                         'destination_port': destination, 'start': start.isoformat(), 'end': end.isoformat(),
                         'edge_id': edge.id if edge else None, 'km': edge.km if edge else 0,
                         'volume_t': kg / 1000})

    def transit(legs, ready, kg):
        for leg in legs:
            edge = scenario.edges[leg.edge_id]
            event('WAITING', edge.origin, edge.origin, ready, leg.depart, kg=kg)
            event('LOADED_TRANSIT' if kg else 'EMPTY_TRANSIT', edge.origin, edge.destination,
                  leg.depart, leg.arrive, edge, kg)
            ready = leg.arrive
        return ready

    at_pickup = transit(move.empty, move.start, 0)
    event('WAITING', cargo.origin, cargo.origin, at_pickup, move.load_start)
    event('LOADING', cargo.origin, cargo.origin, move.load_start, move.departure, kg=move.kg)
    arrival = transit(move.loaded, move.departure, move.kg)
    event('UNLOADING', cargo.destination, cargo.destination, arrival, move.unloading_end, kg=move.kg)
    transit(move.return_empty, move.unloading_end, 0)
    return rows


def summarize(scenario, moves, report):
    critical = {category for r in scenario.requirements for category in r.cargo_types}
    critical.update(c.type for c in scenario.cargo.values() if c.critical_category is not None)
    by_cargo = defaultdict(int)
    by_type = defaultdict(int)
    timely = defaultdict(int)
    busy = defaultdict(int)
    loaded_km = empty_km = tonne_km = late_tonne_hours = 0.0
    transit_seconds = loading_seconds = unloading_seconds = waiting_seconds = 0
    details = []
    for index, move in enumerate(moves, 1):
        cargo = scenario.cargo[move.cargo_id]
        by_cargo[cargo.id] += move.kg
        by_type[cargo.type] += move.kg
        if move.unloading_end <= cargo.due:
            timely[cargo.type] += move.kg
        late = max(0, seconds(cargo.due, move.unloading_end))
        loaded = distance(scenario, move.loaded)
        empty = distance(scenario, (*move.empty, *move.return_empty))
        loaded_km += loaded
        empty_km += empty
        tonne_km += loaded * (move.kg / 1000)
        late_tonne_hours += late / 3600 * (move.kg / 1000)
        transit = sum(seconds(leg.depart, leg.arrive) for leg in (*move.empty, *move.loaded, *move.return_empty))
        loading = seconds(move.load_start, move.departure)
        unloading = seconds(move.loaded[-1].arrive, move.unloading_end)
        duration = seconds(move.start, move.end)
        waiting = duration - transit - loading - unloading
        transit_seconds += transit
        loading_seconds += loading
        unloading_seconds += unloading
        waiting_seconds += waiting
        busy[move.tug_id] += duration
        busy[move.barge_id] += duration
        details.append({'voyage_id': index, 'tug_id': move.tug_id, 'barge_id': move.barge_id,
                        'cargo_id': cargo.id, 'cargo_type': cargo.type, 'origin_port': cargo.origin,
                        'destination_port': cargo.destination, 'volume_t': move.kg / 1000,
                        'on_time': not late, 'late_seconds': late, 'loaded_km': loaded, 'empty_km': empty,
                        'tonne_km': loaded * (move.kg / 1000), 'transit_seconds': transit,
                        'loading_seconds': loading, 'unloading_seconds': unloading, 'waiting_seconds': waiting})
    resources = []
    for kind, fleet in (('tug', scenario.tugs), ('barge', scenario.barges)):
        for vessel in sorted(fleet.values(), key=lambda item: item.id):
            available = max(0, seconds(vessel.initial.available_at, min(scenario.end, vessel.available_to)))
            resources.append({'vessel_id': vessel.id, 'vessel_type': kind, 'busy_seconds': busy[vessel.id],
                              'idle_seconds': available - busy[vessel.id]})
    demand = sum(c.kg for c in scenario.cargo.values())
    delivered = sum(by_cargo.values())
    metrics = {'demand_t': demand / 1000, 'delivered_on_time_t': sum(timely.values()) / 1000,
               'delivered_total_t': delivered / 1000, 'undelivered_t': (demand - delivered) / 1000,
               'critical_on_time_t': sum(timely[k] for k in critical) / 1000,
               'social_deficit_t': round(sum(r['deficit_t'] for r in report['requirements']), 3),
               'loaded_km': round(loaded_km, 6), 'empty_km': round(empty_km, 6),
               'tonne_km': round(tonne_km, 6), 'late_tonne_hours': round(late_tonne_hours, 6),
               'resource_busy_hours': sum(busy.values()) / 3600,
               'resource_idle_hours': sum(r['idle_seconds'] for r in resources) / 3600,
               'completed_voyages': len(moves), 'repositioning_voyages': sum(bool(v.empty) for v in moves)}
    finance = {'voyages': details, 'resources': resources, 'totals': {
        'loaded_km': metrics['loaded_km'], 'empty_km': metrics['empty_km'], 'tonne_km': metrics['tonne_km'],
        'transit_seconds': transit_seconds, 'loading_seconds': loading_seconds,
        'unloading_seconds': unloading_seconds, 'waiting_seconds': waiting_seconds,
        'undelivered_t': metrics['undelivered_t'], 'late_tonne_hours': metrics['late_tonne_hours'],
        'social_deficit_t': metrics['social_deficit_t']}}
    cargo_status = [{'cargo_id': c.id, 'cargo_type': c.type, 'delivered_t': by_cargo[c.id] / 1000,
                     'remaining_t': (c.kg - by_cargo[c.id]) / 1000} for c in scenario.cargo.values()]
    return metrics, finance, cargo_status
