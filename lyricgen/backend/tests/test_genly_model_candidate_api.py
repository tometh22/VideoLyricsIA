from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import genly_model_candidate_api as api
from reviewer_shadow import source_binding
from shadow_reference_import import digest


def fixture():
    baseline = [{"text": "Una casa", "start": 1., "end": 3., "id": "one"},
                {"text": "Al cantar", "start": 4., "end": 6., "id": "two"}]
    song = {"job_id": "song1", "audio_sha256": "a" * 64, "audio_revision": 1,
            "segments_revision": 0, "segments_sha256": digest(baseline),
            "segments": baseline, "original_segments": deepcopy(baseline)}
    report = {"schema": "genly-model-result-v1", "source": source_binding(song),
              "model_id": "genly-local-v1", "status": "complete", "candidate_segments": deepcopy(baseline),
              "coverage": {"duration_seconds": 7., "analyzed_intervals": [[0., 4.], [3., 7.]], "full_audio_covered": True}}
    return song, report


class CandidateTests(unittest.TestCase):
    def test_complete_unchanged_candidate_is_displayable_without_proposal(self):
        song, report = fixture()
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(candidate["changes"], [])
        self.assertEqual(candidate["segments"], song["segments"])
        self.assertIsNone(proposal)
        self.assertFalse(candidate["correctness_certified"])

    def test_partial_candidate_without_local_evidence_preserves_source(self):
        song, report = fixture()
        report["status"] = "partial"
        report["candidate_segments"][0]["text"] = "Una cosa"
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(len(candidate["changes"]), 0)
        self.assertIsNone(proposal)

    def test_partial_candidate_can_publish_a_locally_evidenced_line(self):
        song, report = fixture()
        report["status"] = "partial"
        report["candidate_segments"][0]["text"] = "Una cosa"
        report["provenance"] = {"clock": "original_mix_decoded", "reference_conditioned_recognition": False}
        report["findings"] = [{"line_index": 0, "status": "proposed", "original_text": "Una casa",
            "original_start": 1., "original_end": 3., "candidate_text": "Una cosa", "candidate_start": 1., "candidate_end": 3.,
            "window_ids": ["window1"], "aligned_word_hypotheses": [
                {"word": "Una", "start": 1., "end": 1.5}, {"word": "cosa", "start": 1.5, "end": 3.}]}]
        report["alignment_evidence"] = [{"status": "aligned", "window": {"id": "window1", "start": 0., "end": 4.},
            "alignment": {"status": "aligned_hypothesis", "clock": "original_mix_decoded"}}]
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(len(candidate["changes"]), 1)
        self.assertEqual(proposal["local_genly_model"]["analysis_status"], "partial")
        self.assertFalse(proposal["automatic_apply_allowed"])
        report["alignment_evidence"][0]["alignment"]["clock"] = "stem_unverified"
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(candidate["segments"], song["segments"])
        self.assertIsNone(proposal)

    def test_rejects_stale_source_and_false_full_coverage(self):
        song, report = fixture()
        report["source"]["audio_revision"] = 2
        with self.assertRaisesRegex(ValueError, "source_mismatch"):
            api.prepare_candidate(song, report)
        report["source"] = source_binding(song)
        report["coverage"]["analyzed_intervals"] = [[0., 2.], [3., 7.]]
        with self.assertRaisesRegex(ValueError, "audio_gaps"):
            api.prepare_candidate(song, report)

    def test_protected_line_is_retained_without_blocking_other_changes(self):
        song, report = fixture()
        song["segments"][0]["text"] = "Mi edición"
        song["segments_sha256"] = digest(song["segments"])
        report["source"] = source_binding(song)
        report["candidate_segments"][0]["text"] = "Cambio indebido"
        report["candidate_segments"][1]["text"] = "Al cantar más"
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(candidate["segments"][0], song["segments"][0])
        self.assertEqual(len(proposal["windows"]), 1)
        self.assertEqual(proposal["windows"][0]["current_segments"], [song["segments"][1]])

    def test_timing_windows_include_old_and_new_bounds(self):
        song, report = fixture()
        report["candidate_segments"][0]["start"] = .8
        report["candidate_segments"][0]["end"] = 3.2
        _, proposal = api.prepare_candidate(song, report)
        self.assertEqual(proposal["windows"][0]["start"], .8)
        self.assertEqual(proposal["windows"][0]["end"], 3.2)
        self.assertEqual(proposal["windows"][0]["suggestion_type"], "timing")

    def test_timing_component_cannot_move_a_protected_neighbor(self):
        song, report = fixture()
        song["segments"][1]["operator_locked"] = True
        song["segments_sha256"] = digest(song["segments"])
        report["source"] = source_binding(song)
        report["candidate_segments"][0]["end"] = 4.2
        report["candidate_segments"][1]["start"] = 4.3
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertIsNone(proposal)
        self.assertEqual(candidate["segments"], song["segments"])

    def test_word_times_require_text_correspondence(self):
        song, report = fixture()
        report["candidate_segments"][0]["words"] = [{"word": "Otra cosa", "start": 1., "end": 3.}]
        with self.assertRaisesRegex(ValueError, "words_text_mismatch"):
            api.prepare_candidate(song, report)
        report["candidate_segments"][0]["words"] = [{"word": "Una", "start": 1., "end": 1.5}, {"word": "casa", "start": 1.6, "end": 3.}]
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(len(candidate["changes"]), 1)
        self.assertEqual(proposal["windows"][0]["suggestion_type"], "timing")

    def test_real_legacy_accent_mismatch_is_retained_without_recertification(self):
        # Minimal reproduction of Es Tan Facil's unchanged live line 29.
        row = {"text": "Al final, tú vas a perder", "start": 140.26, "end": 143.39,
               "words": [{"word": word, "start": start, "end": end, "score": .9}
                         for word, start, end in [("Al", 140.34, 140.62), ("final", 140.72, 141.14),
                            ("tu", 141.16, 141.38), ("vas", 141.38, 141.64),
                            ("a", 141.68, 141.76), ("perder", 141.82, 143.)]]}
        song, report = fixture()
        song.update(segments=[deepcopy(row)], original_segments=[deepcopy(row)], segments_sha256=digest([row]))
        report.update(source=source_binding(song), candidate_segments=[deepcopy(row)],
                      coverage={"duration_seconds": 150., "analyzed_intervals": [[0., 150.]]})
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(candidate["segments"], [row])
        self.assertIsNone(proposal)
        report["candidate_segments"][0]["end"] = 144.
        candidate, _ = api.prepare_candidate(song, report)
        self.assertEqual(candidate["segments"][0]["words"], row["words"])
        self.assertNotIn("word_alignment_status", candidate["segments"][0])
        for change in ("text", "words", "bounds"):
            altered = deepcopy(report)
            if change == "text":
                altered["candidate_segments"][0]["text"] += " más"
            elif change == "words":
                altered["candidate_segments"][0]["words"][0]["end"] += .01
            else:
                altered["candidate_segments"][0]["start"] += .01
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "words_text_mismatch"):
                api.prepare_candidate(song, altered)

    def test_only_exact_legacy_overlap_bounds_are_allowed(self):
        song, report = fixture()
        song["segments"][0]["end"] = 4.2
        song["original_segments"] = deepcopy(song["segments"])
        song["segments_sha256"] = digest(song["segments"])
        report.update(source=source_binding(song), candidate_segments=deepcopy(song["segments"]))
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertEqual(candidate["segments"], song["segments"])
        self.assertIsNone(proposal)
        for index, field, value in [(0, "end", 4.3), (1, "start", 3.9), (1, "end", 6.1)]:
            altered = deepcopy(report)
            altered["candidate_segments"][index][field] = value
            with self.subTest(index=index, field=field), self.assertRaisesRegex(ValueError, "timeline_overlap"):
                api.prepare_candidate(song, altered)
        song, report = fixture()
        report["candidate_segments"][0]["end"] = 4.2
        with self.assertRaisesRegex(ValueError, "timeline_overlap"):
            api.prepare_candidate(song, report)

    def test_metadata_and_client_proposal_are_not_trusted(self):
        song, report = fixture()
        song["segments"][0].update(pos=[.5, .8], scale=1.2, rot=3, speaker="lead")
        song["segments_sha256"] = digest(song["segments"])
        report["source"] = source_binding(song)
        report["proposal"] = {"automatic_apply_allowed": True, "windows": ["bad"]}
        report["candidate_segments"][0].update(text="Una cosa", content_asr_score=1., secret_path="/secret")
        candidate, proposal = api.prepare_candidate(song, report)
        self.assertNotIn("content_asr_score", candidate["segments"][0])
        self.assertNotIn("secret_path", candidate["segments"][0])
        self.assertEqual(candidate["segments"][0]["pos"], [.5, .8])
        self.assertEqual(candidate["segments"][0]["speaker"], "lead")
        self.assertFalse(proposal["automatic_apply_allowed"])

    def test_registry_is_tenant_and_source_bound(self):
        song, report = fixture()
        candidate, _ = api.prepare_candidate(song, report)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "REVIEWER_CANDIDATE_STORAGE": "local", "REVIEWER_ASSIST_CACHE_DIR": root}):
            identity = api._register("tenant1", candidate)
            pointer = {"identity": identity, "source": source_binding(song)}
            self.assertIsNotNone(api._read_candidate("tenant1", song, pointer, None))
            self.assertIsNone(api._read_candidate("tenant2", song, pointer, None))
            song["audio_revision"] += 1
            self.assertIsNone(api._read_candidate("tenant1", song, pointer, None))


