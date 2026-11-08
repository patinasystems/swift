#!/usr/bin/env python3
"""Register an explicitly selected experimental toolchain; never change defaults."""

import argparse
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

from solver_bench import digest, load, object_digest, output, save, verify_compiler


def validate_selection(record, selection):
    verify_compiler(record)
    toolchain = Path(selection['path'])
    registration = Path(selection['registration'])
    if not registration.is_symlink() or registration.resolve() != toolchain.resolve():
        raise ValueError('Experimental toolchain registration changed')
    checks = [(toolchain / 'usr/bin/swiftc', selection['swiftc_wrapper_sha256']),
              (toolchain / 'Info.plist', selection['info_plist_sha256']),
              (Path(record['provenance']['resource_dir']).parents[1] / 'bin/swiftc',
               selection['driver_sha256'])]
    if any(digest(path) != expected for path, expected in checks):
        raise ValueError('Experimental toolchain wrapper, driver, or metadata changed')
    found = Path(output(['xcrun', '--toolchain', selection['identifier'], '--find', 'swiftc']))
    if found.resolve() != (toolchain / 'usr/bin/swiftc').resolve():
        raise ValueError('Experimental toolchain selection changed')
    if (toolchain / 'usr/bin/swift-frontend').resolve() != Path(record['executable']).resolve():
        raise ValueError('Experimental toolchain frontend changed')


def prepare(record_path, destination, register=False):
    record = load(record_path)
    verify_compiler(record)
    resource_dir = Path(record['provenance']['resource_dir'])
    original = resource_dir.parents[1]
    driver = original / 'bin/swiftc'
    identity = object_digest({'frontend': record['sha256'], 'driver': digest(driver),
                              'original': str(original),
                              'preparation_tool_sha256': digest(__file__)})[:16]
    identifier = 'com.patinasystems.swift.solver.' + identity
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    binary_dir = destination / 'usr/bin'
    binary_dir.mkdir(parents=True)
    (destination / 'usr/lib').symlink_to(original / 'lib', target_is_directory=True)
    for entry in (original / 'bin').iterdir():
        if entry.name not in ('swift', 'swiftc', 'swift-frontend'):
            (binary_dir / entry.name).symlink_to(entry)
    (binary_dir / 'swift-frontend').symlink_to(record['executable'])
    for name in ('swift', 'swiftc'):
        wrapper = binary_dir / name
        # Pat and Xcode query swiftc's version to identify the toolchain. The
        # wrapped driver would otherwise report Xcode's version for both arms.
        version_probe = ('#!/bin/sh\nif [ "$#" -eq 1 ] && '
                         '{ [ "$1" = "--version" ] || [ "$1" = "-version" ]; }; then\n  ' +
                         shlex.quote(record['executable']) + ' --version || exit "$?"\n  ' +
                         "printf '%s\\n' " + shlex.quote('Patina solver frontend SHA256: ' + record['sha256']) +
                         '\n  exit 0\nfi\n')
        wrapper.write_text(version_probe + 'exec ' + shlex.quote(str(original / 'bin' / name)) +
                           ' -driver-use-frontend-path ' + shlex.quote(record['executable']) +
                           ' -resource-dir ' + shlex.quote(str(resource_dir)) + ' "$@"\n')
        wrapper.chmod(0o755)
    info = {'CFBundleIdentifier': identifier, 'CFBundleName': 'Patina Swift Solver Experiment',
            'CFBundleVersion': '1', 'CompatibilityVersion': 2,
            'CompatibilityVersionDisplayString': 'Xcode 27',
            'DisplayName': 'Patina Swift Solver ' + identity,
            'Identifier': identifier, 'ShortDisplayName': 'Patina Swift Solver',
            'Version': '1.0'}
    (destination / 'Info.plist').write_bytes(plistlib.dumps(info))
    save(destination / 'patina-compiler.json', record)
    installed = None
    if register:
        installed = Path.home() / 'Library/Developer/Toolchains' / ('PatinaSolver-' + identity + '.xctoolchain')
        installed.parent.mkdir(parents=True, exist_ok=True)
        if installed.exists() or installed.is_symlink():
            raise ValueError(f'Refuse to replace an existing registration: {installed}')
        installed.symlink_to(destination, target_is_directory=True)
        try:
            found = Path(output(['xcrun', '--toolchain', identifier, '--find', 'swiftc']))
            if found.resolve() != (binary_dir / 'swiftc').resolve():
                raise ValueError('xcrun did not resolve the experimental compiler wrapper')
            frontend = Path(output(['xcrun', '--toolchain', identifier, '--find', 'swift-frontend']))
            if frontend.resolve() != Path(record['executable']).resolve():
                raise ValueError('xcrun did not select the retained frontend')
            # Driver --version reports the driver, not necessarily the selected
            # frontend. Inspect its planned typecheck job instead, without
            # compiling or writing into any application build state.
            probe = destination / 'frontend-selection-probe.swift'
            probe.write_text('// Toolchain selection probe.\n')
            try:
                trace = output(['xcrun', '--toolchain', identifier, 'swiftc',
                                '-###', '-typecheck', str(probe)])
                planned = [shlex.split(line) for line in trace.splitlines() if line.strip()]
                if not any(words and Path(words[0]).resolve() == frontend.resolve()
                           and '-frontend' in words for words in planned):
                    raise ValueError('The selected driver did not plan the retained frontend')
            finally:
                probe.unlink()
        except BaseException:
            if installed.is_symlink() and installed.resolve() == destination:
                installed.unlink()
            raise
    result = {'identifier': identifier, 'path': str(destination),
              'registration': str(installed) if installed else None,
              'compiler_sha256': record['sha256'], 'driver_sha256': digest(driver),
              'swiftc_wrapper_sha256': digest(binary_dir / 'swiftc'),
              'info_plist_sha256': digest(destination / 'Info.plist')}
    save(destination / 'selection.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler-record', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--register', action='store_true')
    args = parser.parse_args()
    result = prepare(args.compiler_record, args.out, args.register)
    print(result['identifier'])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
