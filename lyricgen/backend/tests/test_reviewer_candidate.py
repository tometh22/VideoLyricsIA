from copy import deepcopy

import pytest

from reviewer_candidate import build_candidate, interpretation
from reviewer_shadow import plan_windows, review_window
from shadow_reference_import import digest


def song():
    rows = [{"text": "Canto así", "start": 2., "end": 4.},
            {"text": "Canto así", "start": 6., "end": 8., "locked": True}]
    return {"job_id": "test", "audio_sha256": "a" * 64, "audio_revision": 1,
            "segments_revision": 1, "segments": rows, "segments_sha256": digest(rows),
            "duration_seconds": 10.}


def test_complete_unchanged_candidate_is_not_certified():
    source = song()
    before = deepcopy(source)
    result = build_candidate(source)
    assert result["segments"] == source["segments"]
    assert result["candidate_sha256"] == result["baseline_sha256"]
    assert result["residual_qc"]["all_lines_structurally_checked"] == 2
    assert result["residual_qc"]["complete_audio_coverage_verified"] is False
    assert result["approved"] is False
    assert source == before


def test_repetitions_have_distinct_occurrence_ids():
    nodes = interpretation(song(), [])["occurrences"]
    assert nodes[0]["phrase_key"] == nodes[1]["phrase_key"]
    assert nodes[0]["id"] != nodes[1]["id"]
    assert [n["occurrence_number"] for n in nodes] == [1, 2]


def test_locked_line_survives_supported_text_change():
    source = song()
    w = {"line_index": 1, "start": 5., "end": 9., "offset_seconds": 5.}
    evidence = [{"kind": "content", "family": f, "text": "Canto aquí",
        "tool_status": "ok", "received_audio": True, "conditioning_texts": [],
        "occurrence_verified": True} for f in ["whisper-1", "gemini"]]
    decision = review_window(source, w, evidence=evidence, commit="a" * 40)
    result = build_candidate(source, [decision])
    assert result["changes"] == []
    assert result["segments"][1] == source["segments"][1]


def test_repair_is_isolated_and_does_not_lock_machine_output():
    source = song()
    w = {"line_index": 0, "start": 1., "end": 5., "offset_seconds": 1.}
    evidence = [{"kind": "content", "family": f, "text": "Canto aquí",
        "tool_status": "ok", "received_audio": True, "conditioning_texts": [],
        "occurrence_verified": True} for f in ["whisper-1", "gemini"]]
    d = review_window(source, w, evidence=evidence, commit="a" * 40)
    result = build_candidate(source, [d])
    assert result["segments"][0]["text"] == "Canto aquí"
    assert not result["segments"][0].get("locked")
    assert source["segments"][0]["text"] == "Canto así"
    assert result["segments"][1] == source["segments"][1]
    assert result["changes"][0]["human_decision"] is False
    source["audio_revision"] += 1
    with pytest.raises(ValueError, match="stale"):
        build_candidate(source, [d])


def test_qc_checks_unmodified_rows_outside_repaired_windows():
    source = song()
    source["segments"][1]["end"] = 12.
    source["segments_sha256"] = digest(source["segments"])
    result = build_candidate(source)
    assert result["residual_qc"]["timeline"][0]["reason"] == "invalid_timeline"


def test_runtime_off_and_missing_cache_do_not_call_audio(monkeypatch):
    from reviewer_assist_runtime import run_snapshot
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    assert run_snapshot("x", {}, None, None) == (None, {"enabled": False})
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.delenv("REVIEWER_ASSIST_CACHE_DIR", raising=False)
    assert run_snapshot("x", {}, None, None)[1]["provider_calls"] == 0
    assert run_snapshot("x", {"approved_at": "2026-09-05"}, None, None)[1]["reason"] == "human_approval_preserved"


def test_applied_candidate_later_edit_is_recorded_without_reapplication():
    from types import SimpleNamespace
    from editor import rebase_operator_suggestions_after_manual_edit
    before = [{"text": "Canto aquí", "start": 2., "end": 4.}]
    after = [{"text": "Canto allá", "start": 2., "end": 4.}]
    proposal = {"id": "x", "status": "applied", "reviewer_assist": {
        "accepted_windows": [{"id": "w", "proposed_segments": before}]}}
    doc = SimpleNamespace(current_segments=deepcopy(after), quality_proposal=None, revision=3)
    events = rebase_operator_suggestions_after_manual_edit(doc, proposal, before)
    assert events[0]["decision"] == "edited_after_accept"
    assert doc.current_segments == after


def test_candidate_flag_blocks_serving_independently(monkeypatch):
    from editor import reviewer_candidate_enabled
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    assert not reviewer_candidate_enabled({"reviewer_assist": {"version": "v1"}})
    assert reviewer_candidate_enabled({"operator_suggestion_only": True})


