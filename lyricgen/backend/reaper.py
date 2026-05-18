"""Stuck-job reaper.

Detects jobs that have been in `processing` / `queued` longer than a
configured threshold and marks them as `error` so the operator's UI
shows them as failed instead of a forever-spinning "Generando".

Why we need it:
  - RQ workers can die mid-render (deploy, OOM, signal). RQ moves the
    RQ-side job to FailedJobRegistry with AbandonedJobError, but the
    Postgres `jobs` row stays at status="processing" because the worker
    never reaches its except handler.
  - Without a reaper, the operator sees a zombie that they have to
    manually delete from /admin every time. This module turns it into
    a no-touch failure path.

Side effects per reaped job:
  - Postgres: status flips to "error" with a clear message.
  - AuditLog row recorded (action="reaper.killed").
  - Sentry: capture_message at ERROR level, tagged with job_id and
    tenant_id.
  - Email: a single batched message to OWNER_EMAIL summarising every
    job killed in this pass (so a 30-zombie storm is one notification,
    not 30).

Threshold is read from env REAPER_THRESHOLD_MIN (default 100). The
default is JOB_TIMEOUT_UMG (5400 s = 90 min) + 10 min buffer.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import exists, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database import AIProvenance, AuditLog, Job, SessionLocal

# Markers for Postgres connection drops that we should silently retry
# instead of letting the reaper thread emit Sentry noise every 5 min.
# Same list as the HTTP retry middleware in main.py — kept duplicated
# here because the reaper runs in a thread, bypassing the ASGI stack.
_TRANSIENT_DB_MARKERS = (
    "SSL connection has been closed",
    "server closed the connection",
    "connection already closed",
    "could not connect to server",
)

logger = logging.getLogger("genly.reaper")

# Reaped jobs land at this status (the operator-facing status). Not
# "abandoned" because the existing UI / batches don't know that one.
_REAPED_STATUS = "error"

# How old a job needs to be before we consider it dead.
_DEFAULT_THRESHOLD_MIN = int(os.environ.get("REAPER_THRESHOLD_MIN", "100"))

# Fast-lane threshold for jobs whose latest ai_provenance row is still
# in-flight (duration_ms NULL). Veo polling, Whisper, Gemini etc. all
# record a provenance row at call start and update duration_ms when the
# call returns. If that row is older than this, the worker died mid-
# call (deploy/OOM/crash) and the DB row is a zombie. 10 min is safely
# longer than any healthy single AI call (Veo: ~2 min p99, Whisper:
# ~3 min for 7-min audio) but short enough that the user sees the
# failure surface inside one coffee break instead of two hours later.
_ORPHAN_POLL_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_ORPHAN_POLL_THRESHOLD_MIN", "10",
))

# transcribed_pending jobs are uploaded but waiting on the user to finish
# the lyrics editor and click Generate. They consume disk + R2 storage,
# and abandoned ones (closed tab, lost connection) accumulate forever
# without a separate sweep. A 30-min TTL covers the realistic editing
# window without prematurely killing live sessions.
_TRANSCRIBED_PENDING_TTL_MIN = int(os.environ.get(
    "REAPER_TRANSCRIBED_PENDING_TTL_MIN", "30",
))

# awaiting_upload jobs are even shorter-lived: the browser is mid-PUT to
# R2 with a presigned URL. If the user closes the tab partway through, we
# need to abort the multipart upload so R2 doesn't keep the parts around
# (R2 charges for the storage either way until the abort fires).
_AWAITING_UPLOAD_TTL_MIN = int(os.environ.get(
    "REAPER_AWAITING_UPLOAD_TTL_MIN", "20",
))

# Edit-request abandon threshold. The worst case is a background edit
# which re-runs Veo (~3 min p99) plus the full video composite (~5-8 min
# for a 4-min song). 30 min gives 2-3× headroom over the slowest healthy
# edit; anything older than that is a worker death (deploy/OOM) we need
# to surface to the user instead of leaving them watching "40%" forever.
_EDIT_ABANDON_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_EDIT_ABANDON_THRESHOLD_MIN", "30",
))

# Stalled-render threshold. Catches jobs in `processing` whose progress
# hasn't moved in N minutes — the gap between find_orphan_polling_jobs
# (which fires on stale in-flight AI calls) and find_stuck_jobs (which
# uses a 100-min created_at threshold). A healthy worker calls
# jobs.update_job(progress=...) at every step transition and at multiple
# checkpoints within a step, so a 20-min gap is a strong "worker is dead"
# signal. Veo polling can pause progress ~3 min p99, ffmpeg composite can
# run silently ~5 min on long songs — 20 min gives 4-6× headroom over any
# healthy phase.
_STALLED_RENDER_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_STALLED_RENDER_THRESHOLD_MIN", "20",
))

# Owner inbox for the digest email. Override via env in Railway.
_OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "tomas@epical.digital")


def find_stuck_jobs(db: Session, threshold_min: int = _DEFAULT_THRESHOLD_MIN) -> list[Job]:
    """Return jobs in processing/queued whose staleness anchor is older
    than threshold. Pending_review is intentionally excluded — those are
    waiting on a human, not a worker.

    Staleness anchor is coalesce(last_progress_at, created_at):
      - Worker calls update_job(progress=X) at every step; last_progress_at
        ticks on each call. A genuine worker death stops the ticks → the
        coalesce falls forward to a stale value and the row gets reaped.
      - A queued row that no worker has touched yet has last_progress_at
        NULL → coalesce falls back to created_at, preserving the original
        "100 min in the queue is dead" guarantee.
      - /retry resets last_progress_at = NOW(), so a retried job created
        12 h ago is no longer insta-killed on the next reaper sweep.
        Pre-fix incident 2026-05-15: programmatic retry of 4 omg jobs
        from 13:45 was killed at 16:51 because the reaper still anchored
        on created_at."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    anchor = func.coalesce(Job.last_progress_at, Job.created_at)
    return (
        db.query(Job)
        .filter(Job.status.in_(["processing", "queued"]))
        .filter(anchor < cutoff)
        .order_by(anchor.asc())
        .all()
    )


