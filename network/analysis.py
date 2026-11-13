"""Audited scenario comparisons, observed bottlenecks and state queries."""

from copy import deepcopy
from decimal import Decimal

from network.access import admission_error
from network.adapter import audit_result, run_scenario
from network.models import parse_scenario, sailing_time, timestamp
from network.ports import port_report
from network.scheduler import REPOSITION
from result_adapter import fingerprint


def checked(result):
    if audit_result(result)['schedule_audit']['status'] != 'PASSED':
        raise ValueError('Result failed independent audit')


def positions_at(result, moment):
    instant = timestamp(moment, 'moment')
    scenario = parse_scenario(result['scenario'])
    if not scenario.start <= instant <= scenario.end:
        raise ValueError('Moment must be inside the scenario')
    locations = {}
    for key, vessel in {**scenario.tugs, **scenario.barges}.items():
        available = vessel.initial.available_at <= instant < vessel.available_to and instant < scenario.end
        locations[key] = {'vessel_id': key, 'port': vessel.initial.port, 'state': 'IDLE' if available else 'UNAVAILABLE',
                          'edge_id': None, 'progress': None}
    for event in result['events']:
        a, b = timestamp(event['start'], 'start'), timestamp(event['end'], 'end')
        if instant < a:
            break
        for key in (event['tug_id'], event['barge_id']):
            if instant >= b:
                locations[key]['port'] = event['destination_port']
            else:
                edge = event['edge_id']
                locations[key].update(state=event['state'], port=None if edge else event['origin_port'],
                                      edge_id=edge, progress=(instant-a)/(b-a) if edge else None)
    return locations


def port_at(result, port, moment):
    checked(result)
    scenario = parse_scenario(result['scenario'])
    if port not in scenario.ports:
        raise ValueError(f'Unknown port: {port}')
    instant = timestamp(moment, 'moment')
    vessels = [v for v in positions_at(result, moment).values() if v['port'] == port]
    operations = {e['berth_id']: e for e in result['events'] if e.get('berth_id')
                  and timestamp(e['start'], 'start') <= instant < timestamp(e['end'], 'end')}
    berths = []
    for berth in scenario.berths.get(port, ()):
        operation = operations.get(berth.id)
        external_end = next((b for a, b in berth.occupied if a <= instant < b), None)
        status = 'OCCUPIED' if operation or external_end else (
            'OPEN' if any(a <= instant < b for a, b in berth.windows) else 'CLOSED')
        berths.append({'berth_id': berth.id, 'status': status, 'operation': operation,
                       'expected_release': operation['end'] if operation else external_end.isoformat() if external_end else None})
    return {'port': port, 'time': moment, 'vessels': vessels,
            'queued_tugs': [v['vessel_id'] for v in vessels if v['state'] == 'PORT_QUEUE' and v['vessel_id'] in scenario.tugs],
            'berths': berths, 'capacity_modelled': port in scenario.berths}


def port_load(result, port, start, end):
    checked(result)
    scenario = parse_scenario(result['scenario'])
    if port not in scenario.ports:
        raise ValueError(f'Unknown port: {port}')
    return port_report(scenario, result['events'], timestamp(start, 'start'), timestamp(end, 'end')).get(port)


def _reachable(scenario, start, end, allows=None):
    visited = {start}
    queue = [start]
    while queue:
        port = queue.pop()
        if port == end:
            return True
        for edge in scenario.outgoing[port]:
            if edge.destination not in visited and (allows is None or allows(edge)):
                visited.add(edge.destination)
                queue.append(edge.destination)
    return False


