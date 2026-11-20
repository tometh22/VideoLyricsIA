"""A SHA-bound quality runtime requires one deployment cohort.

Railway watch paths use gitignore semantics and repository-root paths, even
when a service builds from /lyricgen. Exercise the real Git matcher rather
than a second glob implementation. No Railway, database or queue calls.
"""
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
COHORT_CONFIGS = {
    "api": "railway/api.toml",
    "Worker": "railway/worker.toml",
    "ShortWorker": "railway/short-worker.toml",
    "BatchWorker": "railway/batch-worker.toml",
    "BatchShortWorker": "railway/batch-short-worker.toml",
    "quality-worker": "railway/quality-worker.toml",
}


class ReleaseWatchContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="genly-watch-contract-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.repositories = {}
        cls.patterns = {}
        for service, config in COHORT_CONFIGS.items():
            with (ROOT / config).open("rb") as source:
                cls.patterns[service] = tomllib.load(source)["build"]["watchPatterns"]
            repo = Path(cls.temp.name) / service
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True,
                           capture_output=True, text=True)
            (repo / ".gitignore").write_text("\n".join(cls.patterns[service]) + "\n")
            cls.repositories[service] = repo

    def selected_services(self, paths):
        selected = set()
        for service, repo in self.repositories.items():
            result = subprocess.run(
                ["git", "-c", "core.excludesFile=/dev/null", "-c", "core.ignoreCase=false",
                 "-C", str(repo), "check-ignore", "--no-index", "--stdin", "-z"],
                input="\0".join(paths) + "\0", capture_output=True, text=True,
            )
            self.assertIn(result.returncode, (0, 1), result.stderr)
            if result.stdout:
                selected.add(service)
        return selected

    def assert_full_cohort(self, paths):
        self.assertSetEqual(self.selected_services(paths), set(COHORT_CONFIGS),
                            f"Partial deployment cohort for {paths!r}")

    def test_version_only_release_selects_entire_cohort(self):
        self.assert_full_cohort(["VERSION"])

    def test_frontend_only_release_selects_entire_cohort(self):
        self.assert_full_cohort([
            "VERSION", "CHANGELOG.md", "lyricgen/frontend/src/App.jsx",
            "lyricgen/frontend/package.json", "lyricgen/frontend/package-lock.json",
        ])

    def test_frontend_changes_without_bump_cannot_split_runtime_identity(self):
        for path in ["lyricgen/frontend/src/App.jsx", "lyricgen/frontend/package.json",
                     "lyricgen/frontend/package-lock.json", "lyricgen/frontend/vite.config.js",
                     "lyricgen/frontend/e2e/editor-inline-controls.spec.js"]:
            with self.subTest(path=path):
                self.assert_full_cohort([path])

    def test_backend_assets_and_both_dockerfiles_select_entire_cohort(self):
        for path in ["lyricgen/backend/queue_jobs.py", "lyricgen/backend/tests/test_quality_jobs.py",
                     "lyricgen/assets/background.png", "lyricgen/Dockerfile",
                     "lyricgen/Dockerfile.worker"]:
            with self.subTest(path=path):
                self.assert_full_cohort([path])

    def test_each_cohort_config_selects_every_consumer(self):
        for path in COHORT_CONFIGS.values():
            with self.subTest(path=path):
                self.assert_full_cohort([path])

    def test_consumers_keep_identical_ordered_watch_patterns(self):
        for service, patterns in self.patterns.items():
            with self.subTest(service=service):
                self.assertEqual(patterns, self.patterns["api"])

    def test_unrelated_docs_ci_and_independent_services_do_not_restart_cohort(self):
        for path in ["docs/RELEASE.md", ".github/workflows/ci.yml",
                     "ci/tests/test_release_watch_contract.py", "railway/cost-collector.toml",
                     "railway/r2-guard.toml"]:
            with self.subTest(path=path):
                self.assertSetEqual(self.selected_services([path]), set())

    def test_version_pattern_does_not_match_unrelated_nested_files(self):
        self.assertSetEqual(self.selected_services(["docs/VERSION", "independent/VERSION"]), set())


if __name__ == "__main__":
    unittest.main()
