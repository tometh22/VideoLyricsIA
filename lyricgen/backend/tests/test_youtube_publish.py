"""Background YouTube publish — PublishJob lifecycle, endpoints, worker task.

The worker task is called synchronously with the Google seams
(upload_to_youtube / storage.download_object) monkeypatched — no network.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import auth


def _register(client):
    from database import SessionLocal, User

    username = f"pubuser_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        return token, u.id, u.tenant_id
    finally:
        s.close()


def _seed_done_job(user_id, tenant_id, *, with_video=True, s3_keys=None, youtube_data=None):
    from database import SessionLocal, Job
    from jobs import create_job
    from main import OUTPUTS_DIR

    db = SessionLocal()
    try:
        job_id = create_job(
            db,
            artist="Intoxicados",
            style="oscuro",
            filename="Intoxicados - Fuego.mp3",
            user_id=user_id,
            tenant_id=tenant_id,
            song_title="Fuego",
        )
        job = db.query(Job).filter(Job.job_id == job_id).first()
        job.status = "done"
        if s3_keys:
            job.s3_keys = s3_keys
        if youtube_data:
            job.youtube_data = youtube_data
        db.commit()
    finally:
        db.close()

    if with_video:
        job_dir = os.path.join(OUTPUTS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        for fname in ("lyric_video.mp4", "short.mp4"):
            with open(os.path.join(job_dir, fname), "wb") as f:
                f.write(b"\x00" * 512)
    return job_id


def _connect_channel(user_id, tenant_id, channel_id="UCpub1"):
    from database import SessionLocal, YouTubeChannel
    from token_crypto import encrypt_token

    s = SessionLocal()
    try:
        row = YouTubeChannel(
            tenant_id=tenant_id,
            channel_id=channel_id,
            channel_title="Canal Pub",
            token_encrypted=encrypt_token({"token": "at", "refresh_token": "rt"}),
            connected_by=user_id,
            status="active",
            is_default=True,
        )
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _patch_enqueue(monkeypatch):
    """Capture enqueue calls instead of touching Redis/threads."""
    import queue_jobs

    calls = []
    monkeypatch.setattr(queue_jobs, "enqueue_publish", lambda pk: calls.append(pk) or f"fake:{pk}")
    return calls


def _fake_upload_ok(monkeypatch, video_id="vidX"):
    import youtube_upload as yt

    captured = {}

    def _fake(video_path, thumbnail_path, artist, song, lyrics_text, privacy,
              job_id, metadata=None, settings=None, channel=None,
              progress_callback=None, publish_at=None):
        captured.update(dict(
            video_path=video_path, artist=artist, song=song, privacy=privacy,
            metadata=metadata, channel=channel, publish_at=publish_at,
        ))
        if progress_callback:
            for p in (10, 55, 100):
                progress_callback(p)
        return {
            "video_id": video_id,
            "url": f"https://youtube.com/watch?v={video_id}",
            "title": (metadata or {}).get("title", "t"),
            "privacy": privacy,
        }

    monkeypatch.setattr(yt, "upload_to_youtube", _fake)
    return captured


METADATA = {"title": "Fuego Aprobado", "description": "desc", "tags": ["a"], "category": "10"}


# ─── Endpoint: create publish ────────────────────────────────────────

def test_publish_creates_rows_and_enqueues(client, monkeypatch):
    from database import SessionLocal, PublishJob

    calls = _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(
        f"/youtube/publish/{job_id}",
        headers=auth(token),
        json={"privacy": "unlisted", "metadata": METADATA, "include_short": True},
    )
    assert res.status_code == 200, res.text
    rows = res.json()
    assert {r["kind"] for r in rows} == {"video", "short"}
    assert all(r["status"] == "queued" for r in rows)
    assert len(calls) == 2

    s = SessionLocal()
    try:
        db_rows = s.query(PublishJob).filter(PublishJob.job_id == job_id).all()
        assert len(db_rows) == 2
        assert all(r.metadata_json["title"] == "Fuego Aprobado" for r in db_rows)
    finally:
        s.close()


def test_publish_active_duplicate_is_409(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    first = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                        json={"metadata": METADATA})
    assert first.status_code == 200
    second = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                         json={"metadata": METADATA})
    assert second.status_code == 409


def test_publish_already_published_video_is_409(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(
        user_id, tenant_id,
        youtube_data={"video_id": "old1", "url": "u", "privacy": "unlisted"},
    )
    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA})
    assert res.status_code == 409


def test_publish_cross_tenant_is_404(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token_a, user_a, tenant_a = _register(client)
    token_b, _, _ = _register(client)
    _connect_channel(user_a, tenant_a)
    job_id = _seed_done_job(user_a, tenant_a)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token_b),
                      json={"metadata": METADATA})
    assert res.status_code == 404


def test_publish_invalid_privacy_rejected(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"privacy": "everyone", "metadata": METADATA})
    assert res.status_code == 400


# ─── Endpoint: cancel ────────────────────────────────────────────────

def test_cancel_queued_publish(client, monkeypatch):
    from database import SessionLocal, PublishJob

    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    rows = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                       json={"metadata": METADATA, "include_short": False}).json()
    pk = rows[0]["id"]

    res = client.post(f"/youtube/publish-jobs/{pk}/cancel", headers=auth(token))
    assert res.status_code == 200

    s = SessionLocal()
    try:
        assert s.query(PublishJob.status).filter(PublishJob.id == pk).scalar() == "canceled"
    finally:
        s.close()


def test_cancel_uploading_publish_is_409(client, monkeypatch):
    from database import SessionLocal, PublishJob

    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    rows = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                       json={"metadata": METADATA, "include_short": False}).json()
    pk = rows[0]["id"]
    s = SessionLocal()
    try:
        s.query(PublishJob).filter(PublishJob.id == pk).update({"status": "uploading"})
        s.commit()
    finally:
        s.close()

    assert client.post(f"/youtube/publish-jobs/{pk}/cancel", headers=auth(token)).status_code == 409


# ─── Worker task ─────────────────────────────────────────────────────

def _create_publish_row(client, monkeypatch, token, include_short=False, **kw):
    _patch_enqueue(monkeypatch)
    res = client.post(
        f"/youtube/publish/{kw['job_id']}",
        headers=auth(token),
        json={"privacy": kw.get("privacy", "unlisted"), "metadata": METADATA,
              "include_short": include_short},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_task_happy_path_publishes_and_mirrors(client, monkeypatch):
    from database import SessionLocal, PublishJob, Job, AuditLog
    from youtube_publish import publish_to_youtube_task

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)
    captured = _fake_upload_ok(monkeypatch, video_id="vid77")
    emails_sent = []
    import emails
    monkeypatch.setattr(emails, "send_video_published",
                        lambda *a, **k: emails_sent.append(a))

    rows = _create_publish_row(client, monkeypatch, token, job_id=job_id)
    pk = rows[0]["id"]

    result = publish_to_youtube_task(pk)
    assert result["video_id"] == "vid77"
    # Approved metadata went through verbatim; channel was resolved.
    assert captured["metadata"]["title"] == "Fuego Aprobado"
    assert captured["channel"] is not None
    assert captured["song"] == "Fuego"

    s = SessionLocal()
    try:
        row = s.query(PublishJob).filter(PublishJob.id == pk).one()
        assert row.status == "published"
        assert row.progress == 100
        assert row.video_id == "vid77"
        # Mirror on the job row for the legacy UI.
        job = s.query(Job).filter(Job.job_id == job_id).one()
        assert job.youtube_data["video_id"] == "vid77"
        audit = (
            s.query(AuditLog).filter(AuditLog.action == "job.youtube_publish")
            .order_by(AuditLog.id.desc()).first()
        )
        assert audit.detail["publish_job_id"] == pk
    finally:
        s.close()
    assert len(emails_sent) == 1


def test_task_short_appends_shorts_tag(client, monkeypatch):
    from youtube_publish import publish_to_youtube_task

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)
    captured = _fake_upload_ok(monkeypatch)
    import emails
    monkeypatch.setattr(emails, "send_video_published", lambda *a, **k: None)

    rows = _create_publish_row(client, monkeypatch, token, include_short=True, job_id=job_id)
    short_pk = next(r["id"] for r in rows if r["kind"] == "short")

    publish_to_youtube_task(short_pk)
    assert captured["metadata"]["title"].endswith("#Shorts")
    assert captured["video_path"].endswith("short.mp4")


def test_task_downloads_from_r2_when_local_missing(client, monkeypatch):
    import storage
    from youtube_publish import publish_to_youtube_task

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    r2_key = f"{tenant_id}/some-job/lyric_video.mp4"
    job_id = _seed_done_job(
        user_id, tenant_id, with_video=False, s3_keys={"video": r2_key},
    )
    captured = _fake_upload_ok(monkeypatch)
    import emails
    monkeypatch.setattr(emails, "send_video_published", lambda *a, **k: None)

    downloads = []

    def _fake_download(key, dest):
        downloads.append(key)
        with open(dest, "wb") as f:
            f.write(b"\x00" * 64)
        return True

    monkeypatch.setattr(storage, "download_object", _fake_download)

    rows = _create_publish_row(client, monkeypatch, token, job_id=job_id)
    result = publish_to_youtube_task(rows[0]["id"])
    assert "video_id" in result
    assert downloads == [r2_key]
    # Uploaded from the temp download, and the temp dir was cleaned up.
    assert "ytpub_" in captured["video_path"]
    assert not os.path.exists(os.path.dirname(captured["video_path"]))


def test_task_failure_marks_failed_with_sanitized_error(client, monkeypatch):
    import youtube_upload as yt
    from database import SessionLocal, PublishJob
    from youtube_publish import publish_to_youtube_task

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    def _boom(*a, **kw):
        raise RuntimeError("secret /app/token path")

    monkeypatch.setattr(yt, "upload_to_youtube", _boom)
    failures = []
    import emails
    monkeypatch.setattr(emails, "send_video_publish_failed",
                        lambda *a, **k: failures.append(a))

    rows = _create_publish_row(client, monkeypatch, token, job_id=job_id)
    pk = rows[0]["id"]
    result = publish_to_youtube_task(pk)
    assert "error" in result

    s = SessionLocal()
    try:
        row = s.query(PublishJob).filter(PublishJob.id == pk).one()
        assert row.status == "failed"
        assert "RuntimeError" in row.error
        assert "token path" not in row.error
    finally:
        s.close()
    assert len(failures) == 1

    # A failed row is out of the "active" set → the user can retry.
    retry = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                        json={"metadata": METADATA, "include_short": False})
    assert retry.status_code == 200


def test_task_claim_prevents_double_run(client, monkeypatch):
    from youtube_publish import publish_to_youtube_task

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)
    _fake_upload_ok(monkeypatch)
    import emails
    monkeypatch.setattr(emails, "send_video_published", lambda *a, **k: None)

    rows = _create_publish_row(client, monkeypatch, token, job_id=job_id)
    pk = rows[0]["id"]

    first = publish_to_youtube_task(pk)
    assert "video_id" in first
    second = publish_to_youtube_task(pk)
    assert second.get("skipped") is True


def test_task_dedupes_against_existing_mirror(client, monkeypatch):
    """Crash-replay: video kind with youtube_data already set → no re-upload."""
    import youtube_upload as yt
    from database import SessionLocal, PublishJob, Job
    from youtube_publish import publish_to_youtube_task

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    def _boom(*a, **kw):
        raise AssertionError("must not upload again")

    monkeypatch.setattr(yt, "upload_to_youtube", _boom)

    rows = _create_publish_row(client, monkeypatch, token, job_id=job_id)
    pk = rows[0]["id"]

    # Simulate: a previous run published and mirrored, then crashed before
    # updating the PublishJob row; RQ replays the task.
    s = SessionLocal()
    try:
        s.query(Job).filter(Job.job_id == job_id).update(
            {Job.youtube_data: {"video_id": "prev9", "url": "u", "privacy": "unlisted"}},
            synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()

    result = publish_to_youtube_task(pk)
    assert result.get("deduped") is True

    s = SessionLocal()
    try:
        row = s.query(PublishJob).filter(PublishJob.id == pk).one()
        assert row.status == "published"
        assert row.video_id == "prev9"
    finally:
        s.close()


# ─── Reaper ──────────────────────────────────────────────────────────

def test_reaper_fails_orphaned_uploading_rows(client, monkeypatch):
    from datetime import timedelta
    from database import SessionLocal, PublishJob
    from reaper import reap_stuck_publish_jobs

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)
    rows = _create_publish_row(client, monkeypatch, token, job_id=job_id)
    pk = rows[0]["id"]

    s = SessionLocal()
    try:
        s.query(PublishJob).filter(PublishJob.id == pk).update({
            PublishJob.status: "uploading",
            PublishJob.started_at: datetime.now(timezone.utc) - timedelta(hours=2),
        }, synchronize_session=False)
        s.commit()

        flipped = reap_stuck_publish_jobs(s)
        s.commit()
        assert flipped == 1
        assert s.query(PublishJob.status).filter(PublishJob.id == pk).scalar() == "failed"
    finally:
        s.close()
