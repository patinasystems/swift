from pathlib import Path
import tempfile
import unittest

import angles_build_bench as angles
import prepare_toolchain
import solver_bench as bench


class AnglesBenchmarkTests(unittest.TestCase):
    def test_incremental_samples_require_the_seeded_lane_and_affinity(self):
        state = {'buildStatePath': '/lane', 'laneId': 'lane-1', 'cacheKey': 'key',
                 'configuration': 'Debug', 'scheme': 'Angles', 'sdk': 'iphonesimulator',
                 'cacheState': 'hit', 'buildDisposition': 'incremental'}
        angles.validate_warm_state(state, dict(state))
        with self.assertRaisesRegex(ValueError, 'continuity'):
            angles.validate_warm_state(state, dict(state, buildStatePath='/other-lane'))
        with self.assertRaisesRegex(ValueError, 'affinity'):
            angles.validate_warm_state(state, dict(state, cacheState='miss'))

    def test_body_edit_preserves_api_and_api_edit_changes_it(self):
        seed = angles.fixture('body-edit')
        body = angles.fixture('body-edit', True)
        api = angles.fixture('api-edit', True)
        self.assertIn('return 1', seed)
        self.assertIn('return 2', body)
        self.assertIn('var value: Int {', seed)
        self.assertIn('var value: Int {', body)
        self.assertIn('var value: Int64 {', api)
        self.assertIn('Int64(PatinaSolverBenchmarkAPI.value)', angles.CONSUMER)

    def test_deliberate_error_has_a_specific_diagnostic_marker(self):
        self.assertNotIn('missingPatinaSolverBenchmarkValue', angles.fixture('error'))
        self.assertIn('missingPatinaSolverBenchmarkValue', angles.fixture('error', True))

    def test_toolchain_wrapper_selects_frontend_and_quotes_paths(self):
        with tempfile.TemporaryDirectory(prefix='solver toolchain ') as directory:
            root = Path(directory).resolve()
            original = root / 'original/usr'
            (original / 'bin').mkdir(parents=True)
            (original / 'lib/swift').mkdir(parents=True)
            for name in ('swiftc', 'swift', 'clang'):
                (original / 'bin' / name).write_text('fake host tool')
            frontend = root / 'retained frontend'
            frontend.write_text('#!/bin/sh\nprintf "Actual frontend version\\n"\n')
            frontend.chmod(0o755)
            compiler = root / 'compiler.json'
            bench.save(compiler, {'executable': str(frontend), 'sha256': bench.digest(frontend),
                       'version': 'Test Swift', 'provenance': {'resource_dir': str(original / 'lib/swift')}})
            result = prepare_toolchain.prepare(compiler, root / 'Experiment.xctoolchain')
            wrapper = (root / 'Experiment.xctoolchain/usr/bin/swiftc').read_text()
            self.assertIn('-driver-use-frontend-path', wrapper)
            self.assertIn("'" + str(frontend) + "'", wrapper)
            self.assertIsNone(result['registration'])
            version = bench.output([str(root / 'Experiment.xctoolchain/usr/bin/swiftc'), '--version'])
            self.assertIn('Actual frontend version', version)
            self.assertIn(bench.digest(frontend), version)
            self.assertEqual((root / 'Experiment.xctoolchain/usr/bin/clang').resolve(),
                             original / 'bin/clang')


if __name__ == '__main__':
    unittest.main()
