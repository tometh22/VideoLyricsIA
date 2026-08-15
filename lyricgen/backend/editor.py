"""Durable editor documents, optimistic concurrency and collaboration locks."""

from __future__ import annotations

import math
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import EditorDocument, EditorVersion, Job, User
from segment_timing import canonicalize_editor_segments

EDITOR_REASONS = {"autosave", "manual", "restore", "approve", "conflict", "migration"}
EDITOR_CHECKPOINTS = EDITOR_REASONS | {"draft"}
MAX_SEGMENTS = 5000
MAX_TEXT_LENGTH = 2000
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
LOCK_SECONDS = 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def normalize_segments(value: Any) -> list[dict]:
    """Validate timings while preserving renderer/editor metadata."""
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
        # Extra JSON fields are part of the existing renderer contract
        # (`words`, `locked`, `review`, `pos`, `scale`, `rot`, future flags).
        # Normalise only the canonical values instead of silently deleting
        # metadata when a legacy job is migrated into Editor 2.0.
        normalized.append({
            **segment,
            "start": round(start, 4),
            "end": round(end, 4),
            "text": text,
        })
    # Canonicalize appended rows by timestamp, but preserve semantic source
    # order inside an anomalous overlap region and repair only that region.
    # This prevents a post-alignment regression from making playback jump to
    # a later lyric and then back to an earlier one.
    normalized = canonicalize_editor_segments(normalized)
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"segments payload cannot exceed {MAX_PAYLOAD_BYTES} bytes")
    return normalized


def segments_equivalent(left: Any, right: Any) -> bool:
    """Compare operator-owned lyric content across editor snapshots.

    Background/typography renders can refresh renderer metadata (word timing,
    review flags, or local row ids) without changing what the operator edited.
    Those changes must not turn an otherwise safe approval into a revision
    conflict. Text, timing, locks and layout remain part of the comparison.
    """
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return False

    ignored = {"_id", "id", "segment_id", "words", "review"}

    def comparable(segment: Any) -> dict:
        if not isinstance(segment, dict):
            return {"value": segment}
        return {key: value for key, value in segment.items() if key not in ignored}

    def sort_key(segment: Any) -> tuple[float, float, str]:
        if not isinstance(segment, dict):
            return (0.0, 0.0, "")
        return (
            float(segment.get("start") or 0),
            float(segment.get("end") or 0),
            str(segment.get("text") or ""),
        )

    ordered_left = sorted(left, key=sort_key)
    ordered_right = sorted(right, key=sort_key)
    for left_segment, right_segment in zip(ordered_left, ordered_right):
        if abs(float(left_segment.get("start") or 0) - float(right_segment.get("start") or 0)) > 1e-3:
            return False
        if abs(float(left_segment.get("end") or 0) - float(right_segment.get("end") or 0)) > 1e-3:
            return False
        if json.dumps(comparable(left_segment), sort_keys=True, ensure_ascii=False) != json.dumps(
            comparable(right_segment), sort_keys=True, ensure_ascii=False,
        ):
            return False
    return True


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


def _ensure_version(
    db: Session,
    document: EditorDocument,
    revision: int,
    segments: list[dict],
    user_id: int | None,
    reason: str,
    *,
    approved: bool = False,
) -> EditorVersion:
    existing = db.query(EditorVersion).filter(
        EditorVersion.job_id == document.job_id,
        EditorVersion.revision == revision,
    ).first()
    if existing:
        if existing.segments != segments:
            raise RuntimeError("editor_version_content_mismatch")
        if approved:
            existing.is_approved = True
            existing.reason = "approve"
        return existing
    version = EditorVersion(
        id=str(uuid.uuid4()), job_id=document.job_id, tenant_id=document.tenant_id,
        revision=revision, segments=segments, created_by=user_id,
        reason=reason, is_approved=approved,
    )
    db.add(version)
    db.flush()
    return version


def _prune_versions(db: Session, document: EditorDocument) -> None:
    drafts = (
        db.query(EditorVersion)
        .filter(EditorVersion.job_id == document.job_id, EditorVersion.is_approved.is_(False))
        .order_by(EditorVersion.revision.desc())
        .all()
    )
    for stale in drafts[50:]:
        db.delete(stale)


