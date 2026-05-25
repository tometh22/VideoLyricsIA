"""Tests for the /generate flow that reuses an existing job_id from
/transcribe instead of re-uploading the audio body.

This is the fix for the OOM-on-large-WAV bug: previously the frontend
uploaded the same WAV file twice (once to /transcribe, once to /generate)
and the second read caused a Railway 502 with no CORS headers, surfacing
to the operator as the generic "Error al procesar. Intentá de nuevo."
"""

import io
import json
import os
import struct

import pytest

from tests.conftest import auth


def _wav_bytes(payload_size: int = 64) -> bytes:
    """Build a minimal valid RIFF/WAVE file. Size is whatever; the magic
    bytes are what `_validate_audio_upload` checks."""
    sample_data = b"\x00" * payload_size
    riff_chunk_size = 36 + len(sample_data)
    return (
        b"RIFF"
        + struct.pack("<I", riff_chunk_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", len(sample_data))
        + sample_data
    )


def _make_user(client, *, ai_authorized: bool = True):
    """Register a user and authorize them for AI tooling so /generate
    doesn't 403 us out."""
    import uuid

    from database import SessionLocal, User

    username = f"genuser_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    token = res.json()["token"]

    if ai_authorized:
        s = SessionLocal()
        try:
            u = s.query(User).filter(User.username == username).first()
            u.ai_authorized = True
            s.commit()
        finally:
            s.close()

    return username, token


def _seed_transcribed_pending(user_id: int, tenant_id: str, *, filename: str = "song.wav"):
    """Drop a transcribed_pending Job + audio file on disk, simulating a
    completed /transcribe call without actually running Whisper."""
    from database import SessionLocal
    from jobs import create_job
    from pipeline import OUTPUTS_DIR

    db = SessionLocal()
    try:
        job_id = create_job(
            db,
            artist="Intoxicados",
            style="oscuro",
            filename=filename,
            user_id=user_id,
            tenant_id=tenant_id,
            initial_status="transcribed_pending",
            song_title="No Tengo Ganas",
        )
    finally:
        db.close()

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    with open(os.path.join(job_dir, filename), "wb") as f:
        f.write(_wav_bytes(payload_size=512))

    return job_id


def test_generate_reuses_persisted_audio_when_job_id_provided(
    client, monkeypatch,
):
    """The reuse path must NOT call await file.read(). The handler should
    pull the audio from the existing transcribed_pending row, flip the
    status to queued, and enqueue with no body re-read."""
    username, token = _make_user(client)

    from database import SessionLocal, User

    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        user_id, tenant_id = u.id, u.tenant_id
    finally:
        s.close()

    job_id = _seed_transcribed_pending(user_id, tenant_id)

    captured = {}
    def _fake_enqueue(**kwargs):
        captured.update(kwargs)
        return "thread:fake"
    monkeypatch.setattr("main.enqueue_pipeline", _fake_enqueue)

    res = client.post(
        "/generate",
        data={
            "job_id": job_id,
            "artist": "Intoxicados",
            "song_title": "No Tengo Ganas",
            "style": "oscuro",
            "segments_json": json.dumps([{"start": 0, "end": 1, "text": "test"}]),
            "delivery_profile": "youtube",
        },
        headers=auth(token),
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"

    # The pipeline should have been enqueued with the same job_id and the
    # audio path that /transcribe persisted — no new upload took place.
    assert captured["job_id"] == job_id
    assert captured["mp3_path"].endswith("song.wav")
    assert captured["segments_override"] == [{"start": 0, "end": 1, "text": "test"}]


def test_generate_with_job_id_rejects_other_users_jobs(client, monkeypatch):
    """A second user trying to hijack someone else's transcribed_pending
    job_id must get 404 — not a leak of the audio."""
    username_a, _ = _make_user(client)
    username_b, token_b = _make_user(client)

    from database import SessionLocal, User

    s = SessionLocal()
    try:
        u_a = s.query(User).filter(User.username == username_a).first()
        a_user_id, a_tenant_id = u_a.id, u_a.tenant_id
    finally:
        s.close()

    job_id = _seed_transcribed_pending(a_user_id, a_tenant_id)

    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: pytest.fail("must not enqueue across owners"),
    )

    res = client.post(
        "/generate",
        data={
            "job_id": job_id,
            "artist": "Intoxicados",
            "segments_json": "[]",
            "delivery_profile": "youtube",
        },
        headers=auth(token_b),
    )
    assert res.status_code == 404


def test_generate_with_job_id_rejects_already_promoted_jobs(client, monkeypatch):
    """Once /generate has flipped a row to queued, a duplicate /generate
    for the same job_id must 409 instead of double-enqueueing."""
    username, token = _make_user(client)

    from database import SessionLocal, User
    from jobs import get_job_model

    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        user_id, tenant_id = u.id, u.tenant_id
    finally:
        s.close()

    job_id = _seed_transcribed_pending(user_id, tenant_id)

    # Manually promote the row past transcribed_pending.
    s = SessionLocal()
    try:
        row = get_job_model(s, job_id)
        row.status = "queued"
        s.commit()
    finally:
        s.close()

    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: pytest.fail("must not double-enqueue"),
    )

    res = client.post(
        "/generate",
        data={
            "job_id": job_id,
            "artist": "Intoxicados",
            "segments_json": "[]",
            "delivery_profile": "youtube",
        },
        headers=auth(token),
    )
    assert res.status_code == 409


def test_generate_legacy_path_still_accepts_full_upload(client, monkeypatch):
    """When no job_id is provided, /generate must keep working in the
    legacy mode that takes the file inline. This is the back-compat
    contract for older frontends and direct API callers."""
    _, token = _make_user(client)

    captured = {}
    def _fake_enqueue(**kwargs):
        captured.update(kwargs)
        return "thread:fake"
    monkeypatch.setattr("main.enqueue_pipeline", _fake_enqueue)

    wav = _wav_bytes(payload_size=256)
    res = client.post(
        "/generate",
        data={
            "artist": "Intoxicados",
            "song_title": "No Tengo Ganas",
            "style": "oscuro",
            "segments_json": json.dumps([{"start": 0, "end": 1, "text": "x"}]),
            "delivery_profile": "youtube",
        },
        files={"file": ("legacy.wav", io.BytesIO(wav), "audio/wav")},
        headers=auth(token),
    )

    assert res.status_code == 200, res.text
    body = res.json()
    # Legacy path mints a fresh job_id (not "reuse"), so it's just non-empty.
    assert body["job_id"]
    assert captured["mp3_path"].endswith("legacy.wav")
