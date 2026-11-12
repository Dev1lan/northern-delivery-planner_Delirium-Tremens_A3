"""Validated network inputs and the itinerary format shared by planner and auditor."""

from dataclasses import dataclass
from datetime import date as calendar_date, datetime, timedelta
import math
from network.access import VesselAccess, provenance, vessel_access

from scenario_validation import date, ident, number
from social_requirements import Requirement, kilograms, parse_requirements


def timestamp(value, label):
    result = date(value, label)
    if result.microsecond:
        raise ValueError(f'{label} must use whole seconds')
    return result


@dataclass(frozen=True)
class Location:
    port: str
    available_at: datetime


@dataclass(frozen=True)
class Edge:
    id: str
    origin: str
    destination: str
    km: float
    direction: str
    condition: dict
    overrides: dict
    water_type: str
    area: str | None
    marine: bool
    allowed_classes: frozenset
    allowed_pairs: frozenset
    windows: tuple
    speed_limit: float | None


@dataclass(frozen=True)
class Tug:
    id: str
    initial: Location
    available_to: datetime
    draft: float
    speed_upstream_kmh: float
    speed_downstream_kmh: float
    access: VesselAccess


@dataclass(frozen=True)
class Barge:
    id: str
    initial: Location
    available_to: datetime
    capacity_kg: int
    draft_empty: float
    draft_full: float
    handling_tph: float
    cargo_types: tuple[str, ...]
    access: VesselAccess


@dataclass(frozen=True)
class Cargo:
    id: str
    type: str
    origin: str
    destination: str
    kg: int
    ready: datetime
    due: datetime
    priority: int
    critical_category: str | None
    data_status: str


@dataclass(frozen=True)
class Leg:
    edge_id: str
    depart: datetime
    arrive: datetime


@dataclass(frozen=True)
class Voyage:
    tug_id: str
    barge_id: str
    cargo_id: str
    kg: int
    empty: tuple[Leg, ...]
    load_start: datetime
    departure: datetime
    loaded: tuple[Leg, ...]
    unloading_end: datetime
    return_empty: tuple[Leg, ...] = ()

    @property
    def start(self):
        return self.empty[0].depart if self.empty else self.load_start

    @property
    def end(self):
        return self.return_empty[-1].arrive if self.return_empty else self.unloading_end


@dataclass(frozen=True)
class Limits:
    beam_width: int = 12
    candidate_limit: int = 16
    max_actions: int = 60
    max_states: int = 400
    max_route_expansions: int = 150
    max_hops: int = 12
    max_wait_days: int = 14
    route_options: int = 3
    repair_attempts: int = 4
    repair_states: int = 25

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')


@dataclass(frozen=True)
class Scenario:
    id: str
    start: datetime
    end: datetime
    ports: tuple[str, ...]
    edges: dict[str, Edge]
    outgoing: dict[str, tuple[Edge, ...]]
    tugs: dict[str, Tug]
    barges: dict[str, Barge]
    pairs: tuple[tuple[str, str], ...]
    cargo: dict[str, Cargo]
    requirements: tuple[Requirement, ...]
    social_input: dict
    underkeel: float
    fixed_handling_seconds: int
    data_status: str
    allow_marine_reposition: bool

    def get(self, key):
        """Calendar lookup accepted by the existing physical passage check."""
        day, edge_id = key
        edge = self.edges[edge_id]
        return edge.overrides.get(day, edge.condition)

    def initial_locations(self):
        return {v.id: v.initial for v in (*self.tugs.values(), *self.barges.values())}


def _unique(rows, convert):
    values = {}
    for row in rows:
        value = convert(row)
        if value.id in values:
            raise ValueError(f'Duplicate identifier: {value.id}')
        values[value.id] = value
    return values


def _condition(raw):
    closed = raw.get('is_closed', False)
    if type(closed) is not bool:
        raise ValueError('is_closed must be a JSON boolean')
    return {'is_closed': closed,
            'usable_depth_m': number(raw['usable_depth_m'], 'usable_depth_m', nonnegative=True),
            'max_allowed_draft_m': number(raw['max_allowed_draft_m'], 'max_allowed_draft_m', nonnegative=True)}


