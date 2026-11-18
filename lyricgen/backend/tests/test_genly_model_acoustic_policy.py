from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from genly_model_acoustic_policy import CHECKPOINT_SHA256, classify_nonvocal_window, endpoint_extension_candidates
from reviewer_singing_activity import REVISION
from shadow_reference_import import digest
from vocal_continuity import _segments_fingerprint


def activity():
    return {"audio_sha256": "a" * 64, "clock": "original_mix_decoded", "model": "CPJKU/veracity/model_log_0mean",
            "revision": REVISION, "checkpoint_sha256": CHECKPOINT_SHA256, "window": {"start": 0., "end": 5.},
            "frame_step_seconds": 1/70, "receptive_field_seconds": 115/70,
            "frames": [{"time": index/70, "probability": .01} for index in range(351)]}


class ActivityTests(unittest.TestCase):
    def classify(self, receipt, window=None):
        return classify_nonvocal_window(receipt, window or {"start": 1., "end": 3.}, audio_sha256="a" * 64, audio_duration=5.)

    def test_low_activity_is_independent_fallible_evidence(self):
        result = self.classify(activity())
        self.assertEqual(result["status"], "nonvocal_supported")
        self.assertTrue(result["suppression_allowed"])
        self.assertFalse(result["correctness_certified"])
        self.assertFalse(result["editor_lyrics_used"])

    def test_a_single_vocal_peak_vetoes_negative_evidence(self):
        receipt = activity()
        receipt["frames"][100]["probability"] = .9
        self.assertFalse(self.classify(receipt)["suppression_allowed"])

    def test_missing_frame_and_wrong_audio_abstain(self):
        receipt = activity()
        receipt["frames"].pop(120)
        self.assertFalse(self.classify(receipt)["suppression_allowed"])
        receipt = activity()
        receipt["audio_sha256"] = "b" * 64
        self.assertFalse(self.classify(receipt)["suppression_allowed"])

    def test_crop_padding_cannot_establish_absence_of_voice(self):
        receipt = activity()
        receipt["window"] = {"start": 1., "end": 3.}
        self.assertEqual(self.classify(receipt)["reason"], "activity_context_insufficient")

    def test_physical_eof_is_explicit_not_confused_with_crop_end(self):
        result = self.classify(activity(), {"start": 4., "end": 5.})
        self.assertTrue(result["suppression_allowed"])
        self.assertTrue(result["physical_file_edge"])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio, self.stem = Path(self.temp.name)/"audio", Path(self.temp.name)/"stem"
        self.audio.write_bytes(b"audio")
        self.stem.write_bytes(b"stem")
        self.rows = [{"text": "Una voz", "start": 1., "end": 3., "words": [{"word": "voz", "start": 2., "end": 3.}]}]
        self.song = {"job_id": "one", "audio_sha256": hashlib.sha256(b"audio").hexdigest(), "audio_revision": 1,
                     "segments_revision": 0, "segments": self.rows, "original_segments": deepcopy(self.rows),
                     "segments_sha256": digest(self.rows)}

    def observer(self, result, *_):
        return {"status": "shadow", "clock": {"verified": True, "offset_s": .025}, "audio_duration_s": 6.,
                "source_segments_fingerprint": _segments_fingerprint(result["segments"]),
                "candidates": [{"segment_index": 0, "current_end": 3., "proposed_end": 3.8, "boundary_spread_s": .04,
                                "selector_policy": "vocal-continuity-v2-periodicity",
                                "periodicity_evidence": {"supported": True, "version": "tail-periodicity-v1", "longest_run_frames": 3}}]}

    def test_screened_extension_is_proposed_without_mutation(self):
        frozen = deepcopy(self.song)
        result = endpoint_extension_candidates(self.song, self.audio, self.stem, observer=self.observer)
        self.assertEqual(result["status"], "proposals")
        self.assertEqual(result["candidates"][0]["proposed_end"], 3.8)
        self.assertEqual(self.song, frozen)
        self.assertFalse(result["automatic_apply_allowed"])

    def test_unverified_clock_or_trim_is_never_accepted(self):
        def unverified(result, *args):
            receipt = self.observer(result)
            receipt["clock"]["verified"] = False
            return receipt
        self.assertEqual(endpoint_extension_candidates(self.song, self.audio, self.stem, observer=unverified)["candidates"], [])
        def trim(result, *args):
            receipt = self.observer(result)
            receipt["candidates"][0]["proposed_end"] = 2.8
            return receipt
        self.assertEqual(endpoint_extension_candidates(self.song, self.audio, self.stem, observer=trim)["candidates"], [])

    def test_changed_lyrics_and_human_edits_preserve_endpoints(self):
        candidate = [{**self.rows[0], "text": "Otra voz"}]
        self.assertEqual(endpoint_extension_candidates(self.song, self.audio, self.stem,
                         candidate_segments=candidate, observer=self.observer)["candidates"], [])
        self.song["original_segments"][0]["end"] = 2.9
        self.assertEqual(endpoint_extension_candidates(self.song, self.audio, self.stem, observer=self.observer)["candidates"], [])


if __name__ == "__main__":
    unittest.main()