def diagnose(result):
    checked(result)
    scenario = parse_scenario(result['scenario'])
    late = {}
    expired = {c['cargo_id']: c['expired_t'] for c in result.get('cargo_condition', [])}
    for voyage in result['financial_inputs']['voyages']:
        if voyage['late_seconds']:
            key = voyage['cargo_id']
            late[key] = late.get(key, 0) + voyage['volume_t']
    rows = []
    for item in result['cargo_status']:
        key = item['cargo_id']
        if not item['remaining_t'] and not late.get(key):
            continue
        cargo = scenario.cargo[key]
        evidence = []
        connected = _reachable(scenario, cargo.origin, cargo.destination)
        pairs = [(scenario.tugs[t], scenario.barges[b]) for t, b in scenario.pairs
                 if cargo.type in scenario.barges[b].cargo_types]
        if not connected:
            evidence.append({'kind': 'PROVEN', 'code': 'NO_DIRECTED_PATH'})
        if not pairs:
            evidence.append({'kind': 'PROVEN', 'code': 'NO_COMPATIBLE_PAIR'})
        if connected and pairs:
            def path_exists(tug, barge, *, check_windows):
                def allows(edge):
                    if admission_error(scenario, edge, tug, barge):
                        return False
                    if not check_windows:
                        return True
                    cutoff = min(scenario.end, tug.available_to, barge.available_to)
                    return any(max(a, scenario.start, cargo.ready) + sailing_time(edge, tug) <= min(b, cutoff)
                               for a, b in edge.windows)
                return _reachable(scenario, cargo.origin, cargo.destination, allows)

            if not any(path_exists(t, b, check_windows=False) for t, b in pairs):
                evidence.append({'kind': 'PROVEN', 'code': 'NO_ADMITTED_PATH'})
            elif not any(path_exists(t, b, check_windows=True) for t, b in pairs):
                evidence.append({'kind': 'PROVEN', 'code': 'NO_PATH_WITH_NAVIGATION_WINDOWS'})
        if cargo.ready >= scenario.end:
            evidence.append({'kind': 'PROVEN', 'code': 'READY_AFTER_SEASON'})
        if cargo.expires_at is not None and cargo.ready >= cargo.expires_at:
            evidence.append({'kind': 'PROVEN', 'code': 'EXPIRED_BEFORE_DISPATCH'})
        if expired.get(key):
            evidence.append({'kind': 'OBSERVED', 'code': 'EXPIRED_BY_UNLOADING', 'expired_t': expired[key]})
        if late.get(key):
            evidence.append({'kind': 'OBSERVED', 'code': 'LATE_UNLOADING', 'late_t': round(late[key], 3)})
        if item['remaining_t'] and not any(e['kind'] == 'PROVEN' for e in evidence):
            evidence.append({'kind': 'UNDETERMINED', 'code': 'RESIDUAL_WITHOUT_PROVEN_CAUSE'})
        rows.append({**item, 'late_t': round(late.get(key, 0), 3), 'evidence': evidence})
    queues = [{'port': port, 'seconds': info['waiting_seconds'], 'kind': 'OBSERVED'}
              for port, info in result.get('port_operations', {}).items() if info['waiting_seconds']]
    return {'cargo': rows, 'queues': queues}


def explain_voyage(result, voyage_id):
    checked(result)
    index = int(voyage_id) - 1
    if not 0 <= index < len(result['voyages']):
        raise ValueError('Unknown voyage')
    move = result['voyages'][index]
    metrics = result['financial_inputs']['voyages'][index]
    events = [e for e in result['events'] if e['voyage_id'] == index + 1]
    scenario = parse_scenario(result['scenario'])
    cargo = scenario.cargo[move['cargo_id']]
    return {'voyage_id': index + 1, 'tug_id': move['tug_id'], 'barge_id': move['barge_id'],
            'departure_port': events[0]['origin_port'], 'pickup': metrics['origin_port'],
            'destination': metrics['destination_port'], 'loaded': move['loaded'], 'empty': move['empty'],
            'unloading_end': move['unloading_end'], 'due_at': cargo.due.isoformat(), 'on_time': metrics['on_time'],
            'checked_constraints': ['resource_availability', 'cargo_compatibility', 'draft_and_navigation_windows',
                                    'marine_admission_if_applicable', 'berth_reservations_if_modelled'],
            'audit': 'PASSED', 'cost': next((r for r in (result['financial_evaluation'] or {}).get('cost_by_voyage', [])
                                           if r['voyage_id'] == index + 1), None),
            'plan_level_comparison': result.get('reposition_comparison'),
            'voyage_level_alternatives_calculated': False}


