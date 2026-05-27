"""Tests for supersede_sibling_drafts — the dedup that removes the orphan
draft left when the wizard re-uploads on generate instead of reusing the
transcribe job (the '2 jobs per video' bug)."""

from datetime import datetime, timedelta, timezone

from database import Job, SessionLocal
from jobs import supersede_sibling_drafts

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
