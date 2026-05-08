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
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from database import AuditLog, Job, SessionLocal

logger = logging.getLogger("genly.reaper")

# Reaped jobs land at this status (the operator-facing status). Not
# "abandoned" because the existing UI / batches don't know that one.
_REAPED_STATUS = "error"

# How old a job needs to be before we consider it dead.
_DEFAULT_THRESHOLD_MIN = int(os.environ.get("REAPER_THRESHOLD_MIN", "100"))

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
        if not stuck:
            return 0
        for job in stuck:
            reap_stuck_job(db, job, _reason_for(job))
        db.commit()
        # Detach so we can pass to background helpers without keeping the
        # session open. The fields we need are already populated.
        for job in stuck:
            db.expunge(job)
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
