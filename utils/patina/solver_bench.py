#!/usr/bin/env python3
"""Capture real frontend jobs and run paired, input-checked typecheck benchmarks.

Use through the host's compute scheduler (Kitchen: pat run --cost heavy).
Captured application sources, compiler modules, transcripts, and results stay local.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import shlex
import statistics
import subprocess
import sys
import threading
import time


SCHEMA = 1
# Remove all output producers from an Xcode compile before replay. The original
# lane remains read-only; this benchmark produces no object/module/index files.
OUTPUT_VALUES = {
    '-o', '-supplementary-output-file-map', '-output-file-map',
    '-emit-dependencies-path', '-emit-reference-dependencies-path',
    '-serialize-diagnostics-path', '-emit-const-values-path',
    '-emit-module-path', '-emit-module-doc-path', '-emit-module-source-info-path',
    '-emit-objc-header-path', '-emit-tbd-path', '-emit-abi-descriptor-path',
    '-index-store-path', '-index-unit-output-path', '-debug-module-path',
    '-stats-output-dir', '-trace-stats-events-path', '-pch-output-dir',
    '-optimization-record-file', '-emit-loaded-module-trace-path',
    '-emit-module-semantic-info-path', '-emit-module-summary-path',
}
OUTPUT_FLAGS = {
    '-emit-dependencies', '-emit-reference-dependencies', '-serialize-diagnostics',
    '-emit-module', '-emit-objc-header', '-emit-tbd', '-emit-const-values',
    '-emit-module-interface', '-emit-private-module-interface',
    '-emit-package-module-interface', '-emit-module-separately-wmo',
}
PROFILE_FLAGS = {
    '-debug-time-function-bodies', '-debug-time-expression-type-checking',
    '-Rmodule-loading', '-Rmodule-interface-rebuild',
}
PROFILE_PREFIXES = ('-warn-long-function-bodies=',
                    '-warn-long-expression-type-checking=')
INPUT_VALUES = {
    '-import-objc-header', '-import-pch', '-explicit-swift-module-map-file',
    '-const-gather-protocols-file', '-vfsoverlay', '-swift-module-file',
    '-clang-build-session-file', '-in-process-plugin-server-path',
    '-load-plugin-executable', '-load-plugin-library', '-load-resolved-plugin',
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def object_digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def load(path):
    return json.loads(Path(path).read_text())


def output(command, cwd=None):
    return subprocess.check_output(command, cwd=cwd, text=True,
                                   stderr=subprocess.STDOUT).strip()


def expand_response_files(words, cwd, inputs, active=None):
    active = set() if active is None else active
    expanded = []
    for word in words:
        if not word.startswith('@'):
            expanded.append(word)
            continue
        path = (Path(cwd) / word[1:]).resolve()
        if path in active:
            raise ValueError(f'Recursive response file: {path}')
        inputs.add(path)
        expanded.extend(expand_response_files(shlex.split(path.read_text()), cwd,
                                              inputs, active | {path}))
    return expanded


def typecheck_arguments(words):
    if '-c' not in words or '-frontend' not in words:
        raise ValueError('Expected an explicit swift-frontend -frontend -c job')
    result = []
    i = 1
    while i < len(words):
        word = words[i]
        if word in OUTPUT_VALUES:
            if i + 1 == len(words):
                raise ValueError(f'Missing value for {word}')
            i += 2
            continue
        if word in OUTPUT_FLAGS or word in PROFILE_FLAGS or word.startswith(PROFILE_PREFIXES):
            i += 1
            continue
        if word.startswith(('-emit-', '-serialize-diagnostics', '-index-store-',
                            '-supplementary-output-', '-output-file-map')):
            raise ValueError(f'Unrecognized output option; refuse unsafe replay: {word}')
        result.append('-typecheck' if word == '-c' else word)
        i += 1
    return result


def module_arguments(words):
    if '-emit-module' not in words or '-frontend' not in words:
        raise ValueError('Expected an explicit module-emission job')
    result = []
    i = 1
    while i < len(words):
        word = words[i]
        if word in ('-supplementary-output-file-map', '-output-file-map'):
            raise ValueError('Module-emission output maps require explicit support')
        if word in OUTPUT_VALUES:
            if i + 1 == len(words):
                raise ValueError(f'Missing value for {word}')
            if word not in ('-index-store-path', '-index-unit-output-path', '-debug-module-path',
                            '-stats-output-dir', '-trace-stats-events-path'):
                result += [word, '{outputs}/' + str(i) + '-' + Path(words[i + 1]).name]
            i += 2
            continue
        if word in PROFILE_FLAGS or word.startswith(PROFILE_PREFIXES):
            i += 1
            continue
        if word.startswith('-emit-') and word not in OUTPUT_FLAGS:
            raise ValueError(f'Unrecognized output option; refuse unsafe replay: {word}')
        result.append(word)
        i += 1
    if '-o' not in result and '-emit-module-path' not in result:
        raise ValueError('Module-emission job has no explicit redirected module output')
    return result


def source_file_list(path, cwd):
    result = []
    for line in Path(path).read_text().splitlines():
        if line.startswith('"') and line.endswith('"'):
            line = shlex.split(line)[0]
        if line:
            source = (Path(cwd) / line).resolve(strict=True)
            if not source.is_file():
                raise ValueError(f'Source file list entry is not a file: {source}')
            result.append(source)
    return result


def existing_paths(value, cwd):
    # Swift plugin options and explicit module options carry suffixes after '#'
    # and '='. The executable or module itself is an input as well.
    candidates = [value, *value.split('#')]
    payload = value
    for prefix in ('-swift-module-file=', '-fmodule-file=', '-fmodule-map-file='):
        if payload.startswith(prefix):
            payload = payload[len(prefix):]
            break
    if not payload.startswith(('/', '.')) and '=' in payload:
        payload = payload.split('=', 1)[1]
    candidates.append(payload)
    for candidate in candidates:
        if candidate and not candidate.startswith('-'):
            path = (Path(cwd) / candidate).resolve()
            if path.is_file():
                yield path


def job_inputs(argv, cwd):
    inputs = set()
    for i, word in enumerate(argv):
        if word.endswith('.swift') and not word.startswith('-'):
            inputs.add((Path(cwd) / word).resolve(strict=True))
        if word.startswith('/') or word.endswith('.swift') or (i and argv[i - 1] in INPUT_VALUES):
            inputs.update(existing_paths(word, cwd))
        if word.startswith(('-swift-module-file=', '-fmodule-file=', '-fmodule-map-file=')):
            inputs.update(existing_paths(word, cwd))
        if word in ('-filelist', '-primary-filelist'):
            path = (Path(cwd) / argv[i + 1]).resolve(strict=True)
            inputs.add(path)
            inputs.update(source_file_list(path, cwd))
        if word.startswith('-I') and len(word) > 2:
            inputs.update(existing_paths(word[2:], cwd))
        if word == '-sdk' and i + 1 < len(argv):
            sdk = Path(argv[i + 1])
            inputs.update(p.resolve() for p in (sdk / 'SDKSettings.json',
                                                 sdk / 'SDKSettings.plist') if p.is_file())
    # Explicit module maps name the actual module/PCM/header inputs. Hash their
    # contents, not just the JSON that points at them.
    pending = [p for p in inputs if p.suffix == '.json']
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        data = load(path)
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, str):
                for p in existing_paths(node, cwd):
                    if p not in inputs and p.suffix == '.json':
                        pending.append(p)
                    inputs.add(p)
    return inputs


def import_roots(argv, cwd):
    roots = set()
    for i, word in enumerate(argv):
        if word in ('-I', '-F', '-Fsystem', '-Isystem', '-prebuilt-module-cache-path', '-resource-dir'):
            roots.add(str(Path(os.path.abspath(Path(cwd) / argv[i + 1]))))
        elif word.startswith(('-I', '-F')) and len(word) > 2:
            roots.add(str(Path(os.path.abspath(Path(cwd) / word[2:]))))
    return roots


def search_modules(root):
    root = Path(root)
    files = {}
    if root.is_dir():
        for path in root.rglob('*'):
            if path.is_dir() and path.suffix == '.swiftmodule' and path.is_symlink():
                candidates = path.rglob('*')
            else:
                candidates = [path]
            for candidate in candidates:
                if candidate.is_file() and candidate.suffix in ('.swiftmodule', '.swiftinterface', '.pcm', '.modulemap'):
                    files[str(candidate.relative_to(root))] = str(candidate.resolve())
    return {'resolved_root': str(root.resolve()), 'files': files}


def capture(transcript, receipt, modules, destination, sources=(), kind='typecheck'):
    transcript = Path(transcript).resolve()
    opener = gzip.open if transcript.suffix == '.gz' else open
    inputs = {transcript, Path(receipt).resolve()}
    jobs = {}
    roots = set()
    cwd = None
    with opener(transcript, 'rt', errors='replace') as stream:
        for line in stream:
            trimmed = line.strip()
            if trimmed.startswith('cd '):
                values = shlex.split(trimmed)
                if len(values) == 2:
                    cwd = values[1]
            if not trimmed.startswith('/') or '/swift-frontend ' not in trimmed:
                continue
            words = shlex.split(trimmed)
            if not cwd or not Path(cwd).is_dir():
                raise ValueError('Frontend job has no existing working directory')
            response_inputs = set()
            words = expand_response_files(words, cwd, response_inputs)
            action = 'typecheck' if '-c' in words else 'module-emission' if '-emit-module' in words else None
            if not action or '-module-name' not in words or kind not in ('both', action):
                continue
            module = words[words.index('-module-name') + 1]
            if module not in modules:
                continue
            if '-sdk' in words and '-explicit-swift-module-map-file' not in words:
                raise ValueError('SDK job capture requires an explicit Swift module dependency map')
            if '-sdk' in words and '-disable-implicit-swift-modules' not in words:
                raise ValueError('SDK job capture requires disabled implicit Swift modules')
            for i, word in enumerate(words[:-1]):
                if word in ('-import-objc-header', '-import-pch') and Path(words[i + 1]).suffix.lower() != '.pch':
                    raise ValueError('Capture requires a precompiled bridging header; raw header dependency closure is unsupported')
            primaries = [words[i + 1] for i, w in enumerate(words[:-1]) if w == '-primary-file']
            for i, word in enumerate(words[:-1]):
                if word == '-primary-filelist':
                    primaries.extend(str(p) for p in source_file_list(Path(cwd) / words[i + 1], cwd))
            selection_sources = primaries if action == 'typecheck' else [w for w in words if w.endswith('.swift')]
            if sources and not any(any(p.endswith(s) for s in sources) for p in selection_sources):
                continue
            if action == 'typecheck' and not primaries:
                raise ValueError('Capture expects primary-file jobs, not WMO')
            argv = typecheck_arguments(words) if action == 'typecheck' else module_arguments(words)
            ident = object_digest({'cwd': cwd, 'argv': argv})[:16]
            jobs[ident] = {'id': ident, 'module': module, 'cwd': cwd, 'kind': action,
                           'argv': argv, 'primaries': primaries,
                           'captured_compiler': str(Path(words[0]).resolve())}
            inputs.update(response_inputs)
            inputs.update(job_inputs(argv, cwd))
            roots.update(import_roots(argv, cwd))
            roots.add(str(Path(words[0]).resolve().parent.parent / 'lib/swift'))
    if not jobs:
        raise ValueError('No matching frontend compile jobs were captured')
    # Receipts must be complete JSON. Failed or interrupted builds are discovery
    # evidence, not an accepted workload baseline.
    pat = load(receipt)
    if pat.get('ok') is not True or not pat.get('result', {}).get('productPath'):
        raise ValueError('Pat build did not pass with a completed product')
    compilers = {}
    for executable in sorted({job['captured_compiler'] for job in jobs.values()}):
        compilers[executable] = {'sha256': digest(executable),
                                 'version': output([executable, '--version'])}
    search_roots = {root: search_modules(root) for root in sorted(roots)}
    for state in search_roots.values():
        inputs.update(Path(p) for p in state['files'].values())
    manifest = {'schema': SCHEMA, 'kind': 'frontend-replay',
                'captured_at': time.time(), 'pat_receipt_sha256': digest(receipt),
                'pat_provenance': pat, 'jobs': sorted(jobs.values(), key=lambda j: j['id']),
                'captured_compilers': compilers,
                'search_roots': search_roots,
                'inputs': {str(p): digest(p) for p in sorted(inputs)},
                'environment': {'SWIFT_DETERMINISTIC_HASHING': '1'}}
    manifest['input_set_sha256'] = object_digest(manifest['inputs'])
    save(destination, manifest)
    return manifest


def verify_inputs(manifest):
    changed = []
    for root, expected in manifest.get('search_roots', {}).items():
        if search_modules(root) != expected:
            raise ValueError('Import-search module membership changed: ' + root)
    for path, expected in manifest['inputs'].items():
        if not Path(path).is_file() or digest(path) != expected:
            changed.append(path)
    if changed:
        raise ValueError('Captured inputs changed or disappeared: ' + ', '.join(changed[:5]))


def record_compiler(executable, destination, provenance=None):
    executable = Path(executable).resolve(strict=True)
    record = {'schema': SCHEMA, 'executable': str(executable),
              'sha256': digest(executable),
              'version': output([str(executable), '--version']),
              'provenance': provenance or {'kind': 'reference-only'}}
    save(destination, record)
    return record


def verify_compiler(record):
    if digest(record['executable']) != record['sha256']:
        raise ValueError(f'Compiler changed: {record["executable"]}')
    root = Path(record['executable']).parent.parent
    for relative, expected in record.get('provenance', {}).get('runtime_libraries', {}).items():
        if digest(root / relative) != expected:
            raise ValueError(f'Compiler companion library changed: {relative}')


def validate_capture_compiler(manifest, baseline):
    if manifest.get('kind') != 'frontend-replay':
        return
    captured = manifest.get('captured_compilers', {})
    if not captured:
        raise ValueError('Capture lacks compiler identity; capture the jobs again')
    if any(record['version'] != baseline.get('version') or
           record['sha256'] != baseline['sha256'] for record in captured.values()):
        raise ValueError('Captured compiler version or binary differs from the baseline; '
                         'rebuild dependencies and capture with the baseline compiler')


def validate_pair(baseline, candidate, control=False):
    for record in (baseline, candidate):
        verify_compiler(record)
    if control:
        if (baseline['sha256'] != candidate['sha256'] or
            baseline['provenance'].get('runtime_libraries', {}) !=
                candidate['provenance'].get('runtime_libraries', {})):
            raise ValueError('An A/A control requires the same compiler binary')
        return
    if baseline['sha256'] == candidate['sha256']:
        raise ValueError('Identical compiler binaries cannot demonstrate an improvement')
    a, b = baseline['provenance'], candidate['provenance']
    for key in ('upstream_revision', 'dependency_lock_sha256', 'build_options',
                'host_swift_version', 'host_clang_version', 'resource_dir'):
        if not a.get(key) or a.get(key) != b.get(key):
            raise ValueError(f'Compiler provenance mismatch: {key}')
    if a.get('compiler_source_diff') != '':
        raise ValueError('Baseline must be an unmodified upstream compiler-source build')
    if not b.get('compiler_source_diff'):
        raise ValueError('Candidate must record its compiler-source change')


def replay_arguments(argv, scratch, outputs=None):
    argv = list(argv)
    for i, word in enumerate(argv):
        if word in ('-module-cache-path', '-clang-scanner-module-cache-path'):
            argv[i + 1] = str(scratch / 'module-cache')
        elif word.startswith('-fmodules-cache-path='):
            argv[i] = '-fmodules-cache-path=' + str(scratch / 'module-cache')
        elif word == '-fmodules-cache-path':
            value_index = i + 2 if argv[i + 1] == '-Xcc' else i + 1
            argv[value_index] = str(scratch / 'module-cache')
    if '-module-cache-path' not in argv:
        argv += ['-module-cache-path', str(scratch / 'module-cache')]
    if outputs:
        argv = [str(outputs / word[len('{outputs}/'):]) if word.startswith('{outputs}/') else word
                for word in argv]
    return argv


def parse_resources(text):
    if sys.platform == 'darwin':
        match = re.search(r'^\s*(\d+)\s+maximum resident set size', text, re.M)
        cpu = re.search(r'([\d.]+) real\s+([\d.]+) user\s+([\d.]+) sys', text)
        return {'peak_rss_bytes': int(match[1]) if match else None,
                'user_seconds': float(cpu[2]) if cpu else None,
                'system_seconds': float(cpu[3]) if cpu else None}
    match = re.search(r'Maximum resident set size \(kbytes\): (\d+)', text)
    return {'peak_rss_bytes': int(match[1]) * 1024 if match else None}


def statistics_totals(directory):
    totals = {}
    for path in sorted(directory.glob('*.json')):
        data = load(path)
        for key, value in data.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if key.startswith(('Sema.', 'Constraint solver', 'CSBindings.', 'CSDisjunction.',
                                   'PotentialBindings.', 'CSLookahead.', 'CSScheduling.')):
                    if key.split('.')[-1].startswith('Max'):
                        totals[key] = max(totals.get(key, 0), value)
                    else:
                        totals[key] = totals.get(key, 0) + value
    return totals


def trial(record, job, root, cache, timeout, environment, metrics=False):
    root.mkdir(parents=True, exist_ok=False)
    cache.mkdir(parents=True, exist_ok=True)
    (root / 'outputs').mkdir()
    argv = replay_arguments(job['argv'], cache, root / 'outputs')
    resource_dir = record['provenance'].get('resource_dir')
    if resource_dir and '-resource-dir' not in argv:
        argv += ['-resource-dir', resource_dir]
    if metrics:
        (root / 'stats').mkdir()
        argv += ['-stats-output-dir', str(root / 'stats')]
    command = [record['executable'], *argv]
    resource_file = root / 'resources.txt'
    timed = ['/usr/bin/time', '-l' if sys.platform == 'darwin' else '-v',
             '-o', str(resource_file), *command]
    child_env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'TMPDIR',
                'DEVELOPER_DIR', 'LANG', 'LC_ALL') if key in os.environ}
    child_env.update(environment)
    started = time.perf_counter()
    with (root / 'stdout.txt').open('wb') as out, (root / 'stderr.txt').open('wb') as err:
        process = subprocess.Popen(timed, cwd=job['cwd'], env=child_env,
                                   stdout=out, stderr=err, start_new_session=True)
        expired = threading.Event()

        def expire():
            import signal
            expired.set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        timer = threading.Timer(timeout, expire)
        timer.start()
        try:
            code = process.wait()
        finally:
            timer.cancel()
            timer.join()
        if expired.is_set():
            raise ValueError(f'Compiler timed out after {timeout}s: {job["id"]}')
    elapsed = time.perf_counter() - started
    resources = parse_resources(resource_file.read_text())
    result = {'job': job['id'], 'seconds': elapsed, 'exit_code': code,
              'stdout_sha256': digest(root / 'stdout.txt'),
              'stderr_sha256': digest(root / 'stderr.txt'),
              'load_average': list(os.getloadavg()), **resources}
    save(root / 'sample.json', result)
    if code != 0:
        raise ValueError(f'Compiler failed ({code}); inspect {root / "stderr.txt"}')
    if job.get('kind') == 'module-emission':
        modules = list((root / 'outputs').glob('*.swiftmodule'))
        if len(modules) != 1 or modules[0].stat().st_size == 0:
            raise ValueError('Module emission succeeded without producing one nonempty module')
        result['module_sha256'] = digest(modules[0])
    if metrics:
        result['counters'] = statistics_totals(root / 'stats')
    return result


def interval(values):
    rng = random.Random(0)
    estimates = sorted(statistics.median(rng.choices(values, k=len(values)))
                       for _ in range(5000))
    return [estimates[int(len(estimates) * p)] for p in (.025, .975)]


def summarize(samples, jobs, repetitions, control):
    speedups = []
    cpu_speedups = []
    for repetition in range(repetitions):
        by_arm = {arm: sum(s['seconds'] for s in samples if s['round'] == repetition
                           and s['arm'] == arm) for arm in ('baseline', 'candidate')}
        speedups.append(by_arm['baseline'] / by_arm['candidate'])
        round_samples = [s for s in samples if s['round'] == repetition]
        if all(s.get('user_seconds') is not None and s.get('system_seconds') is not None for s in round_samples):
            cpu = {arm: sum(s['user_seconds'] + s['system_seconds'] for s in round_samples if s['arm'] == arm)
                   for arm in ('baseline', 'candidate')}
            if cpu['baseline'] > 0 and cpu['candidate'] > 0:
                cpu_speedups.append(cpu['baseline'] / cpu['candidate'])
    cases = []
    semantic_equal = True
    modules_equal = True
    for job in jobs:
        by_arm = {arm: [s for s in samples if s['job'] == job['id'] and s['arm'] == arm]
                  for arm in ('baseline', 'candidate')}
        diagnostics = {s['stderr_sha256'] for s in by_arm['baseline'] + by_arm['candidate']}
        outputs = {s['stdout_sha256'] for s in by_arm['baseline'] + by_arm['candidate']}
        semantic_equal &= len(diagnostics) == 1 and len(outputs) == 1
        if job.get('kind') == 'module-emission':
            module_hashes = {s.get('module_sha256') for s in by_arm['baseline'] + by_arm['candidate']}
            modules_equal &= len(module_hashes) == 1 and None not in module_hashes
        cases.append({'job': job['id'], 'module': job['module'], 'kind': job.get('kind', 'typecheck'),
                      'median_seconds': {arm: statistics.median(s['seconds'] for s in v)
                                         for arm, v in by_arm.items()},
                      'peak_rss_bytes': {arm: max(s['peak_rss_bytes'] or 0 for s in v)
                                         for arm, v in by_arm.items()}})
    ci = interval(speedups)
    no_regression = all(c['median_seconds']['candidate'] <= c['median_seconds']['baseline'] * 1.05
                        for c in cases)
    memory_ok = all(c['peak_rss_bytes']['baseline'] > 0 and
                    c['peak_rss_bytes']['candidate'] > 0 and
                    c['peak_rss_bytes']['candidate'] <= c['peak_rss_bytes']['baseline'] * 1.05
                    for c in cases)
    return {'kind': 'control' if control else 'comparison',
            'scope': 'serial replay of captured frontend jobs; see each job kind',
            'whole_build_speedup_measured': False,
            'repetitions': repetitions, 'paired_speedups': speedups,
            'median_speedup': statistics.median(speedups),
            'speedup_95_percent_bootstrap_interval': ci,
            'control_precision_qualified_for_5_percent_changes': control and ci[0] >= .95 and ci[1] <= 1.05,
            'median_cpu_speedup': statistics.median(cpu_speedups) if cpu_speedups else None,
            'cpu_speedup_95_percent_bootstrap_interval': interval(cpu_speedups) if cpu_speedups else None,
            'diagnostics_and_stdout_equal': semantic_equal,
            'emitted_modules_equal': modules_equal,
            'no_case_median_regression_over_5_percent': no_regression,
            'no_peak_rss_regression_over_5_percent': memory_ok,
            'improvement_demonstrated': not control and semantic_equal and modules_equal and no_regression and memory_ok and ci[0] > 1.05,
            'cases': cases}


def run(manifest_path, baseline_path, candidate_path, destination,
        repetitions=7, warmups=2, timeout=300, control=False, only=None):
    if repetitions < 7 or warmups < 1:
        raise ValueError('Require at least seven paired rounds and one warmup per arm')
    manifest = load(manifest_path)
    records = {'baseline': load(baseline_path), 'candidate': load(candidate_path)}
    validate_pair(records['baseline'], records['candidate'], control)
    validate_capture_compiler(manifest, records['baseline'])
    verify_inputs(manifest)
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    jobs = [j for j in manifest['jobs'] if only is None or j['id'] in only]
    if not jobs:
        raise ValueError('No benchmark jobs selected')
    receipt = {'schema': SCHEMA, 'status': 'running', 'harness_sha256': digest(__file__),
               'manifest_sha256': digest(manifest_path),
               'input_set_sha256': manifest['input_set_sha256'], 'compilers': records,
               'host': {'platform': platform.platform(), 'machine': platform.machine(),
                        'hostname': platform.node(), 'cpus': os.cpu_count()},
               'environment': manifest['environment'], 'samples': [], 'metrics': {}}
    save(root / 'run.json', receipt)
    try:
        for phase, rounds in (('warmup', warmups), ('timing', repetitions), ('metrics', 1)):
            for n in range(rounds):
                arms = ('baseline', 'candidate') if n % 2 == 0 else ('candidate', 'baseline')
                for arm in arms:
                    for job in jobs:
                        verify_inputs(manifest)
                        verify_compiler(records[arm])
                        sample = trial(records[arm], job, root / phase / str(n) / arm / job['id'],
                                       root / 'caches' / arm, timeout, manifest['environment'],
                                       metrics=phase == 'metrics')
                        verify_inputs(manifest)
                        sample.update({'round': n, 'arm': arm})
                        if phase == 'timing':
                            receipt['samples'].append(sample)
                        elif phase == 'metrics':
                            receipt['metrics'].setdefault(arm, {})[job['id']] = sample
                        save(root / 'run.json', receipt)
                        print(f'{phase} {n + 1} {arm} {job["module"]}/{job["id"]}: '
                              f'{sample["seconds"]:.3f}s', flush=True)
        validate_pair(records['baseline'], records['candidate'], control)
        receipt['summary'] = summarize(receipt['samples'], jobs, repetitions, control)
        receipt['status'] = 'passed' if (receipt['summary']['diagnostics_and_stdout_equal'] and
                                       receipt['summary']['emitted_modules_equal']) else 'invalid'
        save(root / 'run.json', receipt)
        if receipt['status'] != 'passed':
            raise ValueError('Diagnostics, stdout, or emitted modules changed; review before any performance claim')
    except BaseException as error:
        receipt['status'] = 'invalid'
        receipt['error'] = str(error)
        save(root / 'run.json', receipt)
        raise
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--transcript', required=True)
    cap.add_argument('--pat-receipt', required=True)
    cap.add_argument('--module', action='append', required=True)
    cap.add_argument('--source', action='append', default=[])
    cap.add_argument('--kind', choices=['typecheck', 'module-emission', 'both'], default='typecheck')
    cap.add_argument('--out', required=True)
    rec = sub.add_parser('record-compiler')
    rec.add_argument('--compiler', required=True)
    rec.add_argument('--provenance')
    rec.add_argument('--out', required=True)
    bench = sub.add_parser('run')
    bench.add_argument('--manifest', required=True)
    bench.add_argument('--baseline', required=True)
    bench.add_argument('--candidate', required=True)
    bench.add_argument('--out', required=True)
    bench.add_argument('--control', action='store_true')
    bench.add_argument('--repetitions', type=int, default=7)
    bench.add_argument('--warmups', type=int, default=2)
    bench.add_argument('--timeout', type=int, default=300)
    bench.add_argument('--job', action='append')
    args = parser.parse_args()
    if args.command == 'capture':
        result = capture(args.transcript, args.pat_receipt, args.module, args.out, args.source, args.kind)
        print(f'Captured {len(result["jobs"])} jobs; {len(result["inputs"])} pinned inputs')
    elif args.command == 'record-compiler':
        record_compiler(args.compiler, args.out, load(args.provenance) if args.provenance else None)
    else:
        result = run(args.manifest, args.baseline, args.candidate, args.out,
                     args.repetitions, args.warmups, args.timeout, args.control, args.job)
        print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
