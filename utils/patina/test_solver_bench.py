import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import solver_bench as bench
import synthetic_workloads


class BenchmarkTests(unittest.TestCase):
    def frontend(self, root):
        compiler = root / 'swift-frontend'
        compiler.write_text('#!/bin/sh\necho "Test Swift"\n')
        compiler.chmod(0o755)
        return compiler

    def test_replay_keeps_primary_files_and_read_inputs_but_removes_every_output(self):
        words = ['/tools/swift-frontend', '-frontend', '-c', '-primary-file',
                 '/source/a.swift', '/source/b.swift', '-o', '/lane/a.o',
                 '-supplementary-output-file-map', '/lane/outputs.json',
                 '-emit-dependencies-path', '/lane/a.d', '-serialize-diagnostics-path',
                 '/lane/a.dia', '-index-unit-output-path', '/lane/a.o',
                 '-debug-time-function-bodies', '-warn-long-function-bodies=100',
                 '-explicit-swift-module-map-file', '/lane/modules.json',
                 '-import-objc-header', '/source/bridge.h', '-module-name', 'Angles']
        result = bench.typecheck_arguments(words)
        self.assertEqual(result, ['-frontend', '-typecheck', '-primary-file',
                                 '/source/a.swift', '/source/b.swift',
                                 '-explicit-swift-module-map-file', '/lane/modules.json',
                                 '-import-objc-header', '/source/bridge.h',
                                 '-module-name', 'Angles'])

    def test_unknown_output_producer_refuses_replay(self):
        with self.assertRaisesRegex(ValueError, 'unsafe replay'):
            bench.typecheck_arguments(['swift-frontend', '-frontend', '-c',
                                       '-emit-future-artifact', '/lane/output'])

    def test_module_emission_redirects_all_outputs_into_trial_scratch(self):
        args = bench.module_arguments(['/tools/swift-frontend', '-frontend', '-emit-module',
                  '/source/a.swift', '-o', '/lane/Angles.swiftmodule',
                  '-emit-module-doc-path', '/lane/Angles.swiftdoc',
                  '-emit-dependencies-path', '/lane/Angles.d', '-module-name', 'Angles'])
        replay = bench.replay_arguments(args, Path('/scratch/cache'), Path('/scratch/trial'))
        self.assertIn('-emit-module', replay)
        self.assertFalse(any(word.startswith('/lane') for word in replay))
        self.assertIn('/scratch/trial/4-Angles.swiftmodule', replay)

    def test_source_file_list_and_every_secondary_source_are_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'secondary source.swift'
            source.write_text('struct Secondary {}')
            listing = root / 'files.SwiftFileList'
            listing.write_text(str(source) + '\n')
            inputs = bench.job_inputs(['-filelist', str(listing)], root)
            self.assertEqual(inputs, {source, listing})
            manifest = {'inputs': {str(p): bench.digest(p) for p in inputs}}
            source.write_text('struct Changed {}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                bench.verify_inputs(manifest)

    def test_recursive_response_files_refuse_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'args'
            path.write_text('@args')
            with self.assertRaisesRegex(ValueError, 'Recursive response'):
                bench.expand_response_files(['@args'], directory, set())

    def test_explicit_module_contents_are_pinned_and_mutations_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / 'Dep.swiftmodule'
            module.write_bytes(b'original module')
            mapping = root / 'modules.json'
            mapping.write_text(json.dumps([{'modulePath': str(module)}]))
            inputs = bench.job_inputs(['-explicit-swift-module-map-file', str(mapping)], directory)
            self.assertIn(module.resolve(), inputs)
            manifest = {'inputs': {str(p): bench.digest(p) for p in inputs}}
            bench.verify_inputs(manifest)
            module.write_bytes(b'rebuilt by another job')
            with self.assertRaisesRegex(ValueError, 'changed'):
                bench.verify_inputs(manifest)

    def test_capture_uses_working_directory_and_deduplicates_exact_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'My View.swift'
            source.write_text('let value = 1\n')
            receipt = root / 'receipt.json'
            bench.save(receipt, {'ok': True, 'result': {'productPath': '/product/Angles.app'}})
            compiler = self.frontend(root)
            line = f'{compiler} -frontend -c -primary-file "{source}" -module-name Angles -o /lane/view.o'
            transcript = root / 'transcript.log'
            transcript.write_text(f'    cd "{root}"\n{line}\n{line}\n')
            manifest = bench.capture(transcript, receipt, ['Angles'], root / 'manifest.json')
            self.assertEqual(len(manifest['jobs']), 1)
            self.assertEqual(manifest['jobs'][0]['primaries'], [str(source)])
            self.assertIn(str(source.resolve()), manifest['inputs'])
            self.assertEqual(manifest['captured_compilers'][str(compiler.resolve())]['version'], 'Test Swift')

    def test_primary_filelist_capture_keeps_selection_and_source_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            compiler = self.frontend(root)
            source = root / 'primary.swift'
            source.write_text('let value = 1')
            listing = root / 'primaries'
            listing.write_text(str(source) + '\n')
            bench.save(root / 'receipt', {'ok': True, 'result': {'productPath': '/product'}})
            (root / 'log').write_text(f'cd {root}\n{compiler} -frontend -c '
                f'-primary-filelist {listing} -module-name Angles\n')
            result = bench.capture(root / 'log', root / 'receipt', ['Angles'],
                                   root / 'manifest', sources=['primary.swift'])
            self.assertEqual(result['jobs'][0]['primaries'], [str(source)])
            self.assertIn(str(source), result['inputs'])
            self.assertIn(str(listing), result['inputs'])

    def test_replay_rejects_capture_from_another_compiler_before_timing(self):
        record = {'sha256': 'baseline', 'version': 'Swift baseline'}
        manifest = {'kind': 'frontend-replay', 'captured_compilers': {
            '/compiler': {'sha256': 'baseline', 'version': 'Swift baseline'}}}
        bench.validate_capture_compiler(manifest, record)
        manifest['captured_compilers']['/compiler']['version'] = 'Swift other'
        with self.assertRaisesRegex(ValueError, 'Captured compiler version'):
            bench.validate_capture_compiler(manifest, record)
        manifest['captured_compilers']['/compiler'] = {'sha256': 'other', 'version': 'Swift baseline'}
        with self.assertRaisesRegex(ValueError, 'Captured compiler version'):
            bench.validate_capture_compiler(manifest, record)

    def test_failed_build_is_not_an_accepted_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'a.swift'
            source.write_text('let x = 1')
            bench.save(root / 'receipt.json', {'status': 'failed'})
            (root / 'log').write_text(f'cd {root}\n/tools/swift-frontend -frontend -c '
                                    f'-primary-file {source} -module-name Angles\n')
            with self.assertRaisesRegex(ValueError, 'did not pass'):
                bench.capture(root / 'log', root / 'receipt.json', ['Angles'], root / 'manifest')

    def test_keyed_joined_module_and_pcm_inputs_are_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            module = root / 'Some=Module.swiftmodule'
            pcm = root / 'Clang.pcm'
            module.write_bytes(b'module')
            pcm.write_bytes(b'pcm')
            inputs = bench.job_inputs(['-swift-module-file=Named=' + str(module),
                                       '-Xcc', '-fmodule-file=Clang=' + str(pcm)], root)
            self.assertEqual(inputs, {module, pcm})

    def test_resolved_plugin_pins_library_and_separate_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            library, server = root / 'Macros.dylib', root / 'swift-plugin-server'
            library.write_bytes(b'library')
            server.write_bytes(b'server')
            inputs = bench.job_inputs(['-load-resolved-plugin', f'{library}#{server}#Macros'], root)
            self.assertEqual(inputs, {library, server})
            inputs = bench.job_inputs(['-load-resolved-plugin', f'#{server}#Macros'], root)
            self.assertEqual(inputs, {server})

    def test_import_search_module_changes_and_new_modules_invalidate_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            module = root / 'Dependency.swiftmodule'
            module.write_bytes(b'original')
            manifest = {'search_roots': {str(root): bench.search_modules(root)},
                        'inputs': {str(module): bench.digest(module)}}
            bench.verify_inputs(manifest)
            module.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                bench.verify_inputs(manifest)
            module.write_bytes(b'original')
            (root / 'New.swiftmodule').write_bytes(b'new')
            with self.assertRaisesRegex(ValueError, 'membership'):
                bench.verify_inputs(manifest)

    def test_filtering_happens_after_response_file_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'a.swift'
            source.write_text('let x = 1')
            response = root / 'arguments'
            compiler = self.frontend(root)
            response.write_text(f'-frontend -c -primary-file {source} -module-name Angles')
            bench.save(root / 'receipt', {'ok': True, 'result': {'productPath': '/product'}})
            (root / 'log').write_text(f'cd {root}\n{compiler} @{response}\n')
            result = bench.capture(root / 'log', root / 'receipt', ['Angles'], root / 'manifest')
            self.assertEqual(len(result['jobs']), 1)
            self.assertIn(str(response), result['inputs'])

    def test_reference_compilers_cannot_masquerade_as_matching_source_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = root / 'a'
            b = root / 'b'
            a.write_bytes(b'compiler A')
            b.write_bytes(b'compiler B')
            ra = {'executable': str(a), 'sha256': bench.digest(a), 'provenance': {}}
            rb = {'executable': str(b), 'sha256': bench.digest(b), 'provenance': {}}
            with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
                bench.validate_pair(ra, rb)
            with self.assertRaisesRegex(ValueError, 'same compiler'):
                bench.validate_pair(ra, rb, control=True)
            bench.validate_pair(ra, ra, control=True)
            with self.assertRaisesRegex(ValueError, 'Identical compiler'):
                bench.validate_pair(ra, ra)

    def test_host_toolchain_mismatch_and_modified_baselines_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('a', 'b'):
                (root / name).write_text(name)
            provenance = {'upstream_revision': 'base', 'dependency_lock_sha256': 'pins',
                          'build_options': ['--release'], 'host_swift_version': 'Swift host',
                          'host_clang_version': 'Clang host', 'resource_dir': '/resources',
                          'compiler_source_diff': ''}
            a = {'executable': str(root / 'a'), 'sha256': bench.digest(root / 'a'),
                 'provenance': dict(provenance)}
            b = {'executable': str(root / 'b'), 'sha256': bench.digest(root / 'b'),
                 'provenance': dict(provenance, compiler_source_diff='scheduling patch')}
            bench.validate_pair(a, b)
            b['provenance']['host_swift_version'] = 'Different Swift host'
            with self.assertRaisesRegex(ValueError, 'host_swift_version'):
                bench.validate_pair(a, b)
            b['provenance']['host_swift_version'] = provenance['host_swift_version']
            a['provenance']['compiler_source_diff'] = 'modified baseline'
            with self.assertRaisesRegex(ValueError, 'unmodified upstream'):
                bench.validate_pair(a, b)

    def samples(self, candidate_seconds=1, stderr='same', rss=100):
        return [{'round': i, 'arm': arm, 'job': 'job',
                 'seconds': 2 if arm == 'baseline' else candidate_seconds,
                 'peak_rss_bytes': 100 if arm == 'baseline' else rss,
                 'stderr_sha256': 'same' if arm == 'baseline' else stderr,
                 'stdout_sha256': 'same'}
                for i in range(7) for arm in ('baseline', 'candidate')]

    def test_diagnostic_change_or_memory_regression_blocks_a_speedup_claim(self):
        jobs = [{'id': 'job', 'module': 'Angles'}]
        summary = bench.summarize(self.samples(), jobs, 7, False)
        self.assertTrue(summary['improvement_demonstrated'])
        self.assertFalse(bench.summarize(self.samples(stderr='different'), jobs, 7, False)
                         ['improvement_demonstrated'])
        self.assertFalse(bench.summarize(self.samples(rss=120), jobs, 7, False)
                         ['improvement_demonstrated'])
        self.assertFalse(bench.summarize(self.samples(), jobs, 7, True)
                         ['improvement_demonstrated'])

    def test_different_emitted_module_blocks_a_speedup_claim(self):
        jobs = [{'id': 'job', 'module': 'Angles', 'kind': 'module-emission'}]
        samples = self.samples()
        for sample in samples:
            sample['module_sha256'] = sample['arm']
        summary = bench.summarize(samples, jobs, 7, False)
        self.assertFalse(summary['emitted_modules_equal'])
        self.assertFalse(summary['improvement_demonstrated'])

    def test_counters_exclude_timers_and_keep_operation_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            bench.save(Path(directory) / 'stats.json', {'Sema.NumConstraintScopes': 10,
                       'time.swift.typecheck': 1.5, 'CSBindings.NumBindingSetsSkipped': 7})
            self.assertEqual(bench.statistics_totals(Path(directory)),
                             {'Sema.NumConstraintScopes': 10, 'CSBindings.NumBindingSetsSkipped': 7})

    def test_cache_rewrite_does_not_touch_captured_lane_cache(self):
        original = ['-module-cache-path', '/lane/cache', '-typecheck', 'a.swift']
        result = bench.replay_arguments(original, Path('/scratch'))
        self.assertEqual(result[1], '/scratch/module-cache')
        self.assertEqual(original[1], '/lane/cache')
        self.assertEqual(bench.replay_arguments(['-typecheck', 'a.swift'], Path('/scratch'))[-2:],
                         ['-module-cache-path', '/scratch/module-cache'])

    def test_synthetic_capture_pins_sdk_and_resource_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sdk = root / 'sdk'
            sdk_module = sdk / 'usr/lib/swift/SDK.swiftmodule'
            sdk_module.parent.mkdir(parents=True)
            sdk_module.write_bytes(b'sdk module')
            resources = root / 'resources'
            resources.mkdir()
            module = resources / 'Swift.swiftmodule'
            module.write_bytes(b'stdlib')
            destination = root / 'generated'
            with patch('sys.argv', ['synthetic_workloads.py', '--out', str(destination),
                                   '--sdk', str(sdk), '--resource-dir', str(resources), '--size', '8']):
                synthetic_workloads.main()
            manifest = bench.load(destination / 'manifest.json')
            self.assertIn(str(sdk_module), manifest['inputs'])
            self.assertIn(str(module), manifest['inputs'])
            bench.verify_inputs(manifest)
            module.write_bytes(b'changed stdlib')
            with self.assertRaisesRegex(ValueError, 'changed'):
                bench.verify_inputs(manifest)

    def test_control_executes_real_processes_and_records_no_improvement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            compiler = root / 'fake-frontend'
            compiler.write_text('#!/bin/sh\nexit 0\n')
            compiler.chmod(0o755)
            record = {'executable': str(compiler), 'sha256': bench.digest(compiler),
                      'version': 'Test Swift', 'provenance': {}}
            bench.save(root / 'compiler.json', record)
            bench.save(root / 'manifest.json', {'inputs': {}, 'input_set_sha256': 'test',
                       'environment': {}, 'jobs': [{'id': 'job', 'module': 'Fixture',
                       'cwd': str(root), 'argv': []}]})
            result = bench.run(root / 'manifest.json', root / 'compiler.json',
                               root / 'compiler.json', root / 'run', warmups=1, control=True)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(len(result['samples']), 14)
            self.assertFalse(result['summary']['improvement_demonstrated'])
            self.assertTrue((root / 'run/metrics/0/baseline/job/sample.json').is_file())

    def test_failed_process_leaves_an_invalid_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            compiler = root / 'fake-frontend'
            compiler.write_text('#!/bin/sh\nexit 3\n')
            compiler.chmod(0o755)
            bench.save(root / 'compiler.json', {'executable': str(compiler),
                       'sha256': bench.digest(compiler), 'provenance': {}})
            bench.save(root / 'manifest.json', {'inputs': {}, 'input_set_sha256': 'test',
                       'environment': {}, 'jobs': [{'id': 'job', 'module': 'Fixture',
                       'cwd': str(root), 'argv': []}]})
            with self.assertRaisesRegex(ValueError, 'Compiler failed'):
                bench.run(root / 'manifest.json', root / 'compiler.json',
                          root / 'compiler.json', root / 'run', warmups=1, control=True)
            self.assertEqual(bench.load(root / 'run/run.json')['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()
