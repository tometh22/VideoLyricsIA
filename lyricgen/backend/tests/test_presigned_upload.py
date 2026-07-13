"""Tests for the presigned-R2 upload flow.

Covers:
  - /upload-url: single-PUT vs multipart routing based on size
  - /upload-multipart-init / part-url / complete: lifecycle, idempotency,
    cross-user / wrong-state rejections
  - /upload-multipart-abort: idempotent cleanup
  - /transcribe-uploaded: downloads from R2 + promotes the row, refuses
    cross-user / wrong-state / multipart-incomplete
  - Legacy /upload and /transcribe still serve requests but include the
    Deprecation + Sunset response headers so monitoring can find callers
"""

import io
import struct
import pytest

from tests.conftest import auth


def _wav_bytes(payload_size: int = 1024) -> bytes:
    sample_data = b"\x00" * payload_size
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(sample_data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", len(sample_data))
        + sample_data
    )


def _make_user(client, *, ai_authorized: bool = True):
    """Register + authorize a user. Returns (username, token, user_id, tenant_id)."""
    import uuid
    from database import SessionLocal, User

    username = f"upuser_{uuid.uuid4().hex[:6]}"
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
        if ai_authorized:
            u.ai_authorized = True
        s.commit()
        return username, token, u.id, u.tenant_id
    finally:
        s.close()


# ---------------------------------------------------------------------------
# /upload-url
# ---------------------------------------------------------------------------


def test_upload_url_single_put_for_small_file(client, monkeypatch):
    """Body under the multipart threshold gets a single-PUT URL."""
    import main
    _, token, _, tenant_id = _make_user(client)

    # Force R2 enabled-ness without standing up the real client.
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(
        "main.storage.presign_put_url",
        lambda tenant, jid, fn, content_type=None, expiry_seconds=900: {
            "url": f"https://r2.fake/{tenant}/{jid}/{fn}",
            "key": f"inputs/{tenant}/{jid}/{fn}",
            "expires_in": expiry_seconds,
        },
    )

    res = client.post(
        "/upload-url",
        json={
            "filename": "song.wav",
            "content_type": "audio/wav",
            "size_bytes": 5 * 1024 * 1024,  # 5 MB → single-PUT
            "artist": "Intoxicados",
            "title": "No Tengo Ganas",
        },
        headers=auth(token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["use_multipart"] is False
    assert body["upload_url"].startswith("https://r2.fake/")
    assert body["job_id"]
    assert body["key"].startswith(f"inputs/{tenant_id}/")
    assert body["key"].endswith("song.wav")


def test_upload_url_multipart_for_large_file(client, monkeypatch):
    """Body above the multipart threshold gets use_multipart=true and no
    single-PUT URL — the browser must call /upload-multipart-init."""
    import main
    _, token, _, _ = _make_user(client)

    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 16 * 1024 * 1024)

    res = client.post(
        "/upload-url",
        json={
            "filename": "lossless.wav",
            "content_type": "audio/wav",
            "size_bytes": 60 * 1024 * 1024,  # 60 MB
        },
        headers=auth(token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["use_multipart"] is True
    assert body["upload_url"] is None
    assert body["part_size"] > 0


def test_upload_url_413_when_too_large(client, monkeypatch):
    """size_bytes above MAX_UPLOAD_MB rejects without minting a job."""
    import main
    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)

    res = client.post(
        "/upload-url",
        json={
            "filename": "huge.wav",
            "content_type": "audio/wav",
            "size_bytes": (main.MAX_UPLOAD_MB + 1) * 1024 * 1024,
        },
        headers=auth(token),
    )
    assert res.status_code == 413


def test_upload_url_400_for_bad_extension(client, monkeypatch):
    """Non-MP3/WAV filename is rejected before anything else fires."""
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    _, token, _, _ = _make_user(client)
    res = client.post(
        "/upload-url",
        json={"filename": "track.flac", "size_bytes": 1024},
        headers=auth(token),
    )
    assert res.status_code == 400


def test_upload_url_503_when_storage_disabled(client, monkeypatch):
    """No R2 configured → can't presign; surface a clear 503 instead of
    silently falling back to the legacy multipart-form path."""
    monkeypatch.setattr("main.storage.is_enabled", lambda: False)
    _, token, _, _ = _make_user(client)
    res = client.post(
        "/upload-url",
        json={"filename": "song.wav", "size_bytes": 1024},
        headers=auth(token),
    )
    assert res.status_code == 503


# ---------------------------------------------------------------------------
# Multipart lifecycle
# ---------------------------------------------------------------------------


def test_multipart_init_is_idempotent(client, monkeypatch):
    """Calling /upload-multipart-init twice returns the same upload_id —
    a flaky retry must not leave a parallel multipart upload orphaned in
    R2 (each one accrues storage cost until aborted)."""
    import main
    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)

    # First create the awaiting_upload row.
    ticket = client.post(
        "/upload-url",
        json={"filename": "x.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()
    job_id = ticket["job_id"]

    init_count = {"n": 0}

    def _fake_init(tenant, jid, fn, content_type=None):
        init_count["n"] += 1
        return {"upload_id": "UP123", "key": f"inputs/{tenant}/{jid}/{fn}"}

    monkeypatch.setattr("main.storage.multipart_init", _fake_init)

    r1 = client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "x.wav"},
        headers=auth(token),
    )
    r2 = client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "x.wav"},
        headers=auth(token),
    )
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["upload_id"] == r2.json()["upload_id"] == "UP123"
    # storage.multipart_init was called exactly once — second call short-
    # circuited on the existing upload_id.
    assert init_count["n"] == 1


