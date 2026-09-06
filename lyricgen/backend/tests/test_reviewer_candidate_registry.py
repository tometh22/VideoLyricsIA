from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from reviewer_candidate import build_candidate
from reviewer_candidate_registry import (
    candidate_for_editor, prepare_registry_record, register_candidate,
)
from tests.test_reviewer_batch_bridge import fixture as review_fixture


def setup_registry(monkeypatch, tmp_path, *, no_changes=False):
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_CACHE_DIR", str(tmp_path))
    song, candidate, review = review_fixture()
    if no_changes:
        candidate = build_candidate(song)
    job = SimpleNamespace(job_id=song["job_id"], tenant_id="tenant",
        audio_revision=song["audio_revision"], input_audio_sha256=song["audio_sha256"], status="ready")
    document = SimpleNamespace(job_id=song["job_id"], tenant_id="tenant",
        revision=song["segments_revision"], current_segments=deepcopy(song["segments"]))
    return song, candidate, review, job, document


@pytest.mark.parametrize("no_changes", [False, True])
def test_complete_candidate_with_or_without_edits_is_associated(monkeypatch, tmp_path, no_changes):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path, no_changes=no_changes)
    before = deepcopy(document.current_segments)
    result = register_candidate("tenant", song, candidate, review)
    assert result["created"] is True
    payload = candidate_for_editor(job, document)
    assert payload["segments"] == candidate["segments"]
    assert payload["review_complete"] is True
    assert payload["correctness_certified"] is False
    assert payload["approved"] is False
    assert payload["read_only"] is True
    assert document.current_segments == before
    assert register_candidate("tenant", song, candidate, review)["created"] is False


@pytest.mark.parametrize("mutation", [
    lambda j,d: setattr(j, "audio_revision", 2),
    lambda j,d: setattr(j, "input_audio_sha256", "b" * 64),
    lambda j,d: setattr(d, "revision", 2),
    lambda j,d: d.current_segments[0].update(end=4.5),
    lambda j,d: setattr(d, "tenant_id", "other"),
    lambda j,d: setattr(j, "job_id", "other"),
])
def test_live_identity_mismatch_never_serves_candidate(monkeypatch, tmp_path, mutation):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    mutation(job, document)
    assert candidate_for_editor(job, document) is None


def test_approved_song_stays_read_only(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    job.status = "lyrics_approved"
    payload = candidate_for_editor(job, document)
    assert payload["read_only"] and payload["current_song_approved"]
    assert job.status == "lyrics_approved"


def test_expired_record_not_served(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    created = datetime(2026, 9, 6, tzinfo=timezone.utc)
    register_candidate("tenant", song, candidate, review, now=created)
    assert candidate_for_editor(job, document, now=created + timedelta(days=8)) is None


def test_conflicting_candidate_cannot_overwrite_same_source(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    with pytest.raises(ValueError, match="immutable_candidate_conflict"):
        register_candidate("tenant", song, build_candidate(song), review)
    assert candidate_for_editor(job, document)["segments"] == candidate["segments"]


def test_tampering_fails_closed(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    path = next((tmp_path / "complete_candidates").glob("*.json"))
    data = json.loads(path.read_text())
    data["payload"]["segments"][0]["text"] = "tampered"
    path.write_text(json.dumps(data))
    assert candidate_for_editor(job, document) is None


def test_off_switch_never_reads_or_writes(monkeypatch, tmp_path):
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    monkeypatch.setenv("REVIEWER_ASSIST_CACHE_DIR", str(tmp_path))
    assert register_candidate(None, {}, {}, {})["reason"] == "reviewer_assist_disabled"
    assert candidate_for_editor(None, None) is None
    assert list(tmp_path.iterdir()) == []


def test_preparation_possible_with_flag_off(monkeypatch):
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    record = prepare_registry_record("tenant", *review_fixture())
    assert record["payload"]["review_complete"] is True


def test_localized_reconciliation_doubts_survive_safe_rebuild():
    song, candidate, review = review_fixture()
    review["localized_doubts"] = [{"line_index": 0, "start": 2., "end": 4.,
        "reason": "uncertain_word_occurrence"}]
    review["private_path"] = "/not/for/client"
    record = prepare_registry_record("tenant", song, candidate, review)
    assert record["payload"]["review_details"]["localized_doubts"] == review["localized_doubts"]
    assert "private_path" not in record["payload"]["review_details"]


def test_interrupted_serialization_never_publishes_partial_record(monkeypatch, tmp_path):
    song, candidate, review, _, _ = setup_registry(monkeypatch, tmp_path)
    def fail_dump(value, output, **kwargs):
        output.write("partial")
        raise OSError("interrupted")
    monkeypatch.setattr("reviewer_candidate_registry.json.dump", fail_dump)
    with pytest.raises(OSError, match="interrupted"):
        register_candidate("tenant", song, candidate, review)
    assert list((tmp_path / "complete_candidates").iterdir()) == []


def test_held_evidence_and_private_diagnostics_not_sent_to_editor():
    song, candidate, review = review_fixture()
    review["held_decisions"] = [{"reason": "ambiguous", "decision": {
        "proposal_id": "p", "window": {"line_index": 0},
        "evidence": [{"private_path": "/secret", "text": "raw provider response"}]}}]
    review["line_diagnostics"] = [{"line_index": 0, "phrase_status": "unresolved",
        "private_path": "/secret", "exact": {"raw": "large alignment"}}]
    record = prepare_registry_record("tenant", song, candidate, review)
    details = record["payload"]["review_details"]
    assert details["held_decisions"] == [{"reason": "ambiguous", "proposal_id": "p", "line_index": 0}]
    assert details["line_diagnostics"] == [{"line_index": 0, "phrase_status": "unresolved"}]


def test_editor_get_fetches_only_after_existing_authorization(monkeypatch):
    import asyncio
    # This is a local route-wiring fixture, not a production config fixture.
    # Do not depend on another test having imported main under development.
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
    import main
    calls = []
    job = SimpleNamespace(transcription_quality=None, segments_json=[], artist="Artist",
        song_title="Song", filename="audio.wav", status="ready")
    document = object()
    def authorize(db, job_id, user):
        calls.append("authorized")
        return job, document
    def fetch(actual_job, actual_document):
        assert actual_job is job and actual_document is document
        calls.append("registry")
        return {"id": "candidate", "read_only": True}
    monkeypatch.setattr(main, "_editor_document_or_404", authorize)
    monkeypatch.setattr(main, "_audit_cross_tenant_access", lambda *a, **k: None)
    monkeypatch.setattr(main, "revoke_quality_proposal_if_disabled", lambda *a: None)
    monkeypatch.setattr(main, "serialize_document", lambda *a: {})
    monkeypatch.setattr("reviewer_candidate_registry.candidate_for_editor", fetch)
    result = asyncio.run(main.get_editor_document("song", {"id": 1}, SimpleNamespace(commit=lambda: None)))
    assert calls == ["authorized", "registry"]
    assert result["reviewer_candidate"]["read_only"] is True
    def deny(*args):
        raise main.HTTPException(status_code=404)
    monkeypatch.setattr(main, "_editor_document_or_404", deny)
    with pytest.raises(main.HTTPException):
        asyncio.run(main.get_editor_document("song", {"id": 1}, None))
    assert calls == ["authorized", "registry"]
