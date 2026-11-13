"""Deterministic dispatch and bounded search over complete network schedules."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import lru_cache
from heapq import nlargest, nsmallest
from time import perf_counter

from network.models import Limits, Location
from network.search_policy import SearchPolicy, TimeLimitReached
from network.routing import Router, distance
from network.ports import reservations
from network.sea_allocation import SeaAllocation
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
        self.reset_counters()
        self.router = Router(scenario, self.limits, self.check_budget)
        self.sea_allocation = None
        self.sea_dispatch = False
        self.minimum_for_type = {category: i for i, r in enumerate(scenario.requirements) for category in r.cargo_types}
        self.cargo = tuple(sorted(scenario.cargo.values(), key=lambda c: (c.ready, c.id)))

        self.assignment_options = lru_cache(maxsize=4096)(self._assignment_options)
        self.assignment_voyages = lru_cache(maxsize=16384)(self._assignment_voyages)
        self.pair_representative = lru_cache(maxsize=4096)(self._pair_representative)
        self.equivalent_pairs = {}
        self.berth_ids = {port: tuple(b.id for b in berths) for port, berths in scenario.berths.items()}
        self.cargo_by_barge = {}
        self.cargo_by_barge_port = {}
        for key, barge in scenario.barges.items():
            compatible = tuple(c for c in self.cargo if c.type in barge.cargo_types)
            by_port = defaultdict(list)
            for cargo in compatible:
                by_port[cargo.origin].append(cargo)
            self.cargo_by_barge[key] = compatible
            self.cargo_by_barge_port[key] = by_port

    def reset_counters(self):
        self.cargo_search = defaultdict(Counter)
        self.sea_adjustments = 0
        self.stats = dict(assignments_considered=0, safe_pruned=0, heuristic_pruned=0,
                          detailed_checks=0, generated_candidates=0, generated_states=0,
                          duplicate_states=0, beam_pruned=0)

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
        info = self.assignment_voyages.cache_info()
        return {**self.stats, **self.router.stats(), 'voyage_cache_hits': info.hits,
                'voyage_cache_misses': info.misses,
                **({'sea_opportunity_adjustments': self.sea_adjustments} if self.policy.sea_fleet_allocation else {}),
                **({'cargo_search': dict(self.cargo_search)} if self.policy.recover_undelivered else {})}

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

    def _assignment_options(self, tug_id, barge_id, origin, reposition):
        s = self.scenario
        tug, barge = s.tugs[tug_id], s.barges[barge_id]
        cargo_options = (self.cargo_by_barge[barge_id] if reposition
                         else self.cargo_by_barge_port[barge_id].get(origin, ()))
        available_to = min(s.end, tug.available_to, barge.available_to)
        options = []
        excluded = 0
        for cargo in cargo_options:
            self.check_budget()
            cutoff = min(available_to, cargo.expires_at or available_to)
            if origin != cargo.origin:
                cutoff = min(cutoff, cargo.due)
            approach = self.router.lower_bound(tug, barge, origin, cargo.origin, reposition=True)
            loaded = self.router.lower_bound(tug, barge, cargo.origin, cargo.destination)
            if approach is None or loaded is None or cargo.ready + loaded > cutoff:
                excluded += 1
                continue
            options.append((cargo, cutoff, approach, loaded))
        return tuple(options), excluded

    def assignments(self, state, strategy):
        s = self.scenario
        reposition = strategy in (REPOSITION, COST)
        social = strategy in (SOCIAL, REPOSITION, COST)
        for tug_id, barge_id in s.pairs:
            self.check_budget()
            tug_loc, barge_loc = state.locations[tug_id], state.locations[barge_id]
            if tug_loc.port != barge_loc.port:
                continue
            tug, barge = s.tugs[tug_id], s.barges[barge_id]
            ready = max(tug_loc.available_at, barge_loc.available_at)
            origin = tug_loc.port
            options, excluded = self.assignment_options(tug_id, barge_id, origin, reposition)
            self.stats['safe_pruned'] += excluded
            self.stats['assignments_considered'] += len(options) + excluded
            for cargo, cutoff, approach, loaded in options:
                self.check_budget()
                capacity = min(barge.capacity_kg, state.remaining[cargo.id])
                if not capacity or max(ready, cargo.ready) >= cutoff or max(ready + approach, cargo.ready) + loaded > cutoff:
                    self.stats['safe_pruned'] += 1
                    continue
                index = self.minimum_for_type.get(cargo.type) if social else None
                requirement = s.requirements[index] if index is not None else None
                deficit = state.deficit[index] if index is not None else 0
                yield tug, barge, cargo, capacity, origin, ready, index, requirement, deficit, approach, loaded

    def detailed_assignments(self, state, strategy):
        assignments = self.assignments(state, strategy)
        policy = self.policy
        if all(limit is None for limit in (policy.top_k_pairs_per_cargo, policy.max_candidates_per_state,
                                           policy.max_detailed_checks_per_iteration)):
            yield from assignments
            return

        def estimate(item):
            _, _, cargo, capacity, origin, ready, _, requirement, deficit, approach, loaded = item
            earliest = max(ready + approach, cargo.ready) + loaded
            if strategy in (FCFS, RETURN_EMPTY):
                return cargo.ready, cargo.id, earliest, -capacity
            useful = min(capacity, deficit) if requirement and (
                requirement.deadline is None or earliest <= requirement.deadline) else 0
            elapsed = max(1, (earliest - ready).total_seconds())
            return (-int(useful > 0), -useful, earliest > cargo.due, -capacity / elapsed,
                    -capacity, origin != cargo.origin, earliest, cargo.id)

        ranked = sorted(assignments, key=estimate)
        if policy.top_k_pairs_per_cargo is not None:
            by_cargo = defaultdict(list)
            for item in ranked:
                by_cargo[item[2].id].append(item)
            selected = []
            for options in by_cargo.values():
                classes = set()
                distinct, repeated = [], []
                for item in options:
                    self.check_budget()
                    tug, barge = item[:2]
                    signature = (tug.access.vessel_class, barge.access.vessel_class)
                    if policy.recover_undelivered:
                        signature = (*signature, item[4])
                    (repeated if signature in classes else distinct).append(item)
                    classes.add(signature)
                selected.extend((*distinct, *repeated)[:policy.top_k_pairs_per_cargo])
            self.stats['heuristic_pruned'] += len(ranked) - len(selected)
            kept = {(item[0].id, item[1].id, item[2].id) for item in selected}
            if policy.recover_undelivered:
                for item in ranked:
                    if (item[0].id, item[1].id, item[2].id) not in kept:
                        self.cargo_search[item[2].id]['assignment_pruned'] += 1
            ranked = [item for item in ranked if (item[0].id, item[1].id, item[2].id) in kept]
        limits = [limit for limit in (policy.max_candidates_per_state, policy.max_detailed_checks_per_iteration)
                  if limit is not None]
        if limits:
            keep = min(limits)
            self.stats['heuristic_pruned'] += max(0, len(ranked) - keep)
            if policy.recover_undelivered:
                distinct = {}
                for item in ranked:
                    distinct.setdefault((item[2].id, item[4]), item)
                # Reserve one quarter for distinct pickup assignments.
                chosen = list(distinct.values())[:max(1, keep // 4)]
                ids = {id(item) for item in chosen}
                chosen.extend(item for item in ranked if id(item) not in ids)
                ids = {id(item) for item in chosen[:keep]}
                for item in ranked:
                    if id(item) not in ids:
                        self.cargo_search[item[2].id]['assignment_pruned'] += 1
                ranked = [item for item in ranked if id(item) in ids]
            else:
                ranked = ranked[:keep]
        yield from ranked

    def _pair_representative(self, tug_id, barge_id):
        tug, barge = self.scenario.tugs[tug_id], self.scenario.barges[barge_id]
        # Admission and sailing duration for every edge must agree. Include
        # class for berth access and both ends of the loaded-draft formula.
        key = (self.router.pair_signature(tug_id, barge_id), min(tug.available_to, barge.available_to),
               tug.draft, barge.draft_empty, barge.draft_full, barge.capacity_kg,
               barge.handling_tph, barge.access.vessel_class)
        return self.equivalent_pairs.setdefault(key, (tug_id, barge_id))

    def _assignment_voyages(self, tug_id, barge_id, cargo_id, capacity, origin, ready,
                            requirement_index, deficit, allow_reposition, return_empty, reservations):
        s = self.scenario
        requirement = s.requirements[requirement_index] if requirement_index is not None else None
        return voyages(s, s.tugs[tug_id], s.barges[barge_id], s.cargo[cargo_id], capacity, origin,
                       ready, self.limits, router=self.router, allow_reposition=allow_reposition,
                       require_return=return_empty, requirement=requirement, deficit_kg=deficit,
                       bookings=dict(reservations))

    def candidates(self, state, strategy):
        s = self.scenario
        best = None
        distinct = {}
        traced = self.policy.recover_undelivered
        allocation = self.sea_allocation if self.sea_dispatch else None
        witnesses = {}

        def ranked_moves():
            nonlocal best
            for tug, barge, cargo, capacity, origin, ready, index, _, deficit, _, _ in self.detailed_assignments(state, strategy):
                self.stats['detailed_checks'] += 1
                if traced:
                    self.cargo_search[cargo.id]['detailed'] += 1
                relevant_berths = (*self.berth_ids.get(cargo.origin, ()), *self.berth_ids.get(cargo.destination, ()))
                bookings = tuple((key, tuple(sorted(state.bookings[key]))) for key in relevant_berths
                                 if key in state.bookings)
                requirement = s.requirements[index] if index is not None else None
                actual_pair = (tug.id, barge.id)
                representative = self.pair_representative(*actual_pair)
                for move in self.assignment_voyages(*representative, cargo.id, capacity, origin, ready,
                                                    index, deficit, strategy in (REPOSITION, COST),
                                                    strategy == RETURN_EMPTY, bookings):
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
                            priced_move = self.assign_pair(move, actual_pair)
                            rank = (*rank[:3], self.cost_model.voyage_cost(priced_move) / move.kg, *rank[3:])
                    item = (rank, move, actual_pair)
                    if self.deadline is not None and (best is None or rank < best[0]):
                        best = item
                    self.stats['generated_candidates'] += 1
                    if traced:
                        self.cargo_search[cargo.id]['feasible'] += 1
                        key = (cargo.id, origin)
                        previous = distinct.get(key)
                        if previous is None or rank < previous[0]:
                            distinct[key] = item
                    if allocation and on_time:
                        if allocation.occupies_sea(move):
                            key = (cargo.id, actual_pair)
                            previous = witnesses.get(key)
                            if previous is None or rank < previous[0]:
                                witnesses[key] = item
                        elif allocation.specialized.isdisjoint(actual_pair):
                            previous = witnesses.get(cargo.id)
                            if previous is None or move.kg > previous[1].kg:
                                witnesses[cargo.id] = item
                    yield item

        try:
            selected = nsmallest(self.limits.candidate_limit, ranked_moves(), key=lambda item: item[0])
        except TimeLimitReached:
            if best is not None:
                _, move, pair = best
                self.record(self.append(state, self.assign_pair(move, pair), strategy == RETURN_EMPTY))
            raise
        if allocation and witnesses:
            items = list({id(item): item for item in (*selected, *witnesses.values())}.values())
            selected, adjustments = allocation.shortlist(items, self.limits.candidate_limit, state.remaining)
            self.sea_adjustments += adjustments
        if traced:
            if not allocation and strategy in (REPOSITION, COST) and self.limits.candidate_limit > 1:
                count = max(1, self.limits.candidate_limit // 4)
                leaders = selected[:self.limits.candidate_limit - count]
                represented = {(move.cargo_id, state.locations[pair[0]].port) for _, move, pair in leaders}
                reserve = nsmallest(count, (item for key, item in distinct.items() if key not in represented),
                                    key=lambda item: item[0])
                seen = {id(item) for item in reserve}
                selected = reserve + [item for item in selected if id(item) not in seen][
                    :self.limits.candidate_limit - len(reserve)]
                selected.sort(key=lambda item: item[0])
            for _, move, _ in selected:
                self.cargo_search[move.cargo_id]['shortlisted'] += 1
        return [self.assign_pair(move, pair) for _, move, pair in selected]

    @staticmethod
    def assign_pair(move, pair):
        if (move.tug_id, move.barge_id) == pair:
            return move
        return replace(move, tug_id=pair[0], barge_id=pair[1])

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
            self.stats['beam_pruned'] += max(0, len(next_frontier) - limits.beam_width)
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
        self.reset_counters()
        try:
            if self.policy.sea_fleet_allocation and strategy in (REPOSITION, COST):
                self.sea_allocation = SeaAllocation(self.scenario, self.router)
                if not self.sea_allocation.specialized:
                    self.sea_allocation = None
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
            if (self.policy.recover_undelivered or self.sea_allocation) and strategy in (REPOSITION, COST):
                return self._recover_plan(seed, strategy)
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

    def _recover_plan(self, seed, strategy):
        # Build a service incumbent before optimizing cost. Sea allocation
        # starts with joint dispatch so the local reference cannot consume its budget.
        if self.sea_allocation:
            local, local_limited = seed, False
            self.local_baseline = local
        else:
            local, _, local_limited = self.dispatch(seed, SOCIAL)
            self.local_baseline = local
        self.sea_dispatch = self.sea_allocation is not None
        try:
            greedy, _, greedy_limited = self.dispatch(seed, REPOSITION)
        finally:
            self.sea_dispatch = False
        state, _, limited = self.search(seed, REPOSITION, max((local, greedy), key=lambda item: item.score(True)))
        limited = limited or greedy_limited
        if self.sea_allocation and strategy == REPOSITION:
            local, _, local_limited = self.dispatch(seed, SOCIAL)
            self.local_baseline = local
            state = max((local, state), key=lambda item: item.score(True))
        state, _, repaired = self.repair(seed, state, REPOSITION)
        floor = None
        if strategy == COST:
            floor = self.service_floor = (state.on_time, state.delivered)

            def score(item):
                penalty, _ = self.cost_model.residual(item.remaining)
                meets_floor = item.on_time >= floor[0] and item.delivered >= floor[1]
                return (meets_floor, *item.score(True)[:4], -(item.operating_cost + penalty),
                        *item.score(False)[2:])

            self.active_score = score
            self.best_score = None
            self.record(state)
            state, _, cost_limited = self.search(seed, COST, state, score=score)
            limited = limited or cost_limited
        replay(self.scenario, state.moves)
        return Plan(strategy, state.moves, 'SEARCH_LIMIT' if limited or local_limited or repaired
                    else 'SEARCH_FINISHED', self.expanded, local.moves if strategy == REPOSITION else None,
                    any(move.empty for move in state.moves), floor)