def test_multipart_part_url_rejects_cross_user(client, monkeypatch):
    """User B cannot mint a part URL against User A's upload_id —
    otherwise B could overwrite A's upload mid-flight."""
    import main
    _, token_a, _, _ = _make_user(client)
    _, token_b, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token_a),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token_a),
    )

    res = client.post(
        "/upload-multipart-part-url",
        json={"job_id": job_id, "part_number": 1},
        headers=auth(token_b),
    )
    assert res.status_code == 404


def test_multipart_complete_clears_upload_id(client, monkeypatch):
    """After /upload-multipart-complete, the row's multipart_upload_id is
    cleared but the input_r2_key stays — the upload is durable, only the
    in-flight handle is gone."""
    import main
    from database import SessionLocal
    from jobs import get_job_model

    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )
    monkeypatch.setattr(
        "main.storage.multipart_complete",
        lambda key, upload_id, parts: key,
    )

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )

    res = client.post(
        "/upload-multipart-complete",
        json={
            "job_id": job_id,
            "parts": [{"part_number": 1, "etag": "abc123"}],
        },
        headers=auth(token),
    )
    assert res.status_code == 200

    s = SessionLocal()
    try:
        row = get_job_model(s, job_id)
        assert row.multipart_upload_id is None
        assert row.input_r2_key  # still set
    finally:
        s.close()


def test_multipart_complete_recovers_when_object_already_present(client, monkeypatch):
    """A concurrent / duplicate complete gets NoSuchUpload from R2
    (multipart_complete → None) but the object is durably stored: the
    endpoint HEADs the key, sees the bytes, and answers 200 instead of a
    spurious error. Prod 2026-07-06 (UMG .wav double-submit)."""
    import main
    from database import SessionLocal
    from jobs import get_job_model

    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )
    # R2 says the multipart upload no longer exists (NoSuchUpload swallowed
    # to None), but the stitched object IS present.
    monkeypatch.setattr(
        "main.storage.multipart_complete", lambda key, upload_id, parts: None,
    )
    monkeypatch.setattr("main.storage.head_object_size", lambda key: 42 * 1024 * 1024)

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )

    res = client.post(
        "/upload-multipart-complete",
        json={"job_id": job_id, "parts": [{"part_number": 1, "etag": "abc123"}]},
        headers=auth(token),
    )
    assert res.status_code == 200

    s = SessionLocal()
    try:
        row = get_job_model(s, job_id)
        assert row.multipart_upload_id is None  # handle cleared
        assert row.input_r2_key  # durable object stays
    finally:
        s.close()


