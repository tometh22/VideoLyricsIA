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

from database import AIProvenance, EditorDocument, Job, SessionLocal
import reaper as _reaper
from reaper import (
    find_abandoned_edits,
    find_abandoned_transcribed,
    find_orphan_polling_jobs,
    find_queues_without_consumer,
    find_stalled_renders,
    find_stuck_jobs,
    find_stuck_transcriptions,
    reap_all_stuck,
)

_UNSET = object()


def _seed(db, *, status: str, age_minutes: float, job_id: str | None = None,
          editing_started_minutes_ago: float | None = None,
          last_progress_minutes_ago: float | None = None,
          last_user_activity_minutes_ago: float | None = None,
          segments_json=_UNSET,
          edit_count: int = 0,
          progress: int = 20,
          current_step: str = "video"):
    """Insert a Job row at a synthetic age. editing_started_minutes_ago
    drives the find_abandoned_edits clock; last_progress_minutes_ago
    drives the find_stalled_renders clock. Pass None to leave the
    column unset (mirrors legacy rows / paths that never tick progress)."""
    # Keep synthetic ids inside the same VARCHAR(12) contract as production.
    jid = job_id or f"reap_{uuid.uuid4().hex[:7]}"
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
    values = dict(
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
    )
    if segments_json is not _UNSET:
        values["segments_json"] = segments_json
    db.add(Job(**values))
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


def _reap_seeded_transcription(db, job_id: str) -> None:
    """Exercise the transcription reaper without racing the app daemon.

    The full suite starts FastAPI lifespan threads in earlier tests.  Calling
    ``reap_all_stuck`` here can therefore lose the advisory-lock race to that
    daemon and turn these per-row contract tests into nondeterministic
    integration tests.  Sweep discovery and orchestration have their own
    coverage; these assertions are about the locked row mutation/cancellation
    contract.
    """
    stuck = find_stuck_transcriptions(db, threshold_min=120)
    job = next((row for row in stuck if row.job_id == job_id), None)
    assert job is not None, f"expected seeded transcription {job_id!r} to be stuck"
    _reaper.reap_stuck_transcription(db, job)
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
        # Copy fix 2026-05-25: reaper's user-facing message in reaper.py:395
        # was rewritten ("Worker abandonó" → "El video se interrumpió por
        # un problema temporal del servidor") but the test wasn't updated.
        # Match against the current copy.
        assert row.error and "se interrumpió" in row.error.lower(), (
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


def test_live_veo_with_fresh_heartbeat_is_not_false_killed():
    """Regression (Sentry "Reaper killed 1 stuck job", UMG
    universal_argentina 2026-07-16): a LIVE Veo render whose 1st poll
    attempt timed out at 10 min left a 15-min-old orphan provenance row,
    but the worker is alive on the 2nd attempt and heartbeats
    last_progress_at every ≤60s (jobs.heartbeat). The orphan row alone
    must NOT reap it — the job heartbeat has to be stale too."""
    db = SessionLocal()
    try:
        _cleanup(db)
        # Started 25 min ago; 1st attempt orphaned a 15-min-old NULL row;
        # the live 2nd attempt heartbeated 1 min ago. Explicit job_id ≤12
        # chars: the column is String(12) and _seed's auto-gen is 13.
        jid = _seed(db, job_id="hbg_live", status="processing",
                    age_minutes=25, last_progress_minutes_ago=1)
        _seed_provenance(db, job_id=jid, age_minutes=15, duration_ms=None)
        orphans = find_orphan_polling_jobs(db, threshold_min=10)
        assert all(j.job_id != jid for j in orphans), (
            "a live job heartbeating every 60s must not be reaped just "
            "because a timed-out earlier attempt left an orphan row"
        )
    finally:
        _cleanup(db)
        db.close()


def test_dead_worker_stale_heartbeat_and_orphan_is_reaped():
    """The genuine deploy-death still reaps: orphan NULL provenance row
    AND no heartbeat for >threshold min. Both signals stale → reap."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, job_id="hbg_dead", status="processing",
                    age_minutes=25, last_progress_minutes_ago=15)
        _seed_provenance(db, job_id=jid, age_minutes=15, duration_ms=None)
        orphans = find_orphan_polling_jobs(db, threshold_min=10)
        assert any(j.job_id == jid for j in orphans), (
            "a truly dead worker (stale orphan row + stale heartbeat) "
            "must still be reaped"
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
        # age path ("se interrumpió"), not the orphan path ("se reinició"),
        # since stuck is processed first and orphans are filtered.
        # Copy fix 2026-05-25: was "abandonó"; reaper.py:395 message
        # was rewritten to "El video se interrumpió por un problema
        # temporal del servidor".
        assert n >= 1
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error"
        assert "se interrumpió" in row.error.lower(), (
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

def test_reap_transcribed_with_provenance_is_quarantined_without_data_loss():
    """Incomplete transcriptions become retryable instead of being deleted.

    Provenance and the Job row must remain intact: deleting either made an
    out-of-order browser response unrecoverable and previously triggered FK
    failures when provenance existed.
    """
    from reaper import _delete_abandoned_transcribed
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="transcribed_pending", age_minutes=120,
            job_id="prov_quar",
        )
        _seed_provenance(db, job_id=jid, age_minutes=110, duration_ms=4973,
                         step="lyrics_reference_fetch", tool_name="gemini-2.5-flash")
        job = db.query(Job).filter(Job.job_id == jid).first()
        assert _delete_abandoned_transcribed(db, job) is True
        db.commit()
        row = db.query(Job).filter(Job.job_id == jid).one()
        assert row.status == "transcription_failed"
        assert row.archived_at is not None
        assert row.error and "audio sigue guardado" in row.error
        assert db.query(AIProvenance).filter(AIProvenance.job_id == jid).count() == 1
    finally:
        _cleanup(db)
        db.close()


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
    """Old incomplete row + stale activity → genuinely abandoned."""
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


def test_completed_transcription_is_never_hard_deleted_by_short_ttl():
    """Regression (staging batch A/D, 2026-07-30): the worker successfully
    persisted lyrics and intentionally left the row in transcribed_pending.
    Thirty minutes later the reaper hard-deleted all 12 rows. The same path
    could erase a real UMG operator's completed transcription while they
    reviewed another song. Persisted segments make the row operator work, not
    abandoned upload state, so this short-TTL sweep must never select it."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db,
            status="transcribed_pending",
            age_minutes=24 * 60,
            job_id="done_keep",
            last_user_activity_minutes_ago=23 * 60,
            segments_json=[
                {"start": 1.0, "end": 3.0, "text": "Trabajo del operador"},
            ],
        )
        abandoned = find_abandoned_transcribed(db, ttl_min=30)
        assert all(j.job_id != jid for j in abandoned), (
            "completed transcriptions with segments must not be hard-deleted"
        )
    finally:
        _cleanup(db)
        db.close()


