"""Job management — PostgreSQL backed."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database import Job, get_db


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
) -> str:
    """Create a new job and return its ID. `initial_status` is "processing"
    when there's worker capacity OR "queued" when the system / tenant is at
    its concurrency cap. The caller decides which based on live load."""
    if initial_status not in ("processing", "queued"):
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
        current_step="whisper" if initial_status == "processing" else "queued",
        progress=0,
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


_DELETABLE_STATUSES = {"processing", "queued", "error", "validation_failed"}


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
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(deletable_ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.tenant_id == tenant_id, Job.job_id.in_(deletable_ids)).delete(synchronize_session=False)
        db.commit()
        deleted = deletable_ids

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
