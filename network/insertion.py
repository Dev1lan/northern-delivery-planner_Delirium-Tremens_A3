"""Find auditable additions between fixed voyages without moving their resources."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from network.models import Location
from network.ports import reservations
from network.routing import Router
from network.simulator import voyages
from network.validator import ScheduleViolation, replay, requirement_report


@dataclass(frozen=True)
class Gap:
    location: Location
    end: datetime
    next_port: str | None


def free_intervals(scenario, fixed):
    timeline = defaultdict(list)
    for move in sorted(fixed, key=lambda move: move.start):
        cargo = scenario.cargo[move.cargo_id]
        origin = scenario.edges[move.empty[0].edge_id].origin if move.empty else cargo.origin
        destination = cargo.origin if move.return_empty else cargo.destination
        for key in (move.tug_id, move.barge_id):
            timeline[key].append((move.start, move.end, origin, destination))
    result = {}
    for key, vessel in {**scenario.tugs, **scenario.barges}.items():
        location = vessel.initial
        gaps = []
        for start, end, origin, destination in timeline[key]:
            if location.available_at < start:
                gaps.append(Gap(location, start, origin))
            location = Location(destination, end)
        end = min(vessel.available_to, scenario.end)
        if location.available_at < end:
            gaps.append(Gap(location, end, None))
        result[key] = gaps
    return result


def overlaps(left, right):
    i = j = 0
    while i < len(left) and j < len(right):
        a, b = left[i], right[j]
        start = max(a.location.available_at, b.location.available_at)
        end = min(a.end, b.end)
        if start < end and a.location.port == b.location.port:
            yield a.location.port, start, end, a.next_port, b.next_port
        if a.end <= b.end:
            i += 1
        if b.end <= a.end:
            j += 1


class FixedPlan:
    def __init__(self, scenario, moves, limits):
        self.scenario = scenario
        self.moves = tuple(moves)
        self.limits = limits
        self.router = Router(scenario, limits)
        self.gaps = free_intervals(scenario, self.moves)
        self.bookings = defaultdict(list)
        for move in self.moves:
            for berth, interval in reservations(move):
                self.bookings[berth].append(interval)
        self.pairs = tuple((scenario.tugs[t], scenario.barges[b]) for t, b in scenario.pairs)

    def candidates(self, cargo, kg, *, pair_filter=None):
        for tug, barge in self.pairs:
            if cargo.type not in barge.cargo_types or (pair_filter and not pair_filter(tug, barge)):
                continue
            for port, start, end, next_tug, next_barge in overlaps(self.gaps[tug.id], self.gaps[barge.id]):
                if next_tug not in (None, cargo.destination) or next_barge not in (None, cargo.destination):
                    continue
                approach = self.router.lower_bound(tug, barge, port, cargo.origin, reposition=True)
                loaded = self.router.lower_bound(tug, barge, cargo.origin, cargo.destination)
                if approach is None or loaded is None or max(start + approach, cargo.ready) + loaded >= end:
                    continue
                for move in voyages(self.scenario, tug, barge, cargo, min(kg, barge.capacity_kg), port, start,
                                    self.limits, router=self.router, allow_reposition=True,
                                    bookings=self.bookings, finish_by=end):
                    yield move

    def accepts(self, move):
        try:
            replay(self.scenario, (*self.moves, move))
        except ScheduleViolation:
            return False
        return True


def plan_score(scenario, moves, cost_model=None):
    """Service-first score for audited local changes, with actual usable cargo."""
    report = requirement_report(scenario, moves)
    usable = [move for move in moves if scenario.cargo[move.cargo_id].usable_at(move.unloading_end)]
    timely = sum(move.kg for move in usable if move.unloading_end <= scenario.cargo[move.cargo_id].due)
    total = sum(move.kg for move in usable)
    cost = 0
    if cost_model:
        cost = sum(cost_model.voyage_cost(move) for move in moves)
    return (-sum(not row['satisfied'] for row in report['requirements']),
            -round(sum(row['deficit_t'] for row in report['requirements']), 3), timely, total, -cost)


def improve_fixed_plan(scenario, moves, limits, *, cost_model=None, max_attempts=24):
    """Test bounded additions and reschedule terminal voyages at their next free slot."""
    original = tuple(moves)
    best = original
    score = plan_score(scenario, best, cost_model)
    required = {row['requirement_id'] for row in requirement_report(scenario, original)['requirements']
                if row['satisfied']}

    def preserves_minima(proposal):
        return required <= {row['requirement_id'] for row in requirement_report(scenario, proposal)['requirements']
                            if row['satisfied']}
    served = replay(scenario, best)
    remaining_cargo = sorted((c for c in scenario.cargo.values() if served.remaining[c.id]),
                             key=lambda c: (not bool(c.critical_category), c.due, c.id))
    base = FixedPlan(scenario, original, limits)
    tested = 0
    generated = 0
    for cargo in remaining_cargo:
        for move in base.candidates(cargo, served.remaining[cargo.id]):
            generated += 1
            if not base.accepts(move):
                continue
            proposal = (*original, move)
            tested += 1
            quality = plan_score(scenario, proposal, cost_model)
            if quality > score:
                best, score = proposal, quality
            break
    latest = {}
    for move in sorted(original, key=lambda item: item.start):
        latest[move.tug_id] = latest[move.barge_id] = move
    terminals = [move for move in reversed(original)
                 if latest[move.tug_id] == latest[move.barge_id] == move]
    for removed in terminals[:max_attempts]:
        prefix = tuple(move for move in original if move is not removed)
        fixed = FixedPlan(scenario, prefix, limits)
        targets = (scenario.cargo[removed.cargo_id],
                   *(cargo for cargo in remaining_cargo if cargo.id != removed.cargo_id))
        for cargo in targets:
            kg = served.remaining[cargo.id] + (removed.kg if removed.cargo_id == cargo.id else 0)
            # Check the first auditable replacement across compatible pairs.
            for move in fixed.candidates(cargo, kg):
                generated += 1
                if move == removed or not fixed.accepts(move):
                    continue
                proposal = (*prefix, move)
                tested += 1
                quality = plan_score(scenario, proposal, cost_model)
                if quality > score and preserves_minima(proposal):
                    best, score = proposal, quality
                break
    return best, {'generated_proposals': generated, 'audited_proposals': tested,
                  'terminal_voyages_checked': min(max_attempts, len(terminals)), 'improved': best != original}
