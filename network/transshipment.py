"""Explicit transfer terminals and the custody rules of a two-stage shipment."""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from itertools import groupby

from network.access import provenance
from scenario_validation import ident, number
from social_requirements import kilograms


@dataclass(frozen=True)
class Terminal:
    cargo_types: frozenset
    directions: frozenset
    handling_seconds: int
    capacity_kg: int | None
    rate_per_t: Decimal | None


def parse_terminals(raw, ports, berths, data_status):
    terminals = {}
    for port, row in raw.items():
        if port not in ports or not berths.get(port):
            raise ValueError(f'Transshipment requires explicit berths at {port}')
        provenance(row, data_status)
        directions = frozenset(row['directions'])
        if not directions or directions - {'RIVER_TO_SEA', 'SEA_TO_RIVER'}:
            raise ValueError(f'Invalid transfer directions at {port}')
        types = frozenset(ident(value, 'transfer cargo type') for value in row['cargo_types'])
        delay = row.get('handling_seconds', 0)
        if not types or type(delay) is not int or delay < 0:
            raise ValueError(f'Invalid transfer cargo types or handling time at {port}')
        capacity = kilograms(row['storage_capacity_t']) if 'storage_capacity_t' in row else None
        if capacity is None and row.get('storage_assumption') != 'UNLIMITED':
            raise ValueError(f'Explicit storage capacity or UNLIMITED assumption required at {port}')
        tariff = row.get('transfer_cost_per_t')
        if tariff is not None:
            number(tariff, 'transfer_cost_per_t', nonnegative=True)
            tariff = Decimal(str(tariff))
        terminals[port] = Terminal(types, directions, delay, capacity, tariff)
    return terminals


def leg_cargo(scenario, move, ready=None):
    cargo = scenario.cargo[move.cargo_id]
    if move.stage == 1:
        return replace(cargo, destination=move.transfer_port)
    if move.stage == 2:
        return replace(cargo, origin=move.transfer_port, ready=ready or cargo.ready)
    return cargo


def transfer_ready(scenario, first):
    return first.unloading_end + timedelta(seconds=scenario.terminals[first.transfer_port].handling_seconds)


def direction(scenario, first, second):
    modes = [any(scenario.edges.get(leg.edge_id) and scenario.edges[leg.edge_id].marine
                 for leg in move.loaded) for move in (first, second)]
    return {(False, True): 'RIVER_TO_SEA', (True, False): 'SEA_TO_RIVER'}.get(tuple(modes))


def verify_batches(scenario, voyages, violation):
    batches, storage = {}, {}
    for index, move in enumerate(voyages, 1):
        if move.batch_id is None and move.stage is None and move.transfer_port is None:
            continue
        if (not isinstance(move.batch_id, str) or not move.batch_id or type(move.stage) is not int or move.stage not in (1, 2)
                or move.cargo_id not in scenario.cargo or move.transfer_port not in scenario.terminals):
            raise violation(index, 'INVALID_TRANSFER_STAGE', str(move.batch_id))
        batch = batches.setdefault(move.batch_id, {})
        if move.stage in batch:
            raise violation(index, 'DUPLICATE_TRANSFER_STAGE', move.batch_id)
        batch[move.stage] = (index, move)
    ready = {}
    for batch_id, batch in batches.items():
        if batch.keys() != {1, 2}:
            raise violation(next(iter(batch.values()))[0], 'INCOMPLETE_TRANSFER_BATCH', batch_id)
        index, first = batch[1]
        _, second = batch[2]
        cargo = scenario.cargo[first.cargo_id]
        port = first.transfer_port
        terminal = scenario.terminals[port]
        if (first.cargo_id != second.cargo_id or port != second.transfer_port
                or first.kg != second.kg or port in (cargo.origin, cargo.destination)
                or cargo.type not in terminal.cargo_types
                or direction(scenario, first, second) not in terminal.directions
                or (first.tug_id, first.barge_id) == (second.tug_id, second.barge_id)):
            raise violation(index, 'INVALID_TRANSFER_CHAIN', batch_id)
        ready[batch_id] = transfer_ready(scenario, first)
        if second.load_start < ready[batch_id]:
            raise violation(index, 'TRANSFER_BEFORE_READY', batch_id)
        if terminal.capacity_kg is not None:
            storage.setdefault(port, []).extend([(first.unloading_end, first.kg, index),
                                                 (second.departure, -second.kg, index)])
    for port, events in storage.items():
        occupied = 0
        for _, entries in groupby(sorted(events), key=lambda item: item[0]):
            changes = list(entries)
            occupied += sum(change for _, change, _ in changes)
            if occupied > scenario.terminals[port].capacity_kg:
                raise violation(changes[0][2], 'TRANSFER_STORAGE_CAPACITY', port)
    return ready