def find_orphan_polling_jobs(
    db: Session,
    threshold_min: int = _ORPHAN_POLL_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs whose latest ai_provenance row is an in-flight call
    (duration_ms NULL) older than threshold. This is the deploy-death
    signal: provenance.record_ai_call() inserts the row at call start
    and only fills duration_ms when the call returns. A stale NULL row
    means the worker died mid-poll (Railway redeploy, OOM, hard signal)
    and the job is a zombie — current_step/progress will sit forever.

    Faster path than find_stuck_jobs (which uses created_at and a
    conservative 100 min threshold) because we have a precise signal:
    we KNOW the AI call started and never finished, instead of guessing
    from age."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    orphan_provenance = (
        exists()
        .where(AIProvenance.job_id == Job.job_id)
        .where(AIProvenance.duration_ms.is_(None))
        .where(AIProvenance.created_at < cutoff)
    )
    return (
        db.query(Job)
        .filter(Job.status == "processing")
        .filter(orphan_provenance)
        .order_by(Job.created_at.asc())
        .all()
    )


def find_abandoned_transcribed(
    db: Session,
    ttl_min: int = _TRANSCRIBED_PENDING_TTL_MIN,
) -> list[Job]:
    """Return jobs stuck in transcribed_pending past the editing TTL.
    These represent users who transcribed but never clicked Generate
    (closed tab, lost connection). The associated audio file lives on
    disk + R2 and needs to be reaped or it accumulates forever.

    Staleness anchor is coalesce(last_user_activity_at, created_at): any
    authenticated touch (POST /save-segments, /status poll, etc) bumps
    last_user_activity_at, so an active batch-edit session keeps the job
    alive past the TTL. Older rows with NULL last_user_activity_at fall
    back to created_at — preserves pre-migration behavior.

    Incident 2026-05-14: a user batch-editing 5 lyrics for 90 min got
    reaped at 30 min and lost everything because the anchor was just
    created_at. The coalesce + /save-segments together fix that.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_min)
    anchor = func.coalesce(Job.last_user_activity_at, Job.created_at)
    return (
        db.query(Job)
        .filter(Job.status == "transcribed_pending")
        .filter(anchor < cutoff)
        .order_by(Job.created_at.asc())
        .all()
    )


def find_abandoned_uploads(
    db: Session,
    ttl_min: int = _AWAITING_UPLOAD_TTL_MIN,
) -> list[Job]:
    """Return jobs stuck in awaiting_upload past the upload TTL. The
    browser is supposed to PUT bytes directly to R2 within minutes of
    /upload-url; anything older than ttl_min is a closed tab / abandoned
    multipart upload."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_min)
    return (
        db.query(Job)
        .filter(Job.status == "awaiting_upload")
        .filter(Job.created_at < cutoff)
        .order_by(Job.created_at.asc())
        .all()
    )


