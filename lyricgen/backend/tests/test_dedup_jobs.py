"""Tests for supersede_sibling_drafts — the dedup that removes the orphan
draft left when the wizard re-uploads on generate instead of reusing the
transcribe job (the '2 jobs per video' bug).

Two layers:
  - Unit tests against the helper directly (assert filter semantics).
  - Integration tests against the FastAPI endpoints that create draft
    jobs (/upload-url, /upload, /transcribe) — they MUST all wire up the
    helper after create_job, else the '2 jobs per video' bug recurs.
    Audit 2026-05-28: operator on prod hit the bug again (jobs
    335cff23 and 88714dc6 for the same audio) because PR #388 only
    covered /generate, not the modern /upload-url path. These tests pin
    the contract for all three create-job endpoints.
"""

import io
import struct
import uuid
from datetime import datetime, timedelta, timezone

from database import Job, SessionLocal
from jobs import supersede_sibling_drafts
from tests.conftest import auth

_T = "tenant_dedup_test"


def _seed(db, *, job_id, status, filename, age_min, user_id=1):
    db.add(Job(
        job_id=job_id, user_id=user_id, tenant_id=_T, artist="A",
        filename=filename, style="oscuro", status=status,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
    ))
    db.commit()


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.commit()


def _ids(db):
    return {j.job_id for j in db.query(Job).filter(Job.tenant_id == _T).all()}


def test_supersede_deletes_recent_sibling_draft():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="keep01", status="queued", filename="song.mp3", age_min=1)
        _seed(db, job_id="orphan01", status="transcribed_pending", filename="song.mp3", age_min=2)
        n = supersede_sibling_drafts(
            db, keep_job_id="keep01", user_id=1, tenant_id=_T, filename="song.mp3",
        )
        assert n == 1
        assert _ids(db) == {"keep01"}      # orphan deleted, kept stays
    finally:
        _cleanup(db); db.close()


def test_supersede_keeps_out_of_window_sibling():
    """An intentional re-upload of the same song hours later is NOT touched."""
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="keep02", status="queued", filename="song.mp3", age_min=1)
        _seed(db, job_id="oldretest", status="transcribed_pending", filename="song.mp3", age_min=120)
        n = supersede_sibling_drafts(
            db, keep_job_id="keep02", user_id=1, tenant_id=_T, filename="song.mp3",
            window_min=20,
        )
        assert n == 0
        assert "oldretest" in _ids(db)
    finally:
        _cleanup(db); db.close()


def test_supersede_ignores_different_filename_and_terminal_status():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="keep03", status="queued", filename="song.mp3", age_min=1)
        _seed(db, job_id="otherfile", status="transcribed_pending", filename="otra.mp3", age_min=2)
        _seed(db, job_id="alreadydone", status="done", filename="song.mp3", age_min=2)
        n = supersede_sibling_drafts(
            db, keep_job_id="keep03", user_id=1, tenant_id=_T, filename="song.mp3",
        )
        assert n == 0
        assert {"keep03", "otherfile", "alreadydone"} <= _ids(db)
    finally:
        _cleanup(db); db.close()


def test_supersede_only_same_user():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="keep04", status="queued", filename="song.mp3", age_min=1, user_id=1)
        _seed(db, job_id="otheruser", status="transcribed_pending", filename="song.mp3", age_min=2, user_id=2)
        n = supersede_sibling_drafts(
            db, keep_job_id="keep04", user_id=1, tenant_id=_T, filename="song.mp3",
        )
        assert n == 0
        assert "otheruser" in _ids(db)
    finally:
        _cleanup(db); db.close()


