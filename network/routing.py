"""Time-dependent directed path search with explicit waits at ports."""

from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from heapq import heappop, heappush
from itertools import count

from generate import check_passage
from network.access import admission_error
from network.models import Leg, sailing_time


def passage_open(scenario, edge, depart, arrive, draft):
    if not any(start <= depart < arrive <= end for start, end in edge.windows):
        return False
    event = {'segment_id': edge.id, 'start': depart.isoformat(), 'end': arrive.isoformat()}
    return check_passage(event, draft, scenario, scenario.underkeel)


class Calendar:
    """Piecewise-constant daily conditions, including the default between overrides."""

    def __init__(self, edge, underkeel):
        self.underkeel = underkeel
        changes = {}
        for day, conditions in sorted(edge.overrides.items()):
            ordinal = date.fromisoformat(day).toordinal()
            changes[ordinal] = conditions
            changes[ordinal + 1] = edge.condition
        self.starts = [0]
        self.conditions = [edge.condition]
        for day, conditions in sorted(changes.items()):
            if conditions != self.conditions[-1]:
                self.starts.append(day)
                self.conditions.append(conditions)

    def blocked_until(self, depart, arrive, draft):
        """Return the end ordinal of the first impassable run or None if clear."""
        first = depart.date().toordinal()
        last = arrive.date().toordinal() - (arrive.time() == time.min)
        index = bisect_right(self.starts, first) - 1
        while True:
            row = self.conditions[index]
            next_index = index + 1
            end = self.starts[next_index] if next_index < len(self.starts) else date.max.toordinal() + 1
            if (row['is_closed'] or draft > row['max_allowed_draft_m'] + 1e-6
                    or draft + self.underkeel > row['usable_depth_m'] + 1e-6):
                return end
            if end > last:
                return None
            index = next_index


class Router:
    """Caches belong to one parsed scenario and one set of search limits."""

    def __init__(self, scenario, limits, check_budget=None):
        self.scenario = scenario
        self.limits = limits
        self.check_budget = check_budget or (lambda: None)
        self.calendars = {key: Calendar(edge, scenario.underkeel) for key, edge in scenario.edges.items()}
        self._paths = lru_cache(maxsize=8192)(self._search)
        self._edges = lru_cache(maxsize=512)(self._admitted_edges)
        self._distances = lru_cache(maxsize=1024)(self._lower_bounds)
        self.calendar_checks = 0

    def stats(self):
        info = self._paths.cache_info()
        return {'route_cache_hits': info.hits, 'route_cache_misses': info.misses,
                'route_cache_size': info.currsize, 'calendar_checks': self.calendar_checks}

    def _admitted_edges(self, tug_id, barge_id, reposition):
        tug, barge = self.scenario.tugs[tug_id], self.scenario.barges[barge_id]
        outgoing = {port: [] for port in self.scenario.ports}
        incoming = {port: [] for port in self.scenario.ports}
        for edge in sorted(self.scenario.edges.values(), key=lambda item: item.id):
            if (reposition and edge.marine and not self.scenario.allow_marine_reposition
                    or admission_error(self.scenario, edge, tug, barge)):
                continue
            duration = sailing_time(edge, tug)
            if not any(b - a >= duration for a, b in edge.windows):
                continue
            outgoing[edge.origin].append((edge, duration))
            incoming[edge.destination].append((edge.origin, duration))
        return outgoing, incoming

    def _lower_bounds(self, tug_id, barge_id, destination, reposition):
        _, incoming = self._edges(tug_id, barge_id, reposition)
        zero = timedelta()
        best = {destination: zero}
        queue = [(zero, destination)]
        while queue:
            duration, port = heappop(queue)
            if best[port] != duration:
                continue
            for previous, extra in incoming[port]:
                estimate = duration + extra
                if previous not in best or estimate < best[previous]:
                    best[previous] = estimate
                    heappush(queue, (estimate, previous))
        return best

    def lower_bound(self, tug, barge, origin, destination, *, reposition=False):
        return self._distances(tug.id, barge.id, destination, reposition).get(origin)

    def _earliest_leg(self, edge, duration, draft, ready, cutoff):
        calendar = self.calendars[edge.id]
        last_day = ready.date().toordinal() + self.limits.max_wait_days
        for start, end in edge.windows:
            depart = max(start, ready)
            latest = min(end, cutoff) - duration
            while depart <= latest and depart.date().toordinal() <= last_day:
                self.check_budget()
                arrive = depart + duration
                self.calendar_checks += 1
                resume = calendar.blocked_until(depart, arrive, draft)
                if resume is None:
                    return Leg(edge.id, depart, arrive)
                if resume > last_day or resume > date.max.toordinal():
                    break
                depart = datetime.combine(date.fromordinal(resume), time.min)
        return None

    def paths(self, tug, barge, origin, destination, ready, cutoff, draft, *, reposition=False):
        self.check_budget()
        return self._paths(tug.id, barge.id, origin, destination, ready, cutoff, draft, reposition)

    def _search(self, tug_id, barge_id, origin, destination, ready, cutoff, draft, reposition):
        if ready > cutoff:
            return ()
        if origin == destination:
            return ((),)
        lower = self._distances(tug_id, barge_id, destination, reposition).get(origin)
        if lower is None or lower > cutoff - ready:
            return ()
        outgoing, _ = self._edges(tug_id, barge_id, reposition)
        limits = self.limits
        serial = count()
        queue = [(ready, 0.0, next(serial), origin, (origin,), ())]
        results = []
        expansions = 0
        while queue and len(results) < limits.route_options and expansions < limits.max_route_expansions:
            self.check_budget()
            ready, km, _, port, visited, legs = heappop(queue)
            expansions += 1
            if port == destination:
                results.append(legs)
                continue
            if len(legs) >= limits.max_hops:
                continue
            for edge, duration in outgoing[port]:
                if edge.destination in visited:
                    continue
                leg = self._earliest_leg(edge, duration, draft, ready, cutoff)
                if leg is not None:
                    heappush(queue, (leg.arrive, km + edge.km, next(serial), edge.destination,
                                     (*visited, edge.destination), (*legs, leg)))
        return tuple(results)


def paths(scenario, tug, barge, origin, destination, ready, cutoff, draft, limits, *, reposition=False):
    return Router(scenario, limits).paths(tug, barge, origin, destination, ready, cutoff, draft,
                                         reposition=reposition)


def distance(scenario, legs):
    return sum(scenario.edges[leg.edge_id].km for leg in legs)
