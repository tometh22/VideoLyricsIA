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
