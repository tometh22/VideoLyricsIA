import hashlib
from pathlib import Path
import tempfile
import unittest

from genly_model_runtime import assemble_candidate, attach_endpoint_extensions, cached_stem_for_audio, plan_windows, recognize_song
from local_genly_review import normalize_snapshot
from reviewer_shadow import source_binding


class FullSongRuntimeTests(unittest.TestCase):
    def fixture(self, root):
        audio = root / "original"
        audio.write_bytes(b"original audio fixture")
        song = normalize_snapshot({"job_id": "job", "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                                   "audio_revision": 1, "revision": 0,
                                   "segments": [{"text": "hola mundo", "start": 1., "end": 3., "words": [], "ctc_lr": 99}]})
        return song, audio

    def recognition(self, song, text="hola mundo", duration=5):
        window = plan_windows(duration)[0]
        return {"source": source_binding(song), "duration_seconds": duration, "model_id": "test-listener",
                "observations": [{"source": source_binding(song), "window": window, "status": "recognized",
                                  "reference_conditioned": False, "text": text}]}

    def aligned(self, _audio, text, window):
        words = text.split()
        return {"status": "aligned_hypothesis", "clock": "original_mix_decoded", "model": "test-ctc",
                "words": [{"word": word, "global_start": 1.1 + i * .8, "global_end": 1.7 + i * .8} for i, word in enumerate(words)]}

    def test_tail_and_ownership_partition_are_complete(self):
        windows = plan_windows(223.293)
        self.assertEqual(windows[0]["core_start"], 0)
        self.assertEqual(windows[-1]["core_end"], 223.293)
        for a, b in zip(windows, windows[1:]):
            self.assertEqual(a["core_end"], b["core_start"])
        self.assertTrue(all(w["end"] - w["start"] <= 24 for w in windows))
        self.assertLess(plan_windows(223.293, max_windows=2)[-1]["end"], 223.293)

    def test_full_editable_candidate_has_original_clock_words(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            recognition = self.recognition(song, "hola cielo")
            recognition["stem_evidence"] = {"mix_stem_sync_verified": True}
            recognition["observations"].append({**recognition["observations"][0], "signal": "vocals"})
            report = assemble_candidate(song, audio, recognition, aligner=self.aligned)
            self.assertEqual(report["status"], "complete")
            self.assertTrue(report["coverage"]["full_audio_covered"])
            self.assertEqual(report["candidate_segments"][0]["text"], "hola cielo")
            self.assertEqual(report["findings"][0]["aligned_word_hypotheses"][0]["start"], 1.1)
            self.assertEqual(report["candidate_segments"][0]["start"], 1.)
            self.assertEqual(report["candidate_segments"][0]["end"], 3.)
            self.assertNotIn("words", report["candidate_segments"][0])
            self.assertNotIn("ctc_lr", report["candidate_segments"][0])
            self.assertEqual(song["segments"][0]["text"], "hola mundo")
            self.assertFalse(report["automatic_apply_allowed"])

    def test_uncertain_text_preserves_original_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            report = assemble_candidate(song, audio, self.recognition(song, "otra frase"), aligner=self.aligned)
            self.assertEqual(report["candidate_segments"], song["segments"])
            self.assertTrue(report["findings"][0]["original_timing_retained"])

    def test_invalid_ctc_clock_and_provider_failure_are_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            def bad(*args):
                value = self.aligned(*args)
                value["words"][0]["global_start"] = -1
                return value
            report = assemble_candidate(song, audio, self.recognition(song), aligner=bad)
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["candidate_segments"], song["segments"])
            observation = self.recognition(song)
            observation["observations"][0]["status"] = "failed"
            report = assemble_candidate(song, audio, observation, aligner=self.aligned)
            self.assertFalse(report["coverage"]["full_audio_covered"])

    def test_stale_recognition_and_changed_audio_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            recognition = self.recognition(song)
            recognition["source"] = {**recognition["source"], "segments_revision": 99}
            with self.assertRaisesRegex(ValueError, "stale"):
                assemble_candidate(song, audio, recognition, aligner=self.aligned)
            audio.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "audio_identity"):
                assemble_candidate(song, audio, self.recognition(song), aligner=self.aligned)

    def test_recognition_covers_gaps_and_does_not_receive_editor_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            song, audio = self.fixture(root)
            class Listener:
                model_id = "test"
                def listen(self, clip):
                    self.last = clip
                    return "hola mundo"
            listener = Listener()
            def extract(_audio, window, destination):
                destination.write_bytes(b"clip")
                return "test-sha"
            recognition = recognize_song(song, audio, root / "clips", listener=listener,
                                         duration_probe=lambda _: 50., clip_extractor=extract)
            self.assertEqual(len(recognition["observations"]), 3)
            self.assertEqual(recognition["windows"][-1]["end"], 50.)
            self.assertIsInstance(listener.last, Path)

    def test_protected_song_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            song["approved_at"] = "2026-11-18"
            report = assemble_candidate(song, audio, self.recognition(song), aligner=self.aligned)
            self.assertEqual(report["candidate_segments"], song["segments"])
            self.assertEqual(report["findings"][0]["status"], "protected")

    def test_signal_disagreement_is_local_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            recognition = self.recognition(song)
            recognition["stem_evidence"] = {"mix_stem_sync_verified": True}
            recognition["observations"].append({**recognition["observations"][0], "signal": "vocals", "text": "hola cielo"})
            report = assemble_candidate(song, audio, recognition, aligner=self.aligned)
            self.assertEqual(report["candidate_segments"], song["segments"])
            self.assertEqual(report["findings"][0]["alternatives"]["vocals"], "hola cielo")

    def test_manual_edit_preserved_and_invalid_saved_window_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            song["original_segments"] = [{"text": "earlier machine", "start": 1., "end": 3.}]
            report = assemble_candidate(song, audio, self.recognition(song), aligner=self.aligned)
            self.assertEqual(report["findings"][0]["reason"], "human_edited_source")
            recognition = self.recognition(song)
            recognition["observations"][0]["window"]["core_end"] = 900.
            with self.assertRaisesRegex(ValueError, "invalid_recognition_window"):
                assemble_candidate(song, audio, recognition, aligner=self.aligned)

    def test_missing_stem_allows_new_song_without_fake_second_view(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(cached_stem_for_audio(Path(directory) / "absent.json", "audio-sha"))

    def test_independent_activity_can_veto_but_never_delete_editor_text(self):
        from genly_model_acoustic_policy import CHECKPOINT_SHA256
        from reviewer_singing_activity import REVISION
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            receipt = {"audio_sha256": song["audio_sha256"], "clock": "original_mix_decoded",
                       "model": "CPJKU/veracity/model_log_0mean", "revision": REVISION,
                       "checkpoint_sha256": CHECKPOINT_SHA256, "window": {"start": 0., "end": 5.},
                       "frame_step_seconds": 1/70, "receptive_field_seconds": 115/70,
                       "frames": [{"time": i/70, "probability": .01} for i in range(351)]}
            report = assemble_candidate(song, audio, self.recognition(song),
                                        aligner=lambda *args: self.fail("independent nonvocal veto must precede CTC"),
                                        activity_receipts=[receipt])
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["alignment_evidence"][0]["status"], "nonvocal_supported")
            self.assertEqual(report["candidate_segments"], song["segments"])

    def test_endpoint_policy_only_extends_with_bound_periodicity_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            song, audio = self.fixture(Path(directory))
            report = assemble_candidate(song, audio, self.recognition(song), aligner=self.aligned)
            evidence = {"source": source_binding(song), "policy": "vocal-continuity-v2-periodicity",
                        "automatic_apply_allowed": False, "candidates": [{"source": source_binding(song),
                        "line_index": 0, "current_end": 3., "proposed_end": 3.6,
                        "clock": {"verified": True}, "periodicity_evidence": {"supported": True}}]}
            updated = attach_endpoint_extensions(report, song, evidence)
            self.assertEqual(updated["candidate_segments"][0]["end"], 3.6)
            evidence["candidates"][0]["proposed_end"] = 2.8
            updated = attach_endpoint_extensions(report, song, evidence)
            self.assertEqual(updated["candidate_segments"][0]["end"], 3.)


if __name__ == "__main__":
    unittest.main()
