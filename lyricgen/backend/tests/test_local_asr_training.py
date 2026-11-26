import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_asr_training import balanced_subset, edit_distance, generated_metrics, load_partitions


class TrainingTests(unittest.TestCase):
    def test_word_metrics_and_coverage(self):
        rows = [{"job_id": "a", "text": "mi canción"}, {"job_id": "b", "text": "hola"}]
        result = generated_metrics(rows, ["mi canción", "hola mundo"])
        self.assertAlmostEqual(result["micro_wer"], 1 / 3)
        self.assertEqual(result["song_macro_wer"], .5)
        with self.assertRaisesRegex(ValueError, "coverage"):
            generated_metrics(rows, ["one"])
        self.assertEqual(edit_distance(["a", "b"], ["a"]), 1)

    def test_balanced_sampling_reproducible(self):
        rows = [{"job_id": str(i), "sample_id": f"{i}-{j}"} for i in range(3) for j in range(5)]
        selected = balanced_subset(rows, 3, 17)
        self.assertEqual(len({row["job_id"] for row in selected}), 3)
        self.assertEqual(selected, balanced_subset(list(reversed(rows)), 3, 17))
        self.assertEqual(len(rows), 15)

    def fixture(self, root):
        rows = []
        for i, split in enumerate(["train", "train", "calibration", "calibration", "development_replay"]):
            audio = root / f"audio-{i}"
            audio.write_bytes(str(i).encode())
            rows.append({"sample_id": str(i), "job_id": str(i), "artist": str(i), "split_group": str(i),
                         "split": split, "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                         "audio_path": str(audio), "start_s": 0, "end_s": 1, "text": "hola",
                         "known_registry_roles": []})
        return rows

    def write(self, root, rows):
        path = root / "samples.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows))
        (root / "manifest.json").write_text(json.dumps({"outputs": {"samples.jsonl": {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}))
        return path

    def test_audio_identity_and_partition_leakage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.fixture(root)
            result = load_partitions(self.write(root, rows))
            self.assertEqual(len(result["train"]), 2)
            rows[2]["artist"] = rows[0]["artist"]
            with self.assertRaisesRegex(ValueError, "partition_leakage"):
                load_partitions(self.write(root, rows))
            rows[2]["artist"] = "other"
            Path(rows[0]["audio_path"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "audio_hash_mismatch"):
                load_partitions(self.write(root, rows))

    def test_protected_role_and_nonfinite_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.fixture(root)
            rows[0]["known_registry_roles"] = ["eval_holdout"]
            with self.assertRaisesRegex(ValueError, "protected_training_role"):
                load_partitions(self.write(root, rows))
            rows[0]["known_registry_roles"] = []
            rows[0]["end_s"] = float("inf")
            with self.assertRaisesRegex(ValueError, "invalid_crop"):
                load_partitions(self.write(root, rows))


if __name__ == "__main__":
    unittest.main()
