from copy import deepcopy

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