def test_interval_does_not_invent_word_end_at_pitch_change():
    from reviewer_endpoint_interval import bracket_sustain
    result = bracket_sustain([0., .01, .02], [200., 200., 400.], [True]*3,
        anchor_start=0., anchor_end=.01, ceiling=.03)
    assert result["status"] == "pitch_changed_not_word_end"
    assert result["interval"] is None


def test_worker_builds_complete_candidate_via_existing_proposal_schema(monkeypatch, tmp_path):
    import reviewer_assist_runtime as runtime
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.setenv("QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "file_sha", lambda _: "a" * 64)
    monkeypatch.setattr(runtime, "probe", lambda _: 10.)
    monkeypatch.setattr(runtime, "extract_clip", lambda *args: None)
    monkeypatch.setattr(runtime, "align_phrase", lambda *args: {"status": "aligned_hypothesis", "words": []})
    class Listener:
        def __init__(self, *args, **kwargs): self.calls = 0
        def listen(self, clip, *, provider, source, **kwargs):
            self.calls += 1
            return {"family": "whisper-1" if provider == "openai" else "gemini",
                "source": source, "tool_status": "ok", "received_audio": True,
                "conditioning_texts": [], "response": {"text": "Cómo es tripartito sobra el apetito"}}
    monkeypatch.setattr(runtime, "BlindAudioTools", Listener)
    rows = [{"text": "Cómo estripartito sobra el apetito", "start": 2., "end": 4.},
            {"text": "Otra línea", "start": 6., "end": 8., "locked": True}]
    before = deepcopy(rows)
    proposal, report = runtime.run_snapshot("test", {"segments": rows,
        "revision": 1, "audio_revision": 1, "audio_sha256": "a" * 64,
        "quality": {"unsafe_windows": [{"start": 1., "end": 5.}]}}, "mix", "stem")
    assert report["provider_calls"] == 2
    assert proposal["schema"] == "operator-review-proposal-v1"
    assert len(proposal["reviewer_assist"]["candidate"]["segments"]) == 2
    assert proposal["reviewer_assist"]["candidate"]["segments"][1] == before[1]
    assert rows == before


def test_candidate_counts_survive_existing_analytics_sanitizer():
    from quality_jobs import _sanitize_analytical_evidence
    payload = {"reviewer_assist": {"enabled": True, "provider_calls": 2,
        "processed_windows": 1, "generated": 1, "tool_errors": 0}}
    assert _sanitize_analytical_evidence(payload) == payload


def test_human_candidate_adoption_is_versioned_but_not_approved_or_locked(db, monkeypatch):
    import uuid
    from database import Job
    from editor import ensure_document, persist_operator_review_proposal_if_current, apply_quality_proposal
    from operator_review_proposals import build_operator_review_proposal
    from transcription_quality import segments_hash
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.setenv("QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "1")
    rows = [{"text": "Canto así", "start": 2., "end": 4.}]
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id="reviewer-test",
        filename="test.wav", artist="Test", style="oscuro", status="transcribed_pending",
        segments_json=rows, audio_revision=1, input_audio_sha256="a" * 64)
    db.add(job)
    db.flush()
    doc = ensure_document(db, job.job_id, job.tenant_id, rows)
    proposal, _ = build_operator_review_proposal(rows, text_candidates=[{
        "kind": "operator_review_candidate", "id": "test-window", "suggestion_type": "text",
        "start": 2., "end": 4., "current_segments": rows,
        "proposed_segments": [{"text": "Canto aquí", "start": 2., "end": 4.}]}])
    proposal["reviewer_assist"] = {"version": "v1"}
    assert persist_operator_review_proposal_if_current(db, job_id=job.job_id,
        expected_revision=doc.revision, expected_segments_hash=segments_hash(rows),
        expected_audio_revision=1, expected_audio_sha256="a" * 64, proposal=proposal)
    doc, version, applied = apply_quality_proposal(db, job, doc, 1,
        proposal_id=doc.quality_proposal["id"], base_revision=doc.revision,
        window_ids=["test-window"], idempotency_key=str(uuid.uuid4()))
    assert applied and version.reason == "reviewer_candidate"
    assert job.approved_at is None
    assert not doc.current_segments[0].get("locked")
    assert not doc.current_segments[0].get("operator_locked")
    assert doc.quality_proposal["reviewer_assist"]["accepted_windows"][0]["id"] == "test-window"


def test_existing_backend_receipts_join_exposure_and_later_edits():
    from reviewer_assist import operational_counts
    result = operational_counts(["w"], [
        {"id": 1, "properties": {"kind": "shown", "proposal_id": "w"}},
        {"id": 2, "properties": {"decision": "accepted", "window_id": "w"}},
        {"id": 3, "properties": {"decision": "edited_after_accept", "window_id": "w"}},
    ])
    assert result["shown"] == result["accepted"] == result["edited_after_accept"] == 1
    assert result["unexamined"] == 0 and result["rejected"] == 0
