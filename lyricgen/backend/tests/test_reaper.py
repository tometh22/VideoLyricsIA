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

from database import AIProvenance, Job, SessionLocal
from reaper import (
    find_abandoned_edits,
    find_abandoned_transcribed,
    find_orphan_polling_jobs,
    find_stalled_renders,
    find_stuck_jobs,
    reap_all_stuck,
)


def _seed(db, *, status: str, age_minutes: float, job_id: str | None = None,
          editing_started_minutes_ago: float | None = None,
          last_progress_minutes_ago: float | None = None,
          last_user_activity_minutes_ago: float | None = None,
          edit_count: int = 0,
          progress: int = 20,
          current_step: str = "video"):
    """Insert a Job row at a synthetic age. editing_started_minutes_ago
    drives the find_abandoned_edits clock; last_progress_minutes_ago
    drives the find_stalled_renders clock. Pass None to leave the
    column unset (mirrors legacy rows / paths that never tick progress)."""
    jid = job_id or f"reap_{uuid.uuid4().hex[:8]}"
    editing_started_at = None
    if editing_started_minutes_ago is not None:
        editing_started_at = (
            datetime.now(timezone.utc)
            - timedelta(minutes=editing_started_minutes_ago)
        )
    last_progress_at = None
    if last_progress_minutes_ago is not None:
        last_progress_at = (
            datetime.now(timezone.utc)
            - timedelta(minutes=last_progress_minutes_ago)
        )
    last_user_activity_at = None
    if last_user_activity_minutes_ago is not None:
        last_user_activity_at = (
            datetime.now(timezone.utc)
            - timedelta(minutes=last_user_activity_minutes_ago)
        )
    db.add(Job(
        job_id=jid,
        user_id=1,
        tenant_id="tenant_reap_test",
        artist="Test",
        filename="x.mp3",
        style="oscuro",
        status=status,
        progress=progress,
        current_step=current_step,
        delivery_profile="youtube",
        edit_count=edit_count,
        editing_started_at=editing_started_at,
        last_progress_at=last_progress_at,
        last_user_activity_at=last_user_activity_at,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    ))
    db.commit()
    return jid


def _seed_provenance(
    db,
    *,
    job_id: str,
    age_minutes: float,
    duration_ms: int | None,
    step: str = "video_bg",
    tool_name: str = "veo-3.1-fast-generate-001",
):
    """Insert an ai_provenance row at a synthetic age. duration_ms=None
    simulates an in-flight call (call started, never returned)."""
    db.add(AIProvenance(
        job_id=job_id,
        step=step,
        tool_name=tool_name,
        tool_provider="google_vertex",
        prompt_sent="(synthetic test prompt)",
        duration_ms=duration_ms,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    ))
    db.commit()


def _cleanup(db):
    job_ids = [j.job_id for j in db.query(Job).filter(
        Job.tenant_id == "tenant_reap_test").all()]
    if job_ids:
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(job_ids)).delete(
            synchronize_session=False,
        )
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


def test_orphan_in_flight_veo_is_flagged_fast():
    """The actual deploy-death signature: a young job (25 min old, well
    under the 100-min global threshold) whose Veo provenance row is
    stale (15 min, never got duration_ms filled in). Must be flagged by
    the orphan sweep so the user sees an error inside one coffee break
    instead of two hours."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=25)
        _seed_provenance(db, job_id=jid, age_minutes=15, duration_ms=None)
        orphans = find_orphan_polling_jobs(db, threshold_min=10)
        assert any(j.job_id == jid for j in orphans), (
            "orphan sweep must catch a young job with a stale in-flight "
            "provenance row"
        )
    finally:
        _cleanup(db)
        db.close()


def test_healthy_in_flight_veo_is_left_alone():
    """A Veo call that started 2 min ago is healthy — Veo p99 is ~2 min.
    Must NOT be reaped just because duration_ms is still NULL."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=3)
        _seed_provenance(db, job_id=jid, age_minutes=2, duration_ms=None)
        orphans = find_orphan_polling_jobs(db, threshold_min=10)
        assert all(j.job_id != jid for j in orphans), (
            "a 2-min-old in-flight call is healthy, not orphaned"
        )
    finally:
        _cleanup(db)
        db.close()