def change_scenario(raw, option):
    scenario = deepcopy(raw)
    kind = option['kind']
    if kind == 'marine_reposition':
        if type(option['enabled']) is not bool:
            raise ValueError('enabled must be boolean')
        scenario['allow_marine_reposition'] = option['enabled']
    elif kind == 'unlimited_port':
        del scenario['port_operations'][option['port']]
    elif kind == 'add_berth':
        scenario['port_operations'][option['port']]['berths'].append(deepcopy(option['berth']))
    elif kind in ('close_berth', 'handling_tph'):
        berths = scenario['port_operations'][option['port']]['berths']
        berth = next(b for b in berths if b['berth_id'] == option['berth_id'])
        if kind == 'close_berth':
            berth['working_windows'] = []
        else:
            berth['handling_tph'] = deepcopy(option['value'])
    elif kind == 'close_edge':
        edge = next(e for e in scenario['edges'] if e['edge_id'] == option['edge_id'])
        edge['navigation_windows'] = []
    elif kind == 'cargo':
        cargo = next(c for c in scenario['cargo'] if c['cargo_id'] == option['cargo_id'])
        for field in ('volume_t', 'due_at'):
            if field in option:
                cargo[field] = option[field]
    elif kind in ('move_vessel', 'remove_vessel'):
        fleet_key = next(k for k in ('tugs', 'barges') if any(v['vessel_id'] == option['vessel_id'] for v in scenario[k]))
        vessel = next(v for v in scenario[fleet_key] if v['vessel_id'] == option['vessel_id'])
        if kind == 'move_vessel':
            vessel['current_port'] = option['port']
        else:
            scenario[fleet_key].remove(vessel)
            scenario['compatible_pairs'] = [pair for pair in scenario['compatible_pairs']
                                            if option['vessel_id'] not in (pair['tug_id'], pair['barge_id'])]
    elif kind == 'add_vessel':
        if option['fleet'] not in ('tugs', 'barges'):
            raise ValueError('Unknown fleet')
        scenario[option['fleet']].append(deepcopy(option['vessel']))
        scenario['compatible_pairs'].extend(deepcopy(option.get('compatible_pairs', [])))
    else:
        raise ValueError(f'Unsupported change: {kind}')
    return scenario


def compare_results(base, alternative):
    keys = ('delivered_on_time_t', 'delivered_total_t', 'critical_on_time_t', 'social_deficit_t',
            'empty_km', 'undelivered_t', 'completed_voyages', 'resource_busy_hours')
    delta = {k: round(alternative['metrics'][k] - base['metrics'][k], 6) for k in keys}
    delta['port_waiting_seconds'] = sum(p['waiting_seconds'] for p in alternative.get('port_operations', {}).values()) - sum(
        p['waiting_seconds'] for p in base.get('port_operations', {}).values())
    a, b = base['financial_evaluation'], alternative['financial_evaluation']
    delta['satisfied_requirements'] = sum(r['satisfied'] for r in alternative['requirements']) - sum(
        r['satisfied'] for r in base['requirements'])
    delta['cost'] = None
    delta['cost_per_t_on_time'] = None
    if (a and b and a.get('price_status') == b.get('price_status') == 'PRICED'
            and a.get('cost_basis') == b.get('cost_basis')
            and a['currency'] == b['currency'] and a.get('tariff_parameters') == b.get('tariff_parameters')):
        delta['cost'] = str(Decimal(b['total_cost']) - Decimal(a['total_cost']))
        if a.get('cost_per_t_on_time') is not None and b.get('cost_per_t_on_time') is not None:
            delta['cost_per_t_on_time'] = str(Decimal(b['cost_per_t_on_time']) - Decimal(a['cost_per_t_on_time']))
    return delta


def find_improvements(raw, baseline_result, options, *, strategy=REPOSITION, limits=None, tariffs=None):
    checked(baseline_result)
    if baseline_result['input_fingerprint'] != fingerprint(raw):
        raise ValueError('Baseline input differs')
    alternatives = []
    for option in options:
        modified = change_scenario(raw, option)
        result = run_scenario(modified, strategy, limits=limits, tariffs=tariffs)
        checked(result)
        alternatives.append({'change': deepcopy(option), 'delta': compare_results(baseline_result, result),
                             'diagnosis': diagnose(result), 'result': result})

    use_cost = bool(alternatives) and all(a['delta']['cost'] is not None for a in alternatives)

    def quality(result):
        metrics = result['metrics']
        return (-sum(not r['satisfied'] for r in result['requirements']), -metrics['social_deficit_t'],
                metrics['delivered_on_time_t'], metrics['delivered_total_t'],
                -Decimal(result['financial_evaluation']['total_cost']) if use_cost else 0,
                -metrics['empty_km'])

    alternatives.sort(key=lambda item: quality(item['result']), reverse=True)
    for item in alternatives:
        item['improved'] = quality(item['result']) > quality(baseline_result)
        item['explanation'] = {
            'physical_audit': 'PASSED', 'delta': item['delta'], 'unmet_requirements': [
                r['requirement_id'] for r in item['result']['requirements'] if not r['satisfied']],
            'comparison_scope': 'Difference between audited scenarios under the supplied change',
        }
    return {'baseline_fingerprint': baseline_result['input_fingerprint'],
            'improvement_found': any(a['improved'] for a in alternatives),
            'alternatives': alternatives}
