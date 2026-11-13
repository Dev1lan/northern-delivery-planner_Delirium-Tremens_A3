"""Version 3 JSON interface for the independent network planner."""

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter
import sys

from network import VERSION
from network.financial import evaluate_schedule
from network.metrics import cargo_condition, event_rows, summarize
from network.models import Leg, Limits, Voyage, parse_scenario, timestamp
from network.ports import port_report
from network.search_policy import SearchPolicy
from network.scheduler import COST, REPOSITION, RETURN_EMPTY, STRATEGIES, Scheduler
from network.validator import audit, replay, requirement_report
from result_adapter import fingerprint
from social_requirements import kilograms


def encode_voyage(move):
    def legs(items):
        return [{'edge_id': leg.edge_id, 'depart': leg.depart.isoformat(), 'arrive': leg.arrive.isoformat()}
                for leg in items]

    row = {'tug_id': move.tug_id, 'barge_id': move.barge_id, 'cargo_id': move.cargo_id,
            'volume_t': move.kg / 1000, 'empty': legs(move.empty), 'load_start': move.load_start.isoformat(),
            'departure': move.departure.isoformat(), 'loaded': legs(move.loaded),
            'unloading_end': move.unloading_end.isoformat(), 'return_empty': legs(move.return_empty)}
    if move.loading_berth is not None:
        row.update(loading_berth=move.loading_berth, port_ready_at=move.port_ready_at.isoformat())
    if move.unloading_berth is not None:
        row.update(unloading_berth=move.unloading_berth, unloading_start=move.unload_start.isoformat())
    if move.batch_id is not None:
        row.update(batch_id=move.batch_id, stage=move.stage, transfer_port=move.transfer_port)
    return row


def decode_voyages(rows):
    def legs(items):
        return tuple(Leg(row['edge_id'], timestamp(row['depart'], 'depart'), timestamp(row['arrive'], 'arrive'))
                     for row in items)

    return tuple(Voyage(row['tug_id'], row['barge_id'], row['cargo_id'], kilograms(row['volume_t']),
                        legs(row['empty']), timestamp(row['load_start'], 'load_start'),
                        timestamp(row['departure'], 'departure'), legs(row['loaded']),
                        timestamp(row['unloading_end'], 'unloading_end'), legs(row['return_empty']),
                        row.get('loading_berth'), row.get('unloading_berth'),
                        timestamp(row['port_ready_at'], 'port_ready_at') if 'port_ready_at' in row else None,
                        timestamp(row['unloading_start'], 'unloading_start') if 'unloading_start' in row else None,
                        row.get('batch_id'), row.get('stage'), row.get('transfer_port'))
                 for row in rows)


def _transshipment_summary(scenario, moves):
    batches = {}
    for index, move in enumerate(moves, 1):
        if move.batch_id is not None:
            batches.setdefault(move.batch_id, {})[move.stage] = (index, move)
    rows = []
    for batch_id, stages in batches.items():
        first_id, first = stages[1]
        second_id, second = stages[2]
        cargo = scenario.cargo[second.cargo_id]
        rows.append({
            'batch_id': batch_id, 'cargo_id': first.cargo_id, 'transfer_port': first.transfer_port,
            'volume_t': first.kg / 1000, 'inbound_voyage_id': first_id, 'outbound_voyage_id': second_id,
            'arrived_at': first.unloading_end.isoformat(), 'loading_at': second.load_start.isoformat(),
            'stored_until': second.departure.isoformat(), 'delivered_at': second.unloading_end.isoformat(),
            'cargo_state': 'DELIVERED' if cargo.usable_at(second.unloading_end) else 'EXPIRED',
        })
    return rows


def _events(scenario, moves):
    return sorted((event for index, move in enumerate(moves, 1) for event in event_rows(scenario, move, index)),
                  key=lambda row: (row['start'], row['voyage_id']))


def _resources(scenario, locations):
    vessels = {**scenario.tugs, **scenario.barges}
    return [{'vessel_id': vessel, 'current_port': location.port, 'available_at': location.available_at.isoformat(),
             'available_to': vessels[vessel].available_to.isoformat(),
             'status': 'IDLE' if location.available_at < min(scenario.end, vessels[vessel].available_to)
             else 'UNAVAILABLE'} for vessel, location in sorted(locations.items())]