def test_completed_veo_call_is_never_orphan():
    """An old provenance row with duration_ms FILLED means the call
    succeeded — even if the row itself is 99 min old. Only NULL means
    in-flight."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=99)
        _seed_provenance(db, job_id=jid, age_minutes=99, duration_ms=87_000)
        orphans = find_orphan_polling_jobs(db, threshold_min=10)
        assert all(j.job_id != jid for j in orphans), (
            "filled duration_ms means the call returned — not an orphan"
        )
    finally:
        _cleanup(db)
        db.close()


def test_orphan_in_terminal_status_is_left_alone():
    """A job that already moved on to done/error/pending_review is not a
    zombie even if a stale in-flight provenance row from an earlier
    crashed call still exists."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="done", age_minutes=120)
        _seed_provenance(db, job_id=jid, age_minutes=110, duration_ms=None)
        orphans = find_orphan_polling_jobs(db, threshold_min=10)
        assert all(j.job_id != jid for j in orphans), (
            "terminal-status jobs are out of scope for the orphan sweep"
        )
    finally:
        _cleanup(db)
        db.close()


def test_reap_all_stuck_reaps_orphans_with_user_facing_message():
    """End-to-end: orphan sweep flips the row to error with a Spanish
    operator-friendly message that mentions retry-without-re-upload."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=25)
        _seed_provenance(db, job_id=jid, age_minutes=15, duration_ms=None)

        n = reap_all_stuck(threshold_min=100)
        assert n >= 1, "reaper should have flagged the orphan"

        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error", f"expected 'error', got {row.status!r}"
        assert row.error and "reintentar" in row.error.lower(), (
            f"expected retry hint in error message, got {row.error!r}"
        )
        assert row.completed_at is not None
    finally:
        _cleanup(db)
        db.close()


def test_no_double_reap_when_job_is_both_old_and_orphan():
    """A job that's BOTH past the global age threshold AND has a stale
    in-flight row should be reaped exactly once (no duplicate audit log,
    no duplicate Sentry hit). The age-based sweep wins; orphan sweep
    skips it."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=110)
        _seed_provenance(db, job_id=jid, age_minutes=100, duration_ms=None)

        n = reap_all_stuck(threshold_min=100)
        # The exact count depends on other test data; what matters is
        # that the same row didn't get hit twice in one pass. We assert
        # the post-state is consistent and the message comes from the
        # age path ("abandonó"), not the orphan path ("se reinició"),
        # since stuck is processed first and orphans are filtered.
        assert n >= 1
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error"
        assert "abandonó" in row.error.lower(), (
            f"expected age-based message for double-hit job, got {row.error!r}"
        )
    finally:
        _cleanup(db)
        db.close()


# ───────────────────────────────────────────────────
# Abandoned-edit sweep (worker died during /edit re-render)
# ───────────────────────────────────────────────────

def test_fresh_editing_job_is_not_reverted():
    """An edit that just started (5 min ago) is healthy, not abandoned."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="editing", age_minutes=60,
            editing_started_minutes_ago=5, edit_count=1,
        )
        abandoned = find_abandoned_edits(db, threshold_min=30)
        assert all(j.job_id != jid for j in abandoned), (
            "5-min-old edit should not be abandoned at threshold=30"
        )
    finally:
        _cleanup(db)
        db.close()


def test_old_editing_job_is_reverted_to_pending_review():
    """Edit started 45 min ago and still in editing/40% → worker is
    dead. Reaper reverts to pending_review and restores edit_count so
    the user gets the failed attempt back."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="editing", age_minutes=120,
            editing_started_minutes_ago=45, edit_count=2,
            progress=40, current_step="video",
        )
        n = reap_all_stuck(threshold_min=100)
        # The age-based sweep (find_stuck_jobs) might also catch this
        # because the row is 120 min old. What we assert is the final
        # state, not the headline count.
        assert n >= 0  # may be 0 if a different status path won the race

        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "pending_review", (
            f"expected revert to pending_review, got {row.status!r}"
        )
        assert row.edit_count == 1, (
            f"edit_count should be decremented (2 → 1), got {row.edit_count}"
        )
        assert row.progress == 100, (
            f"progress should be reset to 100 (terminal), got {row.progress}"
        )
        assert row.current_step == "thumbnail", (
            f"current_step should be reset to thumbnail, got {row.current_step!r}"
        )
        assert row.editing_started_at is None, (
            "editing_started_at should be cleared so the next edit re-stamps it"
        )
        assert row.error is None, (
            f"error should be None on revert (the original render is fine), got {row.error!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_editing_without_timestamp_is_not_touched():
    """Legacy editing rows that pre-date the editing_started_at column
    (NULL value) must not be reverted — we cannot tell when the edit
    began, so we err on the side of not interfering."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="editing", age_minutes=200,
            editing_started_minutes_ago=None, edit_count=1,
        )
        abandoned = find_abandoned_edits(db, threshold_min=30)
        assert all(j.job_id != jid for j in abandoned), (
            "edit with NULL editing_started_at should be skipped (no clock)"
        )
    finally:
        _cleanup(db)
        db.close()


def test_edit_count_floor_at_zero():
    """Defensive: if a job is at edit_count=0 (corrupted state, manual
    reset) when reaped, decrementing must not produce -1."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="editing", age_minutes=120,
            editing_started_minutes_ago=60, edit_count=0,
        )
        reap_all_stuck(threshold_min=100)
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.edit_count == 0, (
            f"edit_count must not go negative, got {row.edit_count}"
        )
    finally:
        _cleanup(db)
        db.close()


