"""Provider drift checks, including configured state versus stale deployment state."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("watch_gate", Path(__file__).parents[1] / "verify_staging_runtime_watches.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RuntimeWatchGateTest(unittest.TestCase):
    def setUp(self):
        self.expected = ["/VERSION", "lyricgen/frontend/**"]
        self.records = {
            name: {"railwayConfigFile": None if name in gate.BATCH else f"/railway/{stem}.toml",
                   "watchPatterns": self.expected.copy(),
                   "latestDeployment": {"status": "SUCCESS", "meta": {"commitHash": "new",
                       "serviceManifest": {"build": {"watchPatterns": self.expected.copy()}}}}}
            for name, stem in gate.CONFIGS.items()
        }

    def test_before_uses_persisted_batch_triggers_not_old_manifests(self):
        for record in self.records.values():
            record["latestDeployment"]["meta"]["serviceManifest"]["build"]["watchPatterns"] = ["old"]
        self.assertEqual(gate.verify(self.records, self.expected), [])
        self.assertTrue(gate.verify(self.records, self.expected, "after", "new"))

    def test_good_deployment_does_not_hide_stale_persisted_batch_list(self):
        self.records["BatchWorker"]["watchPatterns"] = ["old"]
        self.assertTrue(gate.verify(self.records, self.expected))

    def test_missing_service_or_wrong_binding_fails(self):
        records = copy.deepcopy(self.records)
        del records["quality-worker"]
        self.assertTrue(gate.verify(records, self.expected))
        self.records["api"]["railwayConfigFile"] = None
        self.assertTrue(gate.verify(self.records, self.expected))

    def test_after_requires_matching_release_status_and_effective_filters(self):
        self.assertEqual(gate.verify(self.records, self.expected, "after", "new"), [])
        self.assertTrue(gate.verify(self.records, self.expected, "after", "old"))
        self.records["ShortWorker"]["latestDeployment"]["status"] = "DEPLOYING"
        self.assertTrue(gate.verify(self.records, self.expected, "after", "new"))
