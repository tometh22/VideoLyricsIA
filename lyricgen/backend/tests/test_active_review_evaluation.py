import copy
import unittest
from full_audio_review import binding
from active_review_evaluation import evaluate


class EditorialEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"job_id": "one", "revision": 0, "audio_revision": 1, "audio_sha256": "a"*64,
                         "segments": [{"start": 1., "end": 3., "text": "Una casa"}]}
        self.report = {"job_id": "one", "binding": binding(self.snapshot), "lines": []}
        self.reference = {"job_id": "one", "source_revision": 0, "target_revision": 2,
                          "audio_sha256": "a"*64, "regression_target": True, "example_id": "r1",
                          "original_indices": [0], "before": copy.deepcopy(self.snapshot["segments"]),
                          "after": [{"text": "Una cosa", "start": 1., "end": 3.}]}

    def propose(self, text):
        self.report["lines"] = [{"index": 0, "before": self.snapshot["segments"][0],
                                 "text": {"status": "TEXT_PROPOSAL", "proposed_text": text}}]

    def test_correct_proposal_reduces_editorial_word_error(self):
        self.propose("Una cosa")
        result = evaluate(self.snapshot, self.report, [self.reference])
        self.assertEqual(result["outcomes"], {"improved": 1})
        self.assertEqual(result["candidate_wer"], 0)
        self.assertFalse(result["human_precision_measured"])

    def test_abstention_is_unresolved_not_a_success(self):
        result = evaluate(self.snapshot, self.report, [self.reference])
        self.assertEqual(result["outcomes"], {"unresolved": 1})

    def test_damage_to_correct_text_is_counted(self):
        self.reference["after"][0]["text"] = "Una casa"
        self.propose("Otra cosa")
        self.assertEqual(evaluate(self.snapshot, self.report, [self.reference])["outcomes"], {"worsened": 1})

    def test_unmatched_proposal_is_unknown_not_safe(self):
        self.propose("Otra cosa")
        result = evaluate(self.snapshot, self.report, [])
        self.assertEqual(result["unmatched_proposal_indices"], [0])
        self.assertIsNone(result["candidate_wer"])

    def test_stale_reports_are_rejected(self):
        self.snapshot["revision"] += 1
        with self.assertRaisesRegex(ValueError, "report_source_mismatch"):
            evaluate(self.snapshot, self.report, [self.reference])

    def test_duplicate_approvals_are_not_double_counted(self):
        result = evaluate(self.snapshot, self.report, [self.reference, copy.deepcopy(self.reference)])
        self.assertEqual(result["reference_pairs"], 1)

    def test_conflicting_approvals_are_excluded(self):
        other = copy.deepcopy(self.reference)
        other["after"][0]["text"] = "Otro texto"
        result = evaluate(self.snapshot, self.report, [self.reference, other])
        self.assertEqual(result["reference_pairs"], 0)
        self.assertEqual(result["skipped"], {"conflicting_approved_text": 2})

    def test_wrong_audio_or_original_does_not_match(self):
        self.reference["audio_sha256"] = "b"*64
        result = evaluate(self.snapshot, self.report, [self.reference])
        self.assertEqual(result["reference_pairs"], 0)
        self.assertEqual(result["skipped"], {"audio_identity_mismatch": 1})


if __name__ == "__main__":
    unittest.main()