def build_result(raw, scenario, plan, limits, tariffs=None):
    moves = tuple(sorted(plan.moves, key=lambda move: (move.start, move.tug_id, move.barge_id, move.cargo_id)))
    policy = {'allow_reposition': plan.strategy in (REPOSITION, COST), 'allow_return': plan.strategy == RETURN_EMPTY}
    state = replay(scenario, moves, **policy)
    report = {'schedule_audit': {'status': 'PASSED', 'violations': []}, **requirement_report(scenario, moves)}
    metrics, financial_inputs, cargo_status = summarize(scenario, moves, report)
    final_resources = _resources(scenario, state.locations)
    result = {'schema_version': VERSION, 'algorithm_version': VERSION, 'scenario_id': scenario.id,
              'data_status': scenario.data_status, 'strategy': plan.strategy, 'scenario': deepcopy(raw),
              'input_fingerprint': fingerprint(raw), 'parameters': asdict(limits),
              'termination_reason': plan.termination_reason, 'expanded_states': plan.expanded_states,
              **report, 'metrics': metrics, 'financial_inputs': financial_inputs, 'cargo_status': cargo_status,
              'voyages': [encode_voyage(move) for move in moves], 'final_resources': final_resources,
              'events': _events(scenario, moves),
              'reposition_comparison': None, 'financial_evaluation': None}
    if scenario.terminals:
        result['transshipment_batches'] = _transshipment_summary(scenario, moves)
    if scenario.berths:
        result['port_operations'] = port_report(scenario, result['events'])
    if any(c.expires_at is not None for c in scenario.cargo.values()):
        result['cargo_condition'] = cargo_condition(scenario, moves)
    if plan.cost_service_floor is not None:
        timely, total = plan.cost_service_floor
        result['cost_service_floor'] = {'on_time_t': timely / 1000, 'total_t': total / 1000}
    if plan.counterfactual is not None:
        replay(scenario, plan.counterfactual, allow_reposition=False)
        baseline_report = requirement_report(scenario, plan.counterfactual)
        baseline, _, _ = summarize(scenario, plan.counterfactual, baseline_report)
        delta_on_time = round(metrics['delivered_on_time_t'] - baseline['delivered_on_time_t'], 3)
        delta_total = round(metrics['delivered_total_t'] - baseline['delivered_total_t'], 3)
        delta_deficit = round(metrics['social_deficit_t'] - baseline['social_deficit_t'], 3)
        minima_before = sum(not r['satisfied'] for r in baseline_report['requirements'])
        minima_after = sum(not r['satisfied'] for r in report['requirements'])
        reason = 'NO_IMPROVING_REPOSITION_FOUND'
        if plan.reposition_selected:
            if (minima_after, delta_deficit) < (minima_before, 0):
                reason = 'IMPROVES_SOCIAL_MINIMUMS'
            elif delta_on_time > 0:
                reason = 'IMPROVES_ON_TIME_TONNES'
            else:
                reason = 'IMPROVES_TOTAL_TONNES'
        result['reposition_comparison'] = {'baseline_strategy': 'NETWORK_SOCIAL',
                                           'selected_repositioning': plan.reposition_selected,
                                           'decision_reason': reason,
                                           'baseline_metrics': baseline,
                                           'delta_on_time_t': delta_on_time, 'delta_total_t': delta_total,
                                           'delta_social_deficit_t': delta_deficit}
    if tariffs is not None:
        result['financial_evaluation'] = evaluate_schedule(result, tariffs)
    return result


def run_scenario(raw, strategy=REPOSITION, *, limits=None, tariffs=None, search_policy=None, initial_plan=None):
    started = perf_counter()
    limits = limits or Limits()
    scenario = parse_scenario(raw)
    model = None
    if strategy == COST and tariffs is not None and tariffs.get('tariff_version') == 2:
        from network.costs import CostModel
        model = CostModel(scenario, tariffs)
    moves = None
    if initial_plan is not None:
        if strategy != COST or initial_plan.get('strategy') not in (REPOSITION, COST):
            raise ValueError('An initial plan is supported only for NETWORK_COST from REPOSITION or COST')
        if initial_plan.get('input_fingerprint') != fingerprint(raw):
            raise ValueError('Initial plan scenario differs')
        if audit_result(initial_plan)['schedule_audit']['status'] != 'PASSED':
            raise ValueError('Initial plan failed independent audit')
        moves = decode_voyages(initial_plan['voyages'])
        if any(not scenario.cargo[move.cargo_id].usable_at(move.unloading_end) for move in moves):
            raise ValueError('Initial plan contains expired deliveries')
    scheduler = Scheduler(scenario, limits, model, policy=search_policy)
    result = build_result(raw, scenario, scheduler.plan(strategy, initial_plan=moves), limits, tariffs)
    if search_policy is not None:
        result['search_diagnostics'] = {
            'policy': {key: value for key, value in asdict(search_policy).items()
                       if key not in ('recover_undelivered', 'sea_fleet_allocation', 'disable_transshipment') or value},
            'budget_seconds': search_policy.budget_seconds,
            'elapsed_seconds': round(perf_counter() - started, 6), **scheduler.diagnostics(),
        }
    return result


