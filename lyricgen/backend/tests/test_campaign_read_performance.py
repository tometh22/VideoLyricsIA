"""Read-path parity and query-shape regressions for growing campaign catalogs."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import batch_campaigns as batch
from campaign_review_history import review_activity, saved_review_history
from database import (
    Base, User, BatchCampaign, BatchCampaignItem, Job, EditorDocument,
    EditorVersion, ProductEvent, AuditLog,
)


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "0")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        User, BatchCampaign, BatchCampaignItem, Job, EditorDocument,
        EditorVersion, ProductEvent, AuditLog,
    )])
    with Session(engine) as db:
        user = User(username="reader", email="reader@example.com", hashed_password="unused",
                    tenant_id="read-tenant", role="admin")
        db.add(user); db.flush()
        campaign = BatchCampaign(id=uuid.uuid4().hex[:12], tenant_id=user.tenant_id,
                                 created_by=user.id, name="Read parity", default_render_params={})
        db.add(campaign); db.flush()
        yield db, user, campaign
    engine.dispose()


def add_item(db, user, campaign, ordinal, *, status=None, upload="uploaded", error=None, **kw):
    item = BatchCampaignItem(
        id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
        ordinal=ordinal, filename=f"song-{ordinal}.wav", sha256=f"{ordinal:064x}",
        title=f"Canción {ordinal}", artist="Artista", upload_state=upload,
        metadata_error=error, render_overrides={},
    )
    db.add(item); db.flush()
    job = None
    if status:
        job = Job(job_id=uuid.uuid4().hex[:12], user_id=user.id, tenant_id=campaign.tenant_id,
                  campaign_id=campaign.id, campaign_item_id=item.id, status=status,
                  artist="Artista", filename=item.filename, song_title=item.title, **kw)
        db.add(job); db.flush()
    return item, job


def capture(db):
    statements = []
    def record(_conn, _cursor, sql, _params, _context, _many):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)
    event.listen(db.bind, "before_cursor_execute", record)
    return statements, record


def test_bulk_summary_preserves_phase_precedence_without_loading_jobs(catalog):
    db, user, campaign = catalog
    cases = [
        (None, "registered", None, "waiting_upload"),
        (None, "uploading", None, "uploading"),
        (None, "uploaded", None, "waiting_processing"),
        (None, "error", None, "failed"),
        (None, "uploaded", "invalid_duration", "failed"),
        ("transcribed", "error", None, "lyrics_ready"),
        ("discarded", "error", None, "discarded"),
        ("separating", "uploaded", None, "separating"),
        ("separation_ready", "uploaded", None, "separation_ready"),
        ("transcribing_queued", "uploaded", None, "transcribing"),
        ("lyrics_approved", "uploaded", None, "lyrics_approved"),
        ("processing", "uploaded", None, "rendering"),
        ("pending_review", "uploaded", None, "final_review"),
        ("done", "error", "promotion_failed", "done"),
        ("rejected", "uploaded", None, "failed"),
    ]
    for ordinal, (status, upload, error, _) in enumerate(cases):
        add_item(db, user, campaign, ordinal, status=status, upload=upload, error=error,
                 segments_json=[{"text": "heavy" * 1000}], transcription_quality={"trace": "x" * 10000})
    empty = BatchCampaign(id=uuid.uuid4().hex[:12], tenant_id=user.tenant_id,
                          created_by=user.id, name="Empty", default_render_params={})
    db.add(empty); db.flush()
    statements, listener = capture(db)
    summaries = batch._summaries(db, [campaign, empty])
    event.remove(db.bind, "before_cursor_execute", listener)
    assert summaries[campaign.id]["registered_count"] == len(cases)
    expected = Counter(case[3] for case in cases)
    assert summaries[campaign.id]["counters"] == {key: expected[key] for key in summaries[campaign.id]["counters"]}
    assert summaries[empty.id]["registered_count"] == 0
    assert not any(summaries[empty.id]["counters"].values())
    assert len(statements) == 1
    assert "GROUP BY" in statements[0]
    assert all(name not in statements[0] for name in ("segments_json", "transcription_quality", "render_params"))


def test_bulk_summary_keeps_source_bound_reviewer_receipts(catalog, monkeypatch):
    db, user, campaign = catalog
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_CAMPAIGN_ID", campaign.id)
    _, job = add_item(db, user, campaign, 1, status="transcribed", input_audio_sha256="a" * 64,
                      audio_revision=1, transcription_quality={})
    doc = EditorDocument(job_id=job.job_id, tenant_id=campaign.tenant_id, revision=4,
                         current_segments=[{"text": "hello"}], original_segments=[])
    db.add(doc); db.flush()
    # Display can be enabled before receipts have been published.
    pending = batch._summary(db, campaign)["reviewer_campaign_status"]
    assert pending["counters"]["pending"] == 1
    from reviewer_campaign_product import live_source, KEY, campaign_payload
    projected_pending, _ = campaign_payload(
        db, campaign.id, batch._review_queue_rows(db, campaign.id), {job.job_id: doc},
    )
    assert projected_pending["counters"]["pending"] == 1
    job.transcription_quality = {KEY: {"campaign_id": campaign.id, "source": live_source(job, doc),
        "status": "complete", "candidate_available": True, "changes_count": 2, "doubts_count": 0}}
    db.flush()
    assert batch._summary(db, campaign)["reviewer_campaign_status"]["candidate_count"] == 1
    projected_fresh, _ = campaign_payload(
        db, campaign.id, batch._review_queue_rows(db, campaign.id), {job.job_id: doc},
    )
    assert projected_fresh["candidate_count"] == 1
    doc.revision += 1; db.flush()
    stale = batch._summary(db, campaign)["reviewer_campaign_status"]
    assert stale["candidate_count"] == 0
    assert stale["counters"]["stale"] == 1


@pytest.mark.parametrize("stage,order,scope,filters", [
    ("lyrics", "calendar", "all", {}),
    ("lyrics", "effort", "pending", {}),
    ("lyrics", "learning", "all", {}),
    ("lyrics", "delivery", "approved", {}),
    ("lyrics", "calendar", "drafts", {"reviewed_by": "me"}),
    ("lyrics", "calendar", "all", {"search": "cancion 1", "artist": "artista"}),
    ("final", "calendar", "all", {}),
    ("final", "effort", "pending", {"background_mode": "generated"}),
])
def test_queue_projection_matches_full_jobs_and_reads_heartbeats_once(catalog, monkeypatch, stage, order, scope, filters):
    db, user, campaign = catalog
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(batch, "_now", lambda: now)
    jobs = []
    for i, status in enumerate(("transcribed", "lyrics_approved", "pending_review", "done"), 1):
        _, job = add_item(db, user, campaign, i, status=status,
            segments_json=[{"text": "hello"}, {"text": "\u2003"}, {}, "legacy"],
            transcription_quality={"analysis_status": "complete", "unsafe_windows": [
                {"start": 1, "end": 2, "reasons": ["timing"]}] if i == 1 else [],
                "manual_full_review_required": i == 2, "unused_trace": "x" * 10000},
            s3_keys={"video": "key"} if i == 4 else None,
            approved_by=user.id if i == 4 else None,
            approved_at=now if i == 4 else None, render_params={"unused": "x" * 10000})
        jobs.append(job)
        db.add(EditorDocument(job_id=job.job_id, tenant_id=campaign.tenant_id,
            revision=1, current_segments=job.segments_json, original_segments=[],
            lock_user_id=user.id if i == 1 else None, lock_expires_at=now + timedelta(hours=1),
            updated_by=user.id, updated_at=now))
        db.add(ProductEvent(job_id=job.job_id, user_id=user.id, tenant_id=campaign.tenant_id,
            name="editor_activity_heartbeat", properties={"revision": 1, "session_id": "tab"},
            occurred_at=now, created_at=now))
    add_item(db, user, campaign, 5)
    db.add(AuditLog(user_id=user.id, action="editor.review_saved", created_at=now,
                   detail={"job_id": jobs[0].job_id, "checkpoint": "draft", "to_revision": 1}))
    db.flush()
    args = dict(stage=stage, order=order, scope=scope, state=None, version=None,
        background_mode=None, artist=None, search=None, reviewed_by=None,
        audit_preapproved=False, page=1, limit=2,
        current_user={"id": user.id, "tenant_id": user.tenant_id, "role": "admin"}, db=db)
    args.update(filters)
    # Both paths must deserialize timestamps from the database. SQLite drops
    # tzinfo on round-trip, unlike freshly inserted in-memory model values.
    campaign_id = campaign.id
    db.expunge_all()
    with monkeypatch.context() as patch:
        patch.setattr(batch, "_review_queue_rows", batch._campaign_rows)
        expected = batch.review_queue(campaign_id, **args)
    db.expunge_all()
    statements, listener = capture(db)
    actual = batch.review_queue(campaign_id, **args)
    event.remove(db.bind, "before_cursor_execute", listener)
    assert actual == expected
    heartbeat_reads = [sql for sql in statements if "FROM product_events" in sql]
    assert len(heartbeat_reads) == 1
    document_reads = [sql for sql in statements if "FROM editor_documents" in sql]
    assert all("current_segments" not in sql for sql in document_reads)
    assert all("jobs.render_params" not in sql for sql in statements)
    job_reads = [sql for sql in statements if "JOIN jobs" in sql]
    assert len(job_reads) == 1
    assert "transcription_quality AS" not in job_reads[0]


def test_activity_reuse_preserves_gaps_days_sessions_and_revision_witness(catalog, monkeypatch):
    db, user, campaign = catalog
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(batch, "_now", lambda: now)
    _, job = add_item(db, user, campaign, 1, status="transcribed")
    yesterday = now.replace(hour=0) - timedelta(seconds=10)
    stamps = [yesterday, now, now + timedelta(seconds=10), now + timedelta(seconds=60)]
    for i, at in enumerate(stamps):
        db.add(ProductEvent(job_id=job.job_id, user_id=user.id, tenant_id=campaign.tenant_id,
            name="editor_activity_heartbeat", created_at=at, occurred_at=at,
            properties={"revision": 3, "session_id": "" if i == 3 else "tab"}))
    db.add(AuditLog(user_id=user.id, action="editor.review_saved", created_at=now,
                   detail={"job_id": job.job_id, "checkpoint": "draft", "from_revision": 2, "to_revision": 3}))
    db.flush()
    events = review_activity(db, [job.job_id])
    statements, listener = capture(db)
    minutes = batch._review_minutes_by_job(db, [job.job_id], activity_events=events)
    today = batch._review_minutes_today(db, [job.job_id, "absent"], activity_events=events)
    history = saved_review_history(db, [job.job_id], activity_events=events)
    event.remove(db.bind, "before_cursor_execute", listener)
    assert minutes[job.job_id] == round(10 / 60, 2)
    assert today["total"] == round(10 / 60, 2)
    assert today["songs_with_telemetry"] == 1 and today["songs_without_telemetry"] == 1
    assert today["sessions_with_telemetry"] == 2
    assert history[job.job_id]["user_id"] == user.id
    assert not any("FROM product_events" in sql for sql in statements)
