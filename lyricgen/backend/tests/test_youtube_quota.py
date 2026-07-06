"""YouTube API daily-quota accounting + deferral of publishes."""

import pytest

from tests.conftest import auth
from tests.test_youtube_publish import (
    _register, _seed_done_job, _connect_channel, _patch_enqueue,
    _fake_upload_ok, METADATA,
)


@pytest.fixture(autouse=True)
def _fresh_quota_day():
    """Each test starts with a clean counter for today."""
    from database import SessionLocal, YouTubeApiQuota

    s = SessionLocal()
    try:
        s.query(YouTubeApiQuota).delete()
        s.commit()
    finally:
        s.close()
    yield


def test_record_usage_accumulates():
    import youtube_quota

    assert youtube_quota.record_usage("videos.list") == 1
    assert youtube_quota.record_usage("videos.insert") == 1601
    usage = youtube_quota.get_usage()
    assert usage["units_used"] == 1601
    assert usage["limit"] == youtube_quota.DAILY_LIMIT


def test_check_and_reserve_refuses_at_limit(monkeypatch):
    import youtube_quota

    monkeypatch.setattr(youtube_quota, "DAILY_LIMIT", 2000)
    # First reservation fits (1600 + 50).
    assert youtube_quota.check_and_reserve("videos.insert", extra_units=50) is True
    # Second would exceed 2000.
    assert youtube_quota.check_and_reserve("videos.insert", extra_units=50) is False


def test_alert_fires_exactly_once(monkeypatch):
    import youtube_quota
    import emails

    sent = []
    monkeypatch.setattr(emails, "send_youtube_quota_alert", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(youtube_quota, "DAILY_LIMIT", 100)

    youtube_quota.record_usage("videos.list", count=79)   # 79% — below
    assert sent == []
    youtube_quota.record_usage("videos.list", count=2)    # 81% — crosses
    assert len(sent) == 1
    youtube_quota.record_usage("videos.list", count=5)    # still high — no re-alert
    assert len(sent) == 1


def test_worker_defers_publish_when_quota_exhausted(client, monkeypatch):
    import youtube_quota
    from database import SessionLocal, PublishJob
    from youtube_publish import publish_to_youtube_task

    monkeypatch.setattr(youtube_quota, "DAILY_LIMIT", 100)  # nothing fits
    _fake_upload_ok(monkeypatch)
    import emails
    monkeypatch.setattr(emails, "send_video_published", lambda *a, **k: None)

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)
    _patch_enqueue(monkeypatch)
    pk = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                     json={"metadata": METADATA, "include_short": False}).json()[0]["id"]

    result = publish_to_youtube_task(pk)
    assert result == {"deferred": True, "reason": "youtube_quota_exhausted"}

    s = SessionLocal()
    try:
        row = s.query(PublishJob).filter(PublishJob.id == pk).one()
        # Parked as scheduled for the next Pacific reset → the scheduler
        # daemon re-enqueues it; nothing was uploaded.
        assert row.status == "scheduled"
        assert row.blocked_reason == "youtube_quota_exhausted"
        assert row.scheduled_at is not None
    finally:
        s.close()


def test_admin_stats_include_quota(client):
    res = client.post("/auth/login", json={"username": "admin", "password": "testadmin123"})
    admin = res.json()["token"]
    stats = client.get("/admin/stats", headers=auth(admin)).json()
    assert "youtube_quota" in stats
    assert stats["youtube_quota"]["limit"] > 0


def test_content_id_dormant_by_default(client, monkeypatch):
    import content_id

    assert content_id.is_configured() is False
    result = content_id.check_claim_status(metadata=METADATA)
    assert result["status"] == "unknown"
    assert result["provider"] == "not_configured"

    # Configured stub lights up the provider name.
    monkeypatch.setenv("YOUTUBE_PARTNER_ENABLED", "1")
    monkeypatch.setenv("YOUTUBE_PARTNER_CONTENT_OWNER_ID", "CMS123")
    assert content_id.is_configured() is True
    result = content_id.check_claim_status(metadata=METADATA)
    assert result["provider"] == "youtube_partner"
