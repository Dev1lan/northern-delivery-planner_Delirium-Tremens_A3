"""Deterministic dispatch and bounded search over complete network schedules."""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from heapq import nlargest, nsmallest
from time import perf_counter

from network.models import Limits, Location, loaded_draft
from network.search_policy import SearchPolicy, TimeLimitReached
from network.routing import Router, distance
from network.ports import reservations
from network.simulator import voyages
from network.validator import replay

FCFS = 'NETWORK_FCFS'
GREEDY = 'NETWORK_GREEDY'
SOCIAL = 'NETWORK_SOCIAL'
REPOSITION = 'NETWORK_REPOSITION'
RETURN_EMPTY = 'NETWORK_RETURN_EMPTY'
COST = 'NETWORK_COST'
STRATEGIES = (FCFS, GREEDY, SOCIAL, REPOSITION)


@dataclass(frozen=True)
class State:
    locations: dict
    remaining: dict
    deficit: tuple[int, ...]
    on_time: int = 0
    delivered: int = 0
    empty_km: float = 0
    moves: tuple = ()
    bookings: dict = field(default_factory=dict)
    operating_cost: Decimal = Decimal(0)

    def score(self, social):
        minimums = (-sum(x > 0 for x in self.deficit), -sum(self.deficit)) if social else ()
        return (*minimums, self.on_time, self.delivered, -self.empty_km, -len(self.moves))

    def key(self):
        booked = tuple((key, tuple(sorted(value))) for key, value in sorted(self.bookings.items()))
        return tuple(sorted(self.locations.items())), tuple(sorted(self.remaining.items())), self.deficit, booked


@dataclass(frozen=True)
class Plan:
    strategy: str
    moves: tuple
    termination_reason: str
    expanded_states: int
    counterfactual: tuple | None = None
    reposition_selected: bool = False
    cost_service_floor: tuple | None = None


