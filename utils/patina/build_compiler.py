#!/usr/bin/env python3
"""Build a pinned, optimized compiler and retain an immutable benchmark binary.

Invoke through Pat's heavy-work queue. Source dependencies are sibling checkouts;
this script verifies them and never advances their revisions automatically.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from solver_bench import digest, load, output, record_compiler, save


def compiler_diff(root, revision):
    # Compiler entry points and generators also live outside lib/include.
    # Exclude only this measurement tool directory from the source record.
    paths = ['.', ':(exclude)utils/patina/**']
    untracked = output(['git', 'ls-files', '--others', '--exclude-standard', '--', *paths], root)
    if untracked:
        raise ValueError('Stage new source files before building: ' + untracked.splitlines()[0])
    return output(['git', 'diff', '--binary', revision, '--', *paths], root)


def retain_runtime_libraries(build, destination):
    relative = Path('lib/swift/host/compiler')
    source = build / relative
    libraries = sorted(source.glob('*.dylib'))
    if not libraries:
        raise ValueError('Compiler SwiftSyntax runtime libraries were not built')
    retained = destination / relative
    retained.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for library in libraries:
        target = retained / library.name
        shutil.copy2(library, target, follow_symlinks=True)
        hashes[str(relative / library.name)] = digest(target)
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    lock_path = Path(__file__).with_name('dependencies.json')
    lock = load(lock_path)
    if not 1 <= args.jobs <= 16:
        raise ValueError('--jobs must be between 1 and 16, within the granted Pat core budget')
    if os.environ.get('PAT_JOB_CORES'):
        args.jobs = min(args.jobs, max(1, int(float(os.environ['PAT_JOB_CORES']))))
    for repo, revision in lock['repositories'].items():
        checkout = root.parent / repo
        if output(['git', 'rev-parse', 'HEAD'], checkout) != revision:
            raise ValueError(f'Dependency revision mismatch: {repo}')
        if output(['git', 'status', '--porcelain', '--untracked-files=no'], checkout):
            raise ValueError(f'Dependency has tracked edits: {repo}')
    output(['git', 'cat-file', '-e', lock['upstream_revision']], root)
    developer = Path(output(['xcode-select', '-p']))
    tools = developer / 'Toolchains/XcodeDefault.xctoolchain/usr/bin'
    options = [
        '--release', '--assertions', '--bootstrapping=hosttools',
        '--skip-build-osx', '--skip-build-benchmarks', '--skip-early-swift-driver',
        '--skip-build-compiler-rt', '--skip-build-clang-tools-extra', '--skip-build-lld',
        '--llvm-targets-to-build=AArch64', '--enable-new-runtime-build=false',
        '--native-swift-tools-path=' + str(tools), '--no-swift-pedantic-diagnostics',
        '--extra-swift-cmake-options=-DSWIFT_APPEND_VC_REV=OFF',
        '--build-subdir=patina-solver', '--jobs=' + str(args.jobs),
    ]
    destination = Path(args.out).resolve()
    if destination.exists():
        raise ValueError(f'Refuse to overwrite a retained compiler: {destination}')
    command = [sys.executable, str(root / 'utils/build-script'), *options]
    if args.dry_run:
        subprocess.run([*command, '--dry-run'], cwd=root, check=True)
        return
    destination.mkdir(parents=True)
    source_diff = compiler_diff(root, lock['upstream_revision'])
    if args.name == 'upstream' and source_diff:
        raise ValueError('The upstream baseline must have no compiler-source changes')
    source_head = output(['git', 'rev-parse', 'HEAD'], root)
    provenance = {
        'kind': 'source-build', 'name': args.name, 'upstream_revision': lock['upstream_revision'],
        'source_revision': source_head,
        'compiler_source_diff': source_diff,
        'dependency_lock_sha256': digest(lock_path),
        'build_options': [o for o in options if not o.startswith('--jobs=')],
        'build_jobs': args.jobs,
        'host_swift_version': output([str(tools / 'swiftc'), '--version']),
        'host_clang_version': output([str(tools / 'clang'), '--version']),
        'resource_dir': str(tools.parent / 'lib/swift'),
    }
    save(destination / 'build.json', {'status': 'running', **provenance})
    with (destination / 'build.log').open('w') as log:
        process = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    if process.returncode:
        save(destination / 'build.json', {'status': 'failed', 'exit_code': process.returncode, **provenance})
        raise ValueError(f'Compiler build failed; inspect {destination / "build.log"}')
    current_diff = compiler_diff(root, lock['upstream_revision'])
    if current_diff != source_diff:
        raise ValueError('Compiler sources changed during the build')
    build = root.parent / 'build/patina-solver/swift-macosx-arm64'
    executable = build / 'bin/swift-frontend'
    retained = destination / 'bin/swift-frontend'
    retained.parent.mkdir()
    shutil.copy2(executable, retained)
    provenance['runtime_libraries'] = retain_runtime_libraries(build, destination)
    # Keep the frontend's resource lookup explicit: a retained standalone binary
    # does not live inside an installed .xctoolchain. SDK interfaces are rebuilt
    # into benchmark-owned module caches by the replay runner.
    provenance['cmake_cache_sha256'] = digest(build / 'CMakeCache.txt')
    provenance['linked_libraries'] = output(['otool', '-L', str(retained)])
    record_compiler(retained, destination / 'compiler.json', provenance)
    save(destination / 'build.json', {'status': 'passed', **provenance})
    print(destination / 'compiler.json')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
