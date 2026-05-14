"""get_job_model_resilient + /upload-part-proxy SSL-drop resilience.

Production incident 2026-05-14: Postgres SSL drops on Railway killed
multipart upload parts. The global DbTransientRetryMiddleware can't
recover them because the part body is 8 MB and the middleware's replay
buffer caps at 1 MiB. Inline retry in the handler is the fix.

Cases covered here:

1. helper retries successfully on a single transient OperationalError
2. helper propagates a non-transient OperationalError without retry
3. helper propagates the last error after exhausting attempts
4. /upload-part-proxy returns 200 when the first DB query throws a
   transient OperationalError and the retry succeeds (end-to-end:
   confirms the swap from get_job_model → get_job_model_resilient
   actually wires through the API surface).
"""

import uuid

import pytest
from sqlalchemy.exc import OperationalError

from database import Job as JobModel, User as UserModel
from jobs import get_job_model_resilient


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    return admin.id, admin.tenant_id


def _create_upload_job(db, tenant_id, user_id):
    """Job in 'awaiting_upload' so /upload-part-proxy will accept parts."""
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="SSL Drop Test",
        filename="test.wav",
        status="awaiting_upload",
        delivery_profile="youtube",
        multipart_upload_id="fake-upload-id-12345",
        input_r2_key="inputs/default/x/test.wav",
        progress=0,
    )
    db.add(job)
    db.commit()
    return job_id


# ── Helper-level tests ──────────────────────────────────────────────────


def test_helper_retries_on_transient_ssl_drop(db, admin_token, monkeypatch):
    """First call raises SSL-drop OperationalError, second returns the row.

    admin_token is included as a fixture so the admin user gets seeded
    via the login path (conftest creates it lazily on first login).
    """
    del admin_token  # used only for side-effect of seeding admin
    import jobs

    real = jobs.get_job_model
    call_count = {"n": 0}

    def flaky(session, job_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OperationalError(
                "SELECT 1",
                {},
                Exception("SSL connection has been closed unexpectedly"),
            )
        return real(session, job_id)

    monkeypatch.setattr(jobs, "get_job_model", flaky)

    # Seed a job to look up.
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    job_id = _create_upload_job(db, admin.tenant_id, admin.id)

    result = get_job_model_resilient(db, job_id)
    assert call_count["n"] == 2, "expected exactly one retry"
    assert result is not None
    assert result.job_id == job_id


def test_helper_propagates_non_transient_error(db, monkeypatch):
    """OperationalError without a transient marker → propagate immediately."""
    import jobs

    call_count = {"n": 0}

    def always_fail(session, job_id):
        call_count["n"] += 1
        raise OperationalError(
            "SELECT 1",
            {},
            Exception("syntax error at or near 'SELEC'"),
        )

    monkeypatch.setattr(jobs, "get_job_model", always_fail)

    with pytest.raises(OperationalError) as ei:
        get_job_model_resilient(db, "anyjob")
    assert "syntax error" in str(ei.value)
    assert call_count["n"] == 1, "non-transient should not retry"


def test_helper_exhausts_retries_then_raises(db, monkeypatch):
    """All attempts hit transient errors → propagate the last one."""
    import jobs

    call_count = {"n": 0}

    def always_drop(session, job_id):
        call_count["n"] += 1
        raise OperationalError(
            "SELECT 1",
            {},
            Exception("server closed the connection unexpectedly"),
        )

    monkeypatch.setattr(jobs, "get_job_model", always_drop)

    # Pass max_attempts=2 explicitly so the test is independent of the
    # production default (which we may tune up/down over time).
    with pytest.raises(OperationalError) as ei:
        get_job_model_resilient(db, "anyjob", max_attempts=2)
    assert "server closed the connection" in str(ei.value)
    assert call_count["n"] == 2, "should attempt exactly max_attempts times"


# ── End-to-end: /upload-part-proxy actually uses the resilient path ─────


def test_upload_part_proxy_recovers_from_transient_ssl_drop(
    client, admin_token, db, monkeypatch,
):
    """First DB lookup in /upload-part-proxy fails with SSL drop;
    handler retries via get_job_model_resilient and returns 200."""
    import jobs
    import storage

    user_id, tenant_id = _admin_identity(db)
    job_id = _create_upload_job(db, tenant_id, user_id)

    # First call raises transient; subsequent calls return the row.
    real_get = jobs.get_job_model
    call_count = {"n": 0}

    def flaky(session, jid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OperationalError(
                "SELECT 1",
                {},
                Exception("SSL connection has been closed unexpectedly"),
            )
        return real_get(session, jid)

    monkeypatch.setattr(jobs, "get_job_model", flaky)

    # Don't actually hit R2. Return a fake etag.
    monkeypatch.setattr(storage, "upload_part",
                        lambda key, upload_id, part_number, data: "fake-etag-abc123")

    # Send a tiny part body. The endpoint reads `await request.body()`
    # AFTER the DB lookup — if the retry didn't work, this would 500.
    res = client.post(
        f"/upload-part-proxy?job_id={job_id}&part_number=1",
        headers={"Authorization": f"Bearer {admin_token}",
                 "Content-Type": "application/octet-stream"},
        content=b"x" * 1024,  # 1 KB chunk
    )
    assert res.status_code == 200, res.text
    assert res.json()["etag"] == "fake-etag-abc123"
    assert call_count["n"] >= 2, "expected at least one retry"
