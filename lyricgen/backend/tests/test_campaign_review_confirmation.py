"""Campaign reviewers can attest their saved work without gaining tenant access."""
from copy import deepcopy
import asyncio
import inspect
from datetime import datetime, timezone
import uuid

import pytest

from database import AuditLog, BatchCampaign, BatchCampaignReviewer, Job
from language_review import review_payload
from tests.conftest import auth
from transcription_quality import evaluate, segments_hash


@pytest.fixture
def shared_review(client, user_token, admin_token, db, monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "100")
    reviewer = client.get("/auth/me", headers=auth(user_token)).json()
    owner = client.get("/auth/me", headers=auth(admin_token)).json()
    campaign_id, job_id, private_id = [uuid.uuid4().hex[:12] for _ in range(3)]
    segments = [
        {"start": 1, "end": 4, "text": "Yo quiero volver a mi casa contigo"},
        {"start": 5, "end": 8, "text": "Tu corazón me recuerda nuestra historia"},
        {"start": 9, "end": 12, "text": "No puedo olvidarte cuando llega la noche"},
    ]
    quality = evaluate(segments, {"audio_coverage": .8, "text_mismatches": 1,
        "voiced_gap_s": 0, "uncovered_seconds": 0}, unsafe_windows=[{
            "id": "qw_review", "start": 1, "end": 4,
            "reasons": ["text_mismatch"], "segment_indices": [0],
        }])
    quality["evaluated_revision"] = 7
    quality["reference_hypothesis"] = {"reference_text": (
        "The river flows between the mountains and the city\n"
        "We watch the morning light across the distant sea\n"
        "They will remember all the stars above the sky"
    )}
    assert review_payload(segments, quality, 7)["needs_language_review"]
    db.add(BatchCampaign(id=campaign_id, tenant_id=owner["tenant_id"],
        created_by=owner["id"], name="Shared review", kind="lyric_video"))
    db.flush()
    grant = BatchCampaignReviewer(campaign_id=campaign_id, user_id=reviewer["id"])
    db.add(grant)
    for ident in (job_id, private_id):
        db.add(Job(job_id=ident, user_id=owner["id"], tenant_id=owner["tenant_id"],
            campaign_id=campaign_id if ident == job_id else None,
            filename="song.wav", artist="Rescate", status="transcribed_pending",
            segments_json=deepcopy(segments), segments_revision=7,
            transcription_quality=deepcopy(quality)))
    db.commit()
    yield {"job_id": job_id, "private_id": private_id, "grant": grant,
           "headers": auth(user_token), "reviewer": reviewer, "segments": segments}
    db.query(AuditLog).filter(AuditLog.detail["job_id"].as_string().in_([job_id, private_id])).delete(synchronize_session=False)
    db.query(Job).filter(Job.job_id.in_([job_id, private_id])).delete(synchronize_session=False)
    db.query(BatchCampaignReviewer).filter_by(campaign_id=campaign_id).delete()
    db.query(BatchCampaign).filter_by(id=campaign_id).delete()
    db.commit()


ENDPOINTS = [
    ("language-resolution", "language_resolution", "lyrics.language_review_resolved"),
    ("transcription-quality/acknowledge", "acknowledgement", "lyrics.quality_acknowledged"),
]


def post_confirmation(client, review, endpoint, **overrides):
    body = {"base_revision": 7}
    if endpoint.endswith("acknowledge"):
        body["confirmed_window_ids"] = ["qw_review"]
    body.update(overrides)
    return client.post(f"/jobs/{review['job_id']}/{endpoint}",
                       headers=review["headers"], json=body)


