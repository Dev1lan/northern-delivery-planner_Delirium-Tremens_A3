"""Deterministic dispatch and bounded search over complete network schedules."""

from dataclasses import dataclass, field

from network.models import Limits, Location
from network.routing import distance
from network.ports import reservations
from network.simulator import voyages
from network.validator import replay

FCFS = 'NETWORK_FCFS'
GREEDY = 'NETWORK_GREEDY'
SOCIAL = 'NETWORK_SOCIAL'
REPOSITION = 'NETWORK_REPOSITION'
RETURN_EMPTY = 'NETWORK_RETURN_EMPTY'
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


class Scheduler:
    def __init__(self, scenario, limits=None):
        self.scenario = scenario
        self.limits = limits or Limits()
        self.minimum_for_type = {category: i for i, r in enumerate(scenario.requirements) for category in r.cargo_types}
        self.cargo = tuple(sorted(scenario.cargo.values(), key=lambda c: (c.ready, c.id)))

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
        return State({**state.locations, move.tug_id: location, move.barge_id: location},
                     {**state.remaining, cargo.id: state.remaining[cargo.id] - move.kg}, tuple(deficits),
                     state.on_time + (move.kg if move.unloading_end <= cargo.due else 0),
                     state.delivered + move.kg, state.empty_km + empty, (*state.moves, move), bookings)

    def candidates(self, state, strategy):
        s = self.scenario
        social = strategy in (SOCIAL, REPOSITION)
        candidates = []
        for tug_id, barge_id in s.pairs:
            tug_loc, barge_loc = state.locations[tug_id], state.locations[barge_id]
            if tug_loc.port != barge_loc.port:
                continue
            tug, barge = s.tugs[tug_id], s.barges[barge_id]
            ready = max(tug_loc.available_at, barge_loc.available_at)
            for cargo in self.cargo:
                if not state.remaining[cargo.id] or cargo.type not in barge.cargo_types:
                    continue
                index = self.minimum_for_type.get(cargo.type) if social else None
                requirement = s.requirements[index] if index is not None else None
                deficit = state.deficit[index] if index is not None else 0
                for move in voyages(s, tug, barge, cargo, min(barge.capacity_kg, state.remaining[cargo.id]),
                                    tug_loc.port, ready, self.limits, allow_reposition=strategy == REPOSITION,
                                    require_return=strategy == RETURN_EMPTY, requirement=requirement,
                                    deficit_kg=deficit, bookings=state.bookings):
                    on_time = move.unloading_end <= cargo.due
                    if strategy in (FCFS, RETURN_EMPTY):
                        rank = (cargo.ready, cargo.id, move.load_start, -move.kg, move.end)
                    else:
                        useful = min(move.kg, deficit) if requirement and (
                            requirement.deadline is None or move.unloading_end <= requirement.deadline) else 0
                        elapsed = max(1, (move.end - move.start).total_seconds())
                        rank = (-int(useful > 0), -useful, -int(on_time), -(move.kg / elapsed), -move.kg,
                                distance(s, move.empty), move.end, cargo.due, cargo.id)
                    candidates.append((rank, move))
        candidates.sort(key=lambda item: item[0])
        return [move for _, move in candidates[:self.limits.candidate_limit]]

    def dispatch(self, initial, strategy):
        state = initial
        for expanded in range(self.limits.max_actions):
            candidates = self.candidates(state, strategy)
            if not candidates:
                return state, expanded + 1, False
            state = self.append(state, candidates[0], strategy == RETURN_EMPTY)
        return state, self.limits.max_actions, True

    def search(self, initial, strategy, incumbent, *, skip_first=None, max_states=None):
        """Evaluate alternative cargo orders and pair assignments before committing to moves."""
        limits = self.limits
        budget = limits.max_states if max_states is None else max_states
        frontier = [initial]
        expanded = 0
        visited = {}
        for _ in range(max(0, limits.max_actions - len(initial.moves))):
            next_frontier = {}
            for state in frontier:
                key = state.key()
                previous_score = visited.get(key)
                if previous_score is not None and previous_score >= state.score(True):
                    continue
                if expanded >= budget:
                    return incumbent, expanded, True
                visited[key] = state.score(True)
                expanded += 1
                for move in self.candidates(state, strategy):
                    if state is initial and move == skip_first:
                        continue
                    child = self.append(state, move)
                    score = child.score(True)
                    if score > incumbent.score(True):
                        incumbent = child
                    key = child.key()
                    old = next_frontier.get(key)
                    if old is None or score > old.score(True):
                        next_frontier[key] = child
            if not next_frontier:
                return incumbent, expanded, False
            frontier = sorted(next_frontier.values(), key=lambda item: item.score(True), reverse=True)[:limits.beam_width]
        return incumbent, expanded, True

    def repair(self, seed, incumbent, strategy):
        """Rebuild a few suffixes after excluding their previous first assignment."""
        prefix = seed
        original = incumbent.moves[len(seed.moves):]
        expanded, truncated = 0, False
        for move in original[:self.limits.repair_attempts]:
            incumbent, count, limited = self.search(prefix, strategy, incumbent, skip_first=move,
                                                    max_states=self.limits.repair_states)
            expanded += count
            truncated = truncated or limited
            prefix = self.append(prefix, move)
        return incumbent, expanded, truncated

    def plan(self, strategy):
        if strategy not in (*STRATEGIES, RETURN_EMPTY):
            raise ValueError(f'Unknown network strategy: {strategy}')
        seed = self.initial()
        baseline = None
        reposition_selected = False
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
            state = baseline
            if strategy == REPOSITION:
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
        # Reconstruct positions and mass independently before exposing the plan.
        replay(self.scenario, state.moves, allow_reposition=strategy == REPOSITION,
               allow_return=strategy == RETURN_EMPTY)
        return Plan(strategy, state.moves, 'SEARCH_LIMIT' if truncated else 'SEARCH_FINISHED', expanded,
                    baseline.moves if strategy == REPOSITION else None, reposition_selected)
