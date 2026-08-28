"""Audit trail on media delivery endpoints (UMG-launch hardening 2026-06-01).

A record label's first compliance question is "who accessed which master,
when". Approve/reject/delete were already audited; the endpoints that
actually serve bytes were not. Pinned here:

  - /download/{job_id}/{file_type} → AuditLog action="job.download"
    (only when the response actually delivers the file).
  - /jobs/{job_id}/source-audio-url → action="job.source_audio_access".
  - /preview/{...} → NO audit by design: the dashboard polls it 6+ times
    per refresh and masters are NON_PREVIEWABLE anyway. A row per poll
    would bury the real download events.
  - Failed/denied requests → NO audit (an audit row means delivery
    happened, not that someone knocked on the door).

R2 storage is mocked at the module level (same approach as
test_source_audio_url.py): is_enabled→True + generate_signed_url→fake URL
exercise the redirect branch without touching boto3.
"""

import uuid

from database import AuditLog, Job as JobModel, SessionLocal, User as UserModel


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    return admin.id, admin.tenant_id


def _create_job(db, tenant_id, user_id, *, status="done", s3_keys=None,
                input_r2_key="inputs/default/x/song.mp3"):
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Audit Test",
        song_title="Audit Trail",
        filename="song.mp3",
        status=status,
        delivery_profile="youtube",
        progress=100,
        input_r2_key=input_r2_key,
        s3_keys=s3_keys or {},
    ))
    db.commit()
    return job_id


def _audit_rows_for(job_id):
    """All AuditLog rows whose detail references this job."""
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).all()
        return [
            r for r in rows
            if isinstance(r.detail, dict) and r.detail.get("job_id") == job_id
        ]
    finally:
        db.close()


def _mock_r2(monkeypatch, *, exists=True):
    import storage
    signed_url_calls = []

    def _signed_url(
        key, expiry_seconds=3600, download_filename=None,
        response_content_type=None,
    ):
        signed_url_calls.append((key, expiry_seconds, download_filename))
        return f"https://r2.fake/{key}?sig=ok"

    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "object_exists", lambda key: exists)
    monkeypatch.setattr(storage, "generate_signed_url", _signed_url)
    return signed_url_calls


def _media_token(client, admin_token, job_id, file_type):
    res = client.get(
        f"/media-token/{job_id}/{file_type}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    return res.json()["token"]


# ---------------------------------------------------------------------------
# /download
# ---------------------------------------------------------------------------

def test_download_writes_audit_log(client, admin_token, db, monkeypatch):
    """Successful R2-redirect download → exactly one job.download row with
    user, tenant, and file_type recorded."""
    signed_url_calls = _mock_r2(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id,
                         s3_keys={"video": f"{tenant_id}/{uuid.uuid4().hex[:8]}/lyric_video.mp4"})

    token = _media_token(client, admin_token, job_id, "video")
    res = client.get(f"/download/{job_id}/video?token={token}", follow_redirects=False)
    assert res.status_code == 302, res.text
    assert signed_url_calls[0][1] == 3600

    rows = [r for r in _audit_rows_for(job_id) if r.action == "job.download"]
    assert len(rows) == 1, f"expected exactly 1 job.download audit row, got {len(rows)}"
    row = rows[0]
    assert row.user_id == user_id
    assert row.detail["tenant_id"] == tenant_id
    assert row.detail["file_type"] == "video"
    assert row.detail["source"] == "r2_redirect"


def test_download_not_done_writes_no_audit(client, admin_token, db, monkeypatch):
    """A 400 (job not done) is not a delivery — no audit row."""
    _mock_r2(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id, status="processing",
                         s3_keys={"video": "t/x/lyric_video.mp4"})

    token = _media_token(client, admin_token, job_id, "video")
    res = client.get(f"/download/{job_id}/video?token={token}", follow_redirects=False)
    assert res.status_code == 400

    assert _audit_rows_for(job_id) == [], "failed download must not create audit rows"


# ---------------------------------------------------------------------------
# /jobs/{job_id}/source-audio-url
# ---------------------------------------------------------------------------

def test_source_audio_url_writes_audit_log(client, admin_token, db, monkeypatch):
    """Serving a signed URL to the ORIGINAL master → audit row tagged with
    source=input."""
    _mock_r2(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id)

    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "input"

    rows = [r for r in _audit_rows_for(job_id) if r.action == "job.source_audio_access"]
    assert len(rows) == 1
    assert rows[0].user_id == user_id
    assert rows[0].detail["tenant_id"] == tenant_id
    assert rows[0].detail["source"] == "input"


def test_preview_signed_url_does_not_outlive_scoped_token(
    client, admin_token, db, monkeypatch,
):
    from auth import MEDIA_TOKEN_EXPIRE_SECONDS

    calls = _mock_r2(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(
        db,
        tenant_id,
        user_id,
        status="pending_review",
        s3_keys={"video": f"{tenant_id}/preview/video.mp4"},
    )
    token = _media_token(client, admin_token, job_id, "video")

    response = client.get(
        f"/preview/{job_id}/video?token={token}",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert calls[0][1] == MEDIA_TOKEN_EXPIRE_SECONDS


def test_source_audio_url_404_writes_no_audit(client, admin_token, db, monkeypatch):
    """When neither the original nor a rendered fallback exists, nothing
    was delivered — no audit row."""
    _mock_r2(monkeypatch, exists=False)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id)

    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404
    assert _audit_rows_for(job_id) == []


# ---------------------------------------------------------------------------
# /preview — deliberately NOT audited
# ---------------------------------------------------------------------------

def test_preview_does_not_write_audit_log(client, admin_token, db, monkeypatch):
    """Documents the design decision: preview is polled by the dashboard,
    auditing it would bury real download events under noise. Masters
    can't be previewed at all (NON_PREVIEWABLE), so no compliance gap."""
    _mock_r2(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id,
                         s3_keys={"video": "t/x/lyric_video.mp4"})

    token = _media_token(client, admin_token, job_id, "video")
    res = client.get(f"/preview/{job_id}/video?token={token}", follow_redirects=False)
    assert res.status_code == 302, res.text

    assert _audit_rows_for(job_id) == [], (
        "/preview must NOT write audit rows (polled endpoint — see module docstring)"
    )