def test_delete_helper_refuses_completed_transcription():
    """Defense in depth: even a future caller that bypasses the selector
    cannot hard-delete a row once operator-visible segments exist."""
    from reaper import _delete_abandoned_transcribed
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db,
            status="transcribed_pending",
            age_minutes=24 * 60,
            job_id="done_guard",
            segments_json=[
                {"start": 1.0, "end": 3.0, "text": "Trabajo del operador"},
            ],
        )
        job = db.query(Job).filter(Job.job_id == jid).one()
        _delete_abandoned_transcribed(db, job)
        db.commit()
        assert db.query(Job).filter(Job.job_id == jid).one_or_none() is not None
    finally:
        _cleanup(db)
        db.close()


def test_editor_document_protects_job_even_if_legacy_snapshot_is_null():
    """A partial legacy bridge must never make durable corrections reapable.

    EditorDocument and EditorVersion cascade from Job.  A hard delete based
    only on a null Job.segments_json would therefore erase the very recovery
    history designed to protect the operator.
    """
    from reaper import _delete_abandoned_transcribed
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="transcribed_pending", age_minutes=24 * 60,
            job_id="editor_guard", segments_json=None,
        )
        db.add(EditorDocument(
            job_id=jid,
            tenant_id="tenant_reap_test",
            current_segments=[{"start": 1.0, "end": 2.0, "text": "Corrección"}],
            original_segments=[{"start": 1.0, "end": 2.0, "text": "Original"}],
            revision=3,
        ))
        db.commit()

        assert jid not in {
            row.job_id for row in find_abandoned_transcribed(db, ttl_min=30)
        }
        job = db.query(Job).filter(Job.job_id == jid).one()
        _delete_abandoned_transcribed(db, job)
        db.commit()
        assert db.query(Job).filter(Job.job_id == jid).one_or_none() is not None
        assert db.query(EditorDocument).filter(
            EditorDocument.job_id == jid,
        ).one_or_none() is not None
    finally:
        _cleanup(db)
        db.close()


