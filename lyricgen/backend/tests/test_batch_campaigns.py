import hashlib
import uuid

import pytest
from fastapi import HTTPException

import batch_campaigns as batch
from database import (
    BatchCampaign, BatchCampaignItem, BatchUploadSession, EditorDocument, Job,
    JobOutboxEvent, SessionLocal, User,
)
from editor import acquire_lock, release_lock


@pytest.fixture(autouse=True)
def clean_batch_campaign_rows():
    """Campaign tests commit deliberately; keep their outbox rows isolated.

    The full PostgreSQL suite shares one database for speed. Leaving the 50
    feeder events from the window tests behind can fill the next outbox
    reconciliation page and make an unrelated stale-consumer test wait for a
    second cycle.
    """
    def clean():
        session = SessionLocal()
        try:
            job_ids = [row[0] for row in session.query(Job.job_id).filter(
                Job.workload_class == "batch",
            ).all()]
            if job_ids:
                session.query(EditorDocument).filter(
                    EditorDocument.job_id.in_(job_ids),
                ).delete(synchronize_session=False)
                session.query(JobOutboxEvent).filter(
                    JobOutboxEvent.job_id.in_(job_ids),
                ).delete(synchronize_session=False)
            session.query(Job).filter(
                Job.workload_class == "batch",
            ).delete(synchronize_session=False)
            session.query(BatchUploadSession).delete(synchronize_session=False)
            session.query(BatchCampaignItem).delete(synchronize_session=False)
            session.query(BatchCampaign).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    clean()
    yield
    clean()


def _campaign(db, count=60):
    user = db.query(User).first()
    tenant = f"batch-test-{uuid.uuid4().hex[:8]}"
    campaign = BatchCampaign(
        id=uuid.uuid4().hex[:12], tenant_id=tenant, created_by=user.id,
        name="Campaña Unicode ñ", status="active", expected_count=count,
        default_render_params={"background_mode": "ai"},
    )
    db.add(campaign)
    db.flush()
    for ordinal in range(1, count + 1):
        db.add(BatchCampaignItem(
            id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=tenant,
            ordinal=ordinal, filename=f"Canción_{ordinal}_Artista_ARF{ordinal}.wav",
            title=f"Canción {ordinal}", artist="Artista",
            technical_code=f"ARF{ordinal}", size_bytes=1024,
            duration_seconds=180, sha256=hashlib.sha256(str(ordinal).encode()).hexdigest(),
            upload_state="uploaded", upload_key=f"campaign/{ordinal}.wav",
            render_overrides={},
        ))
    db.commit()
    return campaign


def test_reconciler_respects_30_active_and_50_ready_windows(db):
    campaign = _campaign(db, 60)
    first = batch._promote_campaign(db, campaign)
    assert len(first) == 30
    assert db.query(Job).filter(
        Job.campaign_id == campaign.id,
        Job.status == "transcribing_queued",
    ).count() == 30
    assert batch._promote_campaign(db, campaign) == []

    jobs = db.query(Job).filter(Job.campaign_id == campaign.id).all()
    for job in jobs:
        job.status = "transcribed_pending"
    db.commit()
    second = batch._promote_campaign(db, campaign)
    assert len(second) == 20
    assert db.query(Job).filter(Job.campaign_id == campaign.id).count() == 50
    assert all(job.workload_class == "batch" for job in db.query(Job).filter(
        Job.campaign_id == campaign.id,
    ))


def test_reconciler_reserves_ready_buffer_for_active_transcriptions(db):
    campaign = _campaign(db, 80)
    batch._promote_campaign(db, campaign)
    jobs = db.query(Job).filter(Job.campaign_id == campaign.id).all()
    for job in jobs[:25]:
        job.status = "transcribed_pending"
    db.commit()

    # 25 ready + 5 still active already reserve 30 of the 50 ready slots,
    # while the 30-active window has room for another 25.
    promoted = batch._promote_campaign(db, campaign)
    assert len(promoted) == 20
    assert db.query(Job).filter(Job.campaign_id == campaign.id).count() == 50