def test_multipart_complete_dead_upload_clears_id_and_asks_reupload(client, monkeypatch):
    """When multipart_complete fails AND no object landed, the upload_id is
    permanently gone (R2 abort / stale sweep). The endpoint drops the dead
    handle so the row doesn't wedge in awaiting_upload and returns 409 —
    retrying complete is futile, the client must re-upload."""
    import main
    from database import SessionLocal
    from jobs import get_job_model

    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )
    monkeypatch.setattr(
        "main.storage.multipart_complete", lambda key, upload_id, parts: None,
    )
    # No object at the key — the multipart upload is genuinely gone.
    monkeypatch.setattr("main.storage.head_object_size", lambda key: None)

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )

    res = client.post(
        "/upload-multipart-complete",
        json={"job_id": job_id, "parts": [{"part_number": 1, "etag": "abc123"}]},
        headers=auth(token),
    )
    assert res.status_code == 409

    s = SessionLocal()
    try:
        row = get_job_model(s, job_id)
        assert row.multipart_upload_id is None  # dead handle dropped
    finally:
        s.close()


def test_multipart_complete_failure_logs_warning_not_error(monkeypatch, caplog):
    """When R2 rejects complete_multipart_upload, storage.multipart_complete
    swallows it and returns None — but must log at WARNING, not ERROR.

    The caller reconciles via head_object_size (a durable object → idempotent
    200), so a NoSuchUpload on a double-submit is a NON-event for the user.
    Logging ERROR made Sentry's logging integration capture it as an issue
    that kept regressing on every recovered UMG upload. This pins the level
    so the noise can't creep back in."""
    import logging
    import storage

    class _FakeClient:
        def complete_multipart_upload(self, **kwargs):
            raise Exception(
                "An error occurred (NoSuchUpload) when calling the "
                "CompleteMultipartUpload operation: The specified multipart "
                "upload does not exist."
            )

    monkeypatch.setattr(storage, "R2_BUCKET", "b")
    monkeypatch.setattr(storage, "_get_client", lambda: _FakeClient())

    with caplog.at_level(logging.DEBUG, logger="genly.storage"):
        result = storage.multipart_complete(
            "inputs/t/j/z.wav", "UPID",
            [{"PartNumber": 1, "ETag": '"abc"'}],
        )

    assert result is None
    recs = [r for r in caplog.records if "multipart_complete failed" in r.message]
    assert recs, "expected a multipart_complete failure log record"
    assert all(r.levelno == logging.WARNING for r in recs), (
        "multipart_complete swallow must log WARNING (below Sentry's ERROR "
        "capture), not ERROR"
    )


def test_multipart_abort_is_idempotent(client, monkeypatch):
    """Two consecutive aborts return 200 and don't error — the abort
    button should be safe to mash."""
    import main
    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )
    monkeypatch.setattr(
        "main.storage.multipart_abort", lambda *a, **k: True,
    )

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )

    r1 = client.post("/upload-multipart-abort",
                     json={"job_id": job_id}, headers=auth(token))
    r2 = client.post("/upload-multipart-abort",
                     json={"job_id": job_id}, headers=auth(token))
    assert r1.status_code == 200
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# /transcribe-uploaded
# ---------------------------------------------------------------------------


def test_transcribe_uploaded_promotes_status_and_calls_pipeline(
    client, monkeypatch, tmp_path,
):
    """Happy path: row is awaiting_upload → transcribed_pending after
    download + Whisper. The legacy transcription core is exercised via
    a stubbed `_run_transcription_for_job`."""
    import main
    from database import SessionLocal
    from jobs import get_job_model

    _, token, _, _ = _make_user(client)

    # Pre-seed an awaiting_upload row + R2 key.
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(
        "main.storage.presign_put_url",
        lambda *a, **k: {"url": "https://x", "key": "inputs/k/j/song.wav", "expires_in": 900},
    )
    job_id = client.post(
        "/upload-url",
        json={"filename": "song.wav", "size_bytes": 1024},
        headers=auth(token),
    ).json()["job_id"]

    # Pretend R2 download succeeds by writing the local file the handler
    # expects to materialize from R2.
    from pipeline import OUTPUTS_DIR
    import os as _os
    _os.makedirs(_os.path.join(OUTPUTS_DIR, job_id), exist_ok=True)
    with open(_os.path.join(OUTPUTS_DIR, job_id, "song.wav"), "wb") as f:
        f.write(_wav_bytes(2048))

    async def _fake_transcription(*args, **kwargs):
        return {
            "job_id": kwargs.get("job_id") or args[3] if len(args) > 3 else None,
            "segments": [{"start": 0, "end": 1, "text": "hi"}],
            "reference_lyrics": "",
        }
    monkeypatch.setattr(main, "_run_transcription_for_job", _fake_transcription)

    res = client.post(
        "/transcribe-uploaded",
        json={"job_id": job_id, "language": "es"},
        headers=auth(token),
    )
    assert res.status_code == 200, res.text
    s = SessionLocal()
    try:
        assert get_job_model(s, job_id).status == "transcribed_pending"
    finally:
        s.close()


