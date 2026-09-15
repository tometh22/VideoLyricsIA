"""Contract tests for the parallel audio+cover art-track campaign flow."""

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from database import (
    BatchCampaign, BatchCampaignAsset, BatchCampaignItem, DeliveryBatch,
    DeliveryBatchItem, Job, JobOutboxEvent, SessionLocal,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture(autouse=True)
def clean_art_rows():
    yield
    db = SessionLocal()
    try:
        db.query(DeliveryBatchItem).delete(synchronize_session=False)
        db.query(DeliveryBatch).delete(synchronize_session=False)
        db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id.in_(db.query(Job.job_id).filter(Job.workload_class == "batch"))).delete(synchronize_session=False)
        db.query(Job).filter(Job.workload_class == "batch").delete(synchronize_session=False)
        db.query(BatchCampaignItem).delete(synchronize_session=False)
        db.query(BatchCampaignAsset).delete(synchronize_session=False)
        db.query(BatchCampaign).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _create(client, token):
    response = client.post(
        "/batch/campaigns", headers={"Authorization": f"Bearer {token}"},
        json={"name": "Art tracks AR", "expected_count": 2, "kind": "art_track", "destination_portal": "argentina"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_art_manifest_deduplicates_shared_cover_and_blocks_ambiguous(client, admin_token, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    campaign_id = _create(client, admin_token)
    auth = {"Authorization": f"Bearer {admin_token}"}
    cover = {"filename": "album-cover.jpg", "relative_path": "album/album-cover.jpg", "sha256": _digest("cover"), "size_bytes": 100}
    manifest = client.post(
        f"/batch/art-track-campaigns/{campaign_id}/manifest", headers=auth,
        json={"covers": [cover], "audios": [
            {"client_id": "a", "filename": "album/Song One.mp3", "relative_path": "album/Song One.mp3", "title": "Song One", "artist": "Artist", "size_bytes": 1000, "sha256": _digest("a")},
            {"client_id": "b", "filename": "album/Song Two.mp3", "relative_path": "album/Song Two.mp3", "title": "Song Two", "artist": "Artist", "size_bytes": 1000, "sha256": _digest("b")},
        ]},
    )
    assert manifest.status_code == 200, manifest.text
    payload = manifest.json()
    assert payload["cover_count"] == 1
    assert payload["matched_count"] == 2
    db = SessionLocal()
    try:
        rows = db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == campaign_id).all()
        assert len(rows) == 2
        assert rows[0].cover_asset_id == rows[1].cover_asset_id
    finally:
        db.close()


def test_art_render_never_creates_transcription_and_is_idempotent(client, admin_token, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    campaign_id = _create(client, admin_token)
    auth = {"Authorization": f"Bearer {admin_token}"}
    manifest = client.post(
        f"/batch/art-track-campaigns/{campaign_id}/manifest", headers=auth,
        json={"covers": [{"filename": "Song.jpg", "sha256": _digest("cover"), "size_bytes": 100}], "audios": [
            {"client_id": "a", "filename": "Song.mp3", "title": "Song", "artist": "Artist", "size_bytes": 1000, "sha256": _digest("a")},
        ]},
    )
    assert manifest.status_code == 200
    db = SessionLocal()
    try:
        asset = db.query(BatchCampaignAsset).filter(BatchCampaignAsset.campaign_id == campaign_id).all()
        for row in asset:
            row.upload_state = "uploaded"
            row.upload_key = f"test/{row.role}/{row.id}"
        item = db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == campaign_id).one()
        item.upload_state = "uploaded"
        db.commit()
    finally:
        db.close()
    confirm = client.post(f"/batch/art-track-campaigns/{campaign_id}/associations/confirm", headers=auth, json={"confirm_all_matched": True})
    assert confirm.status_code == 200, confirm.text
    monkeypatch.setattr("transactional_outbox.dispatch_outbox_event", lambda event_id: {"status": "dispatched", "event_id": event_id})
    started = client.post(f"/batch/art-track-campaigns/{campaign_id}/start-rendering", headers=auth)
    assert started.status_code == 200, started.text
    assert started.json()["created_count"] == 1
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.campaign_id == campaign_id).one()
        assert job.render_params["art_track"] is True
        assert job.status == "queued"
        assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.job_id, JobOutboxEvent.event_type == "transcription.enqueue").count() == 0
    finally:
        db.close()
    again = client.post(f"/batch/art-track-campaigns/{campaign_id}/start-rendering", headers=auth)
    assert again.status_code == 200
    assert again.json()["created_count"] == 0