# ───────────────────────────────────────────────────
# Stalled-render sweep (worker died during non-AI step)
# ───────────────────────────────────────────────────

def test_fresh_processing_job_with_recent_progress_is_left_alone():
    """A processing job whose progress was just updated (1 min ago) is
    healthy — the worker is alive and ticking."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="processing", age_minutes=10,
            last_progress_minutes_ago=1, progress=40,
        )
        stalled = find_stalled_renders(db, threshold_min=20)
        assert all(j.job_id != jid for j in stalled), (
            "a 1-min-old progress update means worker is alive"
        )
    finally:
        _cleanup(db)
        db.close()


def test_stalled_processing_job_is_reaped():
    """The exact Agus / job 2144aacb453e scenario: worker died during
    ffmpeg at video/40%, no AIProvenance in-flight, age below 100 min.
    The new sweep must catch this within the 20-min threshold."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="processing", age_minutes=30,
            last_progress_minutes_ago=25, progress=40,
            current_step="video",
        )
        n = reap_all_stuck(threshold_min=100)
        assert n >= 1, "stalled-render sweep should reap this job"

        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error", (
            f"expected status=error after stalled reap, got {row.status!r}"
        )
        assert row.error and "reinici" in row.error.lower(), (
            f"expected Spanish 'servidor se reinició' message, got {row.error!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_processing_without_progress_timestamp_is_not_touched():
    """Legacy rows that pre-date the last_progress_at column (NULL value)
    must not be reaped by find_stalled_renders — without the timestamp we
    have no clock. The age-based find_stuck_jobs still covers them at
    100 min, just slower."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="processing", age_minutes=50,
            last_progress_minutes_ago=None,  # NULL
            progress=40,
        )
        stalled = find_stalled_renders(db, threshold_min=20)
        assert all(j.job_id != jid for j in stalled), (
            "processing job with NULL last_progress_at should be skipped"
        )
    finally:
        _cleanup(db)
        db.close()


def test_stalled_sweep_only_targets_processing():
    """Editing, queued, pending_review, done — none of these are in the
    stalled-render sweep's scope. Editing has its own dedicated reaper
    (find_abandoned_edits) with different revert semantics; the rest are
    waiting on humans or have already finished."""
    db = SessionLocal()
    try:
        _cleanup(db)
        edit_jid = _seed(
            db, status="editing", age_minutes=30,
            last_progress_minutes_ago=25,
        )
        queued_jid = _seed(
            db, status="queued", age_minutes=30,
            last_progress_minutes_ago=25,
        )
        done_jid = _seed(
            db, status="done", age_minutes=30,
            last_progress_minutes_ago=25,
        )
        stalled = find_stalled_renders(db, threshold_min=20)
        stalled_ids = {j.job_id for j in stalled}
        assert edit_jid not in stalled_ids
        assert queued_jid not in stalled_ids
        assert done_jid not in stalled_ids
    finally:
        _cleanup(db)
        db.close()


# -----------------------------------------------------------------------------
# find_abandoned_transcribed: coalesce(last_user_activity_at, created_at)
# -----------------------------------------------------------------------------
# Incident 2026-05-14: a user batch-editing 5 lyrics for ~90 min got reaped at
# 30 min because the anchor was created_at. The endpoint POST /save-segments
# bumps last_user_activity_at every time the user edits, so active sessions
# stay alive past the TTL.

def test_transcribed_pending_with_recent_user_activity_is_kept():
    """Old created_at but recent last_user_activity_at → active session, keep."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db,
            status="transcribed_pending",
            age_minutes=90,                       # would be reaped on old logic
            last_user_activity_minutes_ago=5,     # user was editing 5 min ago
        )
        abandoned = find_abandoned_transcribed(db, ttl_min=30)
        assert all(j.job_id != jid for j in abandoned), (
            "transcribed_pending with recent activity must NOT be reaped"
        )
    finally:
        _cleanup(db)
        db.close()


