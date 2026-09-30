"""One song-level stage per campaign item, identical in every campaign screen."""
import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import campaign_creative as creative
import campaign_pipeline as pipeline
from database import BatchCampaign, BatchCampaignItem, Job, User
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


@pytest.fixture
def setup(db, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    user = db.query(User).first()
    campaign = BatchCampaign(id=uuid.uuid4().hex[:12], tenant_id=f"pipeline-{uuid.uuid4().hex[:6]}",
                             created_by=user.id, name="Pipeline", kind="lyric_video", status="active",
                             default_render_params={})
    db.add(campaign)
    db.flush()
    items = []
    for n in range(12):
        item = BatchCampaignItem(id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
                                 ordinal=n + 1, filename=f"song{n}.wav", title=f"Tema {n}", artist="Artista",
                                 technical_code=f"ARF{n}", size_bytes=100, upload_state="uploaded",
                                 sha256=hashlib.sha256(f"{campaign.id}-{n}".encode()).hexdigest())
        db.add(item)
        items.append(item)
    db.commit()
    return campaign, items, {"id": user.id, "role": "admin", "tenant_id": campaign.tenant_id}


def add_job(db, campaign, actor, *, item=None, parent=None, status="done", minutes=0, tenant_id=None, video=True):
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=tenant_id or campaign.tenant_id,
              campaign_id=campaign.id if item is not None else None,
              campaign_item_id=item.id if item is not None else None,
              parent_job_id=parent.job_id if parent is not None else None,
              artist="Artista", filename="a.wav", status=status, workload_class="batch",
              video_url="video" if video else None,
              created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes),
              approved_at=datetime.now(timezone.utc) if status == "done" else None)
    db.add(job)
    db.flush()
    return job


