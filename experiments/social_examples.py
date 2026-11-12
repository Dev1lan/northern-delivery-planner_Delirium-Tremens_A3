"""Fixed synthetic fixtures for the social experiment, defined before running it."""

from copy import deepcopy

from experiments.tiny_exact import setup


def examples():
    base = setup('deadlines')
    base['cargo'] = [dict(base['cargo'][0], cargo_type='food', volume_t=0.003)]
    base['units'][0].update(capacity_t=0.003, reference_capacity_t=0.003, cargo_type_allowed=['coal', 'food'])

    def case(name, tonnes, deadline):
        scenario = deepcopy(base)
        scenario['id'] = name
        requirements = [{'requirement_id': 'FOOD', 'cargo_types': ['food'], 'required_t': tonnes,
                         'deadline': f'2026-06-10T{deadline}:00:00', 'basis': 'delivered_by_deadline',
                         'source_status': 'SCENARIO_ASSUMPTION'}]
        return scenario, requirements

    yield case('normal', 0.003, '08')
    conflict, requirements = case('food_tradeoff', 0.001, '03')
    conflict['cargo'] = [dict(conflict['cargo'][0], cargo_id='A_COAL', cargo_type='coal'),
                         dict(conflict['cargo'][0], cargo_id='Z_FOOD', due_date='2026-06-10T07:00:00', volume_t=0.001)]
    conflict['units'][0]['available_to'] = '2026-06-10T08:00:00'
    yield conflict, requirements
    no_fleet, requirements = case('resource_shortage', 0.001, '03')
    no_fleet['units'] = []
    yield no_fleet, requirements
    yield case('proven_infeasible', 0.004, '08')
    yield case('unknown', 0.002, '04')
