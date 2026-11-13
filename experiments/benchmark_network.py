"""Compare the same network inputs in isolated Python processes."""

import argparse
import cProfile
import json
from pathlib import Path
import platform
import pstats
from statistics import median
import subprocess
import sys
from time import perf_counter
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def measure(args):
    sys.path.insert(0, str(args.tree))
    from network.adapter import audit_result, run_scenario
    from network.models import Limits

    raw = read(args.scenario)
    kwargs = {'limits': Limits(**read(args.limits)) if args.limits else None,
              'tariffs': read(args.tariffs) if args.tariffs else None}
    if args.instrument:
        from network.search_policy import SearchPolicy
        kwargs['search_policy'] = SearchPolicy(**json.loads(args.policy or '{}'))

    profile = cProfile.Profile() if args.profile else None
    if args.memory:
        tracemalloc.start()
    if profile:
        profile.enable()
    start = perf_counter()
    result = run_scenario(raw, args.strategy, **kwargs)
    elapsed = perf_counter() - start
    if profile:
        profile.disable()
    peak = None
    if args.memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    start = perf_counter()
    audit = audit_result(result)['schedule_audit']
    audit_elapsed = perf_counter() - start
    if audit['status'] != 'PASSED':
        raise ValueError(audit)
    stats = {}
    if profile:
        if args.profile_output:
            profile.dump_stats(args.profile_output)
        tracked = {'paths', '_search', 'passage_open', 'check_passage', 'blocked_until', 'voyages',
                   'best_mass', 'at_mass', 'candidates', 'append', 'key', 'score', 'sort', 'sorted',
                   'nsmallest', 'nlargest', 'replay', 'assignments'}
        for (file, line, name), (_, calls, own, cumulative, _) in pstats.Stats(profile).stats.items():
            if name in tracked or any(name.endswith(f'.{key}') for key in tracked):
                stats[f'{Path(file).name}:{line}:{name}'] = {
                    'calls': calls, 'own_seconds': own, 'cumulative_seconds': cumulative}
    print(json.dumps({'elapsed_seconds': elapsed, 'audit_seconds': audit_elapsed,
                      'python_peak_bytes': peak, 'profile': stats, 'result': result}, ensure_ascii=True))


def worker(tree, args, *, instrument=False, policy=None, profile=None, memory=False):
    command = [sys.executable, str(Path(__file__).resolve()), '--tree', str(tree), str(args.scenario.resolve()),
               '--strategy', args.strategy]
    for key in ('limits', 'tariffs'):
        path = getattr(args, key)
        if path:
            command += ['--' + key, str(path.resolve())]
    if instrument:
        command.append('--instrument')
    if policy is not None:
        command += ['--policy', json.dumps(policy)]
    if profile:
        command += ['--profile', '--profile-output', str(profile.resolve())]
    if memory:
        command.append('--memory')
    try:
        return json.loads(subprocess.check_output(command, cwd=tree, encoding='utf-8',
                                                  timeout=args.baseline_timeout if tree == args.baseline.resolve() else None))
    except subprocess.TimeoutExpired:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', type=Path)
    parser.add_argument('--strategy', default='NETWORK_REPOSITION')
    parser.add_argument('--limits', type=Path)
    parser.add_argument('--tariffs', type=Path)
    parser.add_argument('--baseline', type=Path, help='Path to a checkout of the baseline commit')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--baseline-timeout', type=float,
                        help='Stop a single baseline run after this many seconds and record it as censored')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--heuristic', type=Path, help='Optional SearchPolicy JSON to compare separately')
    parser.add_argument('--tree', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--instrument', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--policy', help=argparse.SUPPRESS)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--profile-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--memory', action='store_true', help='Measure Python allocations in separate runs')
    args = parser.parse_args()
    if args.tree is not None:
        measure(args)
        return
    if args.baseline is None or args.output is None or args.repeat < 1 or (
            args.baseline_timeout is not None and args.baseline_timeout <= 0):
        parser.error('Provide --baseline, --output and a positive --repeat')
    baseline = args.baseline.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = {'baseline': [], 'optimized': []}
    trees = {'baseline': baseline, 'optimized': ROOT}
    for _ in range(args.repeat):
        for label, tree in trees.items():
            row = worker(tree, args)
            rows[label].append(row)
            print(label, f"{row['elapsed_seconds']:.3f} s" if row else 'TIMEOUT', flush=True)
    improved = rows['optimized'][0]['result']
    original = next((row['result'] for row in rows['baseline'] if row), None)
    identical = (all(row['result'] == original for group in rows.values() for row in group if row)
                 if original is not None else None)
    report = {'python': platform.python_version(), 'strategy': args.strategy,
              'input_fingerprint': improved['input_fingerprint'], 'parameters': improved['parameters'],
              'identical_results': identical, 'baseline_timeout_seconds': args.baseline_timeout, 'runs': {}}
    for label, tree in trees.items():
        samples = [row for row in rows[label] if row]
        if not samples:
            report['runs'][label] = {'status': 'TIMEOUT', 'samples_seconds': []}
            continue
        timed = [row['elapsed_seconds'] for row in samples]
        result = samples[0]['result']
        report['runs'][label] = {'samples_seconds': timed, 'median_seconds': median(timed),
                                 'audit_seconds': median(row['audit_seconds'] for row in samples),
                                 'metrics': result['metrics'], 'expanded_states': result['expanded_states'],
                                 'financial_evaluation': result['financial_evaluation'],
                                 'schedule_audit': result['schedule_audit']}
        if args.profile:
            profile = worker(tree, args, instrument=label == 'optimized',
                             profile=args.output / f'{label}.prof')
            if profile:
                report['runs'][label]['profile'] = profile['profile']
            if label == 'optimized':
                report['runs'][label]['search_diagnostics'] = profile['result']['search_diagnostics']
        if args.memory:
            sample = worker(tree, args, memory=True)
            report['runs'][label]['python_peak_bytes'] = sample['python_peak_bytes'] if sample else None
    before = report['runs']['baseline'].get('median_seconds')
    after = report['runs']['optimized']['median_seconds']
    report['speedup'] = before / after if before is not None else None
    if args.heuristic:
        report['heuristic'] = worker(ROOT, args, instrument=True, policy=read(args.heuristic))
    for name, payload in [('comparison', report), ('baseline', original), ('optimized', improved)]:
        if payload is not None:
            with (args.output / f'{name}.json').open('x', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
    print(f"Speedup: {report['speedup']:.2f}x." if before is not None else 'Baseline timed out.')
    print(f"Identical results: {identical}.")
    if identical is False:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