@pytest.mark.parametrize("endpoint,record_key,action", ENDPOINTS)
def test_granted_reviewer_can_confirm_exact_saved_revision(client, db, shared_review, endpoint, record_key, action):
    result = post_confirmation(client, shared_review, endpoint)
    assert result.status_code == 200, result.text
    db.expire_all()
    job = db.query(Job).filter_by(job_id=shared_review["job_id"]).one()
    record = job.transcription_quality[record_key]
    assert record["revision"] == 7
    assert record["segments_hash"] == segments_hash(shared_review["segments"])
    assert record["user_id"] == shared_review["reviewer"]["id"]
    assert job.segments_json == shared_review["segments"]
    assert job.status == "transcribed_pending"  # confirmation is not approval
    audit = db.query(AuditLog).filter(AuditLog.action == action,
        AuditLog.detail["job_id"].as_string() == job.job_id).one()
    assert audit.user_id == shared_review["reviewer"]["id"]


@pytest.mark.parametrize("endpoint,record_key,_action", ENDPOINTS)
def test_grant_does_not_remove_revision_check(client, db, shared_review, endpoint, record_key, _action):
    result = post_confirmation(client, shared_review, endpoint, base_revision=6)
    assert result.status_code == 409, result.text
    assert result.json()["code"] == "stale_revision"
    db.expire_all()
    assert record_key not in db.query(Job).filter_by(job_id=shared_review["job_id"]).one().transcription_quality


@pytest.mark.parametrize("endpoint,_record,_action", ENDPOINTS)
@pytest.mark.parametrize("denial", ["revoked", "unrelated_job", "moved_tenant", "missing_grant"])
def test_confirmation_grant_remains_narrow(client, db, shared_review, endpoint, _record, _action, denial):
    if denial == "revoked":
        shared_review["grant"].revoked_at = datetime.now(timezone.utc)
    elif denial == "missing_grant":
        db.delete(shared_review["grant"])
    elif denial == "unrelated_job":
        shared_review = {**shared_review, "job_id": shared_review["private_id"]}
    else:
        db.query(Job).filter_by(job_id=shared_review["job_id"]).one().tenant_id = "unrelated-team"
    db.commit()
    result = post_confirmation(client, shared_review, endpoint)
    assert result.status_code == 404, result.text


def test_reviewer_must_still_confirm_every_quality_window(client, shared_review):
    result = post_confirmation(client, shared_review, "transcription-quality/acknowledge", confirmed_window_ids=[])
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "unsafe_windows_not_confirmed"


@pytest.mark.parametrize("endpoint,_record,_action", ENDPOINTS)
@pytest.mark.parametrize("outcome", ["stale", "revoked", "success_and_repeat"])
def test_confirmation_releases_locks_before_dependency_teardown(
    client, db, shared_review, endpoint, _record, _action, outcome,
):
    import main
    from fastapi import HTTPException
    from starlette.requests import Request
    from database import SessionLocal, engine

    if outcome == "revoked":
        shared_review["grant"].revoked_at = datetime.now(timezone.utc)
        db.commit()
    acknowledgement = endpoint.endswith("acknowledge")
    handler = inspect.unwrap(main.acknowledge_transcription_quality if acknowledgement
                             else main.resolve_language_review)
    body_data = {"base_revision": 6 if outcome == "stale" else 7}
    if acknowledgement:
        body_data["confirmed_window_ids"] = ["qw_review"]
    body = (main.TranscriptionQualityAckRequest if acknowledgement
            else main.LanguageResolutionRequest)(**body_data)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})
    for _ in range(2 if outcome == "success_and_repeat" else 1):
        try:
            result = asyncio.run(handler(request=request, job_id=shared_review["job_id"],
                body=body, current_user=shared_review["reviewer"], db=db))
            assert outcome != "revoked"
            if outcome == "stale":
                assert result.status_code == 409
            else:
                assert result["ok"] is True
        except HTTPException as exc:
            assert outcome == "revoked" and exc.status_code == 404
        assert not db.in_transaction()
        if engine.dialect.name == "postgresql":
            with SessionLocal() as contender:
                assert contender.query(Job).filter_by(job_id=shared_review["job_id"]).with_for_update(nowait=True).one()
