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

from sqlalchemy import exists
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

# Owner inbox for the digest email. Override via env in Railway.
_OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "tomas@epical.digital")


def find_stuck_jobs(db: Session, threshold_min: int = _DEFAULT_THRESHOLD_MIN) -> list[Job]:
    """Return jobs in processing/queued whose `created_at` is older than
    threshold. Pending_review is intentionally excluded — those are
    waiting on a human, not a worker."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    return (
        db.query(Job)
        .filter(Job.status.in_(["processing", "queued"]))
        .filter(Job.created_at < cutoff)
        .order_by(Job.created_at.asc())
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
    disk + R2 and needs to be reaped or it accumulates forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_min)
    return (
        db.query(Job)
        .filter(Job.status == "transcribed_pending")
        .filter(Job.created_at < cutoff)
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
    # R2 object (best-effort; failure is fine — orphan stays for next sweep).
    if job.input_r2_key:
        try:
            import storage as _storage
            _storage.delete_object(job.input_r2_key)
        except Exception as e:  # pragma: no cover
            logger.debug(f"reaper: R2 delete failed for {job_id}: {e}")
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
    """Flip the row to error and log it. Caller commits."""
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
        # Drop jobs that already appear in `stuck` so we don't double-reap
        # the same row (the age-based and provenance-based sweeps overlap
        # for jobs that are both very old AND have an in-flight AI call).
        stuck_ids = {j.job_id for j in stuck}
        orphans = [j for j in orphans if j.job_id not in stuck_ids]
        abandoned = find_abandoned_transcribed(db)
        abandoned_uploads = find_abandoned_uploads(db)
        # Reap abandoned transcribed_pending rows quietly: the user never
        # got a job started, so the failure isn't operator-visible. Just
        # delete the row and clean up the input file.
        for job in abandoned:
            _delete_abandoned_transcribed(db, job)
        for job in abandoned_uploads:
            _delete_abandoned_upload(db, job)
        if abandoned or abandoned_uploads:
            db.commit()
            if abandoned:
                print(f"[REAPER] cleaned up {len(abandoned)} abandoned "
                      f"transcribed_pending job(s)")
            if abandoned_uploads:
                print(f"[REAPER] cleaned up {len(abandoned_uploads)} abandoned "
                      f"awaiting_upload job(s)")
        if not stuck and not orphans:
            return 0
        for job in stuck:
            reap_stuck_job(db, job, _reason_for(job))
        for job in orphans:
            reap_stuck_job(db, job, _reason_for_orphan(job))
        db.commit()
        # Detach so we can pass to background helpers without keeping the
        # session open. The fields we need are already populated.
        for job in stuck:
            db.expunge(job)
        for job in orphans:
            db.expunge(job)
        # Merge orphan reaps into the same notification batch as age-based
        # ones — operators care about "what got reaped this cycle", not
        # which sweep flagged it.
        stuck = stuck + orphans
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