def find_stalled_renders(
    db: Session,
    threshold_min: int = _STALLED_RENDER_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs in `processing` whose progress hasn't moved in
    threshold_min minutes.

    The scenario this catches: worker SIGKILLed during ffmpeg / moviepy /
    R2 upload — a non-AI step where there is no in-flight AIProvenance
    row to anchor find_orphan_polling_jobs, and find_stuck_jobs's 100-min
    created_at cutoff is too long. Confirmed in prod 2026-05-12: job
    2144aacb453e killed at video/40% during a deploy, invisible to any
    reaper for 87 min.

    Signal: last_progress_at, written by jobs.update_job() at every
    progress call. A healthy worker hits multiple progress checkpoints
    per minute across whisper / background / video / thumbnail steps.
    A 20-min gap means the worker is gone.

    Rows with last_progress_at IS NULL are skipped — they predate this
    column (or status=queued, never picked up). The age-based
    find_stuck_jobs covers those at 100 min.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    return (
        db.query(Job)
        .filter(Job.status == "processing")
        .filter(Job.last_progress_at.isnot(None))
        .filter(Job.last_progress_at < cutoff)
        .order_by(Job.last_progress_at.asc())
        .all()
    )


def find_abandoned_edits(
    db: Session,
    threshold_min: int = _EDIT_ABANDON_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs stuck in `status='editing'` past the edit threshold.

    The scenario this catches: operator clicks "Regenerate background" /
    "Change typography" / "Fix lyrics", worker picks up the RQ job, then
    Railway redeploys mid-render. Worker process dies. RQ moves the
    queue entry to FailedJobRegistry with AbandonedJobError, but the
    Postgres row stays at status='editing'/progress=N% because the worker
    never reached its except handler. From the user's POV, the video is
    stuck at "40%" indefinitely.

    Why we don't use created_at like find_stuck_jobs: lyrics edits are
    allowed from `done` and `rejected` status, so editing_started_at
    might be hours/days after created_at. The dedicated timestamp is set
    by the /edit handler (main.py) the moment the row flips to editing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    return (
        db.query(Job)
        .filter(Job.status == "editing")
        .filter(Job.editing_started_at.isnot(None))
        .filter(Job.editing_started_at < cutoff)
        .order_by(Job.editing_started_at.asc())
        .all()
    )


def revert_abandoned_edit(db: Session, job: Job) -> None:
    """Roll an abandoned edit back to pending_review so the user can
    re-try. The video on R2 from the prior successful render is still
    intact (the worker overwrites only at the very end of the pipeline,
    after the composite is finalized — if it died mid-render no bytes
    were written). Decrement edit_count so the failed attempt doesn't
    burn one of the operator's 3 allowed edits — Railway's fault, not
    theirs. Caller commits.

    Also cancels the RQ entry so a worker restart can't resurrect the
    half-finished edit and silently overwrite the user's good video.
    """
    try:
        from queue_jobs import cancel_rq_job
        cancel_rq_job(job.job_id)
    except Exception as e:  # pragma: no cover
        logger.warning("cancel_rq_job (edit) failed for %s: %s", job.job_id, e)
    prev_edit_count = job.edit_count or 0
    new_edit_count = max(0, prev_edit_count - 1)
    age = 0.0
    if job.editing_started_at:
        started = job.editing_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
    job.status = "pending_review"
    # The pre-edit terminal state was current_step="thumbnail"/progress=100
    # for any job that made it to pending_review. Restoring those values
    # keeps the progress bar / status badge consistent with what the user
    # saw before they clicked the edit button.
    job.current_step = "thumbnail"
    job.progress = 100
    job.edit_count = new_edit_count
    job.editing_started_at = None
    job.error = None
    db.add(AuditLog(
        action="reaper.reverted_edit",
        detail={
            "job_id": job.job_id,
            "tenant_id": job.tenant_id,
            "artist": job.artist,
            "song_title": job.song_title,
            "age_minutes": round(age, 1),
            "reason": (
                "Edit request abandoned by worker (probable Railway deploy "
                "or OOM mid-render). Reverted to pending_review with "
                "edit_count restored so the user can re-try at no cost."
            ),
            "previous": {
                "status": "editing",
                "current_step": job.current_step,
                "progress": job.progress,
                "edit_count": prev_edit_count,
            },
            "now": {
                "status": "pending_review",
                "edit_count": new_edit_count,
            },
        },
    ))


def _age_minutes(job: Job) -> float:
    if not job.created_at:
        return 0.0
    created = job.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60.0


def _reason_for(job: Job) -> str:
    age = _age_minutes(job)
    return (
        f"Worker abandonó el job tras {age:.0f} min sin progreso "
        f"(probable container restart, timeout o crash). "
        f"Re-uploadeá el archivo para reintentar."
    )


def _reason_for_orphan(job: Job) -> str:
    return (
        "El servidor se reinició mientras generábamos el video de fondo. "
        "Tu MP3 sigue guardado: apretá \"Reintentar sin re-subir\" "
        "para volver a generarlo."
    )


def _reason_for_stalled(job: Job) -> str:
    return (
        "El servidor se reinició mientras renderizábamos tu video. "
        "Tu archivo sigue guardado: apretá \"Reintentar\" para "
        "volver a procesarlo."
    )


def _delete_abandoned_transcribed(db: Session, job: Job) -> None:
    """Hard-delete an abandoned transcribed_pending row + its audio file.
    The user never finalized the upload, so there's no operator-facing
    artifact to preserve."""
    job_id = job.job_id
    # Local file under OUTPUTS_DIR.
    try:
        from pipeline import OUTPUTS_DIR
        local_dir = os.path.join(OUTPUTS_DIR, job_id)
        if os.path.isdir(local_dir):
            import shutil as _sh
            _sh.rmtree(local_dir, ignore_errors=True)
    except Exception as e:  # pragma: no cover
        logger.debug(f"reaper: local dir cleanup failed for {job_id}: {e}")
    # Cancel any stale RQ entry (best-effort, consistent with the other
    # cleanup paths in this file).
    try:
        from queue_jobs import cancel_rq_job
        cancel_rq_job(job_id)
    except Exception as e:  # pragma: no cover
        logger.debug("reaper: cancel_rq_job failed for %s: %s", job_id, e)
    # R2 object (best-effort; failure is fine — orphan stays for next sweep).
    if job.input_r2_key:
        try:
            import storage as _storage
            _storage.delete_object(job.input_r2_key)
        except Exception as e:  # pragma: no cover
            logger.debug("reaper: R2 delete failed for %s: %s", job_id, e)
    db.delete(job)


def _delete_abandoned_upload(db: Session, job: Job) -> None:
    """Hard-delete an abandoned awaiting_upload row.

    Three cleanup paths depending on what state the upload reached:
      1. Multipart in-flight → abort_multipart_upload to release the
         parts R2 has accepted so far.
      2. Single-PUT or completed multipart → delete the object.
      3. No R2 key recorded yet (browser never started) → just drop
         the row.
    """
    job_id = job.job_id
    try:
        import storage as _storage
        if job.multipart_upload_id and job.input_r2_key:
            _storage.multipart_abort(job.input_r2_key, job.multipart_upload_id)
        elif job.input_r2_key:
            try:
                _storage.delete_object(job.input_r2_key)
            except Exception:
                pass
    except Exception as e:  # pragma: no cover
        logger.debug(f"reaper: R2 cleanup failed for {job_id}: {e}")
    # Local dir (rare for awaiting_upload, but defensive).
    try:
        from pipeline import OUTPUTS_DIR
        local_dir = os.path.join(OUTPUTS_DIR, job_id)
        if os.path.isdir(local_dir):
            import shutil as _sh
            _sh.rmtree(local_dir, ignore_errors=True)
    except Exception:
        pass
    db.delete(job)


def reap_stuck_job(db: Session, job: Job, reason: str) -> None:
    """Flip the row to error, cancel the RQ job, and log it. Caller commits.

    Cancelling the RQ entry is critical: without it, RQ's Retry / cleanup
    path resurrects the abandoned job on the next worker boot. The worker
    then re-runs the pipeline against a row already marked `error`, and
    `jobs.update_job`'s terminal-state guard silently discards the result
    at the end — 20 min of compute thrown away while the user keeps seeing
    the "Worker abandonó el job" message.

    Incident 2026-05-15: 4 omg jobs got reaped, then resurrected by RQ on
    the next worker restart, re-processed silently, and ended in the same
    `error` state. Fixing the desync here closes the loop.
    """
    rq_removed = False
    try:
        from queue_jobs import cancel_rq_job
        rq_removed = cancel_rq_job(job.job_id)
    except Exception as e:  # pragma: no cover
        logger.warning("cancel_rq_job failed for %s: %s", job.job_id, e)
    job.status = _REAPED_STATUS
    job.error = reason
    job.completed_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        action="reaper.killed",
        detail={
            "job_id": job.job_id,
            "tenant_id": job.tenant_id,
            "artist": job.artist,
            "filename": job.filename,
            "previous_status": "processing",  # by definition
            "current_step": job.current_step,
            "progress": job.progress,
            "age_minutes": round(_age_minutes(job), 1),
            "reason": reason,
            "rq_removed": rq_removed,
        },
    ))


