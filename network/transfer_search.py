"""Bounded search for complete two-leg deliveries from an audited plan."""

from dataclasses import replace

from network.simulator import voyages
from network.transshipment import direction, transfer_ready
from network.validator import ScheduleViolation, replay


def extend(scheduler, state, *, score=None, incumbent=None):
    best = state
    incumbent = best if incumbent is None else incumbent
    score = score or (lambda item: item.score(True))
    examined = 0
    while examined < scheduler.limits.repair_states:
        candidate = None
        candidate_score = None
        for proposal in _chains(scheduler, best):
            examined += 1
            proposal_score = score(proposal)
            if candidate_score is None or proposal_score > candidate_score:
                candidate, candidate_score = proposal, proposal_score
            if proposal_score > score(incumbent):
                incumbent = proposal
                scheduler.record(incumbent)
            if examined >= scheduler.limits.repair_states:
                break
        if candidate is None or candidate.delivered <= best.delivered:
            break
        best = candidate
    return incumbent


def _chains(scheduler, state):
    scenario = scheduler.scenario
    for cargo in scheduler.cargo:
        scheduler.check_budget()
        remaining = state.remaining[cargo.id]
        if not remaining:
            continue
        for port, terminal in scenario.terminals.items():
            if (port in (cargo.origin, cargo.destination) or cargo.type not in terminal.cargo_types
                    or terminal.capacity_kg == 0):
                continue
            capacity = min(remaining, terminal.capacity_kg) if terminal.capacity_kg is not None else remaining
            batch_id = f'{cargo.id}:{len(state.moves) + 1}'
            first_cargo = replace(cargo, destination=port)
            second_cargo = replace(cargo, origin=port)
            if not _may_reach(scheduler, second_cargo):
                continue
            for first in _options(scheduler, state, first_cargo, capacity):
                first = replace(first, batch_id=batch_id, stage=1, transfer_port=port)
                proposal, smaller = _finish(scheduler, state, cargo, first, terminal)
                if proposal is not None:
                    yield proposal
                    continue
                # The receiving barge or a shallower second leg may accept
                # less than the first. Re-simulate both stages at that mass.
                if smaller:
                    pair = (first.tug_id, first.barge_id)
                    for resized in _options(scheduler, state, first_cargo, smaller, pairs=(pair,)):
                        resized = replace(resized, batch_id=batch_id, stage=1, transfer_port=port)
                        proposal, _ = _finish(scheduler, state, cargo, resized, terminal)
                        if proposal is not None:
                            yield proposal
                            break


def _finish(scheduler, state, cargo, first, terminal):
    scenario = scheduler.scenario
    ready = transfer_ready(scenario, first)
    if ready >= min(scenario.end, cargo.due):
        return None, 0
    staged = scheduler.append(state, first)
    second_cargo = replace(cargo, origin=first.transfer_port, ready=ready)
    smaller = 0
    for second in _options(scheduler, staged, second_cargo, first.kg):
        if (first.tug_id, first.barge_id) == (second.tug_id, second.barge_id):
            continue
        if direction(scenario, first, second) not in terminal.directions:
            continue
        if second.kg != first.kg:
            smaller = max(smaller, second.kg)
            continue
        second = replace(second, batch_id=first.batch_id, stage=2, transfer_port=first.transfer_port)
        proposal = scheduler.append(staged, second)
        try:
            replay(scenario, proposal.moves)
        except ScheduleViolation:
            continue
        return proposal, 0
    return None, smaller


def _may_reach(scheduler, cargo):
    scenario = scheduler.scenario
    for tug_id, barge_id in scenario.pairs:
        scheduler.check_budget()
        tug, barge = scenario.tugs[tug_id], scenario.barges[barge_id]
        if cargo.type in barge.cargo_types:
            travel = scheduler.router.lower_bound(tug, barge, cargo.origin, cargo.destination)
            if travel is not None and cargo.ready + travel < min(cargo.due, tug.available_to, barge.available_to):
                return True
    return False


def _options(scheduler, state, cargo, capacity, *, pairs=None):
    scenario = scheduler.scenario
    for tug_id, barge_id in scenario.pairs if pairs is None else pairs:
        scheduler.check_budget()
        tug, barge = scenario.tugs[tug_id], scenario.barges[barge_id]
        a, b = state.locations[tug_id], state.locations[barge_id]
        if a.port != b.port or cargo.type not in barge.cargo_types:
            continue
        ready = max(a.available_at, b.available_at)
        router = scheduler.router
        approach = router.lower_bound(tug, barge, a.port, cargo.origin, reposition=True)
        loaded = router.lower_bound(tug, barge, cargo.origin, cargo.destination)
        if approach is None or loaded is None or max(ready + approach, cargo.ready) + loaded >= cargo.due:
            continue
        yield from voyages(scenario, tug, barge, cargo, min(capacity, barge.capacity_kg),
                            a.port, ready, scheduler.limits, router=router, allow_reposition=True,
                            bookings=state.bookings, finish_by=cargo.due)
