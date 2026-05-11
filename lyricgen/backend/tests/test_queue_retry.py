"""Pipeline retry + failure-callback tests.

Fix #1 of deploy-resilience: a Railway redeploy that kills the worker
mid-render must not leave the DB row in `processing` forever. Two
layers:

  1. RQ's Retry mechanism re-enqueues the job once after the worker
     dies (covers planned redeploys where a new pod boots within a
     minute).
  2. When the retry is also lost (rare — back-to-back redeploys or a
     pathological pipeline bug), RQ calls our on_failure callback,
     which flips the DB row to `error` with a Spanish message that
     points the user at the Reintentar button.

This test file pins both layers down without spinning up real Redis or
RQ workers. The callback is the unit we care about most — it has the
operator-visible side effect.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from database import Job, SessionLocal
from queue_jobs import pipeline_failure_callback


def _seed_job(db, *, status: str = "processing", job_id: str | None = None) -> str:
    jid = job_id or f"qrtest_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=1,
        tenant_id="tenant_qr_test",
        artist="Test Artist",
        filename="x.mp3",
        style="oscuro",
        status=status,
        progress=22,
        current_step="background",
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    db.commit()
    return jid


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == "tenant_qr_test").delete()
    db.commit()


def _fake_job(job_id: str):
    """Minimal RQ-job stand-in. The callback only reads .id."""
    return SimpleNamespace(id=job_id)


class _AbandonedJobError(Exception):
    """Stand-in for rq.exceptions.AbandonedJobError so the test doesn't
    need to import RQ internals — the callback dispatches on the class
    NAME, not identity."""


def test_callback_marks_processing_job_as_error_on_worker_death():
    """The deploy-death path: RQ moves the abandoned job to failed
    after retries are exhausted, then calls on_failure. We must flip
    the DB row to error with a user-actionable Spanish message."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db)
        pipeline_failure_callback(
            _fake_job(jid), None, _AbandonedJobError, _AbandonedJobError(), None,
        )
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error", f"expected 'error', got {row.status!r}"
        assert "reintentar" in (row.error or "").lower(), (
            f"expected user-actionable retry hint, got {row.error!r}"
        )
        assert "servidor se reinici" in (row.error or "").lower(), (
            "deploy-death path should explain the cause"
        )
        assert row.completed_at is not None
    finally:
        _cleanup(db)
        db.close()


def test_callback_surfaces_real_pipeline_error_to_user():
    """A real exception inside run_pipeline (not a worker death) should
    surface a short version of the message to the user, prefixed so
    they understand it was a render failure after retries."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db)

        class _Boom(Exception):
            pass

        pipeline_failure_callback(
            _fake_job(jid),
            None,
            _Boom,
            _Boom("ffmpeg returned non-zero status: -9"),
            None,
        )
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error"
        assert "fall" in (row.error or "").lower()
        assert "ffmpeg" in (row.error or "").lower(), (
            f"expected exception message to leak into user-facing error, "
            f"got {row.error!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_callback_does_not_clobber_terminal_states():
    """If the pipeline managed to write status=done before the worker
    died on a post-success cleanup step (rare but possible — e.g. ProRes
    prewarm enqueue fails), the callback must not regress the row to
    error. The user already has their video."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db, status="done")
        pipeline_failure_callback(
            _fake_job(jid), None, RuntimeError, RuntimeError("late failure"), None,
        )
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "done", (
            f"terminal status must be preserved, got {row.status!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_callback_is_safe_when_job_row_is_missing():
    """If the DB row was already deleted by the user before the
    callback ran, the callback must not raise. RQ's failure bookkeeping
    runs after this and would be derailed by an unhandled exception."""
    pipeline_failure_callback(
        _fake_job("does_not_exist_123"),
        None,
        RuntimeError,
        RuntimeError("x"),
        None,
    )  # must not raise


def test_callback_ignores_blank_job_id():
    """Defense in depth: a malformed RQ job (no .id) must not crash."""
    pipeline_failure_callback(
        SimpleNamespace(id=""),
        None,
        RuntimeError,
        RuntimeError("x"),
        None,
    )  # must not raise


def test_enqueue_pipeline_attaches_retry_metadata():
    """End-to-end smoke: enqueue_pipeline should configure RQ's retry
    so that worker death is recoverable without operator intervention.
    Uses fakeredis to avoid needing a real Redis instance — the test
    cares about retry config, not actual job execution."""
    try:
        from fakeredis import FakeStrictRedis
    except ImportError:
        import pytest
        pytest.skip("fakeredis not installed — skipping enqueue smoke test")

    import queue_jobs
    fake = FakeStrictRedis()
    from rq import Queue
    q = Queue("default", connection=fake)

    # Monkey-patch the queue picker to return our fake queue, and stub
    # out the pipeline import so RQ can serialize the job without
    # trying to actually pull in the heavy pipeline module.
    original_pick = queue_jobs._pick_queue
    queue_jobs._pick_queue = lambda plan: q
    try:
        # Stub run_pipeline with a no-op to avoid importing the real one.
        import pipeline
        original_run = getattr(pipeline, "run_pipeline", None)
        pipeline.run_pipeline = lambda *a, **kw: None
        try:
            rq_id = queue_jobs.enqueue_pipeline(
                job_id="smoke_qr_123",
                mp3_path="/tmp/x.wav",
                artist="A",
                style="oscuro",
                plan="100",
                delivery_profile="youtube",
            )
            assert rq_id == "smoke_qr_123"
            rq_job = q.fetch_job("smoke_qr_123")
            assert rq_job is not None, "enqueued job should be retrievable"
            assert rq_job.retries_left == queue_jobs.PIPELINE_RETRY_MAX, (
                f"expected retries_left={queue_jobs.PIPELINE_RETRY_MAX}, "
                f"got {rq_job.retries_left}"
            )
            # on_failure callback wiring is harder to inspect across RQ
            # versions (sometimes stored as failure_callback, sometimes
            # as _failure_callback_name). Settle for: the attribute
            # exists in some form.
            has_callback = (
                getattr(rq_job, "failure_callback", None) is not None
                or getattr(rq_job, "_failure_callback_name", None) is not None
            )
            assert has_callback, "enqueue should attach on_failure callback"
        finally:
            if original_run is not None:
                pipeline.run_pipeline = original_run
    finally:
        queue_jobs._pick_queue = original_pick