def _sentry_capture(reaped: list[Job]) -> None:
    """Best-effort Sentry alert. Never raises — failure is logged but
    swallowed so the reaper itself doesn't get killed by observability."""
    if not reaped:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("event", "reaper.killed")
            scope.set_tag("job_count", str(len(reaped)))
            scope.set_extra("jobs", [
                {
                    "job_id": j.job_id,
                    "tenant_id": j.tenant_id,
                    "artist": j.artist,
                    "current_step": j.current_step,
                    "age_min": round(_age_minutes(j), 1),
                }
                for j in reaped
            ])
            tenants = sorted({j.tenant_id for j in reaped if j.tenant_id})
            tenants_str = ", ".join(tenants[:5]) or "—"
            sentry_sdk.capture_message(
                f"Reaper killed {len(reaped)} stuck job(s) ({tenants_str})",
                level="error",
            )
    except Exception as e:  # pragma: no cover
        logger.warning(f"sentry capture failed: {e}")


def _email_owner(reaped: list[Job]) -> None:
    """Single digest email to OWNER_EMAIL summarising the pass. Silently
    no-op if SMTP is not configured (`emails._send_email` checks)."""
    if not reaped:
        return
    try:
        from emails import _send_email, _wrap_template
    except Exception as e:  # pragma: no cover
        logger.warning(f"email module unavailable: {e}")
        return
    try:
        rows = "\n".join(
            f"<tr>"
            f"<td style='padding:6px 12px;font-family:monospace;font-size:11px'>{j.job_id[:12]}…</td>"
            f"<td style='padding:6px 12px'>{j.tenant_id or '—'}</td>"
            f"<td style='padding:6px 12px'>{j.artist or '—'}</td>"
            f"<td style='padding:6px 12px'>{j.current_step or '—'}</td>"
            f"<td style='padding:6px 12px;text-align:right'>{_age_minutes(j):.0f} min</td>"
            f"</tr>"
            for j in reaped
        )
        body = _wrap_template(f"""
            <h2 style="margin:0 0 12px">Reaper killed {len(reaped)} stuck job(s)</h2>
            <p style="color:#555">
              These jobs were in <code>processing</code> for more than
              {_DEFAULT_THRESHOLD_MIN} min and got auto-marked as error.
              Investigate worker logs around the timestamps below to find
              the root cause (container restart, OOM, hung ffmpeg, Veo
              429-storm, etc.).
            </p>
            <table style="border-collapse:collapse;font-size:13px;margin-top:16px">
              <thead>
                <tr style="background:#f6f6f6;text-align:left">
                  <th style="padding:6px 12px">Job</th>
                  <th style="padding:6px 12px">Tenant</th>
                  <th style="padding:6px 12px">Artist</th>
                  <th style="padding:6px 12px">Last step</th>
                  <th style="padding:6px 12px;text-align:right">Age</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
        """)
        _send_email(
            _OWNER_EMAIL,
            f"[GenLy] {len(reaped)} stuck job(s) reaped",
            body,
        )
    except Exception as e:  # pragma: no cover
        logger.warning(f"reaper email failed: {e}")