def test_reaper_rechecks_candidate_after_concurrent_segments_commit():
    """Close the selector/use race found by adversarial review."""
    from reaper import _delete_abandoned_transcribed
    selecting_db = SessionLocal()
    writer_db = SessionLocal()
    try:
        _cleanup(selecting_db)
        jid = _seed(
            selecting_db, status="transcribed_pending", age_minutes=120,
            job_id="reaper_race", segments_json=None,
        )
        candidate = next(
            row for row in find_abandoned_transcribed(selecting_db, ttl_min=30)
            if row.job_id == jid
        )

        written = writer_db.query(Job).filter(Job.job_id == jid).one()
        written.segments_json = [
            {"start": 0.0, "end": 1.0, "text": "Guardado concurrente"},
        ]
        writer_db.commit()

        assert _delete_abandoned_transcribed(selecting_db, candidate) is False
        selecting_db.commit()
        survivor = selecting_db.query(Job).filter(Job.job_id == jid).one()
        assert survivor.status == "transcribed_pending"
        assert survivor.segments_json[0]["text"] == "Guardado concurrente"
    finally:
        writer_db.close()
        _cleanup(selecting_db)
        selecting_db.close()


def test_soft_superseded_ids_are_excluded_from_short_ttl_sweeps():
    db = SessionLocal()
    try:
        _cleanup(db)
        transcribed_id = _seed(
            db, status="transcribed_pending", age_minutes=120,
            job_id="arch_tr", segments_json=None,
        )
        upload_id = _seed(
            db, status="awaiting_upload", age_minutes=120,
            job_id="arch_up", segments_json=None,
        )
        archived_at = datetime.now(timezone.utc)
        db.query(Job).filter(Job.job_id.in_([transcribed_id, upload_id])).update(
            {Job.archived_at: archived_at}, synchronize_session=False,
        )
        db.commit()

        assert transcribed_id not in {
            row.job_id for row in find_abandoned_transcribed(db, ttl_min=30)
        }
        assert upload_id not in {
            row.job_id for row in _reaper.find_abandoned_uploads(db, ttl_min=20)
        }
    finally:
        _cleanup(db)
        db.close()


def test_abandoned_upload_is_soft_quarantined_and_remains_resumable():
    from reaper import _delete_abandoned_upload
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(
            db, status="awaiting_upload", age_minutes=120,
            job_id="up_quar", segments_json=None,
        )
        candidate = next(
            row for row in _reaper.find_abandoned_uploads(db, ttl_min=20)
            if row.job_id == jid
        )
        assert _delete_abandoned_upload(db, candidate) is True
        db.commit()
        row = db.query(Job).filter(Job.job_id == jid).one()
        assert row.status == "awaiting_upload"
        assert row.archived_at is not None
    finally:
        _cleanup(db)
        db.close()


def test_abandoned_upload_rechecks_after_concurrent_transcription_commit():
    """An upload selected by TTL cannot delete a newly transcribed editor."""
    from reaper import _delete_abandoned_upload
    selecting_db = SessionLocal()
    writer_db = SessionLocal()
    try:
        _cleanup(selecting_db)
        jid = _seed(
            selecting_db, status="awaiting_upload", age_minutes=120,
            job_id="upload_race", segments_json=None,
        )
        candidate = next(
            row for row in _reaper.find_abandoned_uploads(selecting_db, ttl_min=20)
            if row.job_id == jid
        )

        advanced = writer_db.query(Job).filter(Job.job_id == jid).one()
        advanced.status = "transcribed_pending"
        advanced.segments_json = [
            {"start": 0.0, "end": 1.0, "text": "Ya transcripta"},
        ]
        writer_db.commit()

        assert _delete_abandoned_upload(selecting_db, candidate) is False
        selecting_db.commit()
        survivor = selecting_db.query(Job).filter(Job.job_id == jid).one()
        assert survivor.status == "transcribed_pending"
        assert survivor.archived_at is None
        assert survivor.segments_json[0]["text"] == "Ya transcripta"
    finally:
        writer_db.close()
        _cleanup(selecting_db)
        selecting_db.close()


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


