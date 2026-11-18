import copy
import unittest
from unittest.mock import Mock

from active_timing_review import investigate


class DirectedTimingTests(unittest.TestCase):
    def setUp(self):
        self.segments = [{"text": "Una frase", "start": 4., "end": 6.},
                         {"text": "Siguiente", "start": 10., "end": 11.}]
        self.supported = {"status": "TEXT_SUPPORTED"}
        self.endpoint = Mock(return_value={"status": "periodicity_boundary_observed", "interval": [6.2, 6.3]})

    @staticmethod
    def aligner(audio, text, window):
        return {"status": "aligned_hypothesis", "clock": "original_mix_decoded", "window": window,
                "words": [{"word": text.split()[0], "global_start": 4.1, "global_end": 4.5},
                          {"word": text.split()[1], "global_start": 4.6, "global_end": 6.1}]}

    def run_probe(self, **kwargs):
        return investigate("original.wav", self.segments, 0, 20., text_decision=self.supported,
                           aligner=kwargs.pop("aligner", self.aligner), endpoint_measure=self.endpoint, **kwargs)

    def test_unresolved_text_routes_to_listening_without_alignment(self):
        aligner = Mock()
        result = investigate("a.wav", self.segments, 0, 20, aligner=aligner)
        aligner.assert_not_called()
        self.assertEqual(result["next_action"], "independent_audio_listening")

    def test_supported_text_gets_bounded_context_and_sustain_investigation(self):
        before = copy.deepcopy(self.segments)
        result = self.run_probe()
        self.assertEqual(result["status"], "ACOUSTIC_HYPOTHESES")
        self.assertEqual(len(result["hypotheses"]), 2)
        self.assertEqual(result["investigations"][1]["result"]["window"], {"start": 1., "end": 9.})
        self.assertFalse(result["automatic_apply_allowed"])
        self.assertFalse(result["timing_verified"])
        self.assertEqual(self.segments, before)

    def test_conditional_probe_does_not_claim_it_recognized_text(self):
        result = investigate("a.wav", self.segments, 0, 20, conditional=True,
                             aligner=self.aligner, endpoint_measure=self.endpoint)
        self.assertEqual(result["status"], "CONDITIONAL_ACOUSTIC_HYPOTHESES")
        self.assertFalse(result["text_independently_supported"])

    def test_occurrence_drift_stops_before_sustain_measurement(self):
        def drift(audio, text, window):
            result = self.aligner(audio, text, window)
            if window["start"] == 1:
                for w in result["words"]:
                    w["global_start"] += 1
                    w["global_end"] += 1
            return result
        result = self.run_probe(aligner=drift)
        self.assertEqual(result["reason"], "occurrence_not_stable")
        self.endpoint.assert_not_called()

    def test_unstable_final_boundary_stops_before_sustain_measurement(self):
        def drift(audio, text, window):
            result = self.aligner(audio, text, window)
            if window["start"] == 1:
                result["words"][-1]["global_end"] += .4
            return result
        result = self.run_probe(aligner=drift)
        self.assertEqual(result["reason"], "boundary_changes_with_context")
        self.endpoint.assert_not_called()

    def test_next_occurrence_clamps_search(self):
        self.segments[1]["start"] = 6.8
        result = self.run_probe()
        self.assertEqual(result["investigations"][1]["result"]["window"]["end"], 6.8)
        self.assertEqual(self.endpoint.call_args.kwargs["next_occurrence_start"], 6.8)

    def test_protected_and_overlapping_lines_are_not_processed(self):
        self.segments[0]["locked"] = True
        aligner = Mock()
        self.assertEqual(self.run_probe(aligner=aligner)["reason"], "protected_line")
        aligner.assert_not_called()
        del self.segments[0]["locked"]
        self.segments[1]["start"] = 5
        self.assertEqual(self.run_probe(aligner=aligner)["reason"], "overlapping_occurrences_require_review")
        aligner.assert_not_called()

    def test_invalid_alignment_and_failed_tool_are_explicit(self):
        for aligner in (Mock(return_value={"status": "tool_error"}), Mock(side_effect=RuntimeError)):
            result = self.run_probe(aligner=aligner)
            self.assertEqual(result["status"], "NEEDS_REVIEW")
            self.assertEqual(result["hypotheses"], [])
        self.endpoint.assert_not_called()

    def test_invalid_periodicity_interval_is_never_a_candidate(self):
        self.endpoint.return_value = {"status": "periodicity_boundary_observed", "interval": [8., 100.]}
        self.assertEqual(len(self.run_probe()["hypotheses"]), 1)

    def test_text_proposal_realigns_new_text(self):
        self.supported = {"status": "TEXT_PROPOSAL", "proposed_text": "Otra frase"}
        result = self.run_probe()
        self.assertEqual(result["conditioning_text"], "Otra frase")
        self.assertTrue(result["text_change_requires_realignment"])

    def test_periodicity_before_word_end_cannot_be_used_to_trim_word(self):
        # Real smoke exposed this distinction: CTC word end 1.96s vs a
        # periodicity interval at 1.71–1.72s. Translate the same gap here.
        self.endpoint.return_value = {"status": "periodicity_boundary_observed", "interval": [5.85, 5.86]}
        result = self.run_probe()
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "periodicity_ends_before_aligned_word")
        self.assertEqual(len(result["hypotheses"]), 1)


if __name__ == "__main__":
    unittest.main()
