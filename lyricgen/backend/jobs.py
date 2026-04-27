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
) -> str:
    """Create a new job and return its ID."""
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist=artist,
        style=style,
        filename=filename,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
        status="processing",
        current_step="whisper",
        progress=0,
    )
    db.add(job)
    db.commit()
    return job_id


def get_job(db: Session, job_id: str, tenant_id: str = None) -> Optional[dict]:
    """Return a job dict or None if not found."""
    query = db.query(Job).filter(Job.job_id == job_id)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)
    job = query.first()
    return job.to_dict() if job else None


def get_job_model(db: Session, job_id: str) -> Optional[Job]:
    """Return the raw Job model instance."""
    return db.query(Job).filter(Job.job_id == job_id).first()


def get_all_jobs(db: Session, tenant_id: str = "default", limit: int = 200) -> list[dict]:
    """Return all jobs for a tenant, sorted by creation time (newest first)."""
    jobs = (
        db.query(Job)
        .filter(Job.tenant_id == tenant_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    return [j.to_list_dict() for j in jobs]


def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job. Creates its own DB session for thread safety."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if not job:
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
