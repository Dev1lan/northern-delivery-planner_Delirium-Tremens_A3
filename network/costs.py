"""Itemized external tariffs used by both pricing and bounded cost search."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from network.metrics import seconds
from scenario_validation import ident


ZERO = Decimal(0)
HOUR = Decimal(3600)
TONNE = Decimal(1000)
UNITS = {
    'loaded_fuel': 'fuel_basis', 'empty_fuel': 'fuel_basis',
    'tug_operation': 'hour', 'barge_operation': 'hour',
    'loading': 'tonne', 'unloading': 'tonne', 'port_call': 'call',
    'waiting': 'hour', 'repositioning': 'km',
}


def rate(row, unit):
    ident(row.get('source'), 'tariff source')
    if row.get('source_status') not in ('SYNTHETIC', 'REAL', 'ASSUMED'):
        raise ValueError('Tariff source_status must be SYNTHETIC, REAL or ASSUMED')
    if row.get('unit') != unit:
        raise ValueError(f'Tariff unit must be {unit}')
    try:
        amount = Decimal(str(row['value']))
    except (InvalidOperation, KeyError) as exc:
        raise ValueError('Invalid tariff value') from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError('Tariff must be finite and nonnegative')
    return amount


def money(amount):
    return str(amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


class CostModel:
    def __init__(self, scenario, tariffs):
        self.scenario = scenario
        self.tariffs = tariffs
        self.currency = ident(tariffs.get('currency'), 'currency')
        self.fuel_basis = tariffs.get('fuel_basis')
        if self.fuel_basis not in ('km', 'hour'):
            raise ValueError('fuel_basis must be km or hour')
        self.units = {k: self.fuel_basis if v == 'fuel_basis' else v for k, v in UNITS.items()}
        rows = tariffs.get('rates', {})
        if set(rows) != set(self.units):
            raise ValueError(f'Missing tariffs: {sorted(self.units.keys() - rows.keys())}; '
                             f'unknown tariffs: {sorted(rows.keys() - self.units.keys())}')
        self.rates = {k: rate(v, self.units[k]) for k, v in rows.items()}
        self.vessel_rates = self._overrides(tariffs.get('by_vessel', {}), scenario.tugs.keys() | scenario.barges.keys(),
                                            {'tug_operation', 'barge_operation', 'loaded_fuel', 'empty_fuel'})
        self.port_rates = self._overrides(tariffs.get('by_port', {}), scenario.ports,
                                          {'loading', 'unloading', 'port_call', 'waiting'})
        self.alternative = {k: rate(v, 'tonne') for k, v in tariffs.get('alternative_delivery', {}).items()}
        inclusions = tariffs.get('tug_operation_includes_fuel', False)
        if type(inclusions) is not bool:
            raise ValueError('tug_operation_includes_fuel must be boolean')
        if inclusions and any(r.get(k, ZERO) for r in [self.rates, *self.vessel_rates.values()]
                              for k in ('loaded_fuel', 'empty_fuel')):
            raise ValueError('Fuel would be counted twice')

    def _overrides(self, rows, identifiers, allowed):
        if set(rows) - set(identifiers) or any(set(rates) - allowed for rates in rows.values()):
            raise ValueError('Unknown vessel, port or tariff override')
        return {key: {k: rate(v, self.units[k]) for k, v in overrides.items()} for key, overrides in rows.items()}

    def _distance(self, legs):
        return sum((Decimal(str(self.scenario.edges[l.edge_id].km)) for l in legs), ZERO)

    @staticmethod
    def _sailing_seconds(legs):
        return sum(seconds(l.depart, l.arrive) for l in legs)

    def charges(self, move):
        cargo = self.scenario.cargo[move.cargo_id]
        loaded, empty = move.loaded, (*move.empty, *move.return_empty)
        occupied = seconds(move.start, move.end)
        handling = seconds(move.load_start, move.departure) + seconds(move.unload_start, move.unloading_end)
        underway = self._sailing_seconds((*loaded, *empty))
        if self.fuel_basis == 'km':
            loaded_fuel, empty_fuel = self._distance(loaded), self._distance(empty)
        else:
            loaded_fuel = Decimal(self._sailing_seconds(loaded)) / HOUR
            empty_fuel = Decimal(self._sailing_seconds(empty)) / HOUR
        t = Decimal(move.kg) / TONNE
        queued_at_load = seconds(move.port_ready_at, move.load_start) if move.port_ready_at else 0
        queued_at_unload = seconds(move.loaded[-1].arrive, move.unload_start)
        other_wait = occupied - handling - underway - queued_at_load - queued_at_unload
        rows = [
            ('loaded_fuel', loaded_fuel, move.tug_id, None),
            ('empty_fuel', empty_fuel, move.tug_id, None),
            ('tug_operation', Decimal(occupied) / HOUR, move.tug_id, None),
            ('barge_operation', Decimal(occupied) / HOUR, move.barge_id, None),
            ('loading', t, move.barge_id, cargo.origin), ('unloading', t, move.barge_id, cargo.destination),
            ('port_call', Decimal(1), move.barge_id, cargo.origin),
            ('port_call', Decimal(1), move.barge_id, cargo.destination),
            ('waiting', Decimal(queued_at_load) / HOUR, move.barge_id, cargo.origin),
            ('waiting', Decimal(queued_at_unload) / HOUR, move.barge_id, cargo.destination),
            ('waiting', Decimal(other_wait) / HOUR, move.barge_id, None),
            ('repositioning', self._distance(move.empty), move.tug_id, None),
        ]
        return [(key, amount * self.port_rates.get(port, {}).get(
            key, self.vessel_rates.get(vessel, {}).get(key, self.rates[key])), vessel, port)
                for key, amount, vessel, port in rows]

    def voyage_cost(self, move):
        return sum((cost for _, cost, _, _ in self.charges(move)), ZERO)

    def residual(self, remaining):
        penalty, unpriced = ZERO, 0
        for key, kg in remaining.items():
            tariff = self.alternative.get(self.scenario.cargo[key].type)
            if tariff is None:
                unpriced += kg
            else:
                penalty += Decimal(kg) / TONNE * tariff
        return penalty, unpriced

    def breakdown(self, moves):
        by_item, by_vessel, by_port = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
        remaining = {c.id: c.kg for c in self.scenario.cargo.values()}
        voyages = []
        timely = delivered = 0
        for i, move in enumerate(moves, 1):
            charge = defaultdict(Decimal)
            for item, amount, vessel, port in self.charges(move):
                charge[item] += amount
                by_item[item] += amount
                by_vessel[vessel] += amount
                if port is not None:
                    by_port[port] += amount
            voyages.append({'voyage_id': i, 'total_cost': money(sum(charge.values(), ZERO)),
                            'cost_by_item': {k: money(v) for k, v in charge.items()}})
            remaining[move.cargo_id] -= move.kg
            delivered += move.kg
            if move.unloading_end <= self.scenario.cargo[move.cargo_id].due:
                timely += move.kg
        penalty, unpriced = self.residual(remaining)
        by_item['alternative_delivery'] = penalty
        total = sum(by_item.values(), ZERO)
        return {'currency': self.currency, 'source_status': self.tariffs.get('source_status', 'ASSUMED'),
                'rates': self.tariffs['rates'], 'tariff_parameters': self.tariffs,
                'cost_by_unit': {k: money(v) for k, v in by_item.items()},
                'cost_by_voyage': voyages, 'cost_by_vessel': {k: money(v) for k, v in by_vessel.items()},
                'cost_by_port': {k: money(v) for k, v in by_port.items()}, 'total_cost': money(total),
                'cost_per_t': money(total * TONNE / delivered) if delivered else None,
                'cost_per_t_on_time': money(total * TONNE / timely) if timely else None,
                'undelivered_penalty': money(penalty), 'unpriced_undelivered_t': unpriced / 1000,
                'empty_transition_cost': money(by_item.get('empty_fuel', ZERO) + by_item.get('repositioning', ZERO)),
                'price_status': 'PARTIAL' if unpriced else 'PRICED'}
