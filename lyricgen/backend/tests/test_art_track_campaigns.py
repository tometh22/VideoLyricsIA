"""Contract tests for the parallel audio+cover art-track campaign flow."""

import hashlib
import uuid

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
