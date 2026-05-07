"""Reaper unit tests.

The reaper marks long-running jobs as error so the operator's UI
doesn't show forever-spinning zombies (worker died mid-render, etc.).
These tests pin down the threshold semantics + the "don't touch
healthy or terminal jobs" guarantees.

We seed Job rows directly via SQLAlchemy and call the reaper helpers
in-process — no RQ, no FastAPI app, no real time.
"""

import uuid
from datetime import datetime, timedelta, timezone

from database import Job, SessionLocal
from reaper import find_stuck_jobs, reap_all_stuck


def _seed(db, *, status: str, age_minutes: float, job_id: str | None = None):
    """Insert a Job row at a synthetic age."""
    jid = job_id or f"reap_{uuid.uuid4().hex[:8]}"
    db.add(Job(
        job_id=jid,
        user_id=1,
        tenant_id="tenant_reap_test",
        artist="Test",
        filename="x.mp3",
        style="oscuro",
        status=status,
        progress=20,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    ))
    db.commit()
    return jid


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == "tenant_reap_test").delete()
    db.commit()


def test_recent_processing_job_is_left_alone():
    """A job that's only been in processing for 30 min is not a zombie."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=30)
        stuck = find_stuck_jobs(db, threshold_min=100)
        assert all(j.job_id != jid for j in stuck), (
            "30-min-old job should not be considered stuck at threshold=100"
        )
    finally:
        _cleanup(db)
        db.close()


def test_old_processing_job_is_reaped_with_clear_message():
    """A 110-min-old job in processing → reaper flips to error with
    operator-readable Spanish message."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=110)
        n = reap_all_stuck(threshold_min=100)
        assert n >= 1, "reaper should have killed at least the seeded job"

        row = db.query(Job).filter(Job.job_id == jid).first()
        # SQLAlchemy may have cached the pre-reap state in this session;
        # explicitly refresh.
        db.refresh(row)
        assert row.status == "error", f"expected 'error', got {row.status!r}"
        assert row.error and "abandonó" in row.error.lower(), (
            f"expected reaper reason in error field, got {row.error!r}"
        )
        assert row.completed_at is not None, "completed_at should be stamped"
    finally:
        _cleanup(db)
        db.close()


def test_terminal_jobs_are_never_touched():
    """Done / pending_review / error rows are not zombies even at any age."""
    db = SessionLocal()
    try:
        _cleanup(db)
        done_id = _seed(db, status="done", age_minutes=9999)
        review_id = _seed(db, status="pending_review", age_minutes=9999)
        err_id = _seed(db, status="error", age_minutes=9999)

        stuck = find_stuck_jobs(db, threshold_min=10)
        stuck_ids = {j.job_id for j in stuck}
        assert done_id not in stuck_ids
        assert review_id not in stuck_ids
        assert err_id not in stuck_ids
    finally:
        _cleanup(db)
        db.close()
