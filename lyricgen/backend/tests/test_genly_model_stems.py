import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import genly_model_stems as stems


class LocalStemTests(unittest.TestCase):
    def test_interrupted_helper_kills_separator_process_group(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch.object(stems.subprocess, "Popen", return_value=process), patch.object(stems.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                stems._run_child(["local-separator"], timeout_s=5)
        kill.assert_called_once_with(12345, stems.signal.SIGKILL)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.audio, self.checkpoint = self.root/"audio", self.root/"weights.th"
        self.audio.write_bytes(b"original")
        self.checkpoint.write_bytes(b"known weights")
        self.audio_sha = hashlib.sha256(b"original").hexdigest()
        self.weight_sha = hashlib.sha256(b"known weights").hexdigest()
        self.pin = patch.object(stems, "MODEL_SHA256", self.weight_sha)
        self.probe = patch.object(stems, "probe", return_value=30.)
        self.pin.start()
        self.probe.start()
        self.addCleanup(self.pin.stop)
        self.addCleanup(self.probe.stop)

    def runner(self, command, *, timeout_s, verified=True):
        output = Path(command[command.index("--output")+1])
        output.write_bytes(b"separated vocals")
        receipt = {"original_sha256": self.audio_sha, "model_sha256": self.weight_sha,
                   "stem_path": str(output.resolve()), "stem_sha256": hashlib.sha256(b"separated vocals").hexdigest(),
                   "configuration": stems.CONFIG, "clock": {"verified": verified},
                   "status": "ready" if verified else "unverified"}
        (output.parent/"receipt.json").write_text(json.dumps(receipt))
        self.assertIn("--child", command)
        self.assertLessEqual(timeout_s, 600)
        return 0, "", ""

    def prepare(self, runner=None, **kwargs):
        return stems.prepare_local_stem(self.audio, self.audio_sha, self.root/"cache", checkpoint=self.checkpoint,
                                         runner=runner or self.runner, **kwargs)

    def test_generated_stem_and_verified_cache_hit(self):
        first = self.prepare()
        self.assertEqual(first["status"], "ready")
        self.assertFalse(first["cache_hit"])
        runner = Mock(side_effect=AssertionError("must not run twice"))
        second = self.prepare(runner)
        self.assertTrue(second["cache_hit"])
        runner.assert_not_called()

    def test_wrong_original_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "original_audio_hash_mismatch"):
            stems.prepare_local_stem(self.audio, "bad", self.root/"cache", checkpoint=self.checkpoint)

    def test_checkpoint_absence_or_corruption_never_downloads(self):
        runner = Mock(side_effect=AssertionError("must not run"))
        self.checkpoint.unlink()
        self.assertEqual(self.prepare(runner)["reason"], "cached_demucs_checkpoint_missing")
        self.checkpoint.write_bytes(b"different")
        self.assertEqual(self.prepare(runner)["reason"], "cached_demucs_checkpoint_mismatch")
        runner.assert_not_called()

    def test_timeout_and_unverified_clock_are_fallback_statuses(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired("model", 600))
        self.assertEqual(self.prepare(runner)["reason"], "local_stem_timeout")
        result = self.prepare(lambda command, **kwargs: self.runner(command, verified=False, **kwargs))
        self.assertEqual(result["status"], "unverified")

    def test_duration_budget_prevents_model_start(self):
        with patch.object(stems, "probe", return_value=901):
            runner = Mock(side_effect=AssertionError("must not run"))
            self.assertEqual(self.prepare(runner)["reason"], "local_stem_duration_budget")
            runner.assert_not_called()

    def test_existing_manifest_requires_matching_file_hash(self):
        existing = self.root/"manifest.json"
        vocal = self.root/"vocals.wav"
        vocal.write_bytes(b"voice")
        row = {"original_sha256": self.audio_sha, "stem_path": str(vocal),
               "stem_sha256": hashlib.sha256(b"voice").hexdigest()}
        existing.write_text(json.dumps([row]))
        self.assertTrue(stems.manifest_entry(self.audio, self.audio_sha, existing)["runtime_clock_verification_required"])
        vocal.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "cached_stem_identity_mismatch"):
            stems.manifest_entry(self.audio, self.audio_sha, existing)

    def test_manifest_fallback_is_empty_private_json(self):
        output = self.root/"output.json"
        stems.write_manifest(output, [])
        self.assertEqual(json.loads(output.read_text()), [])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