def _next_revision(db: Session, document: EditorDocument, *candidates: int) -> int:
    highest_version = db.query(func.max(EditorVersion.revision)).filter(
        EditorVersion.job_id == document.job_id,
    ).scalar()
    return max(int(highest_version or 0), int(document.revision or 0), *candidates) + 1


def ensure_document(db: Session, job_id: str, tenant_id: str, segments: list[dict]) -> EditorDocument:
    """Create the lazy document once, preserving the original transcription."""
    job = get_job_for_tenant(db, job_id, tenant_id)
    if not job:
        raise LookupError("job_not_found")
    normalized = normalize_segments(segments)
    document = (
        db.query(EditorDocument)
        .filter(EditorDocument.job_id == job_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if document:
        return document

    revision = int(getattr(job, "segments_revision", 0) or 0)
    document = EditorDocument(
        job_id=job_id,
        tenant_id=tenant_id,
        current_segments=normalized,
        original_segments=normalized,
        revision=revision,
        updated_at=now_utc(),
    )
    db.add(document)
    # Creation is one atomic bridge operation. Callers normally pass the Job
    # snapshot; synchronising here also makes direct/lazy creation race-safe
    # when an older Job row has not yet populated segments_json.
    job.segments_json = normalized
    job.segments_revision = revision
    db.flush()
    try:
        _ensure_version(db, document, revision, normalized, job.user_id, "migration")
    except RuntimeError:
        # Historical partial writers may have left an orphan checkpoint at
        # the Job revision. Preserve it and move the deployed Job snapshot to
        # the next free monotonic revision.
        revision = _next_revision(db, document, revision)
        document.revision = revision
        job.segments_revision = revision
        _ensure_version(db, document, revision, normalized, job.user_id, "migration")
    return document


def get_or_create_document(
    db: Session, job_id: str, tenant_id: str, segments: list[dict] | None = None,
) -> EditorDocument:
    job = db.query(Job).filter(
        Job.job_id == job_id, Job.tenant_id == tenant_id,
    ).populate_existing().with_for_update().first()
    if not job:
        raise LookupError("job_not_found")
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
        EditorDocument.tenant_id == tenant_id,
    ).populate_existing().with_for_update().first()
    if not document:
        if segments is None:
            raise LookupError("editor_document_not_found")
        return ensure_document(db, job_id, tenant_id, segments)

    job_segments = normalize_segments(job.segments_json or [])
    job_revision = int(getattr(job, "segments_revision", 0) or 0)
    document_segments = normalize_segments(document.current_segments or [])
    document_revision = int(document.revision or 0)
    job_needs_canonicalization = job.segments_json != job_segments
    document_needs_canonicalization = document.current_segments != document_segments

    if job_revision > document_revision:
        try:
            _ensure_version(db, document, job_revision, job_segments, job.user_id, "migration")
            target_revision = job_revision
        except RuntimeError:
            target_revision = _next_revision(db, document, job_revision, document_revision)
            _ensure_version(db, document, target_revision, job_segments, job.user_id, "migration")
            job.segments_revision = target_revision
        document.current_segments = job_segments
        document.revision = target_revision
        document.updated_at = now_utc()
        job.segments_json = job_segments
    elif document_revision > job_revision:
        job.segments_json = document_segments
        job.segments_revision = document_revision
        try:
            _ensure_version(db, document, document_revision, document_segments, document.updated_by, "migration")
        except RuntimeError:
            target_revision = _next_revision(db, document, job_revision, document_revision)
            document.revision = target_revision
            job.segments_revision = target_revision
            _ensure_version(db, document, target_revision, document_segments, document.updated_by, "migration")
    elif document_segments != job_segments:
        # Preserve both sides. Job is the currently deployed writer, so it
        # becomes current at a fresh revision while the divergent document is
        # retained as an immutable migration snapshot.
        old_revision = _next_revision(db, document, job_revision, document_revision)
        _ensure_version(db, document, old_revision, document_segments, document.updated_by, "migration")
        target_revision = old_revision + 1
        _ensure_version(db, document, target_revision, job_segments, job.user_id, "migration")
        document.current_segments = job_segments
        document.revision = target_revision
        document.updated_at = now_utc()
        job.segments_json = job_segments
        job.segments_revision = target_revision
    elif job_needs_canonicalization or document_needs_canonicalization:
        # A legacy document can already exist at the same revision as the Job
        # while both snapshots contain the old malformed order.  Normalizing
        # only the response would make the next reload resurrect the defect;
        # promote the canonical payload through the ordinary immutable history
        # so the repair is durable and auditable.
        old_revision = _next_revision(db, document, job_revision, document_revision)
        _ensure_version(db, document, old_revision, document_segments, document.updated_by, "migration")
        target_revision = old_revision + 1
        _ensure_version(db, document, target_revision, job_segments, job.user_id, "migration")
        document.current_segments = job_segments
        document.revision = target_revision
        document.updated_at = now_utc()
        job.segments_json = job_segments
        job.segments_revision = target_revision
    if segments is None:
        return document
    return document


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
    job: Job,
    document: EditorDocument,
    user_id: int,
    base_revision: int,
    segments: list[dict],
    reason: str = "autosave",
) -> tuple[EditorDocument, EditorVersion | None, bool]:
    if reason not in EDITOR_CHECKPOINTS:
        raise ValueError(f"unsupported checkpoint reason: {reason}")
    # Serialize writers at the document row. Without this lock two requests
    # can both observe the same revision and overwrite one another before the
    # optimistic check runs. PostgreSQL enforces the lease; SQLite keeps the
    # same code path for local tests.
    job = (
        db.query(Job)
        .filter(Job.job_id == job.job_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    document = (
        db.query(EditorDocument)
        .filter(EditorDocument.job_id == document.job_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    normalized = normalize_segments(segments)
    if document.revision != base_revision:
        # A background/typography render can advance the durable revision
        # while only refreshing renderer metadata. Treat that retry as a
        # semantic no-op; comparing the raw JSON here manufactured the 409
        # that the lyrics editor showed as a collaboration conflict.
        if document.current_segments == normalized or segments_equivalent(
            document.current_segments, normalized,
        ):
            version = None
            # A draft and its 5-second checkpoint can overlap in flight. If
            # the draft wins first, the checkpoint arrives with a stale base
            # but identical content. Preserve the checkpoint instead of
            # treating the safe idempotent retry as a versionless no-op.
            if reason != "draft":
                version = _ensure_version(
                    db, document, document.revision, normalized, user_id, reason,
                    approved=reason == "approve",
                )
                _prune_versions(db, document)
                db.flush()
            return document, version, False
        raise RuntimeError("editor_revision_conflict")
    if document.current_segments == normalized:
        version = None
        if reason != "draft":
            version = _ensure_version(
                db, document, document.revision, normalized, user_id, reason,
                approved=reason == "approve",
            )
            _prune_versions(db, document)
        db.flush()
        return document, version, False
    document.current_segments = normalized
    document.revision += 1
    document.updated_by = user_id
    document.updated_at = now_utc()
    job.segments_json = normalized
    job.segments_revision = document.revision
    version = None
    if reason != "draft":
        version = _ensure_version(
            db, document, document.revision, normalized, user_id, reason,
            approved=reason == "approve",
        )
        _prune_versions(db, document)
    db.flush()
    return document, version, True


def acquire_lock(db: Session, document: EditorDocument, user_id: int) -> dict:
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
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
    db.flush()
    return {
        "acquired": True,
        "user": _user_summary(db, user_id),
        "expires_at": document.lock_expires_at.isoformat(),
    }


def release_lock(db: Session, document: EditorDocument, user_id: int) -> bool:
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
    if document.lock_user_id not in (None, user_id):
        return False
    document.lock_user_id = None
    document.lock_expires_at = None
    db.flush()
    return True


def list_versions(db: Session, document: EditorDocument, limit: int = 50, offset: int = 0) -> list[dict]:
    rows = (
        db.query(EditorVersion)
        .filter(EditorVersion.job_id == document.job_id)
        .order_by(EditorVersion.revision.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 50))
        .all()
    )
    return [_version_summary(db, row) for row in rows]


def get_version(db: Session, document: EditorDocument, version_id: str) -> EditorVersion | None:
    return db.query(EditorVersion).filter(
        EditorVersion.id == version_id,
        EditorVersion.job_id == document.job_id,
        EditorVersion.tenant_id == document.tenant_id,
    ).first()


def sync_legacy_snapshot(
    db: Session, document: EditorDocument, user_id: int, segments: list[dict], revision: int,
) -> EditorDocument:
    """Mirror the existing Job.segments_json CAS path into Editor 2.0.

    Staging already has a battle-tested `/jobs/{id}/save-segments` endpoint.
    Keeping this adapter lets old and new clients share one durable version
    history while the endpoint is migrated incrementally.
    """
    normalized = normalize_segments(segments)
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
    if document.revision == revision and document.current_segments == normalized:
        return document
    if revision <= document.revision:
        raise RuntimeError("editor_revision_conflict")
    document.current_segments = normalized
    document.revision = revision
    document.updated_by = user_id
    document.updated_at = now_utc()
    _ensure_version(db, document, revision, normalized, user_id, "autosave")
    _prune_versions(db, document)
    db.flush()
    return document


def restore_version(
    db: Session, job: Job, document: EditorDocument, user_id: int, version_id: str, base_revision: int,
) -> tuple[EditorDocument, EditorVersion]:
    version = get_version(db, document, version_id)
    if not version:
        raise LookupError("editor_version_not_found")
    document, restored, _ = save_document(
        db, job, document, user_id, base_revision, version.segments, "restore",
    )
    return document, restored


def resolve_conflict(
    db: Session, job: Job, document: EditorDocument, user_id: int,
    server_revision: int, strategy: str, segments: list[dict] | None = None,
) -> tuple[EditorDocument, EditorVersion | None, bool]:
    job = (
        db.query(Job)
        .filter(Job.job_id == job.job_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
    if document.revision != server_revision:
        raise RuntimeError("editor_revision_conflict")
    if strategy == "use_server":
        return document, None, False
    if strategy != "save_local_as_new" or segments is None:
        raise ValueError("invalid conflict resolution strategy")
    _ensure_version(
        db, document, document.revision, document.current_segments,
        document.updated_by, "conflict",
    )
    return save_document(
        db, job, document, user_id, server_revision, segments, "conflict",
    )


def approve_document(
    db: Session,
    job: Job,
    user_id: int,
    *,
    editor_revision: int | None = None,
    editor_version_id: str | None = None,
) -> tuple[EditorDocument, EditorVersion]:
    """Freeze and approve the exact current persisted snapshot.

    A version id is not permission to render an old snapshot after somebody
    else saved. Both selectors must still identify the document's current
    revision, otherwise approval fails closed with the standard conflict.
    """
    job = (
        db.query(Job)
        .filter(Job.job_id == job.job_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    document = get_or_create_document(
        db, job.job_id, job.tenant_id, job.segments_json or [],
    )
    selected = None
    selected_is_equivalent_current = False
    if editor_version_id:
        selected = get_version(db, document, editor_version_id)
        if not selected:
            raise LookupError("editor_version_not_found")
        if not segments_equivalent(selected.segments, document.current_segments):
            raise RuntimeError("editor_revision_conflict")
        # A background/typography operation may have advanced the revision
        # while preserving the operator-owned lyrics. Approve the current
        # equivalent snapshot as a fresh version instead of rejecting it.
        if selected.revision != document.revision:
            selected_is_equivalent_current = True
            selected = None
    if editor_revision is not None and editor_revision != document.revision:
        if not selected_is_equivalent_current:
            raise RuntimeError("editor_revision_conflict")
    if editor_version_id is None and editor_revision is None:
        raise ValueError("editor approval selector required")
    version = selected or _ensure_version(
        db, document, document.revision, document.current_segments,
        user_id, "approve", approved=True,
    )
    version.is_approved = True
    version.reason = "approve"
    job.segments_json = normalize_segments(document.current_segments)
    job.segments_revision = document.revision
    db.flush()
    return document, version
