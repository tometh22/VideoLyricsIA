import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_genly_review import normalize_snapshot, prepare_proposal, publish_review, quality_ranking, verify_local_adapter
from reviewer_shadow import source_binding


class LocalReviewTests(unittest.TestCase):
    def setUp(self):
        self.song = normalize_snapshot({
            "job_id": "test", "revision": 3, "audio_revision": 1,
            "audio_sha256": "a" * 64,
            "segments": [{"_id": "line-1", "text": "mi casa", "start": 1., "end": 3.,
                          "words": [{"text": "mi", "start": 1., "end": 2.}]}],
        })
        self.observation = {"source": source_binding(self.song), "line_index": 0,
                            "window": {"start": 1., "end": 3.}, "clip_sha256": "b" * 64,
                            "reference_conditioned": False, "model_id": "local-test",
                            "status": "recognized", "text": "mi taza"}

    def test_builds_existing_editor_contract_without_claiming_timing(self):
        self.song["segments"][0]["content_asr_score"] = .99
        self.song["segments"][0]["ctc_confidence"] = .99
        self.song["segments"][0].update(pos=[.2, .8], scale=1.2, rot=7)
        self.song.pop("segments_sha256")
        self.song = normalize_snapshot(self.song)
        self.observation["source"] = source_binding(self.song)
        proposal, stats = prepare_proposal(self.song, [self.observation])
        self.assertEqual(stats["proposal_count"], 1)
        self.assertFalse(proposal["automatic_apply_allowed"])
        self.assertFalse(proposal["local_genly"]["correctness_certified"])
        window = proposal["windows"][0]
        self.assertEqual(window["confidence"], "low")
        proposed = window["proposed_segments"][0]
        self.assertEqual(proposed["text"], "mi taza")
        self.assertEqual(proposed["end"], 3.)
        self.assertNotIn("words", proposed)
        self.assertNotIn("content_asr_score", proposed)
        self.assertNotIn("ctc_confidence", proposed)
        self.assertEqual(proposed["pos"], [.2, .8])
        self.assertEqual(proposed["scale"], 1.2)
        self.assertEqual(proposed["rot"], 7)
        self.assertIn("words", self.song["segments"][0])

    def test_stale_revision_rejected_before_publication(self):
        observation = copy.deepcopy(self.observation)
        observation["source"]["segments_revision"] = 2
        with self.assertRaisesRegex(ValueError, "stale_proposal"):
            prepare_proposal(self.song, [observation])
        with self.assertRaisesRegex(ValueError, "stale_proposal"):
            publish_review(None, self.song, {"source": observation["source"], "observations": []})

    def test_protects_approved_and_human_edited_lines(self):
        for extra in ({"approved_at": "now"}, {"original_segments": [
                {"text": "otra frase", "start": 1., "end": 3.}]}):
            proposal, stats = prepare_proposal({**self.song, **extra}, [self.observation])
            self.assertIsNone(proposal)
            self.assertEqual(stats["skipped"]["protected"], 1)

    def test_no_reference_conditioned_transcription_as_new_listening(self):
        proposal, stats = prepare_proposal(self.song, [{**self.observation, "reference_conditioned": True}])
        self.assertIsNone(proposal)
        self.assertEqual(stats["skipped"]["invalid"], 1)

    def test_window_cannot_transfer_to_repeated_occurrence(self):
        with self.assertRaisesRegex(ValueError, "recognition_window_does_not_match_line"):
            prepare_proposal(self.song, [{**self.observation, "window": {"start": 10., "end": 12.}}])

    def test_unchanged_lyrics_are_not_a_correction(self):
        proposal, stats = prepare_proposal(self.song, [{**self.observation, "text": "Mi casa!"}])
        self.assertIsNone(proposal)
        self.assertEqual(stats["skipped"]["unchanged"], 1)

    def test_risk_integration_default_off_and_missing_model_visible(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(quality_ranking(self.song["segments"]))
        with patch.dict("os.environ", {"GENLY_LOCAL_REVIEW_RISK_ENABLED": "1",
                                      "GENLY_LOCAL_REVIEW_RISK_MODEL_PATH": "/missing/model"}, clear=True):
            result = quality_ranking(self.song["segments"])
        self.assertFalse(result["available"])
        self.assertFalse(result["mutated_segments"])

    def test_adapter_bound_to_exact_local_base_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "revision123"
            base.mkdir()
            adapter = root / "adapter"
            adapter.mkdir()
            (adapter / "adapter_model.safetensors").write_bytes(b"fixture")
            config = {"base_model_name_or_path": str(base)}
            (adapter / "adapter_config.json").write_text(json.dumps(config))
            receipt = {"status": "smoke_completed", "finite_gradients": True,
                       "snapshot_revision": base.name,
                       "adapter_sha256": hashlib.sha256(b"fixture").hexdigest()}
            (root / "report.json").write_text(json.dumps(receipt))
            self.assertEqual(verify_local_adapter(base, adapter), receipt)
            with self.assertRaisesRegex(ValueError, "adapter_base"):
                verify_local_adapter(root / "different", adapter)
            (adapter / "adapter_model.safetensors").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "adapter_training_receipt"):
                verify_local_adapter(base, adapter)


if __name__ == "__main__":
    unittest.main()
