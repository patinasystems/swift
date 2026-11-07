#!/usr/bin/env python3
"""Whole-build paired cohorts through Pat, in a dedicated Kitchen worktree.

The two fixture files exist only during a run. They exercise body/API/error
invalidation without changing existing application files.
"""

import argparse
from pathlib import Path
import statistics
import subprocess
import sys
import time

from solver_bench import digest, interval, load, output, save, validate_pair
from prepare_toolchain import validate_selection


DECLARATION = '''// Temporary compiler benchmark input; never ship this file.
private func patinaSolverBenchmarkValue() -> Int { return VALUE }
public enum PatinaSolverBenchmarkAPI {
    public static var value: TYPE { CONVERT(patinaSolverBenchmarkValue()) }
}
'''
CONSUMER = '''// Temporary compiler benchmark input; never ship this file.
func patinaSolverBenchmarkConsumer() -> Int64 {
    Int64(PatinaSolverBenchmarkAPI.value)
}
'''


def fixture(cohort, changed=False):
    value = 'missingPatinaSolverBenchmarkValue' if cohort == 'error' and changed else ('2' if changed else '1')
    typ = 'Int64' if cohort == 'api-edit' and changed else 'Int'
    return DECLARATION.replace('VALUE', value).replace('TYPE', typ).replace('CONVERT', typ)


def validate_warm_state(expected, actual):
    for key in ('buildStatePath', 'laneId', 'cacheKey', 'configuration', 'scheme', 'sdk'):
        if not expected.get(key) or actual.get(key) != expected[key]:
            raise ValueError(f'Warm build-state continuity lost: {key}')
    if actual.get('cacheState') != 'hit' or actual.get('buildDisposition') != 'incremental':
        raise ValueError('Incremental cohort lost warm lane affinity or requested a cold build')