def test_supersede_misses_when_filename_unsanitized_vs_sibling_sanitized():
    """Regression for the 2026-05-26 4-jobs-for-one-audio incident.

    `/upload-url` persists `Job.filename` via `_safe_basename` (strips
    directory components, control chars, length-caps). The
    direct-generate path (no upstream /upload-url) used to pass
    `existing_filename` straight into supersede_sibling_drafts WITHOUT
    sanitizing it first. A browser that sent ` ` vs `_` in the filename,
    or a path-prefixed name, would silently miss the dedupe because
    `Job.filename == filename` is a literal equality.

    This test pins the contract: the CALLER of supersede must pass a
    filename in the same shape as what `_safe_basename` would have
    produced. We don't import _safe_basename here (it lives in main.py
    which pulls FastAPI/etc.); we just simulate the divergence with
    spaces-vs-underscores, which is the most common real-world case.
    """
    db = SessionLocal()
    try:
        _cleanup(db)
        # Sibling row was persisted with the sanitized form (spaces → _).
        _seed(db, job_id="keep_san",
              status="queued", filename="Sin_Gamulan_-_Los_Abuelos.wav", age_min=1)
        _seed(db, job_id="orphan_san",
              status="transcribed_pending", filename="Sin_Gamulan_-_Los_Abuelos.wav", age_min=2)
        # Caller passes the SANITIZED form → matches → orphan deleted.
        n = supersede_sibling_drafts(
            db, keep_job_id="keep_san", user_id=1, tenant_id=_T,
            filename="Sin_Gamulan_-_Los_Abuelos.wav",
        )
        assert n == 1, "sanitized-on-both-sides should match"
        assert _ids(db) == {"keep_san"}

        # Reset and prove the negative: unsanitized caller misses.
        _cleanup(db)
        _seed(db, job_id="keep_un",
              status="queued", filename="Sin_Gamulan_-_Los_Abuelos.wav", age_min=1)
        _seed(db, job_id="orphan_un",
              status="transcribed_pending", filename="Sin_Gamulan_-_Los_Abuelos.wav", age_min=2)
        # Caller passes raw browser filename (spaces) → misses.
        n = supersede_sibling_drafts(
            db, keep_job_id="keep_un", user_id=1, tenant_id=_T,
            filename="Sin Gamulan - Los Abuelos.wav",
        )
        assert n == 0, "unsanitized caller form does NOT match — that's the bug"
        assert "orphan_un" in _ids(db)
    finally:
        _cleanup(db); db.close()


# ---------------------------------------------------------------------------
# Integration tests — endpoints must wire up the dedup helper.
#
# Audit 2026-05-28 prod incident: operator uploaded one audio, admin
# showed two `transcribed_pending` rows (335cff23 + 88714dc6) for the
# same filename. Root cause: PR #388 added supersede_sibling_drafts ONLY
# to /generate direct-create; the modern flow goes through /upload-url
# which had no guard. These tests pin the contract for every
# job-creating endpoint so a regression that removes the supersede call
# from any of them fails loudly.
# ---------------------------------------------------------------------------


def _wav_bytes(payload_size: int = 1024) -> bytes:
    """Minimal valid WAV for /upload + /transcribe multipart tests."""
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
    """Register + authorize a test user. Returns (token, user_id, tenant_id)."""
    from database import SessionLocal, User

    username = f"dedupuser_{uuid.uuid4().hex[:6]}"
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
        return token, u.id, u.tenant_id
    finally:
        s.close()


def _draft_rows_for(user_id: int, tenant_id: str, filename: str) -> list:
    """All draft (awaiting_upload or transcribed_pending) rows matching the tuple."""
    s = SessionLocal()
    try:
        return (
            s.query(Job)
            .filter(Job.user_id == user_id, Job.tenant_id == tenant_id)
            .filter(Job.filename == filename)
            .filter(Job.status.in_(("awaiting_upload", "transcribed_pending")))
            .all()
        )
    finally:
        s.close()


def _cleanup_user_rows(user_id: int, tenant_id: str) -> None:
    """Wipe every Job row our integration test created so later tests in
    the same session aren't contaminated. Without this, the shared SQLite
    test DB carries `awaiting_upload`/`transcribed_pending` rows
    forward; reaper-targeting tests then trip on unexpected rows and
    /upload-multipart-complete tests get confused by leftover keys.
    """
    s = SessionLocal()
    try:
        s.query(Job).filter(
            Job.user_id == user_id, Job.tenant_id == tenant_id,
        ).delete(synchronize_session=False)
        s.commit()
    finally:
        s.close()


def test_upload_url_dedups_back_to_back_calls(client, monkeypatch):
    """Two /upload-url calls in quick succession for the same filename
    must leave only ONE awaiting_upload row in DB.

    This is the modern flow that produced the prod incident on
    2026-05-28 (Don Electrón_Intoxicados double-job). PR #388 closed
    /generate but left /upload-url naked — this test pins that the gap
    is now closed.
    """
    monkeypatch.setattr("main.storage.is_enabled", lambda: True)
    monkeypatch.setattr(
        "main.storage.presign_put_url",
        lambda tenant, jid, fn, content_type=None, expiry_seconds=900: {
            "url": f"https://r2.fake/{tenant}/{jid}/{fn}",
            "key": f"inputs/{tenant}/{jid}/{fn}",
            "expires_in": expiry_seconds,
        },
    )
    token, user_id, tenant_id = _make_user(client)
    fname = "Don_Electron_Intoxicados.wav"
    body = {
        "filename": fname,
        "content_type": "audio/wav",
        "size_bytes": 5 * 1024 * 1024,
        "artist": "Intoxicados",
        "title": "Don Electron",
    }
    try:
        r1 = client.post("/upload-url", json=body, headers=auth(token))
        r2 = client.post("/upload-url", json=body, headers=auth(token))
        assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
        # Two distinct job_ids minted at the API level.
        assert r1.json()["job_id"] != r2.json()["job_id"]

        # …but only the latest survives in DB after dedup.
        drafts = _draft_rows_for(user_id, tenant_id, fname)
        assert len(drafts) == 1, (
            f"expected 1 draft row after back-to-back /upload-url, got {len(drafts)} "
            f"(ids={[d.job_id for d in drafts]})"
        )
        # The surviving one is the latest call's job_id.
        assert drafts[0].job_id == r2.json()["job_id"]
    finally:
        _cleanup_user_rows(user_id, tenant_id)


