"""Scheduled publishing — native publishAt (public) and the scheduler
daemon (unlisted/private targets)."""

import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from tests.conftest import auth
from tests.test_youtube_publish import (
    _register, _seed_done_job, _connect_channel, _patch_enqueue, METADATA,
)


def _future(minutes=60):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# ─── Endpoint validation ─────────────────────────────────────────────

def test_scheduled_at_naive_rejected(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "scheduled_at": "2030-01-01T12:00:00"})
    assert res.status_code == 400
    assert "timezone" in res.json()["detail"]


def test_scheduled_at_past_rejected(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "scheduled_at": "2020-01-01T12:00:00Z"})
    assert res.status_code == 400
    assert "future" in res.json()["detail"]


def test_public_schedule_enqueues_now_with_native_publish_at(client, monkeypatch):
    from database import SessionLocal, PublishJob

    calls = _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "privacy": "public",
                            "include_short": False, "scheduled_at": _future()})
    assert res.status_code == 200, res.text
    row = res.json()[0]
    # Native mode: queued (uploads immediately), publishAt carried along.
    assert row["status"] == "queued"
    assert row["publish_at_youtube"] is not None
    assert len(calls) == 1

    s = SessionLocal()
    try:
        db_row = s.query(PublishJob).filter(PublishJob.id == row["id"]).one()
        assert db_row.publish_at_youtube is not None
    finally:
        s.close()


def test_unlisted_schedule_waits_for_daemon(client, monkeypatch):
    calls = _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "privacy": "unlisted",
                            "include_short": False, "scheduled_at": _future()})
    assert res.status_code == 200, res.text
    assert res.json()[0]["status"] == "scheduled"
    assert calls == []  # nothing enqueued until due


# ─── Native publishAt body ───────────────────────────────────────────

def test_upload_body_carries_publish_at_as_private(client, monkeypatch):
    """The insert body must be privacyStatus=private + RFC3339 publishAt."""
    import youtube_upload as yt

    captured = {}

    class _FakeRequest:
        def next_chunk(self, num_retries=0):
            class _S:  # noqa: N801
                def progress(self):
                    return 1.0
            return _S(), {"id": "vidsched"}

    class _FakeVideos:
        def insert(self, part, body, media_body):
            captured["body"] = body
            return _FakeRequest()

    class _FakeThumbs:
        def set(self, videoId, media_body):
            class _E:
                def execute(self):
                    return {}
            return _E()

    class _FakeYT:
        def videos(self):
            return _FakeVideos()

        def thumbnails(self):
            return _FakeThumbs()

    monkeypatch.setattr(yt, "_get_youtube_client", lambda channel=None: _FakeYT())
    monkeypatch.setattr(yt, "MediaFileUpload", lambda *a, **k: object())

    publish_at = datetime(2030, 7, 12, 17, 0, tzinfo=timezone.utc)
    result = yt.upload_to_youtube(
        __file__, None, "Artista", "Cancion", "", "public", "job1",
        metadata=dict(METADATA), publish_at=publish_at,
    )
    assert result["video_id"] == "vidsched"
    assert captured["body"]["status"]["privacyStatus"] == "private"
    assert captured["body"]["status"]["publishAt"] == "2030-07-12T17:00:00Z"


# ─── Scheduler daemon ────────────────────────────────────────────────

def test_due_rows_flip_and_enqueue_once(client, monkeypatch):
    import queue_jobs
    from database import SessionLocal, PublishJob
    from youtube_scheduler import run_due_publishes

    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                json={"metadata": METADATA, "privacy": "unlisted",
                      "include_short": False, "scheduled_at": _future()})

    # Make it due.
    s = SessionLocal()
    try:
        s.query(PublishJob).filter(PublishJob.job_id == job_id).update(
            {PublishJob.scheduled_at: datetime.now(timezone.utc) - timedelta(minutes=1)},
            synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()

    enqueued = []
    monkeypatch.setattr(queue_jobs, "enqueue_publish", lambda pk: enqueued.append(pk))

    assert run_due_publishes() == 1
    assert len(enqueued) == 1
    # Second pass: already queued, nothing to do.
    assert run_due_publishes() == 0
    assert len(enqueued) == 1

    s = SessionLocal()
    try:
        assert (
            s.query(PublishJob.status).filter(PublishJob.job_id == job_id).scalar()
            == "queued"
        )
    finally:
        s.close()


def test_not_due_and_canceled_rows_untouched(client, monkeypatch):
    import queue_jobs
    from database import SessionLocal, PublishJob
    from youtube_scheduler import run_due_publishes

    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_a = _seed_done_job(user_id, tenant_id)
    job_b = _seed_done_job(user_id, tenant_id)

    client.post(f"/youtube/publish/{job_a}", headers=auth(token),
                json={"metadata": METADATA, "privacy": "unlisted",
                      "include_short": False, "scheduled_at": _future(120)})
    res_b = client.post(f"/youtube/publish/{job_b}", headers=auth(token),
                        json={"metadata": METADATA, "privacy": "private",
                              "include_short": False, "scheduled_at": _future()})
    pk_b = res_b.json()[0]["id"]

    # Cancel B, then make it (nominally) due.
    assert client.post(f"/youtube/publish-jobs/{pk_b}/cancel", headers=auth(token)).status_code == 200
    s = SessionLocal()
    try:
        s.query(PublishJob).filter(PublishJob.id == pk_b).update(
            {PublishJob.scheduled_at: datetime.now(timezone.utc) - timedelta(minutes=5)},
            synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()

    enqueued = []
    monkeypatch.setattr(queue_jobs, "enqueue_publish", lambda pk: enqueued.append(pk))
    assert run_due_publishes() == 0
    assert enqueued == []


def test_enqueue_failure_puts_row_back_to_scheduled(client, monkeypatch):
    import queue_jobs
    from database import SessionLocal, PublishJob
    from youtube_scheduler import run_due_publishes

    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                json={"metadata": METADATA, "privacy": "unlisted",
                      "include_short": False, "scheduled_at": _future()})
    s = SessionLocal()
    try:
        s.query(PublishJob).filter(PublishJob.job_id == job_id).update(
            {PublishJob.scheduled_at: datetime.now(timezone.utc) - timedelta(minutes=1)},
            synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()

    def _boom(pk):
        raise RuntimeError("redis down")

    monkeypatch.setattr(queue_jobs, "enqueue_publish", _boom)
    assert run_due_publishes() == 0

    s = SessionLocal()
    try:
        # Back to scheduled → the next pass retries.
        assert (
            s.query(PublishJob.status).filter(PublishJob.job_id == job_id).scalar()
            == "scheduled"
        )
    finally:
        s.close()