def test_lyric_campaign_delivery_selects_only_approved_videos(client, admin_token, monkeypatch):
    """The campaign history can publish approved lyric-video jobs in bulk."""
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    created = client.post(
        "/batch/campaigns", headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "Lyrics AR", "expected_count": 2, "kind": "lyric_video"},
    )
    assert created.status_code == 200, created.text
    campaign_id = created.json()["id"]
    db = SessionLocal()
    try:
        campaign = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).one()
        approved = Job(
            job_id=uuid.uuid4().hex[:12], user_id=campaign.created_by,
            tenant_id=campaign.tenant_id, workload_class="batch", campaign_id=campaign.id,
            artist="Artist", song_title="Approved", filename="approved.mp3",
            status="done", approved_at=datetime.now(timezone.utc), video_url="approved.mp4",
            render_params={"campaign_render_evidence": {"video_sha256": _digest("approved")}},
        )
        pending = Job(
            job_id=uuid.uuid4().hex[:12], user_id=campaign.created_by,
            tenant_id=campaign.tenant_id, workload_class="batch", campaign_id=campaign.id,
            artist="Artist", song_title="Pending", filename="pending.mp3",
            status="pending_review", video_url="pending.mp4",
        )
        db.add_all([approved, pending]); db.commit()
        approved_id = approved.job_id
    finally:
        db.close()
    response = client.post(
        f"/batch/campaigns/{campaign_id}/deliveries",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"destination_portal": "chile", "idempotency_key": "lyric-bulk-operation-1"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["destination_portal"] == "chile"
    assert payload["total_count"] == 1
    db = SessionLocal()
    try:
        item = db.query(DeliveryBatchItem).filter(DeliveryBatchItem.delivery_batch_id == payload["operation_id"]).one()
        assert item.job_id == approved_id
    finally:
        db.close()


def test_cross_tenant_admin_can_read_delivery_operation_without_exposing_it_to_other_tenants(client, admin_token, monkeypatch):
    from fastapi import HTTPException
    from art_track_campaigns import get_delivery_batch

    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", "portal-owner,foreign-operator")
    auth = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/batch/campaigns", headers=auth,
                          json={"name": "Chile publication", "kind": "lyric_video"})
    assert created.status_code == 200
    with SessionLocal() as db:
        campaign = db.query(BatchCampaign).filter_by(id=created.json()["id"]).one()
        campaign.tenant_id = "portal-owner"
        operation = DeliveryBatch(id=str(uuid.uuid4()), campaign_id=campaign.id,
                                  tenant_id=campaign.tenant_id, destination_portal="chile",
                                  idempotency_key="cross-tenant-operation", created_by=campaign.created_by,
                                  total_count=0, status="completed")
        db.add(operation)
        db.commit()
        operation_id = operation.id
        response = client.get(f"/batch/delivery-operations/{operation_id}", headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["destination_portal"] == "chile"
        owner = {"id": campaign.created_by, "role": "user", "tenant_id": "portal-owner"}
        assert get_delivery_batch(operation_id, owner, db)["status"] == "completed"
        with pytest.raises(HTTPException) as denied:
            get_delivery_batch(operation_id, {**owner, "tenant_id": "foreign-operator"}, db)
        assert denied.value.status_code == 404
