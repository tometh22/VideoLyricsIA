"""Job management — PostgreSQL backed."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import storage
from database import Job, get_db

_logger = logging.getLogger("genly.jobs")


def create_job(
    db: Session,
    artist: str,
    style: str,
    filename: str,
    user_id: int,
    tenant_id: str = "default",
    delivery_profile: str = "youtube",
    umg_spec: Optional[dict] = None,
    initial_status: str = "processing",
    song_title: str = "",
    input_r2_key: Optional[str] = None,
) -> str:
    """Create a new job and return its ID.

    `initial_status` values:
      - "processing": worker is going to pick this up immediately.
      - "queued": waiting in line behind other jobs at the tenant cap.
      - "transcribed_pending": user has called /transcribe; the audio is
        persisted but the user is still editing lyrics. /generate will
        flip the row to processing/queued once the segments come in.
      - "awaiting_upload": browser is still PUTting bytes directly to
        R2 via a presigned URL. /transcribe-uploaded promotes to
        transcribed_pending once the upload completes.
    """
    valid_states = (
        "processing", "queued", "transcribed_pending", "awaiting_upload",
    )
    if initial_status not in valid_states:
        raise ValueError(f"unsupported initial_status {initial_status!r}")
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist=artist,
        song_title=song_title or None,
        style=style,
        filename=filename,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
        status=initial_status,
        current_step=(
            "whisper" if initial_status == "processing"
            else "queued" if initial_status == "queued"
            else "uploading" if initial_status == "awaiting_upload"
            else "editing"
        ),
        progress=0,
        input_r2_key=input_r2_key,
    )
    db.add(job)
    db.commit()
    return job_id


def get_job(
    db: Session,
    job_id: str,
    tenant_id: str = None,
    user_id: int = None,
) -> Optional[dict]:
    """Return a job dict or None if not found.

    Pass user_id (in addition to tenant_id) for self-serve callers — it
    closes the IDOR where many self-registered users land in
    tenant_id="default" (e.g. the admin tenant) and could otherwise see
    each other's jobs by enumerating job_ids.
    """
    query = db.query(Job).filter(Job.job_id == job_id)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)
    if user_id is not None:
        query = query.filter(Job.user_id == user_id)
    job = query.first()
    return job.to_dict() if job else None


def get_job_model(db: Session, job_id: str) -> Optional[Job]:
    """Return the raw Job model instance."""
    return db.query(Job).filter(Job.job_id == job_id).first()


def touch_user_activity(db: Session, job: Job) -> None:
    """Bump last_user_activity_at = now(). Caller commits.

    Used as the staleness anchor for find_abandoned_transcribed: any
    authenticated user touch on the job (POST /save-segments, GET /status,
    etc) refreshes the timestamp, so the reaper only barre genuinely
    abandoned sessions instead of slow batch-edit sessions.
    """
    from datetime import datetime, timezone
    job.last_user_activity_at = datetime.now(timezone.utc)


def _delete_r2_objects(job: Job) -> None:
    """Best-effort delete all R2 objects tied to a job.

    Called before the DB row is removed so we still have the keys.
    Errors are swallowed — R2 cleanup must never block the DB delete.
    """
    keys: list[str] = []
    if job.input_r2_key:
        keys.append(job.input_r2_key)
    s3 = job.s3_keys or {}
    if isinstance(s3, dict):
        keys.extend(v for v in s3.values() if isinstance(v, str) and v)
    for key in keys:
        try:
            storage.delete_object(key)
        except Exception as exc:
            _logger.warning("R2 delete failed key=%r: %s", key, exc)


# Estados que el operador puede borrar desde la UI. La idea: cualquier
# estado "en progreso o atascado" es deletable; solo protegemos done /
# pending_review por audit trail + workflow.
#
# Caso real que motivó incluir "editing" y "transcribed_pending": un job
# que entra a edit_pipeline y el worker muere (timeout, OOM, deploy) se
# queda colgado en status="editing" para siempre porque no hay heartbeat
# de auto-fail. Sin esto, el operador no puede limpiar la fila y termina
# pidiendo al admin que actualice la DB a mano.
_DELETABLE_STATUSES = {
    "processing", "queued", "error", "validation_failed",
    "editing",              # edit pipeline corriendo o colgada
    "transcribed_pending",  # tras pérdida de wizard state
}


def delete_job(db: Session, job_id: str, tenant_id: str) -> tuple[bool, str]:
    """Hard-delete a job row owned by `tenant_id`. Returns (ok, reason).

    Safety: only stuck/failed jobs can be deleted — done/pending_review jobs
    must be kept for the audit trail (UMG compliance + plan-quota counting).
    The operator's intent here is cleaning up junk, not erasing approved
    deliveries.

    AIProvenance has a NOT NULL FK to jobs.job_id without ON DELETE CASCADE,
    so we have to clean up its rows manually before the parent delete or
    Postgres raises IntegrityError → 500. Failed/stuck jobs may have started
    accumulating provenance entries (e.g. lyrics_reference_fetch attempts)
    even though the render never completed.
    """
    from database import AIProvenance  # local import to avoid circular

    job = db.query(Job).filter(Job.job_id == job_id, Job.tenant_id == tenant_id).first()
    if not job:
        return False, "not_found"
    if job.status not in _DELETABLE_STATUSES:
        return False, f"protected_status:{job.status}"
    _delete_r2_objects(job)
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete(synchronize_session=False)
    db.delete(job)
    db.commit()
    return True, "ok"


def bulk_delete_jobs(db: Session, job_ids: list[str], tenant_id: str) -> dict:
    """Delete many jobs in one transaction. Returns {deleted: [...], skipped: {id: reason}}.

    Skipped reasons: 'not_found', 'protected_status:<status>'. The endpoint
    surfaces this dict so the operator sees exactly which IDs were ignored
    and why.
    """
    from database import AIProvenance

    deleted: list[str] = []
    skipped: dict[str, str] = {}
    if not job_ids:
        return {"deleted": deleted, "skipped": skipped}

    rows = (
        db.query(Job)
        .filter(Job.tenant_id == tenant_id, Job.job_id.in_(job_ids))
        .all()
    )
    found_ids = {r.job_id for r in rows}
    for jid in job_ids:
        if jid not in found_ids:
            skipped[jid] = "not_found"

    deletable_ids: list[str] = []
    for r in rows:
        if r.status not in _DELETABLE_STATUSES:
            skipped[r.job_id] = f"protected_status:{r.status}"
        else:
            deletable_ids.append(r.job_id)

    if deletable_ids:
        # Collect R2 keys from already-fetched rows BEFORE the bulk DELETE
        # removes them — the bulk query returns no data after deletion.
        deletable_set = set(deletable_ids)
        r2_rows = [r for r in rows if r.job_id in deletable_set]

        db.query(AIProvenance).filter(AIProvenance.job_id.in_(deletable_ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.tenant_id == tenant_id, Job.job_id.in_(deletable_ids)).delete(synchronize_session=False)
        db.commit()
        deleted = deletable_ids

        # Best-effort R2 cleanup after successful DB commit.
        for r in r2_rows:
            _delete_r2_objects(r)

    return {"deleted": deleted, "skipped": skipped}


def get_all_jobs(
    db: Session,
    tenant_id: str = "default",
    limit: int = 200,
    user_id: int = None,
) -> list[dict]:
    """Return all jobs for a tenant, sorted by creation time (newest first).

    Pass user_id for self-serve callers — see get_job() for rationale.
    """
    query = db.query(Job).filter(Job.tenant_id == tenant_id)
    if user_id is not None:
        query = query.filter(Job.user_id == user_id)
    jobs = (
        query.order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    return [j.to_list_dict() for j in jobs]


_TERMINAL_STATUSES = ("done", "error", "rejected", "validation_failed")


def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job. Creates its own DB session for thread safety.

    A status update that targets a non-terminal state is REFUSED for jobs
    already in a terminal state. This guards against:
      - A stale worker thread flushing progress=55 / status="processing"
        after a reaper marked the job error → resurrects a closed job.
      - Two workers picking the same job and both calling
        update_job(status="processing") → double-processing.

    Updates that target a terminal state OR fields that are safe to set on
    terminal jobs (s3_keys, youtube_data, validation_result, etc.) are
    always applied — the reaper itself relies on the terminal-update path.
    """
    from database import SessionLocal

    target_status = kwargs.get("status")
    target_is_terminal = target_status in _TERMINAL_STATUSES

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return

        # Refuse non-terminal mutations of terminal jobs.
        if (
            job.status in _TERMINAL_STATUSES
            and not target_is_terminal
            and target_status is not None
        ):
            return

        for key, value in kwargs.items():
            if key == "files":
                # Handle the legacy files dict format
                if isinstance(value, dict):
                    if "video_url" in value:
                        job.video_url = value["video_url"]
                    if "short_url" in value:
                        job.short_url = value["short_url"]
                    if "thumbnail_url" in value:
                        job.thumbnail_url = value["thumbnail_url"]
            elif key == "youtube":
                job.youtube_data = value
            elif hasattr(job, key):
                setattr(job, key, value)

        # Heartbeat for the reaper. Any progress update means the worker is
        # alive (even when progress is the same value as before — the call
        # itself proves liveness). reaper.find_stalled_renders flips a
        # processing job to error when this timestamp goes stale, catching
        # workers SIGKILLed during non-AI steps where there is no in-flight
        # AIProvenance row to anchor find_orphan_polling_jobs.
        if "progress" in kwargs:
            job.last_progress_at = datetime.now(timezone.utc)

        # Mark completed_at when pipeline finishes (done or pending_review)
        if kwargs.get("status") in ("done", "pending_review") and not job.completed_at:
            job.completed_at = datetime.now(timezone.utc)

        db.commit()
    except Exception:
        # Without an explicit rollback the session is returned to the pool
        # holding an open transaction; pool_pre_ping only catches
        # disconnects, not in-tx errors, so the next caller can hit
        # "current transaction is aborted, commands ignored until end of
        # transaction block".
        db.rollback()
        raise
    finally:
        db.close()


def get_all_jobs_admin(db: Session, limit: int = 500, offset: int = 0) -> list[dict]:
    """Return all jobs across all tenants (admin only)."""
    jobs = (
        db.query(Job)
        .order_by(Job.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [j.to_dict() for j in jobs]


def get_jobs_stats(db: Session, tenant_id: str = None) -> dict:
    """Get aggregate stats. If tenant_id is None, returns global stats."""
    query = db.query(Job)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)

    total = query.count()
    done = query.filter(Job.status == "done").count()
    errors = query.filter(Job.status == "error").count()
    processing = query.filter(Job.status == "processing").count()

    return {
        "total": total,
        "done": done,
        "errors": errors,
        "processing": processing,
    }