def test_transcribe_uploaded_rejects_cross_user(client, monkeypatch):
    """User B can't trigger transcription on User A's upload."""
    import main
    _, token_a, _, _ = _make_user(client)
    _, token_b, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(
        "main.storage.presign_put_url",
        lambda *a, **k: {"url": "x", "key": "inputs/x/y/z.wav", "expires_in": 900},
    )
    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 1024},
        headers=auth(token_a),
    ).json()["job_id"]

    res = client.post(
        "/transcribe-uploaded",
        json={"job_id": job_id},
        headers=auth(token_b),
    )
    assert res.status_code == 404


def test_transcribe_uploaded_409_when_multipart_incomplete(client, monkeypatch):
    """Calling transcribe before /upload-multipart-complete must 409 —
    R2 doesn't have the assembled object yet."""
    import main
    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )
    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )
    # multipart_upload_id is set but no /upload-multipart-complete called.
    res = client.post(
        "/transcribe-uploaded",
        json={"job_id": job_id},
        headers=auth(token),
    )
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# Legacy endpoint deprecation
# ---------------------------------------------------------------------------


def test_generate_accepts_awaiting_upload_for_direct_path(client, monkeypatch):
    """processQueueDirect calls /generate with the job_id from /upload-url
    (status=awaiting_upload) and segments_json='[]'. The worker runs
    Whisper itself. /generate must accept this; the original reuse path
    only allowed transcribed_pending."""
    import main
    from database import SessionLocal
    from jobs import get_job_model

    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(
        "main.storage.presign_put_url",
        lambda *a, **k: {"url": "x", "key": "inputs/x/y/z.wav", "expires_in": 900},
    )
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "thread:fake")
    monkeypatch.setattr(main, "_enforce_disk_capacity", lambda: None)
    monkeypatch.setattr(main, "_enforce_memory_pressure", lambda: None)

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 1024},
        headers=auth(token),
    ).json()["job_id"]

    res = client.post(
        "/generate",
        data={
            "job_id": job_id,
            "artist": "X",
            "style": "oscuro",
            "segments_json": "[]",
            "delivery_profile": "youtube",
        },
        headers=auth(token),
    )
    assert res.status_code == 200, res.text

    s = SessionLocal()
    try:
        assert get_job_model(s, job_id).status == "queued"
    finally:
        s.close()


def test_generate_rejects_awaiting_upload_with_incomplete_multipart(client, monkeypatch):
    """Same as above but the multipart upload is still in flight (no
    /upload-multipart-complete). Must 409 — the worker can't fetch a
    half-uploaded object."""
    import main
    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )

    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 60 * 1024 * 1024},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )

    res = client.post(
        "/generate",
        data={
            "job_id": job_id,
            "artist": "X",
            "style": "oscuro",
            "segments_json": "[]",
            "delivery_profile": "youtube",
        },
        headers=auth(token),
    )
    assert res.status_code == 409


def test_legacy_upload_emits_deprecation_headers(client, monkeypatch):
    """The legacy multipart-form /upload still works for direct API
    callers but flags itself with Deprecation + Sunset headers so we can
    monitor remaining usage before removal."""
    import main
    _, token, _, _ = _make_user(client)
    # Stub enqueue + R2 so the test doesn't hit infra.
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "thread:fake")
    monkeypatch.setattr("main.storage.is_enabled", lambda: False)
    monkeypatch.setattr(main, "_enforce_disk_capacity", lambda: None)
    monkeypatch.setattr(main, "_enforce_memory_pressure", lambda: None)

    wav = _wav_bytes(1024)
    res = client.post(
        "/upload",
        data={"artist": "X", "style": "oscuro"},
        files={"file": ("legacy.wav", io.BytesIO(wav), "audio/wav")},
        headers=auth(token),
    )
    assert res.status_code == 200, res.text
    assert res.headers.get("Deprecation") == "true"
    assert "Sunset" in res.headers
    assert "successor-version" in res.headers.get("Link", "")


