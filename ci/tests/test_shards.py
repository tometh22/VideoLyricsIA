import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.shards import owner, verify


class CoverageGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        nodes = [f'tests/test_file_{i}.py::test_example[{j}]' for i in range(40) for j in range(2)]
        self.nodes = sorted(nodes)
        self.reports = [{'index': i, 'count': 4, 'commit': 'tested', 'collected': self.nodes,
                         'selected': [n for n in self.nodes if owner(n, 4) == i]} for i in range(4)]

    def write(self, reports):
        for i, report in enumerate(reports):
            (self.path / f'shard-{i}.json').write_text(json.dumps(report))

    def test_exact_union_and_parameter_variants_stay_together(self):
        self.write(self.reports)
        self.assertEqual(verify(self.path, 4, 'tested'), 80)
        for i in range(40):
            self.assertEqual(owner(f'tests/test_file_{i}.py::test_a', 4), owner(f'tests/test_file_{i}.py::test_b[1]', 4))

    def test_missing_failed_runner_cannot_pass(self):
        self.write(self.reports[:-1])
        with self.assertRaises(ValueError): verify(self.path, 4, 'tested')

    def test_tampered_or_incomplete_reports_fail(self):
        for mutation in ['omission', 'duplicate', 'foreign', 'inventory', 'commit', 'index']:
            with self.subTest(mutation=mutation):
                reports = copy.deepcopy(self.reports)
                if mutation == 'omission': reports[0]['selected'].pop()
                if mutation == 'duplicate': reports[0]['selected'].append(reports[0]['selected'][0])
                if mutation == 'foreign': reports[0]['selected'].append(reports[1]['selected'][0])
                if mutation == 'inventory': reports[0]['collected'].pop()
                if mutation == 'commit': reports[0]['commit'] = 'untested'
                if mutation == 'index': reports[0]['index'] = 1
                self.write(reports)
                with self.assertRaises(ValueError): verify(self.path, 4, 'tested')

    def test_real_pytest_preserves_marker_filtering_and_failure_exit(self):
        suite = self.path / 'suite'
        suite.mkdir()
        (suite / 'pytest.ini').write_text('[pytest]\nmarkers = omitted: intentional exclusion\n')
        for i in range(16):
            (suite / f'test_example_{i}.py').write_text('import pytest\n@pytest.mark.parametrize("n", [1, 2])\ndef test_ok(n): assert n > 0\n@pytest.mark.omitted\ndef test_excluded(): assert False\n')
        env = dict(os.environ, PYTHONPATH=str(ROOT), GITHUB_SHA='tested', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        for index in range(4):
            result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'ci.pytest_shard', str(suite),
                '-m', 'not omitted', '--ci-shard-count=4', f'--ci-shard-index={index}',
                f'--ci-shard-report={self.path / f"shard-{index}.json"}', '-q'], env=env, cwd=suite, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(verify(self.path, 4, 'tested'), 32)
        (suite / 'test_failure.py').write_text('def test_failure(): assert False\n')
        result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'ci.pytest_shard', str(suite / 'test_failure.py'),
            f'--ci-shard-report={self.path / "failure.json"}', '-q'], env=env, cwd=suite, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
