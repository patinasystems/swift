#!/usr/bin/env python3
"""Generate public scaling controls; these supplement actual application jobs."""

import argparse
from pathlib import Path
import subprocess
import sys

from solver_bench import digest, object_digest, output, save, search_modules


PRELUDE = '''struct Number {
    let value: Int
}
func + (lhs: Number, rhs: Number) -> Number { Number(value: lhs.value + rhs.value) }
func + (lhs: Number, rhs: Int) -> Number { Number(value: lhs.value + rhs) }
'''


def workload(kind, size):
    if kind == 'overload-chain':
        return PRELUDE + 'func check(_ n: Number) -> Number {\n  ' + ' + '.join(['n'] * size) + '\n}\n'
    if kind == 'generic-nesting':
        expr = 'n'
        for _ in range(size):
            expr = 'unwrap(wrap(' + expr + '))'
        return ('struct Box<T> { let value: T }\n'
                'func wrap<T>(_ value: T) -> Box<T> { Box(value: value) }\n'
                'func unwrap<T>(_ box: Box<T>) -> T { box.value }\n'
                'func check(_ n: Int) -> Int { ' + expr + ' }\n')
    raise ValueError('Unknown synthetic workload kind')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--sdk', help='macOS SDK path; defaults to an xcrun metadata query')
    parser.add_argument('--resource-dir', help='Swift resource directory; defaults to Xcode')
    parser.add_argument('--target', default='arm64-apple-macosx13.0')
    parser.add_argument('--size', type=int, action='append')
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sdk = Path(args.sdk or output(['xcrun', '--sdk', 'macosx', '--show-sdk-path']))
    resource_dir = Path(args.resource_dir or
                        str(Path(output(['xcrun', '--find', 'swiftc'])).parent.parent / 'lib/swift'))
    jobs, inputs = [], {}
    for kind in ('overload-chain', 'generic-nesting'):
        for size in args.size or [8, 16, 32]:
            if not 2 <= size <= 128:
                raise ValueError('Synthetic sizes must be between 2 and 128')
            source = root / f'{kind}-{size}.swift'
            source.write_text(workload(kind, size))
            inputs[str(source)] = digest(source)
            jobs.append({'id': source.stem, 'module': 'SolverScaling', 'kind': 'typecheck',
                         'cwd': str(root), 'primaries': [str(source)], 'size': size,
                         'argv': ['-frontend', '-typecheck', '-primary-file', str(source),
                                  '-module-name', 'SolverScaling', '-target', args.target,
                                  '-sdk', str(sdk), '-resource-dir', str(resource_dir),
                                  '-module-cache-path', '{cache}']})
    for path in (sdk / 'SDKSettings.json', sdk / 'SDKSettings.plist'):
        if path.is_file():
            inputs[str(path)] = digest(path)
    search_roots = {str(p): search_modules(p) for p in (resource_dir, sdk / 'usr/lib/swift')}
    for state in search_roots.values():
        for path in state['files'].values():
            inputs[path] = digest(path)
    save(root / 'manifest.json', {'schema': 1, 'kind': 'synthetic-scaling-controls',
         'search_roots': search_roots,
         'jobs': jobs, 'inputs': inputs, 'input_set_sha256': object_digest(inputs),
         'environment': {'SWIFT_DETERMINISTIC_HASHING': '1'}})
    print(root / 'manifest.json')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
