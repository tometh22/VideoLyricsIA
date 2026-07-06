"""YouTube publish flow — endpoint hardening and metadata templating.

Covers the audit fixes:
  - privacy param validated against YouTube's accepted values
  - idempotency: a job that already has a published video returns it
    instead of uploading a duplicate; a different requested privacy
    updates the existing video instead
  - upload claim: a DB-level marker makes the double-upload guard work
    across workers; fresh claim → 409, orphaned claim → taken over,
    failed upload → claim released
  - missing/broken server token → clean 503 (not a bare 500)
  - upload errors → sanitized 502 detail (no raw exception internals)
  - approved preview metadata is published as-is (no regeneration)
  - job.song_title preferred over re-parsing the filename
  - publish writes an AuditLog row
  - apply_settings_template applies the user's DB-backed template
"""

import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from tests.conftest import auth


# ─── Pure helper: apply_settings_template ────────────────────────────

def _base_metadata():
    return {
        "title": "AI Title",
        "description": "AI description body",
        "tags": ["ai-tag"],
    }


def test_template_empty_settings_is_noop():
    from youtube_upload import apply_settings_template
    md = apply_settings_template(_base_metadata(), "Artista", "Cancion", {})
    assert md["title"] == "AI Title"
    assert md["description"] == "AI description body"
    assert md["tags"] == ["ai-tag"]


def test_template_title_format_placeholders():
    from youtube_upload import apply_settings_template
    md = apply_settings_template(
        _base_metadata(), "Intoxicados", "Fuego",
        {"titleFormat": "{artista} - {cancion} (Letra)"},
    )
    assert md["title"] == "Intoxicados - Fuego (Letra)"


def test_template_description_header_footer_hashtags():
    from youtube_upload import apply_settings_template
    md = apply_settings_template(
        _base_metadata(), "A", "B",
        {
            "descriptionHeader": "Header {artista}",
            "descriptionFooter": "Footer {cancion}",
            "hashtags": "#letra #{artista}",
        },
    )
    parts = md["description"].split("\n\n")
    assert parts[0] == "Header A"
    assert parts[1] == "AI description body"
    assert parts[2] == "Footer B"
    assert parts[3] == "#letra #A"


def test_template_mandatory_tags_prepended():
    from youtube_upload import apply_settings_template
    md = apply_settings_template(
        _base_metadata(), "A", "B", {"mandatoryTags": "uno, dos , ,tres"},
    )
    assert md["tags"] == ["uno", "dos", "tres", "ai-tag"]


# ─── Endpoint fixtures ───────────────────────────────────────────────

def _register(client):
    """Register a user; return (token, user_id, tenant_id)."""
    from database import SessionLocal, User

    username = f"ytuser_{uuid.uuid4().hex[:6]}"
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


def _seed_done_job(user_id, tenant_id, *, youtube_data=None, with_video=True, song_title=""):
    """Create a done Job and (optionally) its video file on disk."""
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
            song_title=song_title,
        )
        job = db.query(Job).filter(Job.job_id == job_id).first()
        job.status = "done"
        if youtube_data:
            job.youtube_data = youtube_data
        db.commit()
    finally:
        db.close()

    if with_video:
        job_dir = os.path.join(OUTPUTS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "lyric_video.mp4"), "wb") as f:
            f.write(b"\x00" * 1024)

    return job_id


# ─── Endpoint: validation ────────────────────────────────────────────

def test_invalid_privacy_rejected(client):
    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id, with_video=False)

    res = client.post(f"/youtube/upload/{job_id}?privacy=everyone", headers=auth(token))
    assert res.status_code == 400
    assert "privacy" in res.json()["detail"]


def test_job_not_done_rejected(client):
    from database import SessionLocal, Job

    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id, with_video=False)
    s = SessionLocal()
    try:
        s.query(Job).filter(Job.job_id == job_id).first().status = "pending_review"
        s.commit()
    finally:
        s.close()

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 400
    assert "not done" in res.json()["detail"]


