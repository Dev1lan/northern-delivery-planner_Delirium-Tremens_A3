"""Time-dependent directed path search with explicit waits at ports."""

from datetime import datetime, time, timedelta
from heapq import heappop, heappush
from itertools import count

from generate import check_passage
from network.models import Leg, sailing_time


def passage_open(scenario, edge, depart, arrive, draft):
    event = {'segment_id': edge.id, 'start': depart.isoformat(), 'end': arrive.isoformat()}
    return check_passage(event, draft, scenario, scenario.underkeel)


def _earliest_leg(scenario, edge, tug, draft, ready, cutoff, wait_days):
    duration = sailing_time(edge, tug)
    depart = ready
    for _ in range(wait_days + 1):
        arrive = depart + duration
        if arrive > cutoff:
            return None
        if passage_open(scenario, edge, depart, arrive, draft):
            return Leg(edge.id, depart, arrive)
        depart = datetime.combine(depart.date() + timedelta(days=1), time.min)
    return None


def paths(scenario, tug, origin, destination, ready, cutoff, draft, limits):
    """Return a bounded set of feasible simple paths ordered by arrival, then km."""
    if ready > cutoff:
        return ()
    if origin == destination:
        return ((),)
    serial = count()
    queue = [(ready, 0.0, next(serial), origin, (origin,), ())]
    results = []
    expansions = 0
    while queue and len(results) < limits.route_options and expansions < limits.max_route_expansions:
        ready, km, _, port, visited, legs = heappop(queue)
        expansions += 1
        if port == destination:
            results.append(legs)
            continue
        if len(legs) >= limits.max_hops:
            continue
        for edge in scenario.outgoing[port]:
            if edge.destination in visited:
                continue
            leg = _earliest_leg(scenario, edge, tug, draft, ready, cutoff, limits.max_wait_days)
            if leg is not None:
                heappush(queue, (leg.arrive, km + edge.km, next(serial), edge.destination,
                                 (*visited, edge.destination), (*legs, leg)))
    return tuple(results)


def distance(scenario, legs):
    return sum(scenario.edges[leg.edge_id].km for leg in legs)
