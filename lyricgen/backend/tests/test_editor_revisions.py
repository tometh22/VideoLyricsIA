"""Concurrency guarantees for the durable lyrics editor."""

import json
import uuid

from tests.conftest import auth


def _user(client):
    username = f"editor_{uuid.uuid4().hex[:8]}"
    response = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert response.status_code == 200, response.text
    from database import SessionLocal, User
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).one()
        user.ai_authorized = True
        db.commit()
    finally:
        db.close()
    return response.json()["token"]


def _job_for(token):
    from auth import decode_token
    from database import SessionLocal
    from jobs import create_job

    # The JWT helper is intentionally not part of the public API; tests use
    # the token payload only to obtain the owner/tenant for the fixture row.
    payload = decode_token(token)
    db = SessionLocal()
    try:
        return create_job(
            db,
            artist="Artist",
            style="oscuro",
            filename="song.wav",
            user_id=int(payload["sub"]),
            tenant_id=payload["tenant_id"],
            initial_status="transcribed_pending",
            song_title="Song",
        )
    finally:
        db.close()


def _seed_segments(job_id):
    from database import SessionLocal
    from jobs import get_job_model

    db = SessionLocal()
    try:
        job = get_job_model(db, job_id)
        job.segments_json = [
            {"segment_id": "0", "start": 0, "end": 1, "text": "one"},
            {"segment_id": "1", "start": 1, "end": 2, "text": "two"},
        ]
        job.segments_revision = 0
        db.commit()
    finally:
        db.close()


def test_stale_editor_write_is_rejected_without_mutating_server(client):
    token = _user(client)
    job_id = _job_for(token)
    _seed_segments(job_id)

    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200
    assert loaded.json()["revision"] == 0

    first = [
        {"segment_id": "0", "start": 0, "end": 1, "text": "ONE"},
        {"segment_id": "1", "start": 1, "end": 2, "text": "two"},
    ]
    saved = client.patch(
        f"/editor/{job_id}",
        json={"base_revision": 0, "segments": first},
        headers=auth(token),
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1

    stale = [
        {"segment_id": "0", "start": 0, "end": 1, "text": "stale"},
        {"segment_id": "1", "start": 1, "end": 2, "text": "two"},
    ]
    rejected = client.patch(
        f"/editor/{job_id}",
        json={"base_revision": 0, "segments": stale},
        headers=auth(token),
    )
    assert rejected.status_code == 409
    detail = rejected.json()["detail"]
    assert detail["server_revision"] == 1
    assert detail["server_segments"] == first

    current = client.get(f"/editor/{job_id}", headers=auth(token)).json()
    assert current["revision"] == 1
    assert current["segments"] == first


def test_approval_requires_current_editor_revision(client, monkeypatch):
    token = _user(client)
    job_id = _job_for(token)
    _seed_segments(job_id)

    saved = client.patch(
        f"/editor/{job_id}",
        json={
            "base_revision": 0,
            "segments": [{"segment_id": "0", "start": 0, "end": 1, "text": "new"}],
        },
        headers=auth(token),
    )
    assert saved.status_code == 200

    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )
    response = client.post(
        "/generate",
        data={
            "job_id": job_id,
            "editor_revision": "0",
            "artist": "Artist",
            "song_title": "Song",
            "segments_json": json.dumps([]),
            "delivery_profile": "youtube",
        },
        headers=auth(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["server_revision"] == 1
