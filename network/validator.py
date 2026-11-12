"""Replay proposed voyages against the inputs, independently of path search."""

from dataclasses import dataclass

from network.models import Location, handling_time, loaded_draft, sailing_time
from network.routing import passage_open
from social_requirements import evaluate


class ScheduleViolation(ValueError):
    def __init__(self, index, code, detail):
        super().__init__(f'Voyage {index}: {code}: {detail}')
        self.index = index
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {'voyage': self.index, 'code': self.code, 'detail': self.detail}


@dataclass
class Replay:
    locations: dict
    remaining: dict


def replay(scenario, voyages, *, allow_reposition=True, allow_return=False, initial=None):
    locations = scenario.initial_locations() if initial is None else dict(initial.locations)
    remaining = {c.id: c.kg for c in scenario.cargo.values()} if initial is None else dict(initial.remaining)
    for index, move in sorted(enumerate(voyages, 1), key=lambda item: (item[1].start, item[0])):
        def require(condition, code, detail):
            if not condition:
                raise ScheduleViolation(index, code, detail)

        pair = (move.tug_id, move.barge_id)
        require(pair in scenario.pairs, 'INCOMPATIBLE_PAIR', str(pair))
        require(move.cargo_id in scenario.cargo, 'UNKNOWN_CARGO', move.cargo_id)
        tug = scenario.tugs[move.tug_id]
        barge = scenario.barges[move.barge_id]
        cargo = scenario.cargo[move.cargo_id]
        tug_state, barge_state = locations[tug.id], locations[barge.id]
        require(move.start >= max(tug_state.available_at, barge_state.available_at),
                'RESOURCE_BUSY', str(pair))
        require(tug_state.port == barge_state.port, 'DIFFERENT_PORTS', str(pair))
        require(scenario.start <= move.start < move.end <= min(scenario.end, tug.available_to, barge.available_to),
                'OUTSIDE_WINDOW', str(pair))
        require(cargo.type in barge.cargo_types, 'INCOMPATIBLE_CARGO', cargo.id)
        require(type(move.kg) is int and 0 < move.kg <= barge.capacity_kg, 'CAPACITY', cargo.id)
        require(move.kg <= remaining[cargo.id], 'CARGO_BALANCE', cargo.id)
        require(move.load_start >= cargo.ready, 'CARGO_NOT_READY', cargo.id)
        require(move.loaded, 'MISSING_LOADED_PATH', cargo.id)

        def follow(legs, origin, destination, not_before, kg):
            current = origin
            visited = {origin}
            draft = loaded_draft(tug, barge, kg)
            for leg in legs:
                edge = scenario.edges.get(leg.edge_id)
                require(edge is not None, 'UNKNOWN_EDGE', leg.edge_id)
                require(edge.origin == current, 'DISCONTINUOUS_PATH', leg.edge_id)
                require(edge.destination not in visited, 'PATH_CYCLE', leg.edge_id)
                require(not_before <= leg.depart and leg.arrive == leg.depart + sailing_time(edge, tug),
                        'TRANSIT_TIME', leg.edge_id)
                require(passage_open(scenario, edge, leg.depart, leg.arrive, draft), 'UNSAFE_PASSAGE', leg.edge_id)
                current, not_before = edge.destination, leg.arrive
                visited.add(current)
            require(current == destination, 'WRONG_PORT', f'{current} → {destination}')
            return not_before

        if move.empty:
            require(allow_reposition and tug_state.port != cargo.origin, 'UNJUSTIFIED_EMPTY_PATH', cargo.id)
            require(move.unloading_end <= cargo.due, 'REPOSITION_MISSES_DEADLINE', cargo.id)
        at_pickup = follow(move.empty, tug_state.port, cargo.origin, move.start, 0)
        handling = handling_time(scenario, barge, move.kg)
        require(move.load_start >= at_pickup and move.departure == move.load_start + handling,
                'LOADING_TIME', cargo.id)
        arrival = follow(move.loaded, cargo.origin, cargo.destination, move.departure, move.kg)
        require(move.unloading_end == arrival + handling, 'UNLOADING_TIME', cargo.id)
        require(bool(move.return_empty) == allow_return, 'RETURN_POLICY', cargo.id)
        final_port = cargo.destination
        if move.return_empty:
            follow(move.return_empty, cargo.destination, cargo.origin, move.unloading_end, 0)
            final_port = cargo.origin
        location = Location(final_port, move.end)
        locations[tug.id] = locations[barge.id] = location
        remaining[cargo.id] -= move.kg
    return Replay(locations, remaining)


def requirement_report(scenario, voyages):
    trips = [{'cargo_type': scenario.cargo[v.cargo_id].type, 'volume_t': v.kg / 1000,
              'unloading_end': v.unloading_end.isoformat()} for v in voyages]
    return evaluate(scenario.requirements, scenario.social_input, trips)


def audit(scenario, voyages, *, allow_reposition=True, allow_return=False):
    try:
        replay(scenario, voyages, allow_reposition=allow_reposition, allow_return=allow_return)
    except ScheduleViolation as exc:
        return {'schedule_audit': {'status': 'FAILED', 'violations': [exc.as_dict()]},
                'requirements_status': 'NOT_CONFIGURED' if not scenario.requirements else 'NOT_EVALUATED',
                'feasibility_status': 'NOT_CHECKED', 'requirements': []}
    return {'schedule_audit': {'status': 'PASSED', 'violations': []}, **requirement_report(scenario, voyages)}
