"""Pure pilot tests, also runnable with unittest without product DB fixtures."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import active_audio_review as active
from reviewer_shadow_audio import file_sha


def observation(provider, phase, text="Otra frase", **kwargs):
    return {"provider": provider, "phase": phase, "valid": True,
            "clip_sha256": phase, "heard": {"text": text, "ambiguous": False,
            "boundary_crossing": False, **kwargs}}


class ActiveListeningTests(unittest.TestCase):
    def setUp(self):
        self.line = {"text": "Una frase", "start": 4., "end": 6.}

    def test_only_two_families_in_two_contexts_produce_candidate(self):
        rows = [observation(p, s) for p in active.PROVIDERS for s in ("baseline", "focused")]
        result = active.text_decision(self.line, rows)
        self.assertEqual(result["status"], "TEXT_PROPOSAL")
        self.assertTrue(result["requires_realignment"])

    def test_same_family_repeated_does_not_establish_independence(self):
        rows = [observation("google", s) for s in ("baseline", "focused")]
        self.assertEqual(active.text_decision(self.line, rows)["status"], "NEEDS_REVIEW")

    def test_same_clip_twice_is_not_new_context(self):
        rows = [observation(p, s) for p in active.PROVIDERS for s in ("baseline", "focused")]
        for r in rows:
            r["clip_sha256"] = "same"
        self.assertEqual(active.text_decision(self.line, rows)["reason"], "same_audio_context")

    def test_disagreement_ambiguity_and_boundary_leak_do_not_propose(self):
        for change in ({"text": "Una frase"}, {"ambiguous": True}, {"boundary_crossing": True}):
            rows = [observation(p, s) for p in active.PROVIDERS for s in ("baseline", "focused")]
            rows[-1]["heard"].update(change)
            self.assertEqual(active.text_decision(self.line, rows)["status"], "NEEDS_REVIEW")

    def test_focus_does_not_truncate_long_lines(self):
        self.assertIsNone(active.focus_window({"start": 0, "end": 34}, 60))
        self.assertEqual(active.focus_window(self.line, 60), {"start": 2., "end": 8.})

    def test_offsets_are_applied_before_selecting_occurrence(self):
        payload = {"response": {"words": [{"word": "otra", "start": 4, "end": 5}]}}
        read = active.heard_line(payload, "openai", {"start": 20}, {"start": 24, "end": 25})
        self.assertEqual(read["text"], "otra")
        self.assertEqual(active.heard_line(payload, "openai", {"start": 20}, self.line)["text"], "")

    def run_review(self, *, max_calls=6, fail=False, wrong_audio=False, http_status=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "master.wav"
            audio.write_bytes(b"test audio")
            snapshot = {"job_id": "test", "revision": 2, "audio_revision": 1,
                        "audio_sha256": file_sha(audio), "segments": [self.line]}
            original = copy.deepcopy(snapshot)
            if wrong_audio:
                snapshot["audio_sha256"] = "a" * 64
            seen = []

            class Listener:
                def __init__(self, *args, policy):
                    self.calls = 0

                def listen(self, clip, *, provider, view, source, window):
                    self.calls += 1
                    seen.append((provider, source, window))
                    if fail:
                        return {"tool_status": "tool_error", "http_status": http_status}
                    start, end = 4 - window["start"], 6 - window["start"]
                    events = ([{"text": "Otra frase", "start": start, "end": end, "kind": "sung"}]
                              if start >= 0 else [])
                    words = ([{"word": "Otra frase", "start": start, "end": end}] if start >= 0 else [])
                    return {"tool_status": "ok", "received_audio": True, "conditioning_texts": [],
                            "response": ({"events": events, "editorial_ambiguity": False,
                                "ambiguity_reason": "none", "reverb": "absent"} if provider == "google"
                                else {"text": "Otra frase" if words else "", "words": words})}

            def extract(_audio, window, clip):
                clip.write_bytes(str(window).encode())

            with patch.object(active, "probe", return_value=40.), patch.object(active, "extract_clip", extract):
                report = active.review(snapshot, audio, root / "cache", max_calls=max_calls,
                                       listener_factory=Listener)
            self.assertEqual(snapshot, original)
            self.assertTrue(all("segments" not in source and "text" not in source for _, source, _ in seen))
            return report, seen

    def test_adaptive_review_and_input_immutability(self):
        report, seen = self.run_review()
        self.assertEqual(report["calls"], 6)
        self.assertEqual(seen[-1][2], {"start": 2., "end": 8.})
        self.assertEqual(report["lines"][0]["text"]["status"], "TEXT_PROPOSAL")
        self.assertFalse(report["automatic_apply_allowed"])
        self.assertFalse(report["timing_verified"])

    def test_budget_cannot_be_exceeded_and_unheard_audio_is_reported(self):
        report, seen = self.run_review(max_calls=3)
        self.assertLessEqual(len(seen), 3)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertTrue(report["coverage"]["openai"]["missing"])
        self.assertEqual(report["lines"][0]["text"]["status"], "NEEDS_REVIEW")

    def test_failed_calls_are_not_successful_listening(self):
        report, _ = self.run_review(fail=True)
        self.assertEqual(report["status"], "NOT_RUN")
        self.assertEqual(report["coverage"]["google"]["fraction"], 0)
        self.assertEqual(report["lines"], [])

    def test_audio_identity_mismatch_fails_before_provider_call(self):
        with self.assertRaisesRegex(ValueError, "audio_identity_changed"):
            self.run_review(wrong_audio=True)

    def test_auth_or_quota_failure_stops_that_provider_without_retries(self):
        for code in (401, 403, 429):
            report, seen = self.run_review(fail=True, http_status=code)
            self.assertEqual(len(seen), 2)
            self.assertEqual(report["unavailable_providers"], {"google": code, "openai": code})
            self.assertEqual(report["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
