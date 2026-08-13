"""Safe, durable lyrics-editor persistence.

The editor is an optimistic-concurrency client: every write carries the
revision it was based on.  The row lock makes the check-and-write atomic, and
EditorVersion keeps every non-draft checkpoint recoverable.
"""

from __future__ import annotations

import json
import math

from sqlalchemy.orm import Session

from database import EditorVersion, Job

MAX_SEGMENTS = 5000
MAX_TEXT_LENGTH = 2000
CHECKPOINTS = {"migration", "autosave", "manual", "approve", "conflict"}


class EditorConflict(RuntimeError):
    def __init__(self, job: Job):
        super().__init__("editor_revision_conflict")
        self.job = job


def normalize_segments(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("segments must be an array")
    if len(value) > MAX_SEGMENTS:
        raise ValueError(f"segments cannot exceed {MAX_SEGMENTS} items")
    result = []
    for index, segment in enumerate(value):
        if not isinstance(segment, dict):
            raise ValueError(f"segment {index} must be an object")
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            raise ValueError(f"segment {index} has invalid timing") from None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"segment {index} has invalid timing")
        text = segment.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"segment {index} text is too long")
        result.append({**segment, "start": round(start, 4), "end": round(end, 4), "text": text})
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 5 * 1024 * 1024:
        raise ValueError("segments payload is too large")
    return result


def editor_job(db: Session, job_id: str, user: dict, *, lock: bool = False) -> Job | None:
    query = db.query(Job).filter(Job.job_id == job_id, Job.tenant_id == user["tenant_id"])
    if user.get("role") != "admin":
        query = query.filter(Job.user_id == user["id"])
    if lock:
        query = query.with_for_update()
    return query.first()


def _version(db: Session, job: Job, reason: str, user_id: int | None):
    existing = db.query(EditorVersion).filter(
        EditorVersion.job_id == job.job_id,
        EditorVersion.revision == int(job.segments_revision or 0),
    ).first()
    if existing:
        return existing
    version = EditorVersion(
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        revision=int(job.segments_revision or 0),
        segments=job.segments_json or [],
        created_by=user_id,
        reason=reason,
    )
    db.add(version)
    db.flush()
    return version


def ensure_editor_snapshot(db: Session, job: Job, user_id: int | None = None):
    """Create the revision-zero checkpoint without changing existing content."""
    if job.segments_json is None:
        job.segments_json = []
        job.segments_revision = 0
    _version(db, job, "migration", user_id or job.user_id)
    db.flush()


def serialize_editor(db: Session, job: Job) -> dict:
    ensure_editor_snapshot(db, job)
    latest = db.query(EditorVersion).filter(
        EditorVersion.job_id == job.job_id,
        EditorVersion.revision == int(job.segments_revision or 0),
    ).first()
    return {
        "job_id": job.job_id,
        "revision": int(job.segments_revision or 0),
        "segments": job.segments_json or [],
        "updated_at": latest.created_at.isoformat() if latest and latest.created_at else None,
        "updated_by": {"id": latest.created_by} if latest and latest.created_by else None,
    }


def save_editor(
    db: Session,
    job: Job,
    user_id: int,
    base_revision: int,
    segments,
    reason: str = "autosave",
):
    if reason not in CHECKPOINTS:
        raise ValueError("unsupported editor checkpoint")
    # Re-read and lock the authoritative row. Two concurrent requests cannot
    # both pass the revision check and then overwrite one another.
    locked = db.query(Job).filter(Job.job_id == job.job_id).with_for_update().one()
    normalized = normalize_segments(segments)
    current_revision = int(locked.segments_revision or 0)
    current_segments = locked.segments_json or []
    if current_revision != int(base_revision):
        if current_segments == normalized:
            return locked, None, False
        raise EditorConflict(locked)
    if current_segments == normalized:
        version = _version(db, locked, reason, user_id) if reason != "autosave" else None
        db.flush()
        return locked, version, False
    locked.segments_json = normalized
    locked.segments_revision = current_revision + 1
    version = _version(db, locked, reason, user_id)
    db.flush()
    return locked, version, True


def conflict_payload(job: Job) -> dict:
    return {
        "detail": "editor_revision_conflict",
        "server_revision": int(job.segments_revision or 0),
        "server_segments": job.segments_json or [],
    }