def audit_result(result, *, require_cost=True):
    """Recheck embedded inputs and voyages from a stored result without planning."""
    if result['schema_version'] != VERSION or fingerprint(result['scenario']) != result['input_fingerprint']:
        raise ValueError('Invalid network version or input fingerprint')
    scenario = parse_scenario(result['scenario'])
    strategy = result['strategy']
    if (scenario.id != result['scenario_id'] or result['data_status'] != scenario.data_status
            or strategy not in (*STRATEGIES, RETURN_EMPTY, COST)):
        raise ValueError('Invalid scenario or strategy')
    moves = decode_voyages(result['voyages'])
    policy = {'allow_reposition': strategy in (REPOSITION, COST), 'allow_return': strategy == RETURN_EMPTY}
    report = audit(scenario, moves, **policy)
    if report['schedule_audit']['status'] == 'FAILED':
        return report
    state = replay(scenario, moves, **policy)
    metrics, finance, cargo_status = summarize(scenario, moves, report)
    expected = {**report, 'events': _events(scenario, moves), 'metrics': metrics, 'financial_inputs': finance,
                'cargo_status': cargo_status, 'final_resources': _resources(scenario, state.locations)}
    if scenario.terminals:
        expected['transshipment_batches'] = _transshipment_summary(scenario, moves)
    if scenario.berths:
        expected['port_operations'] = port_report(scenario, expected['events'])
    if any(c.expires_at is not None for c in scenario.cargo.values()):
        expected['cargo_condition'] = cargo_condition(scenario, moves)
    priced = result.get('financial_evaluation')
    if priced and ('tariff_parameters' in priced or strategy == COST):
        from network.costs import CostModel
        try:
            expected['financial_evaluation'] = CostModel(scenario, priced['tariff_parameters']).breakdown(
                moves, legacy='cost_basis' not in priced)
        except (ValueError, KeyError, TypeError) as exc:
            report['schedule_audit'] = {'status': 'FAILED', 'violations': [
                {'voyage': None, 'code': 'INVALID_TARIFF_PARAMETERS', 'detail': str(exc)}]}
            return report
    elif strategy == COST and require_cost:
        report['schedule_audit'] = {'status': 'FAILED', 'violations': [
            {'voyage': None, 'code': 'MISSING_TARIFF_PARAMETERS', 'detail': 'NETWORK_COST needs version 2 tariffs'}]}
        return report
    floor = result.get('cost_service_floor')
    if strategy == COST and floor is not None and (
            metrics['delivered_on_time_t'] < floor['on_time_t'] or metrics['delivered_total_t'] < floor['total_t']):
        report['schedule_audit'] = {'status': 'FAILED', 'violations': [
            {'voyage': None, 'code': 'COST_SERVICE_FLOOR', 'detail': 'Delivered tonnes below exported floor'}]}
        return report
    mismatches = [name for name, value in expected.items() if result.get(name) != value]
    if mismatches:
        code = 'EVENT_LOG_MISMATCH' if 'events' in mismatches else 'DERIVED_FIELDS_MISMATCH'
        report['schedule_audit'] = {'status': 'FAILED', 'violations': [
            {'voyage': None, 'code': code, 'detail': ', '.join(mismatches)}]}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', type=Path, help='Synthetic scenario JSON')
    parser.add_argument('--strategy', choices=(*STRATEGIES, RETURN_EMPTY, COST), default=REPOSITION)
    parser.add_argument('--limits', type=Path, help='JSON object overriding search limits')
    parser.add_argument('--tariffs', type=Path)
    parser.add_argument('--initial-plan', type=Path, help='Audited v3 seed for NETWORK_COST')
    parser.add_argument('--mode', choices=('FAST', 'DEMO', 'STANDARD', 'RESEARCH'))
    parser.add_argument('--time-seconds', type=float)
    parser.add_argument('--recover-undelivered', action='store_true')
    parser.add_argument('--sea-fleet-allocation', action='store_true')
    parser.add_argument('--disable-transshipment', action='store_true')
    parser.add_argument('--top-k-pairs-per-cargo', type=int)
    parser.add_argument('--max-candidates-per-state', type=int)
    parser.add_argument('--max-detailed-checks-per-iteration', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)

    def read(path):
        return json.loads(path.read_text(encoding='utf-8-sig'))

    policy = SearchPolicy(args.mode, args.time_seconds, args.top_k_pairs_per_cargo, args.max_candidates_per_state,
                          args.max_detailed_checks_per_iteration, args.recover_undelivered, args.sea_fleet_allocation,
                          args.disable_transshipment)
    result = run_scenario(read(args.scenario), args.strategy, limits=Limits(**read(args.limits)) if args.limits else None,
                          tariffs=read(args.tariffs) if args.tariffs else None,
                          search_policy=policy if policy != SearchPolicy() else None,
                          initial_plan=read(args.initial_plan) if args.initial_plan else None)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(payload)
    else:
        sys.stdout.buffer.write(payload.encode('utf-8'))