@contextmanager
def portal(rows):
    """Separate physical portal database, like production."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE deliveries (id INTEGER, job_id TEXT, portal_id TEXT, tenant_snapshot TEXT,"
            " removed_at TEXT, published_render_fingerprint TEXT, published_revision INTEGER,"
            " stale_since TEXT, stale_reason TEXT, approved_at TEXT, content_updated_at TEXT)"
        ))
        conn.execute(text("CREATE TABLE delivery_change_requests (id INTEGER, delivery_id INTEGER, resolved_at TEXT, submitted_at TEXT)"))
        for n, (job_id, portal_id, tenant, pending) in enumerate(rows, start=1):
            conn.execute(text("INSERT INTO deliveries VALUES (:n,:job,:portal,:tenant,NULL,NULL,1,NULL,NULL,NULL,NULL)"),
                         dict(n=n, job=job_id, portal=portal_id, tenant=tenant))
            for request in range(pending):
                conn.execute(text("INSERT INTO delivery_change_requests VALUES (:id,:delivery,NULL,:submitted)"),
                             dict(id=n * 100 + request, delivery=n,
                                  submitted=f"2026-09-{20 - request:02d} 10:00:00.000000"))

    @contextmanager
    def session():
        with Session(engine) as db:
            yield db

    try:
        yield session
    finally:
        engine.dispose()


def test_every_song_has_exactly_one_stage_and_counts_add_up(db, setup, monkeypatch):
    campaign, items, actor = setup
    items[0].upload_state = "registered"
    add_job(db, campaign, actor, item=items[1], status="transcribing", video=False)
    add_job(db, campaign, actor, item=items[2], status="transcribed_pending", video=False)
    add_job(db, campaign, actor, item=items[3], status="lyrics_approved", video=False)
    add_job(db, campaign, actor, item=items[4], status="rendering", video=False)
    add_job(db, campaign, actor, item=items[5], status="pending_review")
    add_job(db, campaign, actor, item=items[6], status="done")
    delivered = add_job(db, campaign, actor, item=items[7], status="done")
    add_job(db, campaign, actor, item=items[8], status="error", video=False)
    add_job(db, campaign, actor, item=items[9], status="discarded", video=False)
    add_job(db, campaign, actor, item=items[10], status="editing", video=False)
    add_job(db, campaign, actor, item=items[11], status="separating", video=False)
    db.commit()
    with portal([(delivered.job_id, "argentina", campaign.tenant_id, 0)]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        result = pipeline.campaign_pipeline(campaign.id, actor, db)
    stages = {row["title"]: row["stage"] for row in result["items"]}
    assert stages == {
        "Tema 0": "audio", "Tema 1": "audio", "Tema 2": "lyrics", "Tema 3": "ready",
        "Tema 4": "rendering", "Tema 5": "qc", "Tema 6": "approved", "Tema 7": "delivered",
        "Tema 8": "attention", "Tema 9": "discarded", "Tema 10": "rendering", "Tema 11": "audio",
    }
    assert sum(result["counts"].values()) == result["total"] == 12
    assert result["active_total"] == 11
    assert result["counts"]["delivered"] == 1 and result["counts"]["approved"] == 1
    assert result["portal_status_available"] is True
    assert result["can_manage"] is True
    by_title = {row["title"]: row for row in result["items"]}
    assert by_title["Tema 7"]["portals"] == ["argentina"]
    assert by_title["Tema 9"]["discard"] is None or isinstance(by_title["Tema 9"]["discard"], dict)


def test_variants_never_add_songs_and_the_current_version_decides_delivery(db, setup, monkeypatch):
    campaign, items, actor = setup
    root = add_job(db, campaign, actor, item=items[0], status="done", minutes=0)
    variant = add_job(db, campaign, actor, parent=root, status="done", minutes=5)
    failed = add_job(db, campaign, actor, parent=variant, status="error", minutes=9)
    other_root = add_job(db, campaign, actor, item=items[1], status="done", minutes=0)
    add_job(db, campaign, actor, parent=other_root, status="rendering", minutes=4, video=False)
    add_job(db, campaign, actor, parent=root, tenant_id="foreign-tenant", status="done", minutes=20)
    db.commit()
    with portal([(variant.job_id, "chile", campaign.tenant_id, 0),
                 (other_root.job_id, "argentina", campaign.tenant_id, 2)]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        result = pipeline.campaign_pipeline(campaign.id, actor, db)
    rows = {row["title"]: row for row in result["items"]}
    assert result["total"] == 12
    first = rows["Tema 0"]
    assert first["stage"] == "delivered"
    assert first["current_job_id"] == variant.job_id and first["current_is_variant"] is True
    assert first["job_id"] == root.job_id
    # The failed newer variant is history, not the current cut; the
    # foreign-tenant job is never visible.
    assert [version["job_id"] for version in first["versions"]] == [root.job_id, variant.job_id, failed.job_id]
    assert first["versions"][1]["portals"] == ["chile"]
    assert first["video_count"] == 3
    second = rows["Tema 1"]
    # A new version in progress moves the song back to work, keeping the
    # evidence that an older cut is already in the portal.
    assert second["stage"] == "rendering"
    assert second["published_other_version"] is True
    assert second["portals"] == ["argentina"]
    assert second["pending_change_requests"] == 2
    assert result["flags"]["change_requests"] == 1
    # Open REQUESTS (2 on one song) and the age of the oldest one.
    assert result["flags"]["change_requests_open"] == 2
    assert result["flags"]["oldest_change_request_at"].startswith("2026-09-19T10:00:00")


def test_published_song_back_in_edit_stays_in_work_with_its_change_requests(db, setup, monkeypatch):
    campaign, items, actor = setup
    job = add_job(db, campaign, actor, item=items[0], status="pending_review")
    db.commit()
    with portal([(job.job_id, "argentina", campaign.tenant_id, 1)]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        row = pipeline.campaign_pipeline(campaign.id, actor, db)["items"][0]
    assert row["stage"] == "qc"
    assert row["portals"] == ["argentina"]
    assert row["pending_change_requests"] == 1
    assert row["published_other_version"] is False


def test_unreachable_portal_is_reported_instead_of_guessing_delivery(db, setup, monkeypatch):
    campaign, items, actor = setup
    add_job(db, campaign, actor, item=items[0], status="done")
    db.commit()

    @contextmanager
    def broken():
        raise RuntimeError("portal down")
        yield  # pragma: no cover

    monkeypatch.setattr(creative, "scoped_deliveries_db", broken)
    result = pipeline.campaign_pipeline(campaign.id, actor, db)
    assert result["portal_status_available"] is False
    assert result["counts"]["approved"] == 1 and result["counts"]["delivered"] == 0


def test_other_tenants_cannot_read_a_campaign_pipeline(db, setup, monkeypatch):
    campaign, _items, actor = setup
    monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", "someone-else")
    outsider = {"id": actor["id"], "role": "user", "tenant_id": "someone-else"}
    with pytest.raises(HTTPException) as denied:
        pipeline.campaign_pipeline(campaign.id, outsider, db)
    assert denied.value.status_code == 404


def test_campaign_list_cards_carry_the_same_counts(db, setup, monkeypatch):
    from batch_campaigns import list_campaigns
    campaign, items, actor = setup
    add_job(db, campaign, actor, item=items[0], status="transcribed", video=False)
    add_job(db, campaign, actor, item=items[1], status="pending_review")
    db.commit()
    with portal([]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        listed = next(row for row in list_campaigns(actor, db)["items"] if row["id"] == campaign.id)
        detail = pipeline.campaign_pipeline(campaign.id, actor, db)
    assert listed["pipeline"]["counts"] == detail["counts"]
    assert listed["pipeline"]["counts"]["lyrics"] == 1
    assert listed["pipeline"]["counts"]["qc"] == 1
    assert listed["pipeline"]["counts"]["audio"] == 10


def test_list_counts_use_bulk_queries_and_ignore_pilot_copies(db, setup, monkeypatch):
    from batch_campaigns import list_campaigns
    from sqlalchemy import event
    campaign, items, actor = setup
    root = add_job(db, campaign, actor, item=items[0], status="done")
    pilot = add_job(db, campaign, actor, parent=root, status="transcribed_pending", minutes=5, video=False)
    pilot.pilot_id = "pilot-1"
    db.commit()
    statements = []
    engine = db.get_bind()

    def count(*_args, **_kwargs):
        statements.append(1)

    with portal([]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        event.listen(engine, "before_cursor_execute", count)
        try:
            from campaign_pipeline import pipeline_counts_bulk
            bulk = pipeline_counts_bulk(db, [campaign])
        finally:
            event.remove(engine, "before_cursor_execute", count)
        listed = next(row for row in list_campaigns(actor, db)["items"] if row["id"] == campaign.id)
        detail = pipeline.campaign_pipeline(campaign.id, actor, db)
    # items + lineage ids + lineage rows: a constant, independent of songs.
    assert len(statements) <= 4
    assert bulk[campaign.id]["counts"] == detail["counts"] == listed["pipeline"]["counts"]
    first = next(row for row in detail["items"] if row["title"] == "Tema 0")
    assert first["stage"] == "approved" and first["current_job_id"] == root.job_id
    assert first["current_sendable"] is True


def test_list_survives_a_pipeline_failure(db, setup, monkeypatch):
    from batch_campaigns import list_campaigns
    import campaign_pipeline
    campaign, _items, actor = setup

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(campaign_pipeline, "_load_lineage", broken)
    listed = next(row for row in list_campaigns(actor, db)["items"] if row["id"] == campaign.id)
    assert listed["pipeline"] is None
    assert listed["counters"]["waiting_processing"] == 12


def test_variants_outside_the_campaign_are_not_sendable(db, setup, monkeypatch):
    campaign, items, actor = setup
    root = add_job(db, campaign, actor, item=items[0], status="done")
    add_job(db, campaign, actor, parent=root, status="done", minutes=5)
    db.commit()
    with portal([]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        row = pipeline.campaign_pipeline(campaign.id, actor, db)["items"][0]
    assert row["current_is_variant"] is True
    assert row["current_sendable"] is False


def test_pipeline_reports_admin_and_inbox_flag(db, setup, monkeypatch):
    campaign, _, actor = setup
    monkeypatch.delenv("CAMPAIGN_CHANGE_REQUESTS_ENABLED", raising=False)
    with portal([]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        off = pipeline.campaign_pipeline(campaign.id, actor, db)
        monkeypatch.setenv("CAMPAIGN_CHANGE_REQUESTS_ENABLED", "1")
        monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", campaign.tenant_id)
        owner = pipeline.campaign_pipeline(campaign.id, {**actor, "role": "user"}, db)
        on = pipeline.campaign_pipeline(campaign.id, actor, db)
    assert off["features"] == {"change_requests_inbox": False, "change_request_actions": False} and off["is_admin"] is True
    assert on["features"] == {"change_requests_inbox": True, "change_request_actions": False} and owner["is_admin"] is False


def test_pipeline_reports_whether_the_portal_serves_the_latest_render(db, setup, monkeypatch):
    campaign, items, actor = setup
    job = add_job(db, campaign, actor, item=items[0], status="done")
    db.commit()
    with portal([(job.job_id, "chile", campaign.tenant_id, 0)]) as session:
        monkeypatch.setattr(creative, "scoped_deliveries_db", session)
        monkeypatch.delenv("PUBLISH_LATEST_POINTER", raising=False)
        frozen = pipeline.campaign_pipeline(campaign.id, actor, db)
        monkeypatch.setenv("PUBLISH_LATEST_POINTER", "1")
        latest = pipeline.campaign_pipeline(campaign.id, actor, db)
    assert frozen["publication_mode"] == "snapshot" and frozen["items"][0]["portal_serves_latest"] is False
    assert latest["publication_mode"] == "pointer" and latest["items"][0]["portal_serves_latest"] is True
