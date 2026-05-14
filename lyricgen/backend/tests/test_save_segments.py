"""Tests for POST /jobs/{job_id}/save-segments.

This endpoint persists user-edited segments during the editing phase
(status=transcribed_pending) so that:
  1. The reaper's staleness anchor (last_user_activity_at) gets bumped
     and active sessions don't get reaped at the TTL.
  2. We can recover segments after a tab refresh / cross-device handoff.

Pre-incident behavior: segments only touched the backend at POST /generate.
A 90-min batch-edit session got reaped at 30 min and the user lost everything.
"""

import uuid

import pytest

from tests.conftest import auth


def _make_user(client):
    """Register a user and return (username, token)."""
    from database import SessionLocal, User

    username = f"saveuser_{uuid.uuid4().hex[:6]}"
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
        u.ai_authorized = True
        s.commit()
        return username, token, u.id, u.tenant_id
    finally:
        s.close()


def _seed_transcribed_pending(user_id: int, tenant_id: str, *, status: str = "transcribed_pending"):
    """Drop a Job row at the given status with no segments yet."""
    from database import SessionLocal
    from jobs import create_job

    db = SessionLocal()
    try:
        if status == "transcribed_pending":
            job_id = create_job(
                db,
                artist="Intoxicados",
                style="oscuro",
                filename="song.wav",
                user_id=user_id,
                tenant_id=tenant_id,
                initial_status="transcribed_pending",
                song_title="No Tengo Ganas",
            )
        else:
            # create_job only accepts a small whitelist of starting states;
            # for anything else (e.g. "done"), seed directly so we can drive
            # the status-gate assertions in /save-segments.
            from database import Job
            job_id = uuid.uuid4().hex[:12]
            db.add(Job(
                job_id=job_id,
                user_id=user_id,
                tenant_id=tenant_id,
                artist="Intoxicados",
                song_title="No Tengo Ganas",
                style="oscuro",
                filename="song.wav",
                status=status,
                current_step="done" if status == "done" else "editing",
                progress=100 if status == "done" else 0,
                delivery_profile="youtube",
            ))
            db.commit()
        return job_id
    finally:
        db.close()


def test_save_segments_persists_and_bumps_activity(client):
    """Happy path: owner posts segments → row gets segments_json +
    last_user_activity_at updated, response is 200."""
    _, token, user_id, tenant_id = _make_user(client)
    job_id = _seed_transcribed_pending(user_id, tenant_id)

    segments = [
        {"start": 0.0, "end": 2.5, "text": "primera línea"},
        {"start": 2.5, "end": 5.0, "text": "segunda línea editada"},
    ]
    res = client.post(
        f"/jobs/{job_id}/save-segments",
        json={"segments": segments},
        headers=auth(token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["job_id"] == job_id
    assert body["count"] == 2
    assert body["saved_at"] is not None

    # Verify side-effects on the row.
    from database import Job, SessionLocal
    s = SessionLocal()
    try:
        row = s.query(Job).filter(Job.job_id == job_id).first()
        assert row.segments_json == segments
        assert row.last_user_activity_at is not None
    finally:
        s.close()


def test_save_segments_rejects_other_users_jobs(client):
    """User B cannot save segments to a job owned by user A. Must 404 —
    the same opaque response /generate uses, not a leak."""
    _, token_a, a_user_id, a_tenant_id = _make_user(client)
    _, token_b, _, _ = _make_user(client)

    job_id = _seed_transcribed_pending(a_user_id, a_tenant_id)

    res = client.post(
        f"/jobs/{job_id}/save-segments",
        json={"segments": [{"start": 0, "end": 1, "text": "x"}]},
        headers=auth(token_b),
    )
    assert res.status_code == 404


def test_save_segments_rejects_unknown_job(client):
    """A job_id that doesn't exist → 404, not 500."""
    _, token, _, _ = _make_user(client)

    res = client.post(
        "/jobs/deadbeefdead/save-segments",
        json={"segments": [{"start": 0, "end": 1, "text": "x"}]},
        headers=auth(token),
    )
    assert res.status_code == 404


def test_save_segments_rejects_wrong_status(client):
    """Status gate: only transcribed_pending accepts /save-segments.
    pending_review uses /edit, done is terminal, etc."""
    _, token, user_id, tenant_id = _make_user(client)
    # Seed as "done" — terminal state, /save-segments must refuse.
    job_id = _seed_transcribed_pending(user_id, tenant_id, status="done")

    res = client.post(
        f"/jobs/{job_id}/save-segments",
        json={"segments": [{"start": 0, "end": 1, "text": "x"}]},
        headers=auth(token),
    )
    assert res.status_code == 409
    assert "transcribed_pending" in res.json()["detail"]


def test_save_segments_validates_shape(client):
    """Missing keys (start/end/text) → 400 with a specific index."""
    _, token, user_id, tenant_id = _make_user(client)
    job_id = _seed_transcribed_pending(user_id, tenant_id)

    # segment[1] is missing "text".
    bad = [
        {"start": 0, "end": 1, "text": "ok"},
        {"start": 1, "end": 2},
    ]
    res = client.post(
        f"/jobs/{job_id}/save-segments",
        json={"segments": bad},
        headers=auth(token),
    )
    assert res.status_code == 400
    assert "segments[1]" in res.json()["detail"]
    assert "text" in res.json()["detail"]


def test_save_segments_requires_auth(client):
    """No Authorization header → 401, never 200."""
    res = client.post(
        "/jobs/anything12345/save-segments",
        json={"segments": []},
    )
    assert res.status_code in (401, 403)
