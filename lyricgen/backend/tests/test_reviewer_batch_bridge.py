from copy import deepcopy
from types import SimpleNamespace

import pytest

from reviewer_batch_bridge import REQUIRED_AUDIO_FAMILIES, prepare_batch_candidate, publish_batch_candidate
from reviewer_candidate import build_candidate
from reviewer_shadow import review_window, source_binding
from shadow_reference_import import digest


def fixture():
    segments = [{"text": "Canto así", "start": 2., "end": 4.},
                {"text": "No cambiar", "start": 6., "end": 8., "locked": True}]
    song = {"job_id": "test", "audio_sha256": "a" * 64, "audio_revision": 1,
        "segments_revision": 1, "segments": segments,
        "segments_sha256": digest(segments), "duration_seconds": 10.}
    evidence = [{"kind": "content", "family": f, "text": "Canto aquí",
        "tool_status": "ok", "received_audio": True, "conditioning_texts": [],
        "occurrence_verified": True} for f in ["whisper-1", "gemini"]]
    decision = review_window(song, {"line_index": 0, "start": 1., "end": 5.,
        "offset_seconds": 1.}, evidence=evidence, commit="a" * 40)
    candidate = build_candidate(song, [decision])
    review = {"schema": "full-song-review-v1", "source": source_binding(song),
        "reconciliation_complete": True, "required_families": sorted(REQUIRED_AUDIO_FAMILIES),
        "audio_evidence": [{"source": source_binding(song), "family": f,
            "tool_status": "ok", "received_audio": True, "start": start, "end": end,
            "clock": "original_mix_decoded", "evidence_sha256": digest([f, start])}
            for f in sorted(REQUIRED_AUDIO_FAMILIES) for start, end in [(0., 6.), (5., 10.)]]}
    return song, candidate, review


def test_complete_candidate_prepares_existing_proposal_without_mutation():
    inputs = fixture()
    original = deepcopy(inputs)
    result = prepare_batch_candidate(*inputs)
    assert inputs == original
    assert result["coverage_seconds"] == {f: 10. for f in REQUIRED_AUDIO_FAMILIES}
    assert result["proposal"]["reviewer_assist"]["candidate"]["segments"][0]["text"] == "Canto aquí"
    assert result["candidate"]["segments"][1] == inputs[0]["segments"][1]
    assert result["automatic_apply_allowed"] is False
    assert result["approved"] is False


@pytest.mark.parametrize("mutation,error", [
    (lambda s,c,r: r.update(required_families=["one", "two"]), "frozen_independent_audio_families_required"),
    (lambda s,c,r: r.update(reconciliation_complete=False), "reconciliation_incomplete"),
    (lambda s,c,r: r["audio_evidence"].pop(), "audio_coverage_incomplete"),
    (lambda s,c,r: r["audio_evidence"][0].update(start=.1), "audio_coverage_incomplete"),
    (lambda s,c,r: r["audio_evidence"][0].update(tool_status="error"), "audio_coverage_incomplete"),
    (lambda s,c,r: r["audio_evidence"][0].update(clock="stem"), "coverage_clock_unverified"),
    (lambda s,c,r: r["audio_evidence"][0].update(source={}), "stale_audio_evidence"),
    (lambda s,c,r: s.update(audio_revision=2), "stale_proposal"),
    (lambda s,c,r: s.update(segments_revision=2), "stale_proposal"),
    (lambda s,c,r: c["segments"][1].update(end=9.), "candidate_contains_unbacked_changes"),
])
def test_incomplete_stale_or_unsupported_candidate_rejected(mutation, error):
    values = fixture()
    mutation(*values)
    with pytest.raises(ValueError, match=error):
        prepare_batch_candidate(*values)


def test_held_dubious_decision_excluded_from_bulk_candidate():
    song, candidate, review = fixture()
    review["held_decision_ids"] = [candidate["decision_evidence"][0]["proposal_id"]]
    result = prepare_batch_candidate(song, candidate, review)
    assert result["proposal"] is None
    assert result["candidate"]["segments"] == song["segments"]
    assert result["ready_for_human_review"] is True


def test_no_change_is_reviewable_not_certified():
    song, _, review = fixture()
    result = prepare_batch_candidate(song, build_candidate(song), review)
    assert result["proposal"] is None
    assert result["candidate"]["residual_qc"]["independently_verified_lines"] == []


def test_unlocked_human_edit_is_not_bulk_adoptable_even_with_same_id():
    song, candidate, review = fixture()
    original = deepcopy(song["segments"])
    original[0]["text"] = "Original machine text"
    result = prepare_batch_candidate(song, candidate, review, original_segments=original)
    assert result["proposal"] is None
    assert result["candidate"]["segments"] == song["segments"]


def test_disabled_type_excluded_from_embedded_candidate():
    result = prepare_batch_candidate(*fixture(), allowed_suggestion_types=("timing",))
    assert result["proposal"] is None
    assert result["candidate"]["changes"] == []


def test_conflicting_duplicate_decisions_cannot_escape_candidate_rollback():
    song, candidate, review = fixture()
    decision = candidate["decision_evidence"][0]
    candidate = build_candidate(song, [decision, decision])
    result = prepare_batch_candidate(song, candidate, review)
    assert result["proposal"] is None
    assert result["candidate"]["segments"] == song["segments"]


def test_default_off_publication_never_accesses_database(monkeypatch):
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    assert publish_batch_candidate(None, {}, {}, {}) == {
        "published": False, "reason": "reviewer_assist_disabled"}


def test_publication_rederives_edit_provenance_from_locked_history(monkeypatch):
    """A cached/forged receipt must never decide migration ownership."""
    song, candidate, review = fixture()
    song.update(campaign_id="fixture-campaign", edit_provenance={"forged": True})
    job = SimpleNamespace(
        campaign_id="fixture-campaign", audio_revision=song["audio_revision"],
        input_audio_sha256=song["audio_sha256"], status="ready",
        approved_at=None,
    )
    document = SimpleNamespace(
        current_segments=deepcopy(song["segments"]),
        original_segments=deepcopy(song["segments"]),
        revision=song["segments_revision"], quality_proposal=None,
        lock_user_id=None, lock_expires_at=None, approved_at=None,
    )

    class Query:
        def __init__(self, value):
            self.value = value

        def filter(self, *args, **kwargs):
            return self

        def populate_existing(self):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return self.value

    class DB:
        def query(self, model):
            from database import EditorDocument, Job
            return Query(job if model is Job else document if model is EditorDocument else None)

    receipt = {"schema": "reviewer-edit-provenance-v1", "lines": []}
    captured = {}

    def fake_prepare(live, *_args, **_kwargs):
        captured["song"] = live
        return {"proposal": {"kind": "operator_review_proposal"}}

    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_CAMPAIGN_ID", "fixture-campaign")
    monkeypatch.setenv("QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "1")
    monkeypatch.setenv("QUALITY_TIMING_OPERATOR_SUGGESTIONS_ENABLED", "0")
    monkeypatch.setattr("reviewer_batch_bridge.prepare_batch_candidate", fake_prepare)
    monkeypatch.setattr("reviewer_edit_provenance.from_database", lambda *_args: receipt)
    monkeypatch.setattr("editor.persist_operator_review_proposal_if_current", lambda *_args, **_kwargs: True)

    result = publish_batch_candidate(DB(), song, candidate, review)

    assert result["published"] is True
    assert captured["song"]["edit_provenance"] is receipt
    assert captured["song"]["edit_provenance"] != song["edit_provenance"]