def parse_scenario(raw):
    start = timestamp(raw['start'], 'start')
    end = timestamp(raw['end'], 'end')
    if end <= start:
        raise ValueError('Scenario end must follow start')
    data_status = raw.get('data_status')
    if data_status not in ('SYNTHETIC', 'REAL', 'MIXED'):
        raise ValueError('Specify SYNTHETIC, REAL or MIXED data_status')
    marine_reposition = raw.get('allow_marine_reposition', True)
    if type(marine_reposition) is not bool:
        raise ValueError('allow_marine_reposition must be boolean')
    if not isinstance(raw['ports'], list):
        raise ValueError('ports must be a list')
    ports = tuple(ident(p, 'port') for p in raw['ports'])
    if not ports or len(set(ports)) != len(ports):
        raise ValueError('Provide unique ports')

    def port(value):
        if value not in ports:
            raise ValueError(f'Unknown port {value!r}')
        return value

    def edge(row):
        provenance(row, data_status)
        origin, destination = port(row['origin_port']), port(row['destination_port'])
        direction = row['direction']
        if origin == destination or direction not in ('upstream', 'downstream'):
            raise ValueError('An edge needs different ports and a supported direction')
        base = _condition(row['condition'])
        overrides = {}
        for day, conditions in row.get('daily_conditions', {}).items():
            if calendar_date.fromisoformat(day).isoformat() != day:
                raise ValueError(f'Expected YYYY-MM-DD condition date: {day}')
            overrides[day] = _condition({**base, **conditions})
        kind = row.get('water_type', 'RIVER')
        if kind not in ('RIVER', 'SEA', 'CANAL', 'ESTUARY'):
            raise ValueError(f'Unknown water_type: {kind}')
        clearance = row.get('sea_clearance_required', False)
        if type(clearance) is not bool:
            raise ValueError('sea_clearance_required must be boolean')
        area = row.get('navigation_area')
        if area is not None:
            ident(area, 'navigation_area')
        classes = row.get('allowed_vessel_classes', [])
        if not isinstance(classes, list):
            raise ValueError('allowed_vessel_classes must be a list')
        classes = frozenset(ident(c, 'allowed vessel class') for c in classes)
        allowed = frozenset((ident(p['tug_id'], 'tug_id'), ident(p['barge_id'], 'barge_id'))
                            for p in row.get('allowed_pairs', []))
        windows = tuple(sorted((timestamp(w['start'], 'window start'), timestamp(w['end'], 'window end'))
                               for w in row.get('navigation_windows', [{'start': raw['start'], 'end': raw['end']}])) )
        if any(a >= b for a, b in windows) or any(b > c for (_, b), (c, _) in zip(windows, windows[1:])):
            raise ValueError('Navigation windows must be non-overlapping and nonempty')
        speed = row.get('speed_limit_kmh')
        return Edge(ident(row['edge_id'], 'edge_id'), origin, destination,
                    number(row['distance_km'], 'distance_km', positive=True), direction, base, overrides,
                    kind, area, kind == 'SEA' or clearance, classes, allowed, windows,
                    number(speed, 'speed_limit_kmh', positive=True) if speed is not None else None)

    def availability(row):
        available = timestamp(row['available_at'], 'available_at')
        expires = timestamp(row['available_to'], 'available_to')
        if expires <= available:
            raise ValueError('available_to must follow available_at')
        return Location(port(row['current_port']), max(available, start)), expires

    def tug(row):
        return Tug(ident(row['vessel_id'], 'vessel_id'), *availability(row),
                   number(row['draft_empty_m'], 'tug draft', nonnegative=True),
                   number(row['speed_upstream_kmh'], 'upstream speed', positive=True),
                   number(row['speed_downstream_kmh'], 'downstream speed', positive=True),
                   vessel_access(row, data_status))

    def barge(row):
        empty = number(row['draft_empty_m'], 'empty draft', nonnegative=True)
        full = number(row['draft_full_m'], 'full draft', nonnegative=True)
        capacity = kilograms(row['capacity_t'])
        categories = row['cargo_types_allowed']
        if full < empty or not capacity or not isinstance(categories, list) or not categories:
            raise ValueError('A barge needs positive capacity, cargo types and full draft ≥ empty draft')
        types = tuple(sorted({ident(t, 'cargo type') for t in categories}))
        return Barge(ident(row['vessel_id'], 'vessel_id'), *availability(row), capacity, empty, full,
                     number(row['handling_tph'], 'handling_tph', positive=True), types, vessel_access(row, data_status))

    def cargo(row):
        origin, destination = port(row['origin_port']), port(row['destination_port'])
        ready = timestamp(row['ready_at'], 'ready_at')
        due = timestamp(row['due_at'], 'due_at')
        priority = row.get('priority', 0)
        if origin == destination or due < ready or type(priority) is not int or priority < 0:
            raise ValueError('Cargo needs different ports, ordered dates and integer nonnegative priority')
        critical = row.get('critical_category')
        if critical is not None:
            ident(critical, 'critical_category')
        status = provenance(row, data_status)
        return Cargo(ident(row['cargo_id'], 'cargo_id'), ident(row['cargo_type'], 'cargo_type'),
                     origin, destination, kilograms(row['volume_t']), ready, due, priority, critical, status)

    edges = _unique(raw['edges'], edge)
    tugs = _unique(raw['tugs'], tug)
    barges = _unique(raw['barges'], barge)
    cargo_rows = _unique(raw['cargo'], cargo)
    if tugs.keys() & barges.keys():
        raise ValueError('Tugs and barges need globally distinct physical IDs')
    pairs = [(p['tug_id'], p['barge_id']) for p in raw['compatible_pairs']]
    if len(set(pairs)) != len(pairs) or any(t not in tugs or b not in barges for t, b in pairs):
        raise ValueError('Invalid or duplicate compatible pair')
    categories = {c.type for c in cargo_rows.values()}
    categories.update(ident(t, 'declared cargo type') for t in raw.get('cargo_types', []))
    social = {'config': {'plans': [{'cargo_type': t} for t in sorted(categories)]},
              'cargo': [{'cargo_type': c.type, 'ready_date': c.ready.isoformat(), 'volume_t': c.kg / 1000}
                        for c in cargo_rows.values()],
              'units': [{'cargo_type_allowed': barges[b].cargo_types} for _, b in pairs]}
    minima = parse_requirements(raw.get('social_requirements'), social)
    fixed = raw.get('fixed_handling_seconds', 0)
    if type(fixed) is not int or fixed < 0:
        raise ValueError('fixed_handling_seconds must be a nonnegative integer')
    ordered_edges = sorted(edges.values(), key=lambda e: e.id)
    outgoing = {p: tuple(e for e in ordered_edges if e.origin == p) for p in ports}
    return Scenario(ident(raw['scenario_id'], 'scenario_id'), start, end, ports, edges, outgoing,
                    tugs, barges, tuple(sorted(pairs)), cargo_rows, minima, social,
                    number(raw.get('underkeel_m', 0), 'underkeel_m', nonnegative=True), fixed,
                    data_status, marine_reposition)


def handling_time(scenario, barge, kg):
    return timedelta(seconds=scenario.fixed_handling_seconds + math.ceil(3.6 * kg / barge.handling_tph))


def sailing_time(edge, tug):
    speed = tug.speed_upstream_kmh if edge.direction == 'upstream' else tug.speed_downstream_kmh
    if edge.speed_limit is not None:
        speed = min(speed, edge.speed_limit)
    return timedelta(seconds=math.ceil(3600 * edge.km / speed))


def loaded_draft(tug, barge, kg):
    return max(tug.draft, barge.draft_empty + (barge.draft_full - barge.draft_empty) * kg / barge.capacity_kg)