# Postgres advisory lock key. 64-bit signed int, must be the same
# across every replica that wants to coordinate. The constant is
# arbitrary; just don't reuse it elsewhere in the app.
_REAPER_ADVISORY_LOCK_KEY = 9118364455199101


def reap_all_stuck(threshold_min: int = _DEFAULT_THRESHOLD_MIN) -> int:
    """Public entrypoint with a transient-error retry. The reaper runs
    in a background thread (main.py:_reaper_loop) which is OUTSIDE the
    ASGI stack — the HTTP retry middleware can't catch a Postgres SSL
    drop here. Without this wrapper, Railway's idle-connection eviction
    on the reaper's first query of the cycle bubbles all the way up to
    Sentry every few minutes, even though the reaper itself recovers on
    the next cycle. One retry with a fresh session swallows the noise.
    """
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            return _reap_all_stuck_inner(threshold_min)
        except OperationalError as exc:
            if not any(m in str(exc) for m in _TRANSIENT_DB_MARKERS):
                raise
            last_exc = exc
            logger.warning(
                "reaper: transient DB error on attempt %d (%s) — retrying",
                attempt, type(exc).__name__,
            )
            time.sleep(0.5)
    # Both attempts hit transient errors. Surface the original so Sentry
    # still has visibility on persistent outages.
    assert last_exc is not None
    raise last_exc


