"""YouTube analytics sync + endpoints.

The Google fetchers (fetch_analytics_rows / fetch_data_api_stats) are the
monkeypatch seams — the Google client is never built.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from tests.conftest import auth
from tests.test_youtube_publish import (
    _register, _seed_done_job, _connect_channel, _patch_enqueue,
    _fake_upload_ok, METADATA,
)

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


@pytest.fixture(autouse=True)
def _reset_quota():
    """The daily YouTube-quota counter is committed shared state; other
    suites' publishes accumulate into it and would exhaust the budget,
    deferring these tests' publishes. Reset it per test."""
    from database import SessionLocal, YouTubeApiQuota

    s = SessionLocal()
    try:
        s.query(YouTubeApiQuota).delete()
        s.commit()
    finally:
        s.close()
    yield


def _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidA"):
    """Create a done job, publish it (video only), return job_id."""
    from youtube_publish import publish_to_youtube_task

    _connect_channel(user_id, tenant_id, channel_id=f"UC-{uuid.uuid4().hex[:6]}")
    _set_channel_scope(tenant_id, [ANALYTICS_SCOPE])
    job_id = _seed_done_job(user_id, tenant_id)
    _fake_upload_ok(monkeypatch, video_id=video_id)
    import emails
    monkeypatch.setattr(emails, "send_video_published", lambda *a, **k: None)
    _patch_enqueue(monkeypatch)
    pk = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                     json={"metadata": METADATA, "include_short": False}).json()[0]["id"]
    publish_to_youtube_task(pk)
    return job_id


