import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_genly_corpus import assign_splits, prepare


class LocalCorpusTests(unittest.TestCase):
    def test_audio_alias_protects_entire_artist_component(self):
        rows = [
            {"job_id": "new", "audio_sha256": "same", "artist": "Artist"},
            {"job_id": "other", "audio_sha256": "different", "artist": " artist "},
        ]
        registry = {"entries": [{"sha256": "same", "artist": "Alias", "role": "eval_holdout", "known_job_ids": [{"job_id": "old"}]}]}
        result = assign_splits(rows, registry)
        self.assertEqual(result["new"]["split"], "development_replay")
        self.assertEqual(result["new"]["split_group"], result["other"]["split_group"])
        self.assertEqual(result["other"]["split"], "development_replay")

    def test_registry_artist_absent_from_export_still_protects(self):
        rows = [{"job_id": "new", "audio_sha256": "new-sha", "artist": "Singer"}]
        registry = {"entries": [{"artist": "Singer", "role": "val", "known_job_ids": [{"job_id": "prior"}]}]}
        self.assertEqual(assign_splits(rows, registry)["new"]["split"], "calibration")

    def test_deterministic_unknown_assignments(self):
        rows = [{"job_id": "a", "audio_sha256": "a-sha", "artist": "A"}, {"job_id": "b", "audio_sha256": "b-sha", "artist": "B"}]
        self.assertEqual(assign_splits(rows, {}), assign_splits(list(reversed(rows)), {}))
        self.assertEqual(assign_splits(rows, {})["a"]["evaluation_claim"], "development_only")

    def fixture(self, root):
        audio = root / "audio.bin"
        audio.write_bytes(b"verified audio fixture")
        sha = hashlib.sha256(audio.read_bytes()).hexdigest()
        row = {"example_id": "a", "job_id": "job", "artist": "Singer", "audio_sha256": sha, "source_revision": 0, "target_revision": 3,
               "supervision": "approved_published_editorial_reference", "regression_target": True, "category": "timing_only", "timing_comparable": True,
               "before": [{"text": "sample", "start": 1, "end": 2}], "after": [{"text": "sample", "start": 1, "end": 3}], "approved_indices": [0]}
        examples, mapping, registry = root / "examples.jsonl", root / "map.json", root / "registry.json"
        examples.write_text(json.dumps(row) + "\n")
        mapping.write_text(json.dumps([{"job_id": "job", "original_sha256": sha, "source_path": str(audio)}]))
        registry.write_text(json.dumps({"entries": [{"sha256": sha, "artist": "Singer", "role": "train"}]}))
        return row, audio, examples, mapping, registry

    def test_verified_sample_preserves_line_not_word_labels_and_private_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, examples, mapping, registry = self.fixture(root)
            report = prepare(examples, mapping, registry, root / "out", duration_probe=lambda _: 10)
            self.assertEqual(report["samples"], 1)
            sample = json.loads((root / "out/samples.jsonl").read_text())
            self.assertFalse(sample["word_timing_supervision"])
            self.assertEqual(sample["split"], "train")
            self.assertEqual(sample["end_s"], 3)
            self.assertEqual((root / "out/samples.jsonl").stat().st_mode & 0o777, 0o600)

    def test_changed_audio_rejected_before_crop_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, audio, examples, mapping, registry = self.fixture(root)
            audio.write_bytes(b"different audio")
            report = prepare(examples, mapping, registry, root / "out", duration_probe=lambda _: self.fail("must not probe changed audio"))
            self.assertEqual(report["samples"], 0)
            self.assertEqual(report["audio_issues"]["job"], "audio_sha_mismatch")

    def test_conflicting_labels_and_out_of_bounds_crops_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row, _, examples, mapping, registry = self.fixture(root)
            other = json.loads(json.dumps(row))
            other["example_id"] = "b"
            other["after"][0]["text"] = "conflicting"
            examples.write_text(json.dumps(row) + "\n" + json.dumps(other) + "\n")
            report = prepare(examples, mapping, registry, root / "out", duration_probe=lambda _: 10)
            self.assertEqual(report["samples"], 0)
            self.assertEqual(report["rejected"]["conflicting_approved_line"], 1)
            examples.write_text(json.dumps(row) + "\n")
            report = prepare(examples, mapping, registry, root / "out", duration_probe=lambda _: 2)
            self.assertEqual(report["rejected"]["invalid_approved_crop"], 1)


if __name__ == "__main__":
    unittest.main()