def _reap_all_stuck_inner(threshold_min: int) -> int:
    """One pass. Owns its own DB session — safe to call from a worker
    thread or a scheduled task. Returns the count of reaped jobs.

    Uses pg_try_advisory_lock so when the API scales horizontally
    (Railway > 1 replica), only one instance does the reap pass per
    cycle. Without this, every replica's reaper thread runs in
    parallel — the work is idempotent but it's 2-3× the DB load and
    triplicates the Sentry/email notification noise.

    SQLite (tests) silently no-ops the lock and proceeds — the test
    suite never has competing reapers.
    """
    db = SessionLocal()
    try:
        # Try to take the advisory lock. pg_try_advisory_lock is non-
        # blocking; if another replica already has it, returns false
        # and we skip this cycle entirely. The lock auto-releases on
        # session close (we always close in finally below).
        is_postgres = db.bind.dialect.name == "postgresql"
        if is_postgres:
            from sqlalchemy import text
            got = db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _REAPER_ADVISORY_LOCK_KEY},
            ).scalar()
            if not got:
                logger.debug(
                    "reaper: another replica holds the advisory lock; "
                    "skipping this cycle",
                )
                return 0

        stuck = find_stuck_jobs(db, threshold_min)
        orphans = find_orphan_polling_jobs(db)
        stalled = find_stalled_renders(db)
        # De-dupe across the three processing-status sweeps. `stuck` (age-
        # based) wins over both, then `orphans` (AI in-flight) wins over
        # `stalled` (last_progress_at). Same root cause "worker dead" so
        # the operator only needs one reaped-notification per job; we just
        # pick the most specific reason.
        stuck_ids = {j.job_id for j in stuck}
        orphans = [j for j in orphans if j.job_id not in stuck_ids]
        orphan_ids = {j.job_id for j in orphans}
        stalled = [j for j in stalled
                   if j.job_id not in stuck_ids and j.job_id not in orphan_ids]
        abandoned = find_abandoned_transcribed(db)
        abandoned_uploads = find_abandoned_uploads(db)
        abandoned_edits = find_abandoned_edits(db)
        # Reap abandoned transcribed_pending rows quietly: the user never
        # got a job started, so the failure isn't operator-visible. Just
        # delete the row and clean up the input file.
        for job in abandoned:
            _delete_abandoned_transcribed(db, job)
        for job in abandoned_uploads:
            _delete_abandoned_upload(db, job)
        # Abandoned edits get reverted (not deleted) — the prior render
        # is still on R2 and the user wants to re-approve or re-try.
        for job in abandoned_edits:
            revert_abandoned_edit(db, job)
        if abandoned or abandoned_uploads or abandoned_edits:
            db.commit()
            if abandoned:
                logger.info(
                    "[REAPER] cleaned up %d abandoned transcribed_pending job(s)",
                    len(abandoned),
                )
            if abandoned_uploads:
                logger.info(
                    "[REAPER] cleaned up %d abandoned awaiting_upload job(s)",
                    len(abandoned_uploads),
                )
            if abandoned_edits:
                logger.info(
                    "[REAPER] reverted %d abandoned edit job(s) back to pending_review",
                    len(abandoned_edits),
                )
        if not stuck and not orphans and not stalled:
            return 0
        for job in stuck:
            reap_stuck_job(db, job, _reason_for(job))
        for job in orphans:
            reap_stuck_job(db, job, _reason_for_orphan(job))
        for job in stalled:
            reap_stuck_job(db, job, _reason_for_stalled(job))
        db.commit()
        # Detach so we can pass to background helpers without keeping the
        # session open. The fields we need are already populated.
        for job in stuck:
            db.expunge(job)
        for job in orphans:
            db.expunge(job)
        for job in stalled:
            db.expunge(job)
        # Merge all three sweeps into one notification batch — operators
        # care about "what got reaped this cycle", not which sweep flagged
        # it. de-dup above already guaranteed no overlap.
        stuck = stuck + orphans + stalled
    finally:
        # Releasing the advisory lock is implicit on session close
        # (Postgres releases all session-scoped locks automatically),
        # but we call pg_advisory_unlock explicitly for clarity and
        # to fail fast if pool reuse ever changes the behaviour.
        if 'is_postgres' in locals() and is_postgres:
            try:
                from sqlalchemy import text
                db.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _REAPER_ADVISORY_LOCK_KEY},
                )
                db.commit()
            except Exception:
                pass
        db.close()

    # Side-effect notifications happen AFTER the DB commit so a failed
    # email/Sentry call never rolls back a successful reap.
    _sentry_capture(stuck)
    _email_owner(stuck)

    logger.warning(
        "reaper killed %d stuck job(s): %s",
        len(stuck),
        [j.job_id for j in stuck],
    )
    return len(stuck)
