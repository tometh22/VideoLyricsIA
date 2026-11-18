"""One explicit campaign grant opens its review, never the owner's workspace."""

import hashlib
import uuid
from datetime import datetime, timezone

from auth import create_user, start_login_session
from database import (
    BatchCampaign, BatchCampaignItem, BatchCampaignReviewer, EditorDocument,
    EditorVersion, Job, JobOutboxEvent, ProductEvent, SessionLocal,
)
from tests.conftest import auth


def test_cross_tenant_reviewer_can_correct_only_the_granted_campaign(client, monkeypatch):
    import main
    import storage
    import waveform_compute

    suffix = uuid.uuid4().hex[:8]
    owner_tenant = f"review-owner-{suffix}"
    reviewer_tenant = f"reviewer-{suffix}"
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", f"{owner_tenant},{reviewer_tenant}")
    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", lambda _id: None)
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "object_exists", lambda _key: True)
    monkeypatch.setattr(storage, "generate_signed_url", lambda *_a, **_kw: "https://example.test/audio")
    monkeypatch.setattr(waveform_compute, "compute_and_cache_waveform", lambda *_a: {
        "peaks": [0.2, 0.5], "duration": 2,
    })

    campaign_id = uuid.uuid4().hex[:12]
    item_id = str(uuid.uuid4())
    job_id = uuid.uuid4().hex[:12]
    other_job_id = uuid.uuid4().hex[:12]
    segments = [{"start": 0, "end": 1, "text": "verso inicial"}]
    with SessionLocal() as db:
        owner = create_user(db, f"review_owner_{suffix}", "testpass12345", tenant_id=owner_tenant)
        reviewer = create_user(db, f"review_user_{suffix}", "testpass12345", tenant_id=reviewer_tenant)
        outsider = create_user(db, f"review_other_{suffix}", "testpass12345", tenant_id=f"outside-{suffix}")
        reviewer_token = start_login_session(db, reviewer)
        outsider_token = start_login_session(db, outsider)
        db.add(BatchCampaign(
            id=campaign_id, tenant_id=owner_tenant, created_by=owner.id,
            name="Review only", kind="lyric_video", status="active", expected_count=1,
        ))
        db.add(BatchCampaignItem(
            id=item_id, campaign_id=campaign_id, tenant_id=owner_tenant,
            ordinal=1, filename="song.wav", title="Song", artist="Artist",
            technical_code=f"T{suffix}", size_bytes=100, duration_seconds=2,
            sha256=hashlib.sha256(b"song").hexdigest(), upload_state="uploaded",
        ))
        db.add(Job(
            job_id=job_id, user_id=owner.id, tenant_id=owner_tenant,
            campaign_id=campaign_id, campaign_item_id=item_id, workload_class="batch",
            filename="song.wav", artist="Artist", song_title="Song",
            status="transcribed_pending", segments_json=segments,
            input_r2_key=f"inputs/{owner_tenant}/{job_id}/song.wav",
        ))
        db.add(Job(
            job_id=other_job_id, user_id=owner.id, tenant_id=owner_tenant,
            filename="private.wav", artist="Other", song_title="Private",
            status="transcribed_pending", segments_json=segments,
        ))
        db.add(BatchCampaignReviewer(campaign_id=campaign_id, user_id=reviewer.id))
        db.commit()
        reviewer_id = reviewer.id

    try:
        headers = auth(reviewer_token)
        outsider_headers = auth(outsider_token)
        listed = client.get("/batch/campaigns", headers=headers)
        assert listed.status_code == 200
        assert campaign_id in {row["id"] for row in listed.json()["items"]}
        pipeline = client.get(f"/batch/campaigns/{campaign_id}/pipeline", headers=headers)
        assert pipeline.status_code == 200
        assert pipeline.json()["can_manage"] is False
        assert job_id in {row["job_id"] for row in pipeline.json()["items"]}
        queue = client.get(f"/batch/campaigns/{campaign_id}/review-queue", headers=headers)
        assert queue.status_code == 200
        assert job_id in {row["job_id"] for row in queue.json()["items"]}
        assert client.get(f"/status/{job_id}", headers=headers).status_code == 200
        loaded = client.get(f"/editor/{job_id}", headers=headers)
        assert loaded.status_code == 200
        updated = [dict(segments[0], text="verso corregido")]
        saved = client.patch(f"/editor/{job_id}", headers=headers, json={
            "base_revision": loaded.json()["revision"],
            "segments": updated, "checkpoint": "draft",
        })
        assert saved.status_code == 200
        assert client.get(f"/jobs/{job_id}/source-audio-url", headers=headers).status_code == 200
        assert client.get(f"/jobs/{job_id}/waveform", headers=headers).status_code == 200
        claimed = client.post(f"/batch/campaigns/{campaign_id}/next", headers={
            **headers, "x-editor-session": "review-session-1234",
        })
        assert claimed.status_code == 200
        assert claimed.json()["job_id"] == job_id

        assert client.get(f"/status/{other_job_id}", headers=headers).status_code == 404
        assert client.get(f"/editor/{other_job_id}", headers=headers).status_code == 404
        assert other_job_id not in {row["job_id"] for row in client.get("/jobs", headers=headers).json()}
        assert client.get(f"/batch/campaigns/{campaign_id}", headers=outsider_headers).status_code == 404
        assert client.get(f"/status/{job_id}", headers=outsider_headers).status_code == 404
        assert client.patch(f"/batch/campaigns/{campaign_id}", headers=headers, json={
            "name": "not owner",
        }).status_code == 403
        assert client.post(f"/batch/campaigns/{campaign_id}/items/{item_id}/discard",
                           headers=headers, json={"reason": "not owner"}).status_code == 403

        with SessionLocal() as db:
            grant = db.query(BatchCampaignReviewer).filter_by(
                campaign_id=campaign_id, user_id=reviewer_id,
            ).one()
            grant.revoked_at = datetime.now(timezone.utc)
            db.commit()
        assert client.get(f"/batch/campaigns/{campaign_id}", headers=headers).status_code == 404
        assert client.get(f"/editor/{job_id}", headers=headers).status_code == 404
        assert client.get(f"/status/{job_id}", headers=headers).status_code == 404
        assert campaign_id not in {
            row["id"] for row in client.get("/batch/campaigns", headers=headers).json()["items"]
        }
    finally:
        with SessionLocal() as db:
            db.query(EditorVersion).filter(EditorVersion.job_id == job_id).delete()
            db.query(EditorDocument).filter(EditorDocument.job_id == job_id).delete()
            db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).delete()
            db.query(ProductEvent).filter(ProductEvent.job_id == job_id).delete()
            db.query(Job).filter(Job.job_id.in_([job_id, other_job_id])).delete(synchronize_session=False)
            db.query(BatchCampaignReviewer).filter_by(campaign_id=campaign_id).delete()
            db.query(BatchCampaignItem).filter_by(campaign_id=campaign_id).delete()
            db.query(BatchCampaign).filter_by(id=campaign_id).delete()
            db.commit()