def invoke(root, record, selection, destination, cohort):
    import os
    validate_selection(record, selection)
    destination.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, TOOLCHAINS=selection['identifier'])
    command = [str(root / 'pat'), 'ios', 'build', 'angles', '--on', 'local', '--json']
    if cohort == 'cold-profile':
        command += ['--profile', '--threshold', '100']
    started = time.perf_counter()
    with (destination / 'pat.json').open('wb') as stdout, (destination / 'progress.log').open('wb') as stderr:
        result = subprocess.run(command, cwd=root, env=env, stdout=stdout, stderr=stderr)
    receipt = load(destination / 'pat.json')
    expected_failure = cohort == 'error'
    if expected_failure:
        if result.returncode == 0 or receipt.get('ok') is not False:
            raise ValueError('Deliberate compiler error unexpectedly succeeded')
        # A signing/storage/setup failure is not a successful error-latency test.
        text = (destination / 'progress.log').read_text(errors='replace')
        log_path = receipt.get('error', {}).get('details', {}).get('logPath')
        if log_path and Path(log_path).is_file():
            text += Path(log_path).read_text(errors='replace')
        for evidence in receipt.get('partialResult', {}).get('evidence', []):
            if evidence.get('kind') == 'xcodebuild-log' and Path(evidence.get('path', '')).is_file():
                text += Path(evidence['path']).read_text(errors='replace')
        if 'missingPatinaSolverBenchmarkValue' not in text:
            raise ValueError('The failed build did not diagnose the intentional source error')
    elif result.returncode or receipt.get('ok') is not True:
        raise ValueError(f'Pat build failed; inspect {destination}')
    snapshots = [e.get('summary') for e in receipt.get('result', {}).get('evidence', [])
                 if e.get('kind') == 'source-snapshot' and e.get('status') == 'passed']
    return {'seconds': time.perf_counter() - started, 'pat_receipt': str(destination / 'pat.json'),
            'load_average': list(os.getloadavg()),
            'build_state': receipt.get('result', {}).get('buildState', {}),
            'failed_build_log': receipt.get('error', {}).get('details', {}).get('logPath'),
            'source_snapshot': snapshots[0] if len(snapshots) == 1 else None,
            'timings': receipt.get('result', {}).get('timings', {}),
            'ok': receipt.get('ok')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kitchen', required=True)
    parser.add_argument('--baseline', required=True, help='Baseline compiler.json')
    parser.add_argument('--candidate', required=True, help='Candidate compiler.json')
    parser.add_argument('--baseline-selection', required=True, help='Registered toolchain selection.json')
    parser.add_argument('--candidate-selection', required=True)
    parser.add_argument('--cohort', choices=['cold-profile', 'no-op', 'body-edit', 'api-edit', 'error'], required=True)
    parser.add_argument('--repetitions', type=int, default=7)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    root = Path(args.kitchen).resolve()
    if not (root / '.git').is_file() or output(['git', 'branch', '--show-current'], root) == 'main':
        raise ValueError('Use a dedicated linked Kitchen worktree, never the main checkout')
    if args.repetitions < 7:
        raise ValueError('Require at least seven paired rounds')
    records = {arm: load(path) for arm, path in [('baseline', args.baseline), ('candidate', args.candidate)]}
    validate_pair(records['baseline'], records['candidate'])
    selections = {arm: load(path) for arm, path in [('baseline', args.baseline_selection),
                                                   ('candidate', args.candidate_selection)]}
    for arm in records:
        if selections[arm]['compiler_sha256'] != records[arm]['sha256'] or not selections[arm]['registration']:
            raise ValueError(f'Wrong or unregistered toolchain selection for {arm}')
    destination = Path(args.out).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    paths = [root / 'apps/angles/ios/Sources/PatinaSolverBenchmarkInput.swift',
             root / 'apps/angles/ios/Sources/PatinaSolverBenchmarkConsumer.swift']
    if any(p.exists() for p in paths):
        raise ValueError('Benchmark fixture path already exists; do not overwrite it')
    source_head = output(['git', 'rev-parse', 'HEAD'], root)
    before_status = output(['git', 'status', '--porcelain'], root)
    if before_status:
        raise ValueError('Dedicated benchmark worktree must be clean before adding fixtures')
    receipt = {'status': 'running', 'cohort': args.cohort, 'source_revision': source_head,
               'compilers': records, 'selections': selections, 'samples': []}
    owned_contents = {}

    def write_inputs(changed):
        for path, content in zip(paths, [fixture(args.cohort, changed), CONSUMER]):
            if path.exists() and path.read_text() != owned_contents.get(path):
                raise ValueError(f'Fixture was edited externally; preserve it: {path}')
            path.write_text(content)
            owned_contents[path] = content

    try:
        write_inputs(False)
        states = {}
        for arm in ('baseline', 'candidate'):
            seed = invoke(root, records[arm], selections[arm], destination / 'seed' / arm, 'seed')
            states[arm] = seed['build_state']
        if states['baseline'].get('cacheKey') == states['candidate'].get('cacheKey'):
            raise ValueError('Pat did not distinguish the experimental compiler identities')
        if args.cohort != 'cold-profile' and (states['baseline'].get('buildStatePath') ==
                                              states['candidate'].get('buildStatePath')):
            raise ValueError('Incremental arms must retain separate mutable build-state lanes')
        receipt['seed_build_states'] = states
        for n in range(args.repetitions):
            pair = []
            for arm in (('baseline', 'candidate') if n % 2 == 0 else ('candidate', 'baseline')):
                if output(['git', 'rev-parse', 'HEAD'], root) != source_head:
                    raise ValueError('Kitchen HEAD changed during the benchmark')
                # Seed the same pristine state before each invalidating edit.
                if args.cohort in ('body-edit', 'api-edit', 'error'):
                    write_inputs(False)
                    preparation = invoke(root, records[arm], selections[arm], destination / 'prepare' / str(n) / arm, 'seed')
                    validate_warm_state(states[arm], preparation['build_state'])
                    write_inputs(True)
                sample = invoke(root, records[arm], selections[arm], destination / 'timing' / str(n) / arm, args.cohort)
                if args.cohort in ('no-op', 'body-edit', 'api-edit'):
                    validate_warm_state(states[arm], sample['build_state'])
                elif args.cohort == 'error':
                    log = sample['failed_build_log']
                    if not log or Path(log).parent.parent.resolve() != Path(states[arm]['buildStatePath']).resolve():
                        raise ValueError('Deliberate error did not use the seeded build-state path')
                sample.update({'round': n, 'arm': arm, 'fixture_sha256': digest(paths[0])})
                receipt['samples'].append(sample)
                pair.append(sample)
                save(destination / 'run.json', receipt)
                print(f'{args.cohort} {n + 1} {arm}: {sample["seconds"]:.3f}s', flush=True)
            if pair[0]['fixture_sha256'] != pair[1]['fixture_sha256']:
                raise ValueError('Paired source fixtures differ')
            if args.cohort != 'error' and (not pair[0]['source_snapshot'] or
                                          pair[0]['source_snapshot'] != pair[1]['source_snapshot']):
                raise ValueError('Paired Pat source snapshots differ')
            if args.cohort != 'error' and any(s['source_snapshot'] != receipt['samples'][0]['source_snapshot']
                                             for s in receipt['samples']):
                raise ValueError('Pat source snapshot changed between rounds')
        validate_pair(records['baseline'], records['candidate'])
        ratios = []
        for n in range(args.repetitions):
            pair = {s['arm']: s for s in receipt['samples'] if s['round'] == n}
            ratios.append(pair['baseline']['seconds'] / pair['candidate']['seconds'])
        receipt.update({'status': 'passed', 'median_speedup': statistics.median(ratios),
                        'warm_affinity_verified': args.cohort in ('no-op', 'body-edit', 'api-edit'),
                        'acceptance_scope': 'diagnostic latency discovery' if args.cohort == 'error' else args.cohort,
                        'paired_speedups': ratios, 'speedup_95_percent_bootstrap_interval': interval(ratios)})
    except BaseException as error:
        receipt.update({'status': 'invalid', 'error': str(error)})
        raise
    finally:
        for path, content in owned_contents.items():
            if path.is_file() and path.read_text() == content:
                path.unlink()
        save(destination / 'run.json', receipt)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