def test_render_capacity_is_separate_and_bounded(db):
    campaign = _campaign(db, 11)
    items = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.campaign_id == campaign.id,
    ).order_by(BatchCampaignItem.ordinal).all()
    user = db.query(User).first()
    for index, item in enumerate(items):
        db.add(Job(
            job_id=uuid.uuid4().hex[:12], user_id=user.id,
            tenant_id=campaign.tenant_id, artist=item.artist,
            song_title=item.title, filename=item.filename,
            status="transcribed_pending" if index == 0 else "queued",
            workload_class="batch", campaign_id=campaign.id,
            campaign_item_id=item.id,
        ))
    db.commit()
    candidate = db.query(Job).filter(
        Job.campaign_id == campaign.id,
        Job.status == "transcribed_pending",
    ).one()
    with pytest.raises(HTTPException) as exc:
        batch.enforce_render_capacity(db, candidate)
    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "batch_render_window_full"


def test_paused_campaign_cannot_start_a_new_render(db):
    campaign = _campaign(db, 1)
    campaign.status = "paused"
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.campaign_id == campaign.id,
    ).one()
    user = db.query(User).first()
    job = Job(
        job_id=uuid.uuid4().hex[:12], user_id=user.id,
        tenant_id=campaign.tenant_id, artist=item.artist,
        song_title=item.title, filename=item.filename,
        status="transcribed_pending", workload_class="batch",
        campaign_id=campaign.id, campaign_item_id=item.id,
    )
    db.add(job)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        batch.enforce_render_capacity(db, job)
    assert exc.value.status_code == 409


def test_same_account_different_tabs_cannot_share_editor_lock(db):
    campaign = _campaign(db, 1)
    user = db.query(User).first()
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.campaign_id == campaign.id,
    ).one()
    job = Job(
        job_id=uuid.uuid4().hex[:12], user_id=user.id,
        tenant_id=campaign.tenant_id, artist=item.artist,
        song_title=item.title, filename=item.filename,
        status="transcribed_pending", workload_class="batch",
        campaign_id=campaign.id, campaign_item_id=item.id,
        segments_json=[{"segment_id": "one", "start": 0, "end": 1, "text": "Hola"}],
    )
    db.add(job)
    db.add(EditorDocument(
        job_id=job.job_id, tenant_id=campaign.tenant_id,
        current_segments=job.segments_json, original_segments=job.segments_json,
        revision=0,
    ))
    db.commit()
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).one()
    assert acquire_lock(db, document, user.id, session_id="tab_session_one")["acquired"] is True
    assert acquire_lock(db, document, user.id, session_id="tab_session_two")["acquired"] is False
    assert release_lock(db, document, user.id, session_id="tab_session_two") is False
    assert release_lock(db, document, user.id, session_id="tab_session_one") is True


