import copy
import json
from pathlib import Path
import tempfile
import unittest

from local_correction_selector import load_artifact, predict, rank_segments, source_features, train, validate_partitions


def examples():
    rows = []
    for split in ("train", "calibration", "development_replay"):
        for group in range(4):
            for index in range(30):
                changed = index % 2 == 0
                identity = f"{split}-{group}"
                rows.append({"example_id": f"{identity}-{index}", "job_id": identity,
                             "audio_sha256": identity, "artist": identity, "split_group": identity,
                             "split": split, "category": "text_or_segmentation_with_timing" if changed else "unchanged",
                             "timing_comparable": True,
                             "before": [{"text": "Una canción", "start": 1., "end": 2.,
                                         "recognition_score": .1 if changed else .9}],
                             "after": [{"text": "Approved must never be a feature"}]})
    return rows


class LocalRiskTests(unittest.TestCase):
    def test_features_ignore_approved_and_unknown_metadata(self):
        original = examples()[0]["before"]
        contaminated = copy.deepcopy(original)
        contaminated[0].update(after="secret", approved_text="secret", category="unchanged", artist="changed")
        self.assertEqual(source_features(original), source_features(contaminated))

    def test_partition_overlap_is_rejected(self):
        rows = examples()
        rows[-1]["audio_sha256"] = rows[0]["audio_sha256"]
        with self.assertRaisesRegex(ValueError, "partition_leakage:audio_sha256"):
            validate_partitions(rows)

    def test_actual_learning_and_no_editor_mutation(self):
        rows = examples()
        model, report = train(rows, corpus_sha256="test")
        high, low = rows[0]["before"], rows[1]["before"]
        self.assertGreater(predict(model, high)["risk_scores"]["any_editorial_edit"],
                           predict(model, low)["risk_scores"]["any_editorial_edit"])
        metrics = report["targets"]["any_editorial_edit"]["partitions"]["development_replay"]
        self.assertLess(metrics["model"]["song_macro_brier"], metrics["train_constant_baseline"]["song_macro_brier"])
        segments = [*low, *high]
        frozen = copy.deepcopy(segments)
        ranked = rank_segments(model, segments)
        self.assertEqual(ranked[0]["line_index"], 1)
        self.assertEqual(segments, frozen)
        self.assertFalse(ranked[0]["automatic_apply_allowed"])
        self.assertFalse(ranked[0]["candidate_correctness_estimated"])

    def test_evaluation_labels_cannot_change_model(self):
        rows = examples()
        first, _ = train(rows, corpus_sha256="test")
        for row in rows:
            if row["split"] == "development_replay":
                row["category"] = "unchanged"
        second, _ = train(rows, corpus_sha256="test")
        self.assertEqual(first, second)

    def test_small_calibration_is_explicitly_uncalibrated(self):
        rows = [r for r in examples() if r["split"] != "calibration" or r["split_group"] == "calibration-0"]
        model, _ = train(rows, corpus_sha256="test")
        self.assertEqual(model["models"]["any_editorial_edit"]["calibration"]["status"],
                         "uncalibrated_insufficient_groups_or_classes")

    def test_artifact_tampering_rejected(self):
        model, _ = train(examples(), corpus_sha256="test")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "model.json"
            path.write_text(json.dumps(model))
            self.assertEqual(load_artifact(path), model)
            model["models"]["any_editorial_edit"]["intercept"] += 1
            path.write_text(json.dumps(model))
            with self.assertRaisesRegex(ValueError, "artifact_model_hash_mismatch"):
                load_artifact(path)

    def test_insertions_without_source_are_counted_not_trained(self):
        rows = examples()
        rows.append({"before": [], "category": "text_or_segmentation"})
        _, report = train(rows, corpus_sha256="test")
        self.assertEqual(report["excluded_missing_source_segments"], 1)


if __name__ == "__main__":
    unittest.main()
