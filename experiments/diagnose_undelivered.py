"""Reproduce residual cargo probes against an exported, independently audited plan."""

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from time import perf_counter

from network.adapter import audit_result, decode_voyages, encode_voyage, run_scenario
from network.analysis import _reachable, diagnose
from network.insertion import FixedPlan, improve_fixed_plan
from network.models import Limits, loaded_draft, parse_scenario
from network.costs import CostModel
from network.scheduler import REPOSITION
from network.search_policy import SearchPolicy
from network.validator import replay


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def inspect_result(result, *, seconds_per_cargo=1):
    audit = audit_result(result)
    if audit['schedule_audit']['status'] != 'PASSED':
        raise ValueError(audit['schedule_audit'])
    raw = result['scenario']
    scenario = parse_scenario(raw)
    moves = decode_voyages(result['voyages'])
    limits = Limits(**result['parameters'])
    fixed = FixedPlan(scenario, moves, limits)
    residual = replay(scenario, moves).remaining
    diagnoses = {row['cargo_id']: row['evidence'] for row in diagnose(result)['cargo']}
    tariff = (result.get('financial_evaluation') or {}).get('tariff_parameters')
    cost_model = CostModel(scenario, tariff) if tariff else None
    trace = result.get('search_diagnostics', {}).get('cargo_search', {})
    used = {key for move in moves for key in (move.tug_id, move.barge_id)}
    idle = {}
    for key, vessel in {**scenario.tugs, **scenario.barges}.items():
        if key not in used:
            is_tug = key in scenario.tugs
            idle[key] = {'type': 'tug' if is_tug else 'barge', 'base': vessel.initial.port,
                         'draft_m': vessel.draft if is_tug else vessel.draft_empty,
                         'full_draft_m': None if is_tug else vessel.draft_full,
                         'capacity_t': None if is_tug else vessel.capacity_kg / 1000,
                         'available_at': vessel.initial.available_at.isoformat(),
                         'available_to': vessel.available_to.isoformat(), 'compatible_cargo': [],
                         'admitted_approach_cargo': [], 'isolated_witness_cargo': [], 'insertion_witness_cargo': []}
    statuses = {row['cargo_id']: row for row in result['cargo_status']}
    rows = []
    categories = Counter()
    for entry in raw['cargo']:
        key = entry['cargo_id']
        status = statuses[key]
        if not status['remaining_t']:
            continue
        categories[status['cargo_type']] += status['remaining_t']
        cargo = scenario.cargo[key]
        pairs = [(tug, barge) for tug, barge in fixed.pairs if cargo.type in barge.cargo_types]
        connected = _reachable(scenario, cargo.origin, cargo.destination)
        load_draft_path = False
        window_path = False
        deadline_bound = False
        for tug, barge in pairs:
            origin = tug.initial.port
            for vessel in (tug, barge):
                if vessel.id in idle:
                    idle[vessel.id]['compatible_cargo'].append(key)
            if origin == barge.initial.port and fixed.router.lower_bound(tug, barge, origin, cargo.origin,
                                                                         reposition=True) is not None:
                for vessel in (tug, barge):
                    if vessel.id in idle:
                        idle[vessel.id]['admitted_approach_cargo'].append(key)
            signature = fixed.router._signatures(tug.id, barge.id, False)
            outgoing, _ = fixed.router._edges(signature)
            if not window_path:
                window_path = fixed.router.lower_bound(tug, barge, cargo.origin, cargo.destination) is not None
            if not load_draft_path:
                draft = loaded_draft(tug, barge, 1)
                visited, queue = {cargo.origin}, [cargo.origin]
                while queue:
                    port = queue.pop()
                    for edge, _ in outgoing[port]:
                        if edge.destination in visited:
                            continue
                        calendar = fixed.router.calendars[edge.id]
                        if any(not row['is_closed'] and draft <= row['max_allowed_draft_m'] + 1e-6
                               and draft + scenario.underkeel <= row['usable_depth_m'] + 1e-6
                               for row in calendar.conditions):
                            visited.add(edge.destination)
                            queue.append(edge.destination)
                load_draft_path = cargo.destination in visited
            lower = fixed.router.lower_bound(tug, barge, cargo.origin, cargo.destination)
            if lower is not None and max(cargo.ready, scenario.start) + lower <= cargo.due:
                deadline_bound = True
        isolated_raw = dict(raw, cargo=[entry], social_requirements=[])
        isolated = run_scenario(isolated_raw, REPOSITION,
                               limits=replace(limits, candidate_limit=max(64, limits.candidate_limit)),
                               search_policy=SearchPolicy(mode='RESEARCH', time_seconds=seconds_per_cargo))
        if audit_result(isolated)['schedule_audit']['status'] != 'PASSED':
            raise ValueError(f'Invalid isolated result: {key}')
        for move in isolated['voyages']:
            for vessel in (move['tug_id'], move['barge_id']):
                if vessel in idle and key not in idle[vessel]['isolated_witness_cargo']:
                    idle[vessel]['isolated_witness_cargo'].append(key)
        witness = None
        if residual[key]:
            for candidate in fixed.candidates(cargo, residual[key]):
                if fixed.accepts(candidate):
                    witness = candidate
                    break
        if witness:
            for vessel in (witness.tug_id, witness.barge_id):
                if vessel in idle:
                    idle[vessel]['insertion_witness_cargo'].append(key)
        evidence = diagnoses.get(key, [])
        proven = [e['code'] for e in evidence if e['kind'] == 'PROVEN']
        if not proven and window_path and not load_draft_path:
            proven.append('DRAFT_RESTRICTION')
        timely_reason = None if deadline_bound else 'DEADLINE_UNREACHABLE'
        rows.append({**entry, **status, 'proven_restrictions': proven, 'evidence': evidence,
                     'timely_lower_bound_failure': timely_reason, 'topological_path': connected,
                     'compatible_tugs': sorted({t.id for t, _ in pairs}),
                     'compatible_barges': sorted({b.id for _, b in pairs}),
                     'admitted_window_path_optimistic': window_path, 'minimum_draft_path_optimistic': load_draft_path,
                     'assignment_trace': trace.get(key),
                     'search_stop': result['termination_reason'],
                     'isolated': {'delivered_t': isolated['metrics']['delivered_total_t'],
                                  'on_time_t': isolated['metrics']['delivered_on_time_t'],
                                  'termination_reason': isolated['termination_reason'],
                                  'seconds': isolated['search_diagnostics']['elapsed_seconds'],
                                  'fleet': sorted({v[k] for v in isolated['voyages'] for k in ('tug_id', 'barge_id')})},
                     'fixed_plan_insertion': encode_voyage(witness) if witness else None,
                     'diagnosis': ('INSERTION_WITNESS' if witness else proven[0] if proven else 'UNKNOWN')})
    local, repair_report = improve_fixed_plan(scenario, moves, limits, cost_model=cost_model)
    removed, added = set(moves) - set(local), set(local) - set(moves)
    repair_report.update(removed=[encode_voyage(v) for v in moves if v in removed],
                         added=[encode_voyage(v) for v in local if v in added])
    for vessel in idle.values():
        for key in ('compatible_cargo', 'admitted_approach_cargo'):
            vessel[key] = sorted(set(vessel[key]))
    return {'local_repair': repair_report, 'input_fingerprint': result['input_fingerprint'], 'metrics': result['metrics'],
            'remaining_by_type_t': dict(categories), 'audit': audit['schedule_audit'],
            'idle_fleet': idle, 'cargo': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds-per-cargo', type=float, default=1)
    args = parser.parse_args()
    started = perf_counter()
    report = inspect_result(read(args.result), seconds_per_cargo=args.seconds_per_cargo)
    report['result_sha256'] = hashlib.sha256(args.result.read_bytes()).hexdigest()
    report['elapsed_seconds'] = round(perf_counter() - started, 3)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"{len(report['cargo'])} residual cargos; "
          f"{sum(row['fixed_plan_insertion'] is not None for row in report['cargo'])} insertion witnesses")


if __name__ == '__main__':
    main()