class RouterTests(unittest.TestCase):
    def setup_client(self, role="admin", denied=False):
        application = FastAPI()
        db = Mock()
        user = {"id": 1, "role": role}
        song, _ = fixture()
        job = SimpleNamespace(job_id="song1", tenant_id="tenant1", input_audio_sha256="a" * 64,
                              audio_revision=1, approved_at=None, status="transcribed_pending", transcription_quality={})
        document = SimpleNamespace(current_segments=song["segments"], original_segments=song["original_segments"],
                                   revision=0, quality_proposal=None)
        def authorize(database, job_id, current_user):
            if denied:
                raise HTTPException(404, "Job not found")
            return job, document
        api.register_routes(application, get_current_user=lambda: user, get_db=lambda: db, authorize_editor=authorize)
        return TestClient(application), db

    @patch.dict(os.environ, {"ENVIRONMENT": "staging", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1"})
    def test_admin_upload_uses_existing_authorization(self):
        client, _ = self.setup_client(denied=True)
        with patch.object(api, "publish_candidate") as publish:
            response = client.post("/editor/song1/genly-model/candidate", json={})
        self.assertEqual(response.status_code, 404)
        publish.assert_not_called()

    @patch.dict(os.environ, {"ENVIRONMENT": "staging", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1"})
    def test_regular_editor_cannot_forge_model_upload(self):
        client, _ = self.setup_client(role="editor")
        response = client.post("/editor/song1/genly-model/candidate", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/editor/song1/genly-model/request").status_code, 200)

    @patch.dict(os.environ, {"ENVIRONMENT": "production", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1"})
    def test_production_remains_disabled(self):
        client, _ = self.setup_client()
        self.assertEqual(client.get("/editor/song1/genly-model/request").status_code, 404)

    @patch.dict(os.environ, {"ENVIRONMENT": "staging", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1"})
    def test_test_copy_requires_admin(self):
        client, _ = self.setup_client(role="editor")
        with patch.object(api, "create_test_copy") as create:
            result = client.post("/admin/genly-model/test-copy", json={"source_job_id": "abcdef123456"})
        self.assertEqual(result.status_code, 403)
        create.assert_not_called()

    @patch.dict(os.environ, {"ENVIRONMENT": "staging", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1"})
    def test_test_copy_publishes_only_after_commit(self):
        client, db = self.setup_client()
        with patch.object(api, "create_test_copy", return_value={"job_id": "new", "learning_eligible": False}) as create:
            result = client.post("/admin/genly-model/test-copy", json={"source_job_id": "abcdef123456"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["job_id"], "new")
        create.assert_called_once()
        db.commit.assert_called_once()


class EditorGuardTests(unittest.TestCase):
    @patch.dict(os.environ, {"ENVIRONMENT": "staging", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1",
                            "QUALITY_OPERATOR_SUGGESTIONS_ENABLED": "1", "QUALITY_TIMING_OPERATOR_SUGGESTIONS_ENABLED": "1"})
    def test_pending_local_proposal_is_preserved_from_native_replacement(self):
        import editor
        proposal = {"local_genly_model": {"changes_text": True, "changes_timing": True},
                    "status": "pending", "base_revision": 2,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        document = SimpleNamespace(quality_proposal=proposal, revision=2)
        self.assertTrue(editor._pending_local_model_proposal(document))
        first, second = Mock(), Mock()
        first.filter.return_value.with_for_update.return_value.first.return_value = SimpleNamespace(job_id="song")
        second.filter.return_value.with_for_update.return_value.first.return_value = document
        db = Mock()
        db.query.side_effect = [first, second]
        native = {"kind": "operator_review_proposal", "schema": "operator-review-proposal-v1",
                  "review_only": True, "operator_suggestion_only": True, "automatic_apply_allowed": False}
        self.assertFalse(editor.persist_operator_review_proposal_if_current(db, job_id="song", expected_revision=2,
                          expected_segments_hash="x", expected_audio_revision=1, expected_audio_sha256="a" * 64,
                          proposal=native))
        self.assertIs(document.quality_proposal, proposal)

    @patch.dict(os.environ, {"ENVIRONMENT": "staging", "GENLY_LOCAL_MODEL_CANDIDATES_ENABLED": "1",
                            "QUALITY_OPERATOR_SUGGESTIONS_ENABLED": "1", "QUALITY_TIMING_OPERATOR_SUGGESTIONS_ENABLED": "0"})
    def test_text_gate_does_not_enable_a_joint_timing_change(self):
        import editor
        self.assertFalse(editor.reviewer_candidate_enabled({"local_genly_model": {"changes_text": True, "changes_timing": True}}))
        self.assertTrue(editor.reviewer_candidate_enabled({"local_genly_model": {"changes_text": True, "changes_timing": False}}))


if __name__ == "__main__":
    unittest.main()