def test_transcribed_pending_with_stale_user_activity_is_reaped():
    """Old created_at AND stale last_user_activity_at → genuinely abandoned."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db,
            status="transcribed_pending",
            age_minutes=120,
            last_user_activity_minutes_ago=60,    # last touch was an hour ago
        )
        abandoned = find_abandoned_transcribed(db, ttl_min=30)
        assert any(j.job_id == jid for j in abandoned), (
            "transcribed_pending with stale activity should be reaped"
        )
    finally:
        _cleanup(db)
        db.close()


def test_transcribed_pending_null_activity_falls_back_to_created_at():
    """Legacy rows pre-migration (NULL last_user_activity_at) must keep
    behaving the same way they did before: anchored on created_at."""
    db = SessionLocal()
    try:
        _cleanup(db)
        # Stale created_at, no activity timestamp → reaped.
        old_jid = _seed(
            db,
            status="transcribed_pending",
            age_minutes=90,
            last_user_activity_minutes_ago=None,
        )
        # Fresh created_at, no activity timestamp → kept.
        new_jid = _seed(
            db,
            status="transcribed_pending",
            age_minutes=10,
            last_user_activity_minutes_ago=None,
        )
        abandoned_ids = {j.job_id for j in find_abandoned_transcribed(db, ttl_min=30)}
        assert old_jid in abandoned_ids, "old NULL-activity row should reap"
        assert new_jid not in abandoned_ids, "fresh NULL-activity row should stay"
    finally:
        _cleanup(db)
        db.close()


def test_transcribed_pending_sweep_skips_other_statuses():
    """find_abandoned_transcribed only looks at transcribed_pending. Other
    statuses are handled by their own sweeps."""
    db = SessionLocal()
    try:
        _cleanup(db)
        processing_jid = _seed(
            db, status="processing", age_minutes=120,
        )
        editing_jid = _seed(
            db, status="editing", age_minutes=120,
        )
        done_jid = _seed(
            db, status="done", age_minutes=120,
        )
        abandoned_ids = {j.job_id for j in find_abandoned_transcribed(db, ttl_min=30)}
        assert processing_jid not in abandoned_ids
        assert editing_jid not in abandoned_ids
        assert done_jid not in abandoned_ids
    finally:
        _cleanup(db)
        db.close()


# ---------------------------------------------------------------------------
# RQ cancellation: reaper must remove the RQ entry when it kills a row.
# Without this, the next worker boot resurrects the job and burns 20 min
# re-processing a row already marked `error`. Pinning the call site so a
# future refactor that drops cancel_rq_job from the reap path fails CI.
# ---------------------------------------------------------------------------


def test_reap_stuck_job_cancels_rq_entry(monkeypatch):
    """When the reaper kills a stuck job, it must also delete the RQ
    entry so RQ's Retry / cleanup_ghosts path can't resurrect it."""
    import queue_jobs
    calls: list[str] = []
    monkeypatch.setattr(queue_jobs, "cancel_rq_job",
                        lambda jid: calls.append(jid) or True)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=110)
        reap_all_stuck(threshold_min=100)
        assert jid in calls, (
            f"cancel_rq_job should have been called with {jid!r}, "
            f"got calls={calls!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_revert_abandoned_edit_cancels_rq_entry(monkeypatch):
    """Edit revert path also cancels the RQ entry — without this, a
    worker that comes back to life after a Railway redeploy would
    overwrite the user's existing pending_review video bytes on R2."""
    import queue_jobs
    from reaper import revert_abandoned_edit
    calls: list[str] = []
    monkeypatch.setattr(queue_jobs, "cancel_rq_job",
                        lambda jid: calls.append(jid) or True)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="editing", age_minutes=60,
            editing_started_minutes_ago=45, edit_count=2,
        )
        row = db.query(Job).filter(Job.job_id == jid).first()
        revert_abandoned_edit(db, row)
        db.commit()
        assert jid in calls, (
            f"cancel_rq_job should have been called with {jid!r} on edit revert, "
            f"got calls={calls!r}"
        )
    finally:
        _cleanup(db)
        db.close()
