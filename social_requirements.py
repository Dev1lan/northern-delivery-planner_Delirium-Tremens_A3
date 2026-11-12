"""Validate and independently evaluate scenario-defined cargo minima."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from scenario_validation import date, ident


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    cargo_types: tuple[str, ...]
    required_kg: int
    deadline: datetime | None
    basis: str
    source_status: str

    def applies(self, trip):
        return self.deadline is None or datetime.fromisoformat(trip['unloading_end']) <= self.deadline

    def as_dict(self):
        return {
            'requirement_id': self.requirement_id,
            'cargo_types': list(self.cargo_types),
            'required_t': self.required_kg / 1000,
            'deadline': self.deadline.isoformat() if self.deadline is not None else None,
            'basis': self.basis,
            'source_status': self.source_status,
        }


def kilograms(value):
    try:
        kg = Decimal(str(value)) * 1000
    except InvalidOperation as exc:
        raise ValueError('Tonnage must be a finite nonnegative multiple of 0.001 t') from exc
    if not kg.is_finite() or kg < 0 or kg != kg.to_integral_value():
        raise ValueError('Tonnage must be a finite nonnegative multiple of 0.001 t')
    return int(kg)


def parse_requirements(rows, scenario):
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise ValueError('Social requirements must be a JSON list')
    known = {row['cargo_type'] for row in scenario['cargo']}
    known.update(plan['cargo_type'] for plan in scenario['config'].get('plans', []))
    ids, used, requirements = set(), set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each social requirement must be a JSON object')
        identifier = ident(row.get('requirement_id'), 'requirement_id')
        types = row.get('cargo_types')
        if not isinstance(types, list) or not types or any(not isinstance(t, str) for t in types):
            raise ValueError(f'{identifier}: cargo_types must be a nonempty list of category names')
        if set(types) - known:
            raise ValueError(f'{identifier}: unknown cargo categories: {sorted(set(types) - known)}')
        if identifier in ids or len(set(types)) != len(types) or used.intersection(types):
            raise ValueError(f'{identifier}: duplicate requirement or overlapping cargo categories')
        basis = row.get('basis')
        if basis not in ('delivered_by_deadline', 'delivered_total'):
            raise ValueError(f'{identifier}: unsupported basis')
        deadline = row.get('deadline')
        if basis == 'delivered_by_deadline':
            deadline = date(deadline, f'{identifier}.deadline')
        elif deadline is not None:
            raise ValueError(f'{identifier}: deadline must be null for delivered_total')
        requirements.append(Requirement(identifier, tuple(sorted(types)), kilograms(row.get('required_t')),
                                        deadline, basis, ident(row.get('source_status'), 'source_status')))
        ids.add(identifier)
        used.update(types)
    return tuple(sorted(requirements, key=lambda r: r.requirement_id))


def delivered_kg(requirements, trips):
    by_type = {category: r for r in requirements for category in r.cargo_types}
    timely = dict.fromkeys(requirements, 0)
    total = dict.fromkeys(requirements, 0)
    for trip in trips:
        r = by_type.get(trip['cargo_type'])
        if r is not None:
            amount = kilograms(trip['volume_t'])
            total[r] += amount
            if r.applies(trip):
                timely[r] += amount
    return timely, total


def deficits(requirements, trips):
    timely, _ = delivered_kg(requirements, trips)
    return tuple(max(0, r.required_kg - timely[r]) for r in requirements)


def evaluate(requirements, scenario, trips):
    """Evaluate an audited schedule; optimistic demand bounds can prove infeasibility."""
    timely, total = delivered_kg(requirements, trips)
    rows = []
    for r in requirements:
        eligible = [cargo for cargo in scenario['cargo'] if cargo['cargo_type'] in r.cargo_types]
        ready = [cargo for cargo in eligible if r.deadline is None or date(cargo['ready_date'], 'ready_date') <= r.deadline]
        compatible = [cargo for cargo in ready if any(cargo['cargo_type'] in unit['cargo_type_allowed']
                                                     for unit in scenario['units'])]

        def upper_bound(cargo_rows):
            # Round each batch upwards so off-grid legacy inputs remain an optimistic bound.
            return sum(int((Decimal(str(c['volume_t'])) * 1000).to_integral_value(rounding=ROUND_CEILING))
                       for c in cargo_rows)

        missing = max(0, r.required_kg - timely[r])
        unavoidable = max(0, r.required_kg - upper_bound(compatible)) if missing else 0
        reason = None
        if missing:
            if upper_bound(eligible) < r.required_kg:
                reason = 'INSUFFICIENT_DEMAND'
            elif upper_bound(ready) < r.required_kg:
                reason = 'INSUFFICIENT_DEMAND_READY_BY_DEADLINE'
            elif unavoidable:
                reason = 'INSUFFICIENT_DEMAND_WITH_COMPATIBLE_FLEET'
            else:
                reason = 'MINIMUM_NOT_MET_BY_SCHEDULE'
        rows.append({**r.as_dict(), 'delivered_by_deadline_t': timely[r] / 1000 if r.deadline else None,
                     'delivered_total_t': total[r] / 1000, 'deficit_t': missing / 1000,
                     'deficit_lower_bound_t': unavoidable / 1000, 'satisfied': not missing, 'reason': reason})
    if not rows:
        status, feasibility = 'NOT_CONFIGURED', 'NOT_CHECKED'
    elif all(r['satisfied'] for r in rows):
        status, feasibility = 'SATISFIED', 'FEASIBLE'
    else:
        status = 'NOT_SATISFIED'
        feasibility = 'INFEASIBLE' if any(r['deficit_lower_bound_t'] for r in rows) else 'UNKNOWN'
    return {'requirements_status': status, 'feasibility_status': feasibility, 'requirements': rows}