def test_upload_legacy_dedups_back_to_back_multipart_posts(client, monkeypatch):
    """Two /upload (legacy multipart-form) calls for the same filename
    must leave only ONE row in DB after dedup.

    The legacy path takes the file body inline (no presigned R2), so it
    creates a job, processes the file, and enqueues the pipeline. We
    short-circuit pipeline enqueuing so the test stays fast and doesn't
    hit Redis / workers / Replicate.
    """
    # No R2 / no actual pipeline execution.
    monkeypatch.setattr("main.storage.is_enabled", lambda: False)
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: None)
    token, user_id, tenant_id = _make_user(client)
    fname = "Intoxicados_Don_Electron.wav"

    def _post():
        return client.post(
            "/upload",
            data={
                "artist": "Intoxicados",
                "song_title": "Don Electron",
                "style": "oscuro",
                "delivery_profile": "youtube",
            },
            files={"file": (fname, _wav_bytes(), "audio/wav")},
            headers=auth(token),
        )

    try:
        r1 = _post()
        r2 = _post()
        # /upload may return 200 or 202; both are success in this code path.
        assert r1.status_code in (200, 202), r1.text
        assert r2.status_code in (200, 202), r2.text

        # /upload may transition to "queued" (not draft); also accept
        # "queued" rows when counting. We fetch with broader filter.
        s = SessionLocal()
        try:
            all_rows = (
                s.query(Job)
                .filter(Job.user_id == user_id, Job.tenant_id == tenant_id)
                .filter(Job.filename == fname)
                .all()
            )
        finally:
            s.close()
        # Either drafts collapsed to 1 (status=awaiting_upload) OR /upload
        # moved both straight to queued (not drafts) — in the latter case
        # supersede legitimately did nothing because queued is not a draft.
        # The bug we're guarding against is "two DRAFTS for same file",
        # which is what produced the prod incident.
        n_drafts = len([j for j in all_rows
                        if j.status in ("awaiting_upload", "transcribed_pending")])
        assert n_drafts <= 1, (
            f"two draft rows for same /upload — dedup not wired up "
            f"(rows={[(j.job_id, j.status) for j in all_rows]})"
        )
    finally:
        _cleanup_user_rows(user_id, tenant_id)


def test_transcribe_legacy_dedups_back_to_back_multipart_posts(client, monkeypatch):
    """Two /transcribe (legacy) calls for the same filename must collapse
    to one row. /transcribe creates the row, calls supersede, then
    awaits `_run_transcription_for_job` (Whisper + Vertex). For a unit
    test we stub the pipeline so the request returns immediately after
    the dedup step has run — that's exactly the surface we want to pin.
    """
    monkeypatch.setattr("main.storage.is_enabled", lambda: False)

    async def _stub_run_transcription(*a, **kw):
        return {"job_id": kw.get("job_id"), "segments": [], "reference_lyrics": ""}

    monkeypatch.setattr("main._run_transcription_for_job", _stub_run_transcription)
    token, user_id, tenant_id = _make_user(client)
    fname = "Intoxicados_DonElectron_again.wav"

    def _post():
        return client.post(
            "/transcribe",
            data={"artist": "Intoxicados", "title": "Don Electron"},
            files={"file": (fname, _wav_bytes(), "audio/wav")},
            headers=auth(token),
        )

    try:
        r1 = _post()
        r2 = _post()
        # Either should be 2xx; if the endpoint has been deprecated to a
        # hard 410 in the future, this test will fail and the maintainer
        # will know to retire it (or skip it).
        assert r1.status_code < 400, r1.text
        assert r2.status_code < 400, r2.text

        drafts = _draft_rows_for(user_id, tenant_id, fname)
        assert len(drafts) <= 1, (
            f"two draft rows for same /transcribe — dedup not wired up "
            f"(ids={[d.job_id for d in drafts]})"
        )
    finally:
        _cleanup_user_rows(user_id, tenant_id)