# ---------------------------------------------------------------------------
# Stuck transcriptions (transcribing_queued / transcribing) — incident
# 2026-05-26. Before this sweep, jobs whose RQ Retry got stranded in
# ScheduledJobRegistry (worker had `with_scheduler=False`) sat in the
# transcribing states forever — find_stuck_jobs only sweeps processing/
# queued/bg_preview_queued, and transcription_failure_callback never fires
# while RQ thinks retries are pending. agus.cafisi (omg) reported 3 stuck
# jobs at 46m/2h/2h that this sweep would have caught at 120 min.
# ---------------------------------------------------------------------------


def test_fresh_transcribing_queued_is_left_alone():
    """A 30-min-old transcribing_queued job is healthy queue lag, not stuck."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="transcribing_queued", age_minutes=30)
        stuck = find_stuck_transcriptions(db, threshold_min=120)
        assert all(j.job_id != jid for j in stuck), (
            "30-min-old transcribing_queued must not be reaped at threshold=120"
        )
    finally:
        _cleanup(db)
        db.close()


def test_old_transcribing_queued_is_reaped_with_retry_cta(monkeypatch):
    """A 130-min transcribing_queued row → transcription_failed with the
    'Reintentar' message the editor's CTA matches. The audio still on R2
    means the retry skips re-upload."""
    db = SessionLocal()
    try:
        import queue_jobs
        monkeypatch.setattr(queue_jobs, "rq_job_is_active", lambda _jid: False)
        _cleanup(db)
        jid = _seed(db, status="transcribing_queued", age_minutes=130)
        _reap_seeded_transcription(db, jid)
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "transcription_failed", (
            f"expected 'transcription_failed', got {row.status!r}"
        )
        assert row.error and "transcripción se interrumpió" in row.error.lower(), (
            f"expected transcription-specific reason, got {row.error!r}"
        )
        assert row.completed_at is not None, "completed_at should be stamped"
    finally:
        _cleanup(db)
        db.close()


def test_old_but_still_queued_transcription_is_not_reaped(monkeypatch):
    """Una ola grande puede esperar >120 min sin estar muerta."""
    import queue_jobs
    monkeypatch.setattr(queue_jobs, "rq_job_is_active", lambda _jid: True)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="transcribing_queued", age_minutes=130)
        _reap_seeded_transcription(db, jid)
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "transcribing_queued"
    finally:
        _cleanup(db)
        db.close()


def test_old_transcribing_in_flight_is_reaped():
    """`transcribing` (worker already picked up + flipped status) is also
    swept — without this, a worker that flipped status then died before
    finishing leaves the row stuck in `transcribing` indefinitely."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="transcribing", age_minutes=130,
                    last_progress_minutes_ago=125)
        _reap_seeded_transcription(db, jid)
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "transcription_failed", (
            f"expected 'transcription_failed', got {row.status!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_stuck_transcription_sweep_skips_terminal_statuses():
    """Don't touch transcription_failed (already terminal) or done/error
    (different terminal states from past pipelines)."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jids = [
            _seed(db, status="transcription_failed", age_minutes=130),
            _seed(db, status="done", age_minutes=130),
            _seed(db, status="error", age_minutes=130),
            _seed(db, status="transcribed_pending", age_minutes=130,
                  last_user_activity_minutes_ago=10),
        ]
        stuck = find_stuck_transcriptions(db, threshold_min=120)
        stuck_ids = {j.job_id for j in stuck}
        for jid in jids:
            assert jid not in stuck_ids, (
                f"sweep should not touch {jid!r}, got {stuck_ids!r}"
            )
    finally:
        _cleanup(db)
        db.close()


def test_stuck_transcription_cancels_rq_entry_with_prefix(monkeypatch):
    """The RQ entry for a transcription uses the `transcribe:<job_id>`
    prefix (queue_jobs.py:457). The reaper must cancel the prefixed form,
    not the bare job_id — otherwise the RQ side stays alive and RQScheduler
    can move a stranded retry back to the queue, resurrecting a row that
    is now in a terminal state."""
    import queue_jobs
    calls: list[str] = []
    monkeypatch.setattr(queue_jobs, "rq_job_is_active", lambda _jid: False)
    monkeypatch.setattr(queue_jobs, "cancel_rq_job",
                        lambda jid: calls.append(jid) or True)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="transcribing_queued", age_minutes=130)
        _reap_seeded_transcription(db, jid)
        prefixed = f"transcribe:{jid}"
        assert prefixed in calls, (
            f"cancel_rq_job should be called with {prefixed!r}, "
            f"got calls={calls!r}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_stuck_transcription_cancels_outbox_attempt_id(monkeypatch):
    """El path actual usa `transcription:<event_id>`, no el id legacy."""
    import queue_jobs
    calls: list[str] = []
    monkeypatch.setattr(queue_jobs, "rq_job_is_active", lambda _jid: False)
    monkeypatch.setattr(
        queue_jobs, "cancel_rq_job", lambda jid: calls.append(jid) or True,
    )
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="transcribing_queued", age_minutes=130)
        row = db.query(Job).filter(Job.job_id == jid).first()
        row.active_transcription_attempt_id = "attempt-123"
        db.commit()
        _reap_seeded_transcription(db, jid)
        assert "transcription:attempt-123" in calls
        assert f"transcribe:{jid}" not in calls
    finally:
        _cleanup(db)
        db.close()


def test_revert_abandoned_edit_cancels_rq_entry(monkeypatch):
    """Edit revert path also cancels the RQ entry — without this, a
    worker that comes back to life after a Railway redeploy would
    overwrite the user's existing pending_review video bytes on R2.

    Regression coverage for the audit-2026-05-26 fix: enqueue_edit uses
    `edit:<job_id>` as the RQ id (queue_jobs.py:694) to avoid colliding
    with the render job sharing the same Postgres job_id. The revert
    path MUST cancel using the prefixed form — calling cancel_rq_job
    with the bare job_id misses the RQ entry, the ScheduledJobRegistry
    retry timer fires, and the half-finished edit overwrites the good
    video. This test guards against that regression specifically.
    """
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
        prefixed = f"edit:{jid}"
        assert prefixed in calls, (
            f"cancel_rq_job should be called with {prefixed!r} (with the "
            f"`edit:` prefix that enqueue_edit uses), got calls={calls!r}"
        )
    finally:
        _cleanup(db)
        db.close()


# ---------------------------------------------------------------------------
# Sentry visibility (UMG-launch hardening 2026-06-01)
# ---------------------------------------------------------------------------

def test_sentry_capture_emits_error_event_per_reaped_batch(monkeypatch):
    """Every reaped batch must surface as a Sentry ERROR event tagged
    `reaper.killed` — stuck jobs being killed silently is exactly the
    kind of incident operators need to see live during a launch."""
    import sys
    import types
    from contextlib import contextmanager
    from reaper import _sentry_capture

    captured = {"messages": [], "tags": {}}

    fake = types.ModuleType("sentry_sdk")

    class _Scope:
        def set_tag(self, k, v):
            captured["tags"][k] = v

        def set_extra(self, k, v):
            captured.setdefault("extras", {})[k] = v

    @contextmanager
    def push_scope():
        yield _Scope()

    fake.push_scope = push_scope
    fake.capture_message = lambda msg, level="info": captured["messages"].append((msg, level))
    fake.capture_exception = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=200)
        row = db.query(Job).filter(Job.job_id == jid).first()
        _sentry_capture([row])
    finally:
        _cleanup(db)
        db.close()

    assert len(captured["messages"]) == 1
    msg, level = captured["messages"][0]
    assert level == "error"
    assert "1 stuck job" in msg
    assert captured["tags"].get("event") == "reaper.killed"
    assert "tenant_reap_test" in msg, "tenant must be visible in the alert title"


def test_sentry_capture_never_raises_when_sdk_is_broken(monkeypatch):
    """Best-effort contract: a broken sentry_sdk must never kill the
    reaper sweep that calls _sentry_capture."""
    import sys
    import types
    from reaper import _sentry_capture

    broken = types.ModuleType("sentry_sdk")

    def _boom(*a, **kw):
        raise RuntimeError("sentry down")

    broken.push_scope = _boom
    broken.capture_message = _boom
    broken.capture_exception = _boom
    monkeypatch.setitem(sys.modules, "sentry_sdk", broken)

    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="processing", age_minutes=200)
        row = db.query(Job).filter(Job.job_id == jid).first()
        _sentry_capture([row])  # must not raise
    finally:
        _cleanup(db)
        db.close()


def test_sentry_capture_noop_on_empty_batch(monkeypatch):
    """No reaped jobs → no Sentry noise."""
    import sys
    import types
    from reaper import _sentry_capture

    fake = types.ModuleType("sentry_sdk")
    fake.capture_message = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("capture_message must not be called for empty batch")
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    _sentry_capture([])  # must not raise nor capture


# --- Queue-without-consumer active alert (P0 2026-06-08 follow-up) ------------

class _FakeWorker:
    def __init__(self, qs):
        self._qs = qs

    def queue_names(self):
        return self._qs


def test_find_queues_without_consumer_flags_dead_pool(monkeypatch):
    """A dead pool leaves its queues with NO live worker — caught regardless of
    depth (the exact gap that made the P0 invisible: ShortWorker died with the
    transcription queue empty)."""
    import queue_jobs
    import rq

    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: ("redis", None, None))
    # Only the render pool alive → transcription + bg_preview + audio_preview
    # have no consumer.
    monkeypatch.setattr(rq.Worker, "all",
                        lambda connection=None: [_FakeWorker(["enterprise", "default"])])
    monkeypatch.setattr(_reaper, "_EXPECTED_QUEUES",
                        ["transcription", "bg_preview", "audio_preview", "enterprise", "default"])

    assert set(find_queues_without_consumer()) == {"transcription", "bg_preview", "audio_preview"}


def test_find_queues_without_consumer_all_served(monkeypatch):
    import queue_jobs
    import rq

    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: ("redis", None, None))
    monkeypatch.setattr(rq.Worker, "all", lambda connection=None: [
        _FakeWorker(["enterprise", "default"]),
        _FakeWorker(["transcription", "bg_preview", "audio_preview"]),
    ])
    monkeypatch.setattr(_reaper, "_EXPECTED_QUEUES",
                        ["transcription", "bg_preview", "audio_preview", "enterprise", "default"])

    assert find_queues_without_consumer() == []


def test_alert_queues_without_consumer_pages_sentry(monkeypatch):
    """A no-consumer queue must fire an ACTIVE Sentry alert at ERROR — not just
    a degraded /health field (that's what made the P0 invisible for 2h)."""
    import sys
    import types
    from reaper import _alert_queues_without_consumer

    captured = {}

    class _Scope:
        def set_tag(self, *a): pass
        def set_extra(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.ModuleType("sentry_sdk")
    fake.push_scope = lambda: _Scope()
    fake.capture_message = lambda msg, level=None: captured.update(msg=msg, level=level)
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    _alert_queues_without_consumer(["transcription"])
    assert captured.get("level") == "error"
    assert "transcription" in captured.get("msg", "")

    captured.clear()
    _alert_queues_without_consumer([])   # empty → no alert
    assert captured == {}


# ---------------------------------------------------------------------------
# upload_still_active — R2-side liveness guard for slow multipart uploads
# ---------------------------------------------------------------------------


def _upload_job(**kw):
    """In-memory Job (no DB) — upload_still_active only reads attributes."""
    defaults = dict(
        job_id="up_live", status="awaiting_upload",
        multipart_upload_id="UP", input_r2_key="inputs/t/j/a.wav",
    )
    defaults.update(kw)
    return Job(**defaults)


def test_upload_still_active_recent_parts_skips_reap(monkeypatch):
    """Parts landed on R2 within the TTL window → the upload is alive
    (slow 150 MB WAV on a residential uplink) and must NOT be reaped."""
    import storage as _storage
    monkeypatch.setattr(
        _storage, "multipart_last_activity",
        lambda key, upload_id: datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    assert _reaper.upload_still_active(_upload_job()) is True


def test_upload_still_active_stale_parts_allows_reap(monkeypatch):
    """Last part is older than the TTL → genuinely abandoned mid-upload."""
    import storage as _storage
    monkeypatch.setattr(
        _storage, "multipart_last_activity",
        lambda key, upload_id: datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    assert _reaper.upload_still_active(_upload_job()) is False


def test_upload_still_active_no_parts_allows_reap(monkeypatch):
    """No parts / upload gone / listing failed → no liveness evidence;
    the created_at TTL decides (reap)."""
    import storage as _storage
    monkeypatch.setattr(
        _storage, "multipart_last_activity", lambda key, upload_id: None,
    )
    assert _reaper.upload_still_active(_upload_job()) is False


def test_upload_still_active_ignores_non_multipart(monkeypatch):
    """Single-PUT rows (no upload_id) never consult R2 — their TTL
    semantics are unchanged."""
    import storage as _storage

    calls = []
    monkeypatch.setattr(
        _storage, "multipart_last_activity",
        lambda key, upload_id: calls.append(key),
    )
    assert _reaper.upload_still_active(
        _upload_job(multipart_upload_id=None)
    ) is False
    assert calls == []