class Scheduler:
    def __init__(self, scenario, limits=None, cost_model=None, *, policy=None):
        self.scenario = scenario
        self.limits = limits or Limits()
        self.cost_model = cost_model
        self.policy = policy or SearchPolicy()
        self.deadline = None
        self.best = None
        self.best_score = None
        self.active_strategy = None
        self.active_score = None
        self.local_baseline = None
        self.service_floor = None
        self.expanded = 0
        self.stats = dict(assignments_considered=0, safe_pruned=0, heuristic_pruned=0,
                          detailed_checks=0, generated_candidates=0, generated_states=0, duplicate_states=0)
        self.router = Router(scenario, self.limits, self.check_budget)
        self.minimum_for_type = {category: i for i, r in enumerate(scenario.requirements) for category in r.cargo_types}
        self.cargo = tuple(sorted(scenario.cargo.values(), key=lambda c: (c.ready, c.id)))

        self.cargo_by_barge = {}
        self.cargo_by_barge_port = {}
        for key, barge in scenario.barges.items():
            compatible = tuple(c for c in self.cargo if c.type in barge.cargo_types)
            by_port = defaultdict(list)
            for cargo in compatible:
                by_port[cargo.origin].append(cargo)
            self.cargo_by_barge[key] = compatible
            self.cargo_by_barge_port[key] = by_port

    def check_budget(self):
        if self.deadline is not None and perf_counter() >= self.deadline:
            raise TimeLimitReached

    def record(self, state):
        if self.active_strategy in (FCFS, GREEDY, RETURN_EMPTY):
            self.best = state
            return
        score = self.active_score(state) if self.active_score else state.score(True)
        if self.best_score is None or score > self.best_score:
            self.best = state
            self.best_score = score

    def diagnostics(self):
        return {**self.stats, **self.router.stats()}

    def initial(self):
        s = self.scenario
        return State(s.initial_locations(), {c.id: c.kg for c in self.cargo},
                     tuple(r.required_kg for r in s.requirements))

    def append(self, state, move, return_policy=False):
        cargo = self.scenario.cargo[move.cargo_id]
        destination = cargo.origin if return_policy else cargo.destination
        location = Location(destination, move.end)
        deficits = list(state.deficit)
        index = self.minimum_for_type.get(cargo.type)
        if index is not None:
            deadline = self.scenario.requirements[index].deadline
            if deadline is None or move.unloading_end <= deadline:
                deficits[index] = max(0, deficits[index] - move.kg)
        empty = distance(self.scenario, (*move.empty, *move.return_empty))
        bookings = dict(state.bookings)
        for berth, interval in reservations(move):
            bookings[berth] = (*bookings.get(berth, ()), interval)
        charge = self.cost_model.voyage_cost(move) if self.cost_model else Decimal(0)
        self.stats['generated_states'] += 1
        return State({**state.locations, move.tug_id: location, move.barge_id: location},
                     {**state.remaining, cargo.id: state.remaining[cargo.id] - move.kg}, tuple(deficits),
                     state.on_time + (move.kg if move.unloading_end <= cargo.due else 0),
                     state.delivered + move.kg, state.empty_km + empty, (*state.moves, move), bookings,
                     state.operating_cost + charge)

    def assignments(self, state, strategy):
        s = self.scenario
        reposition = strategy in (REPOSITION, COST)
        for tug_id, barge_id in s.pairs:
            self.check_budget()
            tug_loc, barge_loc = state.locations[tug_id], state.locations[barge_id]
            if tug_loc.port != barge_loc.port:
                continue
            tug, barge = s.tugs[tug_id], s.barges[barge_id]
            ready = max(tug_loc.available_at, barge_loc.available_at)
            available_to = min(s.end, tug.available_to, barge.available_to)
            origin = tug_loc.port
            cargo_options = (self.cargo_by_barge[barge_id] if reposition
                             else self.cargo_by_barge_port[barge_id].get(origin, ()))
            for cargo in cargo_options:
                self.check_budget()
                self.stats['assignments_considered'] += 1
                capacity = min(barge.capacity_kg, state.remaining[cargo.id])
                cutoff = min(available_to, cargo.expires_at or available_to)
                if origin != cargo.origin:
                    cutoff = min(cutoff, cargo.due)
                if not capacity or max(ready, cargo.ready) >= cutoff:
                    self.stats['safe_pruned'] += 1
                    continue
                approach = self.router.lower_bound(tug, barge, origin, cargo.origin, reposition=True)
                loaded = self.router.lower_bound(tug, barge, cargo.origin, cargo.destination)
                if approach is None or loaded is None or max(ready + approach, cargo.ready) + loaded > cutoff:
                    self.stats['safe_pruned'] += 1
                    continue
                index = self.minimum_for_type.get(cargo.type) if strategy in (SOCIAL, REPOSITION, COST) else None
                requirement = s.requirements[index] if index is not None else None
                deficit = state.deficit[index] if index is not None else 0
                yield tug, barge, cargo, capacity, origin, ready, requirement, deficit, approach, loaded

    def detailed_assignments(self, state, strategy):
        assignments = self.assignments(state, strategy)
        policy = self.policy
        if all(limit is None for limit in (policy.top_k_pairs_per_cargo, policy.max_candidates_per_state,
                                           policy.max_detailed_checks_per_iteration)):
            yield from assignments
            return

        def estimate(item):
            _, _, cargo, capacity, origin, ready, requirement, deficit, approach, loaded = item
            earliest = max(ready + approach, cargo.ready) + loaded
            if strategy in (FCFS, RETURN_EMPTY):
                return cargo.ready, cargo.id, earliest, -capacity
            useful = min(capacity, deficit) if requirement and (
                requirement.deadline is None or earliest <= requirement.deadline) else 0
            elapsed = max(1, (earliest - ready).total_seconds())
            return (-int(useful > 0), -useful, earliest > cargo.due, -capacity / elapsed,
                    -capacity, origin != cargo.origin, earliest, cargo.id)

        ranked = sorted(assignments, key=estimate)
        per_cargo = {}
        selected = skipped = 0
        for item in ranked:
            self.check_budget()
            cargo = item[2].id
            if any(limit is not None and selected >= limit for limit in
                   (policy.max_candidates_per_state, policy.max_detailed_checks_per_iteration)):
                self.stats['heuristic_pruned'] += len(ranked) - selected - skipped
                break
            if policy.top_k_pairs_per_cargo is not None and per_cargo.get(cargo, 0) >= policy.top_k_pairs_per_cargo:
                self.stats['heuristic_pruned'] += 1
                skipped += 1
                continue
            per_cargo[cargo] = per_cargo.get(cargo, 0) + 1
            selected += 1
            yield item

    def candidates(self, state, strategy):
        s = self.scenario
        candidates = []
        try:
            for tug, barge, cargo, capacity, origin, ready, requirement, deficit, _, _ in self.detailed_assignments(state, strategy):
                self.stats['detailed_checks'] += 1
                for move in voyages(s, tug, barge, cargo, capacity, origin, ready, self.limits, router=self.router,
                                    allow_reposition=strategy in (REPOSITION, COST), require_return=strategy == RETURN_EMPTY,
                                    requirement=requirement, deficit_kg=deficit, bookings=state.bookings):
                    on_time = move.unloading_end <= cargo.due
                    if strategy in (FCFS, RETURN_EMPTY):
                        rank = (cargo.ready, cargo.id, move.load_start, -move.kg, move.end)
                    else:
                        useful = min(move.kg, deficit) if requirement and (
                            requirement.deadline is None or move.unloading_end <= requirement.deadline) else 0
                        elapsed = max(1, (move.end - move.start).total_seconds())
                        rank = (-int(useful > 0), -useful, -int(on_time), -(move.kg / elapsed), -move.kg,
                                distance(s, move.empty), move.end, cargo.due, cargo.id)
                        if strategy == COST:
                            rank = (*rank[:3], self.cost_model.voyage_cost(move) / move.kg, *rank[3:])
                    candidates.append((rank, move))
                    self.stats['generated_candidates'] += 1
        except TimeLimitReached:
            if candidates:
                _, move = min(candidates, key=lambda item: item[0])
                partial = self.append(state, move, strategy == RETURN_EMPTY)
                self.record(partial)
            raise
        return [move for _, move in nsmallest(self.limits.candidate_limit, candidates, key=lambda item: item[0])]

    def dispatch(self, initial, strategy):
        state = initial
        for expanded in range(self.limits.max_actions):
            self.check_budget()
            self.expanded += 1
            candidates = self.candidates(state, strategy)
            if not candidates:
                return state, expanded + 1, False
            state = self.append(state, candidates[0], strategy == RETURN_EMPTY)
            self.record(state)
        return state, self.limits.max_actions, True

    def search(self, initial, strategy, incumbent, *, skip_first=None, max_states=None, score=None):
        """Evaluate alternative cargo orders and pair assignments before committing to moves."""
        limits = self.limits
        budget = limits.max_states if max_states is None else max_states
        frontier = [initial]
        expanded = 0
        visited = {}
        if score is None:
            def score(state):
                return state.score(True)
        for _ in range(max(0, limits.max_actions - len(initial.moves))):
            next_frontier = {}
            for state in frontier:
                self.check_budget()
                key = state.key()
                previous_score = visited.get(key)
                state_score = score(state)
                if previous_score is not None and previous_score >= state_score:
                    self.stats['duplicate_states'] += 1
                    continue
                if expanded >= budget:
                    return incumbent, expanded, True
                visited[key] = state_score
                expanded += 1
                self.expanded += 1
                for move in self.candidates(state, strategy):
                    if state is initial and move == skip_first:
                        continue
                    child = self.append(state, move)
                    child_score = score(child)
                    if child_score > score(incumbent):
                        incumbent = child
                        self.record(child)
                    key = child.key()
                    old = next_frontier.get(key)
                    if old is not None:
                        self.stats['duplicate_states'] += 1
                    if old is None or child_score > score(old):
                        next_frontier[key] = child
            if not next_frontier:
                return incumbent, expanded, False
            frontier = nlargest(limits.beam_width, next_frontier.values(), key=score)
        return incumbent, expanded, True

    def repair(self, seed, incumbent, strategy, *, score=None):
        """Rebuild a few suffixes after excluding their previous first assignment."""
        prefix = seed
        original = incumbent.moves[len(seed.moves):]
        expanded, truncated = 0, False
        for move in original[:self.limits.repair_attempts]:
            incumbent, count, limited = self.search(prefix, strategy, incumbent, skip_first=move,
                                                    max_states=self.limits.repair_states, score=score)
            expanded += count
            truncated = truncated or limited
            prefix = self.append(prefix, move)
        return incumbent, expanded, truncated

    def plan(self, strategy):
        started = perf_counter()
        budget = self.policy.budget_seconds
        self.deadline = started + budget if budget is not None else None
        self.active_strategy = strategy
        self.best = self.initial()
        self.best_score = None
        self.active_score = self.local_baseline = self.service_floor = None
        self.expanded = 0
        try:
            return self._plan(strategy)
        except TimeLimitReached:
            state = self.best
            baseline = self.local_baseline
            if strategy == REPOSITION:
                baseline = baseline or state
            replay(self.scenario, state.moves, allow_reposition=strategy in (REPOSITION, COST),
                   allow_return=strategy == RETURN_EMPTY)
            return Plan(strategy, state.moves, 'TIME_LIMIT', self.expanded,
                        baseline.moves if strategy == REPOSITION else None,
                        any(move.empty for move in state.moves), self.service_floor)

    def _plan(self, strategy):
        if strategy not in (*STRATEGIES, RETURN_EMPTY, COST):
            raise ValueError(f'Unknown network strategy: {strategy}')
        if strategy == COST and self.cost_model is None:
            raise ValueError('NETWORK_COST requires version 2 tariffs')
        seed = self.initial()
        baseline = None
        reposition_selected = False
        floor = None
        if strategy in (FCFS, GREEDY, RETURN_EMPTY):
            state, expanded, truncated = self.dispatch(seed, strategy)
        else:
            simple = [self.dispatch(seed, name) for name in (FCFS, GREEDY, SOCIAL)]
            start = max((state for state, _, _ in simple), key=lambda item: item.score(True))
            baseline, expanded, truncated = self.search(seed, SOCIAL, start)
            baseline, count, limited = self.repair(seed, baseline, SOCIAL)
            expanded += count
            expanded += sum(count for _, count, _ in simple)
            truncated = truncated or limited or any(limit for _, _, limit in simple)
            state = self.local_baseline = baseline
            if strategy in (REPOSITION, COST):
                alternative, count, limited = self.search(seed, REPOSITION, baseline)
                expanded += count
                truncated = truncated or limited
                alternative, count, limited = self.repair(seed, alternative, REPOSITION)
                expanded += count
                truncated = truncated or limited
                # The local plan wins every tie, including the case where no path was found.
                if alternative.score(True) > baseline.score(True):
                    state = alternative
                    reposition_selected = any(move.empty for move in state.moves[len(seed.moves):])
            if strategy == COST:
                floor = (state.on_time, state.delivered)

                def cost_score(item):
                    penalty, _ = self.cost_model.residual(item.remaining)
                    return (*item.score(True)[:2], min(item.on_time, floor[0]), min(item.delivered, floor[1]),
                            -(item.operating_cost + penalty), *item.score(False))

                self.service_floor = floor
                self.active_score = cost_score
                self.best_score = None
                self.record(state)
                state, count, limited = self.search(seed, COST, state, score=cost_score)
                expanded += count
                truncated = truncated or limited
                state, count, limited = self.repair(seed, state, COST, score=cost_score)
                expanded += count
                truncated = truncated or limited
        # Reconstruct positions and mass independently before exposing the plan.
        replay(self.scenario, state.moves, allow_reposition=strategy in (REPOSITION, COST),
               allow_return=strategy == RETURN_EMPTY)
        return Plan(strategy, state.moves, 'SEARCH_LIMIT' if truncated else 'SEARCH_FINISHED', expanded,
                    baseline.moves if strategy == REPOSITION else None, reposition_selected, floor)
