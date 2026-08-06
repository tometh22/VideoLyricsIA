"""Durable editor documents, optimistic concurrency and collaboration locks."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from database import EditorDocument, EditorVersion, Job, User

EDITOR_REASONS = {"autosave", "manual", "restore", "approve"}
MAX_SEGMENTS = 5000
MAX_TEXT_LENGTH = 5000
LOCK_SECONDS = 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def normalize_segments(value: Any) -> list[dict]:
    """Validate the small segment contract shared by editor and renderer."""
    if not isinstance(value, list):
        raise ValueError("segments must be an array")
    if len(value) > MAX_SEGMENTS:
        raise ValueError(f"segments cannot exceed {MAX_SEGMENTS} items")

    normalized: list[dict] = []
    for index, segment in enumerate(value):
        if not isinstance(segment, dict):
            raise ValueError(f"segment {index} must be an object")
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            raise ValueError(f"segment {index} has invalid timing") from None
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"segment {index} has non-finite timing")
        if start < 0 or end <= start:
            raise ValueError(f"segment {index} must have end greater than start")
        text = segment.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"segment {index} text is too long")
        normalized.append({"start": round(start, 4), "end": round(end, 4), "text": text})
    return normalized


def get_job_for_tenant(db: Session, job_id: str, tenant_id: str) -> Job | None:
    return (
        db.query(Job)
        .filter(Job.job_id == job_id, Job.tenant_id == tenant_id)
        .first()
    )


def _user_summary(db: Session, user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"id": user_id}
    return {"id": user.id, "username": user.username}


def _version_summary(db: Session, version: EditorVersion) -> dict:
    return {
        "id": version.id,
        "revision": version.revision,
        "reason": version.reason,
        "is_approved": bool(version.is_approved),
        "created_at": _aware(version.created_at).isoformat() if version.created_at else None,
        "created_by": _user_summary(db, version.created_by),
    }


def ensure_document(db: Session, job_id: str, tenant_id: str, segments: list[dict]) -> EditorDocument:
    """Create the lazy document once, preserving the original transcription."""
    job = get_job_for_tenant(db, job_id, tenant_id)
    if not job:
        raise LookupError("job_not_found")
    normalized = normalize_segments(segments)
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).first()
    if document:
        return document

    document = EditorDocument(
        job_id=job_id,
        tenant_id=tenant_id,
        current_segments=normalized,
        original_segments=normalized,
        revision=0,
        updated_at=now_utc(),
    )
    db.add(document)
    db.flush()
    db.add(EditorVersion(
        id=str(uuid.uuid4()),
        job_id=job_id,
        tenant_id=tenant_id,
        revision=0,
        segments=normalized,
        created_by=job.user_id,
        reason="autosave",
    ))
    db.commit()
    return document


def get_or_create_document(
    db: Session, job_id: str, tenant_id: str, segments: list[dict] | None = None,
) -> EditorDocument:
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
        EditorDocument.tenant_id == tenant_id,
    ).first()
    if document:
        return document
    if segments is None:
        raise LookupError("editor_document_not_found")
    return ensure_document(db, job_id, tenant_id, segments)


def serialize_document(db: Session, document: EditorDocument) -> dict:
    lock_expires = _aware(document.lock_expires_at)
    lock_active = bool(lock_expires and lock_expires > now_utc())
    return {
        "job_id": document.job_id,
        "revision": document.revision,
        "segments": document.current_segments,
        "original_segments": document.original_segments,
        "updated_at": _aware(document.updated_at).isoformat() if document.updated_at else None,
        "updated_by": _user_summary(db, document.updated_by),
        "lock": {
            "active": lock_active,
            "user": _user_summary(db, document.lock_user_id) if lock_active else None,
            "expires_at": lock_expires.isoformat() if lock_active else None,
        },
    }


def save_document(
    db: Session,
    document: EditorDocument,
    user_id: int,
    base_revision: int,
    segments: list[dict],
    reason: str = "autosave",
) -> tuple[EditorDocument, EditorVersion | None]:
    if reason not in EDITOR_REASONS:
        raise ValueError(f"unsupported checkpoint reason: {reason}")
    # Serialize writers at the document row. Without this lock two requests
    # can both observe the same revision and overwrite one another before the
    # optimistic check runs. PostgreSQL enforces the lease; SQLite keeps the
    # same code path for local tests.
    document = (
        db.query(EditorDocument)
        .filter(EditorDocument.job_id == document.job_id)
        .with_for_update()
        .one()
    )
    if document.revision != base_revision:
        raise RuntimeError("editor_revision_conflict")
    normalized = normalize_segments(segments)
    document.current_segments = normalized
    document.revision += 1
    document.updated_by = user_id
    document.updated_at = now_utc()
    version = EditorVersion(
        id=str(uuid.uuid4()),
        job_id=document.job_id,
        tenant_id=document.tenant_id,
        revision=document.revision,
        segments=normalized,
        created_by=user_id,
        reason=reason,
        is_approved=reason == "approve",
    )
    db.add(version)
    db.flush()

    # Retain the latest 50 checkpoints, but never delete an approved snapshot.
    versions = (
        db.query(EditorVersion)
        .filter(EditorVersion.job_id == document.job_id, EditorVersion.is_approved.is_(False))
        .order_by(EditorVersion.revision.desc())
        .all()
    )
    for stale in versions[50:]:
        db.delete(stale)
    db.commit()
    return document, version


def acquire_lock(db: Session, document: EditorDocument, user_id: int) -> dict:
    now = now_utc()
    expires = _aware(document.lock_expires_at)
    if expires and expires > now and document.lock_user_id not in (None, user_id):
        return {
            "acquired": False,
            "user": _user_summary(db, document.lock_user_id),
            "expires_at": expires.isoformat(),
        }
    document.lock_user_id = user_id
    document.lock_expires_at = now + timedelta(seconds=LOCK_SECONDS)
    db.commit()
    return {
        "acquired": True,
        "user": _user_summary(db, user_id),
        "expires_at": document.lock_expires_at.isoformat(),
    }


def release_lock(db: Session, document: EditorDocument, user_id: int) -> bool:
    if document.lock_user_id not in (None, user_id):
        return False
    document.lock_user_id = None
    document.lock_expires_at = None
    db.commit()
    return True


def list_versions(db: Session, document: EditorDocument, limit: int = 50) -> list[dict]:
    rows = (
        db.query(EditorVersion)
        .filter(EditorVersion.job_id == document.job_id)
        .order_by(EditorVersion.revision.desc())
        .limit(min(max(limit, 1), 50))
        .all()
    )
    return [_version_summary(db, row) | {"segments": row.segments} for row in rows]


def sync_legacy_snapshot(
    db: Session, document: EditorDocument, user_id: int, segments: list[dict], revision: int,
) -> EditorDocument:
    """Mirror the existing Job.segments_json CAS path into Editor 2.0.

    Staging already has a battle-tested `/jobs/{id}/save-segments` endpoint.
    Keeping this adapter lets old and new clients share one durable version
    history while the endpoint is migrated incrementally.
    """
    normalized = normalize_segments(segments)
    if document.revision >= revision and document.current_segments == normalized:
        return document
    document.current_segments = normalized
    document.revision = revision
    document.updated_by = user_id
    document.updated_at = now_utc()
    exists = db.query(EditorVersion).filter(
        EditorVersion.job_id == document.job_id,
        EditorVersion.revision == revision,
    ).first()
    if not exists:
        db.add(EditorVersion(
            id=str(uuid.uuid4()), job_id=document.job_id,
            tenant_id=document.tenant_id, revision=revision,
            segments=normalized, created_by=user_id, reason="autosave",
        ))
    db.commit()
    return document


def restore_version(
    db: Session, document: EditorDocument, user_id: int, version_id: str, base_revision: int,
) -> tuple[EditorDocument, EditorVersion]:
    if document.revision != base_revision:
        raise RuntimeError("editor_revision_conflict")
    version = db.query(EditorVersion).filter(
        EditorVersion.id == version_id,
        EditorVersion.job_id == document.job_id,
        EditorVersion.tenant_id == document.tenant_id,
    ).first()
    if not version:
        raise LookupError("editor_version_not_found")
    return save_document(db, document, user_id, base_revision, version.segments, "restore")