def _set_channel_scope(tenant_id, scopes):
    from database import SessionLocal, YouTubeChannel

    s = SessionLocal()
    try:
        s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id).update(
            {YouTubeChannel.scopes: scopes}, synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()


def _days_ago(n):
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


# ─── Sync ────────────────────────────────────────────────────────────

def test_analytics_sync_upserts_and_is_idempotent(client, monkeypatch):
    import youtube_analytics as yta
    from database import SessionLocal, VideoStats

    token, user_id, tenant_id = _register(client)
    _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidA")

    rows = [
        {"video_id": "vidA", "stat_date": _days_ago(2), "views": 100, "likes": 10, "comments": 3, "estimated_minutes_watched": 40},
        {"video_id": "vidA", "stat_date": _days_ago(1), "views": 150, "likes": 12, "comments": 4, "estimated_minutes_watched": 55},
    ]
    monkeypatch.setattr(yta, "fetch_analytics_rows", lambda *a, **k: rows)

    s = SessionLocal()
    try:
        r1 = yta.sync_tenant(s, tenant_id)
        assert r1["source"] == "analytics"
        assert r1["rows"] == 2
        # Re-sync same rows → no duplicate (unique constraint on video+date).
        yta.sync_tenant(s, tenant_id)
        count = s.query(VideoStats).filter(VideoStats.video_id == "vidA").count()
        assert count == 2
    finally:
        s.close()


def test_data_api_fallback_when_no_analytics_scope(client, monkeypatch):
    import youtube_analytics as yta
    from database import SessionLocal, VideoStats

    token, user_id, tenant_id = _register(client)
    _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidB")
    _set_channel_scope(tenant_id, ["https://www.googleapis.com/auth/youtube.readonly"])

    snap = [{"video_id": "vidB", "stat_date": _days_ago(0), "views": 500, "likes": 40, "comments": 8, "estimated_minutes_watched": None}]
    monkeypatch.setattr(yta, "fetch_data_api_stats", lambda *a, **k: snap)

    def _boom(*a, **k):
        raise AssertionError("analytics API must not be called without the scope")
    monkeypatch.setattr(yta, "fetch_analytics_rows", _boom)

    s = SessionLocal()
    try:
        r = yta.sync_tenant(s, tenant_id)
        assert r["source"] == "data_api"
        row = s.query(VideoStats).filter(VideoStats.video_id == "vidB").one()
        assert row.source == "data_api"
        assert row.estimated_minutes_watched is None
    finally:
        s.close()


def test_zero_rows_never_overwrite_nonzero(client, monkeypatch):
    import youtube_analytics as yta
    from database import SessionLocal, VideoStats

    token, user_id, tenant_id = _register(client)
    _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidC")
    date = _days_ago(1)

    monkeypatch.setattr(yta, "fetch_analytics_rows",
                        lambda *a, **k: [{"video_id": "vidC", "stat_date": date, "views": 300, "likes": 5, "comments": 1, "estimated_minutes_watched": 20}])
    s = SessionLocal()
    try:
        yta.sync_tenant(s, tenant_id)
        # Later pass returns a not-yet-settled zero row for the same day.
        monkeypatch.setattr(yta, "fetch_analytics_rows",
                            lambda *a, **k: [{"video_id": "vidC", "stat_date": date, "views": 0, "likes": 0, "comments": 0, "estimated_minutes_watched": 0}])
        yta.sync_tenant(s, tenant_id)
        row = s.query(VideoStats).filter(VideoStats.video_id == "vidC", VideoStats.stat_date == date).one()
        assert row.views == 300  # preserved
    finally:
        s.close()


def test_retention_prune(client, monkeypatch):
    import youtube_analytics as yta
    from database import SessionLocal, VideoStats

    token, user_id, tenant_id = _register(client)
    _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidD")

    s = SessionLocal()
    try:
        s.add(VideoStats(video_id="vidD", tenant_id=tenant_id, stat_date=_days_ago(500),
                         views=1, likes=0, comments=0, source="analytics"))
        s.commit()
    finally:
        s.close()

    monkeypatch.setattr(yta, "fetch_analytics_rows", lambda *a, **k: [])
    result = yta.sync_all()
    assert result.get("pruned", 0) >= 1


# ─── Endpoints ───────────────────────────────────────────────────────

def test_analytics_endpoint_totals_and_series(client, monkeypatch):
    import youtube_analytics as yta
    from database import SessionLocal

    token, user_id, tenant_id = _register(client)
    job_id = _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidE")

    rows = [
        {"video_id": "vidE", "stat_date": _days_ago(2), "views": 100, "likes": 10, "comments": 2, "estimated_minutes_watched": 30},
        {"video_id": "vidE", "stat_date": _days_ago(1), "views": 150, "likes": 12, "comments": 3, "estimated_minutes_watched": 45},
    ]
    monkeypatch.setattr(yta, "fetch_analytics_rows", lambda *a, **k: rows)
    s = SessionLocal()
    try:
        yta.sync_tenant(s, tenant_id)
    finally:
        s.close()

    res = client.get(f"/youtube/analytics/{job_id}", headers=auth(token))
    assert res.status_code == 200, res.text
    data = res.json()
    assert "video" in data
    # analytics source → SUM of daily rows.
    assert data["video"]["totals"]["views"] == 250
    assert len(data["video"]["series"]) == 2


def test_analytics_cross_tenant_is_404(client, monkeypatch):
    token_a, user_a, tenant_a = _register(client)
    token_b, _, _ = _register(client)
    job_id = _publish_video(client, monkeypatch, token_a, user_a, tenant_a, video_id="vidF")

    assert client.get(f"/youtube/analytics/{job_id}", headers=auth(token_b)).status_code == 404


def test_summary_aggregates_tenant(client, monkeypatch):
    import youtube_analytics as yta
    from database import SessionLocal

    token, user_id, tenant_id = _register(client)
    _publish_video(client, monkeypatch, token, user_id, tenant_id, video_id="vidG")

    monkeypatch.setattr(yta, "fetch_analytics_rows",
                        lambda *a, **k: [{"video_id": "vidG", "stat_date": _days_ago(1), "views": 999, "likes": 50, "comments": 9, "estimated_minutes_watched": 120}])
    s = SessionLocal()
    try:
        yta.sync_tenant(s, tenant_id)
    finally:
        s.close()

    summary = client.get("/youtube/analytics/summary?days=28", headers=auth(token)).json()
    assert summary["videos_tracked"] == 1
    assert summary["totals"]["views"] == 999
    assert summary["top_videos"][0]["video_id"] == "vidG"


def test_admin_sync_now(client):
    res = client.post("/auth/login", json={"username": "admin", "password": "testadmin123"})
    admin = res.json()["token"]
    out = client.post("/admin/analytics/sync-now", headers=auth(admin))
    assert out.status_code == 200
    assert "tenants" in out.json() or "skipped" in out.json()