def test_pairing_code_registers_unicode_manifest_without_account_token(
    client, admin_token, monkeypatch,
):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    auth = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/batch/campaigns", headers=auth, json={
        "name": "Catálogo música 2026", "expected_count": 2,
        "default_render_params": {"background_mode": "ai"},
    })
    assert created.status_code == 200, created.text
    campaign_id = created.json()["id"]
    pairing = client.post(
        f"/batch/campaigns/{campaign_id}/upload-session", headers=auth,
    )
    assert pairing.status_code == 200
    exchange = client.post("/batch/upload-sessions/exchange", json={
        "campaign_id": campaign_id,
        "code": pairing.json()["pairing_code"],
    })
    assert exchange.status_code == 200
    token = exchange.json()["upload_token"]
    manifest = client.post(
        f"/batch/campaigns/{campaign_id}/manifest",
        headers={"X-Batch-Upload-Token": token},
        json={"items": [
            {
                "client_id": "one", "filename": "Canción_Única_ARF123.wav",
                "title": "Canción", "artist": "Única", "technical_code": "ARF123",
                "size_bytes": 1234, "duration_seconds": 123.4,
                "sha256": "a" * 64,
            },
            {
                "client_id": "two", "filename": "sin metadata.mp3",
                "size_bytes": 4567, "sha256": "b" * 64,
                "metadata_error": "missing_metadata",
            },
        ]},
    )
    assert manifest.status_code == 200, manifest.text
    listed = client.get(
        f"/batch/campaigns/{campaign_id}/items", headers=auth,
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    missing = next(item for item in listed.json()["items"] if item["filename"] == "sin metadata.mp3")
    assert missing["metadata_error"] == "missing_metadata"

    same_hash = client.post(
        f"/batch/campaigns/{campaign_id}/manifest",
        headers={"X-Batch-Upload-Token": token},
        json={"items": [{
            "client_id": "same-hash", "filename": "copia.wav",
            "title": "Copia", "artist": "Otra", "technical_code": "ARF999",
            "size_bytes": 1234, "duration_seconds": 123.4,
            "sha256": "a" * 64,
        }]},
    )
    assert same_hash.json()["items"][0]["duplicate_reason"] == "sha256"
    same_code = client.post(
        f"/batch/campaigns/{campaign_id}/manifest",
        headers={"X-Batch-Upload-Token": token},
        json={"items": [{
            "client_id": "same-code", "filename": "otro.wav",
            "title": "Otro", "artist": "Otra", "technical_code": "arf123",
            "size_bytes": 9999, "duration_seconds": 140,
            "sha256": "c" * 64,
        }]},
    )
    assert same_code.json()["items"][0]["duplicate_reason"] == "technical_code"


def test_manifest_registers_600_items_in_chunks(client, admin_token, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    auth = {"Authorization": f"Bearer {admin_token}"}
    campaign = client.post("/batch/campaigns", headers=auth, json={
        "name": "Carga 600", "expected_count": 600,
    }).json()
    pairing = client.post(
        f"/batch/campaigns/{campaign['id']}/upload-session", headers=auth,
    ).json()
    token = client.post("/batch/upload-sessions/exchange", json={
        "campaign_id": campaign["id"], "code": pairing["pairing_code"],
    }).json()["upload_token"]
    for start in range(0, 600, 100):
        rows = []
        for index in range(start, start + 100):
            rows.append({
                "client_id": str(index),
                "filename": f"Canción_{index}_Artista_ARF{10000 + index}.wav",
                "title": f"Canción {index}", "artist": "Artista",
                "technical_code": f"ARF{10000 + index}",
                "size_bytes": 1024 + index, "duration_seconds": 180,
                "sha256": hashlib.sha256(f"audio-{index}".encode()).hexdigest(),
            })
        response = client.post(
            f"/batch/campaigns/{campaign['id']}/manifest",
            headers={"X-Batch-Upload-Token": token}, json={"items": rows},
        )
        assert response.status_code == 200, response.text
    summary = client.get(
        f"/batch/campaigns/{campaign['id']}", headers=auth,
    ).json()
    assert summary["registered_count"] == 600
    assert summary["counters"]["waiting_upload"] == 600


def test_two_tabs_claim_different_ready_jobs(client, admin_token, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    auth = {"Authorization": f"Bearer {admin_token}"}
    campaign_id = client.post("/batch/campaigns", headers=auth, json={
        "name": "Dos pestañas", "expected_count": 2,
    }).json()["id"]
    session = SessionLocal()
    try:
        campaign = session.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).one()
        for ordinal in (1, 2):
            item = BatchCampaignItem(
                id=str(uuid.uuid4()), campaign_id=campaign.id,
                tenant_id=campaign.tenant_id, ordinal=ordinal,
                filename=f"Tema_{ordinal}_Artista_ARF{200 + ordinal}.wav",
                title=f"Tema {ordinal}", artist="Artista",
                technical_code=f"ARF{200 + ordinal}", size_bytes=1234,
                duration_seconds=120,
                sha256=hashlib.sha256(f"tab-{ordinal}".encode()).hexdigest(),
                upload_state="uploaded", upload_key=f"tab/{ordinal}.wav",
                render_overrides={},
            )
            session.add(item)
            session.flush()
            session.add(Job(
                job_id=uuid.uuid4().hex[:12], user_id=campaign.created_by,
                tenant_id=campaign.tenant_id, artist=item.artist,
                song_title=item.title, filename=item.filename,
                status="transcribed_pending", current_step="editing", progress=100,
                workload_class="batch", campaign_id=campaign.id,
                campaign_item_id=item.id,
                segments_json=[{
                    "segment_id": f"line-{ordinal}", "start": 0,
                    "end": 1, "text": f"línea {ordinal}",
                }],
            ))
        session.commit()
    finally:
        session.close()
    first = client.post(
        f"/batch/campaigns/{campaign_id}/next",
        headers={**auth, "X-Editor-Session": "tab_session_alpha"},
    )
    second = client.post(
        f"/batch/campaigns/{campaign_id}/next",
        headers={**auth, "X-Editor-Session": "tab_session_bravo"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["job_id"]
    assert second.json()["job_id"]
    assert first.json()["job_id"] != second.json()["job_id"]
