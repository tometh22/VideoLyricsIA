import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_lora_probe import available_memory_gib, cached_snapshot, private_json, select_sample, verify_manifest


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audio = self.root / "audio"
        self.audio.write_bytes(b"verified audio fixture")
        self.row = dict(sample_id="one", split="train", known_registry_roles=[],
                        audio_path=str(self.audio), audio_sha256=hashlib.sha256(self.audio.read_bytes()).hexdigest(),
                        start_s=1.0, end_s=2.0, text="example")
        self.manifest = self.root / "samples.jsonl"

    def select(self, **updates):
        self.manifest.write_text(json.dumps({**self.row, **updates}) + "\n")
        return select_sample(self.manifest)

    def test_hash_verified(self):
        self.assertEqual(self.select()["sample_id"], "one")
        with self.assertRaisesRegex(ValueError, "audio_hash"):
            self.select(audio_sha256="0" * 64)

    def test_manifest_hash_verified(self):
        self.select()
        expected = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        (self.root / "manifest.json").write_text(json.dumps({"outputs": {
            "samples.jsonl": {"sha256": expected}}}))
        self.assertEqual(verify_manifest(self.manifest), expected)
        self.manifest.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "manifest_hash_mismatch"):
            verify_manifest(self.manifest)

    def test_protected_samples_rejected(self):
        for split in ("calibration", "development_replay", "excluded", "eval_holdout"):
            with self.assertRaisesRegex(ValueError, "no_train_sample"):
                self.select(split=split)
        for role in ("val", "eval_holdout"):
            with self.assertRaisesRegex(ValueError, "protected_song_role"):
                self.select(known_registry_roles=[role])

    def test_nonfinite_or_bad_intervals(self):
        for updates in ({"start_s": float("nan")}, {"end_s": float("inf")},
                        {"start_s": -1}, {"end_s": 40}, {"end_s": 1}):
            with self.assertRaisesRegex(ValueError, "invalid_interval"):
                self.select(**updates)

    def test_private_report(self):
        path = self.root / "report.json"
        path.touch(mode=0o644)
        private_json(path, {"ok": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_memory_excludes_compressor(self):
        text = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
                'Pages free: 10.\nFile-backed pages: 20.\nPages occupied by compressor: 99999.\n')
        self.assertEqual(available_memory_gib(text), 30 * 16384 / 1024 ** 3)
        with self.assertRaises(ValueError):
            available_memory_gib("bad")

    def test_missing_cache_does_not_download(self):
        with self.assertRaisesRegex(ValueError, "cached_snapshot"):
            cached_snapshot("openai/whisper-tiny", self.root)


if __name__ == "__main__":
    unittest.main()