def test_other_tenants_job_is_404(client):
    token_a, user_a, tenant_a = _register(client)
    token_b, _, _ = _register(client)
    job_id = _seed_done_job(user_a, tenant_a, with_video=False)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token_b))
    assert res.status_code == 404


def test_empty_approved_title_rejected(client):
    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(
        f"/youtube/upload/{job_id}",
        headers=auth(token),
        json={"metadata": {"title": "   ", "description": "x"}},
    )
    assert res.status_code == 400
    assert "title" in res.json()["detail"]


# ─── Endpoint: idempotency & concurrency ─────────────────────────────

def test_already_published_returns_existing_without_reupload(client, monkeypatch):
    import youtube_upload as yt

    def _boom(*a, **kw):
        raise AssertionError("upload_to_youtube must not be called again")

    monkeypatch.setattr(yt, "upload_to_youtube", _boom)

    existing = {"video_id": "abc123", "url": "https://youtube.com/watch?v=abc123",
                "title": "T", "privacy": "unlisted"}
    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id, youtube_data=existing)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 200
    assert res.json()["video_id"] == "abc123"


def test_already_published_different_privacy_updates_video(client, monkeypatch):
    import youtube_upload as yt

    calls = {}

    def _boom(*a, **kw):
        raise AssertionError("upload_to_youtube must not be called again")

    def _fake_privacy(video_id, privacy, channel=None):
        calls["args"] = (video_id, privacy)

    monkeypatch.setattr(yt, "upload_to_youtube", _boom)
    monkeypatch.setattr(yt, "update_video_privacy", _fake_privacy)

    existing = {"video_id": "abc123", "url": "https://youtube.com/watch?v=abc123",
                "title": "T", "privacy": "unlisted"}
    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id, youtube_data=existing)

    res = client.post(f"/youtube/upload/{job_id}?privacy=public", headers=auth(token))
    assert res.status_code == 200, res.text
    assert calls["args"] == ("abc123", "public")
    assert res.json()["privacy"] == "public"
    assert res.json()["video_id"] == "abc123"

    from database import SessionLocal, Job, AuditLog
    s = SessionLocal()
    try:
        job = s.query(Job).filter(Job.job_id == job_id).first()
        assert job.youtube_data["privacy"] == "public"
        entry = (
            s.query(AuditLog)
            .filter(AuditLog.action == "job.youtube_privacy_change")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None and entry.detail["job_id"] == job_id
    finally:
        s.close()


def test_fresh_upload_claim_blocks_second_request(client):
    """A job whose youtube_data holds a fresh 'uploading' claim → 409.
    The claim lives in the DB, so it holds across workers/replicas."""
    token, user_id, tenant_id = _register(client)
    marker = {"status": "uploading",
              "started_at": datetime.now(timezone.utc).isoformat(), "by": user_id}
    job_id = _seed_done_job(user_id, tenant_id, youtube_data=marker)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 409


def test_stale_upload_claim_is_taken_over(client, monkeypatch):
    """An orphaned claim (worker died mid-upload) doesn't wedge the job."""
    import youtube_upload as yt

    def _fake_upload(*a, **kw):
        return {"video_id": "vid9", "url": "u", "title": "t",
                "privacy": kw.get("privacy") or a[5]}

    monkeypatch.setattr(yt, "upload_to_youtube", _fake_upload)

    token, user_id, tenant_id = _register(client)
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    marker = {"status": "uploading", "started_at": stale, "by": user_id}
    job_id = _seed_done_job(user_id, tenant_id, youtube_data=marker)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["video_id"] == "vid9"


# ─── Endpoint: error mapping ─────────────────────────────────────────

def test_missing_token_maps_to_503(client, monkeypatch):
    import youtube_upload as yt

    def _not_configured(*a, **kw):
        raise yt.YouTubeNotConfiguredError("no token file")

    monkeypatch.setattr(yt, "upload_to_youtube", _not_configured)

    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 503
    assert "not connected" in res.json()["detail"]


def test_revoked_refresh_token_maps_to_503(client, monkeypatch):
    """An expired/revoked OAuth grant is an operator problem, not a user
    problem — same 'not connected' message as a missing token."""
    import youtube_upload as yt
    from google.auth.exceptions import RefreshError

    def _revoked(*a, **kw):
        raise RefreshError("invalid_grant: Token has been expired or revoked")

    monkeypatch.setattr(yt, "upload_to_youtube", _revoked)

    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 503
    assert "not connected" in res.json()["detail"]
    assert "invalid_grant" not in res.json()["detail"]


def test_upload_failure_maps_to_502_sanitized_and_releases_claim(client, monkeypatch):
    import youtube_upload as yt

    def _boom(*a, **kw):
        raise RuntimeError("secret /app/youtube_token.json path leaked")

    monkeypatch.setattr(yt, "upload_to_youtube", _boom)

    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 502
    detail = res.json()["detail"]
    # Sanitized: exception class only, never the raw message (may carry
    # token paths / API URLs).
    assert "RuntimeError" in detail
    assert "youtube_token" not in detail

    # Claim must be released so the user can retry.
    from database import SessionLocal, Job
    s = SessionLocal()
    try:
        job = s.query(Job).filter(Job.job_id == job_id).first()
        assert job.youtube_data is None
    finally:
        s.close()


# ─── Endpoint: happy path ────────────────────────────────────────────

def test_publish_uses_approved_metadata_persists_and_audits(client, monkeypatch):
    import youtube_upload as yt

    calls = {}

    def _fake_upload(video_path, thumbnail_path, artist, song, lyrics_text,
                     privacy, job_id, metadata=None, settings=None, channel=None):
        calls["metadata"] = metadata
        calls["privacy"] = privacy
        return {"video_id": "vid42", "url": "https://youtube.com/watch?v=vid42",
                "title": metadata["title"], "privacy": privacy}

    monkeypatch.setattr(yt, "upload_to_youtube", _fake_upload)

    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id)

    approved = {"title": "Approved Title", "description": "Approved desc",
                "tags": ["a", "b"], "category": "10"}
    res = client.post(
        f"/youtube/upload/{job_id}?privacy=public",
        headers=auth(token),
        json={"metadata": approved},
    )
    assert res.status_code == 200, res.text
    assert res.json()["video_id"] == "vid42"

    # The approved metadata went through verbatim — no regeneration.
    assert calls["metadata"]["title"] == "Approved Title"
    assert calls["metadata"]["tags"] == ["a", "b"]
    assert calls["privacy"] == "public"

    # Result persisted on the job.
    from database import SessionLocal, Job, AuditLog
    s = SessionLocal()
    try:
        job = s.query(Job).filter(Job.job_id == job_id).first()
        assert job.youtube_data["video_id"] == "vid42"

        entry = (
            s.query(AuditLog)
            .filter(AuditLog.action == "job.youtube_publish")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        assert entry.user_id == user_id
        assert entry.detail["job_id"] == job_id
        assert entry.detail["video_id"] == "vid42"
        assert entry.detail["privacy"] == "public"
    finally:
        s.close()


def test_song_title_column_preferred_over_filename(client, monkeypatch):
    """A manually corrected song title must reach the published metadata
    instead of the raw-filename heuristic."""
    import youtube_upload as yt

    captured = {}

    def _fake_upload(video_path, thumbnail_path, artist, song, lyrics_text,
                     privacy, job_id, metadata=None, settings=None, channel=None):
        captured["song"] = song
        return {"video_id": "v1", "url": "u", "title": song, "privacy": privacy}

    monkeypatch.setattr(yt, "upload_to_youtube", _fake_upload)

    token, user_id, tenant_id = _register(client)
    job_id = _seed_done_job(user_id, tenant_id, song_title="Fuego (Versión Corregida)")

    res = client.post(f"/youtube/upload/{job_id}", headers=auth(token))
    assert res.status_code == 200, res.text
    assert captured["song"] == "Fuego (Versión Corregida)"