# ---------------------------------------------------------------------------
# Hardening post-150MB (2026-07-02): complete que miente, tamaño real, ge=0
# ---------------------------------------------------------------------------


def _mk_multipart_job(client, monkeypatch, token):
    """upload-url (declared small) + init with multipart forced on."""
    import main
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(main, "_MULTIPART_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        "main.storage.multipart_init",
        lambda *a, **k: {"upload_id": "UP", "key": "inputs/x/y/z.wav"},
    )
    job_id = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": 2048},
        headers=auth(token),
    ).json()["job_id"]
    client.post(
        "/upload-multipart-init",
        json={"job_id": job_id, "filename": "z.wav"},
        headers=auth(token),
    )
    return job_id


def test_multipart_complete_413_when_object_oversized(client, monkeypatch):
    """The 413 in /upload-url trusts the declared size; the presigned
    parts don't constrain the body. After complete, HEAD the real object
    and reject + delete anything over MAX_UPLOAD_MB."""
    import main
    from database import SessionLocal
    from jobs import get_job_model

    _, token, _, _ = _make_user(client)
    job_id = _mk_multipart_job(client, monkeypatch, token)
    monkeypatch.setattr(
        "main.storage.multipart_complete", lambda key, upload_id, parts: key,
    )
    monkeypatch.setattr(main, "MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(
        "main.storage.head_object_size", lambda key: 2 * 1024 * 1024,
    )
    deleted = []
    monkeypatch.setattr(
        "main.storage.delete_object", lambda key: deleted.append(key),
    )

    res = client.post(
        "/upload-multipart-complete",
        json={"job_id": job_id, "parts": [{"part_number": 1, "etag": "abc"}]},
        headers=auth(token),
    )
    assert res.status_code == 413
    assert deleted == ["inputs/x/y/z.wav"]

    s = SessionLocal()
    try:
        # upload handle cleared: nothing left in-flight to abort.
        assert get_job_model(s, job_id).multipart_upload_id is None
    finally:
        s.close()


def test_multipart_complete_head_unavailable_still_succeeds(client, monkeypatch):
    """HEAD failing (None) must not block a legit upload — the size gate
    is defense-in-depth, not a new hard dependency on R2 HEAD."""
    _, token, _, _ = _make_user(client)
    job_id = _mk_multipart_job(client, monkeypatch, token)
    monkeypatch.setattr(
        "main.storage.multipart_complete", lambda key, upload_id, parts: key,
    )
    monkeypatch.setattr("main.storage.head_object_size", lambda key: None)

    res = client.post(
        "/upload-multipart-complete",
        json={"job_id": job_id, "parts": [{"part_number": 1, "etag": "abc"}]},
        headers=auth(token),
    )
    assert res.status_code == 200


def test_transcribe_uploaded_413_when_object_oversized(client, monkeypatch):
    """Single-PUT never passes through /upload-multipart-complete, so the
    real-size gate must also run before downloading in /transcribe-uploaded
    (a client that under-declared size_bytes could otherwise make the API
    materialize an arbitrarily large object on local disk)."""
    import main

    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(
        "main.storage.presign_put_url",
        lambda *a, **k: {"url": "https://x", "key": "inputs/x/y/big.wav", "expires_in": 900},
    )
    job_id = client.post(
        "/upload-url",
        json={"filename": "big.wav", "size_bytes": 1024},
        headers=auth(token),
    ).json()["job_id"]

    monkeypatch.setattr(main, "MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(
        "main.storage.head_object_size", lambda key: 2 * 1024 * 1024,
    )
    downloads = []
    monkeypatch.setattr(
        "main.storage.download_object",
        lambda key, dest: downloads.append(key) or False,
    )

    res = client.post(
        "/transcribe-uploaded",
        json={"job_id": job_id},
        headers=auth(token),
    )
    assert res.status_code == 413
    assert downloads == []  # rejected before pulling a single byte


def test_upload_url_422_when_negative_size(client, monkeypatch):
    """size_bytes is client-declared; negative values used to sail through
    every gate (and log '-0.5 MB'). Pydantic ge=0 rejects at the edge."""
    _, token, _, _ = _make_user(client)
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    res = client.post(
        "/upload-url",
        json={"filename": "z.wav", "size_bytes": -1},
        headers=auth(token),
    )
    assert res.status_code == 422
